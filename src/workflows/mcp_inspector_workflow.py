"""
MCP Inspector Workflow

Main orchestrator that runs the complete MCP server inspection pipeline
through a deterministic LangGraph StateGraph (src/graph/inspection_graph.py):

  load_config → auth_discovery → mcp_discovery → llm_analysis
              → attribute_extraction → generate_report

All routing is pure Python — no LLM in any control path. The public function
signatures are unchanged from the pre-graph implementation, so callers
(CLI, API bridge, scripts) work without modification.

Can be run as standalone script or imported as a module.
"""

import asyncio
import argparse
import sys
from pathlib import Path
from typing import Optional, List

from src.utility.utils import Utils

# Re-exported for backward compatibility — api_bridge/main.py and other callers
# import these names from this module.
from src.graph.report_builder import (
    build_final_report as _build_final_report_impl,
    print_inspection_summary as _print_inspection_summary,
)

# Global utils instance - will be recreated if custom config file is provided
utils = Utils()


class InspectionError(Exception):
    """Custom exception for inspection failures that should be handled gracefully."""
    pass


def _build_final_report(discovery_result: dict, analysis_result: dict, server_attributes=None) -> dict:
    """Backward-compatible wrapper around src.graph.report_builder.build_final_report."""
    return _build_final_report_impl(discovery_result, analysis_result, server_attributes)


async def run_mcp_inspection(
    server_name: Optional[str] = None,
    enable_testing: bool = False,
    output_dir: Optional[Path] = None
) -> Path:
    """
    Run complete MCP server inspection workflow via the LangGraph pipeline.

    Args:
        server_name: Name of server to inspect (from the config file)
        enable_testing: Whether to run testing agent (not yet implemented)
        output_dir: Optional output directory (default: timestamped under reports/)

    Returns:
        Path to generated inspection report

    Raises:
        InspectionError: When the pipeline halts before producing a report
                         (config failure or discovery failure). Skipped servers
                         (skip=1) also raise so multi-server runs can continue.
    """
    from src.graph import single_server_graph, initial_inspection_state

    utils.load_env()

    print("\n" + "="*80)
    print(" "*20 + "🔍 MCP SERVER INSPECTOR 🔍")
    print("="*80 + "\n")

    initial = initial_inspection_state(
        server_name=server_name,
        enable_testing=enable_testing,
        output_dir=output_dir,
    )

    final = await single_server_graph.ainvoke(initial)

    if final.get("phase_completed") == "skipped":
        raise InspectionError(f"Server '{final.get('server_name', server_name)}' skipped (skip=1)")

    report_path = final.get("report_path")
    if not report_path:
        errors = final.get("errors") or ["Unknown error"]
        raise InspectionError("; ".join(str(e) for e in errors))

    return Path(report_path)


async def run_multi_server_inspection(
    server_names: List[str],
    enable_testing: bool = False,
    output_dir: Optional[Path] = None
) -> Path:
    """
    Run inspection for multiple MCP servers in parallel (LangGraph Send fan-out)
    and aggregate results into a single CSV + HTML, matching the --all behavior.

    Args:
        server_names: List of server names to inspect
        enable_testing: Whether to run testing agent (not yet implemented)
        output_dir: Optional output directory

    Returns:
        Path to aggregated CSV file
    """
    from src.graph import multi_server_graph

    utils.load_env()

    print("\n" + "="*80)
    print(" "*20 + "🔍 MCP SERVER INSPECTOR (MULTI-SERVER) 🔍")
    print("="*80 + "\n")
    print(f"📋 Inspecting {len(server_names)} server(s): {', '.join(server_names)}\n")

    if output_dir is None:
        output_dir = utils.setup_output_dir("multi_server_inspection")
    else:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
    print(f"📁 Output directory: {output_dir}\n")

    initial = {
        "server_names": server_names,
        "enable_testing": enable_testing,
        "output_dir": output_dir,
        "completed_reports": [],
        "failed_servers": [],
    }

    final = await multi_server_graph.ainvoke(initial)

    if not final.get("completed_reports"):
        print("\n❌ No data collected from any server")
        sys.exit(1)

    failed = final.get("failed_servers") or []
    if failed:
        # A partially-failed run must not look like a clean one: the aggregated
        # CSV silently omits failed servers, so make the failure loud and exit
        # non-zero for scripts that gate on it.
        print(
            f"\n⚠️  {len(failed)} server(s) failed and are NOT in the aggregated "
            f"report: {', '.join(failed)}"
        )
        sys.exit(2)

    return output_dir / "aggregated_attribute_checklist.csv"


def main():
    """Main entry point for command-line usage."""
    global utils  # Allow updating global utils instance

    parser = argparse.ArgumentParser(
        description="MCP Server Inspector - Discover and analyze MCP server capabilities"
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to server configuration JSON file (default: examples/example_servers.json)"
    )
    parser.add_argument(
        "--server",
        type=str,
        default=None,
        help="Name of server(s) to inspect (from config file). Can be comma-separated for multiple servers (e.g., 'Server1,Server2'). If not provided, uses first server."
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Inspect all servers from config file (respects skip flag)"
    )
    parser.add_argument(
        "--enable-testing",
        action="store_true",
        help="Enable tool testing (experimental)"
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help="Output directory for all reports (default: creates timestamped directories in reports/)"
    )

    args = parser.parse_args()

    # Update utils with custom config file if provided
    if args.config:
        config_path = Path(args.config).resolve()
        if not config_path.exists():
            print(f"❌ Config file not found: {config_path}")
            sys.exit(1)
        utils = Utils(config_file=config_path)
        # The graph nodes resolve configs through their own Utils instance —
        # point it at the same custom config file.
        import src.graph.nodes as graph_nodes
        graph_nodes.utils = utils
        print(f"📁 Using config file: {config_path}")

    # Parse output directory if provided
    output_dir = None
    if args.output_dir:
        output_dir = Path(args.output_dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        print(f"📁 Using output directory: {output_dir}")

    # Handle --all flag
    if args.all:
        try:
            server_names = utils.get_all_server_names()
            print(f"📋 Found {len(server_names)} server(s) in configuration")
            asyncio.run(run_multi_server_inspection(
                server_names=server_names,
                enable_testing=args.enable_testing,
                output_dir=output_dir
            ))
        except Exception as e:
            print(f"❌ Failed to get all servers: {e}")
            sys.exit(1)
        return

    # Parse server names (comma-separated)
    server_names = []
    if args.server:
        server_names = [s.strip() for s in args.server.split(",") if s.strip()]

    # Run inspection
    try:
        if len(server_names) > 1:
            asyncio.run(run_multi_server_inspection(
                server_names=server_names,
                enable_testing=args.enable_testing,
                output_dir=output_dir
            ))
        elif len(server_names) == 1:
            asyncio.run(run_mcp_inspection(
                server_name=server_names[0],
                enable_testing=args.enable_testing,
                output_dir=output_dir
            ))
        else:
            asyncio.run(run_mcp_inspection(
                server_name=None,
                enable_testing=args.enable_testing,
                output_dir=output_dir
            ))
    except KeyboardInterrupt:
        print("\n\n⚠️  Inspection interrupted by user")
        sys.exit(130)
    except InspectionError as e:
        if "skip=1" in str(e):
            print(f"\n⏭️  {e}")
            sys.exit(0)
        print(f"\n\n❌ Inspection failed: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n\n❌ Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    import warnings
    # Suppress RuntimeWarning about module being found in sys.modules
    warnings.filterwarnings("ignore", category=RuntimeWarning, message=".*found in sys.modules.*")
    main()
