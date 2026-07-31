import asyncio
import base64
import logging
import tempfile
import zipfile
from pathlib import Path

import logger_config  # noqa: F401  (sets up logging on import)
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from service.project_reader import ProjectReader
from service.gitlab_service import GitLabService
from service.architecture_resolver import resolve_existing, build_new_path
from service.cypress_runner import run_cypress, CypressRunError
from Agents.interrupt_agent import InterruptAgent
from Agents.planner_agent import PlannerAgent
from workflow.context import WorkflowState
from workflow.executor import (
    ProjectAnalysisExecutor,
    TestCaseExecutor,
    ScriptGenerateExecutor,
    ValidateExecutor,
)

logger = logging.getLogger(__name__)

app = FastAPI()

# Executors — instantiated once, reused across requests
planner_agent          = PlannerAgent()
project_analysis_exec  = ProjectAnalysisExecutor()
test_case_exec         = TestCaseExecutor()
script_generate_exec   = ScriptGenerateExecutor()
validate_exec          = ValidateExecutor()
interrupt_agent        = InterruptAgent()

reader = ProjectReader()

# Maps planner agent names → executor instances
AGENT_MAP = {
    "ProjectAnalysisAgent": project_analysis_exec,
    "FeatureFileAgent":     test_case_exec,
    "ScriptGenerationAgent": script_generate_exec,
    "ValidationAgent":      validate_exec,
}

# Maps executor → the handler method name to call
HANDLER_MAP = {
    "project_analysis_executor": "analyze_project",
    "test_case_executor":        "generate_test_cases",
    "script_generate_executor":  "generate_script",
    "validate_executor":         "validate",
}


def _gitlab_service() -> GitLabService:
    return GitLabService()


async def send_log(ws: WebSocket, text: str, tone: str = "secondary"):
    await ws.send_json({"type": "log", "text": text, "tone": tone})


async def send_status(ws: WebSocket, phase: str):
    await ws.send_json({"type": "status", "phase": phase})


async def send_error(ws: WebSocket, message: str):
    await ws.send_json({"type": "error", "message": message})


async def send_artifacts(ws: WebSocket, session: dict, validation: dict | None = None):
    resolved = session.get("resolved")
    await ws.send_json({
        "type": "artifacts",
        "feature_file": session.get("feature"),
        "script": session.get("script"),
        "validation": validation,
        "origin": session.get("origin"),
        "resolved_path": resolved.feature_path if resolved else None,
        "confidence": resolved.confidence if resolved else None,
        "ambiguous": resolved.ambiguous if resolved else False,
        "exists": bool(session.get("pushed")),
    })


# ---------------------------------------------------------------------------
# FETCH
# ---------------------------------------------------------------------------
async def handle_fetch(ws: WebSocket, session: dict, msg: dict):
    module, screen = (msg.get("module") or "").strip(), (msg.get("screen") or "").strip()
    if not module or not screen:
        await send_error(ws, "module and screen are required.")
        return

    session["module"], session["screen"] = module, screen
    session["origin"] = "fetch"

    await send_status(ws, "resolving")
    await send_log(ws, "reading gitlab repo structure...", "secondary")

    try:
        gl = _gitlab_service()
        tree = gl.get_repo_tree()
    except Exception as e:
        await send_error(ws, f"gitlab connection failed: {e}")
        return

    await send_log(ws, f"scanned {len(tree)} files — looking for {module} / {screen}...", "secondary")

    resolved = resolve_existing(tree, module, screen)
    if resolved is None:
        await send_log(
            ws,
            f"no matching feature/script pair found in the repo for '{module} / {screen}' "
            f"— use Generate instead.",
            "danger",
        )
        await send_status(ws, "not_found")
        return

    if resolved.ambiguous:
        await send_log(
            ws,
            f"more than one close match found — picked {resolved.dir} "
            f"(confidence {resolved.confidence}). Double-check this is the right screen.",
            "accent",
        )
    else:
        await send_log(ws, f"matched {resolved.dir} (confidence {resolved.confidence})", "success")

    try:
        feature_content = gl.fetch_file(resolved.feature_path)
        script_content = gl.fetch_file(resolved.script_path)
    except Exception as e:
        await send_error(ws, f"gitlab fetch failed: {e}")
        return

    if feature_content is None or script_content is None:
        await send_log(ws, "matched folder but couldn't read both files — use generate instead.", "danger")
        await send_status(ws, "not_found")
        return

    session["feature"] = feature_content
    session["script"] = script_content
    session["resolved"] = resolved
    session["pushed"] = True

    await send_log(ws, "fetched — review below, then Run (or Interrupt to change first).", "success")
    await send_artifacts(ws, session)
    await send_status(ws, "awaiting_review")


# ---------------------------------------------------------------------------
# GENERATE  (now planner-driven via WorkflowState + Executors)
# ---------------------------------------------------------------------------
async def handle_generate(ws: WebSocket, session: dict, msg: dict):
    module, screen = (msg.get("module") or "").strip(), (msg.get("screen") or "").strip()
    source_zip_b64 = msg.get("source_zip_base64")

    if not module or not screen:
        await send_error(ws, "module and screen are required.")
        return
    if not source_zip_b64:
        await send_error(ws, "no source zip provided — attach the screen's source before generating.")
        return

    session["module"], session["screen"] = module, screen
    session["origin"] = "generate"
    session["pushed"] = False

    await send_status(ws, "running")
    await send_log(ws, "reading uploaded source...", "secondary")

    try:
        zip_bytes = base64.b64decode(source_zip_b64)
    except Exception as e:
        await send_error(ws, f"could not decode uploaded source zip: {e}")
        return

    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            zip_path = Path(temp_dir) / "source.zip"
            zip_path.write_bytes(zip_bytes)
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(temp_dir)
            project_context = reader.read_project(temp_dir)
    except Exception as e:
        await send_error(ws, f"could not read uploaded source: {e}")
        return

    user_request = (
        msg.get("request")
        or f"Generate Cypress tests for the {screen} screen in the {module} module."
    )

    # ── Build WorkflowState ──────────────────────────────────────────────────
    state = WorkflowState()
    state.user_request    = user_request
    state.project_context = project_context
    state.business_context = msg.get("business_context") or ""

    # ── Ask PlannerAgent which agents to run ────────────────────────────────
    await send_log(ws, "running planner agent...", "secondary")
    try:
        plan = await planner_agent.plan(user_request=user_request)
    except Exception as e:
        await send_error(ws, f"planner failed: {e}")
        return

    if "error" in plan:
        await send_error(ws, f"planner could not parse a workflow: {plan['error']}")
        return

    state.plan = plan
    steps = plan.get("workflow", [])
    await send_log(ws, f"plan: {[s['agent'] for s in steps]}", "secondary")

    # ── Execute each planned step in order ──────────────────────────────────
    try:
        for step in steps:
            agent_name = step.get("agent")
            executor = AGENT_MAP.get(agent_name)
            if executor is None:
                await send_log(ws, f"unknown agent in plan: {agent_name} — skipping", "accent")
                continue

            handler_name = HANDLER_MAP.get(executor.id)
            if handler_name is None:
                await send_log(ws, f"no handler mapped for {agent_name} — skipping", "accent")
                continue

            await send_log(ws, f"running {agent_name}...", "secondary")
            handler_fn = getattr(executor, handler_name)
            await handler_fn(state, ctx=None)   # ctx=None: we handle messaging ourselves via ws

    except Exception as e:
        logger.exception("generate pipeline failed")
        await send_error(ws, f"generation failed: {e}")
        return

    # ── Store results in session ─────────────────────────────────────────────
    session["feature"] = state.test_cases        # feature file content
    session["script"]  = state.generated_script  # cypress script content

    # ── Resolve GitLab path ──────────────────────────────────────────────────
    try:
        gl = _gitlab_service()
        tree = gl.get_repo_tree()
        resolved = resolve_existing(tree, module, screen)
        if resolved is None:
            resolved = build_new_path(module, screen)
            await send_log(ws, f"new screen — will create at {resolved.dir} on approve.", "secondary")
        else:
            await send_log(ws, f"existing files found at {resolved.dir} — will replace on approve.", "secondary")
    except Exception as e:
        await send_log(ws, f"could not check gitlab for existing files ({e}) — will resolve path on approve.", "muted")
        resolved = build_new_path(module, screen)

    session["resolved"] = resolved

    await send_log(ws, "feature file + cypress script generated — review below.", "success")
    await send_artifacts(ws, session, validation=state.validation_result)
    await send_status(ws, "awaiting_approval")


# ---------------------------------------------------------------------------
# APPROVE
# ---------------------------------------------------------------------------
async def handle_approve(ws: WebSocket, session: dict):
    if not session.get("feature") or not session.get("script") or not session.get("resolved"):
        await send_error(ws, "nothing to approve — generate something first.")
        return

    resolved = session["resolved"]

    await send_status(ws, "running")
    try:
        gl = _gitlab_service()
        gl.create_or_update_file(
            resolved.feature_path, session["feature"],
            commit_message=f"QC: add/update feature file for {session.get('module')}/{session.get('screen')}",
        )
        gl.create_or_update_file(
            resolved.script_path, session["script"],
            commit_message=f"QC: add/update cypress script for {session.get('module')}/{session.get('screen')}",
        )
    except Exception as e:
        await send_error(ws, f"gitlab push failed: {e}")
        return

    session["pushed"] = True
    await send_log(ws, f"pushed to gitlab at {resolved.dir}", "success")
    await send_artifacts(ws, session)
    await send_status(ws, "awaiting_review")


# ---------------------------------------------------------------------------
# RUN
# ---------------------------------------------------------------------------
async def handle_run(ws: WebSocket, session: dict):
    if not session.get("feature") or not session.get("script") or not session.get("resolved"):
        await send_error(ws, "nothing to run yet — fetch or generate first.")
        return

    if session.get("origin") == "generate" and not session.get("pushed"):
        await send_error(ws, "approve the generated files before running.")
        return

    resolved = session["resolved"]
    slug     = resolved.slug

    await send_status(ws, "running")
    await send_log(ws, "cypress: starting run...", "secondary")

    try:
        async for kind, payload, tone in run_cypress(
            session,
            session["feature"],
            session["script"],
            slug,
        ):
            if kind == "log":
                await send_log(ws, payload, tone or "secondary")
            elif kind == "exit":
                passed = payload == 0
                await send_status(ws, "done")
                await ws.send_json({"type": "result", "passed": passed, "exit_code": payload})
    except CypressRunError as e:
        await send_error(ws, str(e))
    except asyncio.CancelledError:
        await send_log(ws, "run cancelled.", "muted")
        raise


# ---------------------------------------------------------------------------
# INTERRUPT
# ---------------------------------------------------------------------------
async def handle_interrupt(ws: WebSocket, session: dict, msg: dict):
    note = (msg.get("note") or "").strip()
    if not note:
        return
    if not session.get("feature") or not session.get("script"):
        await send_error(ws, "nothing to change yet — fetch or generate first.")
        return

    await send_log(ws, f'[interrupt] applying change: "{note}"', "accent")

    try:
        result = await interrupt_agent.apply_change(session["feature"], session["script"], note)
    except Exception as e:
        await send_error(ws, f"could not apply change: {e}")
        return

    if "error" in result:
        await send_error(ws, f"could not apply that change — {result['error']}")
        return

    session["feature"] = result["feature_file"]
    session["script"]  = result["script"]
    await send_log(ws, result.get("summary", "change applied"), "secondary")

    resolved = session.get("resolved")
    if resolved and session.get("pushed"):
        try:
            gl = _gitlab_service()
            gl.create_or_update_file(
                resolved.feature_path, session["feature"],
                commit_message=f"QC: interrupt change — {session.get('module')}/{session.get('screen')}",
            )
            gl.create_or_update_file(
                resolved.script_path, session["script"],
                commit_message=f"QC: interrupt change — {session.get('module')}/{session.get('screen')}",
            )
            await send_log(ws, "change replaced the file in gitlab.", "success")
        except Exception as e:
            await send_error(ws, f"change applied locally, but gitlab push failed: {e}")

    await send_artifacts(ws, session)
    next_phase = "awaiting_approval" if session.get("origin") == "generate" and not session.get("pushed") else "awaiting_review"
    await send_status(ws, next_phase)


# ---------------------------------------------------------------------------
# WebSocket entrypoint
# ---------------------------------------------------------------------------
@app.websocket("/ws/qc")
async def qc_session(websocket: WebSocket):
    await websocket.accept()
    session = {
        "module": None, "screen": None, "origin": None,
        "feature": None, "script": None, "resolved": None,
        "pushed": False, "process": None,
    }

    try:
        while True:
            msg = await websocket.receive_json()
            action = msg.get("action")

            if action == "interrupt" and session.get("process") is not None:
                proc = session["process"]
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                asyncio.create_task(handle_interrupt(websocket, session, msg))

            elif action == "interrupt":
                asyncio.create_task(handle_interrupt(websocket, session, msg))

            elif action == "fetch":
                asyncio.create_task(handle_fetch(websocket, session, msg))

            elif action == "generate":
                asyncio.create_task(handle_generate(websocket, session, msg))

            elif action == "approve":
                asyncio.create_task(handle_approve(websocket, session))

            elif action == "reject":
                session["feature"] = None
                session["script"]  = None
                session["resolved"] = None
                session["pushed"] = False
                await send_log(websocket, "rejected — nothing pushed. adjust and try generate again.", "muted")
                await send_status(websocket, "idle")

            elif action == "run":
                asyncio.create_task(handle_run(websocket, session))

            else:
                await send_error(websocket, f"unknown action: {action}")

    except WebSocketDisconnect:
        logger.info("qc session disconnected")
        if session.get("process") is not None:
            try:
                session["process"].kill()
            except ProcessLookupError:
                pass