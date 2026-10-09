"""
Documentation Analyzer

Analyzes server documentation (README, OpenAPI specs) to extract
authentication methods and other metadata that's not in the MCP protocol.
"""

import asyncio
import os
import re
import json
import requests
from pathlib import Path
from typing import Dict, Any, List, Optional
from urllib.parse import urlparse
from openai import OpenAI
from dotenv import load_dotenv


class DocumentationAnalyzer:
    """
    Analyzes server documentation to extract authentication methods
    and other metadata not available in the MCP protocol.
    """

    # Per-request timeout and retry cap for the documentation-analysis LLM call.
    LLM_TIMEOUT_SECONDS = 60.0
    LLM_MAX_RETRIES = 2

    def __init__(self):
        """Initialize the documentation analyzer."""
        # Load .env from project root
        project_root = Path(__file__).resolve().parent.parent.parent
        env_file = project_root / ".env"
        if env_file.exists():
            load_dotenv(env_file, override=True)
        else:
            load_dotenv(override=True)
        api_key = os.getenv("OPENAI_API_KEY")
        if api_key:
            # Bound the request explicitly: the client default is 600 s with
            # retries, which alone exceeds the whole inspection budget.
            self.client = OpenAI(
                api_key=api_key,
                timeout=self.LLM_TIMEOUT_SECONDS,
                max_retries=self.LLM_MAX_RETRIES,
            )
        else:
            self.client = None
            print("⚠️  OpenAI API key not found - LLM analysis will be skipped")
    
    async def analyze_server_documentation(
        self,
        server_config: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Analyze server documentation to extract authentication methods and protocol info.
        
        Args:
            server_config: Server configuration containing repository URL
            
        Returns:
            Dictionary with extracted metadata including authentication methods and protocol version
        """
        repo_url = server_config.get("repository", "")
        endpoint_url = server_config.get("endpoint_url", "")
        
        results = {
            "authentication_methods": [],
            "authentication_details": {},
            "protocol_version": None,
            "protocol_versions_supported": [],
            "server_version": None,
            "transport_protocol": None,
            "transport_protocols_supported": [],
            "documentation_source": None,
            "confidence": "unknown"
        }
        
        print(f"\n🔍 Analyzing server documentation...")
        
        # Try multiple sources in order of reliability
        
        # 0. Inspect endpoint URL for transport clues
        if endpoint_url:
            transport_info = await self._inspect_endpoint_transport(endpoint_url)
            if transport_info:
                results["transport_protocol"] = transport_info.get("transport")
                results["transport_protocols_supported"] = transport_info.get("supported", [])
                print(f"✅ Detected transport from endpoint: {transport_info.get('transport')}")
        
        # 1. Check for OpenAPI spec
        openapi_auth = await self._check_openapi_spec(endpoint_url)
        if openapi_auth:
            results["authentication_methods"] = openapi_auth["methods"]
            results["authentication_details"] = openapi_auth["details"]
            results["documentation_source"] = "OpenAPI Specification"
            results["confidence"] = "high"
            print(f"✅ Found OpenAPI spec with auth methods: {openapi_auth['methods']}")
            return results
        
        # 2. Check for well-known metadata endpoint
        metadata = await self._check_metadata_endpoint(endpoint_url)
        if metadata and metadata.get("authentication_methods"):
            results["authentication_methods"] = metadata["authentication_methods"]
            results["documentation_source"] = "Metadata Endpoint"
            results["confidence"] = "high"
            print(f"✅ Found metadata endpoint with auth methods: {metadata['authentication_methods']}")
            return results
        
        # 3. Check for manifest files (package.json, mcp.json, etc.)
        if repo_url and "github.com" in repo_url:
            manifest_info = await self._check_manifest_files(repo_url)
            if manifest_info:
                if manifest_info.get("protocol_version"):
                    results["protocol_version"] = manifest_info["protocol_version"]
                    results["protocol_versions_supported"] = manifest_info.get("protocol_versions_supported", [])
                if manifest_info.get("version"):
                    results["server_version"] = manifest_info["version"]
                if manifest_info.get("transport_protocol"):
                    if not results["transport_protocol"]:
                        results["transport_protocol"] = manifest_info["transport_protocol"]
                if manifest_info.get("authentication_methods"):
                    results["authentication_methods"].extend(manifest_info["authentication_methods"])
                
                # Store manifest info for distribution type detection (ownership, author, etc.)
                results["manifest_info"] = manifest_info
                
                print(f"✅ Found manifest with metadata: {manifest_info.get('source')}")
        
        # 4. Analyze GitHub README if available
        if repo_url and "github.com" in repo_url:
            readme_analysis = await self._analyze_github_readme(repo_url)
            if readme_analysis:
                results["authentication_methods"] = readme_analysis["methods"]
                results["authentication_details"] = readme_analysis["details"]
                
                # Extract protocol version from README if found
                if readme_analysis.get("protocol_version"):
                    if not results["protocol_version"]:  # Don't override package.json
                        results["protocol_version"] = readme_analysis["protocol_version"]
                    if readme_analysis.get("protocol_versions_supported"):
                        results["protocol_versions_supported"].extend(readme_analysis["protocol_versions_supported"])
                
                # Extract transport protocol from README if found
                if readme_analysis.get("transport_protocol"):
                    if not results["transport_protocol"]:  # Don't override endpoint detection
                        results["transport_protocol"] = readme_analysis["transport_protocol"]
                    if readme_analysis.get("transport_protocols_supported"):
                        results["transport_protocols_supported"].extend(readme_analysis["transport_protocols_supported"])
                
                results["documentation_source"] = "GitHub README (LLM Analysis)"
                results["confidence"] = readme_analysis["confidence"]
                print(f"✅ Analyzed README, found methods: {readme_analysis['methods']}")
                if readme_analysis.get("protocol_version"):
                    print(f"✅ Found protocol version in README: {readme_analysis['protocol_version']}")
                return results
        
        print(f"⚠️  Could not find documentation to analyze")
        return results
    
    async def _inspect_endpoint_transport(self, endpoint_url: str) -> Optional[Dict[str, Any]]:
        """
        Inspect endpoint URL and headers to detect transport protocol.
        
        Args:
            endpoint_url: Endpoint URL to inspect
            
        Returns:
            Dictionary with detected transport protocol
        """
        try:
            # Parse URL for clues
            url_lower = endpoint_url.lower()
            
            # URL-based detection
            if "/sse" in url_lower:
                return {"transport": "HTTP/SSE", "supported": ["HTTP/SSE"]}
            if "/ws" in url_lower or "websocket" in url_lower:
                return {"transport": "WebSocket", "supported": ["WebSocket"]}
            
            # Try to make a HEAD request to check response headers
            try:
                response = await asyncio.to_thread(
                    requests.head, endpoint_url, timeout=3, allow_redirects=True
                )
                headers = response.headers
                
                # Check for SSE indicators
                content_type = headers.get('Content-Type', '').lower()
                if 'text/event-stream' in content_type:
                    return {"transport": "HTTP/SSE", "supported": ["HTTP/SSE"]}
                
                # Check for WebSocket upgrade support
                upgrade = headers.get('Upgrade', '').lower()
                if 'websocket' in upgrade:
                    return {"transport": "WebSocket", "supported": ["WebSocket"]}
                
                # Check for MCP-specific headers
                if 'x-mcp-transport' in headers:
                    transport = headers['x-mcp-transport']
                    return {"transport": transport, "supported": [transport]}
                
                # If it's just HTTP with no special headers
                if response.status_code < 500:
                    return {"transport": "HTTP", "supported": ["HTTP"]}
            
            except requests.RequestException:
                # Can't reach endpoint, rely on URL patterns
                pass
            
            return None
        
        except Exception as e:
            return None
    
    async def _check_openapi_spec(self, base_url: str) -> Optional[Dict[str, Any]]:
        """
        Check for OpenAPI/Swagger specification and extract auth methods.
        
        Args:
            base_url: Base URL of the API
            
        Returns:
            Dictionary with authentication methods and details
        """
        if not base_url:
            return None
        
        # Common OpenAPI spec locations
        spec_paths = [
            "/openapi.json",
            "/swagger.json",
            "/api/openapi.json",
            "/api/swagger.json",
            "/docs/openapi.json",
            "/.well-known/openapi.json"
        ]
        
        parsed = urlparse(base_url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        
        for path in spec_paths:
            try:
                response = await asyncio.to_thread(requests.get, f"{base}{path}", timeout=5)
                if response.status_code == 200:
                    spec = response.json()
                    return self._parse_openapi_security(spec)
            # Must be `except Exception`, not a bare `except`: CancelledError is a
            # BaseException, and swallowing it here would keep probing every
            # remaining path after the job has been cancelled.
            except Exception:
                continue
        
        return None
    
    def _parse_openapi_security(self, spec: Dict[str, Any]) -> Dict[str, Any]:
        """Parse OpenAPI spec to extract security schemes."""
        methods = []
        details = {}
        
        # OpenAPI 3.x
        components = spec.get("components", {})
        security_schemes = components.get("securitySchemes", {})
        
        # OpenAPI 2.x (Swagger)
        if not security_schemes:
            security_schemes = spec.get("securityDefinitions", {})
        
        for scheme_name, scheme_info in security_schemes.items():
            scheme_type = scheme_info.get("type", "").lower()
            
            if scheme_type == "http":
                scheme = scheme_info.get("scheme", "").lower()
                if scheme == "bearer":
                    methods.append("Bearer Token")
                elif scheme == "basic":
                    methods.append("Basic Auth")
            
            elif scheme_type == "oauth2":
                flows = scheme_info.get("flows", {})
                if "authorizationCode" in flows:
                    methods.append("OAuth 2.0 Authorization Code")
                if "clientCredentials" in flows:
                    methods.append("OAuth 2.0 Client Credentials")
                if "implicit" in flows:
                    methods.append("OAuth 2.0 Implicit")
            
            elif scheme_type == "apikey":
                location = scheme_info.get("in", "")
                methods.append(f"API Key ({location})")
            
            details[scheme_name] = scheme_info
        
        return {
            "methods": methods,
            "details": details
        }
    
    async def _check_manifest_files(self, repo_url: str) -> Optional[Dict[str, Any]]:
        """
        Check for various manifest files in the repository.
        Tries multiple manifest formats in order of MCP-specificity.
        
        Args:
            repo_url: GitHub repository URL
            
        Returns:
            Dictionary with extracted metadata from manifest files
        """
        # List of manifest files to check (in priority order)
        manifest_files = [
            "mcp.json",           # MCP-specific manifest (proposed standard)
            "mcp.yaml",           # MCP YAML format
            "mcp-manifest.json",  # Alternative MCP manifest
            ".mcp/config.json",   # MCP config directory
            "package.json",       # npm package manifest
            "pyproject.toml",     # Python project manifest
            "go.mod",             # Go module manifest
            "Cargo.toml",         # Rust cargo manifest
        ]
        
        for manifest_file in manifest_files:
            result = await self._fetch_manifest_from_github(repo_url, manifest_file)
            if result:
                result["source"] = manifest_file
                return result
        
        return None
    
    async def _fetch_manifest_from_github(
        self, 
        repo_url: str, 
        manifest_file: str
    ) -> Optional[Dict[str, Any]]:
        """
        Fetch a specific manifest file from GitHub and extract metadata.
        
        Args:
            repo_url: GitHub repository URL
            manifest_file: Name of the manifest file to fetch
            
        Returns:
            Dictionary with extracted metadata
        """
        try:
            # Parse GitHub URL
            match = re.search(r'github\.com/([^/]+/[^/]+)', repo_url)
            if not match:
                return None
            
            repo_path = match.group(1).rstrip('/')
            
            # Fetch file via GitHub API
            api_url = f"https://api.github.com/repos/{repo_path}/contents/{manifest_file}"
            headers = {"Accept": "application/vnd.github.v3.raw"}
            
            # Add GitHub token if available
            github_token = os.getenv("GITHUB_PERSONAL_ACCESS_TOKEN")
            if github_token:
                headers["Authorization"] = f"token {github_token}"
            
            # Blocking call — offloaded so the event loop stays free and the
            # await acts as a cancellation point for the enclosing job.
            response = await asyncio.to_thread(requests.get, api_url, headers=headers, timeout=10)
            if response.status_code != 200:
                return None
            
            # Parse based on file type
            if manifest_file.endswith('.json'):
                return self._parse_json_manifest(response.text, manifest_file)
            elif manifest_file.endswith('.toml'):
                return self._parse_toml_manifest(response.text, manifest_file)
            elif manifest_file.endswith('.yaml') or manifest_file.endswith('.yml'):
                return self._parse_yaml_manifest(response.text, manifest_file)
            elif manifest_file == 'go.mod':
                return self._parse_go_mod(response.text)
            
            return None
        
        except Exception as e:
            # Silently fail - not all repos have all manifest types
            return None
    
    def _parse_json_manifest(self, content: str, filename: str) -> Optional[Dict[str, Any]]:
        """Parse JSON manifest files."""
        try:
            data = json.loads(content)
            result = {}
            
            # MCP-specific manifest (mcp.json or mcp-manifest.json)
            if filename.startswith('mcp'):
                result["protocol_version"] = data.get("mcpVersion") or data.get("protocolVersion")
                result["protocol_versions_supported"] = data.get("supportedVersions", [])
                result["transport_protocol"] = data.get("transport")
                result["authentication_methods"] = data.get("authentication", {}).get("methods", [])
                result["version"] = data.get("version")
            
            # package.json
            elif filename == 'package.json':
                result["version"] = data.get("version")
                
                # Extract ownership information for distribution type detection
                result["package_name"] = data.get("name", "")
                
                # Extract author (can be string or object)
                author = data.get("author", "")
                if isinstance(author, dict):
                    result["author"] = author.get("name", "")
                    result["author_email"] = author.get("email", "")
                    result["author_url"] = author.get("url", "")
                elif isinstance(author, str):
                    result["author"] = author
                
                # Extract publisher (npm-specific)
                result["publisher"] = data.get("publisher", "")
                
                # Extract repository (can be string or object)
                repo = data.get("repository", "")
                if isinstance(repo, dict):
                    result["repository_url"] = repo.get("url", "")
                    result["repository_type"] = repo.get("type", "")
                elif isinstance(repo, str):
                    result["repository_url"] = repo
                
                # Look for MCP-specific fields
                if "mcpVersion" in data:
                    result["protocol_version"] = data["mcpVersion"]
                elif "mcp" in data and isinstance(data["mcp"], dict):
                    result["protocol_version"] = data["mcp"].get("version")
                    result["protocol_versions_supported"] = data["mcp"].get("versions", [])
                    result["transport_protocol"] = data["mcp"].get("transport")
                
                # Check dependencies for MCP SDK version
                deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
                for dep_name in ["@modelcontextprotocol/sdk", "mcp", "@mcp/sdk"]:
                    if dep_name in deps:
                        result["mcp_sdk_version"] = deps[dep_name]
            
            return result if result else None
        
        except Exception as e:
            return None
    
    def _parse_toml_manifest(self, content: str, filename: str) -> Optional[Dict[str, Any]]:
        """Parse TOML manifest files (pyproject.toml, Cargo.toml)."""
        try:
            # Simple TOML parsing (for MCP-specific fields)
            result = {}
            
            # Look for [tool.mcp] or [mcp] sections
            if '[tool.mcp]' in content or '[mcp]' in content:
                # Extract version
                version_match = re.search(r'version\s*=\s*["\']([^"\']+)["\']', content)
                if version_match:
                    result["protocol_version"] = version_match.group(1)
                
                # Extract transport
                transport_match = re.search(r'transport\s*=\s*["\']([^"\']+)["\']', content)
                if transport_match:
                    result["transport_protocol"] = transport_match.group(1)
            
            # Extract project version
            if filename == 'pyproject.toml':
                project_version_match = re.search(r'\[project\].*?version\s*=\s*["\']([^"\']+)["\']', content, re.DOTALL)
                if project_version_match:
                    result["version"] = project_version_match.group(1)
            
            return result if result else None
        
        except Exception as e:
            return None
    
    def _parse_yaml_manifest(self, content: str, filename: str) -> Optional[Dict[str, Any]]:
        """Parse YAML manifest files."""
        try:
            # Simple YAML parsing for MCP fields
            result = {}
            
            # Look for mcp configuration
            if 'mcpVersion:' in content or 'mcp:' in content:
                version_match = re.search(r'(?:mcpVersion|protocolVersion):\s*["\']?([^"\'\n]+)["\']?', content)
                if version_match:
                    result["protocol_version"] = version_match.group(1).strip()
                
                transport_match = re.search(r'transport:\s*["\']?([^"\'\n]+)["\']?', content)
                if transport_match:
                    result["transport_protocol"] = transport_match.group(1).strip()
            
            return result if result else None
        
        except Exception as e:
            return None
    
    def _parse_go_mod(self, content: str) -> Optional[Dict[str, Any]]:
        """Parse go.mod for MCP SDK version."""
        try:
            result = {}
            
            # Look for MCP SDK dependency
            mcp_match = re.search(r'github\.com/modelcontextprotocol/go-sdk\s+v([0-9.]+)', content)
            if mcp_match:
                result["mcp_sdk_version"] = mcp_match.group(1)
            
            return result if result else None
        
        except Exception as e:
            return None
    
    async def _check_package_json(self, repo_url: str) -> Optional[Dict[str, Any]]:
        """
        Fetch package.json from GitHub repository to extract version info.
        
        Args:
            repo_url: GitHub repository URL
            
        Returns:
            Dictionary with protocol version and server version if found
        """
        try:
            # Parse GitHub URL
            match = re.search(r'github\.com/([^/]+/[^/]+)', repo_url)
            if not match:
                return None
            
            repo_path = match.group(1).rstrip('/')
            
            # Fetch package.json via GitHub API
            api_url = f"https://api.github.com/repos/{repo_path}/contents/package.json"
            headers = {"Accept": "application/vnd.github.v3.raw"}
            
            # Add GitHub token if available
            github_token = os.getenv("GITHUB_PERSONAL_ACCESS_TOKEN")
            if github_token:
                headers["Authorization"] = f"token {github_token}"
            
            # Blocking call — offloaded so the event loop stays free and the
            # await acts as a cancellation point for the enclosing job.
            response = await asyncio.to_thread(requests.get, api_url, headers=headers, timeout=10)
            if response.status_code != 200:
                return None
            
            package_data = json.loads(response.text)
            
            result = {}
            
            # Extract version
            if "version" in package_data:
                result["version"] = package_data["version"]
            
            # Look for MCP protocol version in various fields
            if "mcpVersion" in package_data:
                result["protocol_version"] = package_data["mcpVersion"]
            elif "mcp" in package_data and isinstance(package_data["mcp"], dict):
                if "version" in package_data["mcp"]:
                    result["protocol_version"] = package_data["mcp"]["version"]
                if "versions" in package_data["mcp"]:
                    result["protocol_versions_supported"] = package_data["mcp"]["versions"]
            
            # Check dependencies for MCP SDK version
            deps = package_data.get("dependencies", {})
            dev_deps = package_data.get("devDependencies", {})
            
            for dep_name in ["@modelcontextprotocol/sdk", "mcp", "@mcp/sdk"]:
                if dep_name in deps or dep_name in dev_deps:
                    version = deps.get(dep_name) or dev_deps.get(dep_name)
                    if version and not result.get("protocol_version"):
                        # Extract version number
                        version_match = re.search(r'(\d+\.\d+\.\d+)', version)
                        if version_match:
                            result["mcp_sdk_version"] = version_match.group(1)
            
            return result if result else None
        
        except Exception as e:
            # Silently fail - not all repos have package.json
            return None
    
    async def _check_metadata_endpoint(self, base_url: str) -> Optional[Dict[str, Any]]:
        """
        Check for a metadata endpoint that might describe authentication.
        
        Args:
            base_url: Base URL of the API
            
        Returns:
            Metadata dictionary if found
        """
        if not base_url:
            return None
        
        # Proposed standard endpoints
        metadata_paths = [
            "/.well-known/mcp-metadata",
            "/.well-known/mcp-info",
            "/mcp/metadata",
            "/api/info",
            "/info"
        ]
        
        parsed = urlparse(base_url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        
        for path in metadata_paths:
            try:
                response = await asyncio.to_thread(requests.get, f"{base}{path}", timeout=5)
                if response.status_code == 200:
                    return response.json()
            # `except Exception`, not bare — see _check_openapi_spec above.
            except Exception:
                continue
        
        return None
    
    async def _analyze_github_readme(self, repo_url: str) -> Optional[Dict[str, Any]]:
        """
        Fetch and analyze GitHub README to extract authentication methods.
        
        Args:
            repo_url: GitHub repository URL
            
        Returns:
            Dictionary with authentication methods and confidence
        """
        try:
            # Parse GitHub URL
            # https://github.com/owner/repo -> owner/repo
            match = re.search(r'github\.com/([^/]+/[^/]+)', repo_url)
            if not match:
                return None
            
            repo_path = match.group(1).rstrip('/')
            
            # Fetch README via GitHub API
            api_url = f"https://api.github.com/repos/{repo_path}/readme"
            headers = {"Accept": "application/vnd.github.v3.raw"}
            
            # Add GitHub token if available (for rate limiting)
            github_token = os.getenv("GITHUB_PERSONAL_ACCESS_TOKEN")
            if github_token:
                headers["Authorization"] = f"token {github_token}"
            
            # Blocking call — offloaded so the event loop stays free and the
            # await acts as a cancellation point for the enclosing job.
            response = await asyncio.to_thread(requests.get, api_url, headers=headers, timeout=10)
            if response.status_code != 200:
                return None
            
            readme_content = response.text
            
            # Use LLM to analyze README
            return await self._llm_analyze_documentation(readme_content, repo_url)
        
        except Exception as e:
            print(f"⚠️  Error analyzing GitHub README: {e}")
            return None
    
    async def _llm_analyze_documentation(
        self,
        documentation: str,
        source: str
    ) -> Optional[Dict[str, Any]]:
        """
        Use LLM to analyze documentation and extract authentication methods.
        
        Args:
            documentation: Documentation text (README, etc.)
            source: Source of documentation (for context)
            
        Returns:
            Extracted authentication information
        """
        # Skip if no OpenAI client
        if not self.client:
            print("⚠️  Skipping LLM analysis (no API key)")
            return None
        
        try:
            # Truncate if too long (keep first 8000 chars)
            if len(documentation) > 8000:
                documentation = documentation[:8000] + "\n\n[... truncated ...]"
            
            prompt = f"""Analyze the following MCP server documentation and extract authentication, protocol version, transport protocol, and TLS/SSL security information.

Documentation source: {source}

Documentation:
{documentation}

Please identify:
1. All authentication methods mentioned (e.g., Bearer Token, OAuth 2.0, API Key, Personal Access Token)
2. Any specific details about each method
3. MCP Protocol version (look for: "MCP Protocol", "protocol version", "2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25", "2026-07-28", etc.)
4. List of all protocol versions supported if mentioned
5. Transport protocol (look for: "stdio", "SSE", "Server-Sent Events", "WebSocket", "HTTP", etc.)
6. List of all transport protocols supported if mentioned
7. TLS/SSL Security Requirements:
   - Minimum TLS version required (e.g., "TLS 1.2", "TLS 1.3")
   - Supported TLS versions (e.g., ["TLS 1.3", "TLS 1.2"])
   - Any security policies mentioned (certificate validation, mTLS, certificate pinning, HTTPS required, etc.)
   - Encryption requirements
8. Your confidence level in the extraction (high/medium/low)

Respond in JSON format:
{{
  "methods": ["method1", "method2", ...],
  "details": {{
    "method1": "description or details",
    "method2": "description or details"
  }},
  "protocol_version": "2024-11-05" or "2025-03-26" or "2025-06-18" or "2025-11-25" or "2026-07-28" or null if not found,
  "protocol_versions_supported": ["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25", "2026-07-28"] or [] if not found,
  "transport_protocol": "HTTP/SSE" or "STDIO" or "WebSocket" or null if not found,
  "transport_protocols_supported": ["HTTP/SSE", "STDIO"] or [] if not found,
  "tls_info": {{
    "minimum_version": "TLS 1.2" or "TLS 1.3" or null if not mentioned,
    "supported_versions": ["TLS 1.3", "TLS 1.2"] or [] if not found,
    "requirements": "description of security policies" or null if not mentioned,
    "https_required": true/false if explicitly mentioned, null otherwise
  }},
  "confidence": "high|medium|low",
  "reasoning": "brief explanation of findings including protocol, transport, and TLS detection"
}}

If no authentication methods are mentioned, return an empty methods array.
If no protocol version is mentioned, return null for protocol_version and [] for protocol_versions_supported.
If no transport protocol is mentioned, return null for transport_protocol and [] for transport_protocols_supported.
If no TLS information is mentioned, return null for all tls_info fields.
"""
            
            # The OpenAI client here is the synchronous one, so this call must be
            # offloaded — inline it would block the event loop for as long as the
            # request takes (see LLM_TIMEOUT_SECONDS on the client below).
            response = await asyncio.to_thread(
                self.client.chat.completions.create,
                model="gpt-4o-mini",  # Fast and cost-effective
                messages=[
                    {
                        "role": "system",
                        "content": "You are an expert at analyzing API documentation and extracting authentication requirements. Be thorough and accurate."
                    },
                    {
                        "role": "user",
                        "content": prompt
                    }
                ],
                temperature=0.1,  # Low temperature for consistency
                response_format={"type": "json_object"}
            )
            
            result = json.loads(response.choices[0].message.content)
            
            print(f"🤖 LLM Analysis: {result.get('reasoning', 'No reasoning provided')}")
            
            return result
        
        except Exception as e:
            print(f"⚠️  LLM analysis failed: {e}")
            return None
    
    def merge_with_config_auth(
        self,
        detected_methods: List[str],
        config_auth: Dict[str, Any]
    ) -> List[str]:
        """
        Merge detected authentication methods with configured method.
        
        Ensures the configured method is included even if not detected.
        
        Args:
            detected_methods: Methods detected from documentation
            config_auth: Authentication configuration
            
        Returns:
            Combined list of authentication methods
        """
        auth_type = config_auth.get("type", "").lower()
        
        # Map config auth types to display names
        type_mapping = {
            "bearer_token": "Bearer Token",
            "personal_access_token": "Personal Access Token",
            "api_key": "API Key",
            "api_token": "API Token",
            "oauth2_1_client_credentials": "OAuth 2.1 Client Credentials",
            "oauth2_1_authorization_code": "OAuth 2.1 Authorization Code",
            "oauth2_0_client_credentials": "OAuth 2.0 Client Credentials",
            "oauth2_0_authorization_code": "OAuth 2.0 Authorization Code"
        }
        
        configured_method = type_mapping.get(auth_type, auth_type.replace("_", " ").title())
        
        # Combine and deduplicate
        all_methods = set(detected_methods)
        if configured_method:
            all_methods.add(f"{configured_method} (configured)")
        
        return sorted(list(all_methods))


# Example usage
if __name__ == "__main__":
    import asyncio
    
    async def test_analyzer():
        analyzer = DocumentationAnalyzer()
        
        # Test with GitHub MCP server
        config = {
            "repository": "https://github.com/github/github-mcp-server",
            "endpoint_url": "https://api.github.com"
        }
        
        results = await analyzer.analyze_server_documentation(config)
        print(f"\n📊 Results: {json.dumps(results, indent=2)}")
    
    asyncio.run(test_analyzer())

