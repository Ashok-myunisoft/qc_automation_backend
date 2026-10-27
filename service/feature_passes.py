"""Feature generation in topic passes (every screen that has a formjson spec and is not an MM screen), spec 01.

The agent writes the scenarios; code decides which passes a screen needs, packages the raw source and verified facts,
keeps a record ledger (the values each record holds NOW, updated after every update pass), orders and merges the passes,
and the coverage loop asks for the gaps only."""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import re
from typing import Awaitable, Callable

from service import chunk_analysis, coverage_loop, extended_steps, pass_planner, run_metrics, screen_spec, value_gen

logger = logging.getLogger(__name__)

STANDARD_MESSAGES = ["Details Saved Successfully With Code:", "Details Updated Successfully With Code:",
                     "Details have been deleted successfully"]


def _int(name: str, default: int) -> int:
    """A number setting with a default in code (no .env needed)."""
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def applies(spec: dict | None, is_mm: bool) -> tuple[bool, str]:
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
            out.append(f"- {m}")

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


def _calculated_block(facts: dict) -> str:
    """Calculated fields are only checked; dates are relative. Part of the stable prefix of every pass."""
    lines = []
    calc = facts.get("calculated") or []
    if calc:
        lines.append("CALCULATED / READ-ONLY FIELDS: the app fills these, the user never does. NEVER write an input step, a "
                     "data-pool value or an Examples cell for them and never clear them; they are only CHECKED with the step  "
                     "the \"<Field>\" field should show \"<Value>\".")
        lines += value_gen.describe_calculated(calc)
        lines.append("Value of that check: the exact number when the rule above gives it (work it out from the relative dates of "
                     "the row), otherwise the text (not empty).")
    if facts.get("date_format") or any(f.get("kind") == "date" for f in facts.get("fields") or []):
        fmt = facts.get("date_format")
        lines.append("DATES: write every date value as today, today+N or today-N (N = days), never as a literal date, so the file "
                     "never goes stale; the date step types it in the screen's own date format"
                     + (f" ({fmt})" if fmt else "") + ".")
    return "\n".join(lines)


def build_prefix(project_context: dict, project_analysis: dict, spec: dict, facts: dict,
                 business_context: dict | None, user_request: str) -> str:
    """STABLE PREFIX: identical for every pass of one screen (so prompt caching applies)."""
    pool = project_analysis.get("value_pool") or {}
    try:
        spec_text = screen_spec.rules_for("TEST_CASES") + "\n\n" + value_gen.render_for_test_cases(spec, pool, [])
    except Exception:  # noqa: BLE001
        logger.exception("feature passes: spec rendering failed - using the compact spec")
        spec_text = json.dumps(screen_spec.compact(spec), indent=2)
    calc_text = _calculated_block(facts)
    nav = ""
    page = ((project_analysis.get("modules") or [{}])[0].get("pages") or [{}])[0]
    path = [p for p in page.get("navigation_path") or [] if isinstance(p, str)]
    if path:
        nav = "NAVIGATION: the Background step is  I navigate through the following path " + " -> ".join(f'"{p}"' for p in path)
    return "\n\n".join([
        f"User request: {user_request}",
        "SCREEN SOURCE (raw files):\n" + _source_block(project_context, _int("PASS_SOURCE_CHAR_BUDGET", 120000)),
        spec_text,
        calc_text,
        _datacy_table(facts),
        _messages_block(facts),
        nav,
        "BUSINESS CONTEXT (real values):\n" + (json.dumps(business_context, indent=2) if business_context else "none provided"),
        "STEP VOCABULARY (use ONLY these sentences, exactly as written):\n" + extended_steps.step_vocabulary(),
        "DETECTED SCREEN BEHAVIOUR (code-extracted facts with source snippets):\n"
        + (json.dumps(facts["behavior"], indent=2)[:9000] if facts["behavior"] else "none detected"),
    ]).strip()


def build_tail(p: pass_planner.Pass, facts: dict, ledger: list[dict], titles: list[str], first: bool,
               update_pool_text: str = "", offset: int = 0) -> str:
    lines = [f"=== THIS PASS: {p.id} | title prefix: \"{p.title_prefix}\" ===", f"THEME: {p.theme_text}"]
    if p.id == "business_rules":
        lines.append("BACKEND RULES (the message is its fixed text only; ExpectedResult is exactly that text, never with {braces}):\n"
                     + "\n".join(f"- {r.get('condition') or 'always'} -> \"{r.get('message')}\"" for r in facts["backend_rules"]))
    if p.id.startswith("update_"):
        lines.append(pass_planner.UPDATE_RULES)
        if update_pool_text:
            lines.append(update_pool_text)
        if ledger:
            lines.append(f"Take the records of the RECORD LEDGER in order, starting with record number {offset % len(ledger) + 1} "
                         f"({ledger[offset % len(ledger)]['key']}), and use the next record for each further row.")
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
                     f"AT LEAST {pass_planner.MIN_RECORDS} records for the later delete, open-existing and update checks (distinct "
                     "codes and distinct names, every editable field filled so each update row can open a different record), titled "
                     f"\"{p.title_prefix} - Records for the later checks\".")
    else:
        lines.append("Start directly with Scenario Outline: (no Feature:, no Background:).")
    lines.append("Output only Gherkin.")
    return "\n\n".join(lines)


def _titles(text: str) -> list[str]:
    return [m.group(1).strip() for m in re.finditer(r"^\s*Scenario\s+(?:Outline|Template)\s*:\s*(.*)$", text or "", re.M | re.I)]


def _delete_ledger(original: list[dict], after_update: list[dict]) -> list[dict]:
    """Records the delete pass may use: those saved for deletion, and records the update passes did not touch."""
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
def update_row_counts(text: str) -> dict[str, int]:
    """{update kind: number of Examples rows} of the update scenarios in a feature text."""
    out: dict[str, int] = {}
    for u in pass_planner.update_rows(text):
        out[u["kind"]] = out.get(u["kind"], 0) + 1
    return out


async def run(agent, project_context: dict, project_analysis: dict, spec: dict, business_context: dict | None,
              user_request: str, log: Callable[[str], Awaitable[None]], metrics: run_metrics.ScreenRun | None = None):
    """Returns (feature_text, info). Raises on total failure so the caller can fall back to the old path."""
    facts = pass_planner.build_facts(spec, project_analysis)
    passes = pass_planner.plan_passes(facts)
    await log(f"planned {len(passes)} passes: " + ", ".join(p.id + (f"[{p.extra['label']}]" if p.extra.get("label") else "") for p in passes))
    for sk in facts.get("backend_rules_skipped") or []:
        await log(f"backend rule not UI-triggerable - no scenario: \"{sk['message']}\" ({sk['why']})")
    if facts.get("calculated"):
        await log("calculated / read-only fields (never filled, only checked): "
                  + ", ".join(f["name"] + (f" <- {'+'.join(f['computed_from'])}" if f.get("computed_from") else "") for f in facts["calculated"]))
    prefix = build_prefix(project_context, project_analysis, spec, facts, business_context, user_request)
    update_pool_text = ""
    try:
        update_pool_text = value_gen.render_update_pool(value_gen.build_update_pool(spec, business_context))
    except Exception:  # noqa: BLE001
        logger.exception("update data pool failed - the update passes run without it")
    sem = asyncio.Semaphore(_int("FEATURE_PASS_MAX_PARALLEL", 3))
    done: dict[int, str] = {}
    counter = {"n": 0}
    total = len(passes)
    key_field = next((f["name"] for f in facts["fields"] if f.get("key_field") and f["kind"] == "picklist"), "")

    async def one(idx: int, p: pass_planner.Pass, ledger, titles, first=False, offset: int = 0):
        async with sem:
            tail = build_tail(p, facts, ledger, titles, first, update_pool_text, offset)
            try:
                text = await agent.generate_pass(prefix, tail)
            except Exception as e:  # noqa: BLE001
                logger.exception("pass %s failed", p.id)
                await log(f"pass '{p.id}' failed ({e}) - continuing with the other passes")
                text = ""
            counter["n"] += 1
            c = run_metrics.counts(text)
            label = f"[{p.extra['label']}]" if p.extra.get("label") else ""
            logger.info("pass %d/%d '%s%s': %d outline(s), %d row(s)", counter["n"], total, p.id, label, c["outlines"], c["rows"])
            await log(f"pass {counter['n']}/{total} '{p.id}{label}': {c['outlines']} outline(s), {c['rows']} row(s)")
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
    ledger = pass_planner.build_ledger("\n".join(create_texts), key_field)
    # enough records for the later passes: every update row opens a DIFFERENT record
    if passes and any(p.order_group == "update" for p in passes) and len(ledger) < pass_planner.MIN_RECORDS:
        try:
            extra_text, extra_recs = pass_planner.topup_records(facts, ledger, pass_planner.MIN_RECORDS - len(ledger))
            if extra_text:
                passes.append(pass_planner.Pass("records_topup", "Form core", "records for the later checks", mutates=True))
                idx = len(passes) - 1
                ids.append(idx)
                done[idx] = extra_text
                create_texts.append(extra_text)
                ledger = pass_planner.build_ledger("\n".join(create_texts), key_field)
                await log(f"form_core saved {len(ledger) - len(extra_recs)} record(s); added {len(extra_recs)} more by code (target {pass_planner.MIN_RECORDS})")
        except Exception:  # noqa: BLE001
            logger.exception("record top-up failed - the update passes use the records there are")
    titles = [t for tx in create_texts for t in _titles(tx)]
    # tab passes (create group): also parallel
    tab_idx = [i for i in ids if passes[i].id == "tabs"]
    await asyncio.gather(*[one(i, passes[i], [], list(titles)) for i in tab_idx])
    # read-only group
    await asyncio.gather(*[one(i, passes[i], ledger, list(titles)) for i in group("read_only")])
    titles += [t for i in group("read_only") for t in _titles(done.get(i, ""))]
    # update passes, one after the other: each one sees what the earlier ones left in every record
    ledger_now = copy.deepcopy(ledger)
    upd_texts: list[str] = []
    for n, i in enumerate(group("update")):
        t = await one(i, passes[i], copy.deepcopy(ledger_now), list(titles), offset=n * 2)
        try:
            pass_planner.apply_update_text(ledger_now, t)
        except Exception:  # noqa: BLE001
            logger.exception("ledger update after pass %s failed - the next pass sees the older values", passes[i].id)
        titles += _titles(t)
        upd_texts.append(t)
    if upd_texts:
        counts = update_row_counts("\n".join(upd_texts))
        await log("update rows by kind: " + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none"))
        logger.info("update rows by kind: %s", counts)
    for i in group("delete"):
        await one(i, passes[i], _delete_ledger(ledger, ledger_now), list(titles))

    ordered = pass_planner.order_passes([(passes[i], done.get(i, "")) for i in ids])
    merged = merge_passes(ordered)
    info = {"facts": facts, "passes": [p.id for p in passes], "ledger": ledger, "prefix": prefix,
            "update_pool_text": update_pool_text}
    if metrics:
        metrics.stage("passes_merged", merged)
    return merged, info


async def gap_fill(agent, prefix: str, facts: dict, ledger: list[dict], gap_lines: list[str], current: str,
                   update_pool_text: str = "") -> str:
    parts = [
        "=== THIS PASS: coverage gaps | title prefix: \"Coverage\" ===",
        "THEME: write scenarios ONLY for these gaps, one Scenario Outline per gap group, nothing else:\n"
        + "\n".join(f"- {g}" for g in gap_lines),
    ]
    if any(g.upper().startswith("UPDATE") for g in gap_lines):
        parts.append(pass_planner.UPDATE_RULES + "\nFor an update gap start the title with the title prefix named in the gap "
                     "(for example \"Update single field - text\").")
        if update_pool_text:
            parts.append(update_pool_text)
    parts += [
        pass_planner.render_ledger(ledger),
        "TITLES ALREADY WRITTEN (do not repeat):\n" + "\n".join(f"- {t}" for t in _titles(current)[:150]),
        "Start directly with Scenario Outline: (no Feature:, no Background:). Output only Gherkin.",
    ]
    raw = await agent.generate_pass(prefix, "\n\n".join(parts))
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