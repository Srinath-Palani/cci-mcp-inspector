"""
Backend OAuth proxy for MCP servers whose authorization servers block
browser-originated requests via CORS.

The whole OAuth 2.1 Authorization Code + PKCE flow runs server-side:

1. POST /api/oauth/start
   → discover the authorization server via RFC 9728
     (/.well-known/oauth-protected-resource) then RFC 8414
     (/.well-known/oauth-authorization-server)
   → dynamic client registration (RFC 7591) when no client_id is known
   → generate PKCE + state, store an OAuthSession, return the authorize URL
     for the browser to open in a popup.
2. GET /api/oauth/callback?code&state
   → the authorization server redirects HERE (localhost backend — CORS does
     not apply to server-side requests); the code is exchanged for tokens
     directly from Python.
3. GET /api/oauth/status/{session_id}
   → the frontend polls until the session is complete, then injects the token
     into the inspection request.

Sessions are in-memory with a 10-minute TTL — this is a local, single-process tool.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional
from urllib.parse import urlencode, urlparse

import httpx


SESSION_TTL_SECONDS = 600
DEFAULT_CLIENT_NAME = "MCP Server Inspector"


@dataclass
class OAuthSession:
    session_id: str
    endpoint_url: str
    redirect_uri: str
    status: str = "pending"           # pending | complete | error
    code_verifier: str = ""
    state: str = ""
    authorize_url: str = ""
    token_url: str = ""
    client_id: str = ""
    client_secret: Optional[str] = None
    scopes: Optional[list] = None
    resource: Optional[str] = None    # RFC 8707 resource indicator
    token: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    created_at: float = field(default_factory=time.time)

    @property
    def expired(self) -> bool:
        return time.time() - self.created_at > SESSION_TTL_SECONDS


def _generate_pkce() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) per RFC 7636 S256."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    return verifier, challenge


async def _fetch_json(client: httpx.AsyncClient, url: str) -> Optional[Dict[str, Any]]:
    try:
        resp = await client.get(url, timeout=10.0)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None


async def discover_authorization_server(endpoint_url: str) -> Dict[str, Any]:
    """
    Discover OAuth endpoints for an MCP server.

    Order:
    1. RFC 9728 protected resource metadata on the MCP server's origin —
       points at the authorization server(s).
    2. RFC 8414 authorization server metadata (on the discovered AS origin,
       falling back to the MCP origin itself).

    Returns a dict with authorization_endpoint, token_endpoint, and optionally
    registration_endpoint / scopes_supported / resource.

    Raises ValueError when no OAuth metadata can be found.
    """
    parsed = urlparse(endpoint_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    async with httpx.AsyncClient(follow_redirects=True) as client:
        # RFC 9728 — protected resource metadata (path-aware then origin-wide)
        resource_metadata = None
        path = parsed.path.rstrip("/")
        candidates = []
        if path:
            candidates.append(f"{origin}/.well-known/oauth-protected-resource{path}")
        candidates.append(f"{origin}/.well-known/oauth-protected-resource")

        for url in candidates:
            resource_metadata = await _fetch_json(client, url)
            if resource_metadata:
                break

        auth_server_origins = []
        resource_indicator = None
        if resource_metadata:
            auth_server_origins = resource_metadata.get("authorization_servers") or []
            resource_indicator = resource_metadata.get("resource")

        # Fall back to the MCP origin as its own authorization server
        if not auth_server_origins:
            auth_server_origins = [origin]

        # RFC 8414 — authorization server metadata
        for as_origin in auth_server_origins:
            as_parsed = urlparse(as_origin)
            as_base = f"{as_parsed.scheme}://{as_parsed.netloc}"
            as_path = as_parsed.path.rstrip("/")
            metadata_urls = []
            if as_path:
                metadata_urls.append(f"{as_base}/.well-known/oauth-authorization-server{as_path}")
            metadata_urls.append(f"{as_base}/.well-known/oauth-authorization-server")
            metadata_urls.append(f"{as_base}/.well-known/openid-configuration")

            for url in metadata_urls:
                metadata = await _fetch_json(client, url)
                if metadata and metadata.get("authorization_endpoint") and metadata.get("token_endpoint"):
                    result = {
                        "authorization_endpoint": metadata["authorization_endpoint"],
                        "token_endpoint": metadata["token_endpoint"],
                        "registration_endpoint": metadata.get("registration_endpoint"),
                        "scopes_supported": metadata.get("scopes_supported"),
                        "resource": resource_indicator,
                    }
                    return result

    raise ValueError(
        f"OAuth sign-in isn't possible: {endpoint_url} publishes no authorization-server "
        f"metadata (checked RFC 9728 /.well-known/oauth-protected-resource and "
        f"RFC 8414 /.well-known/oauth-authorization-server). The server may still require "
        f"a token — switch the dropdown to Bearer and paste one, or ask the server "
        f"operator for its OAuth endpoints."
    )


async def register_client(registration_endpoint: str, redirect_uri: str) -> Dict[str, Any]:
    """RFC 7591 dynamic client registration (public client, PKCE, no secret)."""
    payload = {
        "client_name": DEFAULT_CLIENT_NAME,
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(registration_endpoint, json=payload, timeout=15.0)
        resp.raise_for_status()
        return resp.json()


class OAuthSessionManager:
    """Registry of in-flight OAuth proxy sessions."""

    def __init__(self):
        self._sessions: Dict[str, OAuthSession] = {}

    async def start_session(
        self,
        endpoint_url: str,
        redirect_uri: str,
        scopes: Optional[list] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
    ) -> OAuthSession:
        """Discover, register if needed, and build the authorize URL."""
        self._cleanup()

        metadata = await discover_authorization_server(endpoint_url)

        # Dynamic client registration when the caller has no client_id
        if not client_id:
            registration_endpoint = metadata.get("registration_endpoint")
            if not registration_endpoint:
                raise ValueError(
                    "The authorization server does not support dynamic client "
                    "registration (RFC 7591) and no client_id was provided. "
                    "Supply a pre-registered client_id."
                )
            registration = await register_client(registration_endpoint, redirect_uri)
            client_id = registration["client_id"]
            client_secret = registration.get("client_secret")

        verifier, challenge = _generate_pkce()
        state = secrets.token_urlsafe(24)
        session_id = secrets.token_urlsafe(16)

        effective_scopes = scopes or metadata.get("scopes_supported") or None

        params = {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
        if effective_scopes:
            params["scope"] = " ".join(effective_scopes)
        if metadata.get("resource"):
            params["resource"] = metadata["resource"]

        authorize_url = f"{metadata['authorization_endpoint']}?{urlencode(params)}"

        session = OAuthSession(
            session_id=session_id,
            endpoint_url=endpoint_url,
            redirect_uri=redirect_uri,
            code_verifier=verifier,
            state=state,
            authorize_url=authorize_url,
            token_url=metadata["token_endpoint"],
            client_id=client_id,
            client_secret=client_secret,
            scopes=effective_scopes,
            resource=metadata.get("resource"),
        )
        self._sessions[session_id] = session
        return session

    def find_by_state(self, state: str) -> Optional[OAuthSession]:
        """Look up a pending session by its OAuth state parameter (CSRF check)."""
        for session in self._sessions.values():
            if session.state == state and session.status == "pending" and not session.expired:
                return session
        return None

    def get(self, session_id: str) -> Optional[OAuthSession]:
        session = self._sessions.get(session_id)
        if session and session.expired and session.status == "pending":
            session.status = "error"
            session.error_message = "OAuth session expired (10 minutes). Start again."
        return session

    async def exchange_code(self, session: OAuthSession, code: str) -> None:
        """Exchange the authorization code for tokens — server-side, no CORS."""
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": session.redirect_uri,
            "client_id": session.client_id,
            "code_verifier": session.code_verifier,
        }
        if session.client_secret:
            data["client_secret"] = session.client_secret
        if session.resource:
            data["resource"] = session.resource

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    session.token_url,
                    data=data,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    timeout=30.0,
                )
                resp.raise_for_status()
                token_data = resp.json()

            expires_at = None
            if token_data.get("expires_in"):
                expires_at = int(time.time()) + int(token_data["expires_in"])

            session.token = {
                "access_token": token_data.get("access_token"),
                "refresh_token": token_data.get("refresh_token"),
                "token_type": token_data.get("token_type", "Bearer"),
                "scope": token_data.get("scope"),
                "client_id": session.client_id,
                "token_url": session.token_url,
                "expires_at": expires_at,
            }
            session.status = "complete"
        except httpx.HTTPStatusError as e:
            # Log status + OAuth error field only — never the full body, which
            # may echo credentials
            error_field = ""
            try:
                error_field = e.response.json().get("error", "")
            except Exception:
                pass
            session.status = "error"
            session.error_message = (
                f"Token exchange failed with HTTP {e.response.status_code}"
                + (f" ({error_field})" if error_field else "")
            )
        except Exception as e:
            session.status = "error"
            session.error_message = f"Token exchange failed: {type(e).__name__}: {e}"

    def _cleanup(self) -> None:
        expired = [sid for sid, s in self._sessions.items() if s.expired]
        for sid in expired:
            del self._sessions[sid]


oauth_session_manager = OAuthSessionManager()
