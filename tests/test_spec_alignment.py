"""
Regression tests for the 2026-09 spec-alignment work:

- Two-tier R/W/D classification (annotation-first, labeled keyword fallback)
- Skill-format capabilities CSV (8 columns, no rw_source in output)
- Pagination in _safe_list (cursor loop, page cap, legacy SDK fallback)
- Protocol prober: server/discover era detection, -32022 supported-list parsing
- Aggregated protocol + capabilities CSVs
- Batch per-server download route
"""
import sys as _s, os as _o
_s.path.insert(0, _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))))


import asyncio
import csv
import io
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, "api_bridge")

ok = fail = 0


def check(label, cond, extra=""):
    global ok, fail
    if cond:
        print(f"  ✓ {label}")
        ok += 1
    else:
        print(f"  ✗ {label} {extra}")
        fail += 1


from src.utility.tool_operation_classifier import classify_tool_actions, rollup_operation_type

print("two-tier classification:")
r = classify_tool_actions({"name": "delete_thing", "description": "x",
                           "annotations": {"readOnlyHint": True}})
check("declared readOnly beats delete keyword",
      (r["supports_read"], r["supports_write"], r["supports_delete"], r["source"])
      == ("Yes", "No", "No", "annotation"), r)

r = classify_tool_actions({"name": "get_user",
                           "annotations": {"readOnlyHint": False, "destructiveHint": True}})
check("readOnly=false + destructive=true -> W+D annotation",
      (r["supports_read"], r["supports_write"], r["supports_delete"], r["source"])
      == ("No", "Yes", "Yes", "annotation"), r)

r = classify_tool_actions({"name": "purge_cache", "annotations": {"readOnlyHint": False}})
check("readOnly=false, destructive absent -> keyword fallback, heuristic",
      (r["supports_delete"], r["source"]) == ("Yes", "heuristic"), r)

r = classify_tool_actions({"name": "list_files", "description": "List files"})
check("no annotations -> heuristic read",
      (r["supports_read"], r["source"]) == ("Yes", "heuristic"), r)

r = classify_tool_actions({"name": "", "description": ""})
check("no signal at all -> source none, all No",
      (r["supports_read"], r["supports_write"], r["supports_delete"], r["source"])
      == ("No", "No", "No", "none"), r)

r = classify_tool_actions({"name": "get_thing",
                           "annotations": {"readOnlyHint": "false"}})
check("string 'false' hint is NOT treated as declared (falls to heuristic)",
      r["source"] == "heuristic" and r["supports_read"] == "Yes", r)

r = classify_tool_actions({"name": "add_tag",
                           "annotations": {"readOnlyHint": False, "destructiveHint": "yes"}})
check("string destructiveHint ignored -> keyword tier for delete",
      r["source"] == "heuristic" and r["supports_delete"] == "No", r)

ro, ru, rud, stats = rollup_operation_type([
    {"name": "list_a", "annotations": {"readOnlyHint": True}},
    {"name": "remove_b"},
])
check("rollup: one delete -> read_update_delete", (ro, ru, rud) == (False, False, True))
check("rollup stats count tiers", stats["annotation"] == 1 and stats["heuristic"] == 1, stats)


from src.utility.capability_csv_exporter import CapabilityCSVExporter

print("capabilities CSV (skill format):")
discovery = {"tools_discovered": [
    {"name": "get_weather", "description": "Get weather",
     "inputSchema": {"type": "object", "properties": {"location": {"type": "string"}},
                     "required": ["location"]},
     "annotations": {"readOnlyHint": True}},
    {"name": "delete_city", "description": None,
     "inputSchema": {"type": "object", "properties": {}},
     "outputSchema": {"type": "object"}},
]}
with tempfile.TemporaryDirectory() as d:
    path = CapabilityCSVExporter(discovery, {"name": "s"}).save(Path(d))
    rows = list(csv.reader(open(path)))
check("header is skill format (no rw_source in output)",
      rows[0] == ["tool_name", "description", "parameters", "input_schema",
                  "output_schema", "supports_read", "supports_write",
                  "supports_delete"], rows[0])
check("row count = tools only", len(rows) == 3, len(rows))
check("required param starred", rows[1][2] == "location:string*", rows[1][2])
check("absent description -> NA", rows[2][1] == "NA", rows[2][1])
check("annotation row: R=Yes W=No D=No",
      (rows[1][5], rows[1][6], rows[1][7]) == ("Yes", "No", "No"), rows[1])
check("heuristic delete guessed", rows[2][7] == "Yes", rows[2][7])
check("no rw_source column", all(len(r) == 8 for r in rows))


from src.utility.mcp_capability_fetcher import _safe_list

print("pagination:")


class _Resp:
    def __init__(self, tools, nextCursor=None):
        self.tools = tools
        self.nextCursor = nextCursor


class _T:
    def __init__(self, n):
        self.name = n


class _Session:
    def __init__(self, pages):
        self.pages = pages

    async def list_tools(self, cursor=None):
        return self.pages[cursor]


async def _paginated():
    s = _Session({None: _Resp([_T("a")], "c2"), "c2": _Resp([_T("b")], None)})
    return await _safe_list(s, "list_tools")


items = asyncio.run(_paginated())
check("follows nextCursor", [t.name for t in items] == ["a", "b"], items)


class _LegacySession:
    async def list_tools(self):  # no cursor kwarg
        return _Resp([_T("x")])


items = asyncio.run(_safe_list(_LegacySession(), "list_tools"))
check("legacy SDK without cursor param still works", [t.name for t in items] == ["x"])


class _InfSession:
    def __init__(self):
        self.calls = 0

    async def list_tools(self, cursor=None):
        self.calls += 1
        return _Resp([_T(f"t{self.calls}")], f"c{self.calls}")


s = _InfSession()
items = asyncio.run(_safe_list(s, "list_tools"))
check("100-page guard trips", s.calls == 100 and len(items) == 100, (s.calls, len(items)))


class _LoopSession:
    """Buggy server: same cursor forever — must stop early, not at the cap."""

    def __init__(self):
        self.calls = 0

    async def list_tools(self, cursor=None):
        self.calls += 1
        return _Resp([_T(f"t{self.calls}")], "same-cursor")


s = _LoopSession()
items = asyncio.run(_safe_list(s, "list_tools"))
check("repeating cursor stops after 2 pages", s.calls == 2 and len(items) == 2,
      (s.calls, len(items)))


class _ErrSession:
    async def list_tools(self, cursor=None):
        raise RuntimeError("nope")


check("error -> empty list", asyncio.run(_safe_list(_ErrSession(), "list_tools")) == [])


from src.utility import protocol_version_prober as prober

print("protocol prober:")

payload = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32022,
           "message": "Unsupported protocol version",
           "data": {"supported": ["2025-11-25", "2025-06-18"], "requested": "2026-07-28"}}}
v = prober._verdict_from_payload("2026-07-28", payload)
check("-32022 rejected", v["supported"] is False)
check("-32022 supported list captured",
      v.get("server_supported_versions") == ["2025-11-25", "2025-06-18"], v)

discover_reply = {"jsonrpc": "2.0", "id": 0, "result": {
    "resultType": "complete",
    "supportedVersions": ["2026-07-28", "2025-11-25"],
    "capabilities": {"tools": {}},
    "_meta": {"io.modelcontextprotocol/serverInfo": {"name": "Modern", "version": "1"}},
}}
parsed = prober._parse_discover_result(discover_reply)
check("discover parses supportedVersions",
      parsed and parsed["supported_versions"] == ["2026-07-28", "2025-11-25"], parsed)
check("discover legacy error -> None",
      prober._parse_discover_result(
          {"jsonrpc": "2.0", "id": 0, "error": {"code": -32602, "message": "unknown method"}}
      ) is None)


class _FakeResp:
    def __init__(self, text, status=200, headers=None):
        self.text = text
        self.status_code = status
        self.headers = headers or {}


async def _probe_modern():
    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, url, json=None, headers=None, timeout=None):
            if json and json.get("method") == "server/discover":
                return _FakeResp(globals()["json"].dumps(discover_reply))
            raise AssertionError("legacy initialize must not be called for a modern server")

        async def delete(self, *a, **k):
            return _FakeResp("", 200)

    import httpx
    with patch.object(httpx, "AsyncClient", FakeClient):
        return await prober.probe_protocol_versions("http://x/mcp", "streamable_http")


out = asyncio.run(_probe_modern())
check("modern server: era = server/discover", out["era_evidence"] == "server/discover answered", out["era_evidence"])
check("modern server: latest from discover", out["latest_supported"] == "2026-07-28", out["latest_supported"])


async def _probe_legacy():
    legacy_init = {"jsonrpc": "2.0", "id": 1, "result": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "serverInfo": {"name": "Old", "version": "0.1"},
    }}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, url, json=None, headers=None, timeout=None):
            if json and json.get("method") == "server/discover":
                return _FakeResp(globals()["json"].dumps(
                    {"jsonrpc": "2.0", "id": 0, "error": {"code": -32602, "message": "unknown method"}}))
            return _FakeResp(globals()["json"].dumps(legacy_init))

        async def delete(self, *a, **k):
            return _FakeResp("", 200)

    import httpx
    with patch.object(httpx, "AsyncClient", FakeClient):
        return await prober.probe_protocol_versions(
            "http://x/mcp", "streamable_http", versions=["2025-06-18"])

out = asyncio.run(_probe_legacy())
check("legacy server: falls back to initialize", out["era_evidence"] == "legacy initialize", out["era_evidence"])
check("legacy server: version confirmed", out["supported_versions"] == ["2025-06-18"], out["supported_versions"])


from src.graph.inspection_graph import (
    _write_aggregated_protocol_csv,
    _write_aggregated_capabilities_csv,
)

print("aggregated reports:")
reports = [
    {"server_name": "DeepWiki", "server_attributes": {"protocol_version": {
        "negotiated_version": "2025-11-25", "latest_supported": "2025-11-25",
        "era_evidence": "legacy initialize",
        "probes": [{"version": "2026-07-28", "supported": False},
                   {"version": "2025-11-25", "supported": True}],
    }}, "metadata": {"discovery_data": {"tools_discovered": [
        {"name": "ask_question", "description": "Ask",
         "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}},
                         "required": ["q"]},
         "annotations": {"readOnlyHint": True}},
    ]}}},
    {"server_name": "NoProbe", "server_attributes": {"protocol_version": {}},
     "tools": [{"name": "delete_repo", "description": "Remove a repo"}]},
]
with tempfile.TemporaryDirectory() as d:
    d = Path(d)
    _write_aggregated_protocol_csv(reports, d)
    _write_aggregated_capabilities_csv(reports, d)
    proto = list(csv.reader(open(d / "all_servers_protocol.csv")))
    caps = list(csv.reader(open(d / "all_servers_capabilities.csv")))

check("protocol header",
      proto[0] == ["server_name", "latest_spec", "negotiated_protocol",
                   "latest_supported", "version_match", "era_evidence",
                   "negotiation_walk", "resolved_transport", "transport_status",
                   "resolved_url"], proto[0])
check("behind flag", proto[1][4] == "behind", proto[1])
check("unknown flag", proto[2][4] == "unknown", proto[2])
check("caps merged with server prefix", caps[1][0] == "DeepWiki" and caps[2][0] == "NoProbe", caps)
check("caps fallback row works without discovery_data",
      caps[2][8] == "Yes", caps[2])
check("caps header has no rw_source",
      "rw_source" not in caps[0] and len(caps[0]) == 9, caps[0])


print("batch per-server download route:")
# The API's loopback guard treats TestClient's host ("testclient") as non-local,
# so set a token before importing the app and send it as a bearer on each call.
import os as _os
_os.environ.setdefault("MCP_INSPECTOR_TOKEN", "test-token")
import main as api_main
from fastapi.testclient import TestClient

_AUTH = {"Authorization": "Bearer test-token"}

d = Path(tempfile.gettempdir()) / "mcp_inspector_api" / "bdltest"
d.mkdir(parents=True, exist_ok=True)
(d / "mcp_capabilities.csv").write_text("tool_name,description\nask,Ask\n")
(d / "attribute_checklist.csv").write_text("Category,Attribute,Status\nX,Y,Yes\n")
(d / "attributes.csv").write_text(
    "Category,Attribute,Status\nTransport Protocol,StreamableHttp,Yes\n")

api_main._batch_groups["g1"] = {"job_ids": ["j1"], "state": "done", "aggregated": {}}


class _Job:
    server_name = "bdltest"


with patch.object(api_main.job_manager, "get_job", lambda jid: _Job()):
    with TestClient(api_main.app) as c:
        r = c.get("/api/inspect/batch/g1/server/bdltest/download/capabilities_csv", headers=_AUTH)
        check("per-server caps 200", r.status_code == 200, r.status_code)
        check("per-server caps body", "ask,Ask" in r.text, r.text[:40])
        check("per-server caps filename", "bdltest_capabilities.csv" in
              r.headers.get("content-disposition", ""), r.headers.get("content-disposition"))
        r = c.get("/api/inspect/batch/g1/server/bdltest/download/csv", headers=_AUTH)
        check("per-server checklist 200 and distinct", r.status_code == 200 and "Category" in r.text)
        r = c.get("/api/inspect/batch/g1/server/bdltest/download/attributes_csv", headers=_AUTH)
        check("per-server attributes_csv 200", r.status_code == 200, r.status_code)
        check("per-server attributes_csv body", "Transport Protocol" in r.text, r.text[:60])
        check("attributes filename", "bdltest_attributes.csv" in
              r.headers.get("content-disposition", ""), r.headers.get("content-disposition"))
        r = c.get("/api/inspect/batch/g1/server/stranger/download/csv", headers=_AUTH)
        check("server not in group -> 404", r.status_code == 404, r.status_code)
        r = c.get("/api/inspect/batch/nope/server/bdltest/download/csv", headers=_AUTH)
        check("unknown group -> 404", r.status_code == 404, r.status_code)
        r = c.get("/api/inspect/batch/g1/server/bdltest/download/bogus", headers=_AUTH)
        check("bad format -> 400", r.status_code == 400, r.status_code)

del api_main._batch_groups["g1"]

print("multi-mode semaphore survives two event loops:")
from src.graph import inspection_graph as graph_mod


async def _fake_single(init):
    return {"final_report": {"server_name": init["server_name"]}}


def _run_multi(names):
    async def go():
        with patch.object(graph_mod, "single_server_graph") as mock_graph:
            mock_graph.ainvoke = _fake_single
            builder_state = {
                "server_names": names,
                "enable_testing": False,
                "output_dir": Path(tempfile.mkdtemp()),
                "completed_reports": [],
                "failed_servers": [],
            }
            return await graph_mod.multi_server_graph.ainvoke(builder_state)
    return asyncio.run(go())


# Two separate asyncio.run() calls = two event loops. A module-level semaphore
# would raise "bound to a different event loop" on the second pass.
r1 = _run_multi(["a"])
r2 = _run_multi(["b", "c"])
check("first loop completes", len(r1["completed_reports"]) == 1, r1.get("failed_servers"))
check("second loop completes (semaphore not loop-bound)",
      len(r2["completed_reports"]) == 2, r2.get("failed_servers"))

print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
