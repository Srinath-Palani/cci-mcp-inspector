"""
Protocol version prober — finds which MCP protocol versions a server accepts.

Why this exists
---------------
`ClientSession` hardcodes `protocolVersion=types.LATEST_PROTOCOL_VERSION` in the
initialize request and offers no override (mcp 1.28.1, `client/session.py`). So a
handshake through the SDK proves exactly one version — whatever the installed SDK
happens to offer — and can never show that a server also speaks the newest spec
revision, or that it only speaks an older one. The report's per-version flags were
therefore derived from a single string, which made "does this server support
2025-11-25?" unanswerable.

This module answers it by speaking JSON-RPC directly over httpx, offering one
explicit `protocolVersion` per probe. Because the request is hand-built, it can
also offer versions the installed SDK does not know about (e.g. a future
2026-07-28), which is the whole point of a discovery tool.

Negotiation rules this relies on (MCP basic lifecycle):
  * If the server supports the requested version it MUST echo that exact version.
  * Otherwise it MUST respond with a version it does support (SHOULD be its latest).
  * Some servers instead reject with a JSON-RPC error (commonly -32602).

So an echo is positive evidence for the requested version, and a *different*
version in the reply is positive evidence for that other version — which is why
one probe of the newest version usually already reveals the server's ceiling.

Every failure is reported in the same structured shape the capability fetcher uses
(`failure_kind` + `status_code`), never as a bare exception string.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

from src.models.structured_output import SUPPORTED_PROTOCOL_VERSIONS
from src.utility.mcp_capability_fetcher import classify_failure

# Whole-probe wall clock. The prober runs *after* a successful handshake inside the
# discovery phase, so it has to be a small, predictable addition to that phase and
# never a reason for the inspection job to hit its own timeout.
DEFAULT_PROBE_BUDGET_SECONDS = 25.0

# Per-version ceiling. A server that stalls on one version must not consume the
# budget the remaining versions need.
DEFAULT_PER_VERSION_TIMEOUT = 8.0

_CLIENT_INFO = {"name": "mcp-server-inspector", "version": "1.0.0"}

# Capabilities are declared empty on purpose: the probe only wants the server's
# half of the handshake, and claiming client capabilities we will not honour
# (sampling, roots, elicitation) could make a server behave differently.
_CLIENT_CAPABILITIES: Dict[str, Any] = {}


def _initialize_body(version: str, request_id: int = 1) -> Dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "initialize",
        "params": {
            "protocolVersion": version,
            "capabilities": _CLIENT_CAPABILITIES,
            "clientInfo": _CLIENT_INFO,
        },
    }


def _one_line(text: str, limit: int = 300) -> str:
    """Collapse an error to a single short line — these go into report fields."""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _parse_jsonrpc_payload(raw: str) -> Optional[Dict[str, Any]]:
    """
    Pull the JSON-RPC object out of a response body that may be either plain JSON
    or a `text/event-stream` frame carrying it in `data:` lines.
    """
    text = raw.strip()
    if not text:
        return None
    if not text.startswith("event:") and not text.startswith("data:") and not text.startswith(":"):
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None

    # SSE frame: concatenate the data lines of the first event that parses.
    data_lines: List[str] = []
    for line in text.splitlines():
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
        elif not line.strip() and data_lines:
            break
    if not data_lines:
        return None
    try:
        parsed = json.loads("\n".join(data_lines))
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def _verdict_from_payload(requested: str, payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Turn a JSON-RPC initialize reply into a verdict for the requested version.

    `supported` is None (not False) whenever the reply is unreadable: "we could not
    tell" and "the server said no" are different findings and the report must not
    conflate them.
    """
    if payload is None:
        return {
            "supported": None,
            "server_reported": None,
            "error": "Reply was not readable JSON-RPC",
        }

    if isinstance(payload.get("error"), dict):
        err = payload["error"]
        code = err.get("code")
        message = _one_line(err.get("message") or "rejected")
        verdict: Dict[str, Any] = {
            "supported": False,
            "server_reported": None,
            "jsonrpc_error": {"code": code, "message": message},
            # A JSON-RPC error is a definite "no" for this version, so it is
            # reported as the reason rather than as a probe malfunction.
            "error": f"Server rejected {requested} (JSON-RPC {code}): {message}",
        }
        # -32022 UnsupportedProtocolVersionError (spec-defined, 2026-07-28):
        # data.supported lists every version the server accepts — that is
        # positive evidence for all of them, not just a "no" for this one.
        if code == -32022:
            data = err.get("data")
            if isinstance(data, dict):
                supported_list = data.get("supported")
                if isinstance(supported_list, list):
                    verdict["server_supported_versions"] = [
                        str(v) for v in supported_list if v
                    ]
        return verdict

    result = payload.get("result")
    if not isinstance(result, dict):
        return {
            "supported": None,
            "server_reported": None,
            "error": "Reply had neither a result nor an error object",
        }

    reported = str(result.get("protocolVersion") or "")
    server_info = result.get("serverInfo") if isinstance(result.get("serverInfo"), dict) else {}
    return {
        "supported": reported == requested,
        "server_reported": reported or None,
        "server_name": server_info.get("name"),
        "error": None,
    }


# ── modern-era probe: server/discover (2026-07-28) ────────────────────────────

def _discover_body(request_id: int = 0) -> Dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "server/discover",
        "params": {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": SUPPORTED_PROTOCOL_VERSIONS[0],
                "io.modelcontextprotocol/clientInfo": dict(_CLIENT_INFO),
                "io.modelcontextprotocol/clientCapabilities": _CLIENT_CAPABILITIES,
            }
        },
    }


def _parse_discover_result(payload: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    Extract {supported_versions, capabilities, server_name} from a
    server/discover reply, or None if this is not a recognizable modern answer
    (legacy servers respond with implementation-defined errors — commonly
    -32601/-32602 — or not at all; ANY non-modern outcome means: fall back to
    the legacy initialize ladder).

    Reference: https://modelcontextprotocol.io/specification/2026-07-28/server/discover
    """
    if not payload or not isinstance(payload.get("result"), dict):
        return None
    result = payload["result"]
    versions = result.get("supportedVersions")
    if not isinstance(versions, list) or not versions:
        return None
    meta = result.get("_meta") if isinstance(result.get("_meta"), dict) else {}
    server_info = meta.get("io.modelcontextprotocol/serverInfo") or {}
    if not isinstance(server_info, dict):
        server_info = {}
    return {
        "supported_versions": [str(v) for v in versions if v],
        "capabilities": result.get("capabilities"),
        "server_name": server_info.get("name"),
    }


async def _probe_discover(
    url: str,
    transport: str,
    headers: Dict[str, str],
    timeout: float,
) -> Optional[Dict[str, Any]]:
    """
    One server/discover attempt. Returns the parsed discovery result, or None
    when the server is not modern-era (fall back to initialize probing).
    Never raises.
    """
    import httpx

    try:
        if transport == "streamable_http":
            async with httpx.AsyncClient(follow_redirects=True, http2=False) as client:
                response = await client.post(
                    url,
                    json=_discover_body(),
                    headers={
                        **headers,
                        "Content-Type": "application/json",
                        "Accept": "application/json, text/event-stream",
                    },
                    timeout=httpx.Timeout(timeout),
                )
                session_id = response.headers.get("mcp-session-id")
                parsed = _parse_discover_result(_parse_jsonrpc_payload(response.text))
                if session_id:
                    try:
                        await client.delete(
                            url,
                            headers={**headers, "Mcp-Session-Id": session_id},
                            timeout=httpx.Timeout(5.0),
                        )
                    except Exception:
                        pass
                return parsed
        elif transport == "sse":
            # Legacy-SSE servers predate the modern era by definition; probing
            # discover there only burns budget. Skip.
            return None
    except Exception:
        return None
    return None


# ── transport-specific single-version probes ────────────────────────────────────

async def _probe_streamable_http(
    client: Any,
    url: str,
    version: str,
    headers: Dict[str, str],
    timeout: float,
) -> Dict[str, Any]:
    """One initialize POST. The whole Streamable HTTP handshake is a single request."""
    import httpx

    request_headers = {
        **headers,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    response = await client.post(
        url,
        json=_initialize_body(version),
        headers=request_headers,
        timeout=httpx.Timeout(timeout),
    )

    session_id = response.headers.get("mcp-session-id")
    body = response.text

    if response.status_code >= 400:
        verdict: Dict[str, Any] = {
            "supported": False,
            "server_reported": None,
            "error": f"HTTP {response.status_code} on initialize",
        }
        # A JSON-RPC error body carries the real reason; prefer it over the status.
        payload = _parse_jsonrpc_payload(body)
        if payload is not None and isinstance(payload.get("error"), dict):
            verdict = _verdict_from_payload(version, payload)
        verdict["status_code"] = response.status_code
        verdict["failure_kind"] = "http_status"
        return verdict

    verdict = _verdict_from_payload(version, _parse_jsonrpc_payload(body))
    verdict["status_code"] = response.status_code

    if session_id:
        # Release the session we just opened. Best effort: a server that does not
        # implement DELETE is not a probe failure.
        try:
            await client.delete(
                url,
                headers={**headers, "Mcp-Session-Id": session_id},
                timeout=httpx.Timeout(min(timeout, 5.0)),
            )
        except Exception:
            pass

    return verdict


async def _read_sse_endpoint(lines: Any, deadline: float) -> Optional[str]:
    """
    Consume a legacy SSE stream until the bootstrap `event: endpoint` frame, and
    return its data payload (the URL to POST messages to).
    """
    current_event: Optional[str] = None
    async for line in lines:
        if time.monotonic() > deadline:
            return None
        if line.startswith("event:"):
            current_event = line[6:].strip()
        elif line.startswith("data:"):
            data = line[5:].lstrip()
            if current_event == "endpoint":
                return data
            current_event = None
        elif not line.strip():
            current_event = None
    return None


async def _read_sse_message(lines: Any, deadline: float) -> Optional[Dict[str, Any]]:
    """Consume a legacy SSE stream until the next frame that parses as JSON-RPC."""
    data_lines: List[str] = []
    async for line in lines:
        if time.monotonic() > deadline:
            return None
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
        elif not line.strip() and data_lines:
            try:
                payload = json.loads("\n".join(data_lines))
                if isinstance(payload, dict):
                    return payload
            except json.JSONDecodeError:
                pass
            data_lines = []
    return None


async def _probe_sse(
    client: Any,
    url: str,
    version: str,
    headers: Dict[str, str],
    timeout: float,
) -> Dict[str, Any]:
    """
    Legacy SSE handshake: open the GET stream, read the bootstrap endpoint, POST
    initialize there, then read the reply back off the *same* stream.

    The POST is dispatched as a task rather than awaited first, because on this
    transport the reply is delivered on the open GET stream — awaiting the POST to
    completion before reading would work on most servers but deadlocks on any
    server that flushes the SSE frame before closing the POST response.
    """
    import httpx

    deadline = time.monotonic() + timeout
    stream_headers = {**headers, "Accept": "text/event-stream", "Cache-Control": "no-store"}

    async with client.stream(
        "GET",
        url,
        headers=stream_headers,
        timeout=httpx.Timeout(connect=timeout, read=timeout, write=timeout, pool=timeout),
    ) as response:
        if response.status_code >= 400:
            return {
                "supported": None,
                "server_reported": None,
                "status_code": response.status_code,
                "failure_kind": "http_status",
                "error": f"HTTP {response.status_code} opening the SSE stream",
            }

        lines = response.aiter_lines()
        endpoint = await _read_sse_endpoint(lines, deadline)
        if not endpoint:
            return {
                "supported": None,
                "server_reported": None,
                "failure_kind": "stream_closed",
                "error": "SSE stream closed before sending the endpoint event",
            }

        post_url = urljoin(str(response.url), endpoint)
        post_task = asyncio.create_task(
            client.post(
                post_url,
                json=_initialize_body(version),
                headers={**headers, "Content-Type": "application/json"},
                timeout=httpx.Timeout(timeout),
            )
        )
        try:
            payload = await _read_sse_message(lines, deadline)
        finally:
            if not post_task.done():
                post_task.cancel()
            posted = await asyncio.gather(post_task, return_exceptions=True)

        # The POST's own status matters only when no reply arrived on the stream.
        if payload is None:
            post_result = posted[0] if posted else None
            if isinstance(post_result, BaseException) and not isinstance(post_result, asyncio.CancelledError):
                classified = classify_failure(post_result)
                return {
                    "supported": None,
                    "server_reported": None,
                    "status_code": classified["status_code"],
                    "failure_kind": classified["failure_kind"],
                    "error": _one_line(f"initialize POST failed: {post_result}"),
                }
            status = getattr(post_result, "status_code", None)
            if isinstance(status, int) and status >= 400:
                return {
                    "supported": False,
                    "server_reported": None,
                    "status_code": status,
                    "failure_kind": "http_status",
                    "error": f"HTTP {status} on the initialize POST",
                }
            return {
                "supported": None,
                "server_reported": None,
                "failure_kind": "read_timeout",
                "error": "No initialize reply arrived on the SSE stream in time",
            }

        return _verdict_from_payload(version, payload)


# ── public entry point ──────────────────────────────────────────────────────────

async def probe_protocol_versions(
    url: str,
    transport: str = "streamable_http",
    headers: Optional[Dict[str, str]] = None,
    versions: Optional[List[str]] = None,
    budget_seconds: float = DEFAULT_PROBE_BUDGET_SECONDS,
    per_version_timeout: float = DEFAULT_PER_VERSION_TIMEOUT,
) -> Dict[str, Any]:
    """
    Probe `versions` (newest first) against `url` and report which ones the server
    accepts. Never raises: transport failures come back as structured entries.

    Returns:
        {
          "probed_url": str,
          "probed_transport": "streamable_http" | "sse",
          "versions_offered": [str, ...],
          "supported_versions": [str, ...],       # newest first, confirmed only
          "latest_supported": str | None,
          "results": [ {version, supported, server_reported, status_code,
                        failure_kind, jsonrpc_error, error}, ... ],
          "budget_exhausted": bool,
          "error": str | None,                    # set only if nothing was probed
          "failure_kind": str | None,
        }

    `supported` per entry is True / False / None, where None means "could not
    determine" — an unreachable server must not be reported as *not* supporting a
    version it was never actually asked about.
    """
    import httpx

    candidate_versions = list(versions or SUPPORTED_PROTOCOL_VERSIONS)
    base_headers = {k: v for k, v in (headers or {}).items()}
    base_headers.setdefault("User-Agent", "MCP-Inspector/1.0")

    outcome: Dict[str, Any] = {
        "probed_url": url,
        "probed_transport": transport,
        "versions_offered": candidate_versions,
        "supported_versions": [],
        "latest_supported": None,
        # Which protocol era answered: "server/discover answered" (modern,
        # 2026-07-28+) or "legacy initialize". None until determined.
        "era_evidence": None,
        "results": [],
        "budget_exhausted": False,
        "error": None,
        "failure_kind": None,
    }

    if transport not in ("streamable_http", "sse"):
        outcome["error"] = f"Version probing is not supported for transport {transport!r}"
        return outcome

    # Modern-era pre-probe: server/discover is mandatory for 2026-07-28+
    # servers and returns supportedVersions as ground truth — no ladder walk
    # needed when it answers. Any non-modern outcome falls back to the legacy
    # initialize ladder below.
    discover = await _probe_discover(url, transport, base_headers, per_version_timeout)
    if discover:
        outcome["era_evidence"] = "server/discover answered"
        outcome["supported_versions"] = list(discover["supported_versions"])
        known_order = {v: i for i, v in enumerate(SUPPORTED_PROTOCOL_VERSIONS)}
        outcome["supported_versions"].sort(key=lambda v: known_order.get(v, len(known_order)))
        outcome["latest_supported"] = outcome["supported_versions"][0]
        outcome["results"].append({
            "version": "server/discover",
            "supported": True,
            "server_reported": ", ".join(discover["supported_versions"]),
            "server_name": discover.get("server_name"),
            "status_code": None,
            "failure_kind": None,
            "error": None,
        })
        return outcome

    outcome["era_evidence"] = "legacy initialize"

    probe = _probe_streamable_http if transport == "streamable_http" else _probe_sse
    deadline = time.monotonic() + budget_seconds
    # follow_redirects: a 307 to the canonical /mcp path is routine and is not
    # information about version support.
    async with httpx.AsyncClient(follow_redirects=True, http2=False) as client:
        for version in candidate_versions:
            remaining = deadline - time.monotonic()
            if remaining <= 1.0:
                outcome["budget_exhausted"] = True
                break

            entry: Dict[str, Any] = {
                "version": version,
                "supported": None,
                "server_reported": None,
                "status_code": None,
                "failure_kind": None,
                "error": None,
            }
            try:
                verdict = await asyncio.wait_for(
                    probe(client, url, version, base_headers, min(per_version_timeout, remaining)),
                    timeout=min(per_version_timeout, remaining) + 2.0,
                )
                entry.update(verdict)
            except asyncio.TimeoutError:
                entry["failure_kind"] = "read_timeout"
                entry["error"] = f"No initialize reply within {per_version_timeout:.0f}s"
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — every failure is a probe datum
                classified = classify_failure(e)
                entry["failure_kind"] = classified["failure_kind"]
                entry["status_code"] = classified["status_code"]
                entry["error"] = _one_line(f"{type(e).__name__}: {e}")

            outcome["results"].append(entry)

            # A reply naming a *different* version is positive evidence for that
            # version, so record it even though this probe asked for another one.
            # Same for a -32022 error's data.supported list: the server just told
            # us every version it accepts.
            reported = entry.get("server_reported")
            confirmed_versions = [version if entry.get("supported") else None, reported]
            confirmed_versions.extend(entry.get("server_supported_versions") or [])
            for confirmed in filter(None, confirmed_versions):
                if confirmed not in outcome["supported_versions"]:
                    outcome["supported_versions"].append(confirmed)

    # Order newest-first by the canonical list, keeping any unknown version the
    # server volunteered at the end so it is still visible in the report.
    known_order = {v: i for i, v in enumerate(SUPPORTED_PROTOCOL_VERSIONS)}
    outcome["supported_versions"].sort(key=lambda v: known_order.get(v, len(known_order)))
    if outcome["supported_versions"]:
        outcome["latest_supported"] = outcome["supported_versions"][0]

    if not outcome["results"]:
        outcome["error"] = outcome["error"] or "No version probe completed within the budget"
        outcome["failure_kind"] = "read_timeout" if outcome["budget_exhausted"] else "unknown"
    elif not outcome["supported_versions"]:
        # Every probe ran and none confirmed anything — report the first concrete
        # reason rather than leaving the caller to guess from an empty list.
        first_error = next((r for r in outcome["results"] if r.get("error")), None)
        if first_error:
            outcome["error"] = first_error["error"]
            outcome["failure_kind"] = first_error.get("failure_kind")

    return outcome


def summarize_probe(outcome: Dict[str, Any]) -> str:
    """One-line human summary for logs and the report's evidence field."""
    if not outcome:
        return "not probed"
    supported = outcome.get("supported_versions") or []
    if supported:
        detail = ", ".join(supported)
        suffix = " (budget exhausted, list may be incomplete)" if outcome.get("budget_exhausted") else ""
        return f"{len(supported)} version(s) confirmed: {detail}{suffix}"
    return f"no version confirmed: {outcome.get('error') or 'unknown reason'}"
