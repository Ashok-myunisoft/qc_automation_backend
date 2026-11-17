import asyncio
import itertools
from collections import deque
import logging
import os
import re
import time

from service import job_store
from service.cypress_runner import kill_process_tree, process_snapshot

logger = logging.getLogger(__name__)

MAX_CONCURRENT_RUNS = max(1, int(os.getenv("MAX_CONCURRENT_RUNS", "1")))

_HEARTBEAT_SECONDS = 5     # how often pending progress is written to SQL Server
_TOUCH_SECONDS = 30        # write at least this often so the row never looks dead

_counter = itertools.count(1)
_JOBS: dict[int, "Job"] = {}
_semaphore: asyncio.Semaphore | None = None

_MAX_MISSED = 5000        # messages kept for the browser while it is away (replayed on reconnect)

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
        # Everything the browser did not receive while it was disconnected. It is replayed, in
        # order, when the same browser reconnects, so a run that finished during a network blip
        # still ends with its final status / result on screen.
        self._missed: deque = deque(maxlen=_MAX_MISSED)

    def detach(self) -> None:
        self._ws = None

    @property
    def attached(self) -> bool:
        return self._ws is not None

    def pending(self) -> int:
        return len(self._missed)

    async def attach(self, ws) -> None:
        """A (re)connected browser takes over this job: replay what it missed, then go live.
        New messages that arrive while replaying are buffered too, so order is kept."""
        while self._missed:
            await ws.send_json(self._missed.popleft())
        self._ws = ws

    async def send_json(self, data: dict) -> None:
        try:
            self._job.observe(data)
        except Exception:
            logger.exception("job observe failed")
        ws = self._ws
        if ws is None:
            self._missed.append(data)
            return
        try:
            await ws.send_json(data)
        except Exception:
            # The browser went away between checks. Keep the job going.
            self._ws = None
            self._missed.append(data)
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
        self.db_task: asyncio.Task | None = None   # background create_job, never awaited before the run

        self.status = "queued"
        self.stage = ""
        self.counter = ""
        self.last_log = ""
        self.last_error = ""
        self.started_real = False
        self.dirty = True
        self.dedupe_key = ""        # same key = same piece of work (see start_job)
        self.reject_reason = ""     # set when this job is a duplicate and must not run

    # ---- lifecycle helpers -------------------------------------------------

    @property
    def active(self) -> bool:
        return self.task is not None and not self.task.done()

    @property
    def blocks_duplicates(self) -> bool:
        """True while this job is really queued/running. A job that is being cancelled, or that was
        itself refused as a duplicate, no longer blocks the same work from being started again."""
        if self.reject_reason or not self.active:
            return False
        cancelling = getattr(self.task, "cancelling", None)
        return not (cancelling is not None and cancelling() > 0)

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
        if self.db_task is not None and not self.db_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self.db_task), timeout=12)
            except Exception:
                pass
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


async def _log_processes(job: Job, when: str) -> None:
    """Writes how many processes exist to the log. Only for visibility - never breaks a run."""
    try:
        snap = await asyncio.wait_for(asyncio.to_thread(process_snapshot), timeout=10)
        logger.info("[job %s] processes %s: total=%s cypress=%s zombies=%s",
                    job.key, when, snap.get("total"), snap.get("cypress"), snap.get("zombies"))
        if when == "at end" and snap.get("cypress"):
            others = [j for j in _JOBS.values() if j is not job and j.active]
            if not others:
                logger.warning("[job %s] %s Cypress-related process(es) are still alive although no other job "
                               "is running - they may be left over (a data-cy scan can also be the reason).",
                               job.key, snap.get("cypress"))
    except Exception:
        pass


async def _create_row(job: Job) -> None:
    """Background: write the job row. A slow or unreachable DB only means the
    run has no row; it never delays or breaks the run itself."""
    try:
        job.db_id = await asyncio.to_thread(
            job_store.create_job, job.source, job.module, job.screen, "queued"
        )
        job.dirty = True
    except Exception:
        logger.warning("[job %s] could not create job row - running without one", job.key, exc_info=True)


async def _execute(job: Job, runner) -> None:
    heartbeat = None
    outcome = "error"
    try:
        if job.reject_reason:
            # Same work is already queued or running: do nothing, just tell the browser.
            outcome = "rejected"
            await job.socket.send_json({"type": "error", "message": job.reject_reason})
            await job.socket.send_json({
                "type": "status",
                "phase": "awaiting_review" if job.source in ("fetch", "generate") else "idle",
            })
            return

        # FIRST thing the browser hears, before any slow work (DB, GitLab, Cypress).
        await job.socket.send_json({"type": "status", "phase": job.queued_phase})
        await job.socket.send_json({"type": "log", "text": "run request received - starting...", "tone": "secondary"})

        job.db_task = asyncio.create_task(_create_row(job))
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
            await _log_processes(job, "at start")
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
        if outcome != "rejected":
            await _log_processes(job, "at end")
        await job._finalize(outcome)
        _JOBS.pop(job.key, None)


# Runs that count as the same kind of work when checking for duplicates.
_DEDUPE_FAMILY = {"fetch": "run", "generate": "run"}


def start_job(*, session: dict, ws, source: str, module: str, screen: str,
              queued_phase: str, runner, dedupe_key: str | None = None) -> Job:
    """Start `runner(job_socket)` as a background job owned by the backend,
    not by the WebSocket. Returns immediately.

    If the same work (same kind + `dedupe_key`, default module + screen) is already queued or
    running - from any browser or user - the new job is refused: it only tells the browser so
    and never runs."""
    job = Job(session=session, ws=ws, source=source, module=module or "",
              screen=screen or "", queued_phase=queued_phase)
    key = dedupe_key if dedupe_key is not None else f"{job.module}|{job.screen}"
    job.dedupe_key = f"{_DEDUPE_FAMILY.get(source, source)}|{key}"
    for other in _JOBS.values():
        if other.dedupe_key == job.dedupe_key and other.blocks_duplicates:
            target = " / ".join(p for p in (job.module, job.screen) if p) or "this request"
            job.reject_reason = (f"{target} is already queued or running, so this duplicate request was ignored. "
                                 "Wait for it to finish.")
            break
    _JOBS[job.key] = job
    job.task = asyncio.create_task(_execute(job, runner))
    return job


def any_active() -> bool:
    return any(j.active for j in _JOBS.values())


def _kill_process(job: Job) -> None:
    proc = job.session.get("process")
    if proc is not None:
        kill_process_tree(proc)


def cancel_job(db_id: int) -> bool:
    """Cancel a job running on THIS backend process (used by the History page)."""
    for job in list(_JOBS.values()):
        if job.db_id == db_id and job.active:
            _kill_process(job)
            job.task.cancel()
            return True
    return False