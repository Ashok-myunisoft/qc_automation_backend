import asyncio
import base64
import logging
import posixpath
import re
import tempfile
from pathlib import Path
import logger_config
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from service.project_reader import ProjectReader
from service.gitlab_service import GitLabService
from service.architecture_resolver import (
    build_new_path, ResolvedSource,
    resolve_existing_precise, resolve_module_screens_precise,
    resolve_source_screen_precise, resolve_source_module_screens_precise,
    filter_tree_by_module,
)
from service.cypress_runner import run_cypress, CypressRunError
from Agents.project_analyze_agent import ProjectAnalysisAgent
from Agents.test_case_agent import TestCaseAgent
from Agents.script_generate_agent import ScriptGenerateAgent
from Agents.validate_agent import ValidateAgent
from Agents.append_agent import AppendAgent
from Agents.business_context_agent import BusinessContextAgent
from service import report_builder
from service.cypress_runner import clear_stash as clear_screenshot_stash

logger = logging.getLogger(__name__)

app = FastAPI()

project_analysis_agent = ProjectAnalysisAgent()
test_case_agent        = TestCaseAgent()
script_generate_agent  = ScriptGenerateAgent()
validate_agent         = ValidateAgent()
append_agent           = AppendAgent()
business_context_agent = BusinessContextAgent()

reader = ProjectReader()
# Use the default Windows event loop policy instead of explicitly setting the
# deprecated WindowsProactorEventLoopPolicy. On modern Python versions this is
# already the recommended loop implementation.

def _gitlab_service() -> GitLabService:
    return GitLabService()


def _source_gitlab_service() -> GitLabService:
    return GitLabService(env_prefix="SOURCE_GITLAB")


_IMPORT_RE = re.compile(r"""from\s+['"]([^'"]+)['"]""")

_MAX_IMPORT_DEPTH = 2
_MAX_EXTRA_FILES = 40


def _resolve_import_to_tree_path(import_path: str, current_dir: str, tree_set: set) -> str | None:
    """Resolves a TS import specifier to a real file path in the repo tree.
    Handles both styles seen in this codebase:
      - relative ('./x', '../../lib/x') -> resolved against the importing
        file's own directory
      - bare, repo-root-relative specifiers (this project's Nx-style TS
        path mapping, e.g. 'libs/gbdirectives/src/lib/gb-form-controls.module',
        'features/gbdialogbox/gbdialogbox.component') -> tried directly
        against the tree, since these already read like real repo paths
    External packages (@angular/*, rxjs, etc.) simply won't match anything
    in the tree and are silently skipped — no explicit allow/deny list
    needed."""
    bases = [posixpath.normpath(posixpath.join(current_dir, import_path))] if import_path.startswith(".") else [import_path]
    for base in bases:
        for suffix in ("", ".ts", "/index.ts"):
            candidate = base + suffix
            if candidate in tree_set:
                return candidate
    return None


def _fetch_source_project_context(src: GitLabService, resolved: ResolvedSource,
                                   source_tree: list[str] | None = None) -> dict:
    """Pulls every file in the resolved source-repo folder into a temp dir
    (preserving the folder's relative layout) and reuses ProjectReader as-is,
    so the agent pipeline's input shape is unchanged whether the source came
    from an upload or a repo fetch.

    If source_tree is given, ALSO follows local TS imports out of the
    screen's own files into shared library components (this codebase keeps
    reusable components like gb-picklist/gb-input in a separate libs/
    folder, imported by the screen rather than defined in it) — up to
    _MAX_IMPORT_DEPTH hops and _MAX_EXTRA_FILES total. Without this,
    ProjectAnalysisAgent never sees where a field's real data-cy attribute
    actually comes from when it's defined inside the LIBRARY component's
    own template, not the screen's — confirmed to be why locators were
    coming back empty/invented for library-backed fields.

    IMPORTANT: callers MUST pass the FULL, unfiltered source tree here, not
    the module-filtered one — library component files (gbcheckbox,
    gbcombobox, gbinput, etc.) never contain the module or screen name in
    their own path, so a module-name filter strips every one of them out
    before this function ever gets a chance to look for them. This check
    is free (pure Python set membership, no LLM involved), so there's no
    cost to using the full tree here even though the filtered tree is
    still correctly used for the separate ArchitectureAgent LLM call."""
    with tempfile.TemporaryDirectory() as temp_dir:
        target_dir = Path(temp_dir) / resolved.dir
        target_dir.mkdir(parents=True, exist_ok=True)

        fetched: dict[str, str] = {}
        for filename in resolved.files:
            path = f"{resolved.dir}/{filename}"
            content = src.fetch_file(path)
            if content is None:
                continue
            (target_dir / filename).write_text(content, encoding="utf-8")
            fetched[path] = content

        if source_tree:
            tree_set = set(source_tree)
            extra_fetched = 0
            frontier = list(fetched.items())
            seen_paths = set(fetched.keys())

            for _ in range(_MAX_IMPORT_DEPTH):
                if extra_fetched >= _MAX_EXTRA_FILES or not frontier:
                    break
                next_frontier = []
                for path, content in frontier:
                    if not path.endswith(".ts"):
                        continue
                    current_dir = path.rsplit("/", 1)[0] if "/" in path else ""
                    for import_path in _IMPORT_RE.findall(content):
                        if extra_fetched >= _MAX_EXTRA_FILES:
                            break
                        resolved_ts = _resolve_import_to_tree_path(import_path, current_dir, tree_set)
                        if not resolved_ts or resolved_ts in seen_paths:
                            continue
                        # Fetch the matched .ts, and its sibling .html template
                        # if it's a component — that's where a data-cy
                        # binding actually lives, not the .ts logic file.
                        html_sibling = resolved_ts[:-3] + ".html" if resolved_ts.endswith(".ts") else None
                        for candidate_path in (resolved_ts, html_sibling):
                            if not candidate_path or candidate_path in seen_paths:
                                continue
                            candidate_content = src.fetch_file(candidate_path)
                            if candidate_content is None:
                                continue
                            seen_paths.add(candidate_path)
                            dest = Path(temp_dir) / candidate_path
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            dest.write_text(candidate_content, encoding="utf-8")
                            extra_fetched += 1
                            if candidate_path.endswith(".ts"):
                                next_frontier.append((candidate_path, candidate_content))
                frontier = next_frontier

            if extra_fetched:
                logger.info(
                    "pulled in %d additional library file(s) referenced by %s (up to depth %d)",
                    extra_fetched, resolved.dir, _MAX_IMPORT_DEPTH,
                )

        project = reader.read_project(temp_dir)
        project["primary_dir"] = resolved.dir
        return project


def _extract_source_hints(project_context: dict, limit: int = 30) -> list[str]:
    """Pulls candidate identifiers out of the already-fetched Angular source
    for BusinessContextAgent to start from — API route fragments and form
    field names tend to survive table/column renames better than a screen's
    display name does (see prompts/business_context_prompt.txt). This is a
    best-effort regex scan, not a real TS/HTML parser — good enough as a
    starting signal, not treated as ground truth by the agent itself."""
    route_re = re.compile(r"""['"`]/api/([A-Za-z0-9_/-]+)['"`]""")
    field_re = re.compile(r"""(?:formControlName|data-cy|\[data-cy\])\s*=\s*['"]([A-Za-z0-9_]+)['"]""")

    hints: list[str] = []
    seen = set()
    for f in project_context.get("source_files", []):
        content = f.get("content", "")
        for pattern in (route_re, field_re):
            for m in pattern.finditer(content):
                val = m.group(1)
                if val and val not in seen:
                    seen.add(val)
                    hints.append(val)
                if len(hints) >= limit:
                    return hints
    return hints


def _build_locator_map(project_analysis: dict) -> dict[str, str]:
    """Distills the full project_analysis JSON (from ProjectAnalysisAgent)
    down to a small name -> real, verified locator lookup — just
    {name: data-cy value}, nothing else from that larger structure. Handed
    to ScriptGenerateAgent so it can use an already-confirmed selector
    instead of guessing one from the name that ended up quoted in the
    Gherkin.

    Walks FOUR sources within each page, all of which carry real
    per-element locators in ProjectAnalysisAgent's schema:
      - forms[].fields[]                          (field-level inputs)
      - business_actions[]                        (Save/Delete/Print/etc. buttons)
      - tables[].row_actions[] / toolbar_actions[] (grid row + toolbar buttons)
      - dialogs[].confirm_button / cancel_button   (dialog action buttons)

    Confirmed necessary for report/view screens (e.g. PaySlip) that have
    grids and buttons but no input form fields at all — walking only
    forms[].fields[] silently returns an empty map for those screens even
    when ProjectAnalysisAgent found real locators for their buttons/grids.

    Fields are keyed by BOTH label and control_name (not guaranteed which
    one TestCaseAgent quotes in a given step); actions/buttons are keyed
    by their action_name, since that's the only name Gherkin steps refer
    to them by (e.g. 'I click the "Get Payslip" button')."""
    locator_map: dict[str, str] = {}

    def _best_locator(obj: dict) -> str | None:
        locator = (
            obj.get("preferred_locator")
            or obj.get("data_cy")
            or obj.get("id")
            or obj.get("name")
        )
        return locator if locator and locator != "Unknown" else None

    for module in project_analysis.get("modules", []) or []:
        for page in module.get("pages", []) or []:
            # --- form fields ---
            for form in page.get("forms", []) or []:
                for field in form.get("fields", []) or []:
                    locator = _best_locator(field)
                    if not locator:
                        continue
                    for key in (field.get("label"), field.get("control_name"), field.get("data_cy")):
                        if key and key != "Unknown":
                            locator_map[key] = locator

            # --- page-level business actions (Save, Delete, Print, Get Payslip, ...) ---
            for action in page.get("business_actions", []) or []:
                if not isinstance(action, dict):
                    continue  # tolerate older analyses that still emit bare strings
                locator = _best_locator(action)
                name = action.get("action_name")
                if locator and name and name != "Unknown":
                    locator_map[name] = locator

            # --- table/grid row + toolbar actions ---
            for table in page.get("tables", []) or []:
                if not isinstance(table, dict):
                    continue
                for action in (table.get("row_actions") or []) + (table.get("toolbar_actions") or []):
                    if not isinstance(action, dict):
                        continue
                    locator = _best_locator(action)
                    name = action.get("action_name")
                    if locator and name and name != "Unknown":
                        locator_map[name] = locator

            # --- dialog confirm/cancel buttons ---
            for dialog in page.get("dialogs", []) or []:
                if not isinstance(dialog, dict):
                    continue
                for button_key, fallback_name in (("confirm_button", "Confirm"), ("cancel_button", "Cancel")):
                    button = dialog.get(button_key)
                    if not isinstance(button, dict):
                        continue
                    locator = _best_locator(button)
                    if locator:
                        name = dialog.get("trigger_action") or dialog.get("dialog_type") or fallback_name
                        locator_map.setdefault(f"{name} {fallback_name}", locator)
                        locator_map.setdefault(fallback_name, locator)

    return locator_map


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

    await send_log(ws, "fetched — review below, then Run.", "success")
    await send_artifacts(ws, session)
    await send_status(ws, "awaiting_review")


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
                                user_request: str, label: str | None = None,
                                source_tree: list[str] | None = None) -> dict | None:
    """Runs the analysis -> test-case -> script -> validate pipeline for one
    resolved source screen. Returns {feature, script, validation} or None on
    failure (caller decides whether that's fatal or skip-and-continue).
    source_tree: the FULL, unfiltered source tree (not the module-filtered
    one — see _fetch_source_project_context's docstring for why), passed
    through so it can follow local imports into the shared libs/ folder.
    Callers without a tree handy just omit it; behavior degrades
    gracefully to screen-only."""
    tag = f"[{label}] " if label else ""
    try:
        await send_log(ws, f"{tag}fetching source files...", "secondary")
        project_context = _fetch_source_project_context(src, resolved_source, source_tree)

        await send_log(ws, f"{tag}running project analysis agent...", "secondary")
        project_analysis = await project_analysis_agent.analyze(
            project_context=project_context, user_request=user_request,
        )

        source_hints = _extract_source_hints(project_context)
        await send_log(ws, f"{tag}looking up real test data ({len(source_hints)} hint(s) from source)...", "secondary")
        business_context_result = await business_context_agent.find_context(module, screen_name, source_hints)
        if "error" in business_context_result:
            await send_log(ws, f"{tag}business context: {business_context_result['error']} — using placeholder values.", "muted")
            business_context = None
        else:
            preview = ", ".join(f"{k}={v!r}" for k, v in list(business_context_result.items())[:3])
            await send_log(ws, f"{tag}business context found: {preview} — spot-check one of these against the DB if unsure.", "success")
            business_context = business_context_result

        await send_log(ws, f"{tag}running test case agent...", "secondary")
        test_cases = await test_case_agent.generate_test_cases(
            project_analysis=project_analysis, user_request=user_request,
            business_context=business_context,
        )

        locator_map = _build_locator_map(project_analysis)
        await send_log(ws, f"{tag}running script generate agent ({len(locator_map)} known locator(s))...", "secondary")
        generated_script = await script_generate_agent.generate_script(test_cases=test_cases, locator_map=locator_map)

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

    await send_log(ws, "checking the QC repo for existing tests...", "secondary")
    out_tree = None
    out_gl   = None
    try:
        out_gl   = _gitlab_service()
        out_tree = out_gl.get_repo_tree()
    except Exception as e:
        await send_log(ws, f"could not check output repo ({e}) — will resolve path on approve.", "muted")

    if scope == "module":
        await send_log(ws, "reading source repo structure...", "secondary")
        try:
            src      = _source_gitlab_service()
            src_tree = src.get_repo_tree()
        except Exception as e:
            await send_error(ws, f"source gitlab connection failed: {e}")
            return

        await send_log(ws, f"scanned {len(src_tree)} source files — looking for every screen in {module}...", "secondary")

        filtered_src_tree = filter_tree_by_module(src_tree, module)
        candidates = await resolve_source_module_screens_precise(filtered_src_tree, module)
        if not candidates:
            await send_log(ws, f"no source screens found under module '{module}' in the source repo.", "danger")
            await send_status(ws, "not_found")
            return

        await send_log(ws, f"found {len(candidates)} screen(s) in {module}: " +
                       ", ".join(c.dir for c in candidates), "success")

        conflicts = {}
        fresh_candidates = []

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

        screens = []
        for screen_name, c in fresh_candidates:
            screen_request = user_request or f"Generate Cypress tests for the {screen_name} screen in the {module} module."
            outcome = await _generate_one_screen(ws, src, module, screen_name, c, screen_request, label=screen_name, source_tree=src_tree)
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

    resolved_output = None
    existed = False
    existing_feature = existing_script = None
    if out_tree is not None:
        resolved_output, existed, existing_feature, existing_script = await _check_output_existing(out_gl, out_tree, module, screen)

    if existed:
        session["pending_generate"] = {
            "module": module, "screen": screen, "scope": "screen", "user_request": user_request,
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

    await send_log(ws, "reading source repo structure...", "secondary")
    try:
        src      = _source_gitlab_service()
        src_tree = src.get_repo_tree()
    except Exception as e:
        await send_error(ws, f"source gitlab connection failed: {e}")
        return

    await send_log(ws, f"scanned {len(src_tree)} source files — looking for {module} / {screen}...", "secondary")

    filtered_src_tree = filter_tree_by_module(src_tree, module)
    resolved_source = await resolve_source_screen_precise(filtered_src_tree, module, screen)
    if resolved_source is None:
        await send_log(ws, f"no source files found for '{module} / {screen}' in the source repo.", "danger")
        await send_status(ws, "not_found")
        return

    if resolved_source.ambiguous:
        await send_log(ws, f"more than one close match ({resolved_source.resolved_by}) — picked {resolved_source.dir} (confidence {resolved_source.confidence}). Double-check.", "accent")
    else:
        await send_log(ws, f"matched source {resolved_source.dir} via {resolved_source.resolved_by} (confidence {resolved_source.confidence})", "success")

    user_request = user_request or f"Generate Cypress tests for the {screen} screen in the {module} module."

    outcome = await _generate_one_screen(ws, src, module, screen, resolved_source, user_request, source_tree=src_tree)
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

    screen          = pending["screen"]
    resolved_output = pending["resolved_output"]
    validation      = None

    if decision == "replace":
        try:
            src      = _source_gitlab_service()
            src_tree = src.get_repo_tree()
        except Exception as e:
            await send_error(ws, f"source gitlab connection failed: {e}")
            session.pop("pending_generate", None)
            return

        await send_log(ws, f"scanned {len(src_tree)} source files — looking for {module} / {screen}...", "secondary")
        filtered_src_tree = filter_tree_by_module(src_tree, module)
        resolved_source = await resolve_source_screen_precise(filtered_src_tree, module, screen)
        if resolved_source is None:
            await send_error(ws, f"no source files found for '{module} / {screen}' in the source repo.")
            session.pop("pending_generate", None)
            return
        if resolved_source.ambiguous:
            await send_log(ws, f"more than one close match ({resolved_source.resolved_by}) — picked {resolved_source.dir} (confidence {resolved_source.confidence}). Double-check.", "accent")
        else:
            await send_log(ws, f"matched source {resolved_source.dir} via {resolved_source.resolved_by} (confidence {resolved_source.confidence})", "success")

        request = user_request or f"Generate Cypress tests for the {screen} screen in the {module} module."
        outcome = await _generate_one_screen(ws, src, module, screen, resolved_source, request, source_tree=src_tree)
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


async def handle_approve(ws: WebSocket, session: dict, msg: dict | None = None):
    """Approves and pushes to GitLab. If the UI sent edited content in
    msg (Task C — inline edit before commit), that edited text replaces
    the AI-generated content in the session BEFORE the push, so what's
    pushed to GitLab is exactly what the human reviewed. Works for both
    single-screen (msg.feature / msg.script) and module scope
    (msg.edits = [{index, feature, script}, ...] for the selected
    entries in session['screens'])."""
    msg = msg or {}

    if session.get("scope") == "module" and session.get("screens"):
        for edit in msg.get("edits") or []:
            i = edit.get("index")
            if not isinstance(i, int) or i < 0 or i >= len(session["screens"]):
                continue
            if isinstance(edit.get("feature"), str):
                session["screens"][i]["feature"] = edit["feature"]
            if isinstance(edit.get("script"), str):
                session["screens"][i]["script"] = edit["script"]

        await send_status(ws, "running")
        try:
            gl = _gitlab_service()
            for s in session["screens"]:
                resolved = s["resolved"]
                gl.create_or_update_file(
                    resolved.feature_path, s["feature"],
                    commit_message=f"QC: add/update feature file for {session.get('module')}/{resolved.slug}",
                )
                gl.create_or_update_file(
                    resolved.script_path, s["script"],
                    commit_message=f"QC: add/update cypress script for {session.get('module')}/{resolved.slug}",
                )
        except Exception as e:
            await send_error(ws, f"gitlab push failed: {e}")
            return

        session["pushed"] = True
        await send_log(ws, f"pushed {len(session['screens'])} screen(s) to gitlab.", "success")
        await send_artifacts(ws, session)
        await send_status(ws, "awaiting_review")
        return

    if not session.get("feature") or not session.get("script") or not session.get("resolved"):
        await send_error(ws, "nothing to approve — generate something first.")
        return

    edited_feature = msg.get("feature")
    edited_script  = msg.get("script")
    if isinstance(edited_feature, str):
        session["feature"] = edited_feature
    if isinstance(edited_script, str):
        session["script"] = edited_script

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


async def handle_set_env(ws: WebSocket, session: dict, msg: dict):
    """Sets (or replaces) the test-target config used by Cypress runs —
    baseUrl + DB name/username/password, sent from the frontend's env
    popup. Can be called at any point in the session (before or after any
    number of runs) — always a full replace of whatever was set before,
    never merged with cypress.env.json's static TestConnections.UILogin.
    All four fields are required; there is no partial/fallback state."""
    base_url  = (msg.get("baseUrl")  or "").strip()
    db_name   = (msg.get("dbName")   or "").strip()
    user_name = (msg.get("userName") or "").strip()
    password  = msg.get("password") or ""

    missing = [name for name, val in (
        ("baseUrl", base_url), ("dbName", db_name),
        ("userName", user_name), ("password", password),
    ) if not val]
    if missing:
        await send_error(ws, f"test environment config incomplete — missing: {', '.join(missing)}.")
        return

    session["test_env"] = {
        "baseUrl": base_url, "dbName": db_name,
        "userName": user_name, "password": password,
    }
    await send_log(ws, f"test environment set — {base_url} (db={db_name}, user={user_name}).", "success")
    await ws.send_json({
        "type":     "env_set",
        "baseUrl":  base_url,
        "dbName":   db_name,
        "userName": user_name,
        # password intentionally never echoed back
    })


async def handle_run(ws: WebSocket, session: dict):
    if not session.get("test_env"):
        await send_error(
            ws,
            "no test environment set — set baseUrl / db / username / password before running.",
        )
        return

    if session.get("scope") == "module" and session.get("screens"):
        screens = session["screens"]

        if session.get("origin") == "generate" and not session.get("pushed"):
            await send_error(ws, "approve the generated files before running.")
            return

        clear_screenshot_stash()
        session["run_results"] = []

        await send_status(ws, "running")
        await send_log(ws, f"cypress: running {len(screens)} screen(s) in this module...", "secondary")

        fixtures = await _fetch_shared_fixtures(ws)

        summary = []
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
                    test_env=session["test_env"],
                ):
                    if kind == "log":
                        await send_log(ws, f"[{label}] {payload}", tone or "secondary")
                    elif kind == "exit":
                        passed = payload == 0
                        summary.append((label, passed, payload))
                        tone_ = "success" if passed else "danger"
                        await send_log(ws, f"[{label}] {'PASSED' if passed else 'FAILED'} (exit {payload})", tone_)
                    elif kind == "result":
                        session.setdefault("run_results", []).append(payload)
            except CypressRunError as e:
                summary.append((label, False, None))
                await send_log(ws, f"[{label}] run error: {e}", "danger")
                continue
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

    clear_screenshot_stash()
    session["run_results"] = []

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
            test_env=session["test_env"],
        ):
            if kind == "log":
                await send_log(ws, payload, tone or "secondary")
            elif kind == "exit":
                passed = payload == 0
                await send_status(ws, "done")
                await ws.send_json({"type": "result", "passed": passed, "exit_code": payload})
            elif kind == "result":
                session.setdefault("run_results", []).append(payload)
    except CypressRunError as e:
        await send_error(ws, str(e))
    except asyncio.CancelledError:
        await send_log(ws, "run cancelled.", "muted")
        raise


async def handle_report(ws: WebSocket, session: dict):
    """Task A — Excel report. Deterministic: every number comes straight
    from mochawesome via cypress_runner's stashed run_results, no LLM
    involved (see service/report_builder.py's module docstring). Sent as
    base64 since it's binary — the frontend decodes it straight into a
    file download, no inline preview (an .xlsx doesn't render usefully
    in a browser iframe the way the screenshot HTML does)."""
    run_results = session.get("run_results") or []
    if not run_results:
        await send_error(ws, "no run data yet — run something first.")
        return

    module = session.get("module") or ""
    xlsx_bytes = report_builder.build_report_xlsx(module, run_results)
    filename = f"qc-report-{module or 'run'}.xlsx".replace(" ", "_")
    await ws.send_json({
        "type":            "report",
        "content_base64":  base64.b64encode(xlsx_bytes).decode("ascii"),
        "filename":        filename,
        "mime":            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    })


async def handle_screenshots(ws: WebSocket, session: dict):
    run_results = session.get("run_results") or []
    if not run_results:
        await send_error(ws, "no run data yet — run something first.")
        return

    module = session.get("module") or ""
    html = report_builder.build_screenshots_html(module, run_results)
    filename = f"qc-screenshots-{module or 'run'}.html".replace(" ", "_")
    await ws.send_json({
        "type":     "screenshots",
        "html":     html,
        "filename": filename,
    })


@app.websocket("/ws/qc")
async def qc_session(websocket: WebSocket):
    await websocket.accept()
    session = {
        "module": None, "screen": None, "origin": None,
        "feature": None, "script": None, "resolved": None,
        "pushed": False, "process": None, "pending_generate": None,
        "current_task": None,
        # Test-target config (baseUrl / dbName / userName / password), set
        # from the frontend's env popup via the "set_env" action. Session
        # scoped — persists until changed, survives reject/terminate/reset,
        # NOT tied to any single run. No fallback to cypress.env.json:
        # run is refused until this is set (see handle_run's guard).
        "test_env": None,
    }

    try:
        while True:
            msg    = await websocket.receive_json()
            action = msg.get("action")

            if action == "set_env":
                session["current_task"] = asyncio.create_task(handle_set_env(websocket, session, msg))

            elif action == "fetch":
                session["current_task"] = asyncio.create_task(handle_fetch(websocket, session, msg))

            elif action == "generate":
                session["current_task"] = asyncio.create_task(handle_generate(websocket, session, msg))

            elif action == "generate_decision":
                session["current_task"] = asyncio.create_task(handle_generate_decision(websocket, session, msg))

            elif action == "approve":
                session["current_task"] = asyncio.create_task(handle_approve(websocket, session, msg))

            elif action == "reject":
                session["feature"] = None
                session["script"]  = None
                session["resolved"] = None
                session["pushed"]  = False
                session.pop("screens", None)
                session.pop("pending_generate", None)
                session.pop("run_results", None)
                clear_screenshot_stash()
                await send_log(websocket, "rejected — nothing pushed.", "muted")
                await send_status(websocket, "idle")

            elif action == "run":
                session["current_task"] = asyncio.create_task(handle_run(websocket, session))

            elif action == "report":
                session["current_task"] = asyncio.create_task(handle_report(websocket, session))

            elif action == "screenshots":
                session["current_task"] = asyncio.create_task(handle_screenshots(websocket, session))

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
                session.pop("run_results", None)
                clear_screenshot_stash()
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