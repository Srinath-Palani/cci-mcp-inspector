"""
MCP Inspection StateGraph — deterministic, no LLM routing.

All edges are either fixed (add_edge) or conditioned on a pure-Python routing
function (add_conditional_edges). The topology is validated at compile time:
missing nodes or impossible routes raise immediately at import.

Two compiled graphs are exported:
- single_server_graph: the 6-phase inspection pipeline
- multi_server_graph: Send-based parallel fan-out over a server list,
  with an aggregation step matching the CLI --all behavior
"""

from __future__ import annotations

import re
from pathlib import Path

from langgraph.graph import StateGraph, START, END
from langgraph.types import Send

from src.state import InspectionState, MultiServerState
from src.graph.nodes import (
    load_config_node,
    auth_discovery_node,
    mcp_discovery_node,
    llm_analysis_node,
    attribute_extraction_node,
    generate_report_node,
    route_after_config,
    route_after_discovery,
)


# ─── Single-server graph ─────────────────────────────────────────────────────

def build_single_server_graph():
    """Build and compile the single-server inspection graph."""
    builder = StateGraph(InspectionState)

    builder.add_node("load_config", load_config_node)
    builder.add_node("auth_discovery", auth_discovery_node)
    builder.add_node("mcp_discovery", mcp_discovery_node)
    builder.add_node("llm_analysis", llm_analysis_node)
    builder.add_node("attribute_extraction", attribute_extraction_node)
    builder.add_node("generate_report", generate_report_node)

    builder.add_edge(START, "load_config")
    builder.add_conditional_edges(
        "load_config",
        route_after_config,
        {"auth_discovery": "auth_discovery", "end": END},
    )
    builder.add_edge("auth_discovery", "mcp_discovery")
    builder.add_conditional_edges(
        "mcp_discovery",
        route_after_discovery,
        {"llm_analysis": "llm_analysis", "end": END},
    )
    # Analysis failure is non-fatal — always proceed
    builder.add_edge("llm_analysis", "attribute_extraction")
    builder.add_edge("attribute_extraction", "generate_report")
    builder.add_edge("generate_report", END)

    return builder.compile()


single_server_graph = build_single_server_graph()


def initial_inspection_state(
    server_name=None,
    server_config=None,
    enable_testing=False,
    output_dir=None,
) -> InspectionState:
    """Build a fully-populated initial state for single_server_graph.ainvoke()."""
    return {
        "server_name": server_name,
        "server_config": server_config or {},
        "enable_testing": enable_testing,
        "output_dir": Path(output_dir) if output_dir else None,
        "auth_discovery_result": None,
        "discovery_result": None,
        "analysis_result": None,
        "server_attributes": None,
        "final_report": None,
        "report_path": None,
        "phase_completed": "",
        "errors": [],
        "all_phases_output": "",
    }


# ─── Multi-server graph ──────────────────────────────────────────────────────

# CLI multi-mode parity with the API batch runner (api_bridge/main.py):
# bounded fan-out and a per-server wall clock. Without the semaphore, every
# server starts at once (memory + rate-limit spike); without the timeout one
# hung server holds the aggregate hostage forever.
MULTI_SERVER_CONCURRENCY = 5
PER_SERVER_TIMEOUT_SECONDS = 600.0

# A fresh semaphore per multi-server invocation, created in _fan_out — which
# runs on the loop that is actually running. A module-level asyncio.Semaphore
# would bind to the first loop that used it and raise "bound to a different
# event loop" on the next asyncio.run() (each CLI multi-mode invocation runs
# on a fresh loop).
def _new_semaphore() -> "asyncio.Semaphore":
    import asyncio

    return asyncio.Semaphore(MULTI_SERVER_CONCURRENCY)


async def _inspect_one_server(state: dict) -> dict:
    """Worker node: run the single-server graph for one server."""
    import asyncio

    server_name = state["server_name"]
    try:
        init = initial_inspection_state(
            server_name=server_name,
            enable_testing=state.get("enable_testing", False),
            output_dir=state.get("output_dir"),
        )
        async with state["_semaphore"]:
            result = await asyncio.wait_for(
                single_server_graph.ainvoke(init),
                timeout=PER_SERVER_TIMEOUT_SECONDS,
            )
        if result.get("phase_completed") == "skipped":
            print(f"⏭️  Skipped '{server_name}' (skip=1)")
            return {}
        if result.get("final_report"):
            return {"completed_reports": [result["final_report"]]}
        errors = result.get("errors", [])
        print(f"❌ Server '{server_name}' failed: {'; '.join(str(e) for e in errors)}")
        return {"failed_servers": [server_name]}
    except asyncio.TimeoutError:
        print(f"❌ Server '{server_name}' timed out after {PER_SERVER_TIMEOUT_SECONDS:.0f}s")
        return {"failed_servers": [server_name]}
    except Exception as exc:
        print(f"❌ Unexpected error inspecting '{server_name}': {exc}")
        return {"failed_servers": [server_name]}


def _fan_out(state: MultiServerState) -> list:
    """Deterministic fan-out: one Send per server name, all sharing one semaphore."""
    output_dir = state.get("output_dir")
    semaphore = _new_semaphore()
    sends = []
    for name in state["server_names"]:
        safe_name = re.sub(r"[^\w\-_\.]", "_", name)
        server_dir = Path(output_dir) / safe_name if output_dir else None
        sends.append(Send("inspect_one_server", {
            "server_name": name,
            "enable_testing": state.get("enable_testing", False),
            "output_dir": server_dir,
            "_semaphore": semaphore,
        }))
    return sends


async def _aggregate_reports(state: MultiServerState) -> dict:
    """Fan-in: aggregate per-server reports into the combined CSV + HTML."""
    import csv as csv_mod
    import io

    from src.utility.attribute_report_generator import (
        AttributeReportGenerator,
        generate_aggregated_html,
    )

    reports = state.get("completed_reports") or []
    failed = state.get("failed_servers") or []
    output_dir = Path(state["output_dir"])
    server_names_ok = [r.get("server_name", "unknown") for r in reports]

    all_csv_data = []
    csv_header = None

    for report in reports:
        gen = AttributeReportGenerator(report)
        rows = list(csv_mod.reader(io.StringIO(gen.generate_csv())))
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
            for row in all_csv_data:
                writer.writerow(row)

        html_content = generate_aggregated_html(reports, server_names_ok)
        html_path = output_dir / "aggregated_attribute_checklist.html"
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html_content)

        print("\n" + "="*80)
        print("✅ MULTI-SERVER INSPECTION COMPLETED")
        print("="*80)
        print(f"\n📈 Aggregated CSV: {csv_path}")
        print(f"📊 Aggregated HTML: {html_path}")

    # Protocol summary across servers — negotiated version vs latest supported,
    # one row per server, so "which servers are behind the current spec" is
    # answerable from the aggregated output.
    _write_aggregated_protocol_csv(reports, output_dir)

    # Merged capabilities CSV across servers (flat format + server_name).
    _write_aggregated_capabilities_csv(reports, output_dir)

    print(f"📊 Total servers inspected: {len(reports)}/{len(reports) + len(failed)}\n")
    return {}


def _write_aggregated_protocol_csv(reports: list, output_dir) -> None:
    """
    Write all_servers_protocol.csv — one row per server with the negotiated
    protocol version, newest confirmed version, and which era answered
    (server/discover vs legacy initialize).
    """
    import csv as csv_mod

    from src.models.structured_output import SUPPORTED_PROTOCOL_VERSIONS

    latest_spec = SUPPORTED_PROTOCOL_VERSIONS[0]
    rows = []
    for report in reports:
        protocol = ((report.get("server_attributes") or {}).get("protocol_version")) or {}
        negotiated = protocol.get("negotiated_version") or protocol.get("detected_version") or ""
        latest_supported = protocol.get("latest_supported") or ""
        benchmark = latest_supported or negotiated
        if not benchmark:
            version_match = "unknown"
        elif benchmark == latest_spec:
            version_match = "current"
        else:
            version_match = "behind"
        walk = "; ".join(
            f"{p.get('version')}→{'accepted' if p.get('supported') else ('undetermined' if p.get('supported') is None else 'rejected')}"
            for p in (protocol.get("probes") or [])
        )
        transport_info = (report.get("metadata") or {}).get("transport_info") or {}
        rows.append([
            report.get("server_name", "unknown"),
            latest_spec,
            negotiated or "unknown",
            latest_supported,
            version_match,
            protocol.get("era_evidence") or "",
            walk,
            transport_info.get("resolved_transport") or report.get("connection_type") or "",
            transport_info.get("transport_status") or "",
            transport_info.get("resolved_url") or "",
        ])

    if not rows:
        return
    path = output_dir / "all_servers_protocol.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv_mod.writer(f)
        writer.writerow([
            "server_name", "latest_spec", "negotiated_protocol",
            "latest_supported", "version_match", "era_evidence",
            "negotiation_walk", "resolved_transport", "transport_status",
            "resolved_url",
        ])
        writer.writerows(rows)
    print(f"📡 Protocol CSV: {path}")


def _write_aggregated_capabilities_csv(reports: list, output_dir) -> None:
    """
    Write all_servers_capabilities.csv — the per-server mcp_capabilities.csv
    content merged with a leading server_name column.
    """
    import csv as csv_mod

    from src.utility.capability_csv_exporter import (
        _compact_json,
        _format_parameters,
        _strip_server_prefix,
    )
    from src.utility.tool_operation_classifier import classify_tool_actions

    rows = []
    for report in reports:
        sname = report.get("server_name", "unknown")
        tools = (
            (report.get("metadata") or {}).get("discovery_data", {}).get("tools_discovered")
            or report.get("tools")
            or []
        )
        for tool in tools:
            c = classify_tool_actions(tool)
            rows.append([
                sname,
                _strip_server_prefix(tool.get("name") or "(unnamed)", sname),
                (tool.get("description") or "NA").replace("\n", " "),
                _format_parameters(tool.get("inputSchema")),
                _compact_json(tool.get("inputSchema")),
                _compact_json(tool.get("outputSchema")),
                c["supports_read"],
                c["supports_write"],
                c["supports_delete"],
            ])

    if not rows:
        return
    path = output_dir / "all_servers_capabilities.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv_mod.writer(f, quoting=csv_mod.QUOTE_MINIMAL)
        writer.writerow([
            "server_name", "tool_name", "description", "parameters",
            "input_schema", "output_schema",
            "supports_read", "supports_write", "supports_delete",
        ])
        writer.writerows(rows)
    print(f"🧰 Capabilities CSV: {path}")


def build_multi_server_graph():
    """Build and compile the parallel multi-server inspection graph."""
    builder = StateGraph(MultiServerState)

    builder.add_node("inspect_one_server", _inspect_one_server)
    builder.add_node("aggregate_reports", _aggregate_reports)

    builder.add_conditional_edges(START, _fan_out, ["inspect_one_server"])
    builder.add_edge("inspect_one_server", "aggregate_reports")
    builder.add_edge("aggregate_reports", END)

    return builder.compile()


multi_server_graph = build_multi_server_graph()
