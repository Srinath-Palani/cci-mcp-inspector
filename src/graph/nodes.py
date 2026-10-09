"""
LangGraph nodes for the MCP Server Inspector.

Each node wraps an existing agent function without modifying it. Nodes return
a PARTIAL state dict — only the keys they update — and LangGraph merges the
update into the full InspectionState.

Routing functions at the bottom are pure Python: they read the state and
return a string key. No LLM is involved in routing.

Progress reporting: nodes read an optional `phase_callback(phase, progress)`
from config["configurable"] (the API bridge job manager passes one; the CLI
passes none). asyncio.CancelledError is always re-raised so job cancellation
propagates cleanly through the MCP session context managers.
"""

from __future__ import annotations

import asyncio
import io
import traceback
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, Callable, Optional

from langchain_core.runnables import RunnableConfig

from src.state import InspectionState
from src.utility.utils import Utils

utils = Utils()

PhaseCallback = Callable[[str, float], None]


def _callback(config: Optional[RunnableConfig]) -> PhaseCallback:
    """Extract the phase callback from a RunnableConfig, defaulting to no-op."""
    if config:
        cb = (config.get("configurable") or {}).get("phase_callback")
        if callable(cb):
            return cb
    return lambda phase, progress: None


# ═══════════════════════════════════════════════════════════════════════════════
# NODE 0 — load_config
# ═══════════════════════════════════════════════════════════════════════════════
async def load_config_node(state: InspectionState, config: Optional[RunnableConfig] = None) -> dict:
    """
    Resolve the server config and create the output directory.

    Two entry modes:
    - CLI: state["server_config"] is empty → load by name from the config file.
    - API bridge: state["server_config"] is already a full dict → use directly.
    """
    _callback(config)("load_config", 0.05)
    utils.load_env()

    try:
        server_config = state.get("server_config") or {}
        if not server_config:
            server_config = utils.load_server_config(state.get("server_name"))
        server_name = server_config.get("name", "unknown")

        if server_config.get("skip", 0) == 1:
            print(f"⏭️  Skipping server '{server_name}' (skip=1)")
            print("   To run this server, set 'skip' to 0 in the configuration file.")
            return {
                "server_config": server_config,
                "server_name": server_name,
                "phase_completed": "skipped",
            }

        output_dir = state.get("output_dir")
        if output_dir is None:
            output_dir = utils.setup_output_dir(server_name)
        else:
            output_dir = Path(output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)

        print(f"📋 Loaded configuration for: {server_name}")
        print(f"📁 Output directory: {output_dir}\n")

        return {
            "server_config": server_config,
            "server_name": server_name,
            "output_dir": output_dir,
            "phase_completed": "config_loaded",
        }

    except asyncio.CancelledError:
        raise
    except Exception as exc:
        print(f"❌ Failed to load server configuration: {exc}")
        return {
            "phase_completed": "config_failed",
            "errors": [f"Config load failed: {exc}"],
        }


# ═══════════════════════════════════════════════════════════════════════════════
# NODE 1 — auth_discovery  (Phase 0, non-fatal)
# ═══════════════════════════════════════════════════════════════════════════════
async def auth_discovery_node(state: InspectionState, config: Optional[RunnableConfig] = None) -> dict:
    """Phase 0: Authentication Discovery. Errors are recorded and the graph continues."""
    _callback(config)("auth_discovery", 0.15)

    server_config = state["server_config"]
    endpoint_url = server_config.get("endpoint_url")
    repository_url = server_config.get("repository")

    if not endpoint_url and not repository_url:
        return {"auth_discovery_result": None, "phase_completed": "auth_skipped"}

    header = "="*80 + "\nPHASE 0: AUTHENTICATION DISCOVERY\n" + "="*80
    print(header)

    try:
        from src.utility.auth_discovery import (
            AuthenticationDiscovery,
            print_auth_discovery_summary,
        )

        phase_buf = io.StringIO()
        with redirect_stdout(phase_buf):
            discovery = AuthenticationDiscovery()
            result = await discovery.discover_auth_requirements(
                endpoint_url=endpoint_url,
                current_config=server_config.get("authentication"),
                repository_url=repository_url,
            )
            print_auth_discovery_summary(result)

            if result.get("discovered") and server_config.get("authentication"):
                validation = discovery.validate_config_against_discovered(
                    server_config["authentication"]
                )
                if validation.get("errors"):
                    print("\n⚠️  Configuration validation errors:")
                    for error in validation["errors"]:
                        print(f"   • {error}")
                if validation.get("warnings"):
                    print("\n⚠️  Configuration warnings:")
                    for warning in validation["warnings"]:
                        print(f"   • {warning}")
            print()

        captured = phase_buf.getvalue()
        print(captured, end="")

        return {
            "auth_discovery_result": result,
            "all_phases_output": state.get("all_phases_output", "") + header + "\n\n" + captured,
            "phase_completed": "auth_done",
        }

    except asyncio.CancelledError:
        raise
    except Exception as exc:
        msg = f"⚠️  Authentication discovery failed: {exc}\n   Continuing with configured authentication..."
        print(msg)
        print(f"   Debug: {traceback.format_exc()}")
        return {
            "auth_discovery_result": None,
            "all_phases_output": state.get("all_phases_output", "") + header + "\n\n" + msg + "\n",
            "phase_completed": "auth_failed",
            "errors": [msg.strip()],
        }


# ═══════════════════════════════════════════════════════════════════════════════
# NODE 2 — mcp_discovery  (Phase 1, fatal on failure)
# ═══════════════════════════════════════════════════════════════════════════════
async def mcp_discovery_node(state: InspectionState, config: Optional[RunnableConfig] = None) -> dict:
    """Phase 1: MCP Discovery. status != SUCCESS routes the graph to END."""
    _callback(config)("mcp_discovery", 0.35)

    header = "\n" + "="*80 + "\nPHASE 1: DISCOVERY\n" + "="*80
    print(header)

    from src.agents.mcp_discovery_agent import mcp_discovery_agent

    try:
        phase_buf = io.StringIO()
        with redirect_stdout(phase_buf):
            result = await mcp_discovery_agent(state["server_config"])

        captured = phase_buf.getvalue()
        print(captured, end="")
        all_output = state.get("all_phases_output", "") + header + "\n\n" + captured

        if result.get("status") != "SUCCESS":
            error_msg = result.get("error_message", "Unknown error")
            print(f"❌ Discovery failed: {error_msg}")
            return {
                "discovery_result": result,
                "all_phases_output": all_output + f"❌ Discovery failed: {error_msg}\n",
                "phase_completed": "discovery_failed",
                "errors": [f"Discovery failed: {error_msg}"],
            }

        utils.save_report(result, Path(state["output_dir"]), "discovery_raw.json")

        return {
            "discovery_result": result,
            "all_phases_output": all_output,
            "phase_completed": "discovery_done",
        }

    except asyncio.CancelledError:
        raise
    except Exception as exc:
        msg = f"❌ Discovery phase failed: {exc}"
        print(msg)
        return {
            "discovery_result": {"status": "FAILURE", "error_message": str(exc)},
            "all_phases_output": state.get("all_phases_output", "") + header + "\n\n" + msg + "\n",
            "phase_completed": "discovery_failed",
            "errors": [msg],
        }


# ═══════════════════════════════════════════════════════════════════════════════
# NODE 3 — llm_analysis  (Phase 2, non-fatal)
# ═══════════════════════════════════════════════════════════════════════════════
async def llm_analysis_node(state: InspectionState, config: Optional[RunnableConfig] = None) -> dict:
    """Phase 2: LLM Analysis. Failure produces a fallback result and continues."""
    _callback(config)("llm_analysis", 0.55)

    header = "\n" + "="*80 + "\nPHASE 2: ANALYSIS\n" + "="*80
    print(header)

    from src.agents.mcp_analysis_agent import mcp_analysis_agent

    try:
        phase_buf = io.StringIO()
        with redirect_stdout(phase_buf):
            result = await mcp_analysis_agent(state["discovery_result"])

        captured = phase_buf.getvalue()
        print(captured, end="")
        all_output = state.get("all_phases_output", "") + header + "\n\n" + captured

        errors = []
        if result.get("status") != "SUCCESS":
            err = result.get("error_message", "Unknown error")
            warning = f"⚠️  Analysis completed with warnings: {err}"
            print(warning)
            all_output += warning + "\n"
            errors = [f"Analysis warning: {err}"]

        # Input provenance — raw JSON reports carry whichever locator was
        # given in the config (remote endpoint / GitHub repo).
        server_config = state["server_config"]
        result["endpoint_url"] = server_config.get("endpoint_url") or server_config.get("url")
        result["repository"] = server_config.get("repository")

        utils.save_report(result, Path(state["output_dir"]), "analysis_raw.json")

        return {
            "analysis_result": result,
            "all_phases_output": all_output,
            "phase_completed": "analysis_done",
            "errors": errors,
        }

    except asyncio.CancelledError:
        raise
    except Exception as exc:
        msg = f"❌ Analysis phase failed: {exc}"
        print(msg)
        fallback = {
            "status": "FAILURE",
            "tools_analyzed": [],
            "resources_analyzed": [],
            "prompts_analyzed": [],
            "analysis_result": None,
            "error_message": str(exc),
        }
        return {
            "analysis_result": fallback,
            "all_phases_output": state.get("all_phases_output", "") + header + "\n\n" + msg + "\n",
            "phase_completed": "analysis_failed",
            "errors": [msg],
        }


# ═══════════════════════════════════════════════════════════════════════════════
# NODE 4 — attribute_extraction  (Phase 3, non-fatal)
# ═══════════════════════════════════════════════════════════════════════════════
async def attribute_extraction_node(state: InspectionState, config: Optional[RunnableConfig] = None) -> dict:
    """Phase 3: Attribute Extraction. Failure yields a report without attributes."""
    _callback(config)("attribute_extraction", 0.75)

    header = "\n" + "="*80 + "\nPHASE 3: ATTRIBUTE EXTRACTION\n" + "="*80
    print(header)

    from src.agents.mcp_attribute_extraction_agent import extract_server_attributes

    try:
        phase_buf = io.StringIO()
        with redirect_stdout(phase_buf):
            server_attributes = await extract_server_attributes(
                state["server_config"],
                state["discovery_result"],
                state["analysis_result"],
                state.get("auth_discovery_result"),
            )

        captured = phase_buf.getvalue()
        print(captured, end="")

        return {
            "server_attributes": server_attributes,
            "all_phases_output": state.get("all_phases_output", "") + header + "\n\n" + captured,
            "phase_completed": "attributes_done",
        }

    except asyncio.CancelledError:
        raise
    except Exception as exc:
        msg = f"⚠️  Attribute extraction failed: {exc}"
        print(msg)
        return {
            "server_attributes": None,
            "all_phases_output": state.get("all_phases_output", "") + header + "\n\n" + msg + "\n",
            "phase_completed": "attributes_failed",
            "errors": [msg],
        }


# ═══════════════════════════════════════════════════════════════════════════════
# NODE 5 — generate_report  (Phase 4)
# ═══════════════════════════════════════════════════════════════════════════════
async def generate_report_node(state: InspectionState, config: Optional[RunnableConfig] = None) -> dict:
    """Phase 4: Report Generation — final report JSON + all derived formats."""
    _callback(config)("generate_report", 0.9)

    header = "\n" + "="*80 + "\nPHASE 4: REPORT GENERATION\n" + "="*80
    print(header + "\n")

    from src.graph.report_builder import build_final_report, print_inspection_summary
    from src.utility.attribute_report_generator import AttributeReportGenerator

    try:
        output_dir = Path(state["output_dir"])

        phase_buf = io.StringIO()
        with redirect_stdout(phase_buf):
            final_report = build_final_report(
                state["discovery_result"],
                state["analysis_result"],
                state.get("server_attributes"),
                auth_discovery_result=state.get("auth_discovery_result"),
            )

            report_path = utils.save_report(final_report, output_dir, "inspection_report.json")
            print_inspection_summary(final_report)

            try:
                print("\n🎨 Generating attribute reports...")
                AttributeReportGenerator(final_report).save_all_formats(output_dir)
            except Exception as exc:
                print(f"⚠️  Could not generate attribute reports: {exc}")

            try:
                from src.utility.capability_csv_exporter import CapabilityCSVExporter
                CapabilityCSVExporter(state["discovery_result"], state["server_config"]).save(output_dir)
            except Exception as exc:
                print(f"⚠️  Capability CSV skipped: {exc}")

        captured = phase_buf.getvalue()
        print(captured, end="")
        all_output = state.get("all_phases_output", "") + header + "\n\n" + captured

        completion = (
            "\n" + "="*80 + "\n"
            "✅ INSPECTION COMPLETED SUCCESSFULLY\n"
            + "="*80 + "\n"
            + f"\n📄 Full report: {report_path}\n"
            + f"📋 Attribute checklist: {output_dir / 'attribute_checklist.md'}\n"
            + f"📊 HTML report: {output_dir / 'attribute_report.html'}\n"
            + f"📈 CSV export: {output_dir / 'attribute_checklist.csv'}\n"
            + f"📊 Capability CSV: {output_dir / 'mcp_capabilities.csv'}\n"
            + f"📝 Raw output (all phases): {output_dir / 'attribute_extraction_raw.txt'}\n"
        )
        print(completion)
        all_output += completion

        # Persist the combined console output of every phase
        text_report_path = output_dir / "attribute_extraction_raw.txt"
        with open(text_report_path, "w", encoding="utf-8") as fh:
            fh.write(all_output)

        _callback(config)("done", 1.0)

        return {
            "final_report": final_report,
            "report_path": report_path,
            "all_phases_output": all_output,
            "phase_completed": "report_done",
        }

    except asyncio.CancelledError:
        raise
    except Exception as exc:
        msg = f"❌ Report generation failed: {exc}"
        print(msg)
        return {
            "phase_completed": "report_failed",
            "errors": [msg],
        }


# ═══════════════════════════════════════════════════════════════════════════════
# ROUTING FUNCTIONS — pure Python, deterministic, no LLM
# ═══════════════════════════════════════════════════════════════════════════════

def route_after_config(state: InspectionState) -> str:
    """After load_config: proceed or halt (config failure / skip flag)."""
    if state.get("phase_completed") in ("config_failed", "skipped"):
        return "end"
    return "auth_discovery"


def route_after_discovery(state: InspectionState) -> str:
    """After mcp_discovery: halt on failure, analyze on success."""
    result = state.get("discovery_result") or {}
    if result.get("status") != "SUCCESS":
        return "end"
    return "llm_analysis"
