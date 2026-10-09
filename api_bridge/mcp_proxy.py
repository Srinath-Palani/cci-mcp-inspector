"""
Same-origin streaming proxy for remote MCP servers.

Why this exists
---------------
Remote MCP servers are designed for server-to-server use and almost never send
`Access-Control-Allow-Origin`, so the browser MCP client's `fetch` fails with the
opaque "Failed to fetch" before a single byte of MCP is exchanged. Routing that
traffic through this backend removes the cross-origin condition entirely: the
browser talks to its own origin, and this process — which is not subject to CORS —
talks to the server.

URL shape
---------
    /mcp-proxy/<scheme>/<host[:port]><target-path>[?<target-query>]
    e.g. /mcp-proxy/https/api.example.com/mcp

The scheme is a path segment rather than a `?target=` parameter because both the
MCP TypeScript SDK and `strict-url-sanitise` rebuild URLs from the pathname:
`sanitizeUrl` percent-encodes `:` (so `.../https://host/mcp` is not idempotent),
and a query parameter is discarded the moment the SDK resolves a relative URL.
The segment form survives both.

Known limitations (deliberate, not oversights)
---------------------------------------------
* **Browser OAuth does not work through the proxy.** `use-mcp` derives its OAuth
  storage keys and the RFC 8707 `resource` parameter from the URL it is given, so
  in proxy mode metadata discovery points at this backend and the audience is
  wrong. `WWW-Authenticate` is forwarded so 401s stay diagnosable, but the flow
  cannot complete. For authenticated CORS-blocked servers use the backend OAuth
  proxy (`POST /api/oauth/start`) and pass the resulting token as a bearer token.
* No WebSocket or stdio transports, no client certificates.
* Cookies are stripped in both directions, and `Origin` / `Referer` are not
  forwarded — this proxy is not a general-purpose browser proxy.
* No SSE replay buffer here: `Last-Event-ID` is forwarded, and replay is entirely
  the upstream server's business.
* The SSRF guard resolves DNS and then hands the hostname to httpx, which resolves
  it again. That leaves a DNS-rebinding TOCTOU window. Closing it requires pinning
  the connection to a validated IP while keeping SNI/Host intact; it is not closed
  here, and the guard should be treated as defense in depth for a developer tool
  rather than a hard boundary.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
from typing import AsyncIterator, Dict, Optional, Union
from urllib.parse import urljoin, urlsplit

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

router = APIRouter()

PROXY_PREFIX = "/mcp-proxy"
ALLOWED_SCHEMES = ("http", "https")
MAX_TARGET_URL_LENGTH = 2048

# read=None is mandatory: an SSE stream is idle for as long as the server has
# nothing to say, and any read timeout would tear it down mid-session.
PROXY_TIMEOUT = httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)

# Set MCP_PROXY_ALLOW_PRIVATE=1 to proxy to loopback/private addresses. Needed to
# test against a locally running MCP server; keep it off otherwise.
_ALLOW_PRIVATE_ENV = "MCP_PROXY_ALLOW_PRIVATE"


# ── Header policy ────────────────────────────────────────────────────────────

_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)

# Browser → upstream is an allowlist (default deny). A remote MCP server must not
# be able to see the Inspector's origin, cookies, or any browser fingerprinting
# header just because the request happened to pass through here.
_REQUEST_HEADER_ALLOWLIST = frozenset(
    {
        "accept",
        "accept-language",
        "cache-control",
        "content-type",
        "authorization",
        "mcp-session-id",
        "mcp-protocol-version",
        "last-event-id",
    }
)

# Upstream → browser is a denylist (default allow) so MCP headers this proxy has
# never heard of still reach the client. Everything here would either corrupt the
# response (we re-frame the body) or let the upstream override our own policy.
_RESPONSE_HEADER_DENY_EXACT = frozenset(
    {
        "content-encoding",  # we request identity and stream raw bytes
        "content-length",  # length changes when the SSE body is rewritten
        "strict-transport-security",
        "content-security-policy",
        "content-security-policy-report-only",
        "public-key-pins",
        "public-key-pins-report-only",
        "alt-svc",
    }
    | _HOP_BY_HOP
)

# access-control-*: dropped so an upstream CORS header cannot conflict with the
# CORSMiddleware headers this app adds. set-cookie*: never relayed to the browser.
_RESPONSE_HEADER_DENY_PREFIXES = ("set-cookie", "access-control-")

# Documented contract: these must reach the browser or MCP breaks outright.
# `Mcp-Session-Id` carries the session, `WWW-Authenticate` carries the OAuth
# challenge. They are listed here to be asserted in tests, not to be re-added —
# the denylist above already lets them through.
REQUIRED_PASSTHROUGH_RESPONSE_HEADERS = (
    "mcp-session-id",
    "mcp-protocol-version",
    "www-authenticate",
    "retry-after",
)


# ── Shared client ────────────────────────────────────────────────────────────

_client: Optional[httpx.AsyncClient] = None


def get_client() -> httpx.AsyncClient:
    """The shared upstream client. Created lazily; closed by close_client()."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=PROXY_TIMEOUT,
            # Redirects are rewritten to proxy paths instead of followed, so a
            # 302 to an internal address cannot bypass the SSRF guard.
            follow_redirects=False,
            http2=False,
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


# ── Target validation ────────────────────────────────────────────────────────

class ProxyTargetError(Exception):
    """Target URL rejected. `status` is the response status to send."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


def _allow_private() -> bool:
    return os.environ.get(_ALLOW_PRIVATE_ENV, "").strip().lower() in ("1", "true", "yes")


def _is_blocked_address(ip: Union[ipaddress.IPv4Address, ipaddress.IPv6Address]) -> bool:
    """
    True for any address that must not be reachable through the proxy.

    is_link_local covers the cloud metadata endpoints (169.254.169.254 and
    fe80::/10), which are the highest-value SSRF targets on a hosted box.
    """
    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


async def validate_target(url: str, self_authority: Optional[str] = None) -> None:
    """
    Reject targets the proxy must not fetch.

    Called for the initial target and again for every URL the proxy rewrites
    (redirect `Location`, SSE `endpoint`), because a rewrite is a new fetch
    target that has not been checked before.

    Raises ProxyTargetError.
    """
    if len(url) > MAX_TARGET_URL_LENGTH:
        raise ProxyTargetError("Target URL is too long", 400)

    parts = urlsplit(url)
    if parts.scheme not in ALLOWED_SCHEMES:
        raise ProxyTargetError(
            f"Unsupported scheme '{parts.scheme}'. Only http and https can be proxied.", 400
        )
    if not parts.netloc:
        raise ProxyTargetError("Target URL has no host", 400)
    if "@" in parts.netloc:
        raise ProxyTargetError("Credentials in the target URL are not allowed", 400)

    hostname = parts.hostname
    if not hostname:
        raise ProxyTargetError("Target URL has no host", 400)

    # Self-loop guard: proxying to ourselves is either a mistake or an attempt to
    # build an amplification chain.
    if self_authority and parts.netloc.lower() == self_authority.lower():
        raise ProxyTargetError("Refusing to proxy to this server", 403)
    if parts.path.startswith(PROXY_PREFIX):
        raise ProxyTargetError("Refusing to proxy to another proxy path", 403)

    if _allow_private():
        return

    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        infos = await asyncio.to_thread(
            socket.getaddrinfo, hostname, port, 0, socket.SOCK_STREAM
        )
    except socket.gaierror as exc:
        raise ProxyTargetError(f"Could not resolve host '{hostname}': {exc}", 400) from exc

    if not infos:
        raise ProxyTargetError(f"Could not resolve host '{hostname}'", 400)

    # Every resolved address must be public: a hostname resolving to both a public
    # and a private address would otherwise be a trivial bypass.
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            raise ProxyTargetError(f"Unexpected address for '{hostname}'", 400) from None
        if _is_blocked_address(ip):
            raise ProxyTargetError(
                f"Refusing to proxy to non-public address {ip} "
                f"(set {_ALLOW_PRIVATE_ENV}=1 to allow this for local testing)",
                403,
            )


def to_proxy_path(url: str) -> str:
    """Absolute upstream URL → origin-relative proxy path."""
    parts = urlsplit(url)
    path = parts.path or "/"
    proxied = f"{PROXY_PREFIX}/{parts.scheme}/{parts.netloc}{path}"
    if parts.query:
        proxied = f"{proxied}?{parts.query}"
    return proxied


def _build_target_url(scheme: str, rest: str, query: str) -> str:
    target = f"{scheme}://{rest}"
    if query:
        target = f"{target}?{query}"
    return target


# ── SSE `endpoint` rewriting ─────────────────────────────────────────────────

async def _rewrite_sse_endpoint(
    stream: AsyncIterator[bytes],
    base_url: str,
    self_authority: Optional[str],
) -> AsyncIterator[bytes]:
    """
    Rewrite the legacy SSE transport's `endpoint` event to a proxy path.

    The SDK's SSEClientTransport compares the endpoint's origin against the
    connection's origin and throws "Endpoint origin does not match connection
    origin" on a mismatch. So an absolute endpoint from the real server is
    rejected outright, and a relative one resolves against the browser origin and
    POSTs to a path nothing serves. Both are broken unless rewritten.

    Applied to every text/event-stream body. That is safe for Streamable HTTP,
    whose SSE bodies carry only JSON-RPC in `data:` lines and never an
    `event: endpoint` block — so no transport-mode flag is needed.

    Line-oriented and chunk-boundary safe: an `endpoint` event split across two
    TCP reads is still rewritten, and complete lines are emitted as they arrive so
    streaming is not turned into buffering.
    """
    buffer = b""
    in_endpoint_event = False

    async def process_line(raw: bytes) -> bytes:
        nonlocal in_endpoint_event

        body = raw.rstrip(b"\r\n")
        ending = raw[len(body):]

        if not body:
            in_endpoint_event = False  # blank line terminates the event block
            return raw

        lowered = body.lower()
        if lowered.startswith(b"event:"):
            in_endpoint_event = body[len(b"event:"):].strip() == b"endpoint"
            return raw

        if in_endpoint_event and lowered.startswith(b"data:"):
            value = body[len(b"data:"):].strip()
            if not value:
                return raw
            try:
                absolute = urljoin(base_url, value.decode("utf-8", errors="strict"))
                await validate_target(absolute, self_authority=self_authority)
            except (UnicodeDecodeError, ProxyTargetError) as exc:
                # Pass through untouched rather than inventing a target: the SDK
                # will fail loudly, and the reason is in the server log.
                print(f"⚠️  mcp-proxy: not rewriting SSE endpoint {value!r}: {exc}")
                return raw
            return b"data: " + to_proxy_path(absolute).encode("utf-8") + ending

        return raw

    async for chunk in stream:
        buffer += chunk
        out = bytearray()
        while True:
            idx = buffer.find(b"\n")
            if idx == -1:
                break
            out += await process_line(buffer[: idx + 1])
            buffer = buffer[idx + 1 :]
        if out:
            yield bytes(out)

    if buffer:
        yield await process_line(buffer)


# ── Header filtering ─────────────────────────────────────────────────────────

def _filter_request_headers(request: Request) -> Dict[str, str]:
    forwarded = {
        name: value
        for name, value in request.headers.items()
        if name.lower() in _REQUEST_HEADER_ALLOWLIST
    }
    # Ask for plaintext so the streamed chunks need no decoding, and so a
    # compressed SSE body cannot defeat the endpoint rewriting above.
    forwarded["accept-encoding"] = "identity"
    return forwarded


def _filter_response_headers(
    upstream: httpx.Response, self_authority: Optional[str]
) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    for name, value in upstream.headers.multi_items():
        lowered = name.lower()
        if lowered in _RESPONSE_HEADER_DENY_EXACT:
            continue
        if lowered.startswith(_RESPONSE_HEADER_DENY_PREFIXES):
            continue
        if lowered == "location":
            # follow_redirects=False, so the browser would otherwise be sent
            # straight at the upstream origin and hit CORS again.
            absolute = urljoin(str(upstream.request.url), value)
            if urlsplit(absolute).scheme in ALLOWED_SCHEMES:
                value = to_proxy_path(absolute)
        headers[name] = value
    return headers


# ── Routes ───────────────────────────────────────────────────────────────────

# Registered before the catch-all below so it is not swallowed by {scheme}/{rest}.
@router.get(f"{PROXY_PREFIX}/health")
async def proxy_health() -> Dict[str, object]:
    return {
        "status": "ok",
        "allow_private_targets": _allow_private(),
        "allowed_schemes": list(ALLOWED_SCHEMES),
    }


def _proxy_error(message: str, status: int) -> JSONResponse:
    return JSONResponse(
        {"error": message, "proxy": "mcp-proxy"},
        status_code=status,
        headers={"X-MCP-Proxy-Error": "1"},
    )


# OPTIONS is deliberately absent: CORSMiddleware answers preflight before routing,
# and claiming OPTIONS here would shadow it.
@router.api_route(
    f"{PROXY_PREFIX}/{{scheme}}/{{rest:path}}",
    methods=["GET", "POST", "DELETE"],
)
async def proxy(scheme: str, rest: str, request: Request) -> Response:
    self_authority = request.headers.get("host")
    target = _build_target_url(scheme, rest, request.url.query)

    try:
        await validate_target(target, self_authority=self_authority)
    except ProxyTargetError as exc:
        return _proxy_error(str(exc), exc.status)

    client = get_client()
    body = await request.body()

    upstream_request = client.build_request(
        request.method,
        target,
        headers=_filter_request_headers(request),
        content=body or None,
    )

    try:
        upstream = await client.send(upstream_request, stream=True)
    except httpx.HTTPError as exc:
        # 502 is reserved for proxy-level failures. Upstream statuses — including
        # the 404/405/406 that `auto` transport fallback keys on — pass through
        # verbatim below.
        return _proxy_error(f"Upstream request failed: {type(exc).__name__}: {exc}", 502)

    content_type = upstream.headers.get("content-type", "")
    is_sse = content_type.split(";", 1)[0].strip().lower() == "text/event-stream"

    async def stream_body() -> AsyncIterator[bytes]:
        try:
            source = upstream.aiter_raw()
            if is_sse:
                async for chunk in _rewrite_sse_endpoint(
                    source, str(upstream.request.url), self_authority
                ):
                    yield chunk
            else:
                async for chunk in source:
                    yield chunk
        finally:
            # Runs on a browser disconnect too; without it the upstream
            # connection leaks for the lifetime of the process.
            await upstream.aclose()

    return StreamingResponse(
        stream_body(),
        status_code=upstream.status_code,
        headers=_filter_response_headers(upstream, self_authority),
        media_type=content_type or None,
    )
