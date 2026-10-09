import sys as _s, os as _o
_s.path.insert(0, _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))))
import sys; sys.path.insert(0, "api_bridge")
import main
from fastapi.testclient import TestClient
import sys as _sys, os as _os; _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
ok = fail = 0
def check(label, cond, extra=""):
    global ok, fail
    if cond: print(f"  ✓ {label}"); ok += 1
    else: print(f"  ✗ {label} {extra}"); fail += 1

print("clamp is strict at 5:")
check("10 -> 5", main._clamp_concurrency(10) == 5, main._clamp_concurrency(10))
check("6 -> 5", main._clamp_concurrency(6) == 5)
check("5 -> 5", main._clamp_concurrency(5) == 5)
check("0 -> 1", main._clamp_concurrency(0) == 1)
check("garbage -> default 5", main._clamp_concurrency("x") == 5)
check("MAX constant is 5", main.MAX_BATCH_CONCURRENCY == 5)

print("per-server capabilities CSV is downloadable:")
import tempfile, pathlib
d = pathlib.Path(tempfile.gettempdir()) / "mcp_inspector_api" / "dltest"
d.mkdir(parents=True, exist_ok=True)
(d / "mcp_capabilities.csv").write_text("name,kind\nask,tool\n")
(d / "attribute_checklist.csv").write_text("attr,value\nTools,Yes\n")
with TestClient(main.app) as c:
    r = c.get("/api/reports/dltest/capabilities_csv")
    check("capabilities_csv 200", r.status_code == 200, r.status_code)
    check("body is the capability list", "ask,tool" in r.text, r.text[:40])
    check("filename ends .csv", r.headers.get("content-disposition","").endswith('.csv"'),
          r.headers.get("content-disposition"))
    check("media type text/csv", r.headers["content-type"].startswith("text/csv"))
    r2 = c.get("/api/reports/dltest/csv")
    check("checklist csv still distinct", "Tools,Yes" in r2.text)
    check("listing includes capabilities_csv",
          "capabilities_csv" in c.get("/api/reports/dltest").json()["reports"])
    check("bogus format 400", c.get("/api/reports/dltest/nope").status_code == 400)

    print("batch combined download:")
    check("unknown group 404", c.get("/api/inspect/batch/nosuch/download/csv").status_code == 404)
    gid = "grp1"
    main._batch_groups[gid] = {"job_ids": [], "output_dir": str(d), "state": "running",
                              "concurrency": 5, "aggregated": None}
    check("still running -> 409", c.get(f"/api/inspect/batch/{gid}/download/csv").status_code == 409)
    main._batch_groups[gid]["state"] = "done"
    check("done, nothing aggregated -> 404",
          c.get(f"/api/inspect/batch/{gid}/download/csv").status_code == 404)
    agg = d / "aggregated_attribute_checklist.csv"; agg.write_text("server,attr\ns1,Tools\n")
    main._batch_groups[gid]["aggregated"] = {"csv": str(agg), "html": str(d / "missing.html")}
    r3 = c.get(f"/api/inspect/batch/{gid}/download/csv")
    check("combined csv 200", r3.status_code == 200, r3.status_code)
    check("combined csv body", "s1,Tools" in r3.text)
    check("missing file on disk -> 404",
          c.get(f"/api/inspect/batch/{gid}/download/html").status_code == 404)
    check("bad format -> 400", c.get(f"/api/inspect/batch/{gid}/download/pdf").status_code == 400)

print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
