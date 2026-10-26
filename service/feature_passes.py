"""Feature generation in topic passes (flag USE_FEATURE_PASSES), spec 01.

The agent writes the scenarios; code decides which passes a screen needs, packages the raw source and verified facts,
keeps a record ledger, orders and merges the passes, and (with USE_COVERAGE_LOOP) asks for the gaps only."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Awaitable, Callable

from service import chunk_analysis, coverage_loop, extended_steps, pass_planner, run_metrics, screen_spec, value_gen

logger = logging.getLogger(__name__)

STANDARD_MESSAGES = ["Details Saved Successfully With Code:", "Details Updated Successfully With Code:",
                     "Details have been deleted successfully"]


def enabled() -> bool:
    return os.getenv("USE_FEATURE_PASSES", "0").strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def applies(spec: dict | None, is_mm: bool) -> tuple[bool, str]:
    if not enabled():
        return False, "USE_FEATURE_PASSES=0"
    if is_mm:
        return False, "MM screen keeps the old path"
    if not spec or not spec.get("fields"):
        return False, "no formjson spec for this screen"
    return True, "topic passes"


# --------------------------------------------------------------------------- context packager
def _source_block(project_context: dict, budget: int) -> str:
    files = [f for f in project_context.get("source_files") or [] if not str(f.get("path", "")).startswith("[backend]")]
    primary = project_context.get("primary_dir") or ""

    def rank(f):
        p = str(f.get("path", ""))
        own = p.startswith(primary + "/") if primary else True
        return (0 if own and p.endswith(".ts") else 1 if own and p.endswith(".html") else 2, len(f.get("content") or ""))

    parts, used = [], 0
    for f in sorted(files, key=rank):
        body = f.get("content", "") or ""
        if not body or ".spec." in str(f.get("path", "")):
            continue
        if used + len(body) > budget:
            if rank(f)[0] <= 1:
                body = body[: max(0, budget - used)]
            else:
                continue
        if not body:
            break
        used += len(body)
        parts.append(f"----- {f.get('path')} -----\n{body}")
    return "\n\n".join(parts)


def _datacy_table(facts: dict) -> str:
    lines = ["VERIFIED DATA-CY (from the live app scan; use the base name as the field name in steps):"]
    for base, c in facts["controls"].items():
        lines.append(f"- {base}: data-cy={c['dataCy']} kind={c['kind']}")
    if len(lines) == 1:
        lines.append("- none verified: use the field names from the spec")
    return "\n".join(lines)


def _messages_block(facts: dict) -> str:
    out, seen = [], set()

    def add(label, m):
        if m and m not in seen:
            seen.add(m)
            out.append(f"- [{label}] {m}")

    for role, vals in (facts["messages"] or {}).items():
        for v in vals if isinstance(vals, list) else []:
            add(role, v)
    for r in facts["backend_rules"]:
        add("backend rule", r.get("message"))
    for m in STANDARD_MESSAGES:
        add("standard", m)
    es = (facts["behavior"] or {}).get("empty_state") or {}
    add("empty state", es.get("message"))
    return "KNOWN MESSAGES (ExpectedResult may ONLY be one of these):\n" + ("\n".join(out) if out else "- none")


def build_prefix(project_context: dict, project_analysis: dict, spec: dict, facts: dict,
                 business_context: dict | None, user_request: str) -> str:
    """STABLE PREFIX: identical for every pass of one screen (so prompt caching applies)."""
    pool = project_analysis.get("value_pool") or {}
    try:
        spec_text = screen_spec.rules_for("TEST_CASES") + "\n\n" + value_gen.render_for_test_cases(spec, pool, [])
    except Exception:  # noqa: BLE001
        logger.exception("feature passes: spec rendering failed - using the compact spec")
        spec_text = json.dumps(screen_spec.compact(spec), indent=2)
    nav = ""
    page = ((project_analysis.get("modules") or [{}])[0].get("pages") or [{}])[0]
    path = [p for p in page.get("navigation_path") or [] if isinstance(p, str)]
    if path:
        nav = "NAVIGATION: the Background step is  I navigate through the following path " + " -> ".join(f'"{p}"' for p in path)
    return "\n\n".join([
        f"User request: {user_request}",
        "SCREEN SOURCE (raw files):\n" + _source_block(project_context, _int("PASS_SOURCE_CHAR_BUDGET", 120000)),
        spec_text,
        _datacy_table(facts),
        _messages_block(facts),
        nav,
        "BUSINESS CONTEXT (real values):\n" + (json.dumps(business_context, indent=2) if business_context else "none provided"),
        "STEP VOCABULARY (use ONLY these sentences, exactly as written):\n" + extended_steps.step_vocabulary(),
        "DETECTED SCREEN BEHAVIOUR (code-extracted facts with source snippets):\n"
        + (json.dumps(facts["behavior"], indent=2)[:9000] if facts["behavior"] else "none detected"),
    ]).strip()


def build_tail(p: pass_planner.Pass, facts: dict, ledger: list[dict], titles: list[str], first: bool) -> str:
    lines = [f"=== THIS PASS: {p.id} | title prefix: \"{p.title_prefix}\" ===", f"THEME: {p.theme_text}"]
    if p.id == "business_rules":
        lines.append("BACKEND RULES:\n" + "\n".join(f"- {r.get('condition') or 'always'} -> \"{r.get('message')}\"" for r in facts["backend_rules"]))
    if p.id == "delete":
        lines.append("Delete messages: " + " | ".join((facts["messages"].get("delete") or []) + [STANDARD_MESSAGES[2]])
                     + ". Prompt texts: 'Are you sure you want to delete' (list) and 'Are you sure you want to delete this record?' (form), "
                       "unless the source shows other wording.")
    if p.id == "tabs" and p.section:
        lines.append("TAB SECTION: " + json.dumps(p.section))
        lines.append(f"Wrap everything in ONE block starting with the line  Rule: {p.extra.get('label')}")
    if p.id != "form_core":
        lines.append(pass_planner.render_ledger(ledger))
    if titles:
        lines.append("TITLES ALREADY WRITTEN (do not repeat):\n" + "\n".join(f"- {t}" for t in titles[:120]))
    if p.id == "form_core":
        lines.append("Write the Feature: header and the Background: first. Include a dedicated Scenario Outline that saves "
                     "records for the later delete checks and the open-existing/update checks (distinct codes), titled "
                     f"\"{p.title_prefix} - Records for the later checks\".")
    else:
        lines.append("Start directly with Scenario Outline: (no Feature:, no Background:).")
    lines.append("Output only Gherkin.")
    return "\n\n".join(lines)


def _titles(text: str) -> list[str]:
    return [m.group(1).strip() for m in re.finditer(r"^\s*Scenario\s+(?:Outline|Template)\s*:\s*(.*)$", text or "", re.M | re.I)]


def _apply_updates(ledger: list[dict], update_text: str) -> list[dict]:
    """Second ledger: record values after the update pass ran (best effort, by key column)."""
    new = {r["key"].lower(): {"key": r["key"], "condition": r["condition"], "values": dict(r["values"])} for r in ledger}
    for steps, header, rows in pass_planner.examples_tables(update_text):
        if not re.search(r"click the Update button", steps, re.I):
            continue
        rec_col = next((i for i, h in enumerate(header) if h.lower() in ("record", "domaincode", "code")), None)
        if rec_col is None:
            continue
        for r in rows:
            rec = new.get(r[rec_col].lower()) if rec_col < len(r) else None
            if rec:
                for i, h in enumerate(header):
                    if i not in (0, rec_col) and h.lower() != "expectedresult" and i < len(r) and r[i]:
                        rec["values"][h] = r[i]
    return list(new.values())


def _delete_ledger(original: list[dict], after_update: list[dict], update_text: str) -> list[dict]:
    """Records the delete pass may use: those saved for deletion, and records the update pass did not touch."""
    touched = {r["key"].lower() for r, a in zip(original, after_update) if r["values"] != a["values"]}
    keep = [r for r in after_update if r["key"].lower() not in touched or re.search(r"delet", r["condition"], re.I)]
    return keep or after_update


def merge_passes(ordered: list[tuple[pass_planner.Pass, str]]) -> str:
    """create -> read_only -> update -> delete; tab Rule blocks go last so no plain scenario ends up inside a Rule."""
    head, plain, rules = "", [], []
    for p, text in ordered:
        text = chunk_analysis.strip_code_fences(text).strip()
        if not text:
            continue
        if p.id == "form_core" and not head:
            head = text
            continue
        if p.id == "tabs":
            rules.append(chunk_analysis.strip_to_rule_block(text, p.extra.get("label", "Section")))
        else:
            plain.append(chunk_analysis.strip_to_scenarios(text) or text)
    return chunk_analysis.merge_test_case_chunks(head, plain + rules) if head else \
        chunk_analysis.merge_test_case_chunks("", plain + rules)


# --------------------------------------------------------------------------- runner
async def run(agent, project_context: dict, project_analysis: dict, spec: dict, business_context: dict | None,
              user_request: str, log: Callable[[str], Awaitable[None]], metrics: run_metrics.ScreenRun | None = None):
    """Returns (feature_text, info). Raises on total failure so the caller can fall back to the old path."""
    facts = pass_planner.build_facts(spec, project_analysis)
    passes = pass_planner.plan_passes(facts)
    await log(f"planned {len(passes)} passes: " + ", ".join(p.id + (f"[{p.extra['label']}]" if p.extra.get("label") else "") for p in passes))
    prefix = build_prefix(project_context, project_analysis, spec, facts, business_context, user_request)
    sem = asyncio.Semaphore(_int("FEATURE_PASS_MAX_PARALLEL", 3))
    done: dict[int, str] = {}
    counter = {"n": 0}
    total = len(passes)

    async def one(idx: int, p: pass_planner.Pass, ledger, titles, first=False):
        async with sem:
            tail = build_tail(p, facts, ledger, titles, first)
            try:
                text = await agent.generate_pass(prefix, tail)
            except Exception as e:  # noqa: BLE001
                logger.exception("pass %s failed", p.id)
                await log(f"pass '{p.id}' failed ({e}) - continuing with the other passes")
                text = ""
            counter["n"] += 1
            c = run_metrics.counts(text)
            logger.info("pass %d/%d '%s': %d outline(s), %d row(s)", counter["n"], total, p.id, c["outlines"], c["rows"])
            await log(f"pass {counter['n']}/{total} '{p.id}': {c['outlines']} outline(s), {c['rows']} row(s)")
            if metrics:
                metrics.llm()
            done[idx] = text
            return text

    ids = list(range(len(passes)))
    core_idx = next(i for i in ids if passes[i].id == "form_core")
    core_text = await one(core_idx, passes[core_idx], [], [], True)
    if not core_text.strip():
        raise RuntimeError("the form_core pass returned nothing")

    group = lambda g: [i for i in ids if passes[i].order_group == g and i != core_idx and not (g == "create" and passes[i].id == "tabs")]  # noqa: E731
    titles = _titles(core_text)
    # create group (non-tab): parallel, each told the titles written so far
    await asyncio.gather(*[one(i, passes[i], [], list(titles)) for i in group("create")])
    create_texts = [core_text] + [done.get(i, "") for i in group("create")]
    ledger = pass_planner.build_ledger("\n".join(create_texts))
    titles = [t for tx in create_texts for t in _titles(tx)]
    # tab passes (create group): also parallel
    tab_idx = [i for i in ids if passes[i].id == "tabs"]
    await asyncio.gather(*[one(i, passes[i], [], list(titles)) for i in tab_idx])
    # read-only group
    await asyncio.gather(*[one(i, passes[i], ledger, list(titles)) for i in group("read_only")])
    titles += [t for i in group("read_only") for t in _titles(done.get(i, ""))]
    # update, then delete with the second ledger
    upd_text = ""
    for i in group("update"):
        upd_text = await one(i, passes[i], ledger, list(titles))
        titles += _titles(upd_text)
    ledger2 = _apply_updates(ledger, upd_text)
    for i in group("delete"):
        await one(i, passes[i], _delete_ledger(ledger, ledger2, upd_text), list(titles))

    ordered = pass_planner.order_passes([(passes[i], done.get(i, "")) for i in ids])
    merged = merge_passes(ordered)
    info = {"facts": facts, "passes": [p.id for p in passes], "ledger": ledger, "prefix": prefix}
    if metrics:
        metrics.stage("passes_merged", merged)
    return merged, info


async def gap_fill(agent, prefix: str, facts: dict, ledger: list[dict], gap_lines: list[str], current: str) -> str:
    tail = "\n\n".join([
        "=== THIS PASS: coverage gaps | title prefix: \"Coverage\" ===",
        "THEME: write scenarios ONLY for these gaps, one Scenario Outline per gap group, nothing else:\n"
        + "\n".join(f"- {g}" for g in gap_lines),
        pass_planner.render_ledger(ledger),
        "TITLES ALREADY WRITTEN (do not repeat):\n" + "\n".join(f"- {t}" for t in _titles(current)[:150]),
        "Start directly with Scenario Outline: (no Feature:, no Background:). Output only Gherkin.",
    ])
    raw = await agent.generate_pass(prefix, tail)
    return chunk_analysis.strip_to_scenarios(raw)


# --------------------------------------------------------------------------- protect extended scenarios from refine_feature
def uses_extended_step(outline_text: str, defined_base: set[str] | None = None) -> bool:
    return bool(re.search(
        r"new record field|I clear the \"|the form (should|fields)|action should be|field should show|I click the (Reset|Close) button|"
        r"I open the existing record|I request to delete|delete prompt|confirm the delete|cancel the delete|"
        r"I (should still|should no longer) see the record|I search the list|I clear the list search|in the list|"
        r"list row|I filter the list|I sort the list|list should be sorted|I switch to the|I should see the (\"|empty)|"
        r"I remember the number|the number shown", outline_text))


def refine_protecting_extended(feature: str, refine_fn: Callable[[str], tuple[str, dict]]):
    """Runs the existing feature_quality.refine_feature on everything EXCEPT outlines that use extended steps
    (it was written for form/CRUD outlines). Stashed outlines are put back where their marker ends up."""
    lines = feature.split("\n")
    starts = [i for i, l in enumerate(lines) if re.match(r"^\s*(Scenario\s+(Outline|Template)\s*:|Scenario\s*:|Rule\s*:|Background\s*:|@)", l)]
    # cut into blocks at Scenario/Rule/Background lines (tags stay with the next block)
    cuts, prev_tag = [], False
    for i, l in enumerate(lines):
        if re.match(r"^\s*@", l):
            if not prev_tag:
                cuts.append(i)
            prev_tag = True
            continue
        if re.match(r"^\s*(Scenario\s+(Outline|Template)\s*:|Scenario\s*:|Rule\s*:|Background\s*:)", l):
            if not prev_tag:
                cuts.append(i)
        prev_tag = False
    if not cuts:
        return refine_fn(feature)
    blocks = [lines[:cuts[0]]] + [lines[a:b] for a, b in zip(cuts, cuts[1:] + [len(lines)])]
    stash, out = {}, []
    for blk in blocks:
        text = "\n".join(blk)
        if blk and re.match(r"^\s*(@.*|Scenario\s+(Outline|Template)\s*:)", blk[0]) and uses_extended_step(text):
            key = f"# __KEEP_{len(stash)}__"
            stash[key] = text
            out.append(key)
        else:
            out.append(text)
    refined, report = refine_fn("\n".join(out))
    lost = []
    for key, text in stash.items():
        if key in refined:
            refined = refined.replace(key, text.rstrip("\n"), 1)
        else:
            lost.append(text)
    if lost:
        refined = refined.rstrip("\n") + "\n\n" + "\n\n".join(t.rstrip("\n") for t in lost) + "\n"
        logger.warning("refine_feature moved %d extended-step outline(s) to the end of the feature", len(lost))
    if isinstance(report, dict):
        report.setdefault("notes", []).append(f"{len(stash)} outline(s) with list/form-state steps were left as written by the agent")
    return refined, report
