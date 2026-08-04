import asyncio
import queue
import shutil
import subprocess
import threading
from pathlib import Path

_WORKSPACE = Path(__file__).resolve().parent.parent / "cypress-workspace"

# Deliberately NOT under cypress/src/ — this workspace no longer keeps a
# src/ tree at all (config/env/cypress.config.js + pageObject/ + support/
# are the only things that need to exist ahead of time). Each run creates
# exactly cypress/_runs/<slug>/ on demand and deletes it when done — nothing
# here ever requires src/ to exist, and nothing here ever touches
# pageObject/, support/, cypress.config.js, cypress.env.json, or
# package.json.
_RUNS_DIR = _WORKSPACE / "cypress" / "_runs"

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


async def run_cypress(session: dict, feature_content: str, script_content: str, slug: str):
    """
    Runs ONE screen in complete isolation, touching NOTHING in the workspace
    except its own throwaway run folder.

    feature_content / script_content: the screen's self-contained pair,
      already fetched/generated — pulled fresh from GitLab (or freshly
      generated) every single call, never read from local disk.

    slug: filesystem-safe screen identifier, used only to name this run's
      folder at cypress/_runs/<slug>/ — never a real repo path.

    Lifecycle, every run:
      1. cypress/_runs/<slug>/ created (mkdir parents=True — this is the
         ONLY folder ever created; package.json, cypress.config.js,
         cypress.env.json, cypress/pageObject/, cypress/support/ are never
         touched, read-modified, or recreated).
      2. <slug>.feature and <slug>.js written into it.
      3. Cypress runs against just that spec.
      4. The ENTIRE cypress/_runs/<slug>/ folder is deleted in `finally`,
         unconditionally — pass, fail, error, or cancellation. Nothing
         pulled for a run is ever left behind.
    """
    _ensure_workspace()
    _ensure_npm_installed()

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

    try:
        while True:
            kind, payload = await loop.run_in_executor(None, out_queue.get)
            if kind == "log":
                yield ("log", payload, _cypress_tone(payload))
            else:
                yield ("exit", payload, None)
                break

    except asyncio.CancelledError:
        try:
            proc.kill()
        except Exception:
            pass
        raise

    finally:
        session["process"] = None
        reader_thread.join(timeout=2)
        shutil.rmtree(run_dir, ignore_errors=True)  # always — pass, fail, error, or cancel