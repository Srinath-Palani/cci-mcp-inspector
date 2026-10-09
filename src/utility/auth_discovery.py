"""
Authentication Discovery Module

Automatically discovers authentication requirements from MCP server metadata
and provides helpful guidance to users.
"""

import asyncio
import requests
import json
import re
from typing import Dict, Any, Optional, List
from urllib.parse import urlparse


class AuthenticationDiscovery:

    def __init__(self) -> None:
        self._discovered_config: Optional[Dict[str, Any]] = None

    @property
    def discovered_config(self) -> Optional[Dict[str, Any]]:
        """Last discovered auth config, or None if discovery hasn't produced one.

        Initialized in __init__ so validate_config_against_discovered never hits
        AttributeError regardless of which discovery path ran (the GitHub-README
        path returns before the metadata-endpoint path assigns it)."""
        return self._discovered_config

    @discovered_config.setter
    def discovered_config(self, value: Optional[Dict[str, Any]]) -> None:
        self._discovered_config = value

    async def discover_auth_requirements(self, repository_url: Optional[str] = None, endpoint_url: Optional[str] = None, current_config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Discover authentication requirements from GitHub README and/or endpoint metadata.
        Returns a result dictionary with discovered info and recommendations.

        The helpers below use blocking `requests`, so each is dispatched through
        asyncio.to_thread. That keeps the FastAPI event loop free (health checks
        and job-status polls stay responsive while this runs) and gives the caller
        real cancellation points — a cancelled job stops before the next request
        instead of running the whole sequence to completion.
        """
        result = {
            "discovered": False,
            "auth_config": None,
            "source": None,
            "recommendations": [],
            "metadata_available": False,
            "auth_required": False,
            "test_response": None
        }

        print(f"\n🔍 Discovering authentication requirements...")

        # Try GitHub repository discovery first (works for both stdio and remote)
        if repository_url:
            print(f"   Repository: {repository_url}")
            github_result = await asyncio.to_thread(self._discover_auth_from_github, repository_url)
            if github_result:
                # Merge github result into main result
                if github_result.get("discovered"):
                    result.update(github_result)
                    print(f"✅ Found authentication info from GitHub README")
                    self._print_discovered_config(github_result.get("auth_config", {}))
                    # Record it so validate_config_against_discovered works for
                    # README-sourced auth, not just metadata-endpoint-sourced auth.
                    self.discovered_config = github_result.get("auth_config")
                    # Generate recommendations
                    result["recommendations"] = self._generate_recommendations(
                        github_result.get("auth_config", {}),
                        current_config,
                        github_result.get("auth_required", False)
                    )
                    return result
                elif github_result.get("recommendations"):
                    # Include recommendations even if not discovered
                    result["recommendations"].extend(github_result.get("recommendations", []))

        # If no endpoint URL and no GitHub discovery, return early
        if not endpoint_url:
            if not repository_url:
                result["recommendations"].append("No endpoint URL or repository provided - cannot discover auth requirements")
            elif not result["recommendations"]:
                result["recommendations"].append("⚠️  No authentication info found in GitHub repository")
                result["recommendations"].append("💡 Check the repository README or documentation manually")
            return result

        print(f"   Endpoint: {endpoint_url}")

        # STEP 1: Try to connect without authentication to see if auth is required
        auth_test = await asyncio.to_thread(self._test_endpoint_auth_requirement, endpoint_url)
        if auth_test:
            result["auth_required"] = auth_test.get("auth_required", False)
            result["auth_type"] = auth_test.get("auth_type")
            result["test_response"] = auth_test
            if auth_test.get("auth_required"):
                print(f"✅ Authentication is REQUIRED (detected from connection attempt)")
                print(f"   Response: {auth_test.get('status_code')} - {auth_test.get('error_type')}")
                if auth_test.get("auth_type"):
                    print(f"   Detected auth type: {auth_test.get('auth_type')}")
            else:
                print(f"ℹ️  Endpoint answered the MCP handshake without authentication")

        # STEP 2: when the handshake was challenged, check whether the server also
        # publishes RFC 9728 protected-resource metadata — that is what separates
        # "OAuth sign-in is possible" from "paste a bearer token / API key".
        if result["auth_required"]:
            parsed_ep = urlparse(endpoint_url)
            origin = f"{parsed_ep.scheme}://{parsed_ep.netloc}"
            path = parsed_ep.path.rstrip("/")
            prm_candidates = []
            if path:
                prm_candidates.append(f"{origin}/.well-known/oauth-protected-resource{path}")
            prm_candidates.append(f"{origin}/.well-known/oauth-protected-resource")
            for prm_path in prm_candidates:
                prm = await asyncio.to_thread(self._fetch_metadata, origin, prm_path.replace(origin, ""))
                if prm and prm.get("authorization_servers"):
                    result["metadata_available"] = True
                    result["oauth_metadata_url"] = prm_path
                    if not result["auth_type"] or result["auth_type"] == "bearer_token":
                        result["auth_type"] = "oauth2"
                    print(f"✅ OAuth authorization-server metadata found at: {prm_path}")
                    break

        if result["auth_required"] and not result["metadata_available"] and not result["auth_type"]:
            # Challenged but no metadata and no WWW-Authenticate hint — static
            # credential is the only remaining option.
            result["auth_type"] = "bearer_token"
        else:
            print(f"⚠️  Could not test endpoint connectivity")

        # Try multiple metadata endpoints
        metadata_endpoints = [
            "/.well-known/mcp-metadata",
            "/.well-known/mcp-info",
            "/mcp/metadata",
            "/api/mcp-info",
            "/.well-known/mcp.json"
        ]

        parsed = urlparse(endpoint_url)
        base_url = f"{parsed.scheme}://{parsed.netloc}"

        for path in metadata_endpoints:
            metadata = await asyncio.to_thread(self._fetch_metadata, base_url, path)
            if metadata:
                result["metadata_available"] = True
                auth_config = self._extract_auth_config(metadata)
                if auth_config:
                    result["discovered"] = True
                    result["auth_config"] = auth_config
                    result["source"] = f"{base_url}{path}"
                    self.discovered_config = auth_config
                    print(f"✅ Found authentication metadata at: {path}")
                    self._print_discovered_config(auth_config)
                    # Generate recommendations
                    result["recommendations"] = self._generate_recommendations(
                        auth_config,
                        current_config,
                        result["auth_required"]
                    )
                    return result

        # If nothing found, provide generic recommendations
        if current_config and current_config.get("type"):
            result["recommendations"].append("✅ Using configured authentication")
        else:
            result["recommendations"].extend([
                "⚠️  No authentication metadata found",
                "💡 Check server documentation for auth requirements",
                "💡 Try adding authentication to your config manually"
            ])

        return result
    def _test_endpoint_auth_requirement(self, endpoint_url: str) -> Optional[Dict[str, Any]]:
        """
        Test if endpoint requires authentication by attempting a REAL unauthenticated
        MCP initialize handshake (not a bare GET — OAuth-gated MCP servers answer a
        bare GET with 200/405 and only challenge the actual protocol handshake).

        Auth type is classified from the 401 response:
          - WWW-Authenticate with resource_metadata / authorization_uri → oauth2
          - WWW-Authenticate: Bearer (no OAuth metadata)                → bearer_token
          - Non-standard header names (X-API-Key style) / 403           → api_key
        """
        initialize_body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "mcp-inspector-authcheck", "version": "1.0.0"},
            },
        }
        post_headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        try:
            # Primary probe: the MCP initialize handshake, unauthenticated.
            response = requests.post(
                endpoint_url, timeout=8, allow_redirects=False,
                json=initialize_body, headers=post_headers,
            )

            result = {
                "status_code": response.status_code,
                "auth_required": False,
                "auth_type": None,
                "error_type": None,
                "auth_header": None
            }

            www_auth = response.headers.get("WWW-Authenticate", "")

            if response.status_code == 401:
                result["auth_required"] = True
                result["error_type"] = "Unauthorized (401)"
                result["auth_header"] = www_auth or None
                if "resource_metadata" in www_auth or "authorization_uri" in www_auth:
                    result["auth_type"] = "oauth2"
                elif www_auth.lower().startswith("bearer"):
                    result["auth_type"] = "bearer_token"

            elif response.status_code == 403:
                result["auth_required"] = True
                result["error_type"] = "Forbidden (403)"
                # 403 on an unauthenticated handshake usually means a static
                # credential (API key / token) is expected, not an OAuth flow.
                result["auth_type"] = "api_key"

            elif response.status_code in [407, 511]:
                result["auth_required"] = True
                result["error_type"] = f"Proxy/Network Auth Required ({response.status_code})"

            # A successful handshake (200, possibly SSE-framed) proves no auth.
            # Any other 2xx/4xx with no auth challenge is treated as open.
            elif 200 <= response.status_code < 300:
                result["auth_required"] = False

            # Check response body for common auth error messages
            if not result["auth_required"]:
                try:
                    body = None
                    if response.headers.get("Content-Type", "").startswith("application/json"):
                        body = response.json()
                    elif response.text and response.headers.get("Content-Type", "").startswith("text/event-stream"):
                        # SSE-framed JSON-RPC: scan the raw payload for error fields
                        body = response.text
                    if body is not None:
                        body_str = (json.dumps(body) if not isinstance(body, str) else body).lower()
                        error_messages = [
                            "authentication", "unauthorized", "token",
                            "api key", "apikey", "api_key", "access denied", "credentials"
                        ]
                        if any(msg in body_str for msg in error_messages):
                            result["auth_required"] = True
                            if not result["error_type"]:
                                result["error_type"] = "Authentication error in response body"
                            result["response_body"] = body if not isinstance(body, str) else body[:500]
                except Exception:
                    pass

            # Fallback: some servers 405 the initialize POST (SSE-only endpoints).
            # Retry as a GET on the SSE stream — an SSE server challenges with 401
            # on the stream open, which a bare GET to the endpoint does reveal.
            if not result["auth_required"] and response.status_code == 405:
                try:
                    get_response = requests.get(
                        endpoint_url, timeout=5, allow_redirects=False,
                        headers={"Accept": "text/event-stream"},
                    )
                    if get_response.status_code in [401, 403]:
                        result["auth_required"] = True
                        result["status_code"] = get_response.status_code
                        result["error_type"] = f"{'Unauthorized' if get_response.status_code == 401 else 'Forbidden'} (SSE GET)"
                        get_auth = get_response.headers.get("WWW-Authenticate", "")
                        result["auth_header"] = get_auth or None
                        if "resource_metadata" in get_auth or "authorization_uri" in get_auth:
                            result["auth_type"] = "oauth2"
                        elif get_auth.lower().startswith("bearer"):
                            result["auth_type"] = "bearer_token"
                        elif get_response.status_code == 403:
                            result["auth_type"] = "api_key"
                except Exception:
                    pass

            return result

        except requests.exceptions.SSLError as e:
            return {
                "status_code": None,
                "auth_required": False,
                "error_type": "SSL Error",
                "details": str(e)
            }
        except requests.exceptions.ConnectionError as e:
            return {
                "status_code": None,
                "auth_required": False,
                "error_type": "Connection Error",
                "details": str(e)
            }
        except requests.exceptions.Timeout:
            return {
                "status_code": None,
                "auth_required": False,
                "error_type": "Timeout",
                "details": "Request timed out"
            }
        except Exception as e:
            return {
                "status_code": None,
                "auth_required": False,
                "error_type": "Unknown Error",
                "details": str(e)
            }
    
    def _fetch_metadata(self, base_url: str, path: str) -> Optional[Dict[str, Any]]:
        """Fetch metadata from a specific endpoint."""
        try:
            url = f"{base_url}{path}"
            response = requests.get(url, timeout=5)
            
            if response.status_code == 200:
                return response.json()
        except Exception:
            pass
        
        return None
    
    def _discover_auth_from_github(self, repository_url: str, readme_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Discover authentication requirements from GitHub repository README, supporting subdirectory README extraction.
        Args:
            repository_url: GitHub repository URL
            readme_path: Optional path to README (e.g., 'subdir/README.md')
        Returns:
            Dictionary with discovery results or None
        """
        try:
            # Extract owner, repo, and subdirectory from GitHub URL
            match = re.match(r'https?://github\.com/([^/]+)/([^/]+)(/tree/[^/]+/(.+))?', repository_url)
            if not match:
                return None
            owner, repo, _, subdir = match.groups()
            repo = repo.rstrip('/')
            headers = {"Accept": "application/vnd.github.v3.raw"}
            readme_content = None
            # If no explicit readme_path, try to infer from subdirectory in URL
            if not readme_path and subdir:
                # Try common README filenames in subdirectory
                for candidate in ["README.md", "README", "readme.md", "readme"]:
                    candidate_path = f"{subdir}/{candidate}".replace('//', '/')
                    api_url = f"https://api.github.com/repos/{owner}/{repo}/contents/{candidate_path}"
                    print(f"\n📝 [DEBUG] Fetching GitHub README from: {api_url}")
                    response = requests.get(api_url, headers=headers, timeout=10)
                    if response.status_code == 200:
                        # Try JSON first
                        try:
                            file_data = response.json()
                            if "content" in file_data:
                                import base64
                                readme_content = base64.b64decode(file_data["content"]).decode('utf-8')
                                print(f"\n📝 [DEBUG] Using GitHub README: https://github.com/{owner}/{repo}/blob/HEAD/{candidate_path}")
                                readme_path = candidate_path
                                break
                            else:
                                print(f"\n📝 [DEBUG] No 'content' in file_data for: {api_url}")
                                print(f"   file_data: {file_data}")
                        except Exception:
                            # If not JSON, try as raw text
                            if response.headers.get('Content-Type', '').startswith('text/plain') or not response.headers.get('Content-Type'):
                                readme_content = response.text
                                print(f"\n📝 [DEBUG] Using GitHub README (raw text): https://github.com/{owner}/{repo}/blob/HEAD/{candidate_path}")
                                readme_path = candidate_path
                                break
                            if response.text.strip():
                                readme_content = response.text
                                print(f"\n📝 [DEBUG] Using GitHub README (raw text fallback): https://github.com/{owner}/{repo}/blob/HEAD/{candidate_path}")
                                readme_path = candidate_path
                                break
                    else:
                        print(f"\n📝 [DEBUG] Failed to fetch GitHub README: {api_url}")
                        print(f"   Status code: {response.status_code}")
            # If explicit readme_path is provided, try that first
            if not readme_content and readme_path:
                api_url = f"https://api.github.com/repos/{owner}/{repo}/contents/{readme_path}"
                response = requests.get(api_url, headers=headers, timeout=10)
                if response.status_code == 200:
                    try:
                        file_data = response.json()
                        if "content" in file_data:
                            import base64
                            readme_content = base64.b64decode(file_data["content"]).decode('utf-8')
                            print(f"\n📝 [DEBUG] Using GitHub README: https://github.com/{owner}/{repo}/blob/HEAD/{readme_path}")
                        else:
                            print(f"\n📝 [DEBUG] No 'content' in file_data for: {api_url}")
                            print(f"   file_data: {file_data}")
                    except Exception:
                        if response.headers.get('Content-Type', '').startswith('text/plain') or not response.headers.get('Content-Type'):
                            readme_content = response.text
                            print(f"\n📝 [DEBUG] Using GitHub README (raw text): https://github.com/{owner}/{repo}/blob/HEAD/{readme_path}")
                        elif response.text.strip():
                            readme_content = response.text
                            print(f"\n📝 [DEBUG] Using GitHub README (raw text fallback): https://github.com/{owner}/{repo}/blob/HEAD/{readme_path}")
                else:
                    print(f"\n📝 [DEBUG] Failed to fetch GitHub README: {api_url}")
                    print(f"   Status code: {response.status_code}")
            # Fallback to root README if not found
            if not readme_content:
                api_url = f"https://api.github.com/repos/{owner}/{repo}/readme"
                response = requests.get(api_url, headers=headers, timeout=10)
                if response.status_code == 200:
                    # Try JSON first
                    try:
                        file_data = response.json()
                        if "content" in file_data:
                            import base64
                            readme_content = base64.b64decode(file_data["content"]).decode('utf-8')
                            print(f"\n📝 [DEBUG] Using GitHub README: https://github.com/{owner}/{repo}/blob/HEAD/README.md (root)")
                            readme_path = None
                        else:
                            print(f"\n📝 [DEBUG] No 'content' in file_data for: {api_url}")
                            print(f"   file_data: {file_data}")
                    except Exception:
                        if response.headers.get('Content-Type', '').startswith('text/plain') or not response.headers.get('Content-Type'):
                            readme_content = response.text
                            print(f"\n📝 [DEBUG] Using GitHub README (raw text): https://github.com/{owner}/{repo}/blob/HEAD/README.md (root)")
                            readme_path = None
                        elif response.text.strip():
                            readme_content = response.text
                            print(f"\n📝 [DEBUG] Using GitHub README (raw text fallback): https://github.com/{owner}/{repo}/blob/HEAD/README.md (root)")
                            readme_path = None
                else:
                    print(f"\n📝 [DEBUG] Failed to fetch GitHub README: {api_url}")
                    print(f"   Status code: {response.status_code}")
            if not readme_content:
                return None
            
            # Patterns to detect authentication requirements
            # Check for specific OAuth flows first, then generic OAuth
            auth_patterns = {
                "oauth2_authorization_code": [
                    r'authorization\s+code\s+flow',
                    r'oauth.*authorization.*code',
                    r'auth.*code.*flow',
                    r'authorization_code',
                    r'grant.*type.*authorization',
                ],
                "oauth2_client_credentials": [
                    r'client\s+credentials\s+flow',
                    r'oauth.*client.*credentials',
                    r'client_credentials',
                    r'grant.*type.*client',
                    r'service.*account.*oauth',
                ],
                "oauth2": [
                    r'oauth\s*2\.?[01]?',
                    r'oauth\s+token',
                    r'oauth\s+flow',
                    r'requires\s+oauth',
                    r'using\s+oauth',
                    r'via\s+oauth',
                ],
                "bearer_token": [
                    r'bearer\s+token',
                    r'authorization:\s*bearer',
                ],
                "api_key": [
                    r'api\s+key',
                    r'api[_-]?key',
                    r'set\s+your\s+api\s+key',
                ],
                "personal_access_token": [
                    r'personal\s+access\s+token',
                    r'pat\b',
                    r'github\s+token',
                ],
            }
            
            # Environment variable patterns that indicate auth requirement
            env_var_patterns = [
                r'([A-Z_]+(?:API_KEY|TOKEN|SECRET|CLIENT_ID|CLIENT_SECRET|AUTH)[A-Z_]*)',
                r'export\s+([A-Z_]+)=',
            ]
            
            detected_auth_types = []
            detected_env_vars = []
            auth_required = False
            matched_patterns = {}  # Track which patterns matched
            
            # Check for authentication types (can have multiple)
            for auth_type, patterns in auth_patterns.items():
                for pattern in patterns:
                    match = re.search(pattern, readme_content)
                    if match:
                        if auth_type not in detected_auth_types:
                            detected_auth_types.append(auth_type)
                        auth_required = True
                        # Store matched pattern and context
                        if auth_type not in matched_patterns:
                            matched_patterns[auth_type] = []
                        # Get surrounding context (50 chars before and after)
                        start = max(0, match.start() - 50)
                        end = min(len(readme_content), match.end() + 50)
                        context = readme_content[start:end].strip()
                        matched_patterns[auth_type].append({
                            'pattern': pattern,
                            'matched_text': match.group(0),
                            'context': context
                        })
                        break
            
            # Find environment variables that suggest auth
            for pattern in env_var_patterns:
                matches = re.findall(pattern, readme_content)
                detected_env_vars.extend(matches)
            
            # Remove duplicates and filter relevant ones
            detected_env_vars = list(set([
                var for var in detected_env_vars 
                if any(keyword in var.lower() for keyword in ['key', 'token', 'secret', 'auth', 'client'])
            ]))
            
            # Check for common authentication indicators even without specific type
            if not detected_auth_types and detected_env_vars:
                # Try to infer type from env var names
                env_vars_lower = ' '.join(detected_env_vars).lower()
                if 'client_id' in env_vars_lower and 'client_secret' in env_vars_lower:
                    detected_auth_types.append("oauth2_client_credentials")
                    auth_required = True
                elif 'client_id' in env_vars_lower or 'client_secret' in env_vars_lower:
                    detected_auth_types.append("oauth2")
                    auth_required = True
                elif 'api_key' in env_vars_lower or 'apikey' in env_vars_lower:
                    detected_auth_types.append("api_key")
                    auth_required = True
                elif 'token' in env_vars_lower:
                    detected_auth_types.append("bearer_token")
                    auth_required = True
            
            if not auth_required and not detected_env_vars:
                # No clear authentication found
                return {
                    "discovered": False,
                    "auth_required": False,
                    "source": f"GitHub: {repository_url}{('/' + readme_path) if readme_path else ''}",
                    "recommendations": ["ℹ️  No authentication requirements detected in README"]
                }
            
            # Determine primary auth type (prefer specific OAuth flows over generic)
            primary_auth_type = None
            if "oauth2_authorization_code" in detected_auth_types:
                primary_auth_type = "oauth2_authorization_code"
            elif "oauth2_client_credentials" in detected_auth_types:
                primary_auth_type = "oauth2_client_credentials"
            elif "oauth2" in detected_auth_types:
                primary_auth_type = "oauth2"
            elif detected_auth_types:
                primary_auth_type = detected_auth_types[0]
            # Build auth config
            auth_config = {
                "type": primary_auth_type or "unknown",
                "all_types": detected_auth_types,  # Include all detected types
                "env_vars": detected_env_vars[:5],  # Limit to 5 most relevant
                "matched_patterns": matched_patterns,  # Include pattern matches for debugging
            }
            # Print debug information about what was detected
            print(f"\n📝 DEBUG: Authentication Detection Details from README:")
            print(f"   Repository: {repository_url}{('/' + readme_path) if readme_path else ''}")
            print(f"   Detected auth types: {detected_auth_types}")
            for auth_type, matches in matched_patterns.items():
                print(f"\n   🔍 {auth_type.upper()}:")
                for match_info in matches[:2]:  # Show first 2 matches per type
                    print(f"      Pattern: {match_info['pattern']}")
                    print(f"      Matched: '{match_info['matched_text']}'")
                    print(f"      Context: ...{match_info['context']}...")
            if detected_env_vars:
                print(f"\n   🔑 Environment variables found: {', '.join(detected_env_vars[:5])}")
            print()
            result = {
                "discovered": True,
                "auth_required": auth_required,
                "auth_config": auth_config,
                "source": f"GitHub: {repository_url}{('/' + readme_path) if readme_path else ''}",
                "metadata_available": True,
            }
            return result
            
        except Exception as e:
            print(f"   ⚠️  Failed to fetch GitHub README: {e}")
            return None
    
    def _extract_auth_config(self, metadata: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Extract authentication configuration from metadata."""
        # Check various possible locations
        auth_config = (
            metadata.get("authentication") or
            metadata.get("auth") or
            metadata.get("security") or
            metadata.get("oauth")
        )
        
        if auth_config:
            return auth_config
        
        # Check nested paths
        if "config" in metadata and "authentication" in metadata["config"]:
            return metadata["config"]["authentication"]
        
        return None
    
    def _print_discovered_config(self, auth_config: Dict[str, Any]):
        """Print discovered authentication configuration."""
        auth_type = auth_config.get("type", "unknown")
        
        print(f"\n📋 Discovered Authentication Configuration:")
        print(f"   Type: {auth_type}")
        
        if "token_url" in auth_config:
            print(f"   Token URL: {auth_config['token_url']}")
        
        if "authorize_url" in auth_config:
            print(f"   Authorize URL: {auth_config['authorize_url']}")
        
        if "client_id" in auth_config:
            client_id = auth_config['client_id']
            if client_id and len(client_id) > 20:
                client_id = client_id[:20] + "..."
            print(f"   Client ID: {client_id or 'Not provided'}")
        
        if "scopes" in auth_config:
            scopes = auth_config.get("scopes", [])
            if isinstance(scopes, list):
                print(f"   Scopes: {', '.join(scopes)}")
            else:
                print(f"   Scopes: {scopes}")
        
        if "audience" in auth_config:
            print(f"   Audience: {auth_config['audience']}")
    
    def _generate_recommendations(
        self, 
        discovered_config: Dict[str, Any],
        current_config: Optional[Dict[str, Any]],
        auth_required: bool = False
    ) -> List[str]:
        """Generate recommendations based on discovered vs current config and test results."""
        recommendations = []
        auth_type = discovered_config.get("type", "").lower() if discovered_config else None
        
        # If we detected auth is required but have no config
        if auth_required and not current_config:
            recommendations.append("🔐 Server requires authentication (detected from connection attempt)")
            if not discovered_config:
                recommendations.append("⚠️  No authentication metadata found - check server documentation")
        
        # Check if current config matches discovered
        if current_config:
            current_type = current_config.get("type", "").lower()
            
            if current_type == auth_type:
                recommendations.append("✅ Your authentication type matches the server's requirements")
                
                # Check if credentials are complete
                if "oauth" in auth_type:
                    if "client_id" in current_config and "client_secret" in current_config:
                        recommendations.append("✅ OAuth credentials configured")
                    else:
                        missing = []
                        if "client_id" not in current_config:
                            missing.append("client_id")
                        if "client_secret" not in current_config:
                            missing.append("client_secret")
                        recommendations.append(f"⚠️  Missing OAuth credentials: {', '.join(missing)}")
                        recommendations.append(self._get_credentials_guide(auth_type))
            else:
                recommendations.append(f"⚠️  Configuration mismatch:")
                recommendations.append(f"   • Your config: {current_type}")
                recommendations.append(f"   • Server requires: {auth_type}")
                recommendations.append("💡 Update your configuration to match server requirements")
        else:
            # No current config - provide setup instructions
            recommendations.append("⚠️  No authentication configured")
            recommendations.append(f"🔐 Server requires: {auth_type}")
            recommendations.append("")
            recommendations.append(self._get_setup_instructions(discovered_config))
        
        return recommendations
    
    def _get_credentials_guide(self, auth_type: str) -> str:
        """Get guide for obtaining credentials."""
        if "oauth" in auth_type:
            return (
                "📖 To get OAuth credentials:\n"
                "   1. Register your application with the service provider\n"
                "   2. Get your client_id and client_secret\n"
                "   3. Add them to your .env file or config"
            )
        elif "bearer" in auth_type or "token" in auth_type:
            return (
                "📖 To get an access token:\n"
                "   1. Check the service provider's documentation\n"
                "   2. Generate a personal access token or API key\n"
                "   3. Add it to your .env file"
            )
        else:
            return "📖 Check service documentation for authentication setup"
    
    def _get_setup_instructions(self, discovered_config: Dict[str, Any]) -> str:
        """Get setup instructions based on discovered config."""
        auth_type = discovered_config.get("type", "unknown")
        
        instructions = "📝 Setup Instructions:\n\n"
        
        if "oauth2" in auth_type or "oauth" in auth_type:
            instructions += "   Add to your config:\n"
            instructions += "   ```json\n"
            instructions += '   "authentication": {\n'
            instructions += f'     "type": "{auth_type}",\n'
            instructions += f'     "token_url": "{discovered_config.get("token_url", "...")}",\n'
            
            if "authorize_url" in discovered_config:
                instructions += f'     "authorize_url": "{discovered_config["authorize_url"]}",\n'
            
            if discovered_config.get("client_id"):
                instructions += f'     "client_id": "{discovered_config["client_id"]}",\n'
            else:
                instructions += '     "client_id": "${YOUR_CLIENT_ID}",\n'
            
            instructions += '     "client_secret": "${YOUR_CLIENT_SECRET}"'
            
            if "scopes" in discovered_config:
                scopes = discovered_config["scopes"]
                if isinstance(scopes, list):
                    instructions += ',\n'
                    instructions += f'     "scopes": {json.dumps(scopes)}'
            
            if "audience" in discovered_config:
                instructions += ',\n'
                instructions += f'     "audience": "{discovered_config["audience"]}"'
            
            instructions += '\n   }\n'
            instructions += "   ```\n\n"
            instructions += "   Then add to .env:\n"
            instructions += "   ```bash\n"
            
            if not discovered_config.get("client_id"):
                instructions += "   YOUR_CLIENT_ID=your_client_id_here\n"
            
            instructions += "   YOUR_CLIENT_SECRET=your_client_secret_here\n"
            instructions += "   ```"
        
        elif "bearer" in auth_type.lower():
            instructions += "   Add to your config:\n"
            instructions += "   ```json\n"
            instructions += '   "authentication": {\n'
            instructions += '     "type": "bearer_token",\n'
            instructions += '     "token": "${YOUR_TOKEN}"\n'
            instructions += "   }\n"
            instructions += "   ```\n\n"
            instructions += "   Then add to .env:\n"
            instructions += "   ```bash\n"
            instructions += "   YOUR_TOKEN=your_token_here\n"
            instructions += "   ```"
        
        else:
            instructions += f"   Authentication type: {auth_type}\n"
            instructions += "   Check service documentation for specific setup instructions"
        
        return instructions
    
    def validate_config_against_discovered(
        self,
        current_config: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Validate current configuration against discovered requirements.
        
        Args:
            current_config: Current authentication configuration
            
        Returns:
            Validation result with warnings and errors
        """
        if not self.discovered_config:
            return {
                "valid": None,
                "warnings": ["No discovered configuration to validate against"]
            }
        
        result = {
            "valid": True,
            "warnings": [],
            "errors": []
        }
        
        discovered_type = self.discovered_config.get("type", "").lower()
        current_type = current_config.get("type", "").lower()
        
        # Check type match
        if discovered_type != current_type:
            result["valid"] = False
            result["errors"].append(
                f"Authentication type mismatch: config has '{current_type}' "
                f"but server requires '{discovered_type}'"
            )
        
        # Check required fields for OAuth
        if "oauth" in discovered_type:
            required_fields = ["client_id", "client_secret", "token_url"]
            missing = [f for f in required_fields if f not in current_config]
            
            if missing:
                result["valid"] = False
                result["errors"].append(f"Missing required OAuth fields: {', '.join(missing)}")
        
        # Check scopes
        if "scopes" in self.discovered_config:
            discovered_scopes = set(self.discovered_config["scopes"])
            current_scopes = set(current_config.get("scopes", []))
            
            if not current_scopes.issuperset(discovered_scopes):
                missing_scopes = discovered_scopes - current_scopes
                result["warnings"].append(
                    f"Some required scopes may be missing: {', '.join(missing_scopes)}"
                )
        
        return result


def print_auth_discovery_summary(discovery_result: Dict[str, Any]):
    """Print a formatted summary of authentication discovery results."""
    print("\n" + "="*80)
    print("🔐 AUTHENTICATION DISCOVERY SUMMARY")
    print("="*80)
    
    # Show auth requirement test results
    if discovery_result.get("auth_required"):
        print("🔒 Authentication Status: REQUIRED")
        test_response = discovery_result.get("test_response")
        if test_response and isinstance(test_response, dict):
            if test_response.get("status_code"):
                print(f"   Test Result: {test_response.get('status_code')} - {test_response.get('error_type')}")
            if test_response.get("auth_header"):
                print(f"   Auth Type Hint: {test_response.get('auth_header')}")
        print()
    elif discovery_result.get("test_response"):
        print("ℹ️  Authentication Status: Not required or unknown")
        print()
    
    if discovery_result["discovered"]:
        print(f"✅ Authentication metadata found")
        print(f"   Source: {discovery_result['source']}")
        
        # Show discovered auth config details
        auth_config = discovery_result.get("auth_config", {})
        if auth_config:
            auth_type = auth_config.get("type", "unknown")
            print(f"   Type: {auth_type}")
            
            # Show environment variables if discovered from GitHub
            env_vars = auth_config.get("env_vars", [])
            if env_vars:
                print(f"   Environment Variables: {', '.join(env_vars)}")
        print()
        
        if discovery_result["recommendations"]:
            print("📋 Recommendations:")
            for rec in discovery_result["recommendations"]:
                if rec:  # Skip empty strings
                    print(f"{rec}")
    else:
        print("⚠️  No authentication metadata discovered")
        print()
        if discovery_result["recommendations"]:
            for rec in discovery_result["recommendations"]:
                if rec:
                    print(f"{rec}")
    
    print("="*80)

