"""
MCP Discovery Agent

Connects to an MCP server and discovers all available tools, resources, and prompts.
Returns structured discovery data for further analysis.

HTTP-based connections (sse / streamable_http / http / auto) use the direct MCP SDK
via mcp_capability_fetcher for richer capability data: full inputSchema on tools,
typed prompt arguments with required flags, and authoritative null-vs-empty capability
detection from the MCP handshake. Auth headers (OAuth, bearer, API key, basic) are
resolved by the Inspector's own MCPConnectionManager._build_auth_headers() before
being forwarded. Stdio connections use the original path unchanged.
"""

import asyncio
import time
from typing import Dict, Any

from src.models.structured_output import MCPDiscoveryOutput, MCPCapabilities
from src.utility.mcp_connection_manager import MCPConnectionManager
from src.utility.utils import Utils

utils = Utils()

# Total wall-clock budget for walking every (url, transport) candidate. The
# inspection job's own timeout is 300 s and discovery is only its first phase,
# so the walk must finish well inside that or the job dies mid-walk and reports
# a generic timeout instead of the server's actual error.
DISCOVERY_WALK_BUDGET_SECONDS = 90.0

# Per-attempt connect timeout. Each candidate also gets whatever is left of the
# overall budget, whichever is smaller.
PER_ATTEMPT_TIMEOUT_SECONDS = 30.0

# HTTP statuses that mean "this endpoint exists but does not speak this
# transport" rather than "this server is broken / needs auth":
#   400 — endpoint rejected the Streamable HTTP POST body (regdatalab.com)
#   405 — method not allowed (GET-only SSE endpoint hit with POST, or vice versa)
#   406 — Accept header not acceptable for this transport
# All are worth retrying with the other transport, and none should be reported
# as the final error when another candidate produced something more meaningful.
TRANSPORT_MISMATCH_STATUSES = {400, 405, 406}


def _is_transport_level_failure(fetcher_result: Dict[str, Any]) -> bool:
    """
    True when the failure looks like "wrong transport for this endpoint".

    Covers both the explicit HTTP statuses and the case where the server just
    drops the connection mid-handshake (absmartly, fal.ai) — which arrives as a
    stream_closed failure_kind with no status code at all.
    """
    status_code = fetcher_result.get("status_code")
    if status_code in TRANSPORT_MISMATCH_STATUSES:
        return True
    return fetcher_result.get("failure_kind") == "stream_closed"


async def _probe_versions_for(
    url: str,
    transport: str,
    headers: Dict[str, str],
) -> Dict[str, Any]:
    """
    Ask the server which protocol versions it accepts, and never let that question
    fail the inspection.

    Version probing is supplementary evidence: the capabilities are already in hand
    by the time this runs. So a probe that blows up must degrade to a structured
    "not determined" record rather than turning a successful discovery into a
    failure. Cancellation is the one exception — a cancelled job must still unwind.
    """
    from src.utility.protocol_version_prober import probe_protocol_versions, summarize_probe

    try:
        probe = await probe_protocol_versions(url, transport=transport, headers=headers or None)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        print(f"⚠️  Protocol version probe could not run: {type(e).__name__}: {e}")
        return {
            "probed_url": url,
            "probed_transport": transport,
            "supported_versions": [],
            "latest_supported": None,
            "results": [],
            "error": f"Version probe could not run: {type(e).__name__}: {e}",
            "failure_kind": "unknown",
        }

    print(f"📋 Protocol versions — {summarize_probe(probe)}")
    return probe


async def _discover_http_with_fetcher(
    server_config: Dict[str, Any],
    server_name: str,
    connection_type: str,
) -> Dict[str, Any]:
    """
    HTTP discovery using the direct MCP SDK (mcp_capability_fetcher) for richer
    capability data: full inputSchema on tools, typed prompt arguments with required
    flags, and authoritative null-vs-empty capability detection from the MCP handshake.
    Auth headers are resolved by the Inspector's MCPConnectionManager so that the
    existing OAuth flow, bearer tokens, API keys, and basic auth all work unchanged.

    Tries multiple URL/transport combinations before giving up:
      1. base URL  + streamable_http  (skipped for sse-only mode)
      2. base URL  + sse
      3. base URL  + /sse  + sse       (common remote-server pattern)
    Always returns a structured dict — never raises on connection failure.

    The whole walk is bounded by DISCOVERY_WALK_BUDGET_SECONDS so that several
    slow candidates cannot together outlast the caller's inspection timeout.
    """
    from src.utility.mcp_capability_fetcher import fetch_mcp_capabilities

    endpoint_url = server_config.get("endpoint_url") or server_config.get("url")
    if not endpoint_url:
        raise ValueError(f"endpoint_url required for {connection_type} connection: {server_name}")
    # Input provenance for the raw JSON report (GitHub repo as given in config).
    repository = server_config.get("repository")

    # Resolve auth + custom headers; default Accept/User-Agent are added inside the fetcher.
    connection_manager = MCPConnectionManager(server_config)
    headers: Dict[str, str] = {}
    custom_headers = server_config.get("custom_headers", {})
    if custom_headers:
        headers.update(custom_headers)
    auth_config = server_config.get("authentication", {})
    if auth_config:
        headers.update(connection_manager._build_auth_headers(auth_config))

    use_sse_only = (connection_type == "sse")
    base_url = endpoint_url.rstrip("/")
    sse_url = base_url + "/sse"

    # Only add the bare /sse fallback when the URL has no meaningful path (e.g.
    # https://server.com → try https://server.com/sse).  URLs that already name
    # a specific endpoint (https://server.com/mcp, https://server.com/sse, …)
    # must not have /sse appended — it creates nonsensical paths like /mcp/sse
    # that always 404 and hide the real error from the primary candidate.
    from urllib.parse import urlparse as _urlparse
    _url_path = _urlparse(base_url).path.rstrip("/")
    _is_bare_url = not _url_path or _url_path == "/"

    # Ordered list of (url, transport) candidates to attempt.
    candidates = []
    if not use_sse_only:
        candidates.append((base_url, "streamable_http"))
    candidates.append((base_url, "sse"))
    if _is_bare_url and not base_url.endswith("/sse"):
        candidates.append((sse_url, "sse"))

    fetcher_result: Dict[str, Any] = {"error": "no candidates attempted", "stage": "init", "success": False}
    resolved_transport = connection_type
    # The candidate that actually completed a handshake — the only URL worth
    # re-probing for protocol versions, since the others demonstrably do not work.
    resolved_url = base_url
    # Failures are ranked so the reported error is the most informative one:
    #   1. a real error (auth, 5xx, TLS, DNS …) from any candidate
    #   2. a transport-mismatch error (400/405/406/stream-closed)
    #   3. whatever the last candidate produced (typically a 404)
    # Without this ranking a bogus 405 from the wrong transport outranks — and
    # hides — the genuine error from the primary candidate.
    first_meaningful_result: Dict[str, Any] | None = None
    first_transport_level_result: Dict[str, Any] | None = None
    auth_error_detected = False
    attempted: list[str] = []
    budget_exhausted = False

    deadline = time.monotonic() + DISCOVERY_WALK_BUDGET_SECONDS

    for attempt_url, transport in candidates:
        remaining = deadline - time.monotonic()
        if remaining <= 1.0:
            budget_exhausted = True
            print(
                f"\n⏱️  Candidate budget of {DISCOVERY_WALK_BUDGET_SECONDS:.0f}s exhausted — "
                f"skipping {transport.upper()} at {attempt_url}"
            )
            break

        attempt_timeout = min(PER_ATTEMPT_TIMEOUT_SECONDS, remaining)
        attempted.append(f"{transport}@{attempt_url}")
        print(f"\n🔍 Trying {transport.upper()} at {attempt_url} (≤{attempt_timeout:.0f}s) …")
        fetcher_result = await fetch_mcp_capabilities(
            attempt_url,
            transport=transport,
            headers=headers or None,
            timeout=attempt_timeout,
            sse_read_timeout=attempt_timeout,
            request_read_timeout=attempt_timeout,
        )
        if not fetcher_result.get("error"):
            resolved_transport = transport
            resolved_url = attempt_url
            print(f"✅ Connected via {transport.upper()} at {attempt_url}")
            break

        err_text = fetcher_result["error"]
        status_code = fetcher_result.get("status_code")  # int from httpx, or None
        failure_kind = fetcher_result.get("failure_kind", "unknown")
        is_404 = (status_code == 404) or ("404" in err_text and "Not Found" in err_text)
        is_auth = (status_code in (401, 403)) or any(
            token in err_text for token in ("Unauthorized", "Forbidden")
        )
        is_transport_level = _is_transport_level_failure(fetcher_result)

        if is_transport_level and not is_auth:
            if first_transport_level_result is None:
                first_transport_level_result = fetcher_result
        elif first_meaningful_result is None and not is_404:
            first_meaningful_result = fetcher_result

        status_hint = f" [HTTP {status_code}]" if status_code else f" [{failure_kind}]"
        reason = " — looks like a transport mismatch" if is_transport_level else ""
        print(f"⚠️  {transport.upper()} failed{status_hint}{reason} ({err_text}); trying next candidate…")

        if is_auth:
            auth_error_detected = True
            print(f"🔐  Auth error (HTTP {status_code or 'unknown'}) detected — stopping further attempts.")
            break

    if fetcher_result.get("error"):
        # Rank the collected failures (see the comment on first_meaningful_result).
        best_result = first_meaningful_result or first_transport_level_result or fetcher_result
        error_detail = best_result["error"]
        stage = best_result.get("stage", "connect")
        best_status = best_result.get("status_code")
        best_kind = best_result.get("failure_kind", "unknown")
        attempted_summary = ", ".join(attempted) or "none"
        print(
            f"\n❌ All connection attempts failed (tried: {attempted_summary}). "
            f"Best error [{stage}] (HTTP {best_status} / {best_kind}): {error_detail}\n"
        )

        _is_auth_failure = auth_error_detected or (best_status in (401, 403)) or any(
            t in error_detail for t in ("Unauthorized", "Forbidden")
        )
        if _is_auth_failure:
            error_message = (
                f"Authentication required (HTTP {best_status}): the server rejected the request. "
                f"Configure a Bearer token or complete OAuth in the Inspector UI. "
                f"[{stage}]: {error_detail}"
            )
        elif budget_exhausted:
            error_message = (
                f"MCP capability fetch timed out after {DISCOVERY_WALK_BUDGET_SECONDS:.0f}s "
                f"(tried: {attempted_summary}) — the server accepted the connection but did not "
                f"complete the MCP handshake in time. [{stage}]: {error_detail}"
            )
        elif best_status == 410:
            # 410 Gone is the canonical "this endpoint was retired" signal —
            # distinct from a transport mismatch, because no other candidate
            # will fix a URL the server has permanently removed.
            error_message = (
                f"Endpoint gone (HTTP 410): the server has permanently removed this endpoint "
                f"(tried: {attempted_summary}). The given URL is deprecated — check the server's "
                f"documentation for the current endpoint (many servers moved /sse → /mcp when "
                f"Streamable HTTP replaced HTTP/SSE). [{stage}]: {error_detail}"
            )
        elif _is_transport_level_failure(best_result):
            error_message = (
                f"Transport mismatch (HTTP {best_status} / {best_kind}): the endpoint did not accept "
                f"either Streamable HTTP or SSE. Tried: {attempted_summary}. Check the endpoint path "
                f"(/mcp vs /sse) or select the transport explicitly. [{stage}]: {error_detail}"
            )
        else:
            error_message = (
                f"MCP capability fetch failed [{stage}] (tried: {attempted_summary}): {error_detail}"
            )

        discovery_output = MCPDiscoveryOutput(
            status="FAILURE",
            server_name=server_name,
            connection_type=connection_type,
            endpoint_url=endpoint_url,
            repository=repository,
            capabilities=MCPCapabilities(),
            tools_discovered=[],
            resources_discovered=[],
            prompts_discovered=[],
            resource_templates_discovered=[],
            error_message=error_message,
        )
        result = discovery_output.model_dump()
        result["success"] = False
        result["error"] = error_detail
        result["stage"] = stage
        # Machine-readable failure detail for the API layer's error classifier,
        # so it does not have to re-derive the cause from the message text.
        result["failure_kind"] = best_kind
        result["status_code"] = best_status
        result["attempted_candidates"] = attempted
        # Transport status on failure too — a 410 on a /sse URL is the canonical
        # "this endpoint was deprecated/removed" signal and reviewers need it
        # without digging through the error text.
        given_is_sse_path = "/sse" in endpoint_url.split("?")[0]
        result["transport_info"] = {
            "given_url": endpoint_url,
            "given_transport": connection_type,
            "resolved_url": None,
            "resolved_transport": None,
            "transport_changed": False,
            "sse_deprecated": given_is_sse_path,
            "transport_status": (
                "deprecated-endpoint-gone" if best_status == 410
                else "deprecated-sse" if given_is_sse_path
                else "unresolved"
            ),
        }
        return result

    # Build MCPCapabilities from the handshake-authoritative capabilities_supported dict.
    caps = fetcher_result["capabilities_supported"]
    mismatch = fetcher_result.get("capability_mismatch", {})
    print(f"🎯 Capabilities from handshake: {caps}")
    if mismatch.get("resources") or mismatch.get("prompts"):
        mismatched = [k for k, v in mismatch.items() if v]
        print(f"⚠️  Capability mismatch: server answers {', '.join(mismatched)} despite not declaring them in the handshake")
    capabilities = MCPCapabilities(
        tools=bool(caps.get("tools")),
        resources=bool(caps.get("resources")),
        prompts=bool(caps.get("prompts")),
        sampling=bool(caps.get("sampling")),
        logging=bool(caps.get("logging")),
        completions=bool(caps.get("completions")),
        experimental=caps.get("experimental"),
        resources_mismatch=bool(mismatch.get("resources")),
        prompts_mismatch=bool(mismatch.get("prompts")),
    )

    tools_discovered     = fetcher_result.get("tools")             or []
    resources_discovered = fetcher_result.get("resources")         or []
    prompts_discovered   = fetcher_result.get("prompts")           or []
    templates_discovered = fetcher_result.get("resource_templates") or []

    print("\n" + "="*60)
    print("✅ DISCOVERY COMPLETED SUCCESSFULLY")
    print("="*60)
    print(f"🔧 Tools discovered: {len(tools_discovered)}")
    print(f"📦 Resources discovered: {len(resources_discovered)}")
    print(f"📐 Resource templates discovered: {len(templates_discovered)}")
    print(f"📝 Prompts discovered: {len(prompts_discovered)}")
    print(f"🔁 Sampling supported: {caps.get('sampling', False)}")
    print("="*60 + "\n")

    discovery_output = MCPDiscoveryOutput(
        status="SUCCESS",
        server_name=server_name,
        connection_type=resolved_transport,
        endpoint_url=endpoint_url,
        repository=repository,
        capabilities=capabilities,
        tools_discovered=tools_discovered,
        resources_discovered=resources_discovered,
        prompts_discovered=prompts_discovered,
        resource_templates_discovered=templates_discovered,
    )

    # Which protocol versions does this server actually accept? The SDK handshake
    # above cannot answer that — it only ever offers the SDK's own latest — so ask
    # directly, once, against the endpoint/transport that just worked.
    version_probe = await _probe_versions_for(resolved_url, resolved_transport, headers)

    result = discovery_output.model_dump()
    # Recorded on success too, not just on failure: when a fallback candidate wins,
    # this is the only record that the primary transport was rejected first.
    result["attempted_candidates"] = attempted
    result["protocol_version_probe"] = version_probe

    # Transport status — did the server answer on the URL/transport it was given,
    # or did a fallback win? HTTP/SSE is the legacy transport (superseded by
    # Streamable HTTP in the 2025-03-26 spec); a server that only answers on a
    # /sse path, or whose given /sse URL only worked after switching transports,
    # is flagged deprecated so reviewers can tell it has not moved to /mcp yet.
    given_is_sse_path = "/sse" in endpoint_url.split("?")[0]
    resolved_is_sse = resolved_transport == "sse"
    result["transport_info"] = {
        "given_url": endpoint_url,
        "given_transport": connection_type,
        "resolved_url": resolved_url,
        "resolved_transport": resolved_transport,
        "transport_changed": resolved_transport != connection_type or resolved_url.rstrip("/") != endpoint_url.rstrip("/"),
        "sse_deprecated": given_is_sse_path or resolved_is_sse,
        "transport_status": (
            "deprecated-sse" if (given_is_sse_path or resolved_is_sse)
            else "current-streamable-http" if resolved_transport == "streamable_http"
            else resolved_transport
        ),
    }
    result["server_info"] = {
        "name": server_name,
        "connection_type": resolved_transport,
        "server_info": fetcher_result["server_info"],
        "server_capabilities": str(caps),
        "protocol_version": fetcher_result.get("protocol_version", ""),
        "negotiated_version": fetcher_result.get("negotiated_version", ""),
        # What this client offered, as distinct from what the server chose.
        "client_protocol_version": fetcher_result.get("client_protocol_version", ""),
        # Independent of the handshake: what the server *would* accept.
        "protocol_version_probe": version_probe,
    }
    return result


async def mcp_discovery_agent(server_config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Discovery agent that connects to an MCP server and discovers its capabilities.
    
    Args:
        server_config: Server configuration dictionary containing:
            - name: Server name
            - connection_type: Connection type (stdio, sse, websocket)
            - command: Command to run (for stdio)
            - args: Command arguments (for stdio)
            - endpoint_url: URL (for sse/websocket)
    
    Returns:
        Dictionary containing discovery results
    """
    server_name = server_config.get("name", "unknown")
    connection_type = server_config.get("connection_type", "stdio")
    # Input provenance — recorded in the result so raw JSON reports carry
    # whichever locator was given in the config (remote endpoint / GitHub repo).
    endpoint_url = server_config.get("endpoint_url") or server_config.get("url")
    repository = server_config.get("repository")
    
    print(f"\n{'='*60}")
    print(f"🔍 MCP DISCOVERY AGENT")
    print(f"{'='*60}")
    print(f"📡 Server: {server_name}")
    print(f"🔌 Connection Type: {connection_type}")
    print(f"{'='*60}\n")
    
    start_time = time.time()

    # Process-launcher types that run a local subprocess over stdio.
    # "uv", "npx", "docker", "python", "node" are all stdio transports — they
    # identify the executable, not a network protocol.
    STDIO_TYPES = {"stdio", "uv", "npx", "docker", "python", "node"}

    try:
        # HTTP-based connections use the direct MCP SDK for richer capability data.
        # Stdio-like connections use the original MCPConnectionManager path below.
        if connection_type not in STDIO_TYPES:
            return await _discover_http_with_fetcher(server_config, server_name, connection_type)

        # Initialize connection manager
        connection_manager = MCPConnectionManager(server_config)
        
        # Connect to MCP server
        async with connection_manager.connect() as session:
            print("\n🔍 Starting capability discovery...\n")
            
            # Get server info from protocol initialization
            server_info = await connection_manager.get_server_info(session)
            print(f"📋 Server Info: {server_info.get('server_info', {})}")
            if server_info.get('protocol_version'):
                print(f"📡 Protocol Version: {server_info.get('protocol_version')}")

            # Derive capabilities from the MCP handshake (authoritative source).
            # The InitializeResult.capabilities object has each capability as None
            # (unsupported) or a capability object (supported). Using this avoids
            # double list_* calls and avoids misreading "method not found" as success.
            caps_obj = server_info.get("_capabilities_obj")
            if caps_obj is not None:
                experimental_caps = getattr(caps_obj, 'experimental', None)
                capabilities_dict = {
                    "tools":       getattr(caps_obj, 'tools',       None) is not None,
                    "resources":   getattr(caps_obj, 'resources',   None) is not None,
                    "prompts":     getattr(caps_obj, 'prompts',     None) is not None,
                    "sampling":    getattr(caps_obj, 'sampling',    None) is not None,
                    "logging":     getattr(caps_obj, 'logging',     None) is not None,
                    "completions": getattr(caps_obj, 'completions', None) is not None,
                    "experimental": dict(experimental_caps) if experimental_caps else None,
                }
                print(f"🎯 Capabilities from handshake: {capabilities_dict}")
            else:
                # Fallback: probe by calling list_* (may double-call later, but keeps
                # backward compatibility with adapters that don't expose _initialize_result)
                capabilities_dict = await connection_manager.detect_capabilities(session)
                server_capabilities_str = server_info.get("server_capabilities", "")
                capabilities_dict["sampling"] = "sampling" in server_capabilities_str.lower()
                capabilities_dict["logging"] = "logging" in server_capabilities_str.lower()
                capabilities_dict["completions"] = "completions" in server_capabilities_str.lower()
                print(f"🎯 Capabilities from probing: {capabilities_dict}")

            capabilities = MCPCapabilities(**capabilities_dict)

            # Discover tools
            tools_discovered = []
            if capabilities.tools:
                print("🔧 Discovering tools...")
                tools_discovered = await connection_manager.list_tools(session)
            else:
                print("⚠️  Server does not support tools")

            # Discover resources — probe even when undeclared: many servers omit
            # capabilities from the handshake yet answer the list calls. A server
            # that responds despite not declaring gets flagged as a mismatch.
            resources_discovered = []
            resource_templates_discovered = []
            if capabilities.resources:
                print("📦 Discovering resources...")
                resources_discovered = await connection_manager.list_resources(session)
                print("📐 Discovering resource templates...")
                resource_templates_discovered = await connection_manager.list_resource_templates(session)
            else:
                print("📦 Resources not declared in handshake — probing anyway...")
                resources_discovered = await connection_manager.list_resources(session)
                if resources_discovered:
                    capabilities.resources = True
                    capabilities.resources_mismatch = True
                    print(f"⚠️  Capability mismatch: server returned {len(resources_discovered)} resources despite not declaring the capability")
                    resource_templates_discovered = await connection_manager.list_resource_templates(session)
                else:
                    print("⚠️  Server does not support resources")

            # Discover prompts — same fallback probing as resources
            prompts_discovered = []
            if capabilities.prompts:
                print("📝 Discovering prompts...")
                prompts_discovered = await connection_manager.list_prompts(session)
            else:
                print("📝 Prompts not declared in handshake — probing anyway...")
                prompts_discovered = await connection_manager.list_prompts(session)
                if prompts_discovered:
                    capabilities.prompts = True
                    capabilities.prompts_mismatch = True
                    print(f"⚠️  Capability mismatch: server returned {len(prompts_discovered)} prompts despite not declaring the capability")
                else:
                    print("⚠️  Server does not support prompts")

            # Build discovery output
            discovery_output = MCPDiscoveryOutput(
                status="SUCCESS",
                server_name=server_name,
                connection_type=connection_type,
                endpoint_url=endpoint_url,
                repository=repository,
                capabilities=capabilities,
                tools_discovered=tools_discovered,
                resources_discovered=resources_discovered,
                prompts_discovered=prompts_discovered,
                resource_templates_discovered=resource_templates_discovered,
            )

            result = discovery_output.model_dump()

            # Add server info from protocol initialization
            result["server_info"] = server_info

            print("\n" + "="*60)
            print("✅ DISCOVERY COMPLETED SUCCESSFULLY")
            print("="*60)
            print(f"🔧 Tools discovered: {len(tools_discovered)}")
            print(f"📦 Resources discovered: {len(resources_discovered)}")
            print(f"📐 Resource templates discovered: {len(resource_templates_discovered)}")
            print(f"📝 Prompts discovered: {len(prompts_discovered)}")
            print(f"🔁 Sampling supported: {capabilities_dict['sampling']}")
            print("="*60 + "\n")
            
            return result
            
    except Exception as e:
        import traceback
        error_msg = str(e)
        full_traceback = traceback.format_exc()
        print(f"\n❌ Discovery failed: {error_msg}\n")
        
        # Show more details for debugging
        if "TaskGroup" in error_msg or "unhandled" in error_msg.lower():
            print("⚠️  Detailed error information:")
            print(full_traceback)
            
            # Try to extract the actual underlying exception from TaskGroup
            # TaskGroup errors often wrap the real exception
            if hasattr(e, '__cause__') and e.__cause__:
                print(f"\n🔍 Underlying exception: {type(e.__cause__).__name__}: {e.__cause__}")
                error_msg = f"{error_msg}\n\nUnderlying exception: {type(e.__cause__).__name__}: {e.__cause__}"
            elif hasattr(e, '__context__') and e.__context__:
                print(f"\n🔍 Exception context: {type(e.__context__).__name__}: {e.__context__}")
                error_msg = f"{error_msg}\n\nException context: {type(e.__context__).__name__}: {e.__context__}"
        
        # Return failure output with full traceback
        discovery_output = MCPDiscoveryOutput(
            status="FAILURE",
            server_name=server_name,
            connection_type=connection_type,
            endpoint_url=endpoint_url,
            repository=repository,
            capabilities=MCPCapabilities(),
            tools_discovered=[],
            resources_discovered=[],
            prompts_discovered=[],
            resource_templates_discovered=[],
            error_message=f"{error_msg}\n\nFull traceback:\n{full_traceback}"
        )
        
        return discovery_output.model_dump()
        
    finally:
        end_time = time.time()
        elapsed_time = end_time - start_time
        print(f"⏱️  Total discovery time: {elapsed_time:.2f} seconds\n")


if __name__ == "__main__":
    """
    Test the discovery agent with a sample MCP server.
    """
    import asyncio
    
    # Example: Playwright MCP server
    test_server_config = {
        "name": "playwright",
        "connection_type": "stdio",
        "command": "npx",
        "args": ["@playwright/mcp@latest", "--browser=chrome"]
    }
    
    # Run discovery
    result = asyncio.run(mcp_discovery_agent(test_server_config))
    
    # Print results
    import json
    print("\n" + "="*60)
    print("DISCOVERY RESULTS")
    print("="*60)
    print(json.dumps(result, indent=2))

