"""
Pydantic models for MCP Server Inspector structured outputs.

These models define the schema for MCP server attributes, tools, resources,
and analysis results.
"""

from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
from datetime import datetime


# Single source of truth for MCP protocol versions, newest first.
# Report generators iterate this list; adding a future version is a one-line change
# here plus a matching flag on MCPProtocolVersion.
SUPPORTED_PROTOCOL_VERSIONS = [
    "2026-07-28",
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
]


def protocol_version_field(version: str) -> str:
    """Map a protocol version string to its MCPProtocolVersion flag name."""
    return "version_" + version.replace("-", "_")


# --------------------------------------------------------
# Tool-level models
# --------------------------------------------------------

class MCPToolParameter(BaseModel):
    """Schema for a single tool parameter."""
    name: str = Field(description="Parameter name")
    type: str = Field(description="Parameter type (string, number, boolean, object, array)")
    description: Optional[str] = Field(default=None, description="Human-readable parameter description")
    required: bool = Field(default=False, description="Whether this parameter is required")
    default: Optional[Any] = Field(default=None, description="Default value if not provided")
    enum: Optional[List[str]] = Field(default=None, description="Allowed values if parameter is an enum")
    properties: Optional[Dict[str, Any]] = Field(default=None, description="Nested properties for object types")


class MCPTool(BaseModel):
    """Schema for an MCP tool/function."""
    name: str = Field(description="Tool name/identifier")
    description: Optional[str] = Field(default=None, description="What the tool does")
    parameters: List[MCPToolParameter] = Field(default_factory=list, description="List of tool parameters")
    category: Optional[str] = Field(default=None, description="Tool category (auto-categorized by analysis agent)")
    examples: Optional[List[str]] = Field(default=None, description="Usage examples")
    requires_auth: Optional[bool] = Field(default=None, description="Whether tool requires authentication")
    is_async: Optional[bool] = Field(default=None, description="Whether tool is asynchronous")


# --------------------------------------------------------
# Resource-level models
# --------------------------------------------------------

class MCPResource(BaseModel):
    """Schema for an MCP resource."""
    uri: str = Field(description="Resource URI/identifier")
    name: str = Field(description="Resource name")
    description: Optional[str] = Field(default=None, description="Resource description")
    mimeType: Optional[str] = Field(default=None, description="MIME type of resource content")


class MCPResourceTemplate(BaseModel):
    """Schema for an MCP resource template (URI pattern with variables)."""
    uriTemplate: str = Field(description="URI template, e.g. 'file://{path}'")
    name: str = Field(description="Template name")
    description: Optional[str] = Field(default=None)
    mimeType: Optional[str] = Field(default=None)


# --------------------------------------------------------
# Prompt-level models
# --------------------------------------------------------

class MCPPrompt(BaseModel):
    """Schema for an MCP prompt template."""
    name: str = Field(description="Prompt name/identifier")
    title: Optional[str] = Field(default=None, description="Human-readable prompt title (2025-06-18+)")
    description: Optional[str] = Field(default=None, description="Prompt description")
    arguments: Optional[List[MCPToolParameter]] = Field(default=None, description="Prompt arguments/variables")


# --------------------------------------------------------
# Server capability models
# --------------------------------------------------------

class MCPCapabilities(BaseModel):
    """MCP server capabilities.

    Flags come from the InitializeResult handshake (authoritative). The
    *_mismatch flags mark servers that respond to list calls for a capability
    they did NOT declare in the handshake — buggy but common; the Inspector
    still reports the discovered data and flags the discrepancy.
    """
    tools: bool = Field(default=False, description="Server supports tools")
    resources: bool = Field(default=False, description="Server supports resources")
    prompts: bool = Field(default=False, description="Server supports prompts")
    sampling: bool = Field(default=False, description="Server declares sampling in handshake")
    logging: bool = Field(default=False, description="Server supports logging")
    completions: bool = Field(default=False, description="Server supports argument completions")
    experimental: Optional[Dict[str, Any]] = Field(default=None, description="Experimental capabilities declared in handshake")
    resources_mismatch: bool = Field(default=False, description="Resources respond despite not being declared in handshake")
    prompts_mismatch: bool = Field(default=False, description="Prompts respond despite not being declared in handshake")


# --------------------------------------------------------
# Server attribute models (for detailed server profiling)
# --------------------------------------------------------

class ServerInfo(BaseModel):
    """Server info — real, externally-sourced identity. Never fabricated."""
    traffic_name: Optional[str] = Field(
        default=None,
        description="Traffic name from proxy (when configured) else the vendor's canonical name (server.json / registry / live handshake); null when no real source supplied one",
    )
    traffic_name_source: Optional[str] = Field(
        default=None,
        description="Which source produced traffic_name: proxy_map | proxy_header | server_json | registry | handshake",
    )


class DistributionType(BaseModel):
    """Distribution type classification."""
    official: bool = Field(default=False, description="Official MCP server")
    community: bool = Field(default=False, description="Community-built MCP server")

class ProtocolVersionProbe(BaseModel):
    """
    Result of offering one specific protocol version in a raw initialize request.

    `supported` is deliberately tri-state. False means the server answered and
    declined that version; None means the probe could not reach a verdict (timeout,
    TLS failure, unreadable reply) — reporting that as False would claim evidence
    the Inspector does not have.
    """
    version: str = Field(description="Protocol version offered in the initialize request")
    supported: Optional[bool] = Field(
        default=None,
        description="True if the server echoed this version, False if it declined, None if undetermined",
    )
    server_reported: Optional[str] = Field(
        default=None,
        description="Version string the server actually replied with (differs from `version` when it down-negotiated)",
    )
    status_code: Optional[int] = Field(default=None, description="HTTP status of the probe request")
    failure_kind: Optional[str] = Field(
        default=None,
        description="http_status | stream_closed | connect | read_timeout | tls | dns | unknown",
    )
    jsonrpc_error: Optional[Dict[str, Any]] = Field(
        default=None,
        description="JSON-RPC error object when the server rejected the version outright (e.g. code -32602)",
    )
    error: Optional[str] = Field(default=None, description="Single-line reason this version was not confirmed")


class MCPProtocolVersion(BaseModel):
    """
    MCP Protocol version support.

    The per-version booleans mean "confirmed supported": the Inspector offered that
    exact version in an initialize request and the server echoed it back, or the
    server volunteered it while down-negotiating. Versions that could not be
    confirmed stay False, and `probes` records why for each one.

    A version can also be marked from documentation/manifest evidence when no live
    probe was possible; `evidence_source` says which route was used.

    Use negotiated_version for what the live SDK session actually spoke,
    client_protocol_version for what the SDK offered, and latest_supported for the
    newest version the server was *proven* to accept. The first two come from the
    SDK handshake, which can only ever offer the SDK's own latest — which is why
    latest_supported needs the raw probe in
    src/utility/protocol_version_prober.py to be meaningful.
    """
    version_2024_11_05: bool = Field(default=False, description="Confirmed 2024-11-05 support")
    version_2025_03_26: bool = Field(default=False, description="Confirmed 2025-03-26 support")
    version_2025_06_18: bool = Field(default=False, description="Confirmed 2025-06-18 support")
    version_2025_11_25: bool = Field(default=False, description="Confirmed 2025-11-25 support")
    version_2026_07_28: bool = Field(default=False, description="Confirmed 2026-07-28 support")
    detected_version: Optional[str] = Field(default=None, description="Actual detected version string from protocol")
    negotiated_version: Optional[str] = Field(
        default=None,
        description="Version the server selected during the live MCP handshake (empty if no handshake succeeded)",
    )
    client_protocol_version: Optional[str] = Field(
        default=None,
        description="Version this Inspector offered in the initialize request (the installed MCP SDK's latest)",
    )
    latest_supported: Optional[str] = Field(
        default=None,
        description="Newest version the server was proven to accept, from the raw version probe",
    )
    supported_versions: List[str] = Field(
        default_factory=list,
        description="Every version confirmed by the probe, newest first",
    )
    era_evidence: Optional[str] = Field(
        default=None,
        description=(
            "Which protocol era answered the probe: 'server/discover answered' "
            "(modern, 2026-07-28+) or 'legacy initialize'"
        ),
    )
    probes: List[ProtocolVersionProbe] = Field(
        default_factory=list,
        description="Per-version probe results, including the reason each unconfirmed version failed",
    )
    probe_error: Optional[str] = Field(
        default=None,
        description="Why version probing produced no confirmed version at all (None when it worked)",
    )
    evidence_source: Optional[str] = Field(
        default=None,
        description="Where the version evidence came from: 'version probe', 'protocol handshake', 'documentation', or 'configuration'",
    )

class PricingModel(BaseModel):
    """Pricing model classification."""
    free: bool = Field(default=False, description="Free to use")
    paid: bool = Field(default=False, description="Paid/Commercial")

class HostingProvider(BaseModel):
    """Hosting provider classification."""
    saas_vendor: bool = Field(default=False, description="Hosted by SaaS vendor")
    third_party_saas: bool = Field(default=False, description="Hosted by 3rd party SaaS")
    github: bool = Field(default=False, description="Hosted on GitHub")
    gitlab: bool = Field(default=False, description="Hosted on GitLab")
    bitbucket: bool = Field(default=False, description="Hosted on Bitbucket")
    sourcehut_gitea_gogs: bool = Field(default=False, description="Hosted on SourceHut/Gitea/Gogs")

class AuthenticationMethods(BaseModel):
    """Authentication methods supported."""
    oauth2_1_authorization_code: bool = Field(default=False, description="OAuth 2.1 - Authorization Code Flow")
    oauth2_1_client_credentials: bool = Field(default=False, description="OAuth 2.1 - Client Credentials Flow")
    bearer_token: bool = Field(default=False, description="Bearer Token")
    personal_access_token: bool = Field(default=False, description="Personal Access Token")
    api_token: bool = Field(default=False, description="API Token")

class DataProtectionTLS(BaseModel):
    """Data protection through encryption with TLS."""
    tls_1_3: bool = Field(default=False, description="TLS 1.3")
    tls_1_2: bool = Field(default=False, description="TLS 1.2")
    lower_or_no_encryption: bool = Field(default=False, description="Lower versions or no encryption")

class TransportProtocol(BaseModel):
    """Transport protocol used."""
    stdio: bool = Field(default=False, description="STDIO")
    http_sse: bool = Field(default=False, description="HTTP/SSE")
    streamable_http: bool = Field(default=False, description="StreamableHttp")
    fast_api: bool = Field(default=False, description="FastAPI")

class ToolsOperationType(BaseModel):
    """Classification of tool operations."""
    read_only: bool = Field(default=False, description="Read-only operations tools")
    read_update: bool = Field(default=False, description="Has read-only and/or update operations tools")
    read_update_delete: bool = Field(default=False, description="Has read-only, update and/or delete operations tools")

class DeploymentApproach(BaseModel):
    """Deployment approach classification."""
    local: bool = Field(default=False, description="Local deployment")
    container: bool = Field(default=False, description="Container deployment")
    remote: bool = Field(default=False, description="Remote deployment")

class MCPServerAttributes(BaseModel):
    """Complete MCP server attributes for profiling."""
    server_info: ServerInfo = Field(default_factory=ServerInfo, description="Server info (traffic name)")
    distribution_type: DistributionType = Field(description="Distribution type classification")
    protocol_version: MCPProtocolVersion = Field(description="MCP protocol version")
    pricing: PricingModel = Field(description="Pricing model")
    hosting_provider: HostingProvider = Field(description="Hosting provider")
    authentication: AuthenticationMethods = Field(description="Authentication methods")
    data_protection: DataProtectionTLS = Field(description="Data protection with TLS")
    transport_protocol: TransportProtocol = Field(description="Transport protocol")
    tools_operation_type: ToolsOperationType = Field(description="Tools operation types")
    deployment_approach: DeploymentApproach = Field(description="Deployment approach")


# --------------------------------------------------------
# Structured error reporting (backend → frontend)
# --------------------------------------------------------

class ErrorSuggestion(BaseModel):
    """One actionable suggestion attached to a structured error."""
    text: str = Field(description="Human-readable suggestion")
    action: Optional[str] = Field(default=None, description="Machine hint: retry | oauth_start | fix_env | switch_transport")


class ErrorDetails(BaseModel):
    """Structured error returned by the API bridge for typed UI rendering."""
    # OAUTH_CORS_BLOCKED is deliberately absent: CORS is enforced in the browser,
    # so the backend never observes it. Browser-side "Failed to fetch" failures are
    # categorized client-side by ErrorDisplay's regex fallback, which has no
    # error_details to map.
    error_type: str = Field(description="OAUTH_REQUIRED | AUTH_INVALID_TOKEN | AUTH_FORBIDDEN | CONNECTION_TIMEOUT | DNS_FAILURE | TLS_ERROR | TRANSPORT_MISMATCH | ENV_VARS_MISSING | ENV_VARS_PLACEHOLDER | CONFIG_INVALID | SERVER_ERROR | UNKNOWN")
    title: str = Field(description="Short human-readable title")
    description: str = Field(description="What went wrong, in plain language")
    suggestions: List[ErrorSuggestion] = Field(default_factory=list, description="Actionable next steps")
    technical_detail: Optional[str] = Field(default=None, description="Raw error text for the collapsible details view")
    stage: str = Field(default="unknown", description="Pipeline stage: oauth | auth_discovery | mcp_discovery | llm_analysis | attribute_extraction | generate_report | config | unknown")
    status_code: Optional[int] = Field(default=None, description="HTTP status code when applicable")


# --------------------------------------------------------
# Analysis result models
# --------------------------------------------------------

class ToolCategoryAnalysis(BaseModel):
    """Analysis of tool categories."""
    category_name: str = Field(description="Category name (e.g., navigation, data, interaction)")
    tool_count: int = Field(description="Number of tools in this category")
    tools: List[str] = Field(description="List of tool names in this category")
    description: Optional[str] = Field(default=None, description="Category description")


class MCPAnalysisResult(BaseModel):
    """Results from LLM-powered analysis."""
    tool_categories: List[ToolCategoryAnalysis] = Field(description="Categorized tools")
    complexity_score: float = Field(description="Overall complexity score (0-10)")
    primary_use_case: Optional[str] = Field(default=None, description="Primary use case of the server")
    strengths: Optional[List[str]] = Field(default=None, description="Server strengths")
    limitations: Optional[List[str]] = Field(default=None, description="Server limitations")
    recommendations: Optional[List[str]] = Field(default=None, description="Usage recommendations")
    relationships: Optional[List[str]] = Field(default=None, description="Tool relationships and workflows")


# --------------------------------------------------------
# Testing result models
# --------------------------------------------------------

class MCPToolTestResult(BaseModel):
    """Result from testing a single tool."""
    tool_name: str = Field(description="Name of tested tool")
    test_status: str = Field(description="SUCCESS, FAILURE, or SKIPPED")
    test_input: Optional[Dict[str, Any]] = Field(default=None, description="Input used for testing")
    test_output: Optional[Any] = Field(default=None, description="Output received from tool")
    error_message: Optional[str] = Field(default=None, description="Error message if test failed")
    execution_time_ms: Optional[float] = Field(default=None, description="Execution time in milliseconds")


class MCPTestingReport(BaseModel):
    """Report from testing agent."""
    total_tools_tested: int = Field(description="Total number of tools tested")
    successful_tests: int = Field(description="Number of successful tests")
    failed_tests: int = Field(description="Number of failed tests")
    skipped_tests: int = Field(description="Number of skipped tests")
    test_results: List[MCPToolTestResult] = Field(description="Individual test results")


# --------------------------------------------------------
# Main inspection report model
# --------------------------------------------------------

class MCPServerInspectionReport(BaseModel):
    """Complete MCP server inspection report."""
    
    # Server metadata
    server_name: str = Field(description="MCP server name")
    server_version: Optional[str] = Field(default=None, description="Server version if available")
    connection_type: str = Field(description="Connection type: stdio, sse, websocket")
    discovery_timestamp: str = Field(description="ISO 8601 timestamp of discovery")
    
    # Server attributes (NEW)
    server_attributes: Optional[MCPServerAttributes] = Field(default=None, description="Detailed server attributes")
    
    # Capabilities
    capabilities: MCPCapabilities = Field(description="Server capabilities")
    
    # Discovered entities
    tools: List[MCPTool] = Field(default_factory=list, description="List of available tools")
    resources: List[MCPResource] = Field(default_factory=list, description="List of available resources")
    prompts: List[MCPPrompt] = Field(default_factory=list, description="List of available prompts")
    
    # Analysis results
    analysis: Optional[MCPAnalysisResult] = Field(default=None, description="LLM-powered analysis results")
    
    # Testing results (if testing was performed)
    testing_report: Optional[MCPTestingReport] = Field(default=None, description="Tool testing results")
    
    # Additional metadata
    metadata: Optional[Dict[str, Any]] = Field(default=None, description="Additional server metadata")
    
    # Statistics
    statistics: Optional[Dict[str, int]] = Field(
        default=None,
        description="Quick stats: total_tools, total_resources, total_prompts"
    )


# --------------------------------------------------------
# Discovery agent output (intermediate)
# --------------------------------------------------------

class MCPDiscoveryOutput(BaseModel):
    """Output from the discovery agent."""
    status: str = Field(description="SUCCESS or FAILURE")
    server_name: str = Field(description="Server name")
    connection_type: str = Field(description="Connection type")
    endpoint_url: Optional[str] = Field(default=None, description="Remote endpoint URL as given in input config")
    repository: Optional[str] = Field(default=None, description="GitHub repository URL as given in input config")
    capabilities: MCPCapabilities = Field(description="Detected capabilities")
    tools_discovered: List[Dict[str, Any]] = Field(description="Raw tools from list_tools()")
    resources_discovered: List[Dict[str, Any]] = Field(description="Raw resources from list_resources()")
    prompts_discovered: List[Dict[str, Any]] = Field(description="Raw prompts from list_prompts()")
    resource_templates_discovered: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="List of resource templates discovered"
    )
    error_message: Optional[str] = Field(default=None, description="Error message if discovery failed")


# --------------------------------------------------------
# Analysis agent output (intermediate)
# --------------------------------------------------------

class MCPAnalysisOutput(BaseModel):
    """Output from the analysis agent."""
    status: str = Field(description="SUCCESS or FAILURE")
    tools_analyzed: List[MCPTool] = Field(description="Analyzed and categorized tools")
    resources_analyzed: List[MCPResource] = Field(description="Analyzed resources")
    prompts_analyzed: List[MCPPrompt] = Field(description="Analyzed prompts")
    analysis_result: MCPAnalysisResult = Field(description="Analysis insights")
    error_message: Optional[str] = Field(default=None, description="Error message if analysis failed")

