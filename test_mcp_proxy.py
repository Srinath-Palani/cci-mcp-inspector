"""
Checks for the /mcp-proxy CORS-bypass route.

Runs a throwaway upstream server on loopback and drives the proxy through the
real FastAPI app, so the header policy, status passthrough, SSE `endpoint`
rewriting and SSRF guard are all exercised end to end.

Loopback targets are normally rejected by the SSRF guard, so the upstream tests
set MCP_PROXY_ALLOW_PRIVATE=1 — the guard itself is verified separately with the
flag off.

Run directly:  .venv/bin/python test_mcp_proxy.py
"""

import asyncio
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

from api_bridge import mcp_proxy
from api_bridge.mcp_proxy import (
    REQUIRED_PASSTHROUGH_RESPONSE_HEADERS,
    ProxyTargetError,
    to_proxy_path,
    validate_target,
)

# Captured by the upstream handler so the request-header allowlist can be asserted.
received_headers: dict = {}
received_bodies: list = []


class Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # silence the default stderr logging
        pass

    def _record(self):
        received_headers.clear()
        received_headers.update({k.lower(): v for k, v in self.headers.items()})

    def do_DELETE(self):
        self._record()
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        self._record()
        if self.path.startswith("/sse"):
            # No Content-Length and no chunked framing: end of body is signalled by
            # closing the connection, which is what ends the test's read.
            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            # Deliberately split across writes and mid-line to prove the rewriter
            # is chunk-boundary safe.
            self.wfile.write(b"event: endpoint\ndata: /mess")
            self.wfile.flush()
            self.wfile.write(b"ages?sessionId=abc123\n\n")
            self.wfile.write(b"event: message\ndata: {\"jsonrpc\":\"2.0\"}\n\n")
            self.wfile.flush()
            return
        if self.path.startswith("/redirect"):
            self.send_response(302)
            self.send_header("Location", "/moved/mcp")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path.startswith("/status/"):
            code = int(self.path.rsplit("/", 1)[-1])
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        self._record()
        length = int(self.headers.get("Content-Length") or 0)
        received_bodies.append(self.rfile.read(length))

        body = b'{"jsonrpc":"2.0","id":1,"result":{}}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        # Must survive the proxy
        self.send_header("Mcp-Session-Id", "sess-42")
        self.send_header("MCP-Protocol-Version", "2025-11-25")
        self.send_header("WWW-Authenticate", 'Bearer realm="mcp"')
        # Must be stripped
        self.send_header("Set-Cookie", "a=b")
        self.send_header("Access-Control-Allow-Origin", "https://evil.example")
        self.send_header("Strict-Transport-Security", "max-age=31536000")
        self.end_headers()
        self.wfile.write(body)


def _start_upstream():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"127.0.0.1:{server.server_address[1]}"


def _make_app_client():
    """ASGI client against the real app (so CORSMiddleware and routing apply)."""
    from api_bridge.main import app

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        timeout=20.0,
    )


async def test_health():
    async with _make_app_client() as c:
        r = await c.get("/mcp-proxy/health")
    assert r.status_code == 200, r.status_code
    assert r.json()["status"] == "ok"
    print("  ✓ /mcp-proxy/health answers before the catch-all route")


async def test_post_passthrough_and_headers(authority: str):
    async with _make_app_client() as c:
        r = await c.post(
            f"/mcp-proxy/http/{authority}/mcp",
            content=b'{"jsonrpc":"2.0","id":1,"method":"initialize"}',
            headers={
                "content-type": "application/json",
                "accept": "application/json, text/event-stream",
                "authorization": "Bearer tok",
                "mcp-session-id": "sess-42",
                "mcp-protocol-version": "2025-11-25",
                "last-event-id": "17",
                # Must NOT be forwarded
                "cookie": "secret=1",
                "origin": "http://localhost:5003",
                "referer": "http://localhost:5003/",
                "x-forwarded-for": "10.0.0.1",
                "sec-fetch-mode": "cors",
            },
        )

    assert r.status_code == 200, r.status_code
    assert received_bodies and b"initialize" in received_bodies[-1], "body was not forwarded"

    for name in ("authorization", "mcp-session-id", "mcp-protocol-version", "last-event-id", "accept"):
        assert name in received_headers, f"{name} should have been forwarded"
    for name in ("cookie", "origin", "referer", "x-forwarded-for", "sec-fetch-mode"):
        assert name not in received_headers, f"{name} must not be forwarded"
    assert received_headers.get("accept-encoding") == "identity", received_headers.get("accept-encoding")

    lowered = {k.lower() for k in r.headers}
    for name in REQUIRED_PASSTHROUGH_RESPONSE_HEADERS[:3]:
        assert name in lowered, f"{name} must reach the browser"
    assert r.headers["mcp-session-id"] == "sess-42"
    for name in ("set-cookie", "strict-transport-security"):
        assert name not in lowered, f"{name} must be stripped"
    assert r.headers.get("access-control-allow-origin") != "https://evil.example", (
        "upstream CORS headers must not override this app's own"
    )
    print("  ✓ POST body/status pass through; request allowlist and response denylist hold")


async def test_status_codes_pass_through(authority: str):
    async with _make_app_client() as c:
        for code in (400, 404, 405, 406, 500):
            r = await c.get(f"/mcp-proxy/http/{authority}/status/{code}")
            assert r.status_code == code, f"expected {code}, got {r.status_code}"
            assert "x-mcp-proxy-error" not in {k.lower() for k in r.headers}
    print("  ✓ upstream 400/404/405/406/500 pass through verbatim (transport fallback needs this)")


async def test_sse_endpoint_rewritten(authority: str):
    async with _make_app_client() as c:
        r = await c.get(f"/mcp-proxy/http/{authority}/sse", headers={"accept": "text/event-stream"})
        text = r.text

    expected = f"data: /mcp-proxy/http/{authority}/messages?sessionId=abc123"
    assert expected in text, f"endpoint not rewritten:\n{text}"
    assert "data: /messages?sessionId=abc123" not in text, "original endpoint leaked"
    # Non-endpoint events must be untouched.
    assert 'data: {"jsonrpc":"2.0"}' in text, text
    print("  ✓ SSE 'endpoint' event rewritten across a chunk boundary; other events untouched")


async def test_redirect_location_rewritten(authority: str):
    async with _make_app_client() as c:
        r = await c.get(f"/mcp-proxy/http/{authority}/redirect", follow_redirects=False)
    assert r.status_code == 302, r.status_code
    assert r.headers["location"] == f"/mcp-proxy/http/{authority}/moved/mcp", r.headers["location"]
    print("  ✓ redirect Location rewritten to a proxy path instead of being followed")


async def test_ssrf_guard():
    prev = os.environ.pop("MCP_PROXY_ALLOW_PRIVATE", None)
    try:
        async with _make_app_client() as c:
            for path, expected in (
                ("/mcp-proxy/https/127.0.0.1/mcp", 403),
                ("/mcp-proxy/https/169.254.169.254/latest/meta-data/", 403),
                ("/mcp-proxy/https/10.0.0.1/mcp", 403),
                ("/mcp-proxy/https/[::1]/mcp", 403),
                ("/mcp-proxy/file/etc/passwd", 400),
                ("/mcp-proxy/https/user:pw@example.com/mcp", 400),
            ):
                r = await c.get(path)
                assert r.status_code == expected, f"{path}: expected {expected}, got {r.status_code}"
                assert r.headers.get("x-mcp-proxy-error") == "1", path

        # Direct unit checks, including the self-loop guard.
        for url, expected in (
            ("http://testserver/mcp", 403),
            ("https://example.com/mcp-proxy/https/1.1.1.1/", 403),
        ):
            try:
                await validate_target(url, self_authority="testserver")
            except ProxyTargetError as exc:
                assert exc.status == expected, (url, exc.status)
            else:
                raise AssertionError(f"{url} should have been rejected")
    finally:
        if prev is not None:
            os.environ["MCP_PROXY_ALLOW_PRIVATE"] = prev
    print("  ✓ loopback/link-local/private/[::1] → 403, bad scheme and userinfo → 400, self-loop → 403")


def test_to_proxy_path():
    assert to_proxy_path("https://api.example.com/mcp") == "/mcp-proxy/https/api.example.com/mcp"
    assert to_proxy_path("http://h:8080/a/b?x=1&y=2") == "/mcp-proxy/http/h:8080/a/b?x=1&y=2"
    assert to_proxy_path("https://api.example.com") == "/mcp-proxy/https/api.example.com/"
    print("  ✓ to_proxy_path builds the segment form for host, port, path and query")


async def main():
    server, authority = _start_upstream()
    os.environ["MCP_PROXY_ALLOW_PRIVATE"] = "1"  # the upstream is on loopback
    try:
        print("routing:")
        await test_health()
        test_to_proxy_path()
        print("passthrough:")
        await test_post_passthrough_and_headers(authority)
        await test_status_codes_pass_through(authority)
        await test_redirect_location_rewritten(authority)
        print("sse:")
        await test_sse_endpoint_rewritten(authority)
        print("ssrf guard:")
        await test_ssrf_guard()
    finally:
        os.environ.pop("MCP_PROXY_ALLOW_PRIVATE", None)
        server.shutdown()
        server.server_close()
        await mcp_proxy.close_client()

    print("\nAll /mcp-proxy checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
