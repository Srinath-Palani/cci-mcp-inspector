"""
In-memory job manager for cancellable, progress-reporting inspections.

Each POST /api/inspect creates an InspectionJob backed by an asyncio.Task.
The LangGraph nodes report progress through a phase callback that updates the
job, the frontend polls GET /api/inspect/{job_id}/status, and POST
/api/inspect/{job_id}/cancel calls task.cancel() — CancelledError propagates
through the graph nodes (which re-raise it) and the MCP session context
managers close cleanly.

State is deliberately in-memory: the Inspector is a local, single-process tool.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


# Human-readable labels for the graph phases, in execution order
PHASE_LABELS = {
    "pending": "Queued",
    "queued": "Waiting for a free slot",
    "resolve_target": "Resolving target",
    "load_config": "Loading configuration",
    "auth_discovery": "Discovering authentication",
    "mcp_discovery": "Connecting & discovering capabilities",
    "llm_analysis": "Analyzing with LLM",
    "attribute_extraction": "Extracting server attributes",
    "generate_report": "Generating reports",
    "done": "Complete",
}


@dataclass
class InspectionJob:
    job_id: str
    server_name: str
    # queued: admitted to a batch but waiting on a concurrency slot. Distinct from
    # pending (created, not yet dispatched) so the dashboard can show a target as
    # accepted-but-not-started rather than implying work is under way.
    state: str = "pending"          # pending | queued | running | done | cancelled | error
    current_phase: str = "pending"
    progress: float = 0.0
    cancel_requested: bool = False
    result: Optional[Dict[str, Any]] = None   # InspectResponse payload when done
    error: Optional[str] = None
    error_details: Optional[Dict[str, Any]] = None
    # What the user actually typed for this job (URL or GitHub repo). Kept so a
    # batch row stays identifiable before a config — and therefore a name — exists.
    target: Optional[str] = None
    target_kind: Optional[str] = None   # "remote" | "github" | "stdio"
    # Stable identity for a batch row: "R1"/"G2"/"S1" (kind + 1-based ordinal within
    # that kind) plus the 0-based position in the submitted list. Two targets can
    # resolve to the same server name, and names only appear after resolution, so
    # neither is usable as a label — these are assigned at submit time and never
    # change, which is what lets the UI keep rows in the order the user typed them.
    identity: Optional[str] = None
    position: Optional[int] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    task: Optional[asyncio.Task] = None

    def status_payload(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "server_name": self.server_name,
            "state": self.state,
            "current_phase": self.current_phase,
            "phase_label": PHASE_LABELS.get(self.current_phase, self.current_phase),
            "progress": self.progress,
            "result": self.result,
            "error": self.error,
            "error_details": self.error_details,
            "target": self.target,
            "target_kind": self.target_kind,
            "identity": self.identity,
            "position": self.position,
            "queued_seconds": round(max(0.0, (self.started_at or time.time()) - self.created_at), 1),
            "elapsed_seconds": (
                round(max(0.0, (self.finished_at or time.time()) - self.started_at), 1)
                if self.started_at is not None else None
            ),
            "cancel_requested": self.cancel_requested,
            # False means cancellation was requested but the task has not unwound
            # yet — usually a blocking call already handed to a worker thread,
            # which cannot be interrupted and must finish its own timeout.
            "task_settled": self.task.done() if self.task is not None else True,
        }


class InspectionJobManager:
    """Registry of inspection jobs keyed by job_id."""

    def __init__(self, max_age_seconds: int = 3600):
        self._jobs: Dict[str, InspectionJob] = {}
        self._max_age = max_age_seconds

    def create_job(
        self,
        server_name: str,
        target: Optional[str] = None,
        target_kind: Optional[str] = None,
        identity: Optional[str] = None,
        position: Optional[int] = None,
    ) -> InspectionJob:
        self._cleanup()
        job = InspectionJob(
            job_id=uuid.uuid4().hex,
            server_name=server_name,
            target=target,
            target_kind=target_kind,
            identity=identity,
            position=position,
        )
        self._jobs[job.job_id] = job
        return job

    def get_job(self, job_id: str) -> Optional[InspectionJob]:
        return self._jobs.get(job_id)

    def update_phase(self, job_id: str, phase: str, progress: float) -> None:
        job = self._jobs.get(job_id)
        if job and job.state == "running":
            job.current_phase = phase
            job.progress = progress
            job.updated_at = time.time()

    def rename_job(self, job_id: str, server_name: str) -> None:
        """
        Set the display name once it is known.

        A batch job is created before its config is built (GitHub targets need a
        network round trip to resolve), so it starts out named after the raw target
        and is renamed when the real server name appears.
        """
        job = self._jobs.get(job_id)
        if job and server_name:
            job.server_name = server_name
            job.updated_at = time.time()

    def mark_queued(self, job_id: str, task: asyncio.Task) -> None:
        """Attach the task while the job still waits for a concurrency slot."""
        job = self._jobs.get(job_id)
        if job and job.state == "pending":
            job.state = "queued"
            job.current_phase = "queued"
            job.task = task
            job.updated_at = time.time()

    def mark_running(self, job_id: str, task: Optional[asyncio.Task] = None) -> None:
        job = self._jobs.get(job_id)
        if job:
            # A cancel that landed while the job was queued must not be undone by
            # the worker then picking it up.
            if job.cancel_requested:
                return
            job.state = "running"
            if task is not None:
                job.task = task
            if job.started_at is None:
                job.started_at = time.time()
            job.updated_at = time.time()

    def complete_job(self, job_id: str, result: Dict[str, Any]) -> None:
        job = self._jobs.get(job_id)
        if job:
            job.state = "done"
            job.current_phase = "done"
            job.progress = 1.0
            job.result = result
            job.finished_at = time.time()
            job.updated_at = job.finished_at

    def fail_job(self, job_id: str, error: str, error_details: Optional[Dict[str, Any]] = None) -> None:
        job = self._jobs.get(job_id)
        if job:
            job.state = "error"
            job.error = error
            job.error_details = error_details
            job.finished_at = time.time()
            job.updated_at = job.finished_at

    def cancel_job(self, job_id: str) -> bool:
        """
        Cancel a running job. Returns True if a cancellation was initiated.

        The state flips to "cancelled" immediately so the UI stays responsive, but
        task.cancel() is only a request: the coroutine unwinds at its next await.
        Blocking work is dispatched via asyncio.to_thread precisely so those awaits
        exist — a cancelled job stops before issuing its next HTTP request instead
        of running the whole sequence to completion. Any single request already
        in flight still finishes on its worker thread, bounded by its own timeout;
        `task_settled` in the status payload reports whether that has happened.
        """
        job = self._jobs.get(job_id)
        if not job:
            return False
        if job.state not in ("pending", "queued", "running"):
            return False
        job.cancel_requested = True
        if job.task and not job.task.done():
            job.task.cancel()
        job.state = "cancelled"
        job.finished_at = time.time()
        job.updated_at = job.finished_at
        return True

    def reset_for_retry(self, job_id: str, task: asyncio.Task) -> bool:
        """
        Requeue a settled job (error/cancelled) for another attempt.

        Only terminal-settled states may retry: a running job already has a live
        task, and re-running it would open a second session against the same
        server. The job keeps its identity, position and target — those were
        assigned at submit time and are what the UI rows are keyed on.
        """
        job = self._jobs.get(job_id)
        if not job or job.state not in ("error", "cancelled"):
            return False
        job.state = "queued"
        job.current_phase = "queued"
        job.progress = 0.0
        job.cancel_requested = False
        job.result = None
        job.error = None
        job.error_details = None
        job.task = task
        job.started_at = None
        job.finished_at = None
        job.created_at = time.time()
        job.updated_at = job.created_at
        return True

    def _cleanup(self) -> None:
        """Drop finished jobs older than max_age to bound memory."""
        now = time.time()
        stale = [
            jid for jid, job in self._jobs.items()
            if job.state in ("done", "cancelled", "error")
            and now - job.updated_at > self._max_age
        ]
        for jid in stale:
            del self._jobs[jid]


job_manager = InspectionJobManager()
