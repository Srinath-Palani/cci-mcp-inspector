"""
Capability CSV Exporter

Generates mcp_capabilities.csv in a flat tools-only format:

  tool_name,description,parameters,input_schema,output_schema,
  supports_read,supports_write,supports_delete
  
- One row per tool; resources/prompts are not in this file (they remain in the
  JSON report).
- ``description`` is verbatim from the server, ``NA`` when absent.
- ``parameters`` is comma-separated ``name:type`` with ``*`` marking required.
- Schemas are compact JSON, ``""`` when absent.
- supports_read/write/delete come from the two-tier classifier (annotation
  hints first, name+description fallback). The annotation/heuristic split is
  reported on the console and in the JSON report, NOT in this CSV — the file
  carries the verdict only.

Spec reference for the annotation semantics:
https://modelcontextprotocol.io/specification/2026-07-28/server/tools
"""

import csv
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.utility.tool_operation_classifier import classify_tool_actions


def _format_parameters(input_schema: Optional[Dict[str, Any]]) -> str:
    """
    inputSchema -> "name:type, other:type*" (``*`` = required), matching the
    parameters column format. Returns "NA" when no parameters are declared.
    """
    if not input_schema or not isinstance(input_schema, dict):
        return "NA"

    props: Dict[str, Any] = input_schema.get("properties") or {}
    required = set(input_schema.get("required") or [])

    parts: List[str] = []
    for param_name, meta in props.items():
        param_type = meta.get("type", "any") if isinstance(meta, dict) else "any"
        if param_type == "array" and isinstance(meta, dict):
            items_type = (meta.get("items") or {}).get("type", "any")
            param_type = f"array[{items_type}]"
        suffix = "*" if param_name in required else ""
        parts.append(f"{param_name}:{param_type}{suffix}")

    return ", ".join(parts) if parts else "NA"


def _compact_json(schema: Optional[Dict[str, Any]]) -> str:
    """Compact JSON for a schema column; empty string when absent."""
    if not schema or not isinstance(schema, dict):
        return ""
    return json.dumps(schema, separators=(",", ":"), ensure_ascii=False)


def _strip_server_prefix(tool_name: str, server_name: str) -> str:
    """
    Drop a redundant server-name prefix from a tool name: ``tavily_search``
    for the server "tavily" reports as ``search``. Servers that prefix every
    tool with their own name make the column noisier without adding
    information — the server is already identified by the file/download.

    Only an exact ``<server>_`` prefix is stripped (case-insensitive, with
    non-alphanumerics in the server name normalised to ``_``), so a tool like
    ``research`` on the server "tavily_researcher" or ``search_images`` on
    "tavily" keeps its name.
    """
    if not tool_name or not server_name:
        return tool_name
    prefix = re.sub(r"[^a-z0-9]+", "_", server_name.strip().lower()).strip("_")
    name = tool_name.strip()
    normalised = re.sub(r"[^a-z0-9]+", "_", name.lower())
    if prefix and normalised.startswith(prefix + "_"):
        return name[len(prefix) + 1:]
    return name


# ── main exporter ───────────────────────────────────────────────────────────────

class CapabilityCSVExporter:
    """
    Builds and saves mcp_capabilities.csv (flat, tools only) from a
    discovery result dict and the original server config.
    """

    COLUMNS = [
        "tool_name",
        "description",
        "parameters",
        "input_schema",
        "output_schema",
        "supports_read",
        "supports_write",
        "supports_delete",
    ]

    def __init__(self, discovery_result: Dict[str, Any], server_config: Dict[str, Any]):
        self.discovery = discovery_result
        self.config = server_config

    # ── row builders ───────────────────────────────────────────────────────────

    def build_rows(self) -> List[Dict[str, str]]:
        rows: List[Dict[str, str]] = []

        for tool in self.discovery.get("tools_discovered") or []:
            classification = classify_tool_actions(tool)
            rows.append({
                "tool_name": _strip_server_prefix(
                    tool.get("name", ""), self.config.get("name", "")
                ),
                "description": tool.get("description") or "NA",
                "parameters": _format_parameters(tool.get("inputSchema")),
                "input_schema": _compact_json(tool.get("inputSchema")),
                "output_schema": _compact_json(tool.get("outputSchema")),
                "supports_read": classification["supports_read"],
                "supports_write": classification["supports_write"],
                "supports_delete": classification["supports_delete"],
            })

        return rows

    # ── save ───────────────────────────────────────────────────────────────────

    def save(self, output_dir: Path) -> Path:
        rows = self.build_rows()
        path = Path(output_dir) / "mcp_capabilities.csv"

        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=self.COLUMNS, quoting=csv.QUOTE_MINIMAL)
            writer.writeheader()
            writer.writerows(rows)

        print(f"📊 Capability CSV saved: {path} ({len(rows)} rows)")
        return path
