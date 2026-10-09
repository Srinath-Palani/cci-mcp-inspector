"""
Checks for the connection-hardening changes:

  * every failure path is time-bounded (no infinite hang on a silent server)
  * failures are classified into an actionable failure_kind
  * the transport candidate walk retries on transport-level failures, stops
    immediately on auth failures, and reports what it tried

Run directly:  .venv/bin/python test_connection_hardening.py
"""

import asyncio
import socket
import threading
import time

from src.agents.mcp_discovery_agent import (
    DISCOVERY_WALK_BUDGET_SECONDS,
    _discover_http_with_fetcher,
    _is_transport_level_failure,
)
from src.utility.mcp_capability_fetcher import classify_failure, fetch_mcp_capabilities


class BlackHoleServer:
    """Accepts TCP connections and never writes a byte back."""

    def __init__(self):
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._held = []
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self):
        self._sock.settimeout(0.5)
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
                self._held.append(conn)  # keep open, never respond
            except OSError:
                pass

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=2)
        for conn in self._held:
            conn.close()
        self._sock.close()


def _unused_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def test_silent_server_is_bounded():
    """A server that accepts and never replies must fail fast, not hang forever."""
    with BlackHoleServer() as server:
        for transport in ("streamable_http", "sse"):
            t0 = time.monotonic()
            result = await fetch_mcp_capabilities(
                f"http://127.0.0.1:{server.port}/mcp",
                transport=transport,
                timeout=3.0,
                sse_read_timeout=3.0,
                request_read_timeout=3.0,
            )
            elapsed = time.monotonic() - t0
            assert result.get("error"), f"{transport}: expected a failure"
            assert elapsed < 15.0, f"{transport}: took {elapsed:.1f}s — not bounded"
            print(f"  ✓ {transport}: failed in {elapsed:.1f}s "
                  f"(kind={result.get('failure_kind')})")


async def test_refused_connection_classified():
    port = _unused_port()
    result = await fetch_mcp_capabilities(
        f"http://127.0.0.1:{port}/mcp", transport="streamable_http", timeout=3.0
    )
    assert result.get("failure_kind") == "connect", result.get("failure_kind")
    print(f"  ✓ refused connection → failure_kind={result['failure_kind']}")


async def test_dns_failure_classified():
    result = await fetch_mcp_capabilities(
        "http://no-such-host.invalid/mcp", transport="streamable_http", timeout=5.0
    )
    # Resolver behaviour varies by environment; either diagnosis is acceptable,
    # "unknown" is not.
    assert result.get("failure_kind") in {"dns", "connect"}, result.get("failure_kind")
    print(f"  ✓ unresolvable host → failure_kind={result['failure_kind']}")


def test_transport_level_predicate():
    for status in (400, 405, 406):
        assert _is_transport_level_failure({"status_code": status}), status
    assert _is_transport_level_failure({"failure_kind": "stream_closed"})
    # Real errors must NOT be treated as "try the other transport".
    for status in (401, 403, 404, 500):
        assert not _is_transport_level_failure({"status_code": status}), status
    assert not _is_transport_level_failure({"failure_kind": "read_timeout"})
    print("  ✓ 400/405/406/stream_closed are transport-level; 401/403/404/500 are not")


def test_classify_failure_unwraps_groups():
    """The classifier must see through ExceptionGroups and __cause__ chains."""
    inner = ConnectionResetError("peer went away")
    wrapped = ExceptionGroup("task group failed", [ExceptionGroup("inner", [inner])])
    assert classify_failure(wrapped)["failure_kind"] == "stream_closed"

    chained = RuntimeError("outer")
    chained.__cause__ = TimeoutError("no response")
    assert classify_failure(chained)["failure_kind"] == "read_timeout"
    print("  ✓ classify_failure unwraps ExceptionGroups and cause chains")


async def test_walk_reports_attempts_and_is_bounded():
    port = _unused_port()
    t0 = time.monotonic()
    result = await _discover_http_with_fetcher(
        {"endpoint_url": f"http://127.0.0.1:{port}", "name": "dead"}, "dead", "auto"
    )
    elapsed = time.monotonic() - t0

    assert result["status"] == "FAILURE"
    assert elapsed < DISCOVERY_WALK_BUDGET_SECONDS + 10, f"walk took {elapsed:.1f}s"
    attempted = result.get("attempted_candidates") or []
    assert len(attempted) >= 2, f"expected a multi-transport walk, got {attempted}"
    assert any(a.startswith("streamable_http@") for a in attempted), attempted
    assert any(a.startswith("sse@") for a in attempted), attempted
    # The failure message must name what was tried, so the UI is diagnosable.
    assert "tried:" in (result.get("error_message") or "")
    print(f"  ✓ walk tried {attempted} in {elapsed:.1f}s and reported them")


def test_classifier_typed_branches():
    """Transport-level failures must render as TRANSPORT_MISMATCH, not UNKNOWN."""
    from api_bridge.error_classifier import classify_error, classify_failure_result

    for status in (400, 405, 406):
        d = classify_error(message="rejected", stage="mcp_discovery", status_code=status)
        assert d.error_type == "TRANSPORT_MISMATCH", (status, d.error_type)
        assert any(s.action == "switch_transport" for s in d.suggestions), status

    d = classify_error(message="boom", stage="mcp_discovery", failure_kind="stream_closed")
    assert d.error_type == "TRANSPORT_MISMATCH", d.error_type

    # A message that merely mentions TLS is not a TLS error.
    d = classify_error(message="server reports it supports TLS 1.3", stage="mcp_discovery")
    assert d.error_type != "TLS_ERROR", d.error_type
    d = classify_error(message="[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
    assert d.error_type == "TLS_ERROR", d.error_type

    # status_code / failure_kind must survive the agent result dict.
    d = classify_failure_result(
        {"status": "FAILURE", "error_message": "all candidates failed",
         "status_code": 400, "failure_kind": "http_status"},
        "mcp_discovery",
    )
    assert (d.error_type, d.status_code) == ("TRANSPORT_MISMATCH", 400), d
    print("  ✓ 400/405/406 and stream_closed render as TRANSPORT_MISMATCH; TLS test is narrow")


def test_pipeline_error_carries_structured_failure():
    """A phase that reports FAILURE must not lose status_code/failure_kind."""
    from api_bridge.main import InspectionPipelineError, _classify_exception

    exc = InspectionPipelineError(
        "Discovery failed: all transport candidates failed",
        failure_result={"status": "FAILURE", "error_message": "closed early",
                        "failure_kind": "stream_closed"},
        stage="mcp_discovery",
    )
    try:
        raise exc
    except Exception as e:
        _text, details = _classify_exception(e)
    assert details.error_type == "TRANSPORT_MISMATCH", details.error_type
    assert details.stage == "mcp_discovery"
    print("  ✓ InspectionPipelineError reaches classify_failure_result intact")


async def main():
    print("silent server is time-bounded:")
    await test_silent_server_is_bounded()
    print("failure classification:")
    await test_refused_connection_classified()
    await test_dns_failure_classified()
    test_classify_failure_unwraps_groups()
    test_transport_level_predicate()
    test_classifier_typed_branches()
    test_pipeline_error_carries_structured_failure()
    print("candidate walk:")
    await test_walk_reports_attempts_and_is_bounded()
    print("\nAll connection-hardening checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
