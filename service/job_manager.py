"""Background jobs that keep running after the browser tab is closed.

Analogy: the WebSocket is the service window, not the kitchen. A job is an
order the kitchen finishes by itself. While the window is open, updates are
passed through to the browser exactly as before; once it closes, updates are
simply not delivered and the kitchen carries on.

Three pieces live here:
  * JobSocket - a stand-in for the WebSocket that never fails when the
    browser has gone away, and also copies every log line into the backend
    log and the job's SQL Server row (via job_store).
  * Job       - one queued/running background run.
  * A concurrency gate (MAX_CONCURRENT_RUNS, default 1) - Cypress is heavy
    and all runs still share one Cypress workspace, so extra runs wait their
    turn in the queue.
"""

import asyncio
import itertools
import logging
import os
import re
import time

from service import job_store

logger = logging.getLogger(__name__)

MAX_CONCURRENT_RUNS = max(1, int(os.getenv("MAX_CONCURRENT_RUNS", "1")))

_HEARTBEAT_SECONDS = 5     # how often pending progress is written to SQL Server
_TOUCH_SECONDS = 30        # write at least this often so the row never looks dead

_counter = itertools.count(1)
_JOBS: dict[int, "Job"] = {}
_semaphore: asyncio.Semaphore | None = None

_COUNTER_RE = re.compile(r"^\[(\d+)/(\d+)\]")


def _gate() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(MAX_CONCURRENT_RUNS)
    return _semaphore


class JobSocket:
    """Looks like a WebSocket to the handlers (send_json only)."""

    def __init__(self, ws, job: "Job"):
        self._ws = ws
        self._job = job

    def detach(self) -> None:
        self._ws = None

    @property
    def attached(self) -> bool:
        return self._ws is not None

    async def send_json(self, data: dict) -> None:
        try:
            self._job.observe(data)
        except Exception:
            logger.exception("job observe failed")
        ws = self._ws
        if ws is None:
            return
        try:
            await ws.send_json(data)
        except Exception:
            # The browser went away between checks. Keep the job going.
            self._ws = None
            logger.info("[job %s] browser disconnected - continuing in the background", self._job.key)


class Job:
    def __init__(self, *, session: dict, ws, source: str, module: str, screen: str, queued_phase: str):
        self.key = next(_counter)
        self.db_id: int | None = None
        self.session = session
        self.source = source
        self.module = module
        self.screen = screen
        self.queued_phase = queued_phase
        self.socket = JobSocket(ws, self)
        self.task: asyncio.Task | None = None

        self.status = "queued"
        self.stage = ""
        self.counter = ""
        self.last_log = ""
        self.last_error = ""
        self.started_real = False
        self.dirty = True

    # ---- lifecycle helpers -------------------------------------------------

    @property
    def active(self) -> bool:
        return self.task is not None and not self.task.done()

    def detach(self) -> None:
        """The browser closed. The job keeps running."""
        self.socket.detach()
        logger.info("[job %s] browser closed - %s run for %s continues in the backend",
                    self.key, self.source, self.module or "(no module)")

    @property
    def progress(self) -> str:
        if self.status == "queued":
            return "Waiting for a free run slot"
        if self.stage and self.counter:
            return f"{self.stage} — screen {self.counter}"
        return self.stage

    def observe(self, data: dict) -> None:
        """Called for every message the handlers send. Cheap and non-blocking."""
        kind = data.get("type")
        if kind == "log":
            text = str(data.get("text") or "")
            logger.info("[job %s] %s", self.key, text)
            self.last_log = text
            self._track_stage(text)
            self.dirty = True
        elif kind == "error":
            message = str(data.get("message") or "")
            logger.warning("[job %s] ERROR: %s", self.key, message)
            self.last_error = message
            self.last_log = f"ERROR: {message}"
            self.dirty = True
        elif kind == "status":
            if self.status == "running" and data.get("phase") in ("running", "auto_running"):
                self.started_real = True

    def _track_stage(self, text: str) -> None:
        low = text.lower()
        if low.startswith("auto-run started"):
            self.stage, self.counter = "Starting", ""
        elif low.startswith(("checking the qc repo", "reading source", "scanning")):
            self.stage, self.counter = "Discovering screens", ""
        elif low.startswith("generating and pushing"):
            self.stage, self.counter = "Generating & pushing", ""
        elif low.startswith("cypress:"):
            self.stage, self.counter = "Running tests", ""
        elif low.startswith(("module run complete", "report ready")):
            self.stage, self.counter = "Building report", ""
        m = _COUNTER_RE.match(text)
        if m:
            self.counter = f"{m.group(1)}/{m.group(2)}"

    # ---- SQL Server bookkeeping (never allowed to break a run) ------------

    async def _flush(self) -> None:
        if self.db_id is None:
            return
        self.dirty = False
        try:
            await asyncio.to_thread(
                job_store.update_job, self.db_id,
                status=self.status, progress=self.progress, last_log=self.last_log,
            )
        except Exception:
            logger.warning("[job %s] could not update job row", self.key, exc_info=True)

    async def _heartbeat(self) -> None:
        last_write = time.monotonic()
        while True:
            await asyncio.sleep(_HEARTBEAT_SECONDS)
            if self.dirty or time.monotonic() - last_write >= _TOUCH_SECONDS:
                await self._flush()
                last_write = time.monotonic()

    async def _finalize(self, outcome: str) -> None:
        """outcome: 'done' | 'error' | 'cancelled'"""
        if self.db_id is None:
            return
        try:
            history_saved = bool(self.session.pop("_history_saved", False))
            if outcome == "cancelled" or history_saved:
                # Cancelled runs are discarded; finished runs now live in History.
                await asyncio.to_thread(job_store.delete_job, self.db_id)
            elif self.started_real or outcome == "error":
                message = self.last_error or self.last_log or "run ended without results"
                await asyncio.to_thread(job_store.mark_failed, self.db_id, message)
            else:
                # Rejected before it really started (no env set, nothing to run...).
                await asyncio.to_thread(job_store.delete_job, self.db_id)
        except Exception:
            logger.warning("[job %s] could not finalize job row", self.key, exc_info=True)


async def _execute(job: Job, runner) -> None:
    heartbeat = None
    outcome = "error"
    try:
        try:
            job.db_id = await asyncio.to_thread(
                job_store.create_job, job.source, job.module, job.screen, "queued"
            )
        except Exception:
            logger.warning("[job %s] could not create job row - running without one", job.key, exc_info=True)
        heartbeat = asyncio.create_task(job._heartbeat())

        gate = _gate()
        if gate.locked():
            await job.socket.send_json({
                "type": "log",
                "text": "another run is in progress — this one is queued and will start automatically.",
                "tone": "accent",
            })
            await job.socket.send_json({"type": "status", "phase": job.queued_phase})
            await job._flush()

        async with gate:
            job.status = "running"
            job.dirty = True
            job.session["_history_saved"] = False
            await job._flush()
            await runner(job.socket)
        outcome = "done"
    except asyncio.CancelledError:
        outcome = "cancelled"
        try:
            await job.socket.send_json({"type": "status", "phase": "idle"})
        except Exception:
            pass
        raise
    except Exception as e:
        logger.exception("[job %s] crashed", job.key)
        job.last_error = f"{type(e).__name__}: {e}"
        try:
            await job.socket.send_json({"type": "error", "message": f"run crashed: {e}"})
        except Exception:
            pass
    finally:
        if heartbeat is not None:
            heartbeat.cancel()
        await job._finalize(outcome)
        _JOBS.pop(job.key, None)


def start_job(*, session: dict, ws, source: str, module: str, screen: str,
              queued_phase: str, runner) -> Job:
    """Start `runner(job_socket)` as a background job owned by the backend,
    not by the WebSocket. Returns immediately."""
    job = Job(session=session, ws=ws, source=source, module=module or "",
              screen=screen or "", queued_phase=queued_phase)
    _JOBS[job.key] = job
    job.task = asyncio.create_task(_execute(job, runner))
    return job


def any_active() -> bool:
    return any(j.active for j in _JOBS.values())


def _kill_process(job: Job) -> None:
    proc = job.session.get("process")
    if proc is not None:
        try:
            proc.kill()
        except Exception:
            pass


def cancel_job(db_id: int) -> bool:
    """Cancel a job running on THIS backend process (used by the History page)."""
    for job in list(_JOBS.values()):
        if job.db_id == db_id and job.active:
            _kill_process(job)
            job.task.cancel()
            return True
    return False