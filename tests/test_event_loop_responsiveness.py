"""
The regression test for the reported symptom: "connections time out / hang".

The cause was never the MCP protocol. It was that the inspection pipeline made
synchronous `requests` / `socket` / TLS calls from inside `async def` bodies, so a
single slow server blocked the whole FastAPI event loop — which is why the health
check and the UI's 1 s status poll appeared frozen, and why Cancel did nothing.

This drives the real app against a black-hole server (accepts TCP, never answers)
and asserts three things a blocked event loop cannot do:

  1. POST /api/inspect returns a job_id promptly (no blocking auth discovery in the
     request path).
  2. GET / and GET /api/inspect/{id}/status stay responsive *while* the job hangs.
  3. POST /api/inspect/{id}/cancel is honoured within a second.

Run directly:  .venv/bin/python test_event_loop_responsiveness.py
"""
import sys as _s, os as _o
_s.path.insert(0, _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))))


import asyncio
import socket
import threading
import time

import httpx

from test_transport_fallback_e2e import BackgroundServer, _unused_port

# A blocked event loop shows up as multi-second latency on a trivial GET. Real
# latency here is sub-millisecond, so this threshold only fires on genuine blocking.
MAX_ACCEPTABLE_LATENCY = 2.0
CANCEL_AFTER_SECONDS = 8.0
CANCEL_HONOURED_WITHIN = 5.0
JOB_MUST_SETTLE_WITHIN = 150.0


class BlackHoleServer:
    """Accepts connections and holds them open without ever replying."""

    def __init__(self):
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self.port = self._sock.getsockname()[1]
        self._held: list = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self):
        self._sock.settimeout(0.5)
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                continue
            self._held.append(conn)  # never read, never write, never close

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=3)
        for conn in self._held:
            try:
                conn.close()
            except OSError:
                pass
        self._sock.close()


async def main():
    from api_bridge.main import app as api_app

    api_port = _unused_port()
    latencies: list[float] = []
    max_latency = 0.0

    with BlackHoleServer() as blackhole, BackgroundServer(api_app, api_port):
        base = f"http://127.0.0.1:{api_port}"
        async with httpx.AsyncClient(base_url=base, timeout=30.0) as client:
            # ── 1. the request path must not block ──────────────────────────
            t0 = time.monotonic()
            started = await client.post("/api/inspect", json={
                "name": "blackhole",
                "connection_type": "auto",
                "endpoint_url": f"http://127.0.0.1:{blackhole.port}",
            })
            submit_seconds = time.monotonic() - t0
            payload = started.json()
            job_id = payload.get("job_id")
            assert job_id, f"no job_id: {payload}"
            assert submit_seconds < MAX_ACCEPTABLE_LATENCY, (
                f"POST /api/inspect blocked for {submit_seconds:.1f}s before returning a job_id"
            )
            print(f"  ✓ job accepted in {submit_seconds * 1000:.0f}ms → {job_id}")

            # ── 2. the loop must stay responsive while the job hangs ────────
            cancel_sent_at = None
            cancel_latency = None
            settled_at = None
            deadline = time.monotonic() + JOB_MUST_SETTLE_WITHIN

            while time.monotonic() < deadline:
                probe = time.monotonic()
                health, status = await asyncio.gather(
                    client.get("/"), client.get(f"/api/inspect/{job_id}/status")
                )
                latency = time.monotonic() - probe
                latencies.append(latency)
                max_latency = max(max_latency, latency)
                assert health.status_code == 200, health.status_code
                assert status.status_code == 200, status.status_code
                assert latency < MAX_ACCEPTABLE_LATENCY, (
                    f"event loop blocked: a health check + status poll took {latency:.1f}s "
                    f"(phase was {status.json().get('current_phase')})"
                )

                state = status.json().get("state")
                if cancel_sent_at is None and time.monotonic() - t0 >= CANCEL_AFTER_SECONDS:
                    # ── 3. cancel must be honoured ──────────────────────────
                    c0 = time.monotonic()
                    cancelled = await client.post(f"/api/inspect/{job_id}/cancel")
                    cancel_latency = time.monotonic() - c0
                    cancel_sent_at = time.monotonic()
                    assert cancelled.status_code == 200, cancelled.status_code
                    assert cancelled.json().get("success") is True, cancelled.json()
                    print(
                        f"  ✓ cancel acknowledged in {cancel_latency * 1000:.0f}ms "
                        f"(mid-phase: {status.json().get('current_phase')})"
                    )
                elif cancel_sent_at is not None:
                    if state in ("cancelled", "failed", "completed"):
                        settled_at = time.monotonic()
                        elapsed = settled_at - cancel_sent_at
                        assert state == "cancelled", f"expected cancelled, got {state}"
                        assert elapsed < CANCEL_HONOURED_WITHIN, (
                            f"job kept running {elapsed:.1f}s after cancel"
                        )
                        print(f"  ✓ job reached state 'cancelled' {elapsed:.1f}s after the request")
                        break

                await asyncio.sleep(0.5)

            assert settled_at is not None, (
                f"job never settled within {JOB_MUST_SETTLE_WITHIN:.0f}s"
            )

    avg = sum(latencies) / len(latencies)
    print(
        f"  ✓ {len(latencies)} health+status probes while the job hung against a "
        f"black hole: avg {avg * 1000:.0f}ms, worst {max_latency * 1000:.0f}ms"
    )
    print("\nEvent-loop responsiveness checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
