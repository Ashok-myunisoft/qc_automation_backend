import asyncio
import logging
import tempfile
from pathlib import Path

import logger_config  # noqa: F401
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from service.project_reader import ProjectReader
from service.gitlab_service import GitLabService
from service.architecture_resolver import (
    resolve_existing, resolve_module_screens, build_new_path,
    resolve_source_screen, resolve_source_module_screens, ResolvedSource,
)
from service.cypress_runner import run_cypress, CypressRunError
from Agents.project_analyze_agent import ProjectAnalysisAgent
from Agents.test_case_agent import TestCaseAgent
from Agents.script_generate_agent import ScriptGenerateAgent
from Agents.validate_agent import ValidateAgent
from Agents.interrupt_agent import InterruptAgent

logger = logging.getLogger(__name__)

app = FastAPI()

project_analysis_agent = ProjectAnalysisAgent()
test_case_agent        = TestCaseAgent()
script_generate_agent  = ScriptGenerateAgent()
validate_agent         = ValidateAgent()
interrupt_agent        = InterruptAgent()

reader = ProjectReader()


def _gitlab_service() -> GitLabService:
    return GitLabService()


def _source_gitlab_service() -> GitLabService:
    # Separate repo — its own SOURCE_GITLAB_URL / _TOKEN / _PROJECT_ID / _BRANCH.
    return GitLabService(env_prefix="SOURCE_GITLAB")


def _fetch_source_project_context(src: GitLabService, resolved: ResolvedSource) -> dict:
    """Pulls every file in the resolved source-repo folder into a temp dir
    (preserving the folder's relative layout) and reuses ProjectReader as-is,
    so the agent pipeline's input shape is unchanged whether the source came
    from an upload or a repo fetch."""
    with tempfile.TemporaryDirectory() as temp_dir:
        target_dir = Path(temp_dir) / resolved.dir
        target_dir.mkdir(parents=True, exist_ok=True)
        for filename in resolved.files:
            content = src.fetch_file(f"{resolved.dir}/{filename}")
            if content is None:
                continue
            (target_dir / filename).write_text(content, encoding="utf-8")
        return reader.read_project(temp_dir)


async def send_log(ws: WebSocket, text: str, tone: str = "secondary"):
    await ws.send_json({"type": "log", "text": text, "tone": tone})

async def send_status(ws: WebSocket, phase: str):
    await ws.send_json({"type": "status", "phase": phase})

async def send_error(ws: WebSocket, message: str):
    await ws.send_json({"type": "error", "message": message})

async def send_artifacts(ws: WebSocket, session: dict, validation: dict | None = None):
    if session.get("scope") == "module" and session.get("screens"):
        screens = session["screens"]
        await ws.send_json({
            "type":   "artifacts",
            "scope":  "module",
            "screens": [
                {
                    "name":         s["resolved"].dir,
                    "feature_file": s["feature"],
                    "script":       s["script"],
                    "resolved_path": s["resolved"].feature_path,
                }
                for s in screens
            ],
            "origin":  session.get("origin"),
            "exists":  bool(session.get("pushed")),
        })
        return

    resolved = session.get("resolved")
    await ws.send_json({
        "type":          "artifacts",
        "scope":         "screen",
        "feature_file":  session.get("feature"),
        "script":        session.get("script"),
        "validation":    validation,
        "origin":        session.get("origin"),
        "resolved_path": resolved.feature_path if resolved else None,
        "confidence":    resolved.confidence   if resolved else None,
        "ambiguous":     resolved.ambiguous    if resolved else False,
        "exists":        bool(session.get("pushed")),
    })


# ---------------------------------------------------------------------------
# FETCH
# ---------------------------------------------------------------------------
async def handle_fetch(ws: WebSocket, session: dict, msg: dict):
    module = (msg.get("module") or "").strip()
    screen = (msg.get("screen") or "").strip()
    scope  = (msg.get("scope") or "screen").strip()

    if not module:
        await send_error(ws, "module is required.")
        return
    if scope == "screen" and not screen:
        await send_error(ws, "module and screen are required.")
        return

    session["module"], session["screen"], session["scope"] = module, screen, scope
    session["origin"] = "fetch"

    await send_status(ws, "resolving")
    await send_log(ws, "reading gitlab repo structure...", "secondary")

    try:
        gl   = _gitlab_service()
        tree = gl.get_repo_tree()
    except Exception as e:
        await send_error(ws, f"gitlab connection failed: {e}")
        return

    if scope == "module":
        await send_log(ws, f"scanned {len(tree)} files — looking for every screen in {module}...", "secondary")

        candidates = resolve_module_screens(tree, module)
        if not candidates:
            await send_log(ws, f"no screens found under module '{module}'.", "danger")
            await send_status(ws, "not_found")
            return

        await send_log(ws, f"found {len(candidates)} screen(s) in {module}: " +
                       ", ".join(c.dir for c in candidates), "success")

        screens = []
        for c in candidates:
            try:
                feature_content = gl.fetch_file(c.feature_path)
                script_content  = gl.fetch_file(c.script_path)
            except Exception as e:
                await send_log(ws, f"[{c.dir}] gitlab fetch failed: {e} — skipping.", "danger")
                continue
            if feature_content is None or script_content is None:
                await send_log(ws, f"[{c.dir}] couldn't read both files — skipping.", "danger")
                continue
            screens.append({"resolved": c, "feature": feature_content, "script": script_content})

        if not screens:
            await send_log(ws, "matched screen folders but couldn't read any files.", "danger")
            await send_status(ws, "not_found")
            return

        session["screens"] = screens
        session["pushed"]  = True
        # Keep single-screen keys clear so handle_run's screen-scope path
        # doesn't accidentally pick up stale data from an earlier fetch.
        session.pop("feature", None)
        session.pop("script", None)
        session.pop("resolved", None)

        await send_log(ws, "fetched — review each screen below, then Run to execute all of them.", "success")
        await send_artifacts(ws, session)
        await send_status(ws, "awaiting_review")
        return

    await send_log(ws, f"scanned {len(tree)} files — looking for {module} / {screen}...", "secondary")

    resolved = resolve_existing(tree, module, screen)
    if resolved is None:
        await send_log(ws, f"no matching feature/script pair found for '{module} / {screen}' — use Generate instead.", "danger")
        await send_status(ws, "not_found")
        return

    if resolved.ambiguous:
        await send_log(ws, f"more than one close match — picked {resolved.dir} (confidence {resolved.confidence}). Double-check.", "accent")
    else:
        await send_log(ws, f"matched {resolved.dir} (confidence {resolved.confidence})", "success")

    try:
        feature_content = gl.fetch_file(resolved.feature_path)
        script_content  = gl.fetch_file(resolved.script_path)
    except Exception as e:
        await send_error(ws, f"gitlab fetch failed: {e}")
        return

    if feature_content is None or script_content is None:
        await send_log(ws, "matched folder but couldn't read both files — use generate instead.", "danger")
        await send_status(ws, "not_found")
        return

    session["feature"] = feature_content
    session["script"]  = script_content
    session["resolved"] = resolved
    session["pushed"]   = True
    session.pop("screens", None)

    await send_log(ws, "fetched — review below, then Run (or Interrupt to change first).", "success")
    await send_artifacts(ws, session)
    await send_status(ws, "awaiting_review")


# ---------------------------------------------------------------------------
# GENERATE
# Fully automated — no manual uploads. Source is fetched from the SOURCE_
# GITLAB_* repo (scope-aware: one screen's files, or every screen in a
# module), then run through the same 4-agent pipeline as before.
# ---------------------------------------------------------------------------
async def _generate_one_screen(ws: WebSocket, src: GitLabService, module: str,
                                screen_name: str, resolved_source: ResolvedSource,
                                user_request: str, label: str | None = None) -> dict | None:
    """Runs the analysis -> test-case -> script -> validate pipeline for one
    resolved source screen. Returns {feature, script, validation} or None on
    failure (caller decides whether that's fatal or skip-and-continue)."""
    tag = f"[{label}] " if label else ""
    try:
        await send_log(ws, f"{tag}fetching source files...", "secondary")
        project_context = _fetch_source_project_context(src, resolved_source)

        await send_log(ws, f"{tag}running project analysis agent...", "secondary")
        project_analysis = await project_analysis_agent.analyze(
            project_context=project_context, user_request=user_request,
        )

        await send_log(ws, f"{tag}running test case agent...", "secondary")
        test_cases = await test_case_agent.generate_test_cases(
            project_analysis=project_analysis, user_request=user_request,
            business_context=None,
        )

        await send_log(ws, f"{tag}running script generate agent...", "secondary")
        generated_script = await script_generate_agent.generate_script(test_cases=test_cases)

        await send_log(ws, f"{tag}running validate agent...", "secondary")
        validation_result = await validate_agent.validate(
            test_cases=test_cases, generated_script=generated_script,
        )
    except Exception as e:
        logger.exception("generate pipeline failed for %s", screen_name)
        await send_log(ws, f"{tag}generation failed: {e}", "danger")
        return None

    return {"feature": test_cases, "script": generated_script, "validation": validation_result}


async def handle_generate(ws: WebSocket, session: dict, msg: dict):
    module       = (msg.get("module")  or "").strip()
    screen       = (msg.get("screen")  or "").strip()
    scope        = (msg.get("scope")   or "screen").strip()
    user_request = (msg.get("request") or "").strip()

    if not module:
        await send_error(ws, "module is required.")
        return
    if scope == "screen" and not screen:
        await send_error(ws, "module and screen are required.")
        return

    session["module"], session["screen"], session["scope"] = module, screen, scope
    session["origin"] = "generate"
    session["pushed"] = False

    await send_status(ws, "resolving")
    await send_log(ws, "reading source repo structure...", "secondary")

    try:
        src      = _source_gitlab_service()
        src_tree = src.get_repo_tree()
    except Exception as e:
        await send_error(ws, f"source gitlab connection failed: {e}")
        return

    # Best-effort peek at the output (qc_test) repo so we know up front
    # whether we're replacing existing files or creating new ones.
    out_tree = None
    try:
        out_gl   = _gitlab_service()
        out_tree = out_gl.get_repo_tree()
    except Exception as e:
        await send_log(ws, f"could not check output repo ({e}) — will resolve path on approve.", "muted")

    def resolve_output_path(screen_name: str):
        resolved = resolve_existing(out_tree, module, screen_name) if out_tree is not None else None
        if resolved is None:
            return build_new_path(module, screen_name), False
        return resolved, True

    # -----------------------------------------------------------------
    # Whole module: fetch + generate for every screen under it
    # -----------------------------------------------------------------
    if scope == "module":
        await send_log(ws, f"scanned {len(src_tree)} source files — looking for every screen in {module}...", "secondary")

        candidates = resolve_source_module_screens(src_tree, module)
        if not candidates:
            await send_log(ws, f"no source screens found under module '{module}' in the source repo.", "danger")
            await send_status(ws, "not_found")
            return

        await send_log(ws, f"found {len(candidates)} screen(s) in {module}: " +
                       ", ".join(c.dir for c in candidates), "success")

        screens = []
        for c in candidates:
            screen_name    = c.dir.rsplit("/", 1)[-1]
            screen_request = user_request or f"Generate Cypress tests for the {screen_name} screen in the {module} module."

            outcome = await _generate_one_screen(ws, src, module, screen_name, c, screen_request, label=screen_name)
            if outcome is None:
                continue

            resolved, existed = resolve_output_path(screen_name)
            await send_log(
                ws,
                f"[{screen_name}] existing files found at {resolved.dir} — will replace on approve."
                if existed else
                f"[{screen_name}] new screen — will create at {resolved.dir} on approve.",
                "secondary",
            )
            screens.append({"resolved": resolved, "feature": outcome["feature"], "script": outcome["script"]})

        if not screens:
            await send_log(ws, "found source screens but generation failed for all of them.", "danger")
            await send_status(ws, "not_found")
            return

        session["screens"] = screens
        session.pop("feature", None)
        session.pop("script", None)
        session.pop("resolved", None)

        await send_log(ws, "generated — review each screen below, then Approve to push all of them.", "success")
        await send_artifacts(ws, session)
        await send_status(ws, "awaiting_approval")
        return

    # -----------------------------------------------------------------
    # Single screen
    # -----------------------------------------------------------------
    await send_log(ws, f"scanned {len(src_tree)} source files — looking for {module} / {screen}...", "secondary")

    resolved_source = resolve_source_screen(src_tree, module, screen)
    if resolved_source is None:
        await send_log(ws, f"no source files found for '{module} / {screen}' in the source repo.", "danger")
        await send_status(ws, "not_found")
        return

    if resolved_source.ambiguous:
        await send_log(ws, f"more than one close match — picked {resolved_source.dir} (confidence {resolved_source.confidence}). Double-check.", "accent")
    else:
        await send_log(ws, f"matched source {resolved_source.dir} (confidence {resolved_source.confidence})", "success")

    user_request = user_request or f"Generate Cypress tests for the {screen} screen in the {module} module."

    outcome = await _generate_one_screen(ws, src, module, screen, resolved_source, user_request)
    if outcome is None:
        await send_error(ws, "generation failed.")
        return

    session["feature"] = outcome["feature"]
    session["script"]  = outcome["script"]
    session.pop("screens", None)

    resolved, existed = resolve_output_path(screen)
    await send_log(
        ws,
        f"existing files found at {resolved.dir} — will replace on approve."
        if existed else
        f"new screen — will create at {resolved.dir} on approve.",
        "secondary",
    )
    session["resolved"] = resolved

    await send_log(ws, "generated — review below.", "success")
    await send_artifacts(ws, session, validation=outcome["validation"])
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
    if session.get("scope") == "module" and session.get("screens"):
        screens = session["screens"]

        if session.get("origin") == "generate" and not session.get("pushed"):
            await send_error(ws, "approve the generated files before running.")
            return

        await send_status(ws, "running")
        await send_log(ws, f"cypress: running {len(screens)} screen(s) in this module...", "secondary")

        summary = []  # [(dir, passed, exit_code)]
        for i, s in enumerate(screens, start=1):
            resolved = s["resolved"]
            slug     = resolved.slug
            label    = resolved.dir

            await send_log(ws, f"[{i}/{len(screens)}] {label} — preparing workspace...", "secondary")
            await send_log(ws, f"[{i}/{len(screens)}] {label} — starting run...", "secondary")

            try:
                async for kind, payload, tone in run_cypress(
                    session,
                    s["feature"],
                    s["script"],
                    slug,
                ):
                    if kind == "log":
                        await send_log(ws, f"[{label}] {payload}", tone or "secondary")
                    elif kind == "exit":
                        passed = payload == 0
                        summary.append((label, passed, payload))
                        tone_ = "success" if passed else "danger"
                        await send_log(ws, f"[{label}] {'PASSED' if passed else 'FAILED'} (exit {payload})", tone_)
            except CypressRunError as e:
                summary.append((label, False, None))
                await send_log(ws, f"[{label}] run error: {e}", "danger")
                continue  # keep going — a broken screen shouldn't stop the rest of the module
            except asyncio.CancelledError:
                await send_log(ws, "run cancelled.", "muted")
                raise

        passed_count = sum(1 for _, p, _ in summary if p)
        await send_log(ws, f"module run complete: {passed_count}/{len(summary)} screens passed.",
                       "success" if passed_count == len(summary) else "accent")
        await send_status(ws, "done")
        await ws.send_json({
            "type":    "module_result",
            "results": [{"name": name, "passed": p, "exit_code": ec} for name, p, ec in summary],
        })
        return

    if not session.get("feature") or not session.get("script") or not session.get("resolved"):
        await send_error(ws, "nothing to run yet — fetch or generate first.")
        return

    if session.get("origin") == "generate" and not session.get("pushed"):
        await send_error(ws, "approve the generated files before running.")
        return

    resolved = session["resolved"]
    slug     = resolved.slug

    await send_status(ws, "running")
    await send_log(ws, "cypress: preparing workspace...", "secondary")
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

    await send_log(ws, f'[interrupt] applying: "{note}"', "accent")

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
            await send_log(ws, "change replaced in gitlab.", "success")
        except Exception as e:
            await send_error(ws, f"change applied locally, gitlab push failed: {e}")

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
            msg    = await websocket.receive_json()
            action = msg.get("action")

            if action == "interrupt" and session.get("process") is not None:
                try:
                    session["process"].kill()
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
                session["pushed"]  = False
                await send_log(websocket, "rejected — nothing pushed.", "muted")
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