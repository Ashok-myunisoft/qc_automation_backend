import asyncio
import shutil
import uuid
from pathlib import Path

_WORKSPACE = Path(__file__).resolve().parent.parent / "cypress-workspace"
_RUNS_DIR  = _WORKSPACE / "cypress" / "e2e" / "_runs"

TABLE_CHARS = set("┌┐└┘├┤┬┴┼─│═╞╡╥╨╫")


class CypressRunError(Exception):
    pass


def _ensure_workspace() -> None:
    if not _WORKSPACE.exists():
        raise CypressRunError(
            f"cypress-workspace/ not found at {_WORKSPACE}. "
            "Make sure it is committed inside qc-backend/."
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


async def run_cypress(session: dict, feature_content: str, script_content: str, slug: str):
    _ensure_workspace()
    _ensure_npm_installed()

    run_id  = uuid.uuid4().hex[:8]
    run_dir = _RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    feature_file = run_dir / f"{slug}.feature"
    script_file  = run_dir / f"{slug}.js"

    feature_file.write_text(feature_content, encoding="utf-8")
    script_file.write_text(script_content,  encoding="utf-8")

    spec_rel = feature_file.relative_to(_WORKSPACE)

    try:
        process = await asyncio.create_subprocess_exec(
            "npx", "cypress", "run", "--spec", str(spec_rel),
            cwd=str(_WORKSPACE),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        session["process"] = process

        async for raw_line in process.stdout:
            line = raw_line.decode("utf-8", errors="replace").rstrip()
            if line:
                yield ("log", line, _cypress_tone(line))

        returncode = await process.wait()
        yield ("exit", returncode, None)

    except asyncio.CancelledError:
        if session.get("process"):
            session["process"].kill()
        raise

    finally:
        session["process"] = None
        try:
            shutil.rmtree(run_dir, ignore_errors=True)
        except Exception:
            pass