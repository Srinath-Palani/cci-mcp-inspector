"""
Typed state for the MCP inspection LangGraph pipeline.

Each node receives the full InspectionState and returns a dict with ONLY the
keys it mutates — LangGraph merges partial updates automatically. The `errors`
channel uses operator.add so any node can append without reading first.
"""

from __future__ import annotations

import operator
from pathlib import Path
from typing import Any, Annotated, List, Optional, TypedDict


class InspectionState(TypedDict, total=False):
    # ── inputs ────────────────────────────────────────────────────────────────
    server_name: Optional[str]        # None → first server in config file
    server_config: dict               # pre-built config (API bridge) or {} → load from file
    enable_testing: bool
    output_dir: Optional[Path]        # None → auto-created timestamped dir

    # ── phase outputs ─────────────────────────────────────────────────────────
    auth_discovery_result: Optional[dict]   # Phase 0
    discovery_result: Optional[dict]        # Phase 1
    analysis_result: Optional[dict]         # Phase 2
    server_attributes: Optional[Any]        # Phase 3 (MCPServerAttributes)
    final_report: Optional[dict]            # Phase 4
    report_path: Optional[Path]             # Phase 4

    # ── control / diagnostics ─────────────────────────────────────────────────
    phase_completed: str                    # last milestone reached
    errors: Annotated[List[str], operator.add]
    all_phases_output: str                  # accumulated console text


class MultiServerState(TypedDict, total=False):
    server_names: List[str]
    enable_testing: bool
    output_dir: Optional[Path]
    # operator.add merges the lists produced by parallel Send workers
    completed_reports: Annotated[List[dict], operator.add]
    failed_servers: Annotated[List[str], operator.add]
