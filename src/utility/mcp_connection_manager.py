"""
MCP Connection Manager

Handles connections to MCP servers via different transport protocols
(stdio, SSE, WebSocket) and provides a unified interface for interacting
with MCP servers.
"""

import asyncio
from typing import Dict, Any, List, Optional
from contextlib import asynccontextmanager

from langchain_mcp_adapters.client import MultiServerMCPClient
from src.utility.oauth_manager import OAuthManager


# MCP list operations paginate via cursor/nextCursor — a single call silently
# truncates a server with more than one page. Shared by every list_* below.
_MAX_LIST_PAGES = 100


async def _list_all_pages(session: Any, method_name: str, attr: str) -> List[Any]:
    """
    Call session.<method_name>() following nextCursor until exhausted.

    Stops early (with a warning) on a repeating cursor or at the page cap —
    both are buggy-server protection, and the partial result is kept either
    way. Falls back to a no-cursor call for SDKs that predate pagination.
    """
    items: List[Any] = []
    cursor: Optional[str] = None
    seen_cursors: set = set()
    pages = 0
    while True:
        try:
            response = await getattr(session, method_name)(cursor=cursor)
        except TypeError:
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
            print(f"⚠️  [INCOMPLETE — {len(items)} items] {method_name} repeated a cursor; stopping")
            break
        seen_cursors.add(cursor)
        if pages >= _MAX_LIST_PAGES:
            print(f"⚠️  [INCOMPLETE — {len(items)} items, page cap] {method_name} still paginating after {pages} pages")
            break
    return items


class MCPConnectionManager:
    """
    Manages connections to MCP servers and provides methods for
    discovering and interacting with server capabilities.
    """

    def __init__(self, server_config: Dict[str, Any]):
        """
        Initialize connection manager with server configuration.
        
        Args:
            server_config: Configuration dictionary containing:
                - name: Server name
                - connection_type: "stdio", "sse", or "websocket"
                - command: Command to run (for stdio)
                - args: Command arguments (for stdio)
                - endpoint_url: URL (for sse/websocket)
        """
        self.server_config = server_config
        self.server_name = server_config.get("name", "unknown")
        self.connection_type = server_config.get("connection_type", "stdio")
        self.client: Optional[MultiServerMCPClient] = None
        self.session = None

    # Connection types that launch a local subprocess over stdio.
    # When connection_type is one of these (and not "stdio" itself), the type
    # name doubles as the executable command (e.g. "uv" → run `uv …`).
    _STDIO_LAUNCHER_TYPES = {"uv", "npx", "docker", "python", "node"}

    def _build_stdio_config(self) -> Dict[str, Any]:
        """
        Build configuration for stdio-based MCP connection.

        For launcher types (uv, npx, docker, …) the connection_type is used as
        the command when no explicit "command" key is present in the config.

        Returns:
            Configuration dictionary for MultiServerMCPClient
        """
        # Fall back to the connection_type name for launcher types (e.g. "uv").
        command = (
            self.server_config.get("command")
            or self.server_config.get("type")
            or (self.connection_type if self.connection_type in self._STDIO_LAUNCHER_TYPES else None)
        )
        args = self.server_config.get("args", [])

        if not command:
            raise ValueError(f"Command required for stdio connection: {self.server_name}")

        server_entry: Dict[str, Any] = {
            "transport": "stdio",
            "command": command,
            "args": args,
        }

        env = self.server_config.get("env", {})
        if env:
            server_entry["env"] = env

        cwd = self.server_config.get("cwd")
        if cwd:
            server_entry["cwd"] = cwd

        return {self.server_name: server_entry}

    def _build_sse_config(self) -> Dict[str, Any]:
        """
        Build configuration for SSE-based MCP connection.
        
        Returns:
            Configuration dictionary for MultiServerMCPClient
        """
        endpoint_url = self.server_config.get("endpoint_url")
        
        if not endpoint_url:
            raise ValueError(f"Endpoint URL required for SSE connection: {self.server_name}")
        
        config = {
            self.server_name: {
                "transport": "sse",
                "url": endpoint_url
            }
        }
        
        # Initialize headers with required Accept headers for SSE
        headers = {
            "Accept": "application/json, text/event-stream"
        }
        
        # Add authentication headers if configured
        auth_config = self.server_config.get("authentication", {})
        if auth_config:
            auth_headers = self._build_auth_headers(auth_config)
            if auth_headers:
                headers.update(auth_headers)
        
        # Add headers to config
        if headers:
            config[self.server_name]["headers"] = headers
        
        return config
    
    def _build_auth_headers(self, auth_config: Dict[str, Any]) -> Dict[str, str]:
        """
        Build authentication headers from auth configuration.
        Supports static tokens and dynamic OAuth flows.
        
        Args:
            auth_config: Authentication configuration dictionary
        
        Returns:
            Dictionary of HTTP headers for authentication
        """
        headers = {}
        auth_type = auth_config.get("type", "").lower()
        
        # Check if this is an OAuth flow
        if "oauth" in auth_type:
            # Use OAuth manager to get token dynamically
            try:
                oauth_manager = OAuthManager(auth_config)
                token = oauth_manager.get_access_token()
                headers["Authorization"] = f"Bearer {token}"
                return headers
            except Exception as e:
                print(f"⚠️  OAuth token generation failed: {e}")
                print(f"   Falling back to static token if available...")
        
        # Static token handling — check both "token" and "access_token" keys
        # (frontend sends API keys under "access_token", legacy configs use "token")
        token = auth_config.get("token") or auth_config.get("access_token", "")
        
        if not token:
            return headers
        
        # Bearer token (most common)
        if "bearer" in auth_type:
            headers["Authorization"] = f"Bearer {token}"
        
        # API key/token
        elif "api" in auth_type:
            header_name = auth_config.get("header", "X-API-Key")
            headers[header_name] = token
        
        # Personal Access Token (GitHub-style)
        elif "personal_access_token" in auth_type or "pat" in auth_type:
            headers["Authorization"] = f"token {token}"
        
        # Custom header
        elif auth_config.get("header"):
            headers[auth_config.get("header")] = token
        
        # Default: Bearer token
        elif token:
            headers["Authorization"] = f"Bearer {token}"
        
        return headers

    def _build_streamable_http_config(self) -> Dict[str, Any]:
        """
        Build configuration for Streamable HTTP MCP connection (/mcp endpoints).

        Returns:
            Configuration dictionary for MultiServerMCPClient
        """
        endpoint_url = self.server_config.get("endpoint_url")
        if not endpoint_url:
            raise ValueError(f"Endpoint URL required for streamable_http: {self.server_name}")

        config = {
            self.server_name: {
                "transport": "streamable_http",
                "url": endpoint_url,
            }
        }

        # MCP Streamable HTTP spec requires both content types in Accept.
        # Omitting text/event-stream causes a 406 from servers that stream SSE responses.
        headers = {"Accept": "application/json, text/event-stream"}
        auth_config = self.server_config.get("authentication", {})
        if auth_config:
            headers.update(self._build_auth_headers(auth_config))
        config[self.server_name]["headers"] = headers

        return config

    def get_client_config(self) -> Dict[str, Any]:
        """
        Get appropriate client configuration based on connection type.

        Returns:
            Configuration dictionary for MultiServerMCPClient
        """
        if self.connection_type in ("stdio", *self._STDIO_LAUNCHER_TYPES):
            return self._build_stdio_config()
        elif self.connection_type == "sse":
            return self._build_sse_config()
        # "auto" is normally resolved to "http" or "sse" upstream before it gets
        # here; accepting it defensively means an unnormalized value degrades to
        # a Streamable HTTP attempt instead of raising ValueError.
        elif self.connection_type in ("streamable_http", "http", "auto"):
            return self._build_streamable_http_config()
        else:
            raise ValueError(f"Unsupported connection type: {self.connection_type}")

    @asynccontextmanager
    async def connect(self):
        """
        Async context manager for connecting to MCP server.
        
        Yields:
            Active MCP session
            
        Example:
            async with manager.connect() as session:
                tools = await manager.list_tools(session)
        """
        config = self.get_client_config()
        self.client = MultiServerMCPClient(config)
        
        try:
            async with self.client.session(self.server_name) as session:
                self.session = session
                print(f"✅ Connected to MCP server: {self.server_name}")
                yield session
        finally:
            self.session = None
            self.client = None
            print(f"🔌 Disconnected from MCP server: {self.server_name}")

    async def list_tools(self, session) -> List[Dict[str, Any]]:
        """
        List all available tools from the MCP server.
        
        Args:
            session: Active MCP session
            
        Returns:
            List of tool dictionaries
        """
        try:
            # Paginated: follow nextCursor until the server stops sending one.
            tools = await _list_all_pages(session, "list_tools", "tools")
            
            # Convert to dict format
            tools_list = []
            for tool in tools:
                tool_dict = {
                    "name": tool.name,
                    "description": getattr(tool, 'description', None),
                }

                # Extract input schema if available
                if hasattr(tool, 'inputSchema'):
                    tool_dict["inputSchema"] = tool.inputSchema

                # Output schema, title and annotations feed the R/W/D
                # classification and the capabilities CSV — keep them.
                if getattr(tool, 'outputSchema', None):
                    tool_dict["outputSchema"] = tool.outputSchema
                if getattr(tool, 'title', None):
                    tool_dict["title"] = tool.title
                annotations = getattr(tool, 'annotations', None)
                if annotations is not None:
                    tool_dict["annotations"] = (
                        annotations.model_dump(exclude_none=True)
                        if hasattr(annotations, "model_dump")
                        else annotations
                    )

                tools_list.append(tool_dict)
            
            print(f"🔧 Discovered {len(tools_list)} tools")
            return tools_list
            
        except Exception as e:
            print(f"❌ Error listing tools: {e}")
            return []

    async def list_resources(self, session) -> List[Dict[str, Any]]:
        """
        List all available resources from the MCP server.
        
        Args:
            session: Active MCP session
            
        Returns:
            List of resource dictionaries
        """
        try:
            resources = await _list_all_pages(session, "list_resources", "resources")
            
            # Convert to dict format
            resources_list = []
            for resource in resources:
                # Convert AnyUrl objects to strings for JSON serialization
                uri = str(resource.uri) if hasattr(resource.uri, '__str__') else resource.uri
                annotations = getattr(resource, 'annotations', None)
                resource_dict = {
                    "uri": uri,
                    "name": resource.name,
                    "title": getattr(resource, 'title', None),
                    "description": getattr(resource, 'description', None),
                    "mimeType": getattr(resource, 'mimeType', None),
                    "size": getattr(resource, 'size', None),
                    "annotations": annotations.model_dump(exclude_none=True) if hasattr(annotations, 'model_dump') else annotations,
                }
                resources_list.append(resource_dict)
            
            print(f"📦 Discovered {len(resources_list)} resources")
            return resources_list
            
        except Exception as e:
            error_msg = str(e).lower()
            # Check if it's a "method not found" error (expected for servers without resources)
            if "method not found" in error_msg or "not found" in error_msg:
                print(f"⚠️  Resources not supported by this server (expected)")
            else:
                print(f"❌ Error listing resources: {e}")
            return []

    async def list_prompts(self, session) -> List[Dict[str, Any]]:
        """
        List all available prompts from the MCP server.

        Args:
            session: Active MCP session

        Returns:
            List of prompt dictionaries
        """
        try:
            prompts = await _list_all_pages(session, "list_prompts", "prompts")

            # Convert to dict format — normalize arguments to plain dicts so the
            # full detail (name, description, required) survives serialization.
            prompts_list = []
            for prompt in prompts:
                raw_args = getattr(prompt, 'arguments', None) or []
                arguments = [
                    {
                        "name": a.name,
                        "description": getattr(a, 'description', None),
                        "required": getattr(a, 'required', False),
                    }
                    for a in raw_args
                ]
                prompt_dict = {
                    "name": prompt.name,
                    "title": getattr(prompt, 'title', None),
                    "description": getattr(prompt, 'description', None),
                    "arguments": arguments or None,
                }
                prompts_list.append(prompt_dict)

            print(f"📝 Discovered {len(prompts_list)} prompts")
            return prompts_list

        except Exception as e:
            error_msg = str(e).lower()
            # Check if it's a "method not found" error (expected for servers without prompts)
            if "method not found" in error_msg or "not found" in error_msg:
                print(f"⚠️  Prompts not supported by this server (expected)")
            else:
                print(f"❌ Error listing prompts: {e}")
            return []

    async def list_resource_templates(self, session) -> List[Dict[str, Any]]:
        """
        List all available resource templates from the MCP server.

        Args:
            session: Active MCP session

        Returns:
            List of resource template dictionaries
        """
        try:
            templates = await _list_all_pages(session, "list_resource_templates", "resourceTemplates")

            result = []
            for t in templates:
                result.append({
                    "uriTemplate": str(t.uriTemplate),
                    "name": t.name,
                    "description": getattr(t, 'description', None),
                    "mimeType": getattr(t, 'mimeType', None),
                })

            print(f"📐 Discovered {len(result)} resource templates")
            return result

        except Exception as e:
            error_msg = str(e).lower()
            if "method not found" in error_msg or "not found" in error_msg:
                print("⚠️  Resource templates not supported by this server (expected)")
            else:
                print(f"❌ Error listing resource templates: {e}")
            return []

    async def call_tool(self, session, tool_name: str, arguments: Dict[str, Any]) -> Any:
        """
        Call a specific tool with provided arguments.
        
        Args:
            session: Active MCP session
            tool_name: Name of the tool to call
            arguments: Tool arguments
            
        Returns:
            Tool response
        """
        try:
            response = await session.call_tool(tool_name, arguments)
            print(f"✅ Tool '{tool_name}' executed successfully")
            return response
        except Exception as e:
            print(f"❌ Error calling tool '{tool_name}': {e}")
            raise

    async def _probe_list(self, session, method_name: str, attr: str) -> Optional[bool]:
        """
        Probe one list_* capability without swallowing the outcome.

        Returns True if the server answered the method (regardless of whether the
        list is empty), False if it reported the method unsupported, and None if
        the probe itself failed (transport error, timeout) so the result is
        unknown rather than falsely affirmative.

        Unlike list_tools()/list_resources()/list_prompts() — which catch every
        exception and return [] — this distinguishes the failure modes. Using the
        public list_* wrappers here was the original bug: they turned
        "method not found" into [] and made every capability look supported.
        """
        try:
            await _list_all_pages(session, method_name, attr)
            return True
        except Exception as e:
            msg = str(e).lower()
            if "method not found" in msg or "not found" in msg or "unknown method" in msg:
                return False
            # Transport/timeout/other errors: the probe didn't resolve the question.
            return None

    async def detect_capabilities(self, session) -> Dict[str, bool]:
        """
        Detect server capabilities by attempting to list various entity types.

        Args:
            session: Active MCP session

        Returns:
            Dictionary of capability flags. Flags are only set True when the server
            actually answered the corresponding list method — a failed probe leaves
            the flag False (and is reported as unknown in the log), never True.
        """
        capabilities = {
            "tools": False,
            "resources": False,
            "prompts": False,
            "sampling": False
        }

        for flag, method_name, attr in (
            ("tools", "list_tools", "tools"),
            ("resources", "list_resources", "resources"),
            ("prompts", "list_prompts", "prompts"),
        ):
            probe = await self._probe_list(session, method_name, attr)
            if probe is True:
                capabilities[flag] = True
            elif probe is None:
                print(f"⚠️  {flag.capitalize()} capability unknown (probe failed)")

        print(f"🎯 Detected capabilities: {capabilities}")
        return capabilities

    async def get_server_info(self, session) -> Dict[str, Any]:
        """
        Get server information from MCP protocol initialization.

        Args:
            session: Active MCP session

        Returns:
            Server information dictionary including protocol version and capabilities
        """
        server_info = {
            "name": self.server_name,
            "connection_type": self.connection_type,
        }

        try:
            # The MCP SDK stores the InitializeResult in session._initialize_result
            # after langchain_mcp_adapters calls initialize() during connect().
            # Reading it directly is reliable — re-calling initialize() always fails
            # with "already initialized" and returns nothing useful.
            init_result = getattr(session, '_initialize_result', None)

            if init_result is None:
                # Fallback for older adapter versions that may not set _initialize_result
                try:
                    init_result = await session.initialize()
                except Exception as e:
                    if "already" not in str(e).lower():
                        print(f"   Note: Could not initialize: {e}")

            if init_result:
                server_info["protocol_version"] = getattr(init_result, 'protocolVersion', '')
                # Same distinction the HTTP path reports: what the server chose vs
                # what this client offered (the installed SDK's latest).
                server_info["negotiated_version"] = server_info["protocol_version"]
                try:
                    from mcp.types import LATEST_PROTOCOL_VERSION
                    server_info["client_protocol_version"] = LATEST_PROTOCOL_VERSION
                except ImportError:
                    pass
                if hasattr(init_result, 'serverInfo') and init_result.serverInfo:
                    server_info["server_info"] = {
                        "name": init_result.serverInfo.name,
                        "version": getattr(init_result.serverInfo, 'version', None)
                    }
                if hasattr(init_result, 'capabilities'):
                    server_info["server_capabilities"] = str(init_result.capabilities)
                    server_info["_capabilities_obj"] = init_result.capabilities

        except Exception as e:
            print(f"⚠️  Could not extract full server info: {e}")

        return server_info

