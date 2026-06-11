from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_ms() -> int:
    return int(time.time() * 1000)


class Job:
    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        self.status = JobStatus.PENDING
        self.stage = ""
        self.progress_pct = 0
        self.message = ""
        self.result: dict[str, Any] = {}
        self.error: str | None = None
        self.created_at = _now_iso()
        self.updated_at = _now_iso()

        # Stage log: each entry = {id, label, status, started_at, ended_ms, duration_ms, message}
        self.stage_log: list[dict] = []
        self._stage_start_ms: dict[str, int] = {}

        # Approval gate
        self.approval_data: dict | None = None      # data the UI must show for approval
        self.approval_response: dict | None = None  # user's (possibly edited) response
        self._approval_event = threading.Event()

    # ── Stage log helpers ──────────────────────────────────────────────────────

    def begin_stage(self, stage_id: str, label: str, message: str = "") -> None:
        """Mark a stage as started; close any currently-open stage."""
        self._close_current_stage()
        self._stage_start_ms[stage_id] = _now_ms()
        self.stage_log.append({
            "id": stage_id,
            "label": label,
            "status": "active",
            "started_at": _now_iso(),
            "duration_ms": None,
            "message": message,
        })
        self.stage = stage_id
        self.message = message
        self.updated_at = _now_iso()

    def end_stage(self, stage_id: str, message: str = "") -> None:
        for entry in self.stage_log:
            if entry["id"] == stage_id:
                entry["status"] = "done"
                start = self._stage_start_ms.get(stage_id)
                if start:
                    entry["duration_ms"] = _now_ms() - start
                if message:
                    entry["message"] = message
        self.updated_at = _now_iso()

    def _close_current_stage(self) -> None:
        for entry in reversed(self.stage_log):
            if entry["status"] == "active":
                entry["status"] = "done"
                start = self._stage_start_ms.get(entry["id"])
                if start:
                    entry["duration_ms"] = _now_ms() - start
                break

    def update(self, **kwargs: Any) -> None:
        """General field update; also refreshes the active stage's message if provided."""
        for k, v in kwargs.items():
            if k not in ("_approval_event",):
                setattr(self, k, v)
        # Mirror message into the active stage log entry
        if "message" in kwargs:
            for entry in reversed(self.stage_log):
                if entry["status"] == "active":
                    entry["message"] = kwargs["message"]
                    break
        self.updated_at = _now_iso()

    # ── Approval gate ──────────────────────────────────────────────────────────

    def request_approval(self, data: dict) -> None:
        """Pause the pipeline and signal the frontend that approval is needed."""
        self._approval_event.clear()
        self.approval_data = data
        self.approval_response = None
        self.status = JobStatus.AWAITING_APPROVAL
        self.updated_at = _now_iso()

    def wait_for_approval(self, timeout: float = 600) -> bool:
        """Block the pipeline thread until the user approves (or times out)."""
        return self._approval_event.wait(timeout=timeout)

    def submit_approval(self, response: dict) -> None:
        """Called from the HTTP handler when the user clicks Approve."""
        self.approval_response = response
        self.approval_data = None
        self.status = JobStatus.RUNNING
        self.updated_at = _now_iso()
        self._approval_event.set()

    # ── Serialisation ──────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "stage": self.stage,
            "progress_pct": self.progress_pct,
            "message": self.message,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "stage_log": self.stage_log,
            "approval_data": self.approval_data,
        }


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}

    def create(self) -> Job:
        job_id = str(uuid.uuid4())[:8]
        job = Job(job_id=job_id)
        self._jobs[job_id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def all(self) -> list[dict]:
        return [j.to_dict() for j in self._jobs.values()]


job_manager = JobManager()
