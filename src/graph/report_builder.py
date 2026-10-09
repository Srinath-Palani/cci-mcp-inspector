"""
Final report assembly for the MCP Inspector.

Moved out of mcp_inspector_workflow.py so both the CLI workflow and the
LangGraph report node can import it without circular imports.
"""

from __future__ import annotations

from typing import Any, Optional

from src.utility.utils import Utils

utils = Utils()


def build_final_report(
    discovery_result: dict,
    analysis_result: dict,
    server_attributes: Any = None,
    auth_discovery_result: Optional[dict] = None,
) -> dict:
    """
    Build final inspection report combining discovery, analysis, and attributes.
    """
    from src.models.structured_output import MCPCapabilities, MCPServerInspectionReport

    server_name = discovery_result.get("server_name", "unknown")
    connection_type = discovery_result.get("connection_type", "unknown")
    capabilities_dict = discovery_result.get("capabilities", {})

    tools_analyzed = analysis_result.get("tools_analyzed", [])
    resources_analyzed = analysis_result.get("resources_analyzed", [])
    prompts_analyzed = analysis_result.get("prompts_analyzed", [])
    analysis_insights = analysis_result.get("analysis_result")

    capabilities = MCPCapabilities(**capabilities_dict)

    statistics = utils.calculate_statistics(
        tools_analyzed,
        resources_analyzed,
        prompts_analyzed
    )

    metadata = {
        "inspector_version": "1.0.0",
        "discovery_status": discovery_result.get("status"),
        "analysis_status": analysis_result.get("status"),
        # Input provenance — whichever locator was given in the config
        # (remote endpoint / GitHub repo), null when not provided.
        "server_input": {
            "endpoint_url": discovery_result.get("endpoint_url"),
            "repository": discovery_result.get("repository"),
        },
        # Transport status — whether the server still relies on deprecated
        # HTTP/SSE (/sse) or has moved to Streamable HTTP (/mcp).
        "transport_info": discovery_result.get("transport_info"),
        "discovery_data": {
            "tools_discovered": discovery_result.get("tools_discovered", []),
            "resources_discovered": discovery_result.get("resources_discovered", []),
            "prompts_discovered": discovery_result.get("prompts_discovered", [])
        }
    }
    if auth_discovery_result:
        metadata["auth_discovery"] = auth_discovery_result

    report = MCPServerInspectionReport(
        server_name=server_name,
        server_version=None,
        connection_type=connection_type,
        discovery_timestamp=utils.get_timestamp(),
        server_attributes=server_attributes,
        capabilities=capabilities,
        tools=tools_analyzed,
        resources=resources_analyzed,
        prompts=prompts_analyzed,
        analysis=analysis_insights,
        testing_report=None,
        metadata=metadata,
        statistics=statistics
    )

    report_dict = report.model_dump()

    # Merge inputSchema from raw discovery back into each tool so the frontend
    # can render the full parameter schema (MCPTool.parameters only has parsed fields).
    # Annotations and outputSchema are merged the same way — the analyzed MCPTool
    # model drops both, but reports and the capabilities CSV need them.
    raw_tools = {
        t.get("name"): t
        for t in discovery_result.get("tools_discovered", [])
        if t.get("name")
    }
    if raw_tools:
        for tool in report_dict.get("tools", []):
            raw = raw_tools.get(tool.get("name"))
            if not raw:
                continue
            if raw.get("inputSchema"):
                tool["inputSchema"] = raw["inputSchema"]
            if raw.get("annotations"):
                tool["annotations"] = raw["annotations"]
            if raw.get("outputSchema"):
                tool["outputSchema"] = raw["outputSchema"]

    return report_dict


def print_inspection_summary(report: dict) -> None:
    """Print a human-readable summary of the inspection."""

    print("\n" + "="*80)
    print("📊 INSPECTION SUMMARY")
    print("="*80 + "\n")

    print(f"🏷️  Server Name: {report.get('server_name')}")
    print(f"🔌 Connection Type: {report.get('connection_type')}")
    print(f"📅 Timestamp: {report.get('discovery_timestamp')}")

    # Transport status — deprecated /sse vs current /mcp
    transport_info = (report.get('metadata') or {}).get('transport_info') or {}
    if transport_info:
        t_status = transport_info.get('transport_status', '')
        if t_status == 'deprecated-sse':
            print(f"⚠️  Transport: HTTP/SSE (DEPRECATED — server has not moved to Streamable HTTP /mcp)")
            if transport_info.get('transport_changed'):
                print(f"   Given: {transport_info.get('given_url')} ({transport_info.get('given_transport')})")
                print(f"   Resolved: {transport_info.get('resolved_url')} ({transport_info.get('resolved_transport')})")
        elif t_status:
            print(f"🚀 Transport: Streamable HTTP (current)")

    # Server Attributes
    attributes = report.get('server_attributes')
    if attributes:
        print(f"\n🏷️  Server Attributes:")

        dist_type = attributes.get('distribution_type', {})
        if dist_type.get('official'):
            print(f"   • Distribution: Official")
        elif dist_type.get('community'):
            print(f"   • Distribution: Community")

        pricing = attributes.get('pricing', {})
        if pricing.get('free'):
            print(f"   • Pricing: Free")
        elif pricing.get('paid'):
            print(f"   • Pricing: Paid")

        transport = attributes.get('transport_protocol', {})
        if transport.get('stdio'):
            print(f"   • Transport: STDIO")
        elif transport.get('http_sse'):
            print(f"   • Transport: HTTP/SSE")

        tools_op = attributes.get('tools_operation_type', {})
        if tools_op.get('read_only'):
            print(f"   • Operations: Read-only")
        elif tools_op.get('read_update'):
            print(f"   • Operations: Read + Update")
        elif tools_op.get('read_update_delete'):
            print(f"   • Operations: Read + Update + Delete")

        deployment = attributes.get('deployment_approach', {})
        deploy_types = []
        if deployment.get('local'):
            deploy_types.append("Local")
        if deployment.get('container'):
            deploy_types.append("Container")
        if deployment.get('remote'):
            deploy_types.append("Remote")
        if deploy_types:
            print(f"   • Deployment: {', '.join(deploy_types)}")

        auth = attributes.get('authentication', {})
        auth_methods = []
        if auth.get('oauth2_1_authorization_code'):
            auth_methods.append("OAuth 2.1 Auth Code")
        if auth.get('oauth2_1_client_credentials'):
            auth_methods.append("OAuth 2.1 Client Creds")
        if auth.get('bearer_token'):
            auth_methods.append("Bearer Token")
        if auth.get('personal_access_token'):
            auth_methods.append("Personal Access Token")
        if auth.get('api_token'):
            auth_methods.append("API Token")
        if auth_methods:
            print(f"   • Authentication: {', '.join(auth_methods)}")

        protocol = attributes.get('protocol_version', {})
        detected_version = protocol.get('detected_version')
        if detected_version and detected_version != 'unknown':
            print(f"   • MCP Protocol: {detected_version}")

    # Capabilities
    capabilities = report.get('capabilities', {})
    print(f"\n🎯 Capabilities:")
    print(f"   • Tools: {'✅' if capabilities.get('tools') else '❌'}")
    print(f"   • Resources: {'✅' if capabilities.get('resources') else '❌'}{' (undeclared — mismatch)' if capabilities.get('resources_mismatch') else ''}")
    print(f"   • Prompts: {'✅' if capabilities.get('prompts') else '❌'}{' (undeclared — mismatch)' if capabilities.get('prompts_mismatch') else ''}")
    print(f"   • Sampling: {'✅' if capabilities.get('sampling') else '❌'}")
    print(f"   • Logging: {'✅' if capabilities.get('logging') else '❌'}")
    print(f"   • Completions: {'✅' if capabilities.get('completions') else '❌'}")

    # Statistics
    stats = report.get('statistics', {})
    print(f"\n📈 Statistics:")
    print(f"   • Total Tools: {stats.get('total_tools', 0)}")
    print(f"   • Total Resources: {stats.get('total_resources', 0)}")
    print(f"   • Total Prompts: {stats.get('total_prompts', 0)}")

    # Analysis insights
    analysis = report.get('analysis')
    if analysis:
        print(f"\n🧠 Analysis Insights:")
        print(f"   • Complexity Score: {analysis.get('complexity_score', 0)}/10")
        print(f"   • Primary Use Case: {analysis.get('primary_use_case', 'Unknown')}")

        categories = analysis.get('tool_categories', [])
        if categories:
            print(f"   • Tool Categories: {len(categories)}")
            for cat in categories[:5]:
                print(f"      - {cat.get('category_name')}: {cat.get('tool_count')} tools")

        strengths = analysis.get('strengths', [])
        if strengths:
            print(f"   • Key Strengths: {len(strengths)}")
            for strength in strengths[:3]:
                print(f"      - {strength}")

    print("\n" + "="*80)
