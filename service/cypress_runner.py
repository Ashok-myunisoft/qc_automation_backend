import asyncio
import json
import queue
import shutil
import subprocess
import threading
from datetime import datetime
from pathlib import Path

_WORKSPACE = Path(__file__).resolve().parent.parent / "cypress-workspace"

# Per-run scratch — deleted after each run.
_RUNS_DIR = _WORKSPACE / "cypress" / "_runs"

# Where mochawesome writes its per-run JSON+HTML report. See cypress.config.js:
# reporterOptions.reportDir + overwrite:true — that "overwrite" is exactly why
# run_cypress() has to READ this file inside its own finally block, per run,
# before the next screen in a module loop overwrites it.
_REPORTS_DIR = _WORKSPACE / "cypress" / "reports" / "mochawesome-reports"

# Where Cypress drops screenshots (default; not overridden in cypress.config.js).
# Same overwrite hazard as the reports dir — we copy them out per run.
_SCREENSHOTS_DIR = _WORKSPACE / "cypress" / "screenshots"

# Persistent per-run stash for screenshots + report JSON, keyed by slug.
# NOT auto-cleaned per run (unlike _RUNS_DIR) — cleaned when a session's
# results are cleared (terminate/reject) via clear_stash(), or overwritten
# by the next run of the same slug.
_STASH_DIR = _WORKSPACE / "cypress" / "_stash"

TABLE_CHARS = set("┌┐└┘├┤┬┴┼─│═╞╡╥╨╫")

_NPX = shutil.which("npx") or "npx"


class CypressRunError(Exception):
    pass


def _ensure_workspace() -> None:
    if not _WORKSPACE.exists():
        raise CypressRunError(
            f"cypress-workspace/ not found at {_WORKSPACE}. "
            "It only needs: package.json, cypress.config.js, cypress.env.json, "
            "patches/ (for the postinstall patch-package step), and the "
            "cypress/pageObject/ + cypress/support/ folders — every screen's "
            "own feature+script pair is pulled fresh from GitLab on each run, "
            "no local features/stepDefinitions/fixtures tree needed."
        )


def _ensure_npm_installed() -> None:
    node_modules = _WORKSPACE / "node_modules"
    if not node_modules.exists():
        raise CypressRunError(
            "node_modules/ not found inside cypress-workspace/. "
            "Run `npm install` once inside qc-backend/cypress-workspace/ and it will be ready."
        )


def _cypress_tone(line: str) -> str:
    s = line.strip()
    if any(c in line for c in TABLE_CHARS):
        return "table"
    if s.startswith(("✓", "√")) or "passing" in line.lower():
        return "success"
    if s.startswith(("✗", "×")) or "failing" in line.lower() or "error" in line.lower():
        return "danger"
    return "secondary"


def _stream_process(proc: subprocess.Popen, out_queue: "queue.Queue"):
    try:
        for raw_line in proc.stdout:
            line = raw_line.rstrip()
            if line:
                out_queue.put(("log", line))
    finally:
        returncode = proc.wait()
        out_queue.put(("__exit__", returncode))


def _walk_mocha_suites(suites: list, out: list) -> None:
    """mochawesome nests suites inside suites arbitrarily deep. Flatten the
    tree into a single list of {suite_name, scenarios[]} entries — one per
    leaf suite that actually contains tests. Empty container suites are
    skipped, since they carry no pass/fail information a QC reviewer needs."""
    for suite in suites or []:
        tests = suite.get("tests") or []
        if tests:
            scenarios = []
            for t in tests:
                err = t.get("err") or {}
                err_message = err.get("message") or ""
                err_stack = err.get("estack") or err.get("stack") or ""
                scenarios.append({
                    "name":       t.get("title") or "(untitled scenario)",
                    "state":      t.get("state") or ("passed" if t.get("pass") else "failed"),
                    "duration":   t.get("duration") or 0,
                    "err_message": err_message,
                    "err_stack":   err_stack,
                })
            out.append({
                "suite_name": suite.get("title") or "(untitled suite)",
                "scenarios":  scenarios,
            })
        _walk_mocha_suites(suite.get("suites") or [], out)


def _read_latest_mochawesome() -> dict | None:
    """Reads mochawesome's just-written JSON report — the one FROM the run
    that just finished. Called ONCE, right after that run's process exits and
    before the next module-loop iteration starts (per the overwrite hazard
    documented above). Returns None if no report was produced (e.g. the run
    crashed before any test executed)."""
    if not _REPORTS_DIR.exists():
        return None
    candidates = sorted(_REPORTS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        return None
    try:
        return json.loads(candidates[0].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _summarize_run(mocha: dict | None, exit_code: int, slug: str,
                   stash_screenshots: list[str]) -> dict:
    """Turn the raw mochawesome JSON into the compact shape app.py stashes
    and report_builder.py renders. Always returns a dict — even for a
    crashed run with no report, so downstream code never has to null-check.
    stash_screenshots: workspace-relative paths of screenshots already
    copied into _STASH_DIR."""
    result = {
        "slug":        slug,
        "exit_code":   exit_code,
        "passed":      exit_code == 0,
        "duration":    0,
        "stats":       {"passes": 0, "failures": 0, "pending": 0, "tests": 0},
        "suites":      [],
        "screenshots": stash_screenshots,
    }
    if not mocha:
        return result

    stats = mocha.get("stats") or {}
    result["duration"] = stats.get("duration") or 0
    result["stats"] = {
        "passes":   stats.get("passes")   or 0,
        "failures": stats.get("failures") or 0,
        "pending":  stats.get("pending")  or 0,
        "tests":    stats.get("tests")    or 0,
    }
    flat: list = []
    for r in mocha.get("results") or []:
        _walk_mocha_suites(r.get("suites") or [], flat)
    result["suites"] = flat
    return result


def _stash_screenshots(slug: str) -> list[str]:
    """Copy every screenshot the just-finished run produced into the
    persistent stash (keyed by slug), returning workspace-relative paths.
    Returns [] if none exist. Deletes the source Cypress screenshot tree
    afterward so the next module iteration starts clean."""
    if not _SCREENSHOTS_DIR.exists():
        return []

    stash_target = _STASH_DIR / slug
    if stash_target.exists():
        shutil.rmtree(stash_target, ignore_errors=True)
    stash_target.mkdir(parents=True, exist_ok=True)

    stashed: list[str] = []
    for src in _SCREENSHOTS_DIR.rglob("*.png"):
        rel = src.relative_to(_SCREENSHOTS_DIR)
        dst = stash_target / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(src, dst)
            stashed.append(str(dst.relative_to(_WORKSPACE)).replace("\\", "/"))
        except OSError:
            continue

    shutil.rmtree(_SCREENSHOTS_DIR, ignore_errors=True)
    return stashed


def clear_stash(slugs: list[str] | None = None) -> None:
    """Called from app.py on terminate/reject to wipe stashed screenshots.
    slugs=None wipes the whole _STASH_DIR."""
    if not _STASH_DIR.exists():
        return
    if slugs is None:
        shutil.rmtree(_STASH_DIR, ignore_errors=True)
        return
    for slug in slugs:
        target = _STASH_DIR / slug
        shutil.rmtree(target, ignore_errors=True)


def screenshot_absolute_path(rel_path: str) -> Path:
    """report_builder.py uses this to resolve a stashed-screenshot path
    (which app.py sends as workspace-relative) back to an absolute filesystem
    path for base64-embedding into the final HTML report."""
    return _WORKSPACE / rel_path


async def run_cypress(session: dict, feature_content: str, script_content: str, slug: str,
                       fixtures: dict | None = None):
    """
    Runs ONE screen. Yields:
      ("log",    line, tone)              — every stdout line
      ("exit",   returncode, None)        — final exit code (kept for backward
                                             compat with existing handle_run
                                             consumers)
      ("result", run_summary_dict, None)  — parsed mochawesome + screenshot
                                             paths. Emitted ONCE, immediately
                                             after "exit", so handle_run can
                                             stash it before the next
                                             module-loop iteration overwrites
                                             the mochawesome file.
    """
    _ensure_workspace()
    _ensure_npm_installed()

    if fixtures:
        fixtures_dir = _WORKSPACE / "cypress" / "fixtures"
        fixtures_dir.mkdir(parents=True, exist_ok=True)
        for filename, content in fixtures.items():
            (fixtures_dir / filename).write_text(content, encoding="utf-8")

    run_dir = _RUNS_DIR / slug
    if run_dir.exists():
        shutil.rmtree(run_dir, ignore_errors=True)
    run_dir.mkdir(parents=True, exist_ok=True)

    feature_file = run_dir / f"{slug}.feature"
    script_file = run_dir / f"{slug}.js"
    feature_file.write_text(feature_content, encoding="utf-8")
    script_file.write_text(script_content, encoding="utf-8")

    spec_path = str(feature_file.relative_to(_WORKSPACE)).replace("\\", "/")

    out_queue: "queue.Queue" = queue.Queue()
    started_at = datetime.utcnow().isoformat() + "Z"

    try:
        proc = subprocess.Popen(
            [_NPX, "cypress", "run", "--spec", spec_path],
            cwd=str(_WORKSPACE),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except FileNotFoundError as e:
        shutil.rmtree(run_dir, ignore_errors=True)
        raise CypressRunError(
            f"couldn't launch npx ({e}) — is Node.js/npm installed and on PATH "
            "for the account running this backend?"
        )

    session["process"] = proc

    reader_thread = threading.Thread(target=_stream_process, args=(proc, out_queue), daemon=True)
    reader_thread.start()

    loop = asyncio.get_event_loop()
    returncode: int | None = None

    try:
        while True:
            kind, payload = await loop.run_in_executor(None, out_queue.get)
            if kind == "log":
                yield ("log", payload, _cypress_tone(payload))
            else:
                returncode = payload
                yield ("exit", returncode, None)
                break

        # Read report JSON + copy screenshots BEFORE the next module-loop
        # iteration overwrites the mochawesome file. This is the whole reason
        # capture is inline in run_cypress rather than after all runs.
        mocha = _read_latest_mochawesome()
        screenshots = _stash_screenshots(slug)
        summary = _summarize_run(mocha, returncode, slug, screenshots)
        summary["started_at"] = started_at
        yield ("result", summary, None)

    except asyncio.CancelledError:
        try:
            proc.kill()
        except Exception:
            pass
        raise

    finally:
        session["process"] = None
        reader_thread.join(timeout=2)
        shutil.rmtree(run_dir, ignore_errors=True)