"""
import sys as _sys, os as _os; _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
API-bridge e2e: SINGLE-server inspection over HTTP — the path the single-view
ExportButtons drive:

  POST /api/inspect -> job completes
  GET  /api/inspect/{job_id} -> report paths include csv + capabilities_csv
  GET  /api/reports/{server}/csv              -> attribute checklist
  GET  /api/reports/{server}/capabilities_csv -> skill-format capabilities CSV
"""
import sys as _s, os as _o
_s.path.insert(0, _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))))


import csv
import io
import sys
import threading
import time

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


server = FastMCP(name="api-single-e2e")


@server.tool(annotations={"readOnlyHint": True})
def whoami() -> str:
    """Who is calling."""
    return "you"


@server.tool(annotations={"readOnlyHint": False, "destructiveHint": False})
def add_note(text: str) -> str:
    """Add a note (additive write, never deletes)."""
    return "added"


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

print("api e2e: single inspection against a real FastMCP server")
with RunningServer(server.streamable_http_app(), port):
    with TestClient(api_main.app) as client:
        resp = client.post("/api/inspect", json={
            "endpoint_url": url,
            "connection_type": "auto",  # -> streamable HTTP for /mcp; omitting defaults to SSE-only
        })
        check("inspect accepted", resp.status_code in (200, 202),
              (resp.status_code, resp.text[:200]))
        job_id = resp.json().get("job_id")
        check("job_id returned", bool(job_id), resp.json())

        job = None
        for _ in range(600):
            job = client.get(f"/api/inspect/{job_id}/status").json()
            if job.get("state") in ("done", "error"):
                break
            time.sleep(1.0)
        check("job done", job and job.get("state") == "done",
              job and (job.get("state"), job.get("error")))

        result = (job or {}).get("result") or {}
        report_paths = result.get("report_paths") or {}
        check("report_paths has csv + capabilities_csv",
              "csv" in report_paths and "capabilities_csv" in report_paths,
              list(report_paths))
        server_name = (result.get("report_data") or {}).get("server_name") \
            or job.get("server_name")
        check("server name known", bool(server_name), server_name)

        r = client.get(f"/api/reports/{server_name}/capabilities_csv")
        check("single capabilities_csv 200", r.status_code == 200, r.status_code)
        rows = list(csv.DictReader(io.StringIO(r.text)))
        by_name = {row["tool_name"]: row for row in rows}
        check("single caps has both tools", {"whoami", "add_note"} <= set(by_name),
              list(by_name))
        if "add_note" in by_name:
            row = by_name["add_note"]
            check("add_note: readOnly=false + destructive=false -> W=Yes D=No (annotation)",
                  (row["supports_read"], row["supports_write"],
                   row["supports_delete"])
                  == ("No", "Yes", "No"), dict(row))
        check("no rw_source column in output",
              rows and "rw_source" not in rows[0], rows and list(rows[0].keys()))
        check("single caps filename .csv",
              r.headers.get("content-disposition", "").endswith('.csv"'),
              r.headers.get("content-disposition"))

        r = client.get(f"/api/reports/{server_name}/csv")
        check("single checklist csv 200", r.status_code == 200, r.status_code)
        check("checklist is not the capabilities file",
              "supports_read" not in r.text, r.text[:120])

print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
