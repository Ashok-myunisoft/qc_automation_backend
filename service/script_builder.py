from __future__ import annotations

import json
import re

SCRIPT_MARKER = (
    "// QC-AUTO-GENERATED SCRIPT. Do not edit by hand: it is rebuilt from the live data-cy "
    "scan every time tests are generated."
)
_CONTROLS_BEGIN = "// CONTROLS-BEGIN"
_CONTROLS_END = "// CONTROLS-END"
_ACTIONS_BEGIN = "// ACTIONS-BEGIN"
_ACTIONS_END = "// ACTIONS-END"

# ---------------------------------------------------------------------------
# control kinds
# ---------------------------------------------------------------------------
# suffix on the real data-cy -> kind. Longest first so "-PickListDrop" is not
# read as "-PickList" and "-UnCheckBox" is not read as "-CheckBox".
_SUFFIX_KINDS: tuple[tuple[str, str], ...] = (
    ("-PickListDrop", "picklist"),
    ("-DatePickerToggle", "date"),
    ("-PickList", "picklist"),
    ("-DatePicker", "date"),
    ("-UnCheckBox", "checkbox"),
    ("-CheckBox", "checkbox"),
    ("-ComboBox", "combobox"),
    ("-RadioBox", "radio"),
    ("-Textarea", "textarea"),
    ("-Input", "input"),
)
_ANY_SUFFIX_RE = re.compile(
    "(?:" + "|".join(re.escape(s) for s, _ in _SUFFIX_KINDS) + ")$", re.IGNORECASE
)

# analysis component hints, used only when the data-cy has no known suffix
_CATEGORY_KINDS: tuple[tuple[str, str], ...] = (
    ("picklist", "picklist"),
    ("combo", "combobox"),
    ("date", "date"),
    ("radio", "radio"),
    ("checkbox", "checkbox"),
    ("textarea", "textarea"),
)

# kind -> (verb, feature sentence). Picklist/input are the QC team's own steps.
_KIND_SENTENCE: dict[str, tuple[str, str]] = {
    "picklist": ("select", 'I select "{v}" in the "{f}" picklist field'),
    "input":    ("enter",  'I enter "{v}" in the "{f}" input field'),
    "combobox": ("select", 'I select "{v}" in the "{f}" combobox field'),
    "date":     ("enter",  'I enter "{v}" in the "{f}" date field'),
    "radio":    ("select", 'I select "{v}" in the "{f}" radio field'),
    "checkbox": ("set",    'I set "{v}" in the "{f}" checkbox field'),
    "textarea": ("enter",  'I enter "{v}" in the "{f}" textarea field'),
}


def _norm(text) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def _split_data_cy(data_cy: str, field: dict | None = None) -> tuple[str, str]:
    """('CostAnalysisCostingType-ComboBox') -> ('CostAnalysisCostingType', 'combobox')."""
    low = data_cy.lower()
    for suffix, kind in _SUFFIX_KINDS:
        if low.endswith(suffix.lower()):
            return data_cy[: -len(suffix)], kind
    hint = ""
    if field:
        hint = f"{field.get('component_category') or ''} {field.get('component_kind') or ''}".lower()
    for key, kind in _CATEGORY_KINDS:
        if key in hint:
            return data_cy, kind
    return data_cy, "input"


# ---------------------------------------------------------------------------
# reading the analysis
# ---------------------------------------------------------------------------
def _verified_data_cy(obj: dict) -> str | None:
    loc = obj.get("automation_locator")
    if not isinstance(loc, dict):
        return None
    strategy = str(loc.get("strategy") or "").lower().replace("-", "_")
    value = loc.get("value")
    if loc.get("verified") is True and strategy == "data_cy" and value and value != "Unknown":
        return str(value)
    return None


def _pages(project_analysis: dict):
    for module in (project_analysis or {}).get("modules") or []:
        for page in (module.get("pages") or []) if isinstance(module, dict) else []:
            if isinstance(page, dict):
                yield page


def controls_from_analysis(project_analysis: dict) -> dict[str, dict]:
    """{base name: {"dataCy": exact verified value, "kind": ...}} for every
    form / repeating-row field whose data-cy is verified."""
    controls: dict[str, dict] = {}
    for page in _pages(project_analysis):
        fields = []
        for form in page.get("forms") or []:
            if isinstance(form, dict):
                fields.extend(f for f in form.get("fields") or [] if isinstance(f, dict))
        for loop in page.get("loops") or []:
            if isinstance(loop, dict):
                fields.extend(f for f in loop.get("fields") or [] if isinstance(f, dict))
        for field in fields:
            data_cy = _verified_data_cy(field)
            if not data_cy:
                continue
            base, kind = _split_data_cy(data_cy, field)
            if base and base not in controls:
                controls[base] = {"dataCy": data_cy, "kind": kind}
    return controls


_DEFAULT_ACTIONS = {
    "addNew": "AddNewForm",
    "save": "SaveForm",
    "update": "UpdateForm",
    "edit": "EditForm",
}
_ACTION_NAMES = {
    "addnew": "addNew", "add": "addNew", "new": "addNew",
    "save": "save",
    "update": "update",
    "edit": "edit",
}


def actions_from_analysis(project_analysis: dict) -> dict[str, str]:
    """Product defaults, overridden by a VERIFIED data-cy from the analysis."""
    actions = dict(_DEFAULT_ACTIONS)
    for page in _pages(project_analysis):
        for action in page.get("business_actions") or []:
            if not isinstance(action, dict):
                continue
            key = _ACTION_NAMES.get(_norm(action.get("action_name")))
            data_cy = _verified_data_cy(action)
            if key and data_cy:
                actions[key] = data_cy
    return actions


# ---------------------------------------------------------------------------
# the script
# ---------------------------------------------------------------------------
# The QC team's template, character for character (same text as the
# #QC TEAM TEMPLATES block of prompts/script_generate_prompt.txt).
_TEMPLATE_BLOCK = r"""import { Given, When, Then } from "@badeball/cypress-cucumber-preprocessor";
const creds = Cypress.env('TestConnections').UILogin;

Then("I select {string} in the {string} picklist field", (value, field) => {
  if (!value || !String(value).trim()) {
    return; // nothing to select - the field stays empty
  }
  cy.get(`[data-cy="${field}-PickListDrop"]`).click({ force: true });
  cy.get("[data-cy='PicklistSearchBar']").should('be.visible').click().clear().type(value);
  cy.get('.ag-row', { timeout: 15000 }).should('exist');
  if (/^PLACEHOLDER_/i.test(String(value))) {
    // no real test data was supplied for this value: take the first row of the list
    cy.get('.ag-row-even > .ag-column-first', { timeout: 15000 }).first().click({ force: true });
  } else {
    // pick the row that really contains the requested value, never just "the first row"
    cy.contains('.ag-row', String(value).trim(), { timeout: 15000 })
      .find('.ag-column-first')
      .first()
      .click({ force: true });
  }
});

Then("I enter {string} in the {string} input field", (value, field) => {
  if (!value) {
    return;
  }
  cy.wait(1000);
  cy.get(`[data-cy="${field}-Input"]`)
    .focus()
    .click({ force: true })
    .clear()
    .type(value, { delay: 100 });
  cy.wait(3000);
});

Then("I should see a confirmation message {string}", (value) => {
  cy.contains(value, { timeout: 15000 }).should('be.visible');
  // close the message dialog only when it has a close icon (inline / toast messages have none)
  cy.get('body').then(($body) => {
    const $close = $body.find('.mat-mdc-dialog-component-host > .mat-icon');
    if ($close.length) {
      cy.wrap($close.first()).click();
    }
  });
});

Then("I click the Delete button", () => {
  cy.wait(5000);
  cy.get('[data-cy="DeleteForm"]').click();
  cy.get('div.alertbutton').click();
  cy.wait(5000);
});

beforeEach(() => {
  loginSession();
});

function loginSession() {
  cy.visit(Cypress.config('baseUrl'), {
    timeout: 120000,
    failOnStatusCode: false,
  });
  cy.get('body').then($body => {
    if ($body.find('[data-cy="Database-Input"]').length === 0) {
      cy.get('.login-btn', { timeout: 60000 }).click();
    }
  });
  cy.get('[data-cy="Database-Input"]', { timeout: 30000 })
    .should('be.visible')
    .type(creds.dbName);
  cy.get('.btn-submit').click();
  cy.get('[data-cy="Username-Input"]', { timeout: 90000 })
    .should('be.visible')
    .type(creds.userName);
  cy.get('[data-cy="Password-Input"]')
    .type(creds.password, { log: false });
  cy.get('.signin-button').click();
  cy.wait(5000);
}
"""

# Everything below the template. __CONTROLS__ / __ACTIONS__ are replaced with JSON.
_GENERATED_BLOCK = r"""
__MARKER__

// Exact data-cy of every field, read from the real page by the live scan.
__CONTROLS_BEGIN__
const CONTROLS = __CONTROLS__;
__CONTROLS_END__

// Action buttons (the Delete button is part of the QC template above).
__ACTIONS_BEGIN__
const ACTIONS = __ACTIONS__;
__ACTIONS_END__

// Finds a field's control by its verified data-cy. When a field is not in
// CONTROLS the QC naming convention (<field><suffix>) is used instead.
function controlOf(field, suffix) {
  const known = CONTROLS[field];
  return cy
    .get(`[data-cy="${known ? known.dataCy : field + suffix}"]`, { timeout: 30000 })
    .first()
    .should('exist')
    .scrollIntoView();
}

function clickAction(name) {
  cy.get(`[data-cy="${ACTIONS[name]}"]`).should('be.visible').click();
}

// ---- popup handling ----
// The ERP shows error / info popups after opening a module or a screen. Which
// popups appear (and how many) differs per screen, so none can be scripted in
// advance. This closes whatever is visible, repeatedly, until a quiet period
// passes with nothing on screen - so it does nothing on a screen with no popups
// and copes with a screen that shows many. Each popup's text is logged, never
// hidden: a popup on load usually means a failed API call.
function dismissPopups({ quietMs = 1500, max = 15 } = {}) {
  const CLOSERS = [
    '.mat-mdc-dialog-component-host > .mat-icon',   // dialog close icon
    'div.alertbutton',                              // OK / confirm button
  ];
  const next = (n) => {
    if (n >= max) return;
    cy.wait(quietMs);                               // popups arrive late: wait for a quiet moment
    cy.get('body').then(($body) => {
      for (const sel of CLOSERS) {
        const $el = $body.find(sel).filter(':visible');
        if ($el.length) {
          const msg = $body.find('.mat-mdc-dialog-component-host').first().text().replace(/\s+/g, ' ').trim().slice(0, 200);
          Cypress.log({ name: 'popup', message: msg || sel });
          cy.wrap($el.first()).click({ force: true });
          next(n + 1);                              // check again: another popup may have appeared
          return;
        }
      }
    });
  };
  next(0);
}

// ---- navigation ----
Then("I navigate through the following path {string} -> {string}", (module, screen) => {
  dismissPopups();                                  // popups left over from login
  cy.get('[data-cy="ModuleList"] > .mat-icon').should('be.visible').and('not.be.disabled').dblclick();
  cy.get(`[data-cy="${module}-Module"] > .card-image`).scrollIntoView().should('be.visible').click();
  dismissPopups();                                  // popups after opening the module
  cy.get(`[data-cy="${screen}-MenuData0"] > .menu-item`).scrollIntoView().should('be.visible').click();
  dismissPopups();                                  // popups after opening the screen
});

Then("I navigate through the following path {string} -> {string} -> {string}", (module, menu, screen) => {
  dismissPopups();                                  // popups left over from login
  cy.get('[data-cy="ModuleList"] > .mat-icon').should('be.visible').and('not.be.disabled').dblclick();
  cy.get(`[data-cy="${module}-Module"] > .card-image`).scrollIntoView().should('be.visible').click();
  dismissPopups();                                  // popups after opening the module
  cy.get(`[data-cy="${menu}-MenuFolder0"]`).scrollIntoView().should('be.visible').click({ force: true });
  dismissPopups();                                  // popups after opening the menu folder
  cy.get(`[data-cy="${screen}-MenuData1"] > .menu-item`).scrollIntoView().should('be.visible').click({ force: true });
  dismissPopups();                                  // popups after opening the screen
});

// ---- buttons ----
Then("I click the Add New button", () => {
  clickAction('addNew');
});

Then("I click the save button", () => {
  clickAction('save');
});

Then("I click the Update button", () => {
  clickAction('update');
});

Then("I click the Edit button", () => {
  clickAction('edit');
});

// ---- other field types (the picklist and input steps are in the QC template) ----
Then("I select {string} in the {string} combobox field", (value, field) => {
  if (!value) {
    return;
  }
  controlOf(field, '-ComboBox').click({ force: true });
  cy.contains('mat-option, .mat-mdc-option, [role="option"]', value, { timeout: 15000 }).click({ force: true });
});

Then("I enter {string} in the {string} date field", (value, field) => {
  if (!value) {
    return;
  }
  controlOf(field, '-DatePicker').then(($el) => {
    const $input = $el.is('input') ? $el : $el.find('input').first();
    if (/^today$/i.test(value.trim()) && $input.val()) {
      return; // the field already holds today's date by default
    }
    cy.wrap($input).click({ force: true }).clear({ force: true }).type(value, { force: true, delay: 50 });
    cy.wrap($input).blur({ force: true });
  });
});

Then("I select {string} in the {string} radio field", (value, field) => {
  if (!value) {
    return;
  }
  controlOf(field, '-RadioBox').then(($el) => {
    const $group = $el.closest('mat-radio-group, gb-radiobutton, .mat-radio-group, .mat-mdc-radio-group');
    const $scope = $group.length ? $group : $el.parent();
    const wanted = String(value).trim().toLowerCase();
    const $buttons = $scope.find('mat-radio-button, label');
    // 1) the option whose visible label is exactly the value, 2) a label that contains it,
    // 3) the option whose underlying radio value is the value (for codes such as 0 / 1)
    let $hit = $buttons.filter((i, e) => Cypress.$(e).text().trim().toLowerCase() === wanted);
    if (!$hit.length) {
      $hit = $buttons.filter((i, e) => Cypress.$(e).text().toLowerCase().includes(wanted));
    }
    if (!$hit.length) {
      const $byValue = $scope.find('input[type="radio"]').filter((i, e) => String(e.value).trim().toLowerCase() === wanted);
      $hit = $byValue.closest('mat-radio-button, label');
      if (!$hit.length) {
        $hit = $byValue;
      }
    }
    expect($hit.length, `a radio option "${value}" in ${field}`).to.be.greaterThan(0);
    cy.wrap($hit.first()).click({ force: true });
  });
});

Then("I set {string} in the {string} checkbox field", (value, field) => {
  if (!value) {
    return;
  }
  const wanted = /^(true|yes|checked|on|1)$/i.test(value.trim());
  controlOf(field, '-CheckBox').then(($el) => {
    const $box = $el.is('input') ? $el : $el.find('input[type="checkbox"]').first();
    if ($box.length === 0 || $box.prop('checked') !== wanted) {
      cy.wrap($el).click({ force: true });
    }
  });
});

Then("I enter {string} in the {string} textarea field", (value, field) => {
  if (!value) {
    return;
  }
  controlOf(field, '-Textarea').then(($el) => {
    const $area = $el.is('textarea') ? $el : $el.find('textarea').first();
    cy.wrap($area).click({ force: true }).clear({ force: true }).type(value, { force: true, delay: 20 });
  });
});
"""


def build_script(project_analysis: dict) -> str:
    """The complete step-definition file for one screen."""
    controls = controls_from_analysis(project_analysis)
    actions = actions_from_analysis(project_analysis)
    generated = (
        _GENERATED_BLOCK
        .replace("__MARKER__", SCRIPT_MARKER)
        .replace("__CONTROLS_BEGIN__", _CONTROLS_BEGIN)
        .replace("__CONTROLS_END__", _CONTROLS_END)
        .replace("__ACTIONS_BEGIN__", _ACTIONS_BEGIN)
        .replace("__ACTIONS_END__", _ACTIONS_END)
        .replace("__CONTROLS__", json.dumps(controls, indent=2, ensure_ascii=False))
        .replace("__ACTIONS__", json.dumps(actions, indent=2, ensure_ascii=False))
    )
    return _TEMPLATE_BLOCK.rstrip("\n") + "\n" + generated


def is_generated(script: str | None) -> bool:
    return bool(script) and SCRIPT_MARKER in script


def controls_from_script(script: str | None) -> dict[str, dict]:
    """Reads the CONTROLS table back out of a script built by build_script()."""
    if not is_generated(script):
        return {}
    m = re.search(
        re.escape(_CONTROLS_BEGIN) + r"\s*const CONTROLS = (.*?);\s*" + re.escape(_CONTROLS_END),
        script, re.DOTALL,
    )
    if not m:
        return {}
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# keeping the feature in step with the script
# ---------------------------------------------------------------------------
_FIELD_STEP_RE = re.compile(
    r'^(?P<head>\s*(?:(?:Given|When|Then|And|But|\*)\s+)?)'
    r'I (?:select|enter|set) "(?P<value>[^"]*)" in the "(?P<field>[^"]*)" '
    r'(?:picklist|input|combobox|date|radio|checkbox|textarea) field\s*$'
)
_BARE_STEP_RE = re.compile(
    r'^(?P<indent>\s*)(?P<text>I (?:select|enter|set|click|navigate|should see)\b.*\S)\s*$'
)
_KEYWORD_START_RE = re.compile(r"^\s*(?:Given|When|Then|And|But|\*)\s", re.IGNORECASE)
_BLOCK_START_RE = re.compile(r"^\s*(?:Scenario(?:\s+Outline|\s+Template)?|Example|Background)\s*:", re.IGNORECASE)
_EXAMPLES_START_RE = re.compile(r"^\s*(?:Examples|Scenarios)\s*:", re.IGNORECASE)


def normalize_feature(feature_text: str, controls: dict[str, dict] | None) -> str:
    """Makes the feature match the script, without changing what it tests:

    * a field step is rewritten to the sentence for the field's real control
      type (e.g. a combobox written as a picklist step), and the field name is
      replaced by its exact base data-cy name;
    * a step line that is missing its Given/When/Then/And keyword gets one
      (without it Gherkin reads the line as description text and the step
      never runs).
    """
    if not feature_text:
        return feature_text
    by_norm = {_norm(base): (base, info) for base, info in (controls or {}).items() if isinstance(info, dict)}

    out: list[str] = []
    steps_in_block = 0
    in_examples = False
    in_docstring = False
    for line in feature_text.splitlines():
        stripped = line.strip()
        if stripped.startswith('"""') or stripped.startswith("```"):
            in_docstring = not in_docstring
            out.append(line)
            continue
        if in_docstring:
            out.append(line)
            continue

        if _BLOCK_START_RE.match(line):
            steps_in_block, in_examples = 0, False
            out.append(line)
            continue
        if _EXAMPLES_START_RE.match(line):
            in_examples = True
            out.append(line)
            continue
        if in_examples or stripped.startswith("|") or stripped.startswith("#") or stripped.startswith("@"):
            out.append(line)
            continue

        # 1) a bare step line with no keyword
        if not _KEYWORD_START_RE.match(line):
            m = _BARE_STEP_RE.match(line)
            if m and (steps_in_block > 0 or _inside_block(out)):
                text = m.group("text")
                if text.startswith("I should see"):
                    kw = "Then"
                elif steps_in_block == 0:
                    kw = "Given" if text.startswith("I navigate") else "When"
                else:
                    kw = "And"
                line = f"{m.group('indent')}{kw} {text}"
                stripped = line.strip()

        if _KEYWORD_START_RE.match(line):
            steps_in_block += 1

        # 2) a field step for a control we know
        m = _FIELD_STEP_RE.match(line)
        if m:
            field = _ANY_SUFFIX_RE.sub("", m.group("field"))
            hit = by_norm.get(_norm(field))
            if hit:
                base, info = hit
                kind = info.get("kind")
                if kind in _KIND_SENTENCE:
                    sentence = _KIND_SENTENCE[kind][1].format(v=m.group("value"), f=base)
                    line = f"{m.group('head')}{sentence}"
        out.append(line)

    text = "\n".join(out)
    return text + "\n" if feature_text.endswith("\n") else text


def _inside_block(already_out: list[str]) -> bool:
    """True when a Scenario / Background line has been seen already."""
    return any(_BLOCK_START_RE.match(l) for l in already_out)