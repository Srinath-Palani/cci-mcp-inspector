"""
MCP Inspector API Bridge

FastAPI server that exposes Server Inspector functionality as REST API.
Handles OAuth token discovery and full server inspection.
"""

import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional, Dict, Any
import json

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

# Add Server Inspector to path
server_inspector_path = Path(__file__).parent.parent
sys.path.insert(0, str(server_inspector_path))

from src.utility.auth_discovery import AuthenticationDiscovery
from src.workflows.mcp_inspector_workflow import run_mcp_inspection, _build_final_report, _print_inspection_summary
from src.utility.utils import Utils
from src.models.structured_output import ErrorDetails
from api_bridge.error_classifier import classify_error, classify_failure_result
from api_bridge.inspection_job_manager import job_manager
from api_bridge import mcp_proxy

# Import functions from generate_mcp_config_from_oauth.py for stdio config generation
generate_config_path = server_inspector_path / "generate_mcp_config_from_oauth.py"
if generate_config_path.exists():
    import importlib.util
    spec = importlib.util.spec_from_file_location("generate_mcp_config", generate_config_path)
    generate_mcp_config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generate_mcp_config)
    parse_github_repository_for_stdio = generate_mcp_config.parse_github_repository_for_stdio
    extract_env_vars_from_github = generate_mcp_config.extract_env_vars_from_github
else:
    # Fallback if file not found
    parse_github_repository_for_stdio = None
    extract_env_vars_from_github = None

@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Own the /mcp-proxy upstream client for the process lifetime."""
    try:
        yield
    finally:
        await mcp_proxy.close_client()


app = FastAPI(title="MCP Inspector API Bridge", version="1.0.0", lifespan=lifespan)

# --- Access control ---------------------------------------------------------
# The API executes attacker-suppliable commands (stdio inspect) and proxies to
# arbitrary URLs (/mcp-proxy), so it must not be reachable by other machines.
# Defense in depth:
#   1. bind 127.0.0.1 only (see __main__ below), and
#   2. require a per-start bearer token on every route unless the request comes
#      from the loopback UI.
#
# MCP_INSPECTOR_TOKEN: set to require `Authorization: Bearer <token>` from ALL
# callers (including localhost). Unset (the default for local dev), loopback
# callers pass unauthenticated and non-loopback callers are rejected outright —
# which, combined with the loopback bind, means remote callers get nothing.
import os
import secrets as _secrets

_REQUIRED_TOKEN = os.environ.get("MCP_INSPECTOR_TOKEN") or None


@app.middleware("http")
async def _enforce_local_or_token(request: Request, call_next):
    if _REQUIRED_TOKEN is not None:
        # Token mode: the bearer token is the only credential, from any host.
        # This is how the API is exposed beyond localhost deliberately — set the
        # token AND bind 0.0.0.0 (or tunnel) when remote access is intended.
        auth = request.headers.get("authorization", "")
        if auth != f"Bearer {_REQUIRED_TOKEN}":
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    else:
        # No token configured (default local-dev mode): loopback only. A client
        # whose address can't be determined (request.client is None) is treated
        # as local — that only happens on Unix sockets / in-process ASGI, never
        # for a real TCP peer.
        client_host = request.client.host if request.client else None
        is_loopback = (
            client_host is None
            or client_host in ("127.0.0.1", "::1", "localhost")
            or client_host.startswith("127.")
        )
        if not is_loopback:
            return JSONResponse({"detail": "Forbidden: loopback only"}, status_code=403)

    return await call_next(request)


# Streaming CORS bypass for browser MCP connections (see api_bridge/mcp_proxy.py)
app.include_router(mcp_proxy.router)

# Enable CORS for localhost
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5001",
        "http://localhost:5003",  # Frontend port
        "http://localhost:5173",
        "http://127.0.0.1:5001",
        "http://127.0.0.1:5003",  # Frontend port
        "http://127.0.0.1:5173",
        "http://localhost:3000",  # Common React dev port
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # Response headers the browser MCP client must be able to read. Without this
    # a cross-origin `response.headers.get('mcp-session-id')` returns null and
    # every session-based server fails silently right after initialize. Redundant
    # when the vite dev server proxies /mcp-proxy (same origin), required when the
    # browser talks to :8000 directly.
    expose_headers=[
        "Mcp-Session-Id",
        "MCP-Protocol-Version",
        "WWW-Authenticate",
        "Retry-After",
        "X-MCP-Proxy-Error",
    ],
)

# Initialize utils
utils = Utils()

# Stdio inspection spawns a subprocess from request-supplied `command`/`args` —
# arbitrary code execution by design. It is off unless the operator opts in with
# MCP_INSPECTOR_ALLOW_STDIO=1, so a casually-started server cannot be turned into
# an RCE endpoint. Local interactive use sets the flag; deployed use should not
# enable it without an allowlist in front.
_ALLOW_STDIO_ENV = "MCP_INSPECTOR_ALLOW_STDIO"


def _stdio_allowed() -> bool:
    return os.environ.get(_ALLOW_STDIO_ENV, "").strip() in ("1", "true", "yes")


def _stdio_disabled_response(model_cls, **extra):
    msg = (
        "Stdio inspection is disabled on this server. Stdio spawns a subprocess "
        f"from request-supplied commands, so it requires an explicit opt-in: set "
        f"{_ALLOW_STDIO_ENV}=1 in the server environment and restart."
    )
    return model_cls(success=False, error=msg, **extra)


# Request/Response Models
class DiscoverAuthRequest(BaseModel):
    endpoint_url: str


class GenerateStdioConfigRequest(BaseModel):
    repository_url: str
    server_name: Optional[str] = None


class GenerateStdioConfigResponse(BaseModel):
    success: bool
    config: Optional[Dict[str, Any]] = None
    env_vars: Optional[list] = None
    error: Optional[str] = None


class TestStdioConnectionRequest(BaseModel):
    name: str
    command: str
    args: list
    env: Optional[Dict[str, str]] = None


class TestStdioConnectionResponse(BaseModel):
    success: bool
    connected: bool
    error: Optional[str] = None


class TargetAuth(BaseModel):
    """
    One server's own credential.

    Every remote server authenticates separately — its own API key, its own bearer
    token, its own OAuth grant — so this travels per target and is never shared
    across a run. `mode` decides which fields matter:

      none     – no credential
      bearer   – token → "Authorization: Bearer <token>"
      api_key  – token → "<header>: <token>", header defaults to X-API-Key
      oauth    – tokens{} from a completed /api/oauth/start flow for this endpoint

    Kept separate from the legacy `authentication` blob because that one only ever
    produced a Bearer header, which silently sent API keys to the wrong place.
    """
    mode: str = "none"
    token: Optional[str] = None
    header: Optional[str] = None
    tokens: Optional[Dict[str, Any]] = None


class InspectRequest(BaseModel):
    # Accept full connection details format from UI
    name: Optional[str] = None
    description: Optional[str] = None
    connection_type: str = "sse"  # Default to SSE for remote servers
    endpoint_url: Optional[str] = None  # Optional for stdio connections
    authentication: Optional[Dict[str, Any]] = None  # Full authentication object from UI
    auth: Optional[TargetAuth] = None  # Per-server credential; takes precedence

    # Stdio connection fields
    command: Optional[str] = None
    args: Optional[list] = None
    env: Optional[Dict[str, str]] = None
    repository: Optional[str] = None  # GitHub repository URL for stdio servers
    
    # Optional metadata fields (used for remote endpoint)
    github_repo_link: Optional[str] = None  # GitHub repository URL for remote servers
    distribution_type: Optional[str] = None  # "official" or "community"
    
    # Names of env vars the server requires (from /api/generate-stdio-config) —
    # used to validate provided values and fill gaps from the backend environment
    required_env_vars: Optional[list] = None

    # Legacy fields for backward compatibility
    server_name: Optional[str] = None
    oauth_tokens: Optional[Dict[str, Any]] = None


class InspectResponse(BaseModel):
    success: bool
    report_path: Optional[str] = None
    report_data: Optional[Dict[str, Any]] = None
    report_paths: Optional[Dict[str, str]] = None  # Paths to all generated reports
    error: Optional[str] = None
    error_details: Optional[ErrorDetails] = None  # Structured error for typed UI rendering


@app.get("/")
async def root():
    return {"message": "MCP Inspector API Bridge", "version": "1.0.0"}


# ── Environment variable resolution ──────────────────────────────────────────

_ENV_PLACEHOLDER_VALUES = {"your_value_here", "your-value-here", "changeme", "change_me", "xxx", "todo", "****", "**"}


def _is_placeholder(value: str) -> bool:
    """True for obviously-unfilled env values (sample text, <angle> templates)."""
    v = (value or "").strip()
    if not v:
        return True
    if v.lower() in _ENV_PLACEHOLDER_VALUES:
        return True
    if v.startswith("<") and v.endswith(">"):
        return True
    return False


def resolve_env_vars(
    env: Optional[Dict[str, str]],
    required_vars: Optional[list] = None,
) -> tuple[Dict[str, str], list, list]:
    """
    Deterministically resolve the env dict for an inspection.

    1. Drop entries whose value is empty or a placeholder ('your_value_here', '<...>').
    2. Resolve `${VAR}` references and dropped/missing declared vars from the
       Inspector's own environment (project .env is loaded via utils.load_env()).
    3. Report what could not be resolved.

    Returns:
        (clean_env, missing_vars, placeholder_vars)
        missing_vars: declared vars with no value anywhere → ENV_VARS_MISSING
        placeholder_vars: vars whose provided value was a placeholder and could
                          not be resolved from the environment → ENV_VARS_PLACEHOLDER
    """
    import os
    utils.load_env()  # make project .env values visible in os.environ

    clean: Dict[str, str] = {}
    placeholder_unresolved: list = []

    for key, value in (env or {}).items():
        value = value if isinstance(value, str) else str(value)
        # ${VAR} / $VAR references → resolve from backend environment
        stripped = value.strip()
        if stripped.startswith("${") and stripped.endswith("}"):
            ref = stripped[2:-1]
            resolved = os.environ.get(ref) or os.environ.get(key)
            if resolved:
                clean[key] = resolved
            else:
                placeholder_unresolved.append(key)
            continue
        if _is_placeholder(value):
            resolved = os.environ.get(key)
            if resolved:
                clean[key] = resolved
                print(f"DEBUG: env var '{key}' had a placeholder value — filled from backend environment")
            else:
                placeholder_unresolved.append(key)
            continue
        clean[key] = value

    # Declared-but-absent vars → backend environment fallback
    missing: list = []
    for var in (required_vars or []):
        if var in clean:
            continue
        resolved = os.environ.get(var)
        if resolved:
            clean[var] = resolved
            print(f"DEBUG: required env var '{var}' filled from backend environment")
        elif var in placeholder_unresolved:
            pass  # already reported as placeholder
        else:
            missing.append(var)

    return clean, missing, placeholder_unresolved


def server_name_from_repo_url(repository_url: str) -> str:
    """Derive a server name from a GitHub repo URL (github.com/owner/repo → repo)."""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(repository_url if "//" in repository_url else f"https://{repository_url}")
        path_parts = [p for p in parsed.path.split("/") if p]
        if len(path_parts) >= 2:
            return path_parts[1].replace(".git", "").replace("-mcp-server", "").replace("-mcp", "")
    except Exception:
        pass
    return "mcp_server"


async def build_stdio_config_from_repo(
    repository_url: str,
    server_name: Optional[str] = None,
) -> GenerateStdioConfigResponse:
    """
    Resolve a GitHub repository into a stdio server config.

    Shared by POST /api/generate-stdio-config and the batch runner, which is why it
    lives outside the route handler. The underlying helpers in
    generate_mcp_config_from_oauth.py use blocking `requests` calls against the
    GitHub API, so they run on a worker thread: called inline they would stall the
    event loop for every other job in a batch (and the health check with it).
    """
    if not parse_github_repository_for_stdio:
        return GenerateStdioConfigResponse(
            success=False,
            error="Stdio config generation not available (generate_mcp_config_from_oauth.py not found)",
        )

    repository_url = (repository_url or "").strip()
    if not repository_url:
        return GenerateStdioConfigResponse(success=False, error="Repository URL is required")

    resolved_name = server_name or server_name_from_repo_url(repository_url)

    github_config = await asyncio.to_thread(
        parse_github_repository_for_stdio, repository_url, resolved_name
    )
    if not github_config:
        return GenerateStdioConfigResponse(
            success=False,
            error=f"Could not extract configuration from repository: {repository_url}",
        )

    env_vars = []
    if extract_env_vars_from_github:
        env_vars = await asyncio.to_thread(
            extract_env_vars_from_github, repository_url, resolved_name
        ) or []

    if github_config.get("env_vars"):
        env_vars = list({str(var).upper() for var in [*env_vars, *github_config["env_vars"]]})

    config: Dict[str, Any] = {
        "name": resolved_name,
        "description": f"{resolved_name.capitalize()} MCP Server",
        "connection_type": "stdio",
        "repository": repository_url,
    }

    if github_config.get("command"):
        config["command"] = github_config["command"]
        config["args"] = github_config.get("args", [])
    elif github_config.get("package_name"):
        config["command"] = "npx"
        config["args"] = ["-y", github_config["package_name"]]
    else:
        return GenerateStdioConfigResponse(
            success=False,
            error=(
                f"Could not determine a launch command for {repository_url}. The repository "
                "has no recognizable package manifest (package.json / pyproject.toml) or "
                "documented run command."
            ),
        )

    return GenerateStdioConfigResponse(
        success=True,
        config=config,
        env_vars=sorted(env_vars) if env_vars else None,
    )


@app.post("/api/generate-stdio-config", response_model=GenerateStdioConfigResponse)
async def generate_stdio_config(request: GenerateStdioConfigRequest):
    """
    Generate stdio configuration from GitHub repository URL.

    Extracts command, args, and environment variable names from the repository.
    """
    if not _stdio_allowed():
        return _stdio_disabled_response(GenerateStdioConfigResponse)
    try:
        return await build_stdio_config_from_repo(request.repository_url, request.server_name)
    except Exception as e:
        import traceback
        error_detail = f"Failed to generate stdio config: {str(e)}\n\n{traceback.format_exc()}"
        return GenerateStdioConfigResponse(
            success=False,
            error=error_detail
        )


@app.post("/api/test-stdio-connection", response_model=TestStdioConnectionResponse)
async def test_stdio_connection(request: TestStdioConnectionRequest):
    """
    Test stdio connection by attempting to connect and initialize the MCP server.
    """
    if not _stdio_allowed():
        return _stdio_disabled_response(TestStdioConnectionResponse, connected=False)
    try:
        server_config = {
            "name": request.name,
            "connection_type": "stdio",
            "command": request.command,
            "args": request.args,
        }
        
        if request.env:
            server_config["env"] = request.env
        
        # Try to connect and do minimal discovery
        from src.utility.mcp_connection_manager import MCPConnectionManager
        
        connection_manager = MCPConnectionManager(server_config)
        
        try:
            async with connection_manager.connect() as session:
                # Try to get server info (this tests the connection)
                await connection_manager.get_server_info(session)
                # Try to list tools (minimal test)
                await connection_manager.list_tools(session)

            return TestStdioConnectionResponse(
                success=True,
                connected=True
            )
        except Exception as e:
            # ExceptionGroup (raised by asyncio.TaskGroup / anyio on Python 3.11+)
            # wraps the real exception in e.exceptions. Unwrap one level so the
            # error message shown to the user is actually useful.
            real_exc = e
            if hasattr(e, 'exceptions') and e.exceptions:
                real_exc = e.exceptions[0]
                # Unwrap a second level if the inner exception is also a group
                if hasattr(real_exc, 'exceptions') and real_exc.exceptions:
                    real_exc = real_exc.exceptions[0]
            return TestStdioConnectionResponse(
                success=False,
                connected=False,
                error=f"Connection test failed: {type(real_exc).__name__}: {real_exc}"
            )
            
    except Exception as e:
        import traceback
        error_detail = f"Failed to test stdio connection: {str(e)}\n\n{traceback.format_exc()}"
        return TestStdioConnectionResponse(
            success=False,
            connected=False,
            error=error_detail
        )


@app.post("/api/discover-auth")
async def discover_auth(request: DiscoverAuthRequest):
    """
    Discover OAuth requirements for an MCP server endpoint.
    
    Returns:
        Dictionary with auth discovery results including:
        - auth_required: bool
        - auth_config: OAuth configuration if discovered
        - recommendations: List of recommendations
    """
    try:
        # Clean the URL - remove any trailing whitespace
        endpoint_url = request.endpoint_url.strip().rstrip() if request.endpoint_url else ""
        
        if not endpoint_url:
            return {
                "success": False,
                "auth_required": False,
                "error": "Server URL is required",
            }
        
        auth_discovery = AuthenticationDiscovery()
        result = await auth_discovery.discover_auth_requirements(
            endpoint_url,
            current_config=None
        )
        
        # Convert to JSON-serializable format
        return {
            "success": True,
            "auth_required": result.get("auth_required", False),
            "auth_type": result.get("auth_type"),
            "auth_config": result.get("auth_config"),
            "metadata_available": result.get("metadata_available", False),
            "recommendations": result.get("recommendations", []),
            "source": result.get("source"),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Auth discovery failed: {str(e)}")


def _auth_config_from_target_auth(auth: TargetAuth) -> Optional[Dict[str, Any]]:
    """
    Translate one server's credential into the connection manager's auth_config.

    Returns None when there is nothing usable, so a row with a mode selected but no
    value behaves as unauthenticated rather than sending an empty header — an empty
    "Authorization: Bearer " reads as a malformed credential to most servers and
    produces a 400 that looks like a transport problem.

    The api_key branch is the reason this exists: mcp_connection_manager sends
    api_key tokens under their own header (X-API-Key by default), which the older
    nested-tokens path could not express.
    """
    mode = (auth.mode or "none").strip().lower()
    token = (auth.token or "").strip()

    if mode == "bearer":
        return {"type": "bearer", "token": token} if token else None

    if mode in ("api_key", "apikey", "api-key"):
        if not token:
            return None
        return {"type": "api_key", "token": token, "header": (auth.header or "X-API-Key").strip()}

    if mode == "oauth":
        tokens = auth.tokens or {}
        access_token = tokens.get("access_token")
        if not access_token:
            return None
        config: Dict[str, Any] = {
            "type": "oauth2_1_authorization_code",
            "client_id": tokens.get("client_id"),
            "access_token": access_token,
            "refresh_token": tokens.get("refresh_token"),
        }
        if tokens.get("token_url"):
            config["token_url"] = tokens["token_url"]
        else:
            # Deferred for the same reason as the nested-tokens path: discovery costs
            # network round trips and must not happen in the request handler.
            config["_discover_token_url"] = True
        expires_at = tokens.get("expires_at")
        if expires_at is None and tokens.get("expires_in"):
            import time
            expires_at = int(time.time()) + int(tokens["expires_in"])
        if expires_at:
            config["expires_at"] = expires_at
        return config

    return None


async def _build_server_config_from_request(request: InspectRequest):
    """
    Build and validate the server_config dict from an InspectRequest.

    Returns:
        (server_config, output_dir, None) on success, or
        (None, None, InspectResponse) when validation fails — the response
        carries structured error_details for the UI.
    """
    import tempfile

    # Clean and validate inputs (supports both new UI format and legacy format)
    server_name = (request.name or request.server_name or "").strip()
    endpoint_url = request.endpoint_url.strip().rstrip() if request.endpoint_url else ""

    # Auto-generate server name from URL if not provided
    if not server_name and endpoint_url:
        try:
            from urllib.parse import urlparse
            parsed = urlparse(endpoint_url)
            server_name = parsed.netloc.replace("www.", "").split(".")[0] or "mcp_server"
        except Exception:
            server_name = "mcp_server"

    if not server_name:
        return None, None, InspectResponse(
            success=False,
            error="Server name is required",
            error_details=classify_error(message="Server name is required", stage="config"),
        )

    # Normalize connection_type based on URL pattern:
    #   /sse in URL  → SSE (legacy transport, explicit indicator)
    #   anything else → Streamable HTTP (current MCP standard; SSE is legacy)
    connection_type = request.connection_type.lower()
    if connection_type in ("http", "auto"):
        parsed_path = endpoint_url.split("?")[0]
        if "/sse" in parsed_path or parsed_path.endswith("/sse"):
            connection_type = "sse"
        else:
            connection_type = "http"

    if connection_type == "stdio":
        if not _stdio_allowed():
            msg = (
                "Stdio inspection is disabled on this server. Stdio spawns a "
                f"subprocess from request-supplied commands, so it requires an "
                f"explicit opt-in: set {_ALLOW_STDIO_ENV}=1 in the server "
                "environment and restart."
            )
            return None, None, InspectResponse(
                success=False,
                error=msg,
                error_details=classify_error(message=msg, stage="config"),
            )
        if not request.command or not request.command.strip():
            msg = f"Command is required for stdio connections. Received: command={request.command}, args={request.args}"
            return None, None, InspectResponse(
                success=False,
                error=msg,
                error_details=classify_error(message="command required " + msg, stage="config"),
            )

        server_config = {
            "name": server_name,
            "description": request.description or f"MCP Server: {server_name}",
            "connection_type": "stdio",
            "command": request.command.strip(),
            "args": request.args or [],
        }

        if request.repository:
            server_config["repository"] = request.repository.strip()
        else:
            print("DEBUG: No repository URL provided in request - documentation analysis may be limited")
    else:
        if not endpoint_url:
            return None, None, InspectResponse(
                success=False,
                error="Server URL is required for remote connections",
                error_details=classify_error(message="endpoint url required for remote connections", stage="config"),
            )

        server_config = {
            "name": server_name,
            "description": request.description or f"MCP Server: {server_name}",
            "connection_type": connection_type,
            "endpoint_url": endpoint_url,
        }

        if request.github_repo_link:
            server_config["repository"] = request.github_repo_link.strip()

    # Environment variables (both stdio and remote): strip placeholders,
    # resolve ${VAR} references, fill declared-but-missing vars from the
    # backend environment (project .env). Unresolvable vars are a hard error.
    if request.env or request.required_env_vars:
        clean_env, missing_vars, placeholder_vars = resolve_env_vars(
            request.env, request.required_env_vars
        )
        if placeholder_vars:
            return None, None, InspectResponse(
                success=False,
                error=f"Environment variables contain placeholder values: {', '.join(placeholder_vars)}",
                error_details=classify_error(placeholder_env_vars=placeholder_vars, stage="config"),
            )
        if missing_vars:
            return None, None, InspectResponse(
                success=False,
                error=f"Required environment variables are missing: {', '.join(missing_vars)}",
                error_details=classify_error(missing_env_vars=missing_vars, stage="config"),
            )
        if clean_env:
            server_config["env"] = clean_env

    if request.distribution_type:
        server_config["distribution_type"] = request.distribution_type.strip()

    # Authentication — per-server `auth` first, then the older nested-tokens
    # format, then legacy oauth_tokens.
    auth_config = None
    if request.auth and (request.auth.mode or "none").lower() != "none":
        auth_config = _auth_config_from_target_auth(request.auth)

    if auth_config is not None:
        pass
    elif request.authentication:
        auth_obj = request.authentication
        tokens = auth_obj.get("tokens", {})

        if tokens and auth_obj.get("has_tokens"):
            expires_at = None
            if "expires_at" in tokens:
                expires_at = tokens.get("expires_at")
            elif "expires_in" in tokens:
                import time
                expires_at = int(time.time()) + tokens.get("expires_in", 3600)

            token_url = tokens.get("token_url")

            auth_config = {
                "type": "oauth2_1_authorization_code",
                "client_id": tokens.get("client_id"),
                "access_token": tokens.get("access_token"),
                "refresh_token": tokens.get("refresh_token"),
            }
            if token_url:
                auth_config["token_url"] = token_url
            else:
                # token_url is optional (only needed to refresh the access token)
                # and discovering it costs several network round trips. Doing that
                # here would block POST /api/inspect, whose whole contract is to
                # return a job_id immediately — so it is deferred to the background
                # job instead. See _resolve_deferred_token_url.
                auth_config["_discover_token_url"] = True
            if expires_at:
                auth_config["expires_at"] = expires_at

        # Static bearer/API key sent without the has_tokens flag
        elif tokens.get("access_token"):
            auth_config = {"type": "bearer", "token": tokens.get("access_token")}

    elif request.oauth_tokens:
        auth_config = {
            "type": "oauth2_1_authorization_code",
            "token_url": request.oauth_tokens.get("token_url"),
            "client_id": request.oauth_tokens.get("client_id"),
            "access_token": request.oauth_tokens.get("access_token"),
            "refresh_token": request.oauth_tokens.get("refresh_token"),
            "expires_at": request.oauth_tokens.get("expires_at"),
        }

    if auth_config:
        server_config["authentication"] = auth_config

    # Output directory
    temp_dir = Path(tempfile.gettempdir()) / "mcp_inspector_api"
    temp_dir.mkdir(parents=True, exist_ok=True)
    output_dir = temp_dir / server_name
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"DEBUG: Final server_config before inspection: {json.dumps(_redact_secrets(server_config), indent=2)}")
    return server_config, output_dir, None


# Keys whose values are credentials and must never be printed. Env values are
# redacted wholesale rather than by key name, since a server's required var can be
# called anything.
_SECRET_KEYS = {
    "token", "access_token", "refresh_token", "client_secret", "api_key",
    "password", "authorization", "bearer_token", "id_token",
}


def _redact_secrets(value: Any, _in_env: bool = False) -> Any:
    """
    Copy a config with every credential replaced by a length marker.

    Used for logging only. A batch run carries one credential per server, so an
    unredacted dump would put every key for every server into the console and any
    log file collecting it.
    """
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for key, inner in value.items():
            lowered = str(key).lower()
            if _in_env or lowered in _SECRET_KEYS:
                out[key] = f"<redacted {len(str(inner))} chars>" if inner else inner
            else:
                out[key] = _redact_secrets(inner, _in_env=lowered == "env")
        return out
    if isinstance(value, list):
        return [_redact_secrets(v, _in_env=_in_env) for v in value]
    return value


def _collect_report_response(report_path: Path, output_dir: Path) -> InspectResponse:
    """Load the final report and enumerate all generated report files."""
    with open(report_path, "r") as f:
        report_data = json.load(f)

    report_paths = {
        "json": str(report_path),
        "markdown": str(output_dir / "attribute_checklist.md"),
        "html": str(output_dir / "attribute_report.html"),
        "csv": str(output_dir / "attribute_checklist.csv"),
        "attributes_csv": str(output_dir / "attributes.csv"),
        "txt": str(output_dir / "attribute_extraction_raw.txt"),
        "capabilities_csv": str(output_dir / "mcp_capabilities.csv"),
    }
    existing_reports = {k: v for k, v in report_paths.items() if Path(v).exists()}

    return InspectResponse(
        success=True,
        report_path=str(report_path),
        report_data=report_data,
        report_paths=existing_reports or None,
    )


class InspectionPipelineError(Exception):
    """
    A pipeline phase reported FAILURE (as opposed to raising).

    Holds the phase's own result dict so `_classify_exception` can classify from
    the structured fields the agent produced (`status_code`, `failure_kind`)
    instead of re-parsing the human-readable message.
    """

    def __init__(
        self,
        message: str,
        failure_result: Optional[Dict[str, Any]] = None,
        stage: str = "unknown",
    ) -> None:
        super().__init__(message)
        self.failure_result = failure_result
        self.stage = stage


def _classify_exception(e: Exception) -> tuple:
    """Unwrap ExceptionGroups and return (error_text, ErrorDetails)."""
    import traceback
    tb_str = traceback.format_exc()

    if isinstance(e, InspectionPipelineError) and e.failure_result:
        message = str(e)
        return (
            f"Inspection failed: {message}",
            classify_failure_result(e.failure_result, stage=e.stage),
        )

    real_exc = e
    if hasattr(e, "exceptions") and e.exceptions:
        real_exc = e.exceptions[0]
        if hasattr(real_exc, "exceptions") and real_exc.exceptions:
            real_exc = real_exc.exceptions[0]

    if real_exc is not e:
        error_str = f"{type(real_exc).__name__}: {real_exc}"
    else:
        error_str = str(e)
        if getattr(e, "__cause__", None):
            error_str = f"{error_str}\n\nCaused by: {e.__cause__}"

    error_detail = f"Inspection failed: {error_str}\n\nFull traceback:\n{tb_str}"
    return error_detail, classify_error(exc=real_exc, message=error_str, stage="mcp_discovery")


async def _resolve_deferred_token_url(server_config: Dict[str, Any]) -> None:
    """
    Fill in an OAuth token_url that _build_server_config_from_request deferred.

    Runs inside the background job (not the request handler) because auth-metadata
    discovery does several network round trips. Best-effort: token_url is only
    needed to refresh an access token we already have, so any failure is ignored.
    Mutates server_config in place and always removes the internal marker.
    """
    auth_config = server_config.get("authentication") or {}
    if not auth_config.pop("_discover_token_url", False):
        return

    endpoint_url = server_config.get("endpoint_url")
    if not endpoint_url:
        return

    try:
        auth_discovery = AuthenticationDiscovery()
        discovery_result = await auth_discovery.discover_auth_requirements(
            endpoint_url, current_config=None
        )
        token_url = (discovery_result.get("auth_config") or {}).get("token_url")
        if token_url:
            auth_config["token_url"] = token_url
    except asyncio.CancelledError:
        raise
    except Exception:
        pass  # token_url is optional


def _timeout_message(job_id: str) -> str:
    """
    Build a timeout message that names the phase that actually stalled.

    The previous wording always blamed the MCP handshake, which is misleading
    when the stall was in documentation analysis or an LLM call.
    """
    job = job_manager.get_job(job_id)
    phase = getattr(job, "current_phase", None) if job else None

    base = f"Inspection timed out after {INSPECTION_TIMEOUT_SECONDS:.0f} seconds"
    if phase:
        return (
            f"{base} during the '{phase}' phase. "
            "Check that the endpoint URL and transport type are correct, and that any "
            "documentation/LLM analysis steps can reach the network."
        )
    return (
        f"{base} before reporting a phase. The server may have accepted the connection "
        "without ever responding to the MCP handshake — check the endpoint URL and transport type."
    )


async def _run_inspection_job(job_id: str, server_config: Dict[str, Any], output_dir: Path) -> None:
    """Background task: run one inspection through the shared LangGraph pipeline."""
    def phase_callback(phase: str, progress: float) -> None:
        job_manager.update_phase(job_id, phase, progress)

    try:
        # Deferred out of the request handler so POST /api/inspect stays fast.
        await _resolve_deferred_token_url(server_config)

        report_path = await asyncio.wait_for(
            run_inspection_with_config(server_config, output_dir, phase_callback=phase_callback),
            timeout=INSPECTION_TIMEOUT_SECONDS,
        )
        response = _collect_report_response(Path(report_path), output_dir)
        job_manager.complete_job(job_id, response.model_dump())

    except asyncio.CancelledError:
        # cancel_job() already set the state; just let cleanup run
        raise
    except asyncio.TimeoutError:
        timeout_msg = _timeout_message(job_id)
        details = classify_error(message=timeout_msg + " timed out", stage="mcp_discovery")
        job_manager.fail_job(job_id, timeout_msg, details.model_dump())
    except Exception as e:
        error_detail, details = _classify_exception(e)
        job_manager.fail_job(job_id, error_detail, details.model_dump())


INSPECTION_TIMEOUT_SECONDS = 300.0  # generous — jobs are cancellable from the UI


class InspectStartResponse(BaseModel):
    job_id: Optional[str] = None
    status: str  # "started" | "error"
    error: Optional[str] = None
    error_details: Optional[ErrorDetails] = None


class JobCancelResponse(BaseModel):
    success: bool
    message: str


@app.post("/api/inspect", response_model=InspectStartResponse)
async def inspect_server(request: InspectRequest):
    """
    Start an MCP server inspection as a cancellable background job.

    Returns a job_id immediately. Poll GET /api/inspect/{job_id}/status for
    phase progress and the final result; POST /api/inspect/{job_id}/cancel to
    abort (the Disconnect button in the UI).
    """
    try:
        server_config, output_dir, error_response = await _build_server_config_from_request(request)
        if error_response is not None:
            return InspectStartResponse(
                status="error",
                error=error_response.error,
                error_details=error_response.error_details,
            )

        job = job_manager.create_job(server_config["name"])
        task = asyncio.create_task(_run_inspection_job(job.job_id, server_config, output_dir))
        job_manager.mark_running(job.job_id, task)

        return InspectStartResponse(job_id=job.job_id, status="started")

    except Exception as e:
        error_detail, details = _classify_exception(e)
        return InspectStartResponse(status="error", error=error_detail, error_details=details)


@app.get("/api/inspect/{job_id}/status")
async def inspection_status(job_id: str):
    """Poll the state of an inspection job (state, current phase, progress, result)."""
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Unknown inspection job: {job_id}")
    return job.status_payload()


@app.post("/api/inspect/{job_id}/cancel", response_model=JobCancelResponse)
async def cancel_inspection(job_id: str):
    """Cancel a running inspection job (Disconnect). Closes the MCP session."""
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Unknown inspection job: {job_id}")
    if job_manager.cancel_job(job_id):
        return JobCancelResponse(success=True, message="Inspection cancelled")
    return JobCancelResponse(success=False, message=f"Job is already {job.state}")


# ── Batch inspection (dashboard + import from client config) ─────────────────

# Concurrency bounds. Each concurrent job holds an MCP session, several outbound
# HTTP connections and (in the analysis phase) an LLM request, so this is a real
# resource limit rather than a cosmetic one — hence a hard ceiling the request
# cannot raise.
#
# The ceiling is 5, matching the local GitHub-repo workload it was set for: each
# repo row also spawns a subprocess and may install packages, which is bound by
# this machine's CPU and disk rather than by the network. Measured on 10 remote
# endpoints, 10-at-a-time finished in 41s vs 75s at 5 — so raising this trades
# machine headroom for wall clock. Raise MAX only alongside a separate, lower
# sub-limit for stdio/GitHub rows.
MIN_BATCH_CONCURRENCY = 1
MAX_BATCH_CONCURRENCY = 5
DEFAULT_BATCH_CONCURRENCY = 5

# Row-identity prefix per target kind: R1 = first remote endpoint, G1 = first
# GitHub repo, S1 = first stdio entry from a config import, X1 = an unparseable
# line (which still gets a row, so the user can see which paste was rejected).
_IDENTITY_PREFIX = {"remote": "R", "github": "G", "stdio": "S", "invalid": "X"}

# Hostnames that would otherwise be read as a GitHub owner in "owner/repo" form.
_NON_REPO_FIRST_SEGMENTS = {"localhost", "127", "0", "host", "0.0.0.0", "::1", "[::1]"}


class BatchTarget(BaseModel):
    """One dashboard target: a remote endpoint URL or a GitHub repository."""
    target: str
    # Explicit kind, when the caller already knows it: the dashboard has a separate
    # input for remote endpoints and for GitHub repositories, so it states which is
    # which instead of leaving it to classify_target's heuristics. Omit it and the
    # target is classified (that is the config-import / API-client path).
    kind: Optional[str] = None          # "remote" | "github"
    name: Optional[str] = None
    connection_type: Optional[str] = None      # defaults to the group's setting
    env: Optional[Dict[str, str]] = None       # merged over the group's env
    required_env_vars: Optional[list] = None
    # This target's own credential. Never inherited from the group and never shared
    # with another row: each remote server has its own key / token / OAuth grant.
    auth: Optional[TargetAuth] = None
    authentication: Optional[Dict[str, Any]] = None
    distribution_type: Optional[str] = None
    github_repo_link: Optional[str] = None     # extra metadata for a remote target


class BatchInspectRequest(BaseModel):
    # Two input shapes. `servers` is the original config-import path (fully formed
    # InspectRequest dicts); `targets` is the dashboard path (raw URLs / repos that
    # the backend classifies and resolves). Both may be supplied at once.
    servers: Optional[list] = None
    targets: Optional[list] = None
    concurrency: int = DEFAULT_BATCH_CONCURRENCY
    connection_type: str = "auto"
    # Env values applied to every entry, overridden per-entry. These are per-run
    # only: nothing is written to disk or kept after the group finishes.
    env: Optional[Dict[str, str]] = None
    output_group: Optional[str] = None


_batch_groups: Dict[str, Dict[str, Any]] = {}


def _clamp_concurrency(requested: int) -> int:
    try:
        value = int(requested)
    except (TypeError, ValueError):
        return DEFAULT_BATCH_CONCURRENCY
    return max(MIN_BATCH_CONCURRENCY, min(MAX_BATCH_CONCURRENCY, value))


def classify_target(raw: str) -> tuple:
    """
    Classify a pasted target line.

    Returns (kind, normalized) where kind is "github", "remote" or "invalid".
    A GitHub URL is a repository to launch over stdio, not an endpoint to connect
    to, so the two must be told apart before anything else happens — connecting to
    github.com as if it were an MCP server is the failure this prevents.
    """
    text = (raw or "").strip().strip(",").strip()
    if not text:
        return "invalid", ""

    lowered = text.lower()
    if lowered.startswith(("http://", "https://")):
        from urllib.parse import urlparse
        host = (urlparse(text).netloc or "").lower().removeprefix("www.")
        if host in ("github.com", "gitlab.com", "bitbucket.org"):
            return "github", text
        return "remote", text

    # Bare forms: github.com/owner/repo, or owner/repo shorthand.
    if lowered.startswith(("github.com/", "www.github.com/")):
        return "github", f"https://{text.removeprefix('www.')}"
    first, _, _ = text.partition("/")
    # owner/repo shorthand. A dotless first segment is only a GitHub owner if it
    # cannot be a hostname: "localhost/mcp", "internal-gw:8080/mcp" and any name
    # that resolves on this network are endpoints, not repositories. Getting this
    # wrong would send an endpoint through the stdio launcher, so the shorthand is
    # deliberately narrow — a full URL is always unambiguous.
    if (
        text.count("/") == 1
        and " " not in text
        and "." not in first
        and ":" not in first
        and first.lower() not in _NON_REPO_FIRST_SEGMENTS
    ):
        return "github", f"https://github.com/{text}"

    # A bare host with a path (mcp.example.com/mcp, localhost:3000/sse) is a common
    # paste; assume https, except for a local host where http is what actually runs.
    if " " not in text and ("." in first or ":" in first or first.lower() in _NON_REPO_FIRST_SEGMENTS):
        scheme = "http" if first.lower().split(":")[0] in _NON_REPO_FIRST_SEGMENTS else "https"
        return "remote", f"{scheme}://{text}"

    return "invalid", text


def _normalize_for_kind(kind: str, raw: str, classified: str) -> str:
    """
    Re-normalize a target when the caller's declared kind disagrees with the guess.

    Only reachable from the dashboard's separate inputs, where the disagreement is
    almost always a shorthand: "owner/repo" typed in the GitHub box (guessed
    github already), or a dotless host typed in the endpoint box.
    """
    text = (raw or "").strip()
    lowered = text.lower()
    if lowered.startswith(("http://", "https://")):
        return text
    if kind == "github":
        return f"https://github.com/{text.removeprefix('github.com/')}" if "github.com/" not in lowered \
            else f"https://{text}"
    scheme = "http" if text.split("/")[0].split(":")[0].lower() in _NON_REPO_FIRST_SEGMENTS else "https"
    return f"{scheme}://{text}"


def _entry_from_target(
    target: BatchTarget,
    group_env: Optional[Dict[str, str]],
    default_connection_type: str,
) -> Dict[str, Any]:
    """Build the InspectRequest-shaped dict for a dashboard target."""
    kind, normalized = classify_target(target.target)
    declared = (target.kind or "").strip().lower()
    if declared in ("remote", "github") and kind != "invalid":
        # Trust the caller's kind, but keep the classifier's normalization (scheme
        # added, shorthand expanded). A URL that could not be parsed at all stays
        # invalid — declaring a kind cannot make an unusable string usable.
        if declared != kind:
            normalized = _normalize_for_kind(declared, target.target, normalized)
        kind = declared
    merged_env = {**(group_env or {}), **(target.env or {})} or None

    entry: Dict[str, Any] = {
        "_target_kind": kind,
        "_target": normalized or target.target,
        "name": target.name,
        "env": merged_env,
        "required_env_vars": target.required_env_vars,
        "auth": target.auth,
        "authentication": target.authentication,
        "distribution_type": target.distribution_type,
    }

    if kind == "github":
        entry["connection_type"] = "stdio"
        entry["repository"] = normalized
    elif kind == "remote":
        entry["connection_type"] = (target.connection_type or default_connection_type or "auto").lower()
        entry["endpoint_url"] = normalized
        if target.github_repo_link:
            entry["github_repo_link"] = target.github_repo_link
    return entry


async def _prepare_batch_entry(job_id: str, entry: Dict[str, Any]):
    """
    Turn one batch entry into (server_config, output_dir), or fail the job with a
    structured error. Runs inside the worker, not the request handler, because
    resolving a GitHub target costs GitHub API round trips.
    """
    kind = entry.get("_target_kind")
    raw_target = entry.get("_target")

    if kind == "invalid":
        job_manager.fail_job(
            job_id,
            f"Not a usable target: {raw_target!r}. Expected an http(s) endpoint URL or a "
            f"GitHub repository (github.com/owner/repo).",
            classify_error(message=f"invalid target {raw_target}", stage="config").model_dump(),
        )
        return None, None

    if kind == "github":
        # GitHub targets resolve to a README-derived stdio command — supply-chain
        # RCE if left ungated, so they follow the same opt-in as direct stdio.
        if not _stdio_allowed():
            job_manager.fail_job(
                job_id,
                f"GitHub/stdio targets are disabled on this server (they execute a "
                f"command parsed from the repository README). Set {_ALLOW_STDIO_ENV}=1 "
                "in the server environment and restart to enable them.",
                classify_error(message="github target blocked: stdio disabled", stage="config").model_dump(),
            )
            return None, None
        job_manager.update_phase(job_id, "resolve_target", 0.05)
        generated = await build_stdio_config_from_repo(entry.get("repository"), entry.get("name"))
        if not generated.success or not generated.config:
            job_manager.fail_job(
                job_id,
                generated.error or f"Could not resolve {raw_target} into a runnable server",
                classify_error(message=generated.error or "github resolution failed", stage="config").model_dump(),
            )
            return None, None

        config = generated.config
        entry["name"] = entry.get("name") or config.get("name")
        entry["command"] = config.get("command")
        entry["args"] = config.get("args")
        entry["description"] = config.get("description")
        # Names discovered from the repo are merged with any the caller declared, so
        # a missing token is reported as ENV_VARS_MISSING up front rather than as an
        # opaque subprocess crash minutes later.
        declared = {str(v) for v in (entry.get("required_env_vars") or [])}
        entry["required_env_vars"] = sorted(declared | {str(v) for v in (generated.env_vars or [])})

    request_kwargs = {k: v for k, v in entry.items() if not k.startswith("_")}
    try:
        entry_request = InspectRequest(**request_kwargs)
    except Exception as e:
        job_manager.fail_job(
            job_id,
            f"Invalid server entry: {e}",
            classify_error(message=f"invalid server entry: {e}", stage="config").model_dump(),
        )
        return None, None

    server_config, output_dir, error_response = await _build_server_config_from_request(entry_request)
    if error_response is not None:
        job_manager.fail_job(
            job_id,
            error_response.error or "Configuration rejected",
            error_response.error_details.model_dump() if error_response.error_details else None,
        )
        return None, None

    job_manager.rename_job(job_id, server_config["name"])
    return server_config, output_dir


async def _run_batch_entry(
    gate: asyncio.Event,
    semaphore: asyncio.Semaphore,
    job_id: str,
    entry: Dict[str, Any],
) -> None:
    """Wait for a free slot, then resolve and run one entry."""
    # The gate exists so every task is registered with the job manager before any
    # of them mutates job state — otherwise a fast worker could reach "running"
    # before mark_queued() had attached its task, and the cancel button would have
    # nothing to cancel.
    await gate.wait()
    async with semaphore:
        job = job_manager.get_job(job_id)
        if job is None or job.cancel_requested:
            return
        job_manager.mark_running(job_id)
        server_config, output_dir = await _prepare_batch_entry(job_id, entry)
        if server_config is None:
            return
        await _run_inspection_job(job_id, server_config, output_dir)


async def _run_batch_group(group_id: str, entries: list, job_ids: list, concurrency: int) -> None:
    """Run a whole group at bounded concurrency, then aggregate."""
    semaphore = asyncio.Semaphore(concurrency)
    gate = asyncio.Event()

    tasks = [
        asyncio.create_task(_run_batch_entry(gate, semaphore, job_id, entry))
        for job_id, entry in zip(job_ids, entries)
    ]
    for job_id, task in zip(job_ids, tasks):
        job_manager.mark_queued(job_id, task)
    gate.set()

    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        await _aggregate_batch_when_done(group_id)


async def _aggregate_batch_when_done(group_id: str) -> None:
    """Wait for all jobs in a batch group, then build the aggregated CSV + HTML."""
    group = _batch_groups.get(group_id)
    if not group:
        return

    jobs = [job_manager.get_job(jid) for jid in group["job_ids"]]
    tasks = [j.task for j in jobs if j and j.task]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    reports = []
    for j in jobs:
        if j and j.state == "done" and j.result and j.result.get("report_data"):
            reports.append(j.result["report_data"])

    group["state"] = "done"
    # The aggregate only ever contains jobs that reached "done"; cancelled and failed
    # entries have no report data to contribute. Report the coverage so the UI does
    # not imply the CSV covers the whole group.
    group["aggregate_covers"] = {"reports": len(reports), "total": len(group["job_ids"])}
    if not reports:
        return

    try:
        import csv as csv_mod
        import io
        from src.utility.attribute_report_generator import (
            AttributeReportGenerator,
            generate_aggregated_html,
        )

        output_dir = Path(group["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)
        server_names_ok = [r.get("server_name", "unknown") for r in reports]

        all_csv_data = []
        csv_header = None
        for report in reports:
            rows = list(csv_mod.reader(io.StringIO(AttributeReportGenerator(report).generate_csv())))
            if not rows:
                continue
            if csv_header is None:
                csv_header = rows[0]
            sname = report.get("server_name", "unknown")
            for row in rows[1:]:
                if row:
                    all_csv_data.append([sname] + row)

        if all_csv_data and csv_header:
            csv_path = output_dir / "aggregated_attribute_checklist.csv"
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                writer = csv_mod.writer(f)
                writer.writerow(["Server Name"] + csv_header)
                writer.writerows(all_csv_data)

            html_path = output_dir / "aggregated_attribute_checklist.html"
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(generate_aggregated_html(reports, server_names_ok))

            group["aggregated"] = {"csv": str(csv_path), "html": str(html_path)}

        # Capabilities CSV: one row per tool across every server, prefixed with
        # the server name — the batch counterpart of the per-server
        # mcp_capabilities.csv, in the same flat format (tools only).
        from src.utility.capability_csv_exporter import (
            _format_parameters,
            _compact_json,
        )
        from src.utility.tool_operation_classifier import classify_tool_actions

        cap_rows = []
        for report in reports:
            sname = report.get("server_name", "unknown")
            # Analyzed tools (MCPTool) drop annotations/outputSchema, so read
            # the raw discovery copy that still carries them.
            tools = (
                (report.get("metadata") or {}).get("discovery_data", {}).get("tools_discovered")
                or report.get("tools")
                or []
            )
            for item in tools:
                classification = classify_tool_actions(item)
                cap_rows.append([
                    sname,
                    item.get("name") or "(unnamed)",
                    (item.get("description") or "NA").replace("\n", " "),
                    _format_parameters(item.get("inputSchema")),
                    _compact_json(item.get("inputSchema")),
                    _compact_json(item.get("outputSchema")),
                    classification["supports_read"],
                    classification["supports_write"],
                    classification["supports_delete"],
                ])
        if cap_rows:
            caps_path = output_dir / "aggregated_capabilities.csv"
            with open(caps_path, "w", newline="", encoding="utf-8") as f:
                writer = csv_mod.writer(f)
                writer.writerow([
                    "server_name",
                    "tool_name",
                    "description",
                    "parameters",
                    "inputSchema",
                    "outputSchema",
                    "supports_read",
                    "supports_write",
                    "supports_delete",
                ])
                writer.writerows(cap_rows)
            group.setdefault("aggregated", {})["capabilities_csv"] = str(caps_path)

        # Protocol summary across servers (same shape as the CLI multi-mode's
        # all_servers_protocol.csv).
        from src.graph.inspection_graph import _write_aggregated_protocol_csv
        _write_aggregated_protocol_csv(reports, output_dir)
        protocol_path = output_dir / "all_servers_protocol.csv"
        if protocol_path.exists():
            group.setdefault("aggregated", {})["protocol_csv"] = str(protocol_path)

        # Raw JSON: the full report_data for every server that produced one.
        json_path = output_dir / "aggregated_report.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(reports, f, indent=2, default=str)
        group.setdefault("aggregated", {})["json"] = str(json_path)
    except Exception as e:
        print(f"⚠️  Batch aggregation failed: {e}")


@app.post("/api/inspect/batch")
async def inspect_batch(request: BatchInspectRequest):
    """
    Inspect many servers at bounded concurrency.

    Accepts raw dashboard `targets` (endpoint URLs and/or GitHub repositories) and
    fully formed `servers` entries from a config import, in the same group. At most
    `concurrency` jobs run at once (1–10, default 5); the rest sit in state
    "queued". Returns immediately with a job per target — including targets whose
    configuration turns out to be invalid, which fail as jobs with structured
    error_details rather than as a separate error list, so the dashboard has one
    row per target no matter what happens to it.

    The aggregated CSV/HTML is written once every job in the group finishes.
    """
    import tempfile
    import uuid as uuid_mod

    entries: list = []

    # Duplicates are dropped rather than inspected twice: two jobs for the same
    # endpoint would each contribute a row to the aggregated CSV, double-counting
    # that server in the checklist.
    duplicates: list = []
    seen_targets: set = set()

    for raw in (request.targets or []):
        try:
            target = BatchTarget(**raw) if isinstance(raw, dict) else BatchTarget(target=str(raw))
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid target entry: {e}")
        entry = _entry_from_target(target, request.env, request.connection_type)
        # The credential is part of the identity: the same endpoint submitted twice
        # with two different API keys is two distinct things to inspect, while the
        # same endpoint with the same credential is a duplicate. The token only ever
        # lives in this local set — it is never logged or returned.
        auth = entry.get("auth")
        key = (
            entry.get("_target_kind"),
            str(entry.get("_target") or "").rstrip("/").lower(),
            (auth.mode or "none").lower() if auth else "none",
            (auth.token or "") if auth else "",
            ((auth.tokens or {}).get("access_token") or "") if auth else "",
        )
        if entry.get("_target_kind") != "invalid" and key in seen_targets:
            duplicates.append(entry.get("_target"))
            continue
        seen_targets.add(key)
        entries.append(entry)

    for raw in (request.servers or []):
        if not isinstance(raw, dict):
            raise HTTPException(status_code=400, detail="servers entries must be objects")
        entry = dict(raw)
        if request.env:
            entry["env"] = {**request.env, **(entry.get("env") or {})}
        entry["_target_kind"] = "stdio" if entry.get("connection_type") == "stdio" else "remote"
        entry["_target"] = entry.get("endpoint_url") or entry.get("repository") or entry.get("name") or "unknown"
        entries.append(entry)

    if not entries:
        raise HTTPException(status_code=400, detail="Provide at least one target or server entry")
    if len(entries) > 100:
        raise HTTPException(status_code=400, detail="At most 100 targets per batch")

    concurrency = _clamp_concurrency(request.concurrency)
    group_id = request.output_group or uuid_mod.uuid4().hex
    group_dir = Path(tempfile.gettempdir()) / "mcp_inspector_api" / f"batch_{group_id}"

    job_ids = []
    jobs_started = []
    # Per-kind ordinals: remote endpoints number R1, R2… independently of GitHub
    # repos G1, G2…, so a mixed submission stays readable and a user can say
    # "R2 failed" without ambiguity even before any server name is known.
    kind_counters: Dict[str, int] = {}
    for position, entry in enumerate(entries):
        kind = entry.get("_target_kind") or "remote"
        kind_counters[kind] = kind_counters.get(kind, 0) + 1
        identity = f"{_IDENTITY_PREFIX.get(kind, 'T')}{kind_counters[kind]}"
        display = entry.get("name") or entry.get("_target") or "unknown"
        entry["_identity"] = identity
        job = job_manager.create_job(
            display,
            target=entry.get("_target"),
            target_kind=kind,
            identity=identity,
            position=position,
        )
        job_ids.append(job.job_id)
        jobs_started.append({
            "name": display,
            "job_id": job.job_id,
            "target": entry.get("_target"),
            "target_kind": kind,
            "identity": identity,
            "position": position,
        })

    _batch_groups[group_id] = {
        "job_ids": job_ids,
        "output_dir": str(group_dir),
        "state": "running",
        "concurrency": concurrency,
        "aggregated": None,
        # The original entry per job, kept so a failed or cancelled row can be
        # retried on its own without re-submitting the whole group.
        "entries": dict(zip(job_ids, entries)),
    }
    asyncio.create_task(_run_batch_group(group_id, entries, job_ids, concurrency))

    return {
        "group_id": group_id,
        "concurrency": concurrency,
        "jobs": jobs_started,
        "duplicates_removed": duplicates,
        # Kept for the config-import caller, which renders it. Now always empty:
        # rejected entries are reported as failed jobs instead.
        "errors": [],
    }


@app.get("/api/inspect/batch/{group_id}/status")
async def batch_status(group_id: str):
    """Poll a batch group: per-job statuses, counts, aggregated report paths."""
    group = _batch_groups.get(group_id)
    if not group:
        raise HTTPException(status_code=404, detail=f"Unknown batch group: {group_id}")

    job_statuses = []
    counts = {"queued": 0, "running": 0, "done": 0, "error": 0, "cancelled": 0, "pending": 0}
    for jid in group["job_ids"]:
        job = job_manager.get_job(jid)
        if job:
            job_statuses.append(job.status_payload())
            counts[job.state] = counts.get(job.state, 0) + 1

    return {
        "group_id": group_id,
        "state": group["state"],
        "concurrency": group.get("concurrency"),
        "total": len(group["job_ids"]),
        "counts": counts,
        "jobs": job_statuses,
        "aggregated": group.get("aggregated"),
        "aggregate_covers": group.get("aggregate_covers"),
    }


@app.post("/api/inspect/batch/{group_id}/cancel")
async def cancel_batch(group_id: str):
    """
    Cancel every unfinished job in a group.

    Queued jobs stop before they ever start; running jobs unwind at their next
    await, exactly like a single-job cancel.
    """
    group = _batch_groups.get(group_id)
    if not group:
        raise HTTPException(status_code=404, detail=f"Unknown batch group: {group_id}")

    cancelled = [jid for jid in group["job_ids"] if job_manager.cancel_job(jid)]
    return {
        "group_id": group_id,
        "cancelled": len(cancelled),
        "job_ids": cancelled,
        # Return the post-cancel snapshot so the caller can update the table
        # immediately instead of waiting for the next poll tick — which is what
        # made per-row cancels look like they did nothing.
        "state": group["state"],
        "jobs": [
            job_manager.get_job(jid).status_payload()
            for jid in group["job_ids"]
            if job_manager.get_job(jid)
        ],
    }


@app.post("/api/inspect/batch/{group_id}/retry/{job_id}")
async def retry_batch_job(group_id: str, job_id: str):
    """
    Re-run one failed or cancelled job in place, leaving the rest of the group
    untouched. The user fixes the cause (e.g. adds the OAuth token the row was
    missing) and retries that row instead of re-inspecting every server.

    The retried job runs immediately — its siblings are done, so there is no
    concurrency slot to wait for — and the combined report is rebuilt once it
    settles so a retry that succeeds flows into the aggregate.
    """
    group = _batch_groups.get(group_id)
    if not group:
        raise HTTPException(status_code=404, detail=f"Unknown batch group: {group_id}")

    if job_id not in group["job_ids"]:
        raise HTTPException(status_code=404, detail=f"Job {job_id} is not part of group {group_id}")

    entry = (group.get("entries") or {}).get(job_id)
    if entry is None:
        raise HTTPException(
            status_code=409,
            detail="This group predates per-row retry support — start a new run to retry.",
        )

    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Unknown job: {job_id}")
    if job.state not in ("error", "cancelled"):
        raise HTTPException(
            status_code=409,
            detail=f"Only failed or cancelled rows can be retried (this row is '{job.state}').",
        )

    async def _rerun() -> None:
        # A dedicated semaphore with one slot: the gate/semaphore shape of
        # _run_batch_entry is preserved, but the retried job starts at once.
        gate = asyncio.Event()
        semaphore = asyncio.Semaphore(1)
        task = asyncio.current_task()
        if task is None or not job_manager.reset_for_retry(job_id, task):
            return
        # Reopening a settled group re-enables the UI's "running" affordances.
        group["state"] = "running"
        gate.set()
        try:
            await _run_batch_entry(gate, semaphore, job_id, entry)
        finally:
            if all(
                (j := job_manager.get_job(jid)) is None
                or j.state in ("done", "error", "cancelled")
                for jid in group["job_ids"]
            ):
                await _aggregate_batch_when_done(group_id)

    asyncio.create_task(_rerun())

    return {
        "group_id": group_id,
        "job_id": job_id,
        "retried": True,
        "job": job_manager.get_job(job_id).status_payload() if job_manager.get_job(job_id) else None,
    }


@app.get("/api/inspect/batch/{group_id}/download/{format}")
async def download_batch_report(group_id: str, format: str):
    """
    Download the combined report for a whole group: one CSV row set (or one HTML
    page) covering every server that produced a report.

    The status payload reports `aggregated` as filesystem paths, which a browser
    cannot open — this route is what makes them reachable. Only jobs that reached
    "done" contribute, so compare against `aggregate_covers` before treating the
    file as covering the whole group.
    """
    group = _batch_groups.get(group_id)
    if not group:
        raise HTTPException(status_code=404, detail=f"Unknown batch group: {group_id}")

    aggregated = group.get("aggregated")
    if not aggregated:
        # Distinguish "not finished yet" from "finished with nothing to aggregate":
        # a group where every job failed never writes a file, and reporting that as
        # a 404 would look like the group had expired.
        if group.get("state") != "done":
            raise HTTPException(
                status_code=409,
                detail="The group is still running. The combined report is written once every job finishes.",
            )
        raise HTTPException(
            status_code=404,
            detail="No combined report: no server in this group produced a report.",
        )

    if format not in aggregated:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid format. Supported formats: {', '.join(sorted(aggregated.keys()))}",
        )

    path = Path(aggregated[format])
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Combined report file is gone: {path}")

    media_types = {
        "csv": "text/csv",
        "capabilities_csv": "text/csv",
        "html": "text/html",
        "json": "application/json",
    }
    # Extension must be the real one: "capabilities_csv" is a CSV, so the saved
    # file opens correctly instead of getting an extension the OS cannot handle.
    ext = {"capabilities_csv": "capabilities.csv", "csv": "csv", "html": "html", "json": "json"}.get(format, format)
    stem = "capabilities" if format == "capabilities_csv" else "combined"
    return FileResponse(
        path=str(path),
        filename=f"mcp_batch_{group_id[:8]}_{stem}.{ext.split('.')[-1]}",
        media_type=media_types.get(format, "application/octet-stream"),
    )


@app.get("/api/inspect/batch/{group_id}/server/{server_name}/download/{format}")
async def download_batch_server_report(group_id: str, server_name: str, format: str):
    """
    Download ONE server's report from inside a batch run — the per-server
    counterpart of the combined download above. ``format`` is the same set as
    single inspections: ``csv`` is the attribute checklist,
    ``capabilities_csv`` the tool capabilities CSV, plus json/markdown/html/txt.
    """
    group = _batch_groups.get(group_id)
    if not group:
        raise HTTPException(status_code=404, detail=f"Unknown batch group: {group_id}")

    # The server must actually be part of this group — otherwise this route
    # would be an alias for the single-report download of any past inspection.
    known_names = {
        job_manager.get_job(jid).server_name
        for jid in group["job_ids"]
        if job_manager.get_job(jid)
    }
    if server_name not in known_names:
        raise HTTPException(
            status_code=404,
            detail=f"Server {server_name!r} is not part of batch group {group_id}.",
        )

    format_map = {
        "json": "inspection_report.json",
        "markdown": "attribute_checklist.md",
        "html": "attribute_report.html",
        "csv": "attribute_checklist.csv",
        "attributes_csv": "attributes.csv",
        "capabilities_csv": "mcp_capabilities.csv",
        "txt": "attribute_extraction_raw.txt",
    }
    if format not in format_map:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid format. Supported formats: {', '.join(format_map.keys())}",
        )

    import tempfile

    report_path = (
        Path(tempfile.gettempdir()) / "mcp_inspector_api" / server_name / format_map[format]
    )
    if not report_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Report not found for {server_name!r}: the inspection may still be running or produced no {format} report.",
        )

    media_types = {
        "json": "application/json",
        "markdown": "text/markdown",
        "html": "text/html",
        "csv": "text/csv",
        "attributes_csv": "text/csv",
        "capabilities_csv": "text/csv",
        "txt": "text/plain",
    }
    stem = ("capabilities" if format == "capabilities_csv"
            else "attributes" if format == "attributes_csv"
            else "checklist" if format == "csv"
            else format)
    return FileResponse(
        path=str(report_path),
        filename=f"{server_name}_{stem}.{report_path.suffix.lstrip('.')}",
        media_type=media_types.get(format, "application/octet-stream"),
    )


# Env vars the Inspector itself reads, with what each one is for. Anything else
# found in .env / .env.template is reported too, as an untyped extra.
_KNOWN_ENV_VARS = [
    ("OPENAI_API_KEY", "LLM analysis phase (report narrative + attribute inference)", True),
    ("GITHUB_PERSONAL_ACCESS_TOKEN", "GitHub API reads: repo metadata, README, env-var detection", False),
    ("MCP_PROXY_ALLOW_PRIVATE", "Let /mcp-proxy reach loopback/private addresses (off by default)", False),
    ("CCI_TOOLKIT_PATH", "Path to the CCI Research Toolkit repo used for HTTP capability discovery", False),
    ("NOTION_TOKEN", "Credential forwarded to Notion MCP servers", False),
    ("LINEAR_API_KEY", "Credential forwarded to Linear MCP servers", False),
]


@app.get("/api/config/env")
async def get_env_config():
    """
    Read-only view of environment-variable resolution — status only, never values.

    This reports on exactly the mechanism inspections already use: utils.load_env()
    loads the project .env with override=True, and resolve_env_vars() then fills any
    unset or placeholder value from os.environ. Nothing here changes that, and
    nothing here writes to .env — per-run overrides are passed with the batch
    request and discarded when the group finishes.

    `source` reflects the real precedence: a name declared in .env wins over an
    exported shell variable, because load_env() overrides.
    """
    import os
    from dotenv import dotenv_values

    utils.load_env()

    env_path = utils.get_project_root() / ".env"
    template_path = utils.get_project_root() / ".env.template"
    dotenv_declared = dotenv_values(env_path) if env_path.exists() else {}

    names = [n for n, _, _ in _KNOWN_ENV_VARS]
    described = {n: (d, r) for n, d, r in _KNOWN_ENV_VARS}
    for extra in dotenv_declared:
        if extra not in described:
            names.append(extra)
            described[extra] = ("Declared in your .env file", False)

    variables = []
    for name in names:
        description, required = described[name]
        value = os.environ.get(name)
        declared_in_dotenv = bool((dotenv_declared.get(name) or "").strip())
        is_set = bool(value) and not _is_placeholder(value)
        variables.append({
            "name": name,
            "description": description,
            "required": required,
            "set": is_set,
            "placeholder": bool(value) and _is_placeholder(value),
            # Length only — enough to spot a truncated paste, reveals nothing usable.
            "value_length": len(value) if value else 0,
            "source": (".env file" if declared_in_dotenv else "process environment") if value else None,
        })

    missing_required = [v["name"] for v in variables if v["required"] and not v["set"]]
    return {
        "dotenv_path": str(env_path),
        "dotenv_exists": env_path.exists(),
        "template_path": str(template_path) if template_path.exists() else None,
        "variables": variables,
        "missing_required": missing_required,
        "resolution_order": [
            ".env in the project root (loaded with override=True, so it wins)",
            "the backend process environment",
            "per-request env values sent with the inspection",
        ],
        "note": "Values are never returned by this API, and per-run overrides are never written to .env.",
    }


def _make_json_serializable(obj: Any) -> Any:
    """
    Recursively convert objects to JSON-serializable format.
    Handles Pydantic models, coroutines, and other non-serializable types.
    """
    import inspect
    
    # Check if it's a coroutine (shouldn't happen if we await properly, but safety check)
    if inspect.iscoroutine(obj):
        raise ValueError("Coroutine found in data - ensure all async functions are awaited")
    
    # Handle Pydantic models
    if hasattr(obj, 'model_dump'):
        return obj.model_dump()
    elif hasattr(obj, 'dict'):
        return obj.dict()
    
    # Handle dicts
    if isinstance(obj, dict):
        return {k: _make_json_serializable(v) for k, v in obj.items()}
    
    # Handle lists
    if isinstance(obj, (list, tuple)):
        return [_make_json_serializable(item) for item in obj]
    
    # Handle basic types
    if isinstance(obj, (str, int, float, bool, type(None))):
        return obj
    
    # Try to convert to dict if it has __dict__
    if hasattr(obj, '__dict__'):
        return _make_json_serializable(obj.__dict__)
    
    # Fallback: convert to string
    return str(obj)


async def run_inspection_with_config(
    server_config: Dict[str, Any],
    output_dir: Path,
    phase_callback: Optional[Any] = None,
) -> Path:
    """
    Run inspection with a dynamically built server config.

    Invokes the same LangGraph pipeline the CLI uses (src/graph) so UI and CLI
    behavior cannot diverge. An optional phase_callback(phase, progress) is
    forwarded to the graph nodes for live job-progress reporting.
    """
    from src.graph import single_server_graph, initial_inspection_state

    # Load environment variables (including OPENAI_API_KEY from .env file)
    utils.load_env()

    print(f"DEBUG: run_inspection_with_config received server_config: {json.dumps(_redact_secrets(server_config), indent=2)}")
    if not server_config.get("repository"):
        print("⚠️  WARNING: No repository URL in server_config - documentation analysis will be limited")

    initial = initial_inspection_state(
        server_config=server_config,
        output_dir=output_dir,
    )

    run_config = {"configurable": {"phase_callback": phase_callback}} if phase_callback else None
    final = await single_server_graph.ainvoke(initial, config=run_config)

    report_path = final.get("report_path")
    if not report_path:
        errors = final.get("errors") or ["Unknown error"]
        message = "; ".join(str(e) for e in errors)

        # Carry the agent's structured failure dict (status_code, failure_kind)
        # up to the classifier. Raising a bare Exception here threw that away and
        # forced classification back onto string matching over a reworded message.
        failure_result = None
        stage = "unknown"
        for key, key_stage in (
            ("discovery_result", "mcp_discovery"),
            ("analysis_result", "llm_analysis"),
        ):
            candidate = final.get(key)
            if isinstance(candidate, dict) and candidate.get("status") != "SUCCESS":
                failure_result, stage = candidate, key_stage
                break

        raise InspectionPipelineError(message, failure_result=failure_result, stage=stage)

    return Path(report_path)


# ── Backend OAuth proxy (no browser CORS restrictions apply here) ────────────

from fastapi.responses import HTMLResponse
from api_bridge.oauth_session_manager import oauth_session_manager


class OAuthStartRequest(BaseModel):
    endpoint_url: str
    scopes: Optional[list] = None
    client_id: Optional[str] = None
    client_secret: Optional[str] = None


class OAuthStartResponse(BaseModel):
    session_id: Optional[str] = None
    authorize_url: Optional[str] = None
    expires_in: int = 600
    error: Optional[str] = None
    error_details: Optional[ErrorDetails] = None


OAUTH_CALLBACK_PATH = "/api/oauth/callback"


@app.post("/api/oauth/start", response_model=OAuthStartResponse)
async def oauth_start(request: OAuthStartRequest, req: Request = None):
    """
    Start a backend-proxied OAuth flow for an MCP server.

    Discovers the authorization server (RFC 9728 → RFC 8414), performs dynamic
    client registration (RFC 7591) when needed, and returns the authorize URL
    for the browser to open. The token exchange happens entirely server-side
    at /api/oauth/callback, so authorization servers that block browser CORS
    work normally.
    """
    endpoint_url = (request.endpoint_url or "").strip()
    if not endpoint_url:
        return OAuthStartResponse(
            error="endpoint_url is required",
            error_details=classify_error(message="endpoint url required", stage="oauth"),
        )

    # The redirect_uri must point at THIS backend instance
    base = str(req.base_url).rstrip("/") if req else "http://localhost:8000"
    redirect_uri = f"{base}{OAUTH_CALLBACK_PATH}"

    try:
        session = await oauth_session_manager.start_session(
            endpoint_url=endpoint_url,
            redirect_uri=redirect_uri,
            scopes=request.scopes,
            client_id=request.client_id,
            client_secret=request.client_secret,
        )
        return OAuthStartResponse(
            session_id=session.session_id,
            authorize_url=session.authorize_url,
        )
    except ValueError as e:
        return OAuthStartResponse(
            error=str(e),
            error_details=classify_error(message=f"oauth {e}", stage="oauth"),
        )
    except Exception as e:
        error_detail, details = _classify_exception(e)
        details.stage = "oauth"
        return OAuthStartResponse(error=error_detail, error_details=details)


@app.get(OAUTH_CALLBACK_PATH)
async def oauth_callback(code: Optional[str] = None, state: Optional[str] = None, error: Optional[str] = None):
    """
    OAuth redirect target. Validates state (CSRF), exchanges the code for
    tokens server-side, and shows a small close-this-window page.
    """
    def page(title: str, body: str, ok: bool) -> HTMLResponse:
        color = "#15803d" if ok else "#b91c1c"
        return HTMLResponse(f"""<!doctype html>
<html><head><title>{title}</title></head>
<body style="font-family: system-ui, sans-serif; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0;">
  <div style="text-align: center; max-width: 26rem;">
    <h2 style="color: {color};">{title}</h2>
    <p style="color: #374151;">{body}</p>
  </div>
  <script>setTimeout(() => window.close(), 2500)</script>
</body></html>""")

    if error:
        session = oauth_session_manager.find_by_state(state) if state else None
        if session:
            session.status = "error"
            session.error_message = f"Authorization denied: {error}"
        return page("Authorization failed", f"The authorization server returned: {error}. You can close this window.", ok=False)

    if not code or not state:
        return page("Invalid callback", "Missing code or state parameter. You can close this window.", ok=False)

    session = oauth_session_manager.find_by_state(state)
    if session is None:
        return page(
            "Session not found",
            "No pending OAuth session matches this callback (it may have expired). Start the flow again from the Inspector.",
            ok=False,
        )

    await oauth_session_manager.exchange_code(session, code)

    if session.status == "complete":
        return page("Authentication complete ✓", "You can close this window and return to the MCP Inspector.", ok=True)
    return page("Token exchange failed", session.error_message or "Unknown error. You can close this window.", ok=False)


@app.get("/api/oauth/status/{session_id}")
async def oauth_status(session_id: str):
    """Poll a backend OAuth session; returns the token once complete."""
    session = oauth_session_manager.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Unknown OAuth session: {session_id}")
    return {
        "status": session.status,
        "token": session.token if session.status == "complete" else None,
        "error_message": session.error_message,
    }


@app.get("/api/reports/{report_id}/{format}")
async def download_report(report_id: str, format: str):
    """
    Download a report in the specified format.
    
    Args:
        report_id: Server name used during inspection
        format: Report format (json, markdown, html, csv, txt)
    
    Returns:
        File download response
    """
    import tempfile
    
    # Map format to filename. The attribute checklist ("csv"), the attribute
    # key-value list ("attributes_csv") and the capability list
    # ("capabilities_csv") are three different CSVs.
    format_map = {
        "json": "inspection_report.json",
        "markdown": "attribute_checklist.md",
        "html": "attribute_report.html",
        "csv": "attribute_checklist.csv",
        "attributes_csv": "attributes.csv",
        "capabilities_csv": "mcp_capabilities.csv",
        "txt": "attribute_extraction_raw.txt",
    }
    
    if format not in format_map:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid format. Supported formats: {', '.join(format_map.keys())}"
        )
    
    # Construct path to report file
    temp_dir = Path(tempfile.gettempdir()) / "mcp_inspector_api"
    report_dir = temp_dir / report_id
    report_path = report_dir / format_map[format]
    
    if not report_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Report not found: {report_path}. The report may have expired or the server name is incorrect."
        )
    
    # Determine media type
    media_types = {
        "json": "application/json",
        "markdown": "text/markdown",
        "html": "text/html",
        "csv": "text/csv",
        "attributes_csv": "text/csv",
        "capabilities_csv": "text/csv",
        "txt": "text/plain",
    }

    # The download name carries a real file extension: naming the capability list
    # "..._report.capabilities_csv" would leave the browser and the OS with no idea
    # it is a CSV.
    suffixes = {"markdown": "md", "capabilities_csv": "csv", "attributes_csv": "csv"}
    suffix = suffixes.get(format, format)
    stem = ("capabilities" if format == "capabilities_csv"
            else "attributes" if format == "attributes_csv"
            else "report")

    return FileResponse(
        path=str(report_path),
        filename=f"{report_id}_{stem}.{suffix}",
        media_type=media_types.get(format, "application/octet-stream")
    )


@app.get("/api/reports/{report_id}")
async def list_reports(report_id: str):
    """
    List all available reports for a given inspection.
    
    Args:
        report_id: Server name used during inspection
    
    Returns:
        Dictionary of available report formats and their paths
    """
    import tempfile
    
    temp_dir = Path(tempfile.gettempdir()) / "mcp_inspector_api"
    report_dir = temp_dir / report_id
    
    if not report_dir.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Report directory not found for server: {report_id}"
        )
    
    format_map = {
        "json": "inspection_report.json",
        "markdown": "attribute_checklist.md",
        "html": "attribute_report.html",
        "csv": "attribute_checklist.csv",
        "capabilities_csv": "mcp_capabilities.csv",
        "txt": "attribute_extraction_raw.txt",
    }
    
    available_reports = {}
    for format_name, filename in format_map.items():
        report_path = report_dir / filename
        if report_path.exists():
            available_reports[format_name] = {
                "path": str(report_path),
                "download_url": f"/api/reports/{report_id}/{format_name}",
                "size": report_path.stat().st_size,
            }
    
    return {
        "server_name": report_id,
        "reports": available_reports,
        "count": len(available_reports),
    }


if __name__ == "__main__":
    import uvicorn
    # Loopback only. The API executes stdio commands and proxies to arbitrary
    # URLs; binding anything wider exposes both to the network. Reach it from
    # another host via an SSH tunnel (ssh -L 8000:localhost:8000 ...) instead.
    uvicorn.run(app, host="127.0.0.1", port=8000)

