"""
Dynamic chunking for ProjectAnalysisAgent.

Problem: a screen with hundreds of fields across many tabs cannot be
reliably enumerated in one LLM call — the model either truncates with an
invalid comment placeholder (fixed separately in the prompt) or silently
stops early and returns a small-but-valid JSON as if it were complete
(confirmed on Item Master: only 5 of ~200+ real fields came back, twice,
even after the truncation fix).

This is NOT a prompt problem — no wording can make one LLM call decide to
call itself again for the next tab. It has to be a code-level decision:
scan the screen first (cheap, deterministic, no LLM), decide whether it's
"small" (single call, unchanged) or "large" (one call per tab, merged
after), and only pay the extra token/call cost on screens that actually
need it. A 5-field screen and a 200-field screen must NOT be treated the
same way — this module makes that decision per-screen, not globally.

Token efficiency: the expensive part of a large screen is its .html file,
not the shared library files (those stay constant regardless of tab
count). So each per-tab call resends the SAME shared library files/.ts
content, but only that one tab's HTML slice instead of the whole
multi-thousand-line file — that's where the real per-call savings come
from, and it also keeps each call's field count small enough to reliably
enumerate in full.
"""

import logging
import re

import logger_config

logger = logging.getLogger(__name__)

# Tune against real screens. A screen at or below this many detected
# interactive elements goes through the existing single-call path
# unchanged — most screens (Target Master ~10 fields, a plain 5-field
# form, etc.) never hit chunking at all.
LARGE_SCREEN_FIELD_THRESHOLD = 40

# Matches the same signal categories Step 0 of the analysis prompt is
# told to enumerate — kept in sync with that list intentionally, since
# this is only a cheap pre-count, not a replacement for the LLM's own
# enumeration.
_FIELD_SIGNAL_RE = re.compile(
    r"""formControlName\s*=|\[formControlName\]\s*=|\(ngModel\)|ngModel\s*=|
        <gb-[a-zA-Z-]+|<mat-select|<mat-checkbox|<mat-radio|<mat-radio-group|
        <input\b|<select\b|<textarea\b""",
    re.VERBOSE,
)

# Matches one <mat-tab ...> opening tag and captures its label attribute,
# plus everything up to the matching </mat-tab>. Assumes tabs are not
# nested inside one another (true for every screen inspected so far in
# this codebase) — a non-greedy DOTALL match is enough for that shape.
_TAB_RE = re.compile(
    r"""<mat-tab\b[^>]*\blabel\s*=\s*["']([^"']+)["'][^>]*>(.*?)</mat-tab>""",
    re.DOTALL,
)


def count_interactive_elements(html_content: str) -> int:
    """Cheap, deterministic count of real form-bound/interactive elements
    in a screen's template — used only to decide small-vs-large, never
    treated as the authoritative field count (that's the LLM's job,
    per Step 0 of the analysis prompt)."""
    if not html_content:
        return 0
    return len(_FIELD_SIGNAL_RE.findall(html_content))


def extract_tab_sections(html_content: str) -> tuple[str, list[tuple[str, str]]]:
    """Splits a screen's HTML into:
      - shared_html: everything OUTSIDE any <mat-tab> block (header
        fields, the top-level form wrapper, business-action bar, etc.) —
        this appears once, in every chunk, since it's common context.
      - tabs: a list of (label, tab_html) for each <mat-tab> block found,
        in source order.

    If no <mat-tab> blocks exist at all, tabs is empty and shared_html is
    the full original content — callers should treat that as "not
    actually tabbed", per should_chunk()'s own combined field+tab check.
    """
    if not html_content:
        return "", []

    tabs: list[tuple[str, str]] = []
    for match in _TAB_RE.finditer(html_content):
        label, tab_body = match.group(1), match.group(2)
        tabs.append((label, tab_body))

    shared_html = _TAB_RE.sub("", html_content)
    return shared_html, tabs


def should_chunk(html_content: str, threshold: int = LARGE_SCREEN_FIELD_THRESHOLD) -> bool:
    """The single decision point: does this screen need per-tab chunked
    analysis, or does it go through the existing single-call path
    unchanged? Deliberately conservative — only chunk when the field
    count is clearly large AND there are real tab boundaries to split
    on. A screen with 100 fields crammed into one un-tabbed page still
    goes through the single-call path (nothing to split on) and instead
    relies on the "never truncate" prompt rule alone — chunking only
    helps when a natural split point (tabs) actually exists."""
    field_count = count_interactive_elements(html_content)
    _, tabs = extract_tab_sections(html_content)
    chunk_needed = field_count > threshold and len(tabs) >= 2
    logger.info(
        "should_chunk: field_count=%d, tab_count=%d, threshold=%d -> chunk=%s",
        field_count, len(tabs), threshold, chunk_needed,
    )
    return chunk_needed


def build_chunk_user_requests(base_user_request: str, tabs: list[tuple[str, str]]) -> list[str]:
    """One scoped user_request per tab, layered on top of the existing
    base request — the underlying analysis prompt file is NOT modified
    for this; the scoping instruction travels in the per-call
    user_request instead, same channel used for anything else that
    varies call-to-call (see ProjectAnalysisAgent.analyze's existing
    user_request parameter)."""
    requests = []
    for label, _ in tabs:
        requests.append(
            f"{base_user_request}\n\n"
            f"CHUNKED ANALYSIS NOTICE: this call covers ONLY the '{label}' "
            f"tab/section of this screen. The supplied source_files contain "
            f"just this tab's HTML plus the screen's shared header/context "
            f"and TypeScript. Populate forms[].fields[] with only the "
            f"fields that are actually present in THIS tab's HTML — do not "
            f"invent fields from other tabs, and do not treat the smaller "
            f"scope as reason to summarize or omit anything that IS present "
            f"here. Still return the complete JSON schema shape; leave "
            f"screen-level fields (business_actions, functional_behavior, "
            f"dialogs, etc.) populated from what the shared/TypeScript "
            f"content shows, same as a normal single-pass analysis would."
        )
    return requests


def build_chunk_source_files(
    base_source_files: list[dict],
    primary_html_path: str,
    shared_html: str,
    tab_label: str,
    tab_html: str,
) -> list[dict]:
    """Rebuilds the source_files list for one chunk call: every
    non-primary-HTML file (shared library .ts/.html, the primary
    screen's own .ts) is kept AS-IS — those don't grow with tab count
    and are needed in full for cross-field/business-logic context every
    time. Only the primary screen's own .html entry is replaced with a
    much smaller slice: shared_html (header/context outside any tab) +
    this one tab's html. This is the actual token saving — the full
    multi-thousand-line template is never resent in a single call."""
    chunk_files = []
    for f in base_source_files:
        if f.get("path") == primary_html_path:
            chunk_files.append({
                "path": f["path"],
                "language": f.get("language", ".html"),
                "content": (
                    f"<!-- CHUNKED VIEW: shared/header markup below, "
                    f"followed by only the '{tab_label}' tab's markup. "
                    f"Other tabs in the real file are intentionally "
                    f"omitted from this call. -->\n"
                    f"{shared_html}\n"
                    f"<!-- BEGIN TAB: {tab_label} -->\n"
                    f"{tab_html}\n"
                    f"<!-- END TAB: {tab_label} -->"
                ),
            })
        else:
            chunk_files.append(f)
    return chunk_files


def _dedupe_by_key(items: list[dict], key: str) -> list[dict]:
    seen = set()
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        k = item.get(key)
        if k and k in seen:
            continue
        if k:
            seen.add(k)
        out.append(item)
    return out


def merge_project_analyses(analyses: list[dict]) -> dict:
    """Merges N per-tab project_analysis dicts (same shape as a normal
    single-pass analysis) back into ONE, so everything downstream
    (TestCaseAgent, ScriptGenerateAgent, _build_locator_map) sees exactly
    the same shape it always has — chunking is invisible past this point.

    Strategy: take the first chunk's analysis as the base for
    project-level fields only (project_summary, frameworks,
    authentication_method, database, navigation_path, page_name, etc. —
    these should be consistent across chunks since they come from shared
    context, not the tab-specific slice). Everything that is
    STRUCTURALLY PER-TAB — forms.fields, screen_sections,
    business_actions, dialogs, tables, loops, other_components — is
    concatenated across every chunk and then deduped, the same way
    forms.fields already was.

    CONFIRMED REAL BUG this fixes: the previous version only merged
    forms.fields[] this way. screen_sections / business_actions /
    dialogs / tables / loops / other_components were left as whatever
    the single base chunk (chunk 0, or the first chunk that parsed) had
    for ITS OWN tab only — every other chunk's version of those fields
    was silently discarded. On a 14-tab screen this meant the merged
    output could report business_actions: [] and only one tab's worth
    of screen_sections even though most/all business actions
    (Save/Delete/AddNew live in the shared header, so they legitimately
    appear in every chunk) and all 14 tabs' sections were actually
    present across the full set of chunk responses. Since
    TestCaseAgent's scenario-depth rules are built around walking
    screen_sections/business_actions/dialogs/loops, this silently
    starved it of the structural scaffolding it needs, producing
    generic/shallow scenarios despite having a full, correctly merged
    field list to work with.

    Dedup keys, matching each object's own natural identity field:
      - forms.fields          -> control_name (unchanged from before)
      - business_actions      -> action_name (Save/Delete/AddNew are
                                  genuinely shared across every chunk,
                                  since they live in the header/action
                                  bar, not per-tab markup)
      - tables                -> grid_name
      - loops                 -> loop_source
      - screen_sections       -> NOT deduped by a single key. Each tab
                                  is a legitimately distinct section even
                                  if two tabs happen to share a label, so
                                  every chunk's section(s) are kept as-is,
                                  concatenated in chunk order.
      - dialogs                -> NOT deduped either — a screen can have
                                  more than one distinct dialog (Delete
                                  confirm, Approval, etc.) and there's no
                                  single reliable natural key across all
                                  dialog_types; concatenating is safe
                                  since exact duplicate dialogs (e.g. the
                                  same shared Delete-confirm reported by
                                  every chunk) don't cause incorrect
                                  scenarios downstream the way duplicate
                                  business_actions could, and dropping a
                                  real dialog silently is worse than one
                                  harmless repeat.
      - other_components      -> NOT deduped — same reasoning as dialogs;
                                  no single reliable natural key, and
                                  concatenating is the safe default.
    """
    if not analyses:
        return {}
    if len(analyses) == 1:
        return analyses[0]

    base = analyses[0]
    if "error" in base:
        # If even the first chunk failed to parse, there's nothing sane
        # to merge onto — surface the first real error we find instead.
        for a in analyses:
            if "error" not in a:
                base = a
                break

    merged = dict(base)
    merged_modules = []

    # Assumes one module/one page per screen, matching every real
    # analysis output seen so far in this pipeline. If a screen ever
    # legitimately returns multiple modules/pages, only the first of
    # each is merged across chunks here — safe default, since chunking
    # is scoped to ONE screen's tabs, not multiple screens.
    base_modules = base.get("modules") or []
    if not base_modules:
        return merged

    base_module = base_modules[0]
    base_pages = base_module.get("pages") or []
    if not base_pages:
        return merged
    base_page = base_pages[0]

    all_fields: list[dict] = []
    all_screen_sections: list[dict] = []
    all_business_actions: list[dict] = []
    all_dialogs: list[dict] = []
    all_tables: list[dict] = []
    all_loops: list[dict] = []
    all_other_components: list[dict] = []
    all_unresolved: list = list(merged.get("unresolved") or [])
    all_folder_structure: list = list(merged.get("folder_structure") or [])

    for analysis in analyses:
        if "error" in analysis:
            all_unresolved.append(
                f"One chunked analysis call failed to parse — its fields could not be merged: {analysis.get('response', '')[:200]}"
            )
            continue
        modules = analysis.get("modules") or []
        if not modules:
            continue
        pages = modules[0].get("pages") or []
        if not pages:
            continue
        page = pages[0]

        for form in page.get("forms") or []:
            all_fields.extend(form.get("fields") or [])

        all_screen_sections.extend(page.get("screen_sections") or [])
        all_business_actions.extend(page.get("business_actions") or [])
        all_dialogs.extend(page.get("dialogs") or [])
        all_tables.extend(page.get("tables") or [])
        all_loops.extend(page.get("loops") or [])
        all_other_components.extend(page.get("other_components") or [])

        all_unresolved.extend(analysis.get("unresolved") or [])
        all_folder_structure.extend(analysis.get("folder_structure") or [])

    deduped_fields = _dedupe_by_key(all_fields, "control_name")
    deduped_business_actions = _dedupe_by_key(all_business_actions, "action_name")
    deduped_tables = _dedupe_by_key(all_tables, "grid_name")
    deduped_loops = _dedupe_by_key(all_loops, "loop_source")

    merged_page = dict(base_page)
    merged_page["forms"] = [{
        "form_name": (base_page.get("forms") or [{}])[0].get("form_name", "") if base_page.get("forms") else "",
        "fields": deduped_fields,
    }]
    merged_page["screen_sections"] = all_screen_sections
    merged_page["business_actions"] = deduped_business_actions
    merged_page["dialogs"] = all_dialogs
    merged_page["tables"] = deduped_tables
    merged_page["loops"] = deduped_loops
    merged_page["other_components"] = all_other_components

    merged_module = dict(base_module)
    merged_module["pages"] = [merged_page]

    merged["modules"] = [merged_module]
    merged["unresolved"] = list(dict.fromkeys(all_unresolved))  # de-dupe, preserve order
    merged["folder_structure"] = list(dict.fromkeys(all_folder_structure))

    logger.info(
        "merge_project_analyses: merged %d chunk(s) -> %d field(s), "
        "%d screen_section(s), %d business_action(s), %d dialog(s), "
        "%d table(s), %d loop(s), %d other_component(s) after dedupe",
        len(analyses), len(deduped_fields), len(all_screen_sections),
        len(deduped_business_actions), len(all_dialogs), len(deduped_tables),
        len(deduped_loops), len(all_other_components),
    )

    return merged


# ---------------------------------------------------------------------------
# TestCaseAgent chunking
#
# Same underlying problem as the analysis stage, one step later in the
# pipeline: a tabbed screen's merged project_analysis (after the fix above)
# now correctly carries every tab's fields/screen_sections/business_actions,
# but asking TestCaseAgent to turn ALL of that into a single Gherkin feature
# file in one call hits the same reliability ceiling ProjectAnalysisAgent
# did — confirmed on Item Master, where only a handful of the 14 tabs'
# worth of scenarios came back despite the input JSON genuinely containing
# all 14 screen_sections. No prompt wording fixes this; it has to be a
# code-level split, exactly like should_chunk()/merge_project_analyses()
# above. test_case_prompt.txt's own Case A / Case B split (screen_sections
# empty vs non-empty) is left completely alone — chunking only changes HOW
# a Case B screen's Rule blocks get generated (one call each instead of
# all at once), never the prompt's own instructions for what should be in
# them.
# ---------------------------------------------------------------------------

import copy


def should_chunk_test_cases(project_analysis: dict) -> bool:
    """The single decision point for the test-case stage: does this
    screen's project_analysis have REAL screen_sections (a Case B /
    tabbed screen per test_case_prompt.txt), or not (Case A / direct
    screen, left completely unchunked).

    'Real' means activation_required=True — a genuine tab, stepper, or
    accordion that hides/shows content on click and requires explicit
    navigation in Cypress to reach. Logical groupings that are always
    visible simultaneously (e.g. a header + grid on the same screen)
    may be reported as screen_sections by ProjectAnalysisAgent but do
    NOT qualify for chunking — confirmed on Role-Menu, where the agent
    over-reported 2 sections for a flat screen with no real tabs at all,
    causing unnecessary chunking and Rule: blocks on a direct screen.

    Using activation_required as the filter is the same
    'move the judgment call from LLM to deterministic Python' pattern
    already proven for navigation (_derive_navigation_hint) and
    analysis chunking (should_chunk)."""
    modules = project_analysis.get("modules") or []
    if not modules:
        return False
    pages = modules[0].get("pages") or []
    if not pages:
        return False
    sections = pages[0].get("screen_sections") or []
    real_tabs = [s for s in sections if s.get("activation_required") is True]
    logger.info(
        "should_chunk_test_cases: total_sections=%d real_tabs=%d -> chunk=%s",
        len(sections), len(real_tabs), len(real_tabs) > 0,
    )
    return len(real_tabs) > 0


def _claimed_control_names(page: dict) -> set[str]:
    """Every control_name claimed by ANY screen_section's
    contains_control_names — used to split fields into shared/header
    fields (belong to no tab) vs section-scoped fields (belong to
    exactly the tab that lists them)."""
    claimed: set[str] = set()
    for section in page.get("screen_sections") or []:
        claimed.update(section.get("contains_control_names") or [])
    return claimed


def build_shared_test_case_analysis(project_analysis: dict) -> dict:
    """Narrows project_analysis to just the screen-level content: fields
    NOT claimed by any screen_section (the shared header fields, e.g.
    Item Code/Item Name sitting above the tab strip), plus
    business_actions/dialogs/tables/loops/functional_behavior exactly as
    merged — these are screen-wide, not tab-specific, so they belong in
    ONE call, not repeated in every per-tab call. screen_sections is
    zeroed out here so this call's own TestCaseAgent invocation follows
    Case A (no Rule: blocks) even though the real screen is Case B."""
    analysis = copy.deepcopy(project_analysis)
    modules = analysis.get("modules") or []
    if not modules:
        return analysis
    pages = modules[0].get("pages") or []
    if not pages:
        return analysis
    page = pages[0]

    claimed = _claimed_control_names(page)
    for form in page.get("forms") or []:
        form["fields"] = [
            f for f in (form.get("fields") or [])
            if f.get("control_name") not in claimed
        ]
    page["screen_sections"] = []
    return analysis


def build_section_test_case_analysis(project_analysis: dict, section: dict) -> dict:
    """Narrows project_analysis to just ONE tab: only the fields that
    tab's contains_control_names lists, and only that one screen_sections
    entry. business_actions/dialogs/tables/loops are zeroed out here —
    those are screen-wide (per the merge stage, nothing ties them to a
    specific tab), so they belong in the shared call only; repeating them
    in every per-tab call would duplicate the same Save/Delete/AddNew
    workflow scenarios 14 times over."""
    analysis = copy.deepcopy(project_analysis)
    modules = analysis.get("modules") or []
    if not modules:
        return analysis
    pages = modules[0].get("pages") or []
    if not pages:
        return analysis
    page = pages[0]

    wanted = set(section.get("contains_control_names") or [])
    for form in page.get("forms") or []:
        form["fields"] = [
            f for f in (form.get("fields") or [])
            if f.get("control_name") in wanted
        ]
    page["screen_sections"] = [section]
    page["business_actions"] = []
    page["dialogs"] = []
    page["tables"] = []
    page["loops"] = []
    return analysis


def build_shared_test_case_request(base_user_request: str) -> str:
    """Scoping instruction for the one shared/header call — travels via
    user_request, same channel used for the analysis-stage chunk
    scoping, so test_case_prompt.txt itself needs no changes."""
    return (
        f"{base_user_request}\n\n"
        f"CHUNKED FEATURE-FILE NOTICE: this call covers ONLY the "
        f"screen-level scenarios — fields that are not part of any tab "
        f"(the shared header fields), plus every business_action, "
        f"dialog, table, and loop reported in this Project Analysis. "
        f"Output the full 'Feature:' header, the 'Background:' "
        f"(navigation step), and ordinary Scenario/Scenario Outline "
        f"blocks for that shared-level content ONLY. Do NOT output any "
        f"'Rule:' block — every tab's own scenarios are being generated "
        f"in separate calls and will be appended below what you return "
        f"here. If this screen genuinely has no shared/header-level "
        f"fields or actions outside its tabs, it is still correct to "
        f"return just the 'Feature:' and 'Background:' with no "
        f"scenarios beneath them — do not invent scenarios to fill "
        f"the gap."
    )


def build_section_test_case_request(base_user_request: str, section_label: str) -> str:
    """Scoping instruction for one tab's call — same channel as above."""
    return (
        f"{base_user_request}\n\n"
        f"CHUNKED FEATURE-FILE NOTICE: this call covers ONLY the "
        f"'{section_label}' tab/section of this screen. The supplied "
        f"Project Analysis has been narrowed to just this section's own "
        f"fields. Output ONLY ONE Gherkin 'Rule: {section_label}' block "
        f"containing that section's Scenario/Scenario Outline coverage, "
        f"exhausted per the #SCENARIO DEPTH rules. Do NOT output "
        f"'Feature:', 'Background:', or any other 'Rule:' block — those "
        f"are being generated separately and combined with this output "
        f"afterward. Start your response directly with the line "
        f"'Rule: {section_label}'."
    )


def strip_to_rule_block(raw_text: str, section_label: str) -> str:
    """Defensive cleanup for one section's TestCaseAgent chunk output.
    The chunk request explicitly asks for ONLY a Rule: block, but LLM
    instruction-following on this is not guaranteed — if the model
    ignores that and emits its own Feature:/Background: preamble anyway,
    strip everything up to the first Rule: line so it doesn't get
    duplicated into the final merged file. If no Rule: line is found at
    all (model dropped the label), wrap the whole response under one,
    labeled with the real section_label so tab-grouping still holds."""
    lines = raw_text.splitlines()
    rule_idx = next(
        (i for i, line in enumerate(lines) if line.strip().startswith("Rule:")),
        None,
    )
    if rule_idx is not None:
        return "\n".join(lines[rule_idx:]).strip()

    body = raw_text.strip()
    if not body:
        return ""
    indented = "\n".join(("  " + line if line.strip() else line) for line in body.splitlines())
    return f"Rule: {section_label}\n{indented}"


def merge_test_case_chunks(shared_text: str, rule_texts: list[str]) -> str:
    """Concatenates the shared/header feature text with every tab's
    already-cleaned Rule: block, in tab order, into one final .feature
    file — the same 'chunk narrowly, merge back into one shape' pattern
    as merge_project_analyses() above, so everything downstream
    (ScriptGenerateAgent) sees one ordinary feature file and has no idea
    chunking happened."""
    parts = [shared_text.strip()]
    parts.extend(t.strip() for t in rule_texts if t.strip())
    return "\n\n".join(p for p in parts if p) + "\n"