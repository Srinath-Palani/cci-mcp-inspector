"""
MCP Attribute Extraction Agent

Extracts and classifies MCP server attributes including:
- Distribution type, protocol version, pricing
- Hosting provider, authentication methods
- Transport protocol, deployment approach
- Tool operation types
"""

import asyncio
import re
from typing import Dict, Any, List, Optional
from urllib.parse import urlparse

from src.models.structured_output import (
    MCPServerAttributes,
    DistributionType,
    MCPProtocolVersion,
    ProtocolVersionProbe,
    PricingModel,
    HostingProvider,
    AuthenticationMethods,
    DataProtectionTLS,
    TransportProtocol,
    ToolsOperationType,
    DeploymentApproach
)
from src.utility.documentation_analyzer import DocumentationAnalyzer

# Protocol versions the report has a checklist column for. Used to normalize the
# free-text versions that documentation analysis returns into known values.
KNOWN_PROTOCOL_VERSIONS = (
    "2024-11-05",
    "2025-03-26",
    "2025-06-18",
    "2025-11-25",
    "2026-07-28",
)


async def extract_server_attributes(
    server_config: Dict[str, Any],
    discovery_data: Dict[str, Any],
    analysis_data: Dict[str, Any],
    auth_discovery_data: Optional[Dict[str, Any]] = None
) -> MCPServerAttributes:
    """
    Extract comprehensive server attributes from configuration and discovered data.
    
    Args:
        server_config: Server configuration dictionary
        discovery_data: Results from discovery agent
        analysis_data: Results from analysis agent
    
    Returns:
        MCPServerAttributes with all classifications
    """
    
    print(f"\n{'='*60}")
    print(f"🏷️  MCP ATTRIBUTE EXTRACTION AGENT")
    print(f"{'='*60}")
    print(f"📊 Extracting server attributes...")
    print(f"{'='*60}\n")
    
    # Analyze documentation for additional metadata (auth methods, etc.)
    doc_analyzer = DocumentationAnalyzer()
    doc_analysis = await doc_analyzer.analyze_server_documentation(server_config)
    
    # Extract all attributes
    distribution_type = _extract_distribution_type(server_config, doc_analysis)
    protocol_version = _extract_protocol_version(server_config, discovery_data, doc_analysis)
    pricing = _extract_pricing(server_config)
    hosting_provider = _extract_hosting_provider(server_config)
    authentication = _extract_authentication(server_config, discovery_data, doc_analysis, auth_discovery_data)

    # These three extractors do blocking network I/O (TLS handshake probes and
    # GitHub README fetches) via synchronous ssl/socket/requests calls. Running
    # them inline would freeze the event loop for tens of seconds — long enough
    # that health checks and job-status polls stop answering and the job timeout
    # cannot fire. Offloading also makes each one a cancellation point.
    data_protection = await asyncio.to_thread(_extract_data_protection, server_config, doc_analysis)
    transport_protocol = await asyncio.to_thread(_extract_transport_protocol, server_config, doc_analysis)
    tools_operation_type = _extract_tools_operation_type(discovery_data)
    deployment_approach = await asyncio.to_thread(_extract_deployment_approach, server_config, doc_analysis)

    # Traffic name — real, externally-sourced only (proxy → server.json →
    # registry → live handshake). None when no real source supplied one.
    from src.utility.traffic_name import resolve_traffic_name
    from src.models.structured_output import ServerInfo
    handshake_name = ((discovery_data.get("server_info") or {}).get("server_info") or {}).get("name")
    traffic_name, traffic_source = await resolve_traffic_name(
        endpoint_url=server_config.get("endpoint_url") or server_config.get("url"),
        repository=server_config.get("repository"),
        handshake_server_name=handshake_name or discovery_data.get("server_name"),
        proxy_url=server_config.get("proxy_url"),
    )
    server_info = ServerInfo(traffic_name=traffic_name, traffic_name_source=traffic_source)

    attributes = MCPServerAttributes(
        server_info=server_info,
        distribution_type=distribution_type,
        protocol_version=protocol_version,
        pricing=pricing,
        hosting_provider=hosting_provider,
        authentication=authentication,
        data_protection=data_protection,
        transport_protocol=transport_protocol,
        tools_operation_type=tools_operation_type,
        deployment_approach=deployment_approach
    )
    
    print("✅ Attribute extraction complete\n")
    _print_attributes_summary(attributes)
    
    return attributes


def _extract_distribution_type(
    server_config: Dict[str, Any],
    doc_analysis: Optional[Dict[str, Any]] = None
) -> DistributionType:
    """
    Determine if server is official or community using dynamic detection.
    
    Priority order (dynamic detection first):
    1. Dynamic domain matching (endpoint URL contains company name)
    2. Dynamic repository ownership (org name matches server/company name)
    3. Dynamic npm package ownership (scoped packages with matching patterns)
    4. Documentation analysis (explicit "official" mentions)
    5. CLI command matching (command matches company name)
    6. Explicit configuration markers
    7. Known MCP organization (@modelcontextprotocol) - only standard org
    """
    # If distribution_type is explicitly provided (e.g., from the UI), use it directly
    explicit_distribution = (server_config.get("distribution_type") or "").strip().lower()
    if explicit_distribution in ("official", "community"):
        is_official = explicit_distribution == "official"
        print(f"   ✅ Distribution type explicitly set by user: {explicit_distribution}")
        return DistributionType(
            official=is_official,
            community=not is_official
        )
    
    server_name = (server_config.get("name") or "").lower()
    command = (server_config.get("command") or "").lower()
    args = " ".join(server_config.get("args") or []).lower()
    repository = (server_config.get("repository") or "").lower()
    endpoint_url = (server_config.get("endpoint_url") or "").lower()
    description = (server_config.get("description") or "").lower()
    
    # Check for official MCP servers from various sources
    is_official = False
    detection_source = None
    
    # Helper function to extract company name from server name
    def extract_company_name(name: str) -> Optional[str]:
        """Extract company name from server name using dynamic pattern matching."""
        if not name:
            return None
        
        # Split by common separators
        server_parts = re.split(r'[-_\s]+', name.lower())
        # Filter out common suffixes
        common_suffixes = ["mcp", "server", "gemini", "oauth", "auth", "cli", "sdk", "api"]
        for part in server_parts:
            if part and part not in common_suffixes and len(part) > 2:
                return part
        
        # If no good part found, try removing suffixes from the whole name
        normalized = name.lower()
        for suffix in common_suffixes:
            normalized = normalized.replace(suffix, "")
        normalized = normalized.replace("-", "").replace("_", "").strip()
        return normalized if len(normalized) > 2 else None
    
    company_name = extract_company_name(server_name)
    
    # PRIORITY 1: Dynamic domain matching (endpoint URL analysis)
    # If endpoint URL contains the company/server name, it's likely official
    if endpoint_url and not is_official:
        from urllib.parse import urlparse
        try:
            parsed = urlparse(endpoint_url)
            domain = parsed.netloc.lower()
            domain_parts = domain.split(".")
            
            # Extract base domain (e.g., "notion.com" from "mcp.notion.com")
            base_domain = '.'.join(domain_parts[-2:]) if len(domain_parts) >= 2 else domain
            # Handle 3-part domains like "co.uk", "com.au", etc.
            if len(domain_parts) >= 3 and domain_parts[-2] in ['co', 'com', 'net', 'org']:
                base_domain = '.'.join(domain_parts[-3:])
            
            # Dynamic matching: check if company name appears in domain
            if company_name and len(company_name) > 2:
                # Check if company name is in the base domain
                # Examples: "canva" in "canva.com", "notion" in "notion.com", "linear" in "linear.app"
                if (company_name in base_domain or 
                    base_domain.startswith(company_name + '.') or
                    base_domain.startswith(company_name + '-') or
                    f'.{company_name}.' in base_domain or
                    f'-{company_name}.' in base_domain):
                    is_official = True
                    detection_source = f"dynamic domain matching ({domain})"
                    print(f"   ✅ Official domain detected: {domain} (contains '{company_name}')")
        except Exception:
            pass
    
    # PRIORITY 2: Dynamic repository ownership (org name matches server/company name)
    if not is_official:
        repo_url = None
        if doc_analysis:
            manifest_info = doc_analysis.get("manifest_info", {})
            if manifest_info:
                repo_url = manifest_info.get("repository_url", "") or manifest_info.get("repository", "")
        
        if not repo_url:
            repo_url = repository
        
        if repo_url:
            repo_owner = _extract_repo_owner(repo_url)
            if repo_owner:
                # Dynamic matching: org name matches server/company name
                repo_owner_lower = repo_owner.lower()
                if company_name:
                    # Check if org name contains company name or vice versa
                    if (company_name in repo_owner_lower or 
                        repo_owner_lower in company_name or
                        repo_owner_lower == company_name):
                        is_official = True
                        detection_source = f"dynamic repository ownership ({repo_owner})"
                        print(f"   ✅ Official repository detected: {repo_owner} (matches '{company_name}')")
    
    # PRIORITY 3: Dynamic npm package ownership (scoped packages with matching patterns)
    if not is_official:
        package_name = None
        if doc_analysis:
            manifest_info = doc_analysis.get("manifest_info", {})
            if manifest_info:
                package_name = manifest_info.get("package_name", "")
        
        if not package_name and args:
            # Try to extract package name from args (e.g., "@canva/cli")
            match = re.search(r'@[^/\s]+(?:/[^/\s]+)?', args)
            if match:
                package_name = match.group(0)  # Full match like "@canva/cli"
        
        if package_name and company_name:
            package_lower = package_name.lower()
            # Dynamic matching: check if package org/name matches company name
            # Examples: "@canva/cli" -> "canva", "@notionhq/notion-mcp" -> "notion"
            if (f"@{company_name}" in package_lower or
                company_name in package_lower):
                is_official = True
                detection_source = f"dynamic npm package ownership ({package_name})"
                print(f"   ✅ Official npm package detected: {package_name} (matches '{company_name}')")
    
    # PRIORITY 4 (documentation "official" mention) was removed. It string-matched
    # the word "official" in the LLM's free-text analysis of an untrusted README,
    # so a hostile or hallucinating README ("This is the official MCP server for
    # X") flipped a security-relevant trust attribute. Distribution status must
    # come only from verifiable signals (domain/owner match), never from text the
    # server controls about itself.

    # PRIORITY 5: CLI command matching (command matches company name)
    if not is_official and command and company_name:
        command_lower = command.lower()
        # Check if command matches company name (e.g., "canva" command for "canva" server)
        if (command_lower == company_name or 
            command_lower == f"{company_name}-cli" or
            company_name in command_lower):
            is_official = True
            detection_source = f"dynamic CLI command matching ({command})"
            print(f"   ✅ Official CLI detected: {command} (matches '{company_name}')")
    
    # PRIORITY 6: Explicit configuration markers
    if not is_official and "official" in description:
        is_official = True
        detection_source = "explicit configuration marker"
    
    # PRIORITY 7: Known MCP organization (only standard org, no server-specific lists)
    if not is_official and "@modelcontextprotocol" in args:
        is_official = True
        detection_source = "MCP organization (@modelcontextprotocol)"
    
    # Print detection source if found
    if is_official and detection_source:
        print(f"   ✅ Official distribution detected: {detection_source}")
    
    return DistributionType(
        official=is_official,
        community=not is_official
    )


def _extract_repo_owner(repo_url: str) -> Optional[str]:
    """Extract repository owner from GitHub URL."""
    import re
    match = re.search(r'github\.com/([^/]+)/', repo_url.lower())
    if match:
        return match.group(1)
    return None


def _is_official_org(org_name: str, server_name: str) -> bool:
    """
    Check if organization is official using dynamic pattern matching.
    
    Uses dynamic matching instead of hardcoded lists:
    - Org name matches server name (e.g., "linear" org for "linear" server)
    - Org name contains server name or vice versa
    - Known standard organizations only (@modelcontextprotocol)
    
    Args:
        org_name: GitHub organization name
        server_name: Server name
    
    Returns:
        True if organization matches server name dynamically
    """
    org_name_lower = org_name.lower()
    server_name_lower = server_name.lower()
    
    # Extract company name from server name (remove common suffixes)
    def extract_company_name(name: str) -> str:
        """Extract company name from server name."""
        common_suffixes = ["mcp", "server", "gemini", "oauth", "auth", "cli", "sdk", "api"]
        parts = re.split(r'[-_\s]+', name.lower())
        for part in parts:
            if part and part not in common_suffixes and len(part) > 2:
                return part
        return name.lower()
    
    company_name = extract_company_name(server_name_lower)
    
    # Dynamic matching: org name matches server/company name
    # Examples: "linear" org for "linear" server, "notionhq" org for "notion" server
    if (org_name_lower == company_name or
        company_name in org_name_lower or
        org_name_lower in company_name or
        org_name_lower in server_name_lower or
        server_name_lower in org_name_lower):
        return True
    
    # Known standard organizations (only MCP org, no server-specific lists)
    if org_name_lower == "modelcontextprotocol":
        return True
    
    return False


def _extract_protocol_version(
    server_config: Dict[str, Any],
    discovery_data: Dict[str, Any],
    doc_analysis: Optional[Dict[str, Any]] = None
) -> MCPProtocolVersion:
    """Extract MCP protocol version from multiple sources."""
    # Priority order:
    # 1. Actual protocol initialization (most reliable)
    # 2. Documentation/package.json analysis
    # 3. Server configuration
    
    protocol_version = ""
    source = ""

    # Evidence set: every version we have a reason to believe is supported.
    # The raw version probe is the only source that can confirm several versions
    # from live behaviour; the SDK handshake contributes exactly one (the negotiated
    # version), because the client only ever offers its own latest.
    # Documentation/manifests can legitimately contribute several but are weaker.
    evidence: set = set()

    # Source 1: Protocol initialization (most reliable)
    server_info = discovery_data.get("server_info", {})
    negotiated_version = str(server_info.get("negotiated_version", "") or
                             server_info.get("protocol_version", "") or "")
    client_protocol_version = str(server_info.get("client_protocol_version", "") or "")
    protocol_version = negotiated_version
    if protocol_version:
        source = "protocol handshake"
        evidence.add(protocol_version)

    # Source 1b: the raw version probe. This offers each version explicitly instead
    # of accepting whatever the SDK hardcodes, so it is the only source that can
    # confirm more than one version — and the only one that can show a server
    # supports a version newer than the installed SDK knows about.
    probe = (
        discovery_data.get("protocol_version_probe")
        or server_info.get("protocol_version_probe")
        or {}
    )
    probe_supported = [str(v) for v in (probe.get("supported_versions") or []) if v]
    probe_latest = str(probe.get("latest_supported") or "") or None
    probe_error = probe.get("error") or None
    probe_entries = probe.get("results") or []
    if probe_supported:
        evidence.update(probe_supported)
        source = "version probe"
        if not protocol_version:
            protocol_version = probe_latest or probe_supported[0]
    era_evidence = probe.get("era_evidence") or None
    if era_evidence:
        print(f"   🕰️  Protocol era: {era_evidence}")

    # Source 2: Documentation analysis. Unlike the handshake this can name several
    # versions, so all of them count as evidence even when a handshake succeeded.
    if doc_analysis:
        documented = doc_analysis.get("protocol_versions_supported") or []
        if isinstance(documented, str):
            documented = [documented]
        documented_primary = doc_analysis.get("protocol_version", "")
        if documented_primary:
            documented = [*documented, documented_primary]
        for entry in documented:
            for known in KNOWN_PROTOCOL_VERSIONS:
                if known in str(entry):
                    evidence.add(known)
        if not protocol_version and documented_primary:
            protocol_version = documented_primary
            source = "documentation"

    # Source 3: Configuration fallback
    if not protocol_version:
        protocol_version = server_config.get("mcp_protocol_version", "")
        if not protocol_version:
            protocol_version = server_config.get("mcp_version", "")
        if protocol_version:
            source = "configuration"
            evidence.add(str(protocol_version))

    version_str = str(protocol_version)

    # Print source if found
    if version_str and version_str != "unknown":
        print(f"   📡 Protocol Version: {version_str} (from {source})")
        if client_protocol_version and negotiated_version and client_protocol_version != negotiated_version:
            print(f"   ↩️  Down-negotiated: offered {client_protocol_version}, server chose {negotiated_version}")

    def _has(version: str) -> bool:
        return any(version in candidate for candidate in evidence)

    return MCPProtocolVersion(
        version_2024_11_05=_has("2024-11-05"),
        version_2025_03_26=_has("2025-03-26"),
        version_2025_06_18=_has("2025-06-18"),
        version_2025_11_25=_has("2025-11-25"),
        version_2026_07_28=_has("2026-07-28"),
        detected_version=version_str if version_str else "unknown",
        negotiated_version=negotiated_version or None,
        client_protocol_version=client_protocol_version or None,
        latest_supported=probe_latest,
        supported_versions=probe_supported,
        era_evidence=era_evidence,
        probes=[ProtocolVersionProbe(**entry) for entry in probe_entries],
        # Only surfaced when the probe confirmed nothing: with a confirmed version
        # in hand, a per-version failure is normal (the server declined it) and is
        # already recorded in `probes`.
        probe_error=None if probe_supported else probe_error,
        evidence_source=source or None,
    )


def _extract_pricing(server_config: Dict[str, Any]) -> PricingModel:
    """Determine pricing model."""
    description = (server_config.get("description") or "").lower()
    pricing_info = (server_config.get("pricing") or "").lower()
    
    is_paid = (
        "paid" in pricing_info or
        "commercial" in pricing_info or
        "enterprise" in description or
        "subscription" in description
    )
    
    return PricingModel(
        free=not is_paid,
        paid=is_paid
    )


def _extract_hosting_provider(server_config: Dict[str, Any]) -> HostingProvider:
    """
    Determine hosting provider from domain name analysis.
    
    Strategy:
    1. Extract domain from endpoint_url (most reliable)
    2. Check against known SaaS providers
    3. Check against cloud providers (AWS, Azure, GCP, etc.)
    4. Check against code hosting platforms
    5. Fall back to repository URL if no endpoint
    """
    endpoint_url = server_config.get("endpoint_url", "")
    repo_url = server_config.get("repository", "")
    
    # Prefer endpoint_url for hosting detection (where it's actually hosted)
    url_to_check = endpoint_url or repo_url
    if not url_to_check:
        return HostingProvider(
            saas_vendor=False,
            third_party_saas=False,
            github=False,
            gitlab=False,
            bitbucket=False,
            sourcehut_gitea_gogs=False
        )
    
    url_lower = url_to_check.lower()
    
    # Extract domain from URL
    from urllib.parse import urlparse
    parsed = urlparse(url_to_check)
    domain = parsed.netloc.lower()
    
    # Extract base domain (e.g., "notion.com" from "mcp.notion.com")
    # Handle subdomains by checking if domain ends with known providers
    domain_parts = domain.split('.')
    base_domain = '.'.join(domain_parts[-2:]) if len(domain_parts) >= 2 else domain
    # Also check for 3-part domains like "co.uk", "com.au", etc.
    if len(domain_parts) >= 3 and domain_parts[-2] in ['co', 'com', 'net', 'org']:
        base_domain = '.'.join(domain_parts[-3:])
    
    # Get server name from config for dynamic detection
    server_name = (server_config.get("name") or "").lower().strip()
    
    # Cloud providers (3rd party hosting) - these are infrastructure providers
    cloud_providers = [
        # AWS
        "amazonaws.com", "aws.amazon.com", "cloudfront.net", "s3.amazonaws.com",
        # Azure
        "azurewebsites.net", "azure.com", "cloudapp.azure.com",
        # GCP
        "googleapis.com", "appspot.com", "run.app", "cloudfunctions.net",
        # Cloudflare
        "workers.dev", "pages.dev", "cloudflare.com",
        # Vercel
        "vercel.app", "vercel.com",
        # Netlify
        "netlify.app", "netlify.com",
        # Heroku
        "herokuapp.com", "heroku.com",
        # DigitalOcean
        "digitaloceanspaces.com", "ondigitalocean.app",
        # Fly.io
        "fly.dev", "fly.io",
        # Railway
        "railway.app",
        # Render
        "onrender.com"
    ]
    
    # Code hosting platforms
    code_hosts = {
        "github": ["github.com", "github.io"],
        "gitlab": ["gitlab.com", "gitlab.io"],
        "bitbucket": ["bitbucket.org", "bitbucket.io"],
        "sourcehut_gitea_gogs": ["sr.ht", "gitea.io", "gitea.com", "gogs.io"]
    }
    
    # Dynamic SaaS vendor detection:
    # 1. Check if server name matches the base domain (e.g., "canva" -> "canva.com")
    # 2. Check if base domain contains server name (e.g., "notion" -> "notion.com")
    # 3. Common TLDs for SaaS companies
    common_tlds = ['.com', '.app', '.io', '.net', '.org', '.so', '.co']
    
    is_saas_vendor = False
    if server_name:
        # Normalize server name (remove common prefixes/suffixes)
        normalized_name = server_name
        # Remove common prefixes
        for prefix in ['mcp-', 'mcp_', 'mcp']:
            if normalized_name.startswith(prefix):
                normalized_name = normalized_name[len(prefix):]
        
        # Check if base domain matches server name pattern
        # Examples: "canva" -> "canva.com", "notion" -> "notion.com", "linear" -> "linear.app"
        for tld in common_tlds:
            expected_domain = f"{normalized_name}{tld}"
            if base_domain == expected_domain or domain.endswith(f".{expected_domain}"):
                is_saas_vendor = True
                break
        
        # Also check if server name is in the domain (for cases like "mcp.notion.com")
        if not is_saas_vendor:
            # Check if normalized server name appears in base domain
            if normalized_name in base_domain and len(normalized_name) >= 3:
                # Make sure it's not just a substring match (e.g., "not" in "notion")
                # Check if it's at the start of the domain or after a dot
                if (base_domain.startswith(normalized_name + '.') or 
                    base_domain.startswith(normalized_name + '-') or
                    f'.{normalized_name}.' in base_domain or
                    f'-{normalized_name}.' in base_domain):
                    is_saas_vendor = True
    
    # Detect cloud provider (3rd party hosting)
    is_cloud_hosted = any(provider in domain for provider in cloud_providers)
    
    # Detect code hosting platforms
    is_github = any(host in domain for host in code_hosts["github"])
    is_gitlab = any(host in domain for host in code_hosts["gitlab"])
    is_bitbucket = any(host in domain for host in code_hosts["bitbucket"])
    is_sourcehut = any(host in domain for host in code_hosts["sourcehut_gitea_gogs"])
    
    # If no endpoint_url, check repository for code hosting
    if not endpoint_url and repo_url:
        # Repository doesn't tell us where it's HOSTED, just where code is stored
        # So only mark code hosting platforms, not as actual hosting
        pass
    
    # 3rd party SaaS: Uses cloud provider but serves as SaaS
    # This is hosting on AWS/Azure/GCP but the service itself is SaaS
    is_third_party_saas = is_cloud_hosted and not is_saas_vendor
    
    # Print detection for debugging
    if endpoint_url:
        print(f"   🏢 Hosting Detection (domain: {domain}):")
        if is_saas_vendor:
            print(f"      • SaaS Vendor: Official hosting")
        if is_third_party_saas:
            print(f"      • 3rd Party SaaS: Cloud-hosted service")
        if is_github or is_gitlab or is_bitbucket:
            print(f"      • Code Repository: Development platform")
        if not (is_saas_vendor or is_third_party_saas or is_github or is_gitlab or is_bitbucket):
            print(f"      • Self-hosted or unknown provider")
    
    return HostingProvider(
        saas_vendor=is_saas_vendor,
        third_party_saas=is_third_party_saas,
        github=is_github,
        gitlab=is_gitlab,
        bitbucket=is_bitbucket,
        sourcehut_gitea_gogs=is_sourcehut
    )


def _extract_authentication(
    server_config: Dict[str, Any],
    discovery_data: Dict[str, Any],
    doc_analysis: Optional[Dict[str, Any]] = None,
    auth_discovery_data: Optional[Dict[str, Any]] = None
) -> AuthenticationMethods:
    """
    Detect authentication methods from multiple sources:
    1. Proactive endpoint testing (401/403 responses)
    2. Server documentation (README, OpenAPI) via LLM
    3. Configuration (what YOU use)
    4. Tool descriptions for hints
    """
    auth_type = server_config.get("authentication", {})
    if isinstance(auth_type, str):
        auth_type = {"type": auth_type}
    
    auth_str = str(auth_type).lower()
    tools = discovery_data.get("tools_discovered", [])
    
    # Get authentication methods from proactive discovery (NEW!)
    from_discovery_bearer = False
    from_discovery_oauth = False
    from_discovery_oauth_auth_code = False
    from_discovery_oauth_client_creds = False
    from_discovery_api_key = False
    from_discovery_pat = False
    from_discovery_auth_required = False
    
    if auth_discovery_data:
        from_discovery_auth_required = auth_discovery_data.get("auth_required", False)
        
        # Check if discovery found auth config (from GitHub README or endpoint test)
        auth_config = auth_discovery_data.get("auth_config")
        if auth_config:
            discovered_type = auth_config.get("type", "").lower()
            all_types = auth_config.get("all_types", [])  # Get all detected types
            env_vars_found = auth_config.get("env_vars", [])

            # Iterate all detected types (primary + secondary) once — no duplicate block
            types_to_check = [discovered_type] + [t for t in all_types if t != discovered_type]

            for auth_type in types_to_check:
                auth_type_lower = auth_type.lower()

                if auth_type_lower == "api_key":
                    from_discovery_api_key = True
                    print(f"   🔐 API Key (detected from GitHub repository)")
                elif auth_type_lower == "bearer_token":
                    from_discovery_bearer = True
                    print(f"   🔐 Bearer Token (detected from GitHub repository)")
                elif auth_type_lower == "oauth2_authorization_code":
                    from_discovery_oauth_auth_code = True
                    print(f"   🔐 OAuth 2.1 - Authorization Code Flow (detected from GitHub repository)")
                elif auth_type_lower == "oauth2_client_credentials":
                    from_discovery_oauth_client_creds = True
                    print(f"   🔐 OAuth 2.1 - Client Credentials Flow (detected from GitHub repository)")
                elif auth_type_lower == "oauth2" or "oauth" in auth_type_lower:
                    from_discovery_oauth = True
                    print(f"   🔐 OAuth 2.x (detected from GitHub repository)")
                elif auth_type_lower == "personal_access_token":
                    from_discovery_pat = True
                    print(f"   🔐 Personal Access Token (detected from GitHub repository)")

            if env_vars_found:
                print(f"   📝 Environment variables: {', '.join(env_vars_found[:3])}")
        
        # Also check endpoint test response
        test_response = auth_discovery_data.get("test_response")
        if test_response and isinstance(test_response, dict):
            auth_header = test_response.get("auth_header", "")
            
            if from_discovery_auth_required and auth_header:
                auth_header_lower = auth_header.lower()
                # Check WWW-Authenticate header for auth type
                if "bearer" in auth_header_lower:
                    from_discovery_bearer = True
                    print(f"   🔐 Bearer Token (detected from endpoint test)")
                if "oauth" in auth_header_lower:
                    from_discovery_oauth = True
                    print(f"   🔐 OAuth (detected from endpoint test)")
    
    # Get authentication methods from documentation analysis
    doc_methods = []
    doc_source = "Unknown"
    confidence = "unknown"
    analysis_text = ""
    
    if doc_analysis:
        doc_methods = doc_analysis.get("authentication_methods") or []
        doc_source = doc_analysis.get("documentation_source") or "Unknown"
        confidence = doc_analysis.get("confidence") or "unknown"
        
        # Also search the full LLM analysis text for comprehensive detection
        llm_analysis = doc_analysis.get("llm_analysis") or {}
        if isinstance(llm_analysis, dict):
            analysis_text = str(llm_analysis).lower()
        elif isinstance(llm_analysis, str):
            analysis_text = llm_analysis.lower()
        elif llm_analysis is not None:
            analysis_text = str(llm_analysis).lower()
    
    # Combine extracted methods and full analysis text for detection
    # Ensure doc_methods is a list before iterating
    if not isinstance(doc_methods, list):
        doc_methods = []
    doc_methods_lower = [m.lower() for m in doc_methods if m is not None and isinstance(m, str)]
    combined_text = " ".join(doc_methods_lower) + " " + analysis_text
    
    # Print documentation findings
    if doc_methods:
        print(f"\n📚 Documentation Analysis ({doc_source}):")
        print(f"   Confidence: {confidence}")
        for method in doc_methods:
            print(f"   ✅ {method}")
        print()
    
    # Combine detection from multiple sources
    # Documentation analysis (most reliable)
    
    # OAuth Authorization Code Flow detection
    from_docs_oauth_auth = (
        any("oauth" in m and "authorization" in m for m in doc_methods_lower) or
        "authorization code" in combined_text or
        "authorization_code" in combined_text or
        "auth code" in combined_text or
        "auth_code" in combined_text
    )
    
    # OAuth Client Credentials Flow detection - improved patterns
    from_docs_oauth_client = (
        any("oauth" in m and "client" in m for m in doc_methods_lower) or
        "client credentials" in combined_text or
        "client_credentials" in combined_text or
        "client-credentials" in combined_text or
        "client credential" in combined_text or
        "clientcredential" in combined_text.replace(" ", "").replace("-", "")
    )
    
    # JWT detection
    from_docs_jwt = (
        any("jwt" in m for m in doc_methods_lower) or
        "jwt" in combined_text or
        "json web token" in combined_text
    )

    # NOTE: JWT is a token FORMAT, not an OAuth flow. A README mentioning "JWT"
    # (or an env value containing one) says nothing about whether the server
    # supports Client Credentials. The old `if from_docs_jwt: from_docs_oauth_client
    # = True` reported a guess as a detected security attribute, so it's removed —
    # JWT alone must not flip any OAuth flow flag.

    # Generic OAuth mention (no specific flow) — note it, but do NOT mark either
    # flow supported: "OAuth" in docs is compatible with auth-code-only, CC-only,
    # or legacy OAuth 2.0, and claiming both flows is a false security attribute.
    has_generic_oauth = any(
        ("oauth" in m or "oauth2" in m or "oauth 2" in m) and
        "authorization" not in m and
        "client" not in m
        for m in doc_methods_lower
    ) or ("oauth" in combined_text and "authorization code" not in combined_text and "client credential" not in combined_text)

    if has_generic_oauth:
        print("   ℹ️  Generic OAuth mentioned in docs, but no specific flow identified — leaving both flow flags unset")
    
    from_docs_bearer = any("bearer" in m for m in doc_methods_lower) or "bearer" in combined_text
    from_docs_pat = (
        any("personal access" in m or "pat" in m or "personal token" in m for m in doc_methods_lower) or
        "personal access token" in combined_text or
        "personal_access_token" in combined_text
    )
    from_docs_api = (
        any("api key" in m or "api token" in m for m in doc_methods_lower) or
        "api key" in combined_text or
        "api_token" in combined_text or
        "api_key" in combined_text
    )
    
    # Check environment variables for hints
    env_vars = server_config.get("env", {})
    env_str = " ".join([f"{k} {v}" for k, v in env_vars.items()]).lower()
    env_keys = " ".join(env_vars.keys()).lower() if env_vars else ""
    
    # Environment variable hints - more precise detection
    if "dev_token" in env_str or "developer_token" in env_str or "jwt" in env_str:
        from_docs_oauth_client = True
        from_docs_jwt = True
        print("   🔐 Developer token/JWT detected in environment variables")
    
    if "client_id" in env_str and "client_secret" in env_str:
        from_docs_oauth_client = True
        print("   🔐 OAuth client credentials detected in environment variables")
    
    # Detect auth type from environment variable NAMES (more precise)
    from_env_bearer = False
    from_env_pat = False
    from_env_api = False
    
    # Check each env var name for auth type hints
    for env_key in env_vars.keys():
        key_lower = env_key.lower()
        # Bearer token indicators (explicit bearer mention)
        if "bearer" in key_lower:
            from_env_bearer = True
            print(f"   🔐 Bearer Token detected from env var: {env_key}")
        # Personal Access Token indicators
        elif "personal" in key_lower or key_lower.endswith("_pat") or "_pat_" in key_lower:
            from_env_pat = True
            print(f"   🔐 Personal Access Token detected from env var: {env_key}")
        # API Key indicators (explicit api_key or apikey)
        elif "api_key" in key_lower or "apikey" in key_lower:
            from_env_api = True
            print(f"   🔐 API Key detected from env var: {env_key}")
        # ACCESS_TOKEN is ambiguous - could be PAT or OAuth, check context
        elif "access_token" in key_lower:
            # If it explicitly says "personal" it's a PAT
            if "personal" in key_lower:
                from_env_pat = True
                print(f"   🔐 Personal Access Token detected from env var: {env_key}")
            # Otherwise, it's likely a bearer token (OAuth access token or PAT used as bearer)
            # But we DON'T set bearer here - let documentation analysis determine this
        # ACCESS_KEY indicators (e.g. BROWSERSTACK_ACCESS_KEY, AWS_ACCESS_KEY_ID)
        elif "access_key" in key_lower:
            from_env_api = True
            print(f"   🔐 API Key (access key) detected from env var: {env_key}")
        # Generic TOKEN (e.g. GITHUB_TOKEN, BUILDKITE_API_TOKEN, AUTH_TOKEN)
        # Exclude already-handled: bearer_token, access_token, dev_token/developer_token (caught above)
        elif "token" in key_lower and "dev" not in key_lower:
            from_env_api = True
            print(f"   🔐 API Token detected from env var: {env_key}")
        # SECRET indicators (e.g. APP_SECRET, API_SECRET, GITHUB_APP_SECRET)
        # client_secret alongside client_id is already handled by the OAuth client check above
        elif "secret" in key_lower:
            from_env_api = True
            print(f"   🔐 API Token (secret) detected from env var: {env_key}")
        # CREDENTIAL indicators (e.g. DB_CREDENTIAL, SERVICE_CREDENTIAL)
        elif "credential" in key_lower:
            from_env_api = True
            print(f"   🔐 API Token (credential) detected from env var: {env_key}")
        # PASSWORD indicators (e.g. API_PASSWORD, SERVICE_PASSWORD)
        elif "password" in key_lower:
            from_env_api = True
            print(f"   🔐 API Token (password) detected from env var: {env_key}")
        # AUTH indicators (e.g. AUTH_TOKEN, AUTH_KEY) — exclude 'authorization' (header name)
        elif "auth" in key_lower and "authorization" not in key_lower:
            from_env_api = True
            print(f"   🔐 API Token (auth) detected from env var: {env_key}")
    
    # Config-based detection (what user explicitly configured in authentication field)
    from_config_oauth_auth = "oauth" in auth_str and "authorization" in auth_str
    from_config_oauth_client = "oauth" in auth_str and "client_credentials" in auth_str
    # More precise bearer detection - only match explicit "bearer" mentions, not generic "token"
    from_config_bearer = "bearer" in auth_str or "bearer_token" in auth_str
    from_config_pat = "personal_access_token" in auth_str or "personal access token" in auth_str
    from_config_api = "api_key" in auth_str or "api_token" in auth_str or "apikey" in auth_str
    
    # Generic `from_discovery_oauth` (a bare "oauth" match with no flow identified)
    # is deliberately NOT OR-ed into either specific flow flag: it only means
    # "some OAuth was mentioned". Reporting it as both flows would be a guess.
    return AuthenticationMethods(
        oauth2_1_authorization_code=from_discovery_oauth_auth_code or from_docs_oauth_auth or from_config_oauth_auth,
        oauth2_1_client_credentials=from_discovery_oauth_client_creds or from_docs_oauth_client or from_config_oauth_client,
        bearer_token=from_discovery_bearer or from_docs_bearer or from_config_bearer or from_env_bearer,
        personal_access_token=from_discovery_pat or from_docs_pat or from_config_pat or from_env_pat,
        api_token=from_discovery_api_key or from_docs_api or from_config_api or from_env_api
    )


def _extract_data_protection(
    server_config: Dict[str, Any],
    doc_analysis: Optional[Dict[str, Any]] = None
) -> DataProtectionTLS:
    """
    Detect TLS version using multiple sources:
    1. Vendor documentation (security policies, requirements)
    2. Proactive testing (actual TLS handshake)
    3. URL analysis (HTTPS vs HTTP)
    
    Strategy:
    - Check documentation for TLS requirements first
    - Then proactively test the endpoint to verify
    - Combine both sources for complete picture
    
    Note: Even if connection_type is "stdio", we still test the endpoint_url's TLS
    because the user might be using stdio as a workaround for authentication,
    but the server still has a remote HTTPS endpoint with TLS.
    """
    endpoint_url = server_config.get("endpoint_url", "")
    connection_type = (server_config.get("connection_type") or "").lower()
    
    # Extract TLS info from documentation
    doc_tls = {}
    if doc_analysis:
        doc_tls = doc_analysis.get("tls_info", {})
    
    # Check if endpoint uses HTTPS (regardless of connection_type)
    if not endpoint_url or not endpoint_url.startswith("https://"):
        # No HTTPS endpoint in config - try to find remote endpoint URL from README
        # This is especially important for STDIO servers that may have a remote SSE/HTTP endpoint
        server_name = (server_config.get("name") or "").lower()
        repository_url = server_config.get("repository", "")
        
        print(f"\n   🔍 TLS Detection: No endpoint_url in config, searching README for remote endpoints...")
        print(f"   📂 Repository: {repository_url}")
        
        # Try to extract remote endpoint URL from GitHub repository README
        remote_endpoint_from_readme = _extract_remote_endpoint_from_readme(repository_url)
        
        # If we found a remote endpoint from README, test TLS
        if remote_endpoint_from_readme:
            print(f"   ✅ Found remote endpoint in README: {remote_endpoint_from_readme}")
            print(f"   🔒 Testing TLS for endpoint: {remote_endpoint_from_readme}")
            try:
                tls_result = _test_tls_version(remote_endpoint_from_readme)
                if tls_result:
                    print(f"   🔒 TLS Test Result: {tls_result['version']}")
                    print(f"      Endpoint tested: {remote_endpoint_from_readme}")
                    print(f"      TLS 1.3: {tls_result['tls_1_3']}, TLS 1.2: {tls_result['tls_1_2']}")
                    if not tls_result.get('lower_or_none', True):
                        return DataProtectionTLS(
                            tls_1_3=tls_result['tls_1_3'],
                            tls_1_2=tls_result['tls_1_2'],
                            lower_or_no_encryption=tls_result['lower_or_none']
                        )
            except Exception as e:
                print(f"   ⚠️ TLS test failed for {remote_endpoint_from_readme}: {e}")
        else:
            print(f"   ⚠️ No remote endpoint URL found in README")
            # For STDIO servers without a documented remote endpoint, TLS is not applicable
            # Don't try to infer API endpoints - it's unreliable and may hit unrelated services
            if connection_type == "stdio":
                print(f"   ℹ️ TLS not applicable for STDIO server without documented remote endpoint")
                return DataProtectionTLS(
                    tls_1_3=False,
                    tls_1_2=False,
                    lower_or_no_encryption=False  # TLS not applicable, not "lower/no encryption"
                )

        # Try to dynamically infer API endpoint from server name (only for non-STDIO servers)
        if server_name and connection_type != "stdio":
            # Extract base name (remove common suffixes)
            base_name = server_name.replace(" ", "").replace("-", "")
            for suffix in ["mcp", "server", "cli", "search"]:
                if base_name.endswith(suffix):
                    base_name = base_name[:-len(suffix)]
            
            # Try common API endpoint patterns dynamically
            # Pattern: api.{base_name}.{tld}
            common_tlds = [".com", ".app", ".io", ".tech", ".net", ".org"]
            api_patterns = [
                f"https://api.{base_name}{tld}" for tld in common_tlds
            ]
            
            # Test each potential endpoint to find one that works
            for potential_endpoint in api_patterns:
                try:
                    tls_result = _test_tls_version(potential_endpoint)
                    if tls_result and not tls_result.get('lower_or_none', True):
                        print(f"   🔒 TLS Test Result: {tls_result['version']} (inferred API endpoint: {potential_endpoint})")
                        return DataProtectionTLS(
                            tls_1_3=tls_result['tls_1_3'],
                            tls_1_2=tls_result['tls_1_2'],
                            lower_or_no_encryption=tls_result['lower_or_none']
                        )
                except:
                    continue
        
        # Check documentation for TLS info before giving up
        if doc_tls:
            doc_supported = doc_tls.get("supported_versions", [])
            if "TLS 1.3" in str(doc_supported) or "1.3" in doc_tls.get("minimum_version", ""):
                print(f"   🔒 TLS Detection: TLS 1.3 (from documentation)")
                return DataProtectionTLS(
                    tls_1_3=True,
                    tls_1_2=False,
                    lower_or_no_encryption=False
                )
            elif "TLS 1.2" in str(doc_supported) or "1.2" in doc_tls.get("minimum_version", ""):
                print(f"   🔒 TLS Detection: TLS 1.2 (from documentation)")
                return DataProtectionTLS(
                    tls_1_3=False,
                    tls_1_2=True,
                    lower_or_no_encryption=False
                )
        
        # No HTTPS endpoint and no remote endpoint found
        # For STDIO servers without remote endpoints, TLS is not applicable (local process communication)
        # So we set all TLS flags to False - it's not "lower or no encryption", it's simply not applicable
        if connection_type == "stdio":
            print(f"   ℹ️ TLS not applicable for STDIO server without remote endpoint")
            return DataProtectionTLS(
                tls_1_3=False,
                tls_1_2=False,
                lower_or_no_encryption=False  # TLS not applicable, not "lower/no encryption"
            )
        
        # For non-STDIO servers (HTTP without HTTPS), this indicates lower/no encryption
        return DataProtectionTLS(
            tls_1_3=False,
            tls_1_2=False,
            lower_or_no_encryption=True
        )
    
    # Initialize results
    detected_tls_1_3 = False
    detected_tls_1_2 = False
    detected_lower = False
    
    # 1. Check documentation first
    if doc_tls:
        doc_min_version = doc_tls.get("minimum_version", "")
        doc_supported = doc_tls.get("supported_versions", [])
        doc_requirements = doc_tls.get("requirements", "")
        
        if doc_min_version or doc_supported or doc_requirements:
            print(f"   📚 Documentation TLS Info:")
            if doc_min_version:
                print(f"      Minimum: {doc_min_version}")
            if doc_supported:
                print(f"      Supported: {', '.join(doc_supported)}")
            if doc_requirements:
                print(f"      Policy: {doc_requirements}")
    
    # 2. Proactively test TLS version on the HTTPS endpoint
    tls_result = _test_tls_version(endpoint_url)
    
    if tls_result:
        # Build version string showing all supported versions
        supported_versions = []
        if tls_result['tls_1_3']:
            supported_versions.append("TLS 1.3")
        if tls_result['tls_1_2']:
            supported_versions.append("TLS 1.2")
        if tls_result['lower_or_none'] and not tls_result['tls_1_3'] and not tls_result['tls_1_2']:
            supported_versions.append("Lower/None")
        
        version_str = ', '.join(supported_versions) if supported_versions else tls_result['version']
        
        if connection_type == "stdio":
            print(f"   🔒 TLS Test Result: {version_str} (remote endpoint, connecting via stdio)")
        else:
            print(f"   🔒 TLS Test Result: {version_str} (proactive test)")
        
        detected_tls_1_3 = tls_result['tls_1_3']
        detected_tls_1_2 = tls_result['tls_1_2']
        detected_lower = tls_result['lower_or_none']
    else:
        # Fallback: Check documentation or assume TLS 1.2+ for HTTPS
        if doc_tls:
            doc_supported = doc_tls.get("supported_versions", [])
            if "TLS 1.3" in str(doc_supported) or "1.3" in doc_tls.get("minimum_version", ""):
                detected_tls_1_3 = True
                print(f"   🔒 TLS Detection: TLS 1.3 (from documentation)")
            elif "TLS 1.2" in str(doc_supported) or "1.2" in doc_tls.get("minimum_version", ""):
                detected_tls_1_2 = True
                print(f"   🔒 TLS Detection: TLS 1.2 (from documentation)")
            else:
                detected_tls_1_2 = True
                print(f"   🔒 TLS Detection: TLS 1.2+ (assumed for HTTPS)")
        else:
            detected_tls_1_2 = True
            print(f"   🔒 TLS Detection: TLS 1.2+ (assumed for HTTPS)")
    
    return DataProtectionTLS(
        tls_1_3=detected_tls_1_3,
        tls_1_2=detected_tls_1_2,
        lower_or_no_encryption=detected_lower
    )


def _test_tls_version(endpoint_url: str) -> Optional[Dict[str, Any]]:
    """
    Test ALL TLS versions an endpoint supports.
    
    Uses Python's ssl library to probe the server for each TLS version independently.
    Most servers support multiple TLS versions for backward compatibility.
    
    Args:
        endpoint_url: HTTPS endpoint to test
        
    Returns:
        Dictionary with TLS version information (all supported versions):
        {
            'version': 'TLS 1.3, TLS 1.2',
            'tls_1_3': True,
            'tls_1_2': True,
            'lower_or_none': False
        }
    """
    import ssl
    import socket
    from urllib.parse import urlparse
    
    try:
        parsed = urlparse(endpoint_url)
        hostname = parsed.netloc
        port = parsed.port or 443
        
        # Remove port from hostname if present
        if ':' in hostname:
            hostname = hostname.split(':')[0]
        
        # Track all supported versions
        supports_tls_1_3 = False
        supports_tls_1_2 = False
        supports_lower = False
        supported_versions = []
        
        # Test TLS 1.3
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.minimum_version = ssl.TLSVersion.TLSv1_3
            context.maximum_version = ssl.TLSVersion.TLSv1_3
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            
            with socket.create_connection((hostname, port), timeout=5) as sock:
                with context.wrap_socket(sock, server_hostname=hostname) as ssock:
                    supports_tls_1_3 = True
                    supported_versions.append("TLS 1.3")
        except:
            pass
        
        # Test TLS 1.2
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.maximum_version = ssl.TLSVersion.TLSv1_2
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            
            with socket.create_connection((hostname, port), timeout=5) as sock:
                with context.wrap_socket(sock, server_hostname=hostname) as ssock:
                    supports_tls_1_2 = True
                    supported_versions.append("TLS 1.2")
        except:
            pass
        
        # Test TLS 1.1 or lower (if system supports it)
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            # Try to set minimum version to TLS 1.0 if supported
            try:
                context.minimum_version = ssl.TLSVersion.TLSv1
                context.maximum_version = ssl.TLSVersion.TLSv1_1
            except:
                # TLS 1.0/1.1 might not be available in newer Python versions
                pass
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            
            with socket.create_connection((hostname, port), timeout=5) as sock:
                with context.wrap_socket(sock, server_hostname=hostname) as ssock:
                    version = ssock.version()
                    if version and "1.3" not in version and "1.2" not in version:
                        supports_lower = True
                        supported_versions.append(f"{version} (outdated)")
        except:
            pass
        
        # If no TLS versions found, return lower_or_none
        if not supports_tls_1_3 and not supports_tls_1_2 and not supports_lower:
            return {
                'version': 'No TLS or unreachable',
                'tls_1_3': False,
                'tls_1_2': False,
                'lower_or_none': True
            }
        
        # Return all supported versions
        return {
            'version': ', '.join(supported_versions) if supported_versions else 'Unknown',
            'tls_1_3': supports_tls_1_3,
            'tls_1_2': supports_tls_1_2,
            'lower_or_none': supports_lower and not supports_tls_1_3 and not supports_tls_1_2
        }
    
    except Exception as e:
        # Error during testing
        return None


def _detect_transport_from_readme(readme_content: str) -> Dict[str, bool]:
    """
    Parse README content to detect transport protocols.
    
    Uses intelligent detection to avoid false positives. HTTP/SSE transport
    is only detected when there's explicit configuration evidence, not just
    URLs mentioned anywhere in the README.
    
    Args:
        readme_content: README file content
        
    Returns:
        Dictionary with stdio, http_sse, streamable_http, fast_api flags
    """
    if not readme_content:
        return {"stdio": False, "http_sse": False, "streamable_http": False, "fast_api": False}
    
    readme_lower = readme_content.lower()
    
    # ============================================
    # Extract code blocks for more accurate analysis
    # ============================================
    code_block_pattern = r'```(?:json|yaml|yml|bash|shell|sh|zsh|typescript|ts|javascript|js)?\s*\n([\s\S]*?)\n```'
    code_blocks = re.findall(code_block_pattern, readme_content, re.IGNORECASE)
    code_block_content = '\n'.join(code_blocks).lower()
    
    # ============================================
    # STDIO transport indicators
    # ============================================
    # STDIO is common and can be detected from general patterns
    stdio_patterns = [
        r'\bstdio\b',
        r'\bcommand\s*[=:]\s*["\']?npx\b',
        r'\bnpx\s+[\w@/-]+',
        r'\bnode\s+[\w./]+\.js\b',
        r'\bpython\s+[\w./]+\.py\b',
        r'transport.*stdio',
        r'stdio.*transport',
        r'"transport"\s*:\s*"stdio"',
    ]
    is_stdio = any(re.search(pattern, readme_lower) for pattern in stdio_patterns)
    
    # ============================================
    # HTTP/SSE transport indicators (more strict)
    # ============================================
    # Only detect SSE from explicit configuration patterns, not general URL mentions
    is_sse = _detect_sse_transport(readme_content, code_block_content)
    
    # ============================================
    # Streamable HTTP indicators
    # ============================================
    streamable_patterns = [
        r'\bstreamable[_-]?http\b',
        r'streamable\s+http',
        r'"transport"\s*:\s*"streamable',
    ]
    is_streamable = any(re.search(pattern, readme_lower) for pattern in streamable_patterns)
    
    # ============================================
    # FastAPI indicators
    # ============================================
    fastapi_patterns = [
        r'\bfastapi\b',
        r'\bfast[_-]?api\b',
        r'uvicorn',
        r'starlette',
    ]
    is_fastapi = any(re.search(pattern, readme_lower) for pattern in fastapi_patterns)
    
    return {
        "stdio": is_stdio,
        "http_sse": is_sse,
        "streamable_http": is_streamable,
        "fast_api": is_fastapi
    }


def _detect_sse_transport(readme_content: str, code_block_content: str) -> bool:
    """
    Intelligently detect HTTP/SSE transport from README content.
    
    Only detects SSE when there's explicit configuration evidence:
    - SSE endpoint URLs in code blocks
    - Transport type set to "sse" in config
    - mcp-remote usage with SSE
    
    Ignores:
    - Badge URLs, documentation links
    - General mentions without config context
    
    Args:
        readme_content: Full README content
        code_block_content: Extracted code block content (lowercased)
        
    Returns:
        True if SSE transport is explicitly configured
    """
    readme_lower = readme_content.lower()
    
    # URLs to ignore (badges, documentation, registries, localhost)
    ignored_url_patterns = [
        r'github\.com',
        r'glama\.ai',
        r'shields\.io',
        r'badge',
        r'npmjs\.com',
        r'pypi\.org',
        r'docs\.',
        r'readme',
        r'registry',
        r'githubusercontent\.com',
        r'modelcontextprotocol\.io',
        r'anthropic\.com',
        # Localhost URLs are local, not remote SSE
        r'localhost',
        r'127\.0\.0\.1',
        r'0\.0\.0\.0',
        r'\[::1\]',
    ]
    
    # ============================================
    # High confidence: Explicit transport configuration
    # ============================================
    transport_config_patterns = [
        r'"transport"\s*:\s*"sse"',
        r'transport:\s*sse\b',
        r'"type"\s*:\s*"sse"',
        r'connection[-_]?type.*sse',
    ]
    
    for pattern in transport_config_patterns:
        if re.search(pattern, code_block_content):
            return True
    
    # ============================================
    # High confidence: SSE endpoint URL in config
    # ============================================
    sse_url_patterns = [
        r'"url"\s*:\s*"https?://[^"]+/sse"',
        r'"endpoint"\s*:\s*"https?://[^"]+/sse"',
        r'"endpoint_url"\s*:\s*"https?://[^"]+/sse"',
        r'url:\s*https?://\S+/sse\b',
        r'endpoint:\s*https?://\S+/sse\b',
    ]
    
    for pattern in sse_url_patterns:
        matches = re.findall(pattern, code_block_content)
        for match in matches:
            # Check if this URL should be ignored
            if not any(re.search(ignored, match, re.IGNORECASE) for ignored in ignored_url_patterns):
                return True
    
    # ============================================
    # Medium confidence: mcp-remote with SSE context
    # ============================================
    if re.search(r'\bmcp-remote\b.*sse|sse.*\bmcp-remote\b', code_block_content):
        return True
    
    # ============================================
    # Medium confidence: Explicit SSE section
    # ============================================
    section_patterns = [
        r'##\s*(?:sse|server-sent events?)\s*(?:setup|configuration|usage|transport)',
        r'###\s*(?:sse|server-sent events?)\s*(?:setup|configuration|usage|transport)',
        r'##\s*(?:using|connect(?:ing)?)\s+(?:sse|server-sent events?)',
    ]
    
    for pattern in section_patterns:
        if re.search(pattern, readme_lower):
            return True
    
    # ============================================
    # Low confidence patterns (only match in code blocks)
    # ============================================
    # "server-sent events" mentioned with transport context
    if re.search(r'server-sent\s+events?.*transport|transport.*server-sent\s+events?', code_block_content):
        return True
    
    return False


def _extract_transport_protocol(
    server_config: Dict[str, Any],
    doc_analysis: Optional[Dict[str, Any]] = None
) -> TransportProtocol:
    """Determine transport protocol from multiple sources."""
    # Priority order:
    # 1. Configuration (explicit user setting)
    # 2. GitHub README analysis
    # 3. Documentation analysis
    # 4. URL patterns
    
    connection_type = (server_config.get("connection_type") or "").lower()
    endpoint_url = server_config.get("endpoint_url", "")
    repository = server_config.get("repository", "")
    
    # Check documentation analysis
    detected_transport = ""
    if doc_analysis:
        transport_protocol = doc_analysis.get("transport_protocol")
        if transport_protocol and isinstance(transport_protocol, str):
            detected_transport = transport_protocol.lower()
            print(f"   🚀 Transport Protocol: {transport_protocol} (from documentation)")
    
    # Check GitHub README for transport options and deployment approach
    readme_transport = {"stdio": False, "http_sse": False, "streamable_http": False, "fast_api": False}
    readme_deployment = {"local": False, "container": False, "remote": False}
    if repository and "github.com" in repository:
        readme_content = _fetch_github_readme(repository)
        if readme_content:
            readme_transport = _detect_transport_from_readme(readme_content)
            readme_deployment = _detect_deployment_from_readme(readme_content)
            
            # Correlate: If Local deployment is detected, STDIO transport should also be Yes
            # Local deployment typically uses command-line (STDIO) execution
            if readme_deployment["local"] and not readme_transport["stdio"]:
                readme_transport["stdio"] = True
            
            # Correlate: If Remote deployment is detected, HTTP/SSE transport should also be Yes
            # Remote deployment typically uses HTTP-based transports
            if readme_deployment["remote"] and not readme_transport["http_sse"]:
                readme_transport["http_sse"] = True
            
            # Log what we found
            found_transports = []
            if readme_transport["stdio"]:
                found_transports.append("STDIO")
            if readme_transport["http_sse"]:
                found_transports.append("HTTP/SSE")
            if readme_transport["streamable_http"]:
                found_transports.append("StreamableHttp")
            if readme_transport["fast_api"]:
                found_transports.append("FastAPI")
            
            if found_transports:
                print(f"   🚀 Transport options from README: {', '.join(found_transports)}")
    
    # Determine each transport type (combine all sources)
    is_stdio = (
        connection_type == "stdio" or 
        "stdio" in detected_transport or
        readme_transport["stdio"]
    )
    is_sse = (
        "sse" in connection_type or 
        "/sse" in endpoint_url or 
        "sse" in detected_transport or
        "server-sent events" in detected_transport or
        readme_transport["http_sse"]
    )
    is_websocket = (
        "websocket" in connection_type or 
        "/ws" in endpoint_url or
        "websocket" in detected_transport
    )
    is_streamable_http = (
        (endpoint_url.startswith("http") and not is_sse and not is_websocket) or
        readme_transport["streamable_http"]
    )
    is_fastapi = (
        "fastapi" in str(server_config).lower() or 
        "fastapi" in detected_transport or
        readme_transport["fast_api"]
    )
    
    return TransportProtocol(
        stdio=is_stdio,
        http_sse=is_sse,
        streamable_http=is_streamable_http,
        fast_api=is_fastapi
    )


def _extract_tools_operation_type(discovery_data: Dict[str, Any]) -> ToolsOperationType:
    """
    Classify tool operations: annotation hints first (readOnlyHint /
    destructiveHint from the live tools/list response), name+description
    keywords only as a labeled fallback for unannotated tools. See
    src/utility/tool_operation_classifier.py for the tier rules.
    """
    from src.utility.tool_operation_classifier import rollup_operation_type

    tools = discovery_data.get("tools_discovered", [])
    read_only, read_update, read_update_delete, stats = rollup_operation_type(tools)
    if tools:
        print(
            f"   🧰 Tool R/W/D evidence: {stats['annotation']} annotation-confirmed, "
            f"{stats['heuristic']} heuristic, {stats['none']} no-signal "
            f"(of {len(tools)} tools)"
        )
    return ToolsOperationType(
        read_only=read_only,
        read_update=read_update,
        read_update_delete=read_update_delete,
    )


def _fetch_github_readme(repo_url: str) -> Optional[str]:
    """
    Fetch README content from GitHub repository.
    
    Args:
        repo_url: GitHub repository URL
        
    Returns:
        README content as string, or None if not found
    """
    try:
        import requests
    except ImportError:
        return None
    
    # Extract owner/repo from URL
    # Patterns: github.com/owner/repo, github.com/owner/repo.git
    match = re.search(r'github\.com[/:]([^/]+)/([^/\s\.]+)', repo_url)
    if not match:
        return None
    
    owner = match.group(1)
    repo = match.group(2).rstrip('.git')
    
    # Try to fetch README from raw.githubusercontent.com
    for branch in ["main", "master"]:
        for readme_file in ["README.md", "README", "readme.md"]:
            url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{readme_file}"
            try:
                response = requests.get(url, timeout=10)
                if response.status_code == 200:
                    return response.text
            except Exception:
                continue
    
    return None


def _extract_remote_endpoint_from_readme(repository_url: str) -> Optional[str]:
    """
    Extract remote MCP endpoint URL from GitHub README.
    
    This function looks for remote endpoint URLs in:
    1. JSON/YAML code blocks (config examples)
    2. SSE endpoint URLs (/sse suffix)
    3. MCP endpoint URLs (/mcp suffix)
    4. API endpoint patterns (api.*, mcp.*)
    
    Filters out:
    - Localhost URLs (127.0.0.1, localhost, 0.0.0.0)
    - Badge/documentation URLs (github.com, glama.ai, shields.io)
    - PyPI/NPM registry URLs
    
    Args:
        repository_url: GitHub repository URL
        
    Returns:
        Remote endpoint URL if found, None otherwise
    """
    if not repository_url or "github.com" not in repository_url:
        return None
    
    try:
        import requests
    except ImportError:
        return None
    
    # Fetch README content
    readme_content = _fetch_github_readme(repository_url)
    if not readme_content:
        return None
    
    # URLs to ignore (badges, documentation, registries, localhost)
    ignored_patterns = [
        r'github\.com',
        r'glama\.ai',
        r'shields\.io',
        r'badge',
        r'npmjs\.com',
        r'pypi\.org',
        r'docs\.',
        r'readme',
        r'registry',
        r'githubusercontent\.com',
        r'modelcontextprotocol\.io',
        r'anthropic\.com',
        r'localhost',
        r'127\.0\.0\.1',
        r'0\.0\.0\.0',
        r'\[::1\]',
    ]
    
    def is_valid_endpoint(url: str) -> bool:
        """Check if URL is a valid remote endpoint (not ignored)."""
        url_lower = url.lower()
        for pattern in ignored_patterns:
            if re.search(pattern, url_lower, re.IGNORECASE):
                return False
        return True
    
    # ============================================
    # PHASE 1: Extract code blocks from README
    # ============================================
    code_block_pattern = r'```(?:json|yaml|yml|bash|shell|sh|typescript|ts|javascript|js)?\s*\n([\s\S]*?)\n```'
    code_blocks = re.findall(code_block_pattern, readme_content, re.IGNORECASE)
    code_block_content = '\n'.join(code_blocks)
    
    # ============================================
    # PHASE 2: Look for SSE/MCP endpoint URLs in code blocks (highest priority)
    # ============================================
    # These are explicit MCP remote endpoints
    sse_mcp_patterns = [
        r'"url"\s*:\s*"(https://[^"]+/sse)"',
        r'"endpoint"\s*:\s*"(https://[^"]+/sse)"',
        r'"endpoint_url"\s*:\s*"(https://[^"]+/sse)"',
        r'"url"\s*:\s*"(https://[^"]+/mcp)"',
        r'"endpoint"\s*:\s*"(https://[^"]+/mcp)"',
        r'url:\s*(https://\S+/sse)\b',
        r'endpoint:\s*(https://\S+/sse)\b',
    ]
    
    for pattern in sse_mcp_patterns:
        matches = re.findall(pattern, code_block_content, re.IGNORECASE)
        for match in matches:
            if is_valid_endpoint(match):
                return match
    
    # ============================================
    # PHASE 3: Look for general endpoint URLs in JSON configs
    # ============================================
    general_url_patterns = [
        r'"url"\s*:\s*"(https://[^"]+)"',
        r'"endpoint"\s*:\s*"(https://[^"]+)"',
        r'"endpoint_url"\s*:\s*"(https://[^"]+)"',
        r'"baseUrl"\s*:\s*"(https://[^"]+)"',
        r'"server_url"\s*:\s*"(https://[^"]+)"',
    ]
    
    for pattern in general_url_patterns:
        matches = re.findall(pattern, code_block_content, re.IGNORECASE)
        for match in matches:
            if is_valid_endpoint(match):
                return match
    
    # ============================================
    # PHASE 4: Look for API/MCP domain patterns anywhere in README
    # ============================================
    # These are common patterns for MCP server endpoints
    api_patterns = [
        r'(https://api\.[a-zA-Z0-9\-]+\.[a-z]{2,}[^\s"\'<>]*)',
        r'(https://mcp\.[a-zA-Z0-9\-]+\.[a-z]{2,}[^\s"\'<>]*)',
        r'(https://[a-zA-Z0-9\-]+\.api\.[a-zA-Z0-9\-]+\.[a-z]{2,}[^\s"\'<>]*)',
    ]
    
    readme_lower = readme_content.lower()
    for pattern in api_patterns:
        matches = re.findall(pattern, readme_content, re.IGNORECASE)
        for match in matches:
            # Clean up the URL (remove trailing punctuation)
            clean_url = match.rstrip('.,;:)\'"]')
            if is_valid_endpoint(clean_url):
                return clean_url
    
    return None


def _detect_deployment_from_readme(readme_content: str) -> Dict[str, bool]:
    """
    Parse README content to detect deployment approaches.
    
    Uses intelligent detection to avoid false positives from badge URLs,
    documentation links, etc. Remote URLs are only detected in executable
    contexts like JSON/YAML code blocks and CLI examples.
    
    Args:
        readme_content: README file content
        
    Returns:
        Dictionary with local, container, remote flags
    """
    if not readme_content:
        return {"local": False, "container": False, "remote": False}
    
    readme_lower = readme_content.lower()
    
    # Local deployment indicators
    local_patterns = [
        r'\bnpx\b',
        r'\bnpm\s+(install|run)\b',
        r'\brun\s+locally\b',
        r'\blocal\s+(installation|setup|development)\b',
        r'\binstall\s+locally\b',
        r'\bnode\s+\w+\.js\b',
        r'\bpython\s+\w+\.py\b',
        r'\bpip\s+install\b',
        r'\buv\s+run\b',
        r'## local',
        r'### local',
        r'## installation',
        r'### installation',
        r'\bstdio\b',
    ]
    
    # Container deployment indicators - must be deployment-related, not product names
    container_patterns = [
        r'\bdocker\s+run\b',
        r'\bdocker\s+build\b',
        r'\bdocker\s+pull\b',
        r'\bdocker\s+compose\b',
        r'\bdocker-compose\b',
        r'\bdockerfile\b',
        r'\bkubernetes\b',
        r'\bk8s\b',
        r'\bhelm\s+(install|chart)\b',
        r'\bpodman\b',
        r'\bcontainer\s+(image|deployment|registry)\b',
        r'\bdeploy\s+(with|using|to)\s+docker\b',
        r'\brun\s+(in|with)\s+(a\s+)?container\b',
        r'## docker',
        r'### docker',
    ]
    
    # Check local and container patterns (simple pattern matching)
    is_local = any(re.search(pattern, readme_lower) for pattern in local_patterns)
    is_container = any(re.search(pattern, readme_lower) for pattern in container_patterns)
    
    # Remote detection requires more intelligent analysis
    is_remote = _detect_remote_deployment(readme_content)
    
    return {
        "local": is_local,
        "container": is_container,
        "remote": is_remote
    }


def _detect_remote_deployment(readme_content: str) -> bool:
    """
    Intelligently detect remote deployment from README content.
    
    Only detects remote URLs in executable contexts:
    - JSON/YAML code blocks
    - CLI examples (npx, uvx, mcp-remote)
    - Config sections
    
    Ignores URLs containing:
    - github.com, glama.ai, shields.io
    - badge, docs, readme, registry
    
    Higher confidence if:
    - URL ends with /sse
    - Appears near: mcp-remote, transport, SSE, server.url
    
    Args:
        readme_content: README file content
        
    Returns:
        True if remote deployment is detected
    """
    if not readme_content:
        return False
    
    readme_lower = readme_content.lower()
    
    # URLs to ignore (badges, documentation, registries, localhost)
    ignored_url_patterns = [
        r'github\.com',
        r'glama\.ai',
        r'shields\.io',
        r'badge',
        r'npmjs\.com',
        r'npmjs\.org',
        r'pypi\.org',
        r'docs\.',
        r'readme',
        r'registry',
        r'githubusercontent\.com',
        r'raw\.githubusercontent',
        r'modelcontextprotocol\.io',
        r'anthropic\.com',
        # Localhost URLs are local, not remote
        r'localhost',
        r'127\.0\.0\.1',
        r'0\.0\.0\.0',
        r'\[::1\]',
    ]
    
    # ============================================
    # PHASE 1: Non-URL based remote indicators
    # ============================================
    # These are strong indicators that don't depend on URLs
    
    non_url_remote_patterns = [
        r'\bremote\s+(server|deployment|endpoint|mcp)\b',
        r'\bhosted\s+(mcp|server|endpoint|service)\b',
        r'\bcloud\s+(deployment|hosted)\b',
        r'\bdeploy\s+to\s+(vercel|heroku|aws|azure|gcp|cloudflare|render|railway|fly\.io)\b',
        r'\bmcp-remote\b',
        r'\bstreamable[-_]?http\b',
    ]
    
    for pattern in non_url_remote_patterns:
        if re.search(pattern, readme_lower):
            return True
    
    # ============================================
    # PHASE 2: Extract code blocks for URL analysis
    # ============================================
    # Only look for URLs inside JSON/YAML/shell code blocks
    
    # Pattern to extract code blocks (```json, ```yaml, ```bash, ```shell, etc.)
    code_block_pattern = r'```(?:json|yaml|yml|bash|shell|sh|zsh|typescript|ts|javascript|js)?\s*\n([\s\S]*?)\n```'
    code_blocks = re.findall(code_block_pattern, readme_content, re.IGNORECASE)
    
    # Combine all code blocks for analysis
    code_block_content = '\n'.join(code_blocks).lower()
    
    # ============================================
    # PHASE 3: Look for remote URLs in code blocks
    # ============================================
    
    # High confidence URL patterns (only in code blocks)
    high_confidence_url_patterns = [
        r'https?://[^\s"\']+/sse\b',           # URL ending with /sse
        r'"url"\s*:\s*"https?://[^"]+',        # "url": "https://..."
        r'"endpoint"\s*:\s*"https?://[^"]+',   # "endpoint": "https://..."
        r'"server_url"\s*:\s*"https?://[^"]+', # "server_url": "https://..."
        r'"endpoint_url"\s*:\s*"https?://[^"]+', # "endpoint_url": "https://..."
        r'"baseUrl"\s*:\s*"https?://[^"]+',    # "baseUrl": "https://..."
        r'url:\s*https?://\S+',                # url: https://... (YAML style)
        r'endpoint:\s*https?://\S+',           # endpoint: https://... (YAML style)
    ]
    
    for pattern in high_confidence_url_patterns:
        matches = re.findall(pattern, code_block_content)
        for match in matches:
            # Check if this URL should be ignored
            if not any(re.search(ignored, match, re.IGNORECASE) for ignored in ignored_url_patterns):
                return True
    
    # ============================================
    # PHASE 4: Look for mcp-remote or SSE transport in config
    # ============================================
    
    # Check for mcp-remote usage in code blocks
    if re.search(r'\bmcp-remote\b', code_block_content):
        return True
    
    # Check for SSE transport configuration
    sse_config_patterns = [
        r'"transport"\s*:\s*"sse"',
        r'transport:\s*sse\b',
        r'"type"\s*:\s*"sse"',
        r'connection[-_]?type.*sse',
    ]
    
    for pattern in sse_config_patterns:
        if re.search(pattern, code_block_content):
            return True
    
    # ============================================
    # PHASE 5: Cloud provider deployment mentions (in context)
    # ============================================
    # Only match if cloud provider is mentioned with deployment context
    
    cloud_deployment_patterns = [
        r'deploy(?:ed|ing|ment)?\s+(?:to|on|with)\s+(?:vercel|heroku|aws|azure|gcp|cloudflare|render|railway|fly\.io)\b',
        r'(?:vercel|heroku|aws|azure|gcp|cloudflare|render|railway|fly\.io)\s+deploy(?:ed|ing|ment)?\b',
        r'hosted\s+on\s+(?:vercel|heroku|aws|azure|gcp|cloudflare|render|railway|fly\.io)\b',
        r'run(?:ning)?\s+on\s+(?:vercel|heroku|aws|azure|gcp|cloudflare|render|railway|fly\.io)\b',
    ]
    
    for pattern in cloud_deployment_patterns:
        if re.search(pattern, readme_lower):
            return True
    
    # ============================================
    # PHASE 6: Check for explicit SSE or remote sections
    # ============================================
    
    section_patterns = [
        r'##\s*(?:remote|sse|hosted|cloud)\s*(?:setup|deployment|configuration|usage)',
        r'###\s*(?:remote|sse|hosted|cloud)\s*(?:setup|deployment|configuration|usage)',
        r'##\s*(?:using|connect(?:ing)?)\s+(?:remote|sse|hosted)',
    ]
    
    for pattern in section_patterns:
        if re.search(pattern, readme_lower):
            return True
    
    return False


def _extract_deployment_approach(server_config: Dict[str, Any], doc_analysis: Optional[Dict[str, Any]] = None) -> DeploymentApproach:
    """
    Determine deployment approach from config, endpoint, and GitHub README.
    
    Priority:
    1. Parse GitHub README for deployment instructions (if repository provided)
    2. Infer from connection_type and endpoint_url
    3. Use explicit deployment field in config
    """
    connection_type = (server_config.get("connection_type") or "").lower()
    endpoint_url = server_config.get("endpoint_url", "")
    deployment = (server_config.get("deployment") or "").lower()
    repository = server_config.get("repository", "")
    
    # Initialize from basic config inference
    # SSE connection_type always implies remote deployment (SSE requires HTTP endpoint)
    is_remote = bool(endpoint_url) or "remote" in deployment or connection_type == "sse"
    is_container = "docker" in deployment or "container" in deployment
    # STDIO can support both local and remote deployment
    is_local = connection_type == "stdio"
    
    # Log if SSE is detected from config
    if connection_type == "sse":
        print(f"   📡 SSE connection type detected - Remote deployment enabled")
    
    # Try to fetch and parse GitHub README for more accurate detection
    readme_deployment = {"local": False, "container": False, "remote": False}
    if repository and "github.com" in repository:
        print(f"   🔍 Checking GitHub README for deployment options...")
        readme_content = _fetch_github_readme(repository)
        if readme_content:
            readme_deployment = _detect_deployment_from_readme(readme_content)
            
            # Log what we found
            found_options = []
            if readme_deployment["local"]:
                found_options.append("Local")
            if readme_deployment["container"]:
                found_options.append("Container")
            if readme_deployment["remote"]:
                found_options.append("Remote")
            
            if found_options:
                print(f"   ✅ Deployment options from README: {', '.join(found_options)}")
            else:
                print(f"   ⚠️  No explicit deployment options found in README")
            
            # Combine README findings with config inference
            # README findings take precedence if found
            if any(readme_deployment.values()):
                is_local = readme_deployment["local"] or is_local
                is_container = readme_deployment["container"] or is_container
                is_remote = readme_deployment["remote"] or is_remote
        else:
            print(f"   ⚠️  Could not fetch README from GitHub repository")
    
    # If still no deployment detected, use defaults based on connection type
    if not is_local and not is_container and not is_remote:
        if connection_type == "stdio":
            is_local = True
        # Note: SSE is already handled above in the initial inference
    
    return DeploymentApproach(
        local=is_local,
        container=is_container,
        remote=is_remote
    )


def _print_attributes_summary(attributes: MCPServerAttributes) -> None:
    """Print a summary of extracted attributes."""
    print("📋 ATTRIBUTE SUMMARY")
    print("-" * 60)
    
    # Distribution Type
    if attributes.distribution_type.official:
        print("✅ Distribution Type: Official")
    elif attributes.distribution_type.community:
        print("✅ Distribution Type: Community")
    
    # Protocol Version
    if attributes.protocol_version.detected_version:
        print(f"✅ Protocol Version: {attributes.protocol_version.detected_version}")
    
    # Pricing
    if attributes.pricing.free:
        print("✅ Pricing: Free")
    elif attributes.pricing.paid:
        print("✅ Pricing: Paid")
    
    # Hosting
    hosting = []
    if attributes.hosting_provider.github:
        hosting.append("GitHub")
    if attributes.hosting_provider.gitlab:
        hosting.append("GitLab")
    if attributes.hosting_provider.saas_vendor:
        hosting.append("SaaS Vendor")
    if hosting:
        print(f"✅ Hosting: {', '.join(hosting)}")
    
    # Transport
    transport = []
    if attributes.transport_protocol.stdio:
        transport.append("STDIO")
    if attributes.transport_protocol.http_sse:
        transport.append("HTTP/SSE")
    if transport:
        print(f"✅ Transport: {', '.join(transport)}")
    
    # Operations
    if attributes.tools_operation_type.read_only:
        print("✅ Tools Operations: Read-only")
    elif attributes.tools_operation_type.read_update:
        print("✅ Tools Operations: Read + Update")
    elif attributes.tools_operation_type.read_update_delete:
        print("✅ Tools Operations: Read + Update + Delete")
    
    # Deployment
    deployment = []
    if attributes.deployment_approach.local:
        deployment.append("Local")
    if attributes.deployment_approach.container:
        deployment.append("Container")
    if attributes.deployment_approach.remote:
        deployment.append("Remote")
    if deployment:
        print(f"✅ Deployment: {', '.join(deployment)}")
    
    print("-" * 60 + "\n")

