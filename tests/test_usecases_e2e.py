"""
End-to-end use-case tests against a REAL local MCP server (FastMCP over
Streamable HTTP), covering the paths the unit tests mock:

  1. CLI single-server mode: full graph run, report + all CSVs on disk
  2. Capabilities CSV content: pagination across pages, annotation-driven
     R/W/D, heuristic fallback, exact skill header
  3. CLI multi-server mode: aggregated attribute CSV, protocol CSV,
     merged capabilities CSV, per-server directories
  4. Multi-mode failure accounting: one good + one dead server -> exit 2

The server exposes 3 tools (one readOnly-annotated, one destructive-annotated,
one unannotated delete-named) and paginates tools/list across 2 pages.
"""

import asyncio
import csv
import json
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import uvicorn
from mcp.server.fastmcp import FastMCP

ok = fail = 0


def check(label, cond, extra=""):
    global ok, fail
    if cond:
        print(f"  ✓ {label}")
        ok += 1
    else:
        print(f"  ✗ {label} {extra}")
        fail += 1


# ── a real MCP server, paginating tools/list across two pages ────────────────

server = FastMCP(name="e2e-test-server")


@server.tool(annotations={"readOnlyHint": True, "destructiveHint": False,
                          "idempotentHint": True, "openWorldHint": False})
def get_status() -> str:
    """Read the current server status."""
    return "ok"


@server.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
def reset_state(confirm: bool) -> str:
    """Reset all server state."""
    return "reset"


@server.tool()
def delete_temp_files(older_than_days: int = 7) -> str:
    """Delete temporary files older than N days."""
    return "deleted"


# Force pagination: page size 2 -> tools arrive in 2 pages (3 tools total).
from mcp.server.fastmcp.server import FastMCP as _FM  # noqa


def _patch_pagination():
    """Wrap the low-level list_tools handler to paginate 2 items per page."""
    low = server._mcp_server
    original = None
    for handler in low.request_handlers.values():
        pass
    # Patch at the server level: wrap list_tools on the low-level server.
    import mcp.types as types

    orig = low.request_handlers[types.ListToolsRequest]

    async def paginated(req):
        result = await orig(req)
        tools = result.root.tools if hasattr(result, "root") else result.tools
        cursor = req.params.cursor if req.params else None
        page = 0 if cursor is None else int(cursor)
        chunk = tools[page * 2:(page + 1) * 2]
        next_cursor = str(page + 1) if (page + 1) * 2 < len(tools) else None
        result.root.tools = chunk
        result.root.nextCursor = next_cursor
        return result

    low.request_handlers[types.ListToolsRequest] = paginated


_patch_pagination()


class RunningServer:
    def __init__(self, app, port):
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self.port = port

    def __enter__(self):
        self._thread.start()
        import time
        for _ in range(100):
            if self._server.started:
                break
            time.sleep(0.05)
        return self

    def __exit__(self, *a):
        self._server.should_exit = True
        self._thread.join(timeout=5)


def _free_port():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _write_config(path: Path, name: str, url: str):
    path.write_text(json.dumps({"servers": [{
        "name": name,
        "connection_type": "streamable_http",
        "endpoint_url": url,
        "description": "e2e test server",
    }]}))


print("e2e: real FastMCP server over Streamable HTTP")
port = _free_port()
url = f"http://127.0.0.1:{port}/mcp"

with RunningServer(server.streamable_http_app(), port):
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        # ── 1. CLI single-server mode ────────────────────────────────────────
        cfg = tmp / "single.json"
        _write_config(cfg, "e2e-single", url)
        out1 = tmp / "out_single"
        proc = subprocess.run(
            [sys.executable, "-m", "src.workflows.mcp_inspector_workflow",
             "--config", str(cfg), "--server", "e2e-single",
             "--output-dir", str(out1)],
            capture_output=True, text=True, timeout=600,
            cwd="/Users/srinathp/MCP/mcp-client/app-info/internal_tools/MCP_Server_Inspector",
        )
        check("single mode exits 0", proc.returncode == 0,
              (proc.returncode, proc.stderr[-500:] if proc.stderr else ""))
        check("single mode wrote inspection_report.json",
              (out1 / "inspection_report.json").exists())
        check("single mode wrote attribute_checklist.csv",
              (out1 / "attribute_checklist.csv").exists())
        check("single mode wrote attributes.csv (new key-value report)",
              (out1 / "attributes.csv").exists())
        check("single mode wrote mcp_capabilities.csv",
              (out1 / "mcp_capabilities.csv").exists())

        if (out1 / "attributes.csv").exists():
            arows = list(csv.DictReader(open(out1 / "attributes.csv")))
            check("attributes.csv has Category/Attribute/Status header",
                  arows and set(arows[0].keys()) == {"Category", "Attribute", "Status"},
                  arows and list(arows[0].keys()))
            cats = []
            for r in arows:
                if r["Category"] not in cats:
                    cats.append(r["Category"])
            check("attributes.csv covers all 10 attribute groups",
                  cats == ["Distribution Type", "MCP Protocol Version",
                           "Pricing", "Hosting Provider", "Authentication",
                           "Data Protection", "Transport Protocol",
                           "Tools Operations", "Deployment Approach",
                           "Capabilities"], cats)
            by_pair = {(r["Category"], r["Attribute"]): r["Status"] for r in arows}
            check("attributes.csv transport resolved",
                  by_pair.get(("Transport Protocol", "StreamableHttp")) == "Yes",
                  by_pair.get(("Transport Protocol", "StreamableHttp")))
            check("attributes.csv tools ops resolved",
                  by_pair.get(("Tools Operations",
                               "Read-only update and/or delete operations")) == "Yes",
                  by_pair.get(("Tools Operations",
                               "Read-only update and/or delete operations")))
            check("attributes.csv has capabilities flags",
                  by_pair.get(("Capabilities", "Tools")) == "Yes", by_pair.get(("Capabilities", "Tools")))
            check("attributes.csv has no detailed_info blobs",
                  all(r["Attribute"] != "detailed_info" for r in arows),
                  [r["Attribute"] for r in arows if r["Attribute"] == "detailed_info"])

        # ── 2. capabilities CSV content ──────────────────────────────────────
        caps_path = out1 / "mcp_capabilities.csv"
        if caps_path.exists():
            rows = list(csv.DictReader(open(caps_path)))
            by_name = {r["tool_name"]: r for r in rows}
            check("all 3 tools found across paginated pages", len(rows) == 3,
                  [r["tool_name"] for r in rows])
            check("header is skill format (no rw_source)",
                  list(rows[0].keys()) == ["tool_name", "description", "parameters",
                                           "inputSchema", "outputSchema",
                                           "supports_read", "supports_write",
                                           "supports_delete"],
                  list(rows[0].keys()) if rows else None)
            if "get_status" in by_name:
                r = by_name["get_status"]
                check("get_status: annotation Read=Yes W=No D=No",
                      (r["supports_read"], r["supports_write"], r["supports_delete"])
                      == ("Yes", "No", "No"), dict(r))
            if "reset_state" in by_name:
                r = by_name["reset_state"]
                check("reset_state: annotation R=No W=Yes D=Yes",
                      (r["supports_read"], r["supports_write"], r["supports_delete"])
                      == ("No", "Yes", "Yes"), dict(r))
                check("reset_state: required param starred",
                      r["parameters"] == "confirm:boolean*", r["parameters"])
            if "delete_temp_files" in by_name:
                r = by_name["delete_temp_files"]
                check("delete_temp_files: heuristic D=Yes",
                      r["supports_delete"] == "Yes", dict(r))

        report = json.loads((out1 / "inspection_report.json").read_text()) \
            if (out1 / "inspection_report.json").exists() else {}
        proto = (report.get("server_attributes") or {}).get("protocol_version") or {}
        check("report has negotiated protocol version",
              bool(proto.get("negotiated_version")), proto.get("negotiated_version"))
        check("report records era evidence",
              proto.get("era_evidence") in ("legacy initialize", "server/discover answered"),
              proto.get("era_evidence"))
        check("report probe confirmed >=1 version",
              len(proto.get("supported_versions") or []) >= 1,
              proto.get("supported_versions"))

        # ── 3. CLI multi-server mode ─────────────────────────────────────────
        cfg2 = tmp / "multi.json"
        cfg2.write_text(json.dumps({"servers": [
            {"name": "e2e-a", "connection_type": "streamable_http",
             "endpoint_url": url, "description": "a"},
            {"name": "e2e-b", "connection_type": "streamable_http",
             "endpoint_url": url, "description": "b"},
        ]}))
        out2 = tmp / "out_multi"
        proc = subprocess.run(
            [sys.executable, "-m", "src.workflows.mcp_inspector_workflow",
             "--config", str(cfg2), "--all", "--output-dir", str(out2)],
            capture_output=True, text=True, timeout=900,
            cwd="/Users/srinathp/MCP/mcp-client/app-info/internal_tools/MCP_Server_Inspector",
        )
        check("multi mode exits 0 when all succeed", proc.returncode == 0,
              (proc.returncode, (proc.stderr or "")[-500:]))
        check("multi aggregated attribute CSV exists",
              (out2 / "aggregated_attribute_checklist.csv").exists())
        check("multi protocol CSV exists",
              (out2 / "all_servers_protocol.csv").exists())
        check("multi merged capabilities CSV exists",
              (out2 / "all_servers_capabilities.csv").exists())
        check("per-server dir e2e-a with both CSVs",
              (out2 / "e2e-a" / "attribute_checklist.csv").exists()
              and (out2 / "e2e-a" / "mcp_capabilities.csv").exists())
        check("per-server dir e2e-b with both CSVs",
              (out2 / "e2e-b" / "attribute_checklist.csv").exists()
              and (out2 / "e2e-b" / "mcp_capabilities.csv").exists())

        if (out2 / "all_servers_protocol.csv").exists():
            rows = list(csv.DictReader(open(out2 / "all_servers_protocol.csv")))
            check("protocol CSV has one row per server",
                  {r["server_name"] for r in rows} == {"e2e-a", "e2e-b"}, rows)
            check("protocol rows flag behind (server speaks SDK's 2025-11-25)",
                  all(r["version_match"] in ("behind", "current", "unknown") for r in rows)
                  and any(r["version_match"] == "behind" for r in rows),
                  [(r["server_name"], r["version_match"]) for r in rows])

        if (out2 / "all_servers_capabilities.csv").exists():
            rows = list(csv.DictReader(open(out2 / "all_servers_capabilities.csv")))
            check("merged caps CSV has 6 tool rows (3 per server)", len(rows) == 6,
                  len(rows))
            check("merged caps rows carry server names",
                  {r["server_name"] for r in rows} == {"e2e-a", "e2e-b"})

        # ── 4. failure accounting: one live + one dead server ────────────────
        cfg3 = tmp / "mixed.json"
        cfg3.write_text(json.dumps({"servers": [
            {"name": "e2e-live", "connection_type": "streamable_http",
             "endpoint_url": url, "description": "live"},
            {"name": "e2e-dead", "connection_type": "streamable_http",
             "endpoint_url": "http://127.0.0.1:1/mcp", "description": "dead"},
        ]}))
        out3 = tmp / "out_mixed"
        proc = subprocess.run(
            [sys.executable, "-m", "src.workflows.mcp_inspector_workflow",
             "--config", str(cfg3), "--all", "--output-dir", str(out3)],
            capture_output=True, text=True, timeout=900,
            cwd="/Users/srinathp/MCP/mcp-client/app-info/internal_tools/MCP_Server_Inspector",
        )
        check("mixed run exits 2 (partial failure)", proc.returncode == 2,
              proc.returncode)
        check("mixed run still produced aggregate for the live server",
              (out3 / "aggregated_attribute_checklist.csv").exists())
        check("failure warning printed", "e2e-dead" in (proc.stdout or ""),
              (proc.stdout or "")[-300:])

print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
