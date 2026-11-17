import asyncio
import atexit
import json
import logging
import os
import queue
import re
import shutil
import signal
import subprocess
import threading
from datetime import datetime
from pathlib import Path
_WORKSPACE = Path(__file__).resolve().parent.parent / 'cypress-workspace'
_RUNS_DIR = _WORKSPACE / 'cypress' / '_runs'
_REPORTS_DIR = _WORKSPACE / 'cypress' / 'reports' / 'mochawesome-reports'
_SCREENSHOTS_DIR = _WORKSPACE / 'cypress' / 'screenshots'
_STASH_DIR = _WORKSPACE / 'cypress' / '_stash'
TABLE_CHARS = set('┌┐└┘├┤┬┴┼─│═╞╡╥╨╫')
_NPX = shutil.which('npx') or 'npx'
 
# Cypress/Node decide whether to emit ANSI color codes based on whether
# they detect a color-capable TTY on their end — this can vary by
# environment (confirmed: same command produced clean text on one machine,
# raw \x1b[...m escape sequences on another server) regardless of the fact
# that neither actually reaches a real terminal here; the output is piped
# through this subprocess, queued, and sent to the frontend over a
# WebSocket, which has no ANSI renderer. FORCE_COLOR/NO_COLOR below try to
# stop Cypress from emitting them in the first place; this regex is the
# defensive backstop that strips them from whatever text arrives anyway,
# so a change in Cypress/Node/terminal-detection behavior on some future
# environment can't reintroduce the same garbled-log symptom. Coloring in
# the UI already comes from _cypress_tone()'s tone below, not from these
# codes, so stripping them loses nothing.
_ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')
 
class CypressRunError(Exception):
    pass

logger = logging.getLogger(__name__)


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except ValueError:
        return default


# --------------------------------------------------------------------------
# Limits (all can be changed in .env, nothing needs to be set for the defaults)
# --------------------------------------------------------------------------
# A single Cypress run that takes longer than this is stopped (its browser too). 0 = no limit.
CYPRESS_RUN_TIMEOUT_SECONDS = max(0, _int_env("CYPRESS_RUN_TIMEOUT_MINUTES", 60)) * 60
# How many Cypress processes may run at the same moment (test runs AND the data-cy scan share this).
MAX_CONCURRENT_CYPRESS = max(1, _int_env("MAX_CONCURRENT_CYPRESS", _int_env("MAX_CONCURRENT_RUNS", 1)))
# If Cypress itself has exited but something it started still keeps the output open, clean it up after this.
_STRAY_GRACE_SECONDS = 10
_TICK_SECONDS = 1.0

_slot = threading.BoundedSemaphore(MAX_CONCURRENT_CYPRESS)


def acquire_slot_blocking(timeout: float) -> bool:
    """For code that runs in a thread (the data-cy scan). True = you now hold the Cypress slot."""
    return _slot.acquire(timeout=timeout)


def release_slot() -> None:
    try:
        _slot.release()
    except ValueError:
        pass


# --------------------------------------------------------------------------
# Killing a whole Cypress run (npx -> node -> Cypress -> Electron/Chrome ...)
# --------------------------------------------------------------------------
_LIVE: set = set()
_LIVE_LOCK = threading.Lock()


def kill_process_tree(proc) -> None:
    """Stops `proc` AND everything it started. proc.kill() alone only stops npx and leaves the
    browser running. Safe to call more than once and on a process that is already gone."""
    if proc is None:
        return
    try:
        if os.name == "nt":
            if proc.poll() is not None:
                return          # Windows reuses process ids quickly - never taskkill a finished one
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=15)
        else:
            # The run was started in its own process group (group id == proc.pid), so this
            # reaches every child even if npx itself is already gone.
            os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def kill_all_live() -> None:
    """Server is stopping: do not leave any Cypress / browser behind."""
    with _LIVE_LOCK:
        procs = list(_LIVE)
    for p in procs:
        kill_process_tree(p)


atexit.register(kill_all_live)


# --------------------------------------------------------------------------
# Process counting (for the logs only - this never changes behaviour)
# --------------------------------------------------------------------------
def process_snapshot() -> dict:
    """{'total': all processes on this machine/container, 'cypress': Cypress/Electron related ones,
    'zombies': finished processes nobody has collected}. Any value may be None if it can't be read."""
    snap = {"total": None, "cypress": None, "zombies": None}
    try:
        if os.name == "nt":
            out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True,
                                 text=True, timeout=15).stdout
            names = [ln.split('","')[0].strip('"').lower() for ln in out.splitlines() if ln.strip()]
            snap["total"] = len(names)
            snap["cypress"] = sum(1 for n in names if n == "cypress.exe")
        elif os.path.isdir("/proc"):
            total = cy = zombies = 0
            me = str(os.getpid())
            for entry in os.listdir("/proc"):
                if not entry.isdigit():
                    continue
                total += 1
                try:
                    with open(f"/proc/{entry}/stat", "rb") as fh:
                        stat = fh.read(2048).decode("utf-8", "replace")
                    if stat.rsplit(")", 1)[-1].strip()[:1] == "Z":
                        zombies += 1
                        continue
                    if entry == me:
                        continue
                    with open(f"/proc/{entry}/cmdline", "rb") as fh:
                        cmd = fh.read(4096).replace(b"\0", b" ").decode("utf-8", "replace").lower()
                    if "cypress" in cmd:
                        cy += 1
                except OSError:
                    continue
            snap.update(total=total, cypress=cy, zombies=zombies)
    except Exception:
        pass
    return snap


class _LoopQueue:
    """Hands lines from the reader thread to the event loop without parking a worker thread."""

    def __init__(self, loop):
        self._loop = loop
        self._q: asyncio.Queue = asyncio.Queue()

    def put(self, item) -> None:          # called from the reader thread
        try:
            self._loop.call_soon_threadsafe(self._q.put_nowait, item)
        except RuntimeError:
            pass                           # event loop already closed (server stopping)

    async def get(self):
        return await self._q.get()
 
def _ensure_workspace() -> None:
    if not _WORKSPACE.exists():
        raise CypressRunError(f"cypress-workspace/ not found at {_WORKSPACE}. It only needs: package.json, cypress.config.js, cypress.env.json, patches/ (for the postinstall patch-package step), and the cypress/pageObject/ + cypress/support/ folders — every screen's own feature+script pair is pulled fresh from GitLab on each run, no local features/stepDefinitions/fixtures tree needed.")
 
def _ensure_npm_installed() -> None:
    node_modules = _WORKSPACE / 'node_modules'
    if not node_modules.exists():
        raise CypressRunError('node_modules/ not found inside cypress-workspace/. Run `npm install` once inside qc-backend/cypress-workspace/ and it will be ready.')
 
def _cypress_tone(line: str) -> str:
    s = line.strip()
    if any((c in line for c in TABLE_CHARS)):
        return 'table'
    if s.startswith(('✓', '√')) or 'passing' in line.lower():
        return 'success'
    if s.startswith(('✗', '×')) or 'failing' in line.lower() or 'error' in line.lower():
        return 'danger'
    return 'secondary'
 
def _stream_process(proc: subprocess.Popen, out_queue: 'queue.Queue'):
    try:
        for raw_line in proc.stdout:
            line = _ANSI_RE.sub('', raw_line.rstrip())
            if line:
                out_queue.put(('log', line))
    finally:
        returncode = proc.wait()
        out_queue.put(('__exit__', returncode))
 
def _walk_mocha_suites(suites: list, out: list) -> None:
    for suite in suites or []:
        tests = suite.get('tests') or []
        if tests:
            scenarios = []
            for t in tests:
                err = t.get('err') or {}
                err_message = err.get('message') or ''
                err_stack = err.get('estack') or err.get('stack') or ''
                scenarios.append({'name': t.get('title') or '(untitled scenario)', 'state': t.get('state') or ('passed' if t.get('pass') else 'failed'), 'duration': t.get('duration') or 0, 'err_message': err_message, 'err_stack': err_stack})
            out.append({'suite_name': suite.get('title') or '(untitled suite)', 'scenarios': scenarios})
        _walk_mocha_suites(suite.get('suites') or [], out)
 
def _read_latest_mochawesome() -> dict | None:
    if not _REPORTS_DIR.exists():
        return None
    candidates = sorted(_REPORTS_DIR.glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        return None
    try:
        return json.loads(candidates[0].read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return None
 
def _summarize_run(mocha: dict | None, exit_code: int, slug: str, stash_screenshots: list[str]) -> dict:
    result = {'slug': slug, 'exit_code': exit_code, 'passed': exit_code == 0, 'duration': 0, 'stats': {'passes': 0, 'failures': 0, 'pending': 0, 'tests': 0}, 'suites': [], 'screenshots': stash_screenshots}
    if not mocha:
        return result
    stats = mocha.get('stats') or {}
    result['duration'] = stats.get('duration') or 0
    result['stats'] = {'passes': stats.get('passes') or 0, 'failures': stats.get('failures') or 0, 'pending': stats.get('pending') or 0, 'tests': stats.get('tests') or 0}
    flat: list = []
    for r in mocha.get('results') or []:
        _walk_mocha_suites(r.get('suites') or [], flat)
    result['suites'] = flat
    return result
 
def _stash_screenshots(slug: str) -> list[str]:
    if not _SCREENSHOTS_DIR.exists():
        return []
    stash_target = _STASH_DIR / slug
    if stash_target.exists():
        shutil.rmtree(stash_target, ignore_errors=True)
    stash_target.mkdir(parents=True, exist_ok=True)
    stashed: list[str] = []
    for src in _SCREENSHOTS_DIR.rglob('*.png'):
        rel = src.relative_to(_SCREENSHOTS_DIR)
        dst = stash_target / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(src, dst)
            stashed.append(str(dst.relative_to(_WORKSPACE)).replace('\\', '/'))
        except OSError:
            continue
    shutil.rmtree(_SCREENSHOTS_DIR, ignore_errors=True)
    return stashed
 
def clear_stash(slugs: list[str] | None=None) -> None:
    if not _STASH_DIR.exists():
        return
    if slugs is None:
        shutil.rmtree(_STASH_DIR, ignore_errors=True)
        return
    for slug in slugs:
        target = _STASH_DIR / slug
        shutil.rmtree(target, ignore_errors=True)
 
def screenshot_absolute_path(rel_path: str) -> Path:
    return _WORKSPACE / rel_path
 
def _build_env_overrides(test_env: dict | None) -> list[str]:
    """Turns the frontend-supplied {baseUrl, dbName, userName, password}
    into Cypress CLI flags that override cypress.config.js's baseUrl and
    cypress.env.json's TestConnections.UILogin for this run only — nothing
    is written back to either file. Cypress.env('TestConnections').UILogin
    and Cypress.config('baseUrl') inside the generated script resolve
    identically whether the value came from a file or a CLI flag, so no
    generated script or prompt needs to change for this to work.
 
    --config takes plain comma-separated key=value pairs (baseUrl has no
    commas, so a single pair is safe as-is). --env accepts a single JSON
    object as its whole argument and merges it into Cypress.env() at the
    top level — passing {"TestConnections": {...}} here fully replaces
    just that one top-level key, which is all UILogin-reading scripts
    ever look at.
 
    Returns [] if test_env is falsy — callers in app.py are expected to
    have already refused to run without one (full-replace, no fallback),
    but this stays defensive rather than crashing the run."""
    if not test_env:
        return []
    base_url = test_env.get("baseUrl", "")
    env_payload = json.dumps({
        "TestConnections": {
            "UILogin": {
                "dbName":   test_env.get("dbName", ""),
                "userName": test_env.get("userName", ""),
                "password": test_env.get("password", ""),
            }
        }
    })
    return ["--config", f"baseUrl={base_url}", "--env", env_payload]
 
 
async def run_cypress(session: dict, feature_content: str, script_content: str, slug: str,
                       fixtures: dict | None=None, test_env: dict | None=None):
    _ensure_workspace()
    _ensure_npm_installed()

    # Only MAX_CONCURRENT_CYPRESS Cypress processes at a time (shared with the data-cy scan).
    if not _slot.acquire(blocking=False):
        yield ('log', 'waiting for another Cypress run or scan to finish...', 'secondary')
        while not _slot.acquire(blocking=False):
            await asyncio.sleep(0.25)

    run_dir = _RUNS_DIR / slug
    proc = None
    reader_thread = None
    getter = None
    try:
        if fixtures:
            fixtures_dir = _WORKSPACE / 'cypress' / 'fixtures'
            fixtures_dir.mkdir(parents=True, exist_ok=True)
            for filename, content in fixtures.items():
                (fixtures_dir / filename).write_text(content, encoding='utf-8')
        if run_dir.exists():
            shutil.rmtree(run_dir, ignore_errors=True)
        run_dir.mkdir(parents=True, exist_ok=True)
        feature_file = run_dir / f'{slug}.feature'
        script_file = run_dir / f'{slug}.js'
        feature_file.write_text(feature_content, encoding='utf-8')
        script_file.write_text(script_content, encoding='utf-8')
        spec_path = str(feature_file.relative_to(_WORKSPACE)).replace('\\', '/')
        loop = asyncio.get_running_loop()
        aq = _LoopQueue(loop)
        started_at = datetime.utcnow().isoformat() + 'Z'
        cmd = [_NPX, 'cypress', 'run', '--spec', spec_path] + _build_env_overrides(test_env)
        # Ask Cypress/Node not to emit ANSI color codes at all - belt-and-braces
        # with the _ANSI_RE stripping in _stream_process above, since which
        # environment variable(s) an underlying tool actually respects can vary.
        proc_env = {**os.environ, 'FORCE_COLOR': '0', 'NO_COLOR': '1'}
        popen_kwargs = dict(cwd=str(_WORKSPACE), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding='utf-8', errors='replace', bufsize=1, env=proc_env)
        if os.name != 'nt':
            # Own process group, so the whole run (npx, node, Cypress, the browser) can be stopped together.
            popen_kwargs['start_new_session'] = True
        try:
            proc = subprocess.Popen(cmd, **popen_kwargs)
        except FileNotFoundError as e:
            raise CypressRunError(f"couldn't launch npx ({e}) - is Node.js/npm installed and on PATH for the account running this backend?")
        with _LIVE_LOCK:
            _LIVE.add(proc)
        session['process'] = proc
        reader_thread = threading.Thread(target=_stream_process, args=(proc, aq), daemon=True)
        reader_thread.start()

        deadline = (loop.time() + CYPRESS_RUN_TIMEOUT_SECONDS) if CYPRESS_RUN_TIMEOUT_SECONDS else None
        leader_gone_at = None
        swept = False
        timed_out = False
        returncode: int | None = None
        while True:
            if getter is None:
                getter = asyncio.ensure_future(aq.get())
            done, _ = await asyncio.wait({getter}, timeout=_TICK_SECONDS)
            if done:
                kind, payload = getter.result()
                getter = None
                if kind == 'log':
                    yield ('log', payload, _cypress_tone(payload))
                    continue
                returncode = payload
                # Cypress has finished: make sure no browser / helper it started is left running.
                kill_process_tree(proc)
                yield ('exit', returncode, None)
                break
            now = loop.time()
            if deadline is not None and now >= deadline:
                timed_out = True
                yield ('log', f'this run took longer than {CYPRESS_RUN_TIMEOUT_SECONDS // 60} minute(s) - stopping Cypress and its browser.', 'danger')
                kill_process_tree(proc)
                returncode = 124
                yield ('exit', returncode, None)
                break
            if proc.poll() is not None and not swept:
                # Cypress is gone but its output is still held open by something it left behind.
                leader_gone_at = leader_gone_at if leader_gone_at is not None else now
                if now - leader_gone_at >= _STRAY_GRACE_SECONDS:
                    swept = True
                    kill_process_tree(proc)

        # File reads / screenshot copying / parsing: off the event loop, so the WebSocket keeps
        # answering its keepalive pings while a big run is being summarised.
        # A run that was stopped for taking too long has no trustworthy report (it could be an older run's).
        mocha = None if timed_out else await asyncio.to_thread(_read_latest_mochawesome)
        screenshots = await asyncio.to_thread(_stash_screenshots, slug)
        summary = await asyncio.to_thread(_summarize_run, mocha, returncode, slug, screenshots)
        summary['started_at'] = started_at
        summary['feature_text'] = feature_content
        yield ('result', summary, None)
    except asyncio.CancelledError:
        kill_process_tree(proc)
        raise
    finally:
        if getter is not None and not getter.done():
            getter.cancel()
        session['process'] = None
        if proc is not None:
            if proc.poll() is None:
                kill_process_tree(proc)      # whatever got us here, never leave it running
            with _LIVE_LOCK:
                _LIVE.discard(proc)
        if reader_thread is not None:
            reader_thread.join(timeout=2)
        shutil.rmtree(run_dir, ignore_errors=True)
        release_slot()