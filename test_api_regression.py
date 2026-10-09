"""
Regression checks for the API surfaces the connection work was *not* supposed to
touch: the health route, the stdio connection test, and the backend OAuth proxy.

Both are exercised against real counterparts rather than mocks — a real stdio MCP
server launched as a subprocess, and a real (if minimal) RFC 9728 / 8414 / 7591
authorization server on loopback that verifies the PKCE challenge.

Run directly:  .venv/bin/python test_api_regression.py
"""

import asyncio
import base64
import hashlib
import os
import sys
import tempfile
from urllib.parse import parse_qs, urlparse

import httpx
from starlette.applications import Starlette
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Route

from test_transport_fallback_e2e import BackgroundServer, _unused_port

STDIO_SERVER_SOURCE = '''
from mcp.server.fastmcp import FastMCP

server = FastMCP(name="stdio-regression-probe")


@server.tool()
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


if __name__ == "__main__":
    server.run()
'''


async def test_health_and_stdio_connection(client: httpx.AsyncClient):
    health = await client.get("/")
    assert health.status_code == 200, health.status_code

    with tempfile.TemporaryDirectory() as tmp:
        script = os.path.join(tmp, "stdio_probe.py")
        with open(script, "w") as fh:
            fh.write(STDIO_SERVER_SOURCE)

        resp = await client.post("/api/test-stdio-connection", json={
            "name": "stdio-regression-probe",
            "command": sys.executable,
            "args": [script],
        })

    body = resp.json()
    assert resp.status_code == 200, resp.status_code
    assert body.get("success") is True and body.get("connected") is True, body
    print("  ✓ GET / healthy; POST /api/test-stdio-connection connected to a real stdio server")


def _build_authorization_server(port: int, issued: dict) -> Starlette:
    """
    A minimal but real OAuth 2.1 authorization server + protected resource:
    RFC 9728 resource metadata → RFC 8414 AS metadata → RFC 7591 registration →
    authorization code with PKCE S256 → token.
    """
    origin = f"http://127.0.0.1:{port}"

    async def resource_metadata(_request):
        return JSONResponse({
            "resource": f"{origin}/mcp",
            "authorization_servers": [origin],
        })

    async def as_metadata(_request):
        return JSONResponse({
            "issuer": origin,
            "authorization_endpoint": f"{origin}/authorize",
            "token_endpoint": f"{origin}/token",
            "registration_endpoint": f"{origin}/register",
            "scopes_supported": ["mcp:read"],
            "code_challenge_methods_supported": ["S256"],
        })

    async def register(request):
        payload = await request.json()
        assert payload.get("token_endpoint_auth_method") == "none", payload
        issued["redirect_uris"] = payload.get("redirect_uris")
        return JSONResponse({"client_id": "dcr-client-1", "client_name": payload.get("client_name")})

    async def authorize(request):
        q = request.query_params
        assert q.get("code_challenge_method") == "S256", dict(q)
        assert q.get("client_id") == "dcr-client-1", dict(q)
        # The resource indicator from the RFC 9728 document must be forwarded (RFC 8707).
        assert q.get("resource") == f"{origin}/mcp", dict(q)
        issued["challenge"] = q.get("code_challenge")
        issued["code"] = "auth-code-xyz"
        redirect = f"{q.get('redirect_uri')}?code={issued['code']}&state={q.get('state')}"
        return RedirectResponse(redirect, status_code=302)

    async def token(request):
        form = await request.form()
        assert form.get("grant_type") == "authorization_code", dict(form)
        assert form.get("code") == issued["code"], dict(form)
        verifier = form.get("code_verifier") or ""
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()
        ).decode().rstrip("=")
        if expected != issued["challenge"]:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        issued["pkce_verified"] = True
        return JSONResponse({
            "access_token": "access-token-abc",
            "refresh_token": "refresh-token-def",
            "token_type": "Bearer",
            "scope": "mcp:read",
            "expires_in": 3600,
        })

    return Starlette(routes=[
        Route("/.well-known/oauth-protected-resource/mcp", resource_metadata),
        Route("/.well-known/oauth-protected-resource", resource_metadata),
        Route("/.well-known/oauth-authorization-server", as_metadata),
        Route("/register", register, methods=["POST"]),
        Route("/authorize", authorize),
        Route("/token", token, methods=["POST"]),
    ])


async def test_backend_oauth_flow(client: httpx.AsyncClient):
    as_port = _unused_port()
    issued: dict = {}

    with BackgroundServer(_build_authorization_server(as_port, issued), as_port):
        started = await client.post("/api/oauth/start", json={
            "endpoint_url": f"http://127.0.0.1:{as_port}/mcp",
        })
        body = started.json()
        assert not body.get("error"), body
        session_id = body["session_id"]
        authorize_url = body["authorize_url"]
        assert issued.get("redirect_uris"), "dynamic client registration did not happen"

        # Stand in for the browser: follow the authorize redirect back to the backend.
        async with httpx.AsyncClient(follow_redirects=False, timeout=15.0) as browser:
            redirected = await browser.get(authorize_url)
        assert redirected.status_code == 302, redirected.status_code
        callback = urlparse(redirected.headers["location"])
        params = parse_qs(callback.query)

        # A forged state must be rejected before any token exchange (CSRF guard).
        # The backend looks the session up *by* state, so an unknown state cannot
        # resolve to a session at all — the tell is that no token call happened.
        forged = await client.get(callback.path, params={"code": params["code"][0], "state": "not-my-state"})
        assert forged.status_code in (200, 400), forged.status_code
        assert "not found" in forged.text.lower() or "invalid" in forged.text.lower(), forged.text[:200]
        assert not issued.get("pkce_verified"), "forged state still reached the token endpoint"

        completed = await client.get(callback.path, params={
            "code": params["code"][0], "state": params["state"][0],
        })
        assert completed.status_code == 200, completed.status_code

        status = await client.get(f"/api/oauth/status/{session_id}")

    payload = status.json()
    assert payload.get("status") == "complete", payload
    token = payload.get("token") or {}
    assert token.get("access_token") == "access-token-abc", payload
    assert token.get("refresh_token") == "refresh-token-def", payload
    assert issued.get("pkce_verified") is True, "token endpoint never validated the PKCE verifier"
    print("  ✓ /api/oauth/start → authorize → /api/oauth/callback → status complete")
    print("    (RFC 7591 registration, RFC 8707 resource indicator, PKCE S256 and the")
    print("     state CSRF guard all exercised end to end)")


async def main():
    from api_bridge.main import app as api_app

    port = _unused_port()
    with BackgroundServer(api_app, port):
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=60.0) as client:
            await test_health_and_stdio_connection(client)
            await test_backend_oauth_flow(client)

    print("\nAPI regression checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
