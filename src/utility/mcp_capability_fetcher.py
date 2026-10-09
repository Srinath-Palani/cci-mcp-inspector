"""
MCP Capability Fetcher (direct SDK)

Uses the MCP Python SDK (mcp.ClientSession) to connect to remote servers
that use the Streamable HTTP (/mcp) or SSE transport — bypassing
langchain-mcp-adapters, which only supports stdio and legacy SSE.

Adapted from cci-research-toolkit/mcp_discovery.py.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any, AsyncIterator, Dict, List, Optional

_DEFAULT_HEADERS: Dict[str, str] = {
    "Accept": "text/event-stream, application/json",
    "User-Agent": "MCP-Inspector/1.0",
}

# How long to wait for a single JSON-RPC response (initialize, list_tools, …).
# Without this, ClientSession passes read_timeout_seconds=None and every request
# waits forever on a server that accepts the connection but never replies — the
# request never returns, so asyncio.wait_for/cancel upstream cannot bound it.
DEFAULT_REQUEST_READ_TIMEOUT = 30.0

# How long an idle SSE/streamable-HTTP stream may stay silent before we give up.
# The SDK default is 300 s per attempt, which alone exceeds the whole inspection
# budget once several transport candidates are tried in sequence.
DEFAULT_SSE_READ_TIMEOUT = 60.0


def _extract_exception_detail(e: BaseException) -> str:
    """Unwrap ExceptionGroup/BaseExceptionGroup to expose the real sub-exception messages."""
    # Python 3.11+ ExceptionGroup exposes .exceptions
    if hasattr(e, "exceptions") and isinstance(getattr(e, "exceptions", None), (list, tuple)):
        sub_parts = [_extract_exception_detail(sub) for sub in e.exceptions]
        return f"{type(e).__name__}: {e}; sub-exceptions: [{'; '.join(sub_parts)}]"
    cause = e.__cause__ or e.__context__
    if cause is not None:
        return f"{type(e).__name__}: {e} (caused by {type(cause).__name__}: {cause})"
    return f"{type(e).__name__}: {e}"


def _exception_chain(e: BaseException) -> List[BaseException]:
    """Collect every exception reachable from e: itself, group members, cause/context."""
    seen: set[int] = set()
    collected: List[BaseException] = []

    def walk(ex: Optional[BaseException]) -> None:
        if ex is None or id(ex) in seen:
            return
        seen.add(id(ex))
        collected.append(ex)
        for sub in getattr(ex, "exceptions", None) or []:
            walk(sub)
        walk(ex.__cause__)
        walk(ex.__context__)

    walk(e)
    return collected


# Exception type names that mean "the peer went away mid-stream". These are the
# signature of talking the wrong transport at an endpoint: the server accepts the
# request, decides it makes no sense, and drops the connection without an HTTP
# status. Matched by name so httpx/anyio/h11 need not be imported here.
_STREAM_CLOSED_TYPES = {
    "RemoteProtocolError",
    "ClosedResourceError",
    "BrokenResourceError",
    "EndOfStream",
    "IncompleteRead",
    "ConnectionResetError",
    "ReadError",
    "WriteError",
    "LocalProtocolError",
}
_CONNECT_TYPES = {"ConnectError", "ConnectTimeout", "ConnectionRefusedError"}
_READ_TIMEOUT_TYPES = {"ReadTimeout", "PoolTimeout", "WriteTimeout", "TimeoutError", "McpError"}
_TLS_TYPES = {"SSLError", "SSLCertVerificationError", "SSLEOFError", "SSLZeroReturnError"}
_DNS_TYPES = {"gaierror", "herror"}


def classify_failure(e: BaseException) -> Dict[str, Any]:
    """
    Classify a connection failure into a coarse ``failure_kind`` plus an optional
    HTTP ``status_code``, walking ExceptionGroups and __cause__/__context__ chains.

    failure_kind is one of:
        "http_status"   — the server answered with an HTTP error status
        "tls"           — certificate/TLS handshake failure
        "dns"           — hostname could not be resolved
        "connect"       — TCP connect refused or timed out
        "stream_closed" — peer closed the stream mid-conversation
        "read_timeout"  — connected, but no response arrived in time
        "unknown"       — nothing matched
    Callers use this to tell a wrong-transport failure (retry the next candidate)
    apart from a real one (report it).
    """
    chain = _exception_chain(e)
    names = {type(ex).__name__ for ex in chain}

    status_code: Optional[int] = None
    for ex in chain:
        if type(ex).__name__ == "HTTPStatusError" and hasattr(ex, "response"):
            status_code = ex.response.status_code
            break

    # Order matters: the most specific/actionable diagnosis wins. An HTTP status
    # is the strongest signal, then transport-setup failures, then stream death.
    if status_code is not None:
        kind = "http_status"
    elif names & _TLS_TYPES:
        kind = "tls"
    elif names & _DNS_TYPES:
        kind = "dns"
    elif names & _CONNECT_TYPES:
        kind = "connect"
    elif names & _STREAM_CLOSED_TYPES:
        kind = "stream_closed"
    elif names & _READ_TIMEOUT_TYPES:
        kind = "read_timeout"
    else:
        kind = "unknown"

    return {"failure_kind": kind, "status_code": status_code}


# ── transport-specific connection helpers ───────────────────────────────────────

@asynccontextmanager
async def _client_context(
    url: str,
    transport: str,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 30.0,
    sse_read_timeout: float = DEFAULT_SSE_READ_TIMEOUT,
) -> AsyncIterator[Any]:
    """
    Yield an (read_stream, write_stream) pair for the given transport.

    transport: "streamable_http" | "sse"

    sse_read_timeout bounds how long a silent stream is tolerated. Both SDK
    clients default it to 300 s; we always pass an explicit, smaller value so a
    single stalled candidate cannot consume the whole inspection budget.
    """
    merged = {**_DEFAULT_HEADERS, **(headers or {})}

    if transport == "streamable_http":
        from mcp.client.streamable_http import streamablehttp_client
        async with streamablehttp_client(
            url, headers=merged, timeout=timeout, sse_read_timeout=sse_read_timeout
        ) as (r, w, _):
            yield r, w
    elif transport == "sse":
        from mcp.client.sse import sse_client
        async with sse_client(
            url, headers=merged, timeout=timeout, sse_read_timeout=sse_read_timeout
        ) as (r, w):
            yield r, w
    else:
        raise ValueError(f"Unsupported transport for capability fetcher: {transport!r}")


def _capability_enabled(capabilities: Any, name: str) -> bool:
    """Return True if the named capability is present and not None."""
    return getattr(capabilities, name, None) is not None


def _annotations_dict(annotations: Any) -> Optional[Dict[str, Any]]:
    """Convert an Annotations object to a plain dict, or None."""
    if annotations is None:
        return None
    if hasattr(annotations, "model_dump"):
        return annotations.model_dump(exclude_none=True)
    if isinstance(annotations, dict):
        return annotations
    return None


async def _safe_list(
    session: Any,
    method_name: str,
    incomplete_flags: Optional[Dict[str, bool]] = None,
) -> List[Any]:
    """
    Call session.<method_name>() and return the inner list, or [] on failure.

    Paginates: MCP list operations support cursor-based pagination
    (``cursor`` in, ``nextCursor`` out) — a single call silently truncates
    servers with more than one page. Follows nextCursor until absent, with a
    100-page guard against runaway pagination.

    When the run is cut short — page cap, repeating cursor, or an exception
    mid-pagination — ``incomplete_flags[method_name]`` is set True (if a dict is
    supplied) so the caller's report can mark the result ``pagination_incomplete``
    instead of presenting a truncated list as complete.
    """
    attr_map = {
        "list_tools": "tools",
        "list_resources": "resources",
        "list_resource_templates": "resourceTemplates",
        "list_prompts": "prompts",
    }
    attr = attr_map.get(method_name, method_name.replace("list_", ""))

    def _mark_incomplete() -> None:
        if incomplete_flags is not None:
            incomplete_flags[method_name] = True

    items: List[Any] = []
    cursor: Optional[str] = None
    seen_cursors: set = set()
    pages = 0
    try:
        while True:
            try:
                response = await getattr(session, method_name)(cursor=cursor)
            except TypeError:
                # Older SDK without cursor support — single page, best effort.
                if pages == 0:
                    response = await getattr(session, method_name)()
                else:
                    break
            items.extend(getattr(response, attr, []) or [])
            pages += 1
            cursor = getattr(response, "nextCursor", None)
            if not cursor:
                break
            if cursor in seen_cursors:
                # Buggy server returning the same cursor forever — without this
                # the page cap is the only thing between us and a 100-page loop
                # of duplicated items.
                print(
                    f"⚠️  [INCOMPLETE — {len(items)} items fetched, repeating cursor] "
                    f"{method_name} returned the same nextCursor twice; stopping"
                )
                _mark_incomplete()
                break
            seen_cursors.add(cursor)
            if pages >= 100:
                print(
                    f"⚠️  [INCOMPLETE — {len(items)} items fetched, page cap reached] "
                    f"{method_name} still returning nextCursor after {pages} pages"
                )
                _mark_incomplete()
                break
        return items
    except Exception:
        # Mid-pagination failure: whatever was fetched is partial.
        _mark_incomplete()
        return items if items else []


# ── main fetch function ─────────────────────────────────────────────────────────

async def fetch_mcp_capabilities(
    url: str,
    transport: str = "streamable_http",
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 30.0,
    sse_read_timeout: float = DEFAULT_SSE_READ_TIMEOUT,
    request_read_timeout: float = DEFAULT_REQUEST_READ_TIMEOUT,
) -> Dict[str, Any]:
    """
    Connect to an MCP server via the SDK and return a discovery dict that
    matches the structure expected by CapabilityCSVExporter and the workflow.

    Returns a dict with keys:
        server_url, server_info, capabilities_supported,
        tools, resources, resource_templates, prompts

    On failure the dict carries error, stage, status_code and failure_kind so
    callers can distinguish a wrong-transport failure from a real one.

    Every phase is time-bounded: `timeout` for the HTTP connect, `sse_read_timeout`
    for stream silence, and `request_read_timeout` for each JSON-RPC round trip.
    """
    from mcp import ClientSession
    from mcp.types import LATEST_PROTOCOL_VERSION, InitializeResult

    result: Dict[str, Any] = {
        "server_url": url,
        "server_info": {"name": url, "version": "unknown"},
        "capabilities_supported": {
            "tools": False,
            "resources": False,
            "prompts": False,
            "sampling": False,
            "logging": False,
            "completions": False,
            "experimental": None,
        },
        # A server that answers list calls for a capability it did NOT declare
        # in the handshake gets flagged here (buggy but common in the wild).
        "capability_mismatch": {
            "resources": False,
            "prompts": False,
        },
        "tools": [],
        "resources": [],
        "resource_templates": [],
        "prompts": [],
        "error": None,
        # The version this client offers in the initialize request. The SDK
        # hardcodes its own latest, so this is a property of the installed SDK,
        # not of the server — recording it makes the negotiation auditable.
        "client_protocol_version": LATEST_PROTOCOL_VERSION,
        "protocol_version": "",
        "negotiated_version": "",
        # Filled with the list_* method names whose results were cut short
        # (page cap, repeating cursor, mid-pagination error). Empty means every
        # fetched list is complete.
        "pagination_incomplete": [],
    }

    # Passed to _safe_list so it can record which method truncated.
    _incomplete: Dict[str, bool] = {}

    try:
        async with _client_context(url, transport, headers, timeout, sse_read_timeout) as (read, write):
            async with ClientSession(
                read,
                write,
                read_timeout_seconds=timedelta(seconds=request_read_timeout),
            ) as session:
                init: InitializeResult = await session.initialize()

                # Server identity
                if init.serverInfo:
                    result["server_info"] = {
                        "name": init.serverInfo.name,
                        "version": getattr(init.serverInfo, "version", "unknown") or "unknown",
                        "title": getattr(init.serverInfo, "title", None),
                    }
                # negotiated_version: the version the server chose in its reply.
                # This is the one version actually in use for this session — it is
                # NOT evidence about which other versions the server would accept.
                result["protocol_version"] = getattr(init, "protocolVersion", "") or ""
                result["negotiated_version"] = result["protocol_version"]

                caps = init.capabilities or {}

                # Capability flags — authoritative source is the handshake.
                # Sampling/elicitation/roots are declared by CLIENTS; a server-side
                # sampling entry in capabilities is the only honest server signal,
                # so we report exactly that and never infer it.
                supported = result["capabilities_supported"]
                supported["tools"] = _capability_enabled(caps, "tools")
                supported["resources"] = _capability_enabled(caps, "resources")
                supported["prompts"] = _capability_enabled(caps, "prompts")
                supported["sampling"] = _capability_enabled(caps, "sampling")
                supported["logging"] = _capability_enabled(caps, "logging")
                supported["completions"] = _capability_enabled(caps, "completions")
                experimental = getattr(caps, "experimental", None)
                if experimental:
                    supported["experimental"] = dict(experimental)

                # Fetch actual items — tools only when declared (universal), but
                # resources/prompts are probed even when undeclared because many
                # servers omit them from the handshake yet answer the list calls.
                if supported["tools"]:
                    raw_tools = await _safe_list(session, "list_tools", _incomplete)
                    result["tools"] = [
                        {
                            "name": t.name,
                            "title": getattr(t, "title", None),
                            "description": getattr(t, "description", None),
                            "inputSchema": getattr(t, "inputSchema", None),
                            "outputSchema": getattr(t, "outputSchema", None),
                            "annotations": getattr(t, "annotations", None),
                        }
                        for t in raw_tools
                    ]

                raw_resources = await _safe_list(session, "list_resources", _incomplete)
                if raw_resources and not supported["resources"]:
                    result["capability_mismatch"]["resources"] = True
                    supported["resources"] = True  # server demonstrably supports them
                if supported["resources"]:
                    result["resources"] = [
                        {
                            "uri": str(r.uri),
                            "name": r.name,
                            "title": getattr(r, "title", None),
                            "description": getattr(r, "description", None),
                            "mimeType": getattr(r, "mimeType", None),
                            "size": getattr(r, "size", None),
                            "annotations": _annotations_dict(getattr(r, "annotations", None)),
                        }
                        for r in raw_resources
                    ]

                    raw_templates = await _safe_list(session, "list_resource_templates", _incomplete)
                    result["resource_templates"] = [
                        {
                            "uriTemplate": str(t.uriTemplate),
                            "name": t.name,
                            "title": getattr(t, "title", None),
                            "description": getattr(t, "description", None),
                            "mimeType": getattr(t, "mimeType", None),
                        }
                        for t in raw_templates
                    ]

                raw_prompts = await _safe_list(session, "list_prompts", _incomplete)
                if raw_prompts and not supported["prompts"]:
                    result["capability_mismatch"]["prompts"] = True
                    supported["prompts"] = True
                if supported["prompts"]:
                    result["prompts"] = [
                        {
                            "name": p.name,
                            "title": getattr(p, "title", None),
                            "description": getattr(p, "description", None),
                            "arguments": [
                                {
                                    "name": a.name,
                                    "description": getattr(a, "description", None),
                                    "required": getattr(a, "required", False),
                                }
                                for a in (getattr(p, "arguments", None) or [])
                            ],
                        }
                        for p in raw_prompts
                    ]

    except Exception as e:
        # Walk the ExceptionGroup / cause chain once to surface both the HTTP
        # status code (as a first-class int) and a coarse failure_kind, so the
        # discovery agent can decide whether trying another transport is sensible.
        classified = classify_failure(e)
        result["error"] = _extract_exception_detail(e)
        result["stage"] = "sse_connect" if transport == "sse" else "http_connect"
        result["success"] = False
        result["failure_kind"] = classified["failure_kind"]
        if classified["status_code"] is not None:
            result["status_code"] = classified["status_code"]

    # Surface any truncated list so the report can flag the run as partial
    # instead of presenting an incomplete capability set as complete.
    result["pagination_incomplete"] = sorted(_incomplete)
    return result


def fetch_mcp_capabilities_sync(
    url: str,
    transport: str = "streamable_http",
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 30.0,
) -> Dict[str, Any]:
    """Synchronous wrapper around fetch_mcp_capabilities."""
    return asyncio.run(fetch_mcp_capabilities(url, transport, headers, timeout))
