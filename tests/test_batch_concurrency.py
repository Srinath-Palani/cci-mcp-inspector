"""
import sys as _sys, os as _os; _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
Tests for the bounded-concurrency batch engine (POST /api/inspect/batch).

The original requirement was "5 to 10 remote endpoints at a time", with remote
endpoints and GitHub repositories mixed in a single submission. The ceiling was
later tightened to a strict 5 (MAX_BATCH_CONCURRENCY), sized for the local
GitHub-repo workload where each row also spawns a subprocess. The failure mode being
guarded against is the previous implementation's: one asyncio task per entry, created
all at once, so submitting 40 targets opened 40 simultaneous inspections.

What this asserts:

  1. Never more than `concurrency` entries are in flight at once, and every entry
     still runs (bounded, not dropped).
  2. Remote endpoints and GitHub repos can be submitted together and are classified
     apart — a github.com URL is a repo to launch, not an endpoint to connect to.
  3. Each row gets a stable identity (R1, R2, G1 …) and keeps its submitted order.
  4. Cancelling a group stops the queued entries that had not started yet.
  5. An unusable target fails as its own job with structured error_details, instead
     of taking down the group or vanishing from the results.

The inspection itself is replaced with a sleep: this exercises the scheduler, not
the MCP pipeline (which the other e2e tests cover).

Run directly:  .venv/bin/python test_batch_concurrency.py
"""
import sys as _s, os as _o
_s.path.insert(0, _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))))


import asyncio
import sys
import time

sys.path.insert(0, "api_bridge")

import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# Reach the manager through `main`, not through a fresh import: main.py imports it as
# `api_bridge.inspection_job_manager`, and importing it again by bare name would
# create a second module object with its own empty registry.
job_manager = main.job_manager


class ConcurrencyTracker:
    """Stands in for _run_inspection_job and records the concurrency high-water mark."""

    def __init__(self, duration: float = 0.35):
        self.duration = duration
        self.in_flight = 0
        self.peak = 0
        self.started: list = []
        self.completed: list = []

    async def run(self, job_id: str, server_config: dict, output_dir):
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        self.started.append(server_config.get("name"))
        try:
            await asyncio.sleep(self.duration)
            job_manager.complete_job(job_id, {"success": True, "server_name": server_config.get("name")})
            self.completed.append(server_config.get("name"))
        except asyncio.CancelledError:
            raise
        finally:
            self.in_flight -= 1


def _install_stub(monkey_duration=0.35):
    """
    Replace both the inspection runner and the config builder.

    _prepare_batch_entry normally resolves GitHub repos over the network; here it
    returns a config directly so the test stays offline and deterministic.
    """
    tracker = ConcurrencyTracker(monkey_duration)
    original_run = main._run_inspection_job
    original_prepare = main._prepare_batch_entry

    async def fake_prepare(job_id: str, entry: dict):
        if entry.get("_target") == "definitely not a target":
            main.job_manager.fail_job(job_id, "Unusable target", {"error_type": "INVALID_TARGET"})
            return None, None
        name = entry.get("name") or entry.get("_identity") or "srv"
        main.job_manager.rename_job(job_id, name)
        return {"name": name, "connection_type": "streamable_http", "endpoint_url": entry.get("_target")}, None

    main._run_inspection_job = tracker.run
    main._prepare_batch_entry = fake_prepare
    return tracker, (original_run, original_prepare)


def _restore(originals):
    main._run_inspection_job, main._prepare_batch_entry = originals


def _wait_for_group(client, group_id, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        payload = client.get(f"/api/inspect/batch/{group_id}/status").json()
        if payload["state"] == "done":
            return payload
        time.sleep(0.05)
    raise AssertionError(f"group {group_id} did not finish within {timeout}s")


def test_concurrency_is_bounded():
    """20 targets at concurrency 5: all run, never more than 5 at once."""
    tracker, originals = _install_stub()
    try:
        with TestClient(main.app) as client:
            targets = [f"https://srv{i}.example.com/mcp" for i in range(20)]
            resp = client.post("/api/inspect/batch", json={"targets": targets, "concurrency": 5})
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["concurrency"] == 5
            assert len(body["jobs"]) == 20

            status = _wait_for_group(client, body["group_id"])
            assert status["counts"]["done"] == 20, status["counts"]
            assert tracker.peak <= 5, f"peak concurrency was {tracker.peak}, expected <= 5"
            assert tracker.peak > 1, "nothing ran in parallel — the pool is serialized"
            print(f"✅ bounded concurrency: 20 targets, peak in-flight {tracker.peak} (limit 5)")
    finally:
        _restore(originals)


def test_concurrency_is_clamped_to_the_ceiling():
    """
    A request asking for 50 slots is clamped to the hard ceiling.

    The ceiling is read from main rather than hardcoded, so lowering or raising
    MAX_BATCH_CONCURRENCY cannot leave this test asserting a stale number — the
    point under test is that the request cannot exceed the ceiling, whatever it is.
    """
    ceiling = main.MAX_BATCH_CONCURRENCY
    tracker, originals = _install_stub(0.2)
    try:
        with TestClient(main.app) as client:
            targets = [f"https://busy{i}.example.com/mcp" for i in range(15)]
            body = client.post("/api/inspect/batch", json={"targets": targets, "concurrency": 50}).json()
            assert body["concurrency"] == ceiling, body["concurrency"]
            _wait_for_group(client, body["group_id"])
            assert tracker.peak <= ceiling, f"peak {tracker.peak} exceeded the hard ceiling {ceiling}"
            print(f"✅ concurrency clamped: requested 50 → {ceiling}, peak in-flight {tracker.peak}")
    finally:
        _restore(originals)


def test_mixed_remote_and_github_targets():
    """Remote endpoints and GitHub repos in one submission, each identified."""
    tracker, originals = _install_stub(0.1)
    try:
        with TestClient(main.app) as client:
            targets = [
                "https://mcp.example.com/mcp",
                "https://github.com/modelcontextprotocol/servers",
                "https://other.example.com/sse",
                "owner/repo",
            ]
            body = client.post("/api/inspect/batch", json={"targets": targets, "concurrency": 5}).json()
            kinds = [j["target_kind"] for j in body["jobs"]]
            assert kinds == ["remote", "github", "remote", "github"], kinds

            identities = [j["identity"] for j in body["jobs"]]
            assert identities == ["R1", "G1", "R2", "G2"], identities

            positions = [j["position"] for j in body["jobs"]]
            assert positions == [0, 1, 2, 3], positions

            status = _wait_for_group(client, body["group_id"])
            # Order in the status payload must match the order submitted.
            assert [j["identity"] for j in status["jobs"]] == identities
            assert status["counts"]["done"] == 4, status["counts"]
            print(f"✅ mixed batch: {identities} → kinds {kinds}, all completed")
    finally:
        _restore(originals)


def test_invalid_target_fails_as_its_own_job():
    """A bad target is a failed row, not a dropped one and not a group failure."""
    tracker, originals = _install_stub(0.1)
    try:
        with TestClient(main.app) as client:
            targets = ["https://good.example.com/mcp", "definitely not a target"]
            body = client.post("/api/inspect/batch", json={"targets": targets, "concurrency": 2}).json()
            assert len(body["jobs"]) == 2, "the invalid target must still get a row"

            status = _wait_for_group(client, body["group_id"])
            assert status["counts"]["done"] == 1, status["counts"]
            assert status["counts"]["error"] == 1, status["counts"]
            failed = [j for j in status["jobs"] if j["state"] == "error"][0]
            assert failed["error_details"] is not None, "failure must be structured"
            assert failed["target"] == "definitely not a target"
            assert failed["identity"] == "X1", failed["identity"]
            print(f"✅ invalid target isolated: {failed['identity']} → {failed['error_details']['error_type']}")
    finally:
        _restore(originals)


def test_cancel_group_stops_queued_entries():
    """Cancelling the group stops entries that had not been dispatched yet."""
    tracker, originals = _install_stub(3.0)
    try:
        with TestClient(main.app) as client:
            targets = [f"https://slow{i}.example.com/mcp" for i in range(12)]
            body = client.post("/api/inspect/batch", json={"targets": targets, "concurrency": 2}).json()
            group_id = body["group_id"]

            time.sleep(0.4)  # let the first slots start; the rest are queued
            mid = client.get(f"/api/inspect/batch/{group_id}/status").json()
            assert mid["counts"]["queued"] >= 8, mid["counts"]

            cancelled = client.post(f"/api/inspect/batch/{group_id}/cancel").json()
            assert cancelled["cancelled"] >= 10, cancelled

            status = _wait_for_group(client, group_id)
            assert status["counts"]["cancelled"] >= 10, status["counts"]
            assert tracker.peak <= 2, tracker.peak
            # The whole point: most entries never ran at all.
            assert len(tracker.started) <= 4, f"{len(tracker.started)} entries started despite cancel"
            print(f"✅ cancel-all: {status['counts']['cancelled']} stopped, only {len(tracker.started)} ever started")
    finally:
        _restore(originals)


def test_duplicate_targets_are_dropped():
    """The same endpoint twice must not produce two rows in the aggregate."""
    tracker, originals = _install_stub(0.1)
    try:
        with TestClient(main.app) as client:
            targets = [
                "https://dup.example.com/mcp",
                "https://dup.example.com/mcp/",   # trailing slash, same server
                "https://unique.example.com/mcp",
            ]
            body = client.post("/api/inspect/batch", json={"targets": targets, "concurrency": 3}).json()
            assert len(body["jobs"]) == 2, body["jobs"]
            assert len(body["duplicates_removed"]) == 1, body["duplicates_removed"]
            status = _wait_for_group(client, body["group_id"])
            assert status["counts"]["done"] == 2, status["counts"]
            assert status["aggregate_covers"]["total"] == 2, status["aggregate_covers"]
            print(f"✅ duplicates dropped: {body['duplicates_removed']}")
    finally:
        _restore(originals)


def test_declared_kind_wins_over_the_heuristic():
    """
    The dashboard has separate inputs per kind, so it declares the kind explicitly.

    The case that matters: a dotless host typed into the endpoint box must stay an
    endpoint, and a shorthand typed into the GitHub box must stay a repository —
    neither may be re-guessed.
    """
    tracker, originals = _install_stub(0.1)
    try:
        with TestClient(main.app) as client:
            targets = [
                {"target": "my-gateway/mcp", "kind": "remote"},
                {"target": "owner/repo", "kind": "github"},
            ]
            body = client.post("/api/inspect/batch", json={"targets": targets, "concurrency": 2}).json()
            kinds = [j["target_kind"] for j in body["jobs"]]
            assert kinds == ["remote", "github"], kinds
            assert [j["identity"] for j in body["jobs"]] == ["R1", "G1"]
            remote_target = body["jobs"][0]["target"]
            assert remote_target.startswith("https://my-gateway/mcp"), remote_target
            assert body["jobs"][1]["target"] == "https://github.com/owner/repo"
            _wait_for_group(client, body["group_id"])
            print(f"✅ declared kind honoured: {remote_target} stayed remote, owner/repo stayed github")
    finally:
        _restore(originals)


def test_each_target_keeps_its_own_credential():
    """
    Per-server credentials must not bleed across rows.

    Three endpoints in one run: one Bearer token, one API key on a custom header,
    one OAuth access token. Each must reach its own connection with its own header,
    and no row may inherit another's. This asserts on the built server_config, which
    is what the connection manager turns into request headers.
    """
    seen: dict = {}
    original_run = main._run_inspection_job

    async def capture(job_id, server_config, output_dir):
        seen[server_config["endpoint_url"]] = server_config.get("authentication")
        main.job_manager.complete_job(job_id, {"success": True})

    main._run_inspection_job = capture
    try:
        with TestClient(main.app) as client:
            targets = [
                {"target": "https://a.example.com/mcp", "kind": "remote", "name": "a",
                 "auth": {"mode": "bearer", "token": "tok-a"}},
                {"target": "https://b.example.com/mcp", "kind": "remote", "name": "b",
                 "auth": {"mode": "api_key", "token": "key-b", "header": "X-Custom-Key"}},
                {"target": "https://c.example.com/mcp", "kind": "remote", "name": "c",
                 "auth": {"mode": "oauth", "tokens": {"access_token": "oauth-c", "client_id": "cid"}}},
                {"target": "https://d.example.com/mcp", "kind": "remote", "name": "d"},
            ]
            body = client.post("/api/inspect/batch", json={"targets": targets, "concurrency": 4}).json()
            _wait_for_group(client, body["group_id"])

            a = seen["https://a.example.com/mcp"]
            b = seen["https://b.example.com/mcp"]
            c = seen["https://c.example.com/mcp"]
            d = seen["https://d.example.com/mcp"]

            assert a == {"type": "bearer", "token": "tok-a"}, a
            assert b["type"] == "api_key" and b["header"] == "X-Custom-Key" and b["token"] == "key-b", b
            assert c["type"] == "oauth2_1_authorization_code" and c["access_token"] == "oauth-c", c
            assert d is None, f"unauthenticated row inherited a credential: {d}"

            # And the decisive check: no token appears under any other endpoint.
            for url, cfg in seen.items():
                blob = repr(cfg)
                for other_token in ("tok-a", "key-b", "oauth-c"):
                    if other_token in blob:
                        assert url.startswith(f"https://{other_token[-1]}."), \
                            f"{other_token} leaked into {url}"
            print("✅ per-server credentials isolated: bearer / api_key(X-Custom-Key) / oauth / none")
    finally:
        main._run_inspection_job = original_run


def test_empty_credential_is_not_sent():
    """A mode selected but left blank must not send an empty header."""
    seen: dict = {}
    original_run = main._run_inspection_job

    async def capture(job_id, server_config, output_dir):
        seen[server_config["endpoint_url"]] = server_config.get("authentication")
        main.job_manager.complete_job(job_id, {"success": True})

    main._run_inspection_job = capture
    try:
        with TestClient(main.app) as client:
            targets = [
                {"target": "https://blank.example.com/mcp", "kind": "remote", "name": "blank",
                 "auth": {"mode": "bearer", "token": "   "}},
            ]
            body = client.post("/api/inspect/batch", json={"targets": targets, "concurrency": 1}).json()
            _wait_for_group(client, body["group_id"])
            assert seen["https://blank.example.com/mcp"] is None, seen
            print("✅ blank credential sent no Authorization header")
    finally:
        main._run_inspection_job = original_run


def test_same_endpoint_with_different_keys_is_not_deduped():
    """Two credentials against one endpoint are two inspections, not a duplicate."""
    tracker, originals = _install_stub(0.1)
    try:
        with TestClient(main.app) as client:
            targets = [
                {"target": "https://shared.example.com/mcp", "kind": "remote", "name": "k1",
                 "auth": {"mode": "api_key", "token": "key-1"}},
                {"target": "https://shared.example.com/mcp", "kind": "remote", "name": "k2",
                 "auth": {"mode": "api_key", "token": "key-2"}},
                {"target": "https://shared.example.com/mcp", "kind": "remote", "name": "k1-again",
                 "auth": {"mode": "api_key", "token": "key-1"}},
            ]
            body = client.post("/api/inspect/batch", json={"targets": targets, "concurrency": 3}).json()
            assert len(body["jobs"]) == 2, body["jobs"]
            assert len(body["duplicates_removed"]) == 1, body["duplicates_removed"]
            _wait_for_group(client, body["group_id"])
            print("✅ same endpoint, different keys → 2 jobs; identical repeat → deduped")
    finally:
        _restore(originals)


def test_classify_target_unit():
    """The remote-vs-repo decision, which everything else depends on."""
    cases = [
        ("https://mcp.example.com/mcp", "remote"),
        ("http://localhost:3000/sse", "remote"),
        ("https://github.com/owner/repo", "github"),
        ("https://www.github.com/owner/repo", "github"),
        ("github.com/owner/repo", "github"),
        ("owner/repo", "github"),
        ("https://gitlab.com/owner/repo", "github"),
        ("mcp.example.com/mcp", "remote"),
        # Dotless hosts must not be read as owner/repo — they are endpoints.
        ("localhost/mcp", "remote"),
        ("localhost:3000/sse", "remote"),
        ("internal-gw:8080/mcp", "remote"),
        ("", "invalid"),
        ("not a url at all", "invalid"),
    ]
    for raw, expected in cases:
        kind, normalized = main.classify_target(raw)
        assert kind == expected, f"{raw!r} → {kind}, expected {expected}"
        if kind != "invalid":
            assert normalized.startswith("http"), f"{raw!r} normalized to {normalized!r}"
    print(f"✅ classify_target: {len(cases)} cases correct")


def main_():
    tests = [
        test_classify_target_unit,
        test_concurrency_is_bounded,
        test_concurrency_is_clamped_to_the_ceiling,
        test_mixed_remote_and_github_targets,
        test_invalid_target_fails_as_its_own_job,
        test_duplicate_targets_are_dropped,
        test_declared_kind_wins_over_the_heuristic,
        test_each_target_keeps_its_own_credential,
        test_empty_credential_is_not_sent,
        test_same_endpoint_with_different_keys_is_not_deduped,
        test_cancel_group_stops_queued_entries,
    ]
    failures = 0
    for test in tests:
        try:
            test()
        except AssertionError as e:
            failures += 1
            print(f"❌ {test.__name__}: {e}")
        except Exception as e:
            failures += 1
            print(f"💥 {test.__name__}: {type(e).__name__}: {e}")
    print()
    print(f"{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main_())
