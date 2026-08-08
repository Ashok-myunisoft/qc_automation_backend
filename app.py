import asyncio
import logging
import tempfile
from pathlib import Path
import sys
import logger_config 
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
import warnings
from service.project_reader import ProjectReader
from service.gitlab_service import GitLabService
from service.architecture_resolver import (
    build_new_path, ResolvedSource,
    resolve_existing_precise, resolve_module_screens_precise,
    resolve_source_screen_precise, resolve_source_module_screens_precise,
)
from service.cypress_runner import run_cypress, CypressRunError
from Agents.project_analyze_agent import ProjectAnalysisAgent
from Agents.test_case_agent import TestCaseAgent
from Agents.script_generate_agent import ScriptGenerateAgent
from Agents.validate_agent import ValidateAgent
from Agents.interrupt_agent import InterruptAgent
from Agents.append_agent import AppendAgent

logger = logging.getLogger(__name__)

app = FastAPI()

project_analysis_agent = ProjectAnalysisAgent()
test_case_agent        = TestCaseAgent()
script_generate_agent  = ScriptGenerateAgent()
validate_agent         = ValidateAgent()
interrupt_agent        = InterruptAgent()
append_agent           = AppendAgent()

reader = ProjectReader()
if sys.platform == "win32":
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())


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

        candidates = await resolve_module_screens_precise(tree, module)
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

    resolved = await resolve_existing_precise(tree, module, screen)
    if resolved is None:
        await send_log(ws, f"no matching feature/script pair found for '{module} / {screen}' — use Generate instead.", "danger")
        await send_status(ws, "not_found")
        return

    if resolved.ambiguous:
        await send_log(ws, f"more than one close match ({resolved.resolved_by}) — picked {resolved.dir} (confidence {resolved.confidence}). Double-check.", "accent")
    else:
        await send_log(ws, f"matched {resolved.dir} via {resolved.resolved_by} (confidence {resolved.confidence})", "success")

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
async def _check_output_existing(out_gl: GitLabService, out_tree, module: str, screen_name: str):
    """Returns (resolved, existed, existing_feature, existing_script).
    existing_feature/script are None whenever existed is False — including
    the case where a matching folder was found but its files couldn't be
    read, since that shouldn't block the generate pipeline."""
    resolved = await resolve_existing_precise(out_tree, module, screen_name) if out_tree is not None else None
    if resolved is None:
        return build_new_path(module, screen_name), False, None, None
    try:
        existing_feature = out_gl.fetch_file(resolved.feature_path)
        existing_script  = out_gl.fetch_file(resolved.script_path)
    except Exception:
        existing_feature = existing_script = None
    if existing_feature is None or existing_script is None:
        return resolved, False, None, None
    return resolved, True, existing_feature, existing_script


async def _append_one_screen(existing_feature: str, existing_script: str,
                              append_request: str, label: str | None = None) -> dict | None:
    """Runs the append agent against one screen's existing feature/script.
    Returns {feature, script, summary} on success, or {"error": ...}."""
    try:
        result = await append_agent.apply_append(existing_feature, existing_script, append_request)
    except Exception as e:
        logger.exception("append pipeline failed for %s", label)
        return {"error": str(e)}
    return result


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
    session.pop("pending_generate", None)

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
    out_gl   = None
    try:
        out_gl   = _gitlab_service()
        out_tree = out_gl.get_repo_tree()
    except Exception as e:
        await send_log(ws, f"could not check output repo ({e}) — will resolve path on approve.", "muted")

    # -----------------------------------------------------------------
    # Whole module: fetch + generate for every screen under it
    # -----------------------------------------------------------------
    if scope == "module":
        await send_log(ws, f"scanned {len(src_tree)} source files — looking for every screen in {module}...", "secondary")

        candidates = await resolve_source_module_screens_precise(src_tree, module)
        if not candidates:
            await send_log(ws, f"no source screens found under module '{module}' in the source repo.", "danger")
            await send_status(ws, "not_found")
            return

        await send_log(ws, f"found {len(candidates)} screen(s) in {module}: " +
                       ", ".join(c.dir for c in candidates), "success")

        conflicts = {}         # screen_name -> {resolved, existing_feature, existing_script, source}
        fresh_candidates = []  # [(screen_name, source_resolved)] — no existing output, always fresh

        for c in candidates:
            screen_name = c.dir.rsplit("/", 1)[-1]
            if out_tree is not None:
                resolved, existed, ef, es = await _check_output_existing(out_gl, out_tree, module, screen_name)
            else:
                resolved, existed, ef, es = build_new_path(module, screen_name), False, None, None
            if existed:
                conflicts[screen_name] = {
                    "resolved": resolved, "existing_feature": ef, "existing_script": es, "source": c,
                }
            else:
                fresh_candidates.append((screen_name, c))

        if conflicts:
            session["pending_generate"] = {
                "module": module, "scope": "module", "user_request": user_request,
                "conflicts": conflicts, "fresh": fresh_candidates,
            }
            await send_log(
                ws,
                f"{len(conflicts)} of {len(candidates)} screen(s) already have tests in the QC repo — "
                f"choose Replace or Append before continuing.",
                "accent",
            )
            await ws.send_json({
                "type": "conflict",
                "scope": "module",
                "conflicts": [
                    {"name": name, "existing_feature": c["existing_feature"], "existing_script": c["existing_script"]}
                    for name, c in conflicts.items()
                ],
                "new_count": len(fresh_candidates),
            })
            await send_status(ws, "awaiting_conflict")
            return

        # no conflicts at all — generate every screen fresh, same as before
        screens = []
        for screen_name, c in fresh_candidates:
            screen_request = user_request or f"Generate Cypress tests for the {screen_name} screen in the {module} module."
            outcome = await _generate_one_screen(ws, src, module, screen_name, c, screen_request, label=screen_name)
            if outcome is None:
                continue
            resolved = build_new_path(module, screen_name)
            await send_log(ws, f"[{screen_name}] new screen — will create at {resolved.dir} on approve.", "secondary")
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

    resolved_source = await resolve_source_screen_precise(src_tree, module, screen)
    if resolved_source is None:
        await send_log(ws, f"no source files found for '{module} / {screen}' in the source repo.", "danger")
        await send_status(ws, "not_found")
        return

    if resolved_source.ambiguous:
        await send_log(ws, f"more than one close match ({resolved_source.resolved_by}) — picked {resolved_source.dir} (confidence {resolved_source.confidence}). Double-check.", "accent")
    else:
        await send_log(ws, f"matched source {resolved_source.dir} via {resolved_source.resolved_by} (confidence {resolved_source.confidence})", "success")

    resolved_output = None
    existed = False
    existing_feature = existing_script = None
    if out_tree is not None:
        resolved_output, existed, existing_feature, existing_script = await _check_output_existing(out_gl, out_tree, module, screen)

    if existed:
        session["pending_generate"] = {
            "module": module, "screen": screen, "scope": "screen", "user_request": user_request,
            "resolved_source": resolved_source,
            "resolved_output": resolved_output,
            "existing_feature": existing_feature, "existing_script": existing_script,
        }
        await send_log(ws, f"existing tests found at {resolved_output.dir} — choose Replace or Append before continuing.", "accent")
        await ws.send_json({
            "type": "conflict",
            "scope": "screen",
            "conflicts": [{"name": screen, "existing_feature": existing_feature, "existing_script": existing_script}],
            "new_count": 0,
        })
        await send_status(ws, "awaiting_conflict")
        return

    user_request = user_request or f"Generate Cypress tests for the {screen} screen in the {module} module."

    outcome = await _generate_one_screen(ws, src, module, screen, resolved_source, user_request)
    if outcome is None:
        await send_error(ws, "generation failed.")
        return

    session["feature"] = outcome["feature"]
    session["script"]  = outcome["script"]
    session.pop("screens", None)

    resolved = resolved_output or build_new_path(module, screen)
    await send_log(ws, f"new screen — will create at {resolved.dir} on approve.", "secondary")
    session["resolved"] = resolved

    await send_log(ws, "generated — review below.", "success")
    await send_artifacts(ws, session, validation=outcome["validation"])
    await send_status(ws, "awaiting_approval")


# ---------------------------------------------------------------------------
# GENERATE DECISION (Replace / Append) — resumes a paused handle_generate
# once the QC repo already had a feature/script for the target screen(s).
# ---------------------------------------------------------------------------
async def handle_generate_decision(ws: WebSocket, session: dict, msg: dict):
    pending = session.get("pending_generate")
    if not pending:
        await send_error(ws, "nothing waiting on a replace/append decision.")
        return

    decision       = (msg.get("decision") or "").strip().lower()
    append_request = (msg.get("append_request") or "").strip()

    if decision not in ("replace", "append"):
        await send_error(ws, "decision must be 'replace' or 'append'.")
        return
    if decision == "append" and not append_request:
        await send_error(ws, "describe what you want to add.")
        return

    module       = pending["module"]
    user_request = pending["user_request"]

    await send_status(ws, "resolving")

    # -----------------------------------------------------------------
    # Whole module — decision applies uniformly to every conflicting
    # screen; screens with no existing output are always freshly
    # generated, independent of the decision.
    # -----------------------------------------------------------------
    if pending["scope"] == "module":
        try:
            src = _source_gitlab_service()
        except Exception as e:
            await send_error(ws, f"source gitlab connection failed: {e}")
            session.pop("pending_generate", None)
            return

        screens = []

        for screen_name, c in pending["fresh"]:
            screen_request = user_request or f"Generate Cypress tests for the {screen_name} screen in the {module} module."
            outcome = await _generate_one_screen(ws, src, module, screen_name, c, screen_request, label=screen_name)
            if outcome is None:
                continue
            resolved = build_new_path(module, screen_name)
            await send_log(ws, f"[{screen_name}] new screen — will create at {resolved.dir} on approve.", "secondary")
            screens.append({"resolved": resolved, "feature": outcome["feature"], "script": outcome["script"]})

        for screen_name, c in pending["conflicts"].items():
            if decision == "replace":
                screen_request = user_request or f"Generate Cypress tests for the {screen_name} screen in the {module} module."
                outcome = await _generate_one_screen(ws, src, module, screen_name, c["source"], screen_request, label=screen_name)
                if outcome is None:
                    continue
                await send_log(ws, f"[{screen_name}] will replace existing tests at {c['resolved'].dir} on approve.", "secondary")
                screens.append({"resolved": c["resolved"], "feature": outcome["feature"], "script": outcome["script"]})
            else:
                await send_log(ws, f"[{screen_name}] appending to existing tests...", "secondary")
                result = await _append_one_screen(c["existing_feature"], c["existing_script"], append_request, label=screen_name)
                if not result or "error" in result:
                    await send_log(ws, f"[{screen_name}] append failed — {result.get('error') if result else 'unknown error'}", "danger")
                    continue
                await send_log(ws, f"[{screen_name}] {result.get('summary', 'appended')}", "success")
                screens.append({"resolved": c["resolved"], "feature": result["feature_file"], "script": result["script"]})

        session.pop("pending_generate", None)

        if not screens:
            await send_log(ws, "no screens could be generated or appended.", "danger")
            await send_status(ws, "not_found")
            return

        session["screens"] = screens
        session.pop("feature", None)
        session.pop("script", None)
        session.pop("resolved", None)

        await send_log(ws, "ready — review each screen below, then Approve to push all of them.", "success")
        await send_artifacts(ws, session)
        await send_status(ws, "awaiting_approval")
        return

    # -----------------------------------------------------------------
    # Single screen
    # -----------------------------------------------------------------
    screen          = pending["screen"]
    resolved_output = pending["resolved_output"]
    validation      = None

    if decision == "replace":
        try:
            src = _source_gitlab_service()
        except Exception as e:
            await send_error(ws, f"source gitlab connection failed: {e}")
            session.pop("pending_generate", None)
            return

        request = user_request or f"Generate Cypress tests for the {screen} screen in the {module} module."
        outcome = await _generate_one_screen(ws, src, module, screen, pending["resolved_source"], request)
        if outcome is None:
            await send_error(ws, "generation failed.")
            session.pop("pending_generate", None)
            return
        session["feature"] = outcome["feature"]
        session["script"]  = outcome["script"]
        validation = outcome["validation"]
        await send_log(ws, f"will replace existing tests at {resolved_output.dir} on approve.", "secondary")
    else:
        await send_log(ws, "appending to existing tests...", "accent")
        result = await _append_one_screen(pending["existing_feature"], pending["existing_script"], append_request)
        if not result or "error" in result:
            await send_error(ws, f"could not apply that addition — {result.get('error') if result else 'unknown error'}")
            session.pop("pending_generate", None)
            return
        session["feature"] = result["feature_file"]
        session["script"]  = result["script"]
        await send_log(ws, result.get("summary", "addition applied"), "success")

    session["resolved"] = resolved_output
    session.pop("screens", None)
    session.pop("pending_generate", None)

    await send_log(ws, "ready — review below.", "success")
    await send_artifacts(ws, session, validation=validation)
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


async def _fetch_shared_fixtures(ws: WebSocket) -> dict:
    """Fetches shared fixture files that live in the QC repo (not the
    cypress-workspace) — currently just fixtures/validation-error-message.json,
    used by VerifyFormValidationMessage across many screens. Always fetched
    fresh so the QC repo stays the single source of truth for these — a
    fetch failure is logged but doesn't block the run, since not every
    screen actually needs it."""
    fixtures = {}
    try:
        out_gl = _gitlab_service()
        content = out_gl.fetch_file("fixtures/validation-error-message.json")
        if content:
            fixtures["validation-error-message.json"] = content
    except Exception as e:
        await send_log(
            ws,
            f"could not fetch shared fixtures from the QC repo ({e}) — "
            f"screens using cy.fixture() may fail.",
            "muted",
        )
    return fixtures


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

        fixtures = await _fetch_shared_fixtures(ws)

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
                    fixtures=fixtures,
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

    fixtures = await _fetch_shared_fixtures(ws)

    try:
        async for kind, payload, tone in run_cypress(
            session,
            session["feature"],
            session["script"],
            slug,
            fixtures=fixtures,
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
        "pushed": False, "process": None, "pending_generate": None,
        "current_task": None,
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
                session["current_task"] = asyncio.create_task(handle_interrupt(websocket, session, msg))

            elif action == "interrupt":
                session["current_task"] = asyncio.create_task(handle_interrupt(websocket, session, msg))

            elif action == "fetch":
                session["current_task"] = asyncio.create_task(handle_fetch(websocket, session, msg))

            elif action == "generate":
                session["current_task"] = asyncio.create_task(handle_generate(websocket, session, msg))

            elif action == "generate_decision":
                session["current_task"] = asyncio.create_task(handle_generate_decision(websocket, session, msg))

            elif action == "approve":
                session["current_task"] = asyncio.create_task(handle_approve(websocket, session))

            elif action == "reject":
                session["feature"] = None
                session["script"]  = None
                session["resolved"] = None
                session["pushed"]  = False
                session.pop("screens", None)
                session.pop("pending_generate", None)
                await send_log(websocket, "rejected — nothing pushed.", "muted")
                await send_status(websocket, "idle")

            elif action == "run":
                session["current_task"] = asyncio.create_task(handle_run(websocket, session))

            elif action == "terminate":
                task = session.get("current_task")
                cancelled = False
                if task is not None and not task.done():
                    task.cancel()
                    cancelled = True
                proc = session.get("process")
                if proc is not None:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                    session["process"] = None
                    cancelled = True
                session["feature"] = None
                session["script"]  = None
                session["resolved"] = None
                session["pushed"]  = False
                session.pop("screens", None)
                session.pop("pending_generate", None)
                await send_log(
                    websocket,
                    "terminated — cancelled the in-progress run." if cancelled else "nothing in progress to terminate.",
                    "muted",
                )
                await send_status(websocket, "idle")

            else:
                await send_error(websocket, f"unknown action: {action}")

    except WebSocketDisconnect:
        logger.info("qc session disconnected")
        if session.get("process") is not None:
            try:
                session["process"].kill()
            except ProcessLookupError:
                pass