"""In-process background jobs for long operations (sync, ingest, normalize, eval).

A sync or a rebuild takes far longer than a request should, so the UI starts a
job, gets an id back, and polls it. Deliberately minimal: threads and a dict, no
queue, no broker, no persistence (§11 — clean seams, not premature machinery).
Jobs die with the process; that is acceptable for a local single-user tool and
is stated plainly in the UI rather than pretended away.

Two properties matter:

* **Single-flight per name.** Two concurrent syncs would double-ingest and race
  the normalizer, so starting a job whose name is already running is refused.
* **Failures are recorded, never swallowed.** A job that raises ends as
  ``failed`` with the exception text captured for display.
"""

from __future__ import annotations

import threading
import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

RUNNING = "running"
OK = "ok"
FAILED = "failed"

# Each job body receives an `emit` callable to report progress lines.
JobBody = Callable[[Callable[[str], None]], None]


@dataclass
class Job:
    id: str
    name: str
    status: str
    started_at: str
    finished_at: str | None = None
    lines: list[str] = field(default_factory=list)
    error: str | None = None

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "lines": list(self.lines),
            "error": self.error,
        }


class JobBusy(RuntimeError):
    """Raised when a job of the same name is already running."""


class JobRunner:
    """Runs job bodies on daemon threads and keeps a bounded history."""

    def __init__(self, history: int = 25) -> None:
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._history = history
        self._lock = threading.Lock()

    def submit(self, name: str, body: JobBody) -> Job:
        with self._lock:
            if any(j.name == name and j.status == RUNNING for j in self._jobs.values()):
                raise JobBusy(f"{name} is already running")
            job = Job(
                id=uuid.uuid4().hex[:12],
                name=name,
                status=RUNNING,
                started_at=datetime.now(UTC).isoformat(),
            )
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._prune_locked()

        def emit(line: str) -> None:
            with self._lock:
                job.lines.append(line)

        def run() -> None:
            try:
                body(emit)
                status, error = OK, None
            except Exception as exc:  # recorded on the job, never swallowed
                status = FAILED
                error = f"{type(exc).__name__}: {exc}"
                emit(traceback.format_exc().strip().splitlines()[-1])
            with self._lock:
                job.status = status
                job.error = error
                job.finished_at = datetime.now(UTC).isoformat()

        threading.Thread(target=run, name=f"job-{name}", daemon=True).start()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def recent(self) -> list[Job]:
        with self._lock:
            return [self._jobs[i] for i in reversed(self._order) if i in self._jobs]

    def running(self) -> list[str]:
        with self._lock:
            return [j.name for j in self._jobs.values() if j.status == RUNNING]

    def _prune_locked(self) -> None:
        while len(self._order) > self._history:
            oldest = self._order.pop(0)
            job = self._jobs.get(oldest)
            # never drop a job that's still running, even if it's the oldest
            if job is not None and job.status == RUNNING:
                self._order.append(oldest)
                return
            self._jobs.pop(oldest, None)
