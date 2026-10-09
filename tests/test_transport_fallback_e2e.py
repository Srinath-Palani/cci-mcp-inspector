"""
End-to-end checks for the two transport failures from the original bug report.

The reported URLs (regdatalab.com → 400, absmartly / fal.ai → connection closed)
are not recorded anywhere in this repo, so rather than guess at them these checks
reproduce both failure modes exactly and against a *real* MCP server built with
the installed SDK:

  1. "regdatalab" shape — an SSE-only server whose /mcp endpoint answers 400 to a
     Streamable HTTP POST. The discovery walk must fall back to SSE and actually
     succeed, listing the server's tools.
  2. "absmartly / fal.ai" shape — an endpoint that accepts the TCP connection and
     then closes it mid-handshake. It must be classified as stream_closed and
     rendered as TRANSPORT_MISMATCH rather than an opaque exception string.
  3. The same real SSE server reached through /mcp-proxy, to prove the proxy's SSE
     `endpoint` rewriting produces a URL a client can complete a session on.

Run directly:  .venv/bin/python test_transport_fallback_e2e.py
"""
import sys as _s, os as _o
_s.path.insert(0, _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))))


import asyncio
import os
import socket
import threading
import time

import uvicorn
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from api_bridge import mcp_proxy
from api_bridge.error_classifier import classify_failure_result
from src.agents.mcp_discovery_agent import _discover_http_with_fetcher
from src.utility.mcp_capability_fetcher import fetch_mcp_capabilities


def _unused_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ── 1. Real SSE-only MCP server whose /mcp rejects Streamable HTTP with 400 ──

def _build_sse_only_app(rejections: list | None = None) -> Starlette:
    """
    A real MCP server that speaks only legacy SSE (at /sse + /messages/) and answers
    400 on the endpoints a Streamable HTTP client would try — the regdatalab shape.

    The 400 sits on "/" as well as "/mcp" because the discovery walk probes the URL
    the user typed, which for these servers is the bare origin.
    """
    server = FastMCP(name="fallback-probe")

    @server.tool()
    def echo(text: str) -> str:
        """Echo the input back."""
        return text

    async def reject_streamable_http(request):
        # What regdatalab.com does: the path exists but does not speak this
        # transport, so the request is rejected outright.
        if rejections is not None:
            rejections.append(f"{request.method} {request.url.path}")
        return JSONResponse({"error": "Bad Request"}, status_code=400)

    return Starlette(
        routes=[
            # Ordered before the Mount so these exact paths win.
            Route("/", reject_streamable_http, methods=["GET", "POST", "DELETE"]),
            Route("/mcp", reject_streamable_http, methods=["GET", "POST", "DELETE"]),
            Mount("/", app=server.sse_app()),
        ]
    )


class BackgroundServer:
    """Runs an ASGI app on loopback in a thread for the duration of a with-block."""

    def __init__(self, app, port: int):
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self.port = port

    def __enter__(self):
        self._thread.start()
        deadline = time.monotonic() + 15
        while not self._server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        if not self._server.started:
            raise RuntimeError("server did not start")
        return self

    def __exit__(self, *exc):
        self._server.should_exit = True
        self._thread.join(timeout=10)


class ClosingServer:
    """Accepts a connection, reads the request, then closes without replying."""

    def __init__(self):
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self):
        self._sock.settimeout(0.5)
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                continue
            try:
                conn.settimeout(1.0)
                conn.recv(65536)  # read the request, answer nothing
            except OSError:
                pass
            finally:
                conn.close()  # EOF mid-handshake

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=3)
        self._sock.close()


async def test_400_falls_back_to_sse_and_succeeds():
    port = _unused_port()
    rejections: list = []
    with BackgroundServer(_build_sse_only_app(rejections), port):
        result = await _discover_http_with_fetcher(
            {"endpoint_url": f"http://127.0.0.1:{port}", "name": "probe"}, "probe", "auto"
        )

    attempted = result.get("attempted_candidates") or []
    assert result["status"] == "SUCCESS", f"{result.get('error_message')} (tried {attempted})"
    tool_names = [t.get("name") for t in result.get("tools_discovered") or []]
    assert "echo" in tool_names, tool_names
    assert attempted and attempted[0].startswith("streamable_http@"), attempted
    assert attempted[-1].startswith("sse@"), attempted
    # The 400 must actually have been provoked, otherwise this proves nothing.
    assert any(r.startswith("POST /") for r in rejections), rejections
    assert result["connection_type"] == "sse", result["connection_type"]
    print(f"  ✓ server answered 400 to {rejections}")
    print(f"  ✓ walked {attempted} → SUCCESS via sse with tools {tool_names}")


async def test_closed_connection_is_typed():
    with ClosingServer() as server:
        url = f"http://127.0.0.1:{server.port}/mcp"
        fetched = await fetch_mcp_capabilities(
            url, transport="streamable_http", timeout=5.0,
            sse_read_timeout=5.0, request_read_timeout=5.0,
        )
        assert fetched.get("failure_kind") == "stream_closed", fetched.get("failure_kind")

        walked = await _discover_http_with_fetcher(
            {"endpoint_url": f"http://127.0.0.1:{server.port}", "name": "closer"}, "closer", "auto"
        )

    assert walked["status"] == "FAILURE"
    assert walked.get("failure_kind") == "stream_closed", walked.get("failure_kind")
    assert len(walked.get("attempted_candidates") or []) >= 2, walked.get("attempted_candidates")

    details = classify_failure_result(walked, "mcp_discovery")
    assert details.error_type == "TRANSPORT_MISMATCH", details.error_type
    assert any(s.action == "switch_transport" for s in details.suggestions)
    print(f"  ✓ closed connection → failure_kind=stream_closed → {details.error_type}")
    print(f"    UI title: {details.title}")


async def test_401_short_circuits_the_walk():
    """An auth failure is not a transport problem — the walk must stop at once."""
    async def unauthorized(_request):
        return JSONResponse(
            {"error": "Unauthorized"},
            status_code=401,
            headers={"WWW-Authenticate": 'Bearer resource_metadata="https://example.com/.well-known"'},
        )

    app = Starlette(routes=[Route("/{rest:path}", unauthorized,
                                 methods=["GET", "POST", "DELETE"])])
    port = _unused_port()
    with BackgroundServer(app, port):
        result = await _discover_http_with_fetcher(
            {"endpoint_url": f"http://127.0.0.1:{port}", "name": "locked"}, "locked", "auto"
        )

    attempted = result.get("attempted_candidates") or []
    assert result["status"] == "FAILURE"
    assert len(attempted) == 1, f"401 should stop the walk, but tried {attempted}"
    assert result.get("status_code") == 401, result.get("status_code")
    details = classify_failure_result(result, "mcp_discovery")
    assert details.error_type in ("OAUTH_REQUIRED", "AUTH_INVALID_TOKEN"), details.error_type
    print(f"  ✓ 401 stopped after {attempted} → {details.error_type}")


async def test_real_sse_session_through_proxy():
    """A full MCP session over the proxy — proves the SSE endpoint rewrite works."""
    upstream_port = _unused_port()
    proxy_port = _unused_port()

    from api_bridge.main import app as api_app

    os.environ["MCP_PROXY_ALLOW_PRIVATE"] = "1"  # upstream is on loopback
    try:
        with BackgroundServer(_build_sse_only_app(), upstream_port), \
             BackgroundServer(api_app, proxy_port):
            proxied = (
                f"http://127.0.0.1:{proxy_port}/mcp-proxy/http/127.0.0.1:{upstream_port}/sse"
            )
            async with sse_client(proxied, timeout=10.0, sse_read_timeout=20.0) as (read, write):
                async with ClientSession(read, write) as session:
                    init = await session.initialize()
                    tools = await session.list_tools()
    finally:
        os.environ.pop("MCP_PROXY_ALLOW_PRIVATE", None)
        await mcp_proxy.close_client()

    names = [t.name for t in tools.tools]
    assert "echo" in names, names
    print(
        f"  ✓ full SSE session through /mcp-proxy: negotiated {init.protocolVersion}, "
        f"tools {names}"
    )


async def main():
    print("regdatalab shape (400 on the wrong transport):")
    await test_400_falls_back_to_sse_and_succeeds()
    print("absmartly / fal.ai shape (connection closed mid-handshake):")
    await test_closed_connection_is_typed()
    print("auth failure must not be treated as a transport problem:")
    await test_401_short_circuits_the_walk()
    print("proxy:")
    await test_real_sse_session_through_proxy()
    print("\nAll transport-fallback end-to-end checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
