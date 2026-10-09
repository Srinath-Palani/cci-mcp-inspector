"""
API-bridge e2e: batch run against a REAL local MCP server, then per-server
and combined downloads over HTTP — the exact flow the UI's batch buttons drive.

Covers:
  1. POST /api/inspect/batch with a live endpoint -> jobs complete
  2. GET  /api/inspect/batch/{gid}/status -> aggregated paths include
     capabilities_csv (new skill format) and protocol_csv (new)
  3. GET  /api/inspect/batch/{gid}/download/capabilities_csv -> merged CSV,
     skill header + server_name prefix, correct rows
  4. GET  /api/inspect/batch/{gid}/download/csv -> attribute checklist
  5. GET  /api/inspect/batch/{gid}/server/{name}/download/csv and
     .../capabilities_csv -> per-server files, distinct content
"""

import csv
import io
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

import uvicorn
from fastapi.testclient import TestClient
from mcp.server.fastmcp import FastMCP

sys.path.insert(0, "api_bridge")
import main as api_main

ok = fail = 0


def check(label, cond, extra=""):
    global ok, fail
    if cond:
        print(f"  ✓ {label}")
        ok += 1
    else:
        print(f"  ✗ {label} {extra}")
        fail += 1


server = FastMCP(name="api-e2e-server")


@server.tool(annotations={"readOnlyHint": True})
def ping() -> str:
    """Ping the server."""
    return "pong"


@server.tool()
def delete_queue(name: str) -> str:
    """Delete a queue by name."""
    return "deleted"


class RunningServer:
    def __init__(self, app, port):
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def __enter__(self):
        self._thread.start()
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


port = _free_port()
url = f"http://127.0.0.1:{port}/mcp"

print("api e2e: batch against a real FastMCP server")
with RunningServer(server.streamable_http_app(), port):
    with TestClient(api_main.app) as client:
        resp = client.post("/api/inspect/batch", json={
            "targets": [url],
            "concurrency": 2,
        })
        check("batch accepted", resp.status_code == 200, (resp.status_code, resp.text[:200]))
        group_id = resp.json().get("group_id")
        check("group_id returned", bool(group_id))

        # poll until done
        status = None
        for _ in range(600):
            status = client.get(f"/api/inspect/batch/{group_id}/status").json()
            if status.get("state") == "done":
                break
            time.sleep(1.0)
        check("batch reached done", status and status.get("state") == "done",
              status and status.get("state"))
        job = (status.get("jobs") or [{}])[0]
        check("job done", job.get("state") == "done", job.get("state"))
        server_name = job.get("server_name")
        check("server name known", bool(server_name), server_name)

        aggregated = status.get("aggregated") or {}
        check("aggregated has capabilities_csv", "capabilities_csv" in aggregated, list(aggregated))
        check("aggregated has protocol_csv (new)", "protocol_csv" in aggregated, list(aggregated))
        check("aggregated has attribute csv", "csv" in aggregated, list(aggregated))

        # combined capabilities CSV — new skill format with server_name prefix
        r = client.get(f"/api/inspect/batch/{group_id}/download/capabilities_csv")
        check("combined caps 200", r.status_code == 200, r.status_code)
        rows = list(csv.DictReader(io.StringIO(r.text)))
        check("combined caps header has server_name + skill columns",
              rows and list(rows[0].keys()) == [
                  "server_name", "tool_name", "description", "parameters",
                  "inputSchema", "outputSchema",
                  "supports_read", "supports_write", "supports_delete"],
              rows and list(rows[0].keys()))
        by_name = {row["tool_name"]: row for row in rows}
        check("combined caps has both tools", {"ping", "delete_queue"} <= set(by_name),
              list(by_name))
        if "ping" in by_name:
            row = by_name["ping"]
            check("ping row annotation-driven",
                  (row["supports_read"], row["supports_write"], row["supports_delete"])
                  == ("Yes", "No", "No"), dict(row))
        if "delete_queue" in by_name:
            row = by_name["delete_queue"]
            check("delete_queue row heuristic",
                  row["supports_delete"] == "Yes", dict(row))

        # protocol CSV
        r = client.get(f"/api/inspect/batch/{group_id}/download/protocol_csv")
        check("protocol csv downloadable",
              r.status_code == 200 and "negotiated_protocol" in r.text,
              (r.status_code, r.text[:120]))

        # combined attribute checklist
        r = client.get(f"/api/inspect/batch/{group_id}/download/csv")
        check("combined attrs 200", r.status_code == 200, r.status_code)

        # per-server downloads — the new route the row buttons call
        r = client.get(f"/api/inspect/batch/{group_id}/server/{server_name}/download/attributes_csv")
        check("per-server attributes_csv 200", r.status_code == 200, r.status_code)
        arows = list(csv.DictReader(io.StringIO(r.text)))
        check("per-server attributes csv has checklist rows",
              arows and arows[0]["Category"] == "Distribution Type"
              and arows[0]["Attribute"] == "Official", arows[:1])
        check("per-server attributes filename",
              f"{server_name}_attributes.csv" in r.headers.get("content-disposition", ""),
              r.headers.get("content-disposition"))

        r = client.get(f"/api/inspect/batch/{group_id}/server/{server_name}/download/capabilities_csv")
        check("per-server caps 200", r.status_code == 200, r.status_code)
        rows = list(csv.DictReader(io.StringIO(r.text)))
        check("per-server caps is per-server skill format (no server_name col)",
              rows and list(rows[0].keys()) == [
                  "tool_name", "description", "parameters", "inputSchema",
                  "outputSchema", "supports_read", "supports_write",
                  "supports_delete"],
              rows and list(rows[0].keys()))
        check("per-server caps has 2 tools", len(rows) == 2, len(rows))
        check("per-server filename", f"{server_name}_capabilities.csv" in
              r.headers.get("content-disposition", ""),
              r.headers.get("content-disposition"))

        r = client.get(f"/api/inspect/batch/{group_id}/server/{server_name}/download/csv")
        check("per-server attrs 200", r.status_code == 200, r.status_code)
        check("per-server attrs is the checklist, not caps",
              "Category" in r.text and "supports_read" not in r.text,
              r.text[:120])

        r = client.get(f"/api/inspect/batch/{group_id}/server/not-in-group/download/csv")
        check("foreign server rejected 404", r.status_code == 404, r.status_code)

print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
