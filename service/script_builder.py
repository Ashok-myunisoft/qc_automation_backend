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
  expectMessages(value);
  // close the message box (close icon OR OK button) so the next step is never blocked by it;
  // inline / toast messages have no box, so nothing is clicked for them
  closeOpenDialogs();
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

// Product default data-cy of every action button (used when no verified one is in ACTIONS).
const DEFAULT_ACTIONS = { addNew: 'AddNewForm', save: 'SaveForm', update: 'UpdateForm', edit: 'EditForm', delete: 'DeleteForm' };

// Save and Update share one place on the form, so a screen shows only one of them
// depending on whether the record is new or already open: try the asked one first,
// then its sibling.
function actionCandidates(name) {
  const order = name === 'save' ? ['save', 'update'] : name === 'update' ? ['update', 'save'] : [name];
  const list = [];
  order.forEach((key) => {
    [ACTIONS[key], DEFAULT_ACTIONS[key]].forEach((v) => {
      if (v && !list.includes(v)) {
        list.push(v);
      }
    });
  });
  return list;
}

// Some screens are still being built and an action button has no data-cy yet. The verified
// data-cy is always tried first; only when none of the candidates is on screen, a button is
// looked for by a screen-specific class (btn-add-domain ...), by its title / aria-label, or
// by its visible text. Every use of a fallback is written to the Cypress log.
const ACTION_TEXT = {
  addNew: /^\s*\+?\s*add(\s+new)?\b/i,
  save: /^\s*save\s*$/i,
  update: /^\s*update\s*$/i,
  edit: /^\s*edit\s*$/i,
  delete: /^\s*delete\s*$/i,
};
const ACTION_CLASS = {
  addNew: '[class*="btn-add"], [class*="add-btn"], [class*="btn-new"], [class*="add-new"]',
  save: '[class*="savebutton"], [class*="btn-save"]',
  update: '[class*="updatebutton"], [class*="btn-update"]',
  edit: '[class*="editbutton"], [class*="btn-edit"]',
  delete: '[class*="deletebutton"], [class*="btn-delete"], [class*="delete-btn"]',
};
const CLICKABLE = 'button, [role="button"], a, span.formbuttons, div.btn, span.btn, [class*="btn"]';

function fallbackAction($body, name) {
  // Delete: when a form / dialog is open, only look inside it (a list row's delete icon must never be hit)
  const $modalRoot = $body.find('.modal, mat-dialog-container, .mat-mdc-dialog-container').filter(':visible');
  const $root = (name === 'delete' && $modalRoot.length) ? $modalRoot.first() : $body;
  const order = name === 'save' ? ['save', 'update'] : name === 'update' ? ['update', 'save'] : [name];
  for (const key of order) {
    if (!ACTION_TEXT[key]) {
      continue;
    }
    let $hit = $root.find(ACTION_CLASS[key]).filter(':visible');
    if ($hit.length) {
      return $hit.first();
    }
    $hit = $root.find('[title], [aria-label]').filter(':visible').filter((i, e) => ACTION_TEXT[key].test(String(e.getAttribute('title') || e.getAttribute('aria-label') || '').trim()));
    if ($hit.length) {
      return $hit.first();
    }
    $hit = $root.find(CLICKABLE).filter(':visible').filter((i, e) => ACTION_TEXT[key].test(Cypress.$(e).text()));
    if ($hit.length) {
      return $hit.first();
    }
  }
  return Cypress.$();
}

function clickAction(name) {
  closeOpenDialogs();                               // a box left open by an earlier step would block this click
  const candidates = actionCandidates(name);
  const visibleOf = ($body, dataCy) => $body.find(`[data-cy="${dataCy}"]`).filter(':visible');
  cy.get('body', { timeout: 30000 }).should(($body) => {
    const hit = candidates.find((v) => visibleOf($body, v).length > 0)
      || (fallbackAction($body, name).length > 0 ? 'fallback' : undefined);
    expect(hit, `a visible "${name}" button (${candidates.join(' / ')}, or one found by class / title / text)`).to.be.a('string');
  });
  cy.get('body').then(($body) => {
    const found = candidates.find((v) => visibleOf($body, v).length > 0);
    if (!found) {
      const $fallback = fallbackAction($body, name);
      if ($fallback.length) {
        Cypress.log({ name: 'fallback', message: `no data-cy found for "${name}" - clicked a button found by class / title / text` });
        cy.wrap($fallback).scrollIntoView().should('not.be.disabled').click();
        return;
      }
    }
    const dataCy = found || candidates[0];
    cy.get(`[data-cy="${dataCy}"]`)
      .filter(':visible')
      .first()
      .scrollIntoView()
      .should('not.be.disabled')
      .click();
  });
}

// Checks a message on screen. A message that is shown as one text is checked as before; a
// combined expectation such as "A* Should not be Empty, B* Should not be Empty" is also
// accepted when each part is on screen separately (the app lists them one per line, in its
// own order).
function expectMessages(value) {
  const squash = (t) => String(t || '').replace(/\s+/g, ' ').trim();
  const whole = squash(value);
  if (!whole) {
    return;                                         // no message expected: nothing to look for
  }
  const parts = whole.split(/\s*[,;]\s*/).map(squash).filter(Boolean);
  cy.get('body', { timeout: 15000 }).should(($body) => {
    const text = squash($body.text());
    const ok = text.includes(whole) || (parts.length > 1 && parts.every((p) => text.includes(p)));
    expect(ok, `message "${whole}" (or each of its parts) on screen`).to.equal(true);
  });
  cy.get('body').then(($body) => {
    const text = squash($body.text());
    const targets = text.includes(whole) ? [whole] : parts;
    targets.forEach((t) => cy.contains(t, { timeout: 15000 }).should('be.visible'));
  });
}

// Closes every message box that is open (close icon or OK button), one after the other,
// then checks that none is left. Used after a message was checked and before a button
// is clicked, so a "Details Saved ..." box can never block the next step.
function closeOpenDialogs({ settleMs = 600, max = 10 } = {}) {
  const CLOSERS = [
    '.mat-mdc-dialog-component-host > .mat-icon',   // dialog close icon
    'div.alertbutton',                              // OK / confirm button
  ];
  const next = (n) => {
    if (n >= max) return;
    cy.get('body').then(($body) => {
      for (const sel of CLOSERS) {
        const $el = $body.find(sel).filter(':visible');
        if ($el.length) {
          cy.wrap($el.first()).click({ force: true });
          cy.wait(settleMs);                        // let it close, and let a follow-up box appear
          next(n + 1);                              // another box may be waiting behind it
          return;
        }
      }
    });
  };
  next(0);
  cy.get('body', { timeout: 5000 }).should(($body) => {
    const stillOpen = CLOSERS.filter((sel) => $body.find(sel).filter(':visible').length > 0);
    expect(stillOpen, 'message boxes still open').to.have.length(0);
  });
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

// ---- checkbox helpers ----
// Lower-case names a checkbox can carry: <name>-CheckBox, <name>-UnCheckBox and its known data-cy.
function checkboxNames(field) {
  const known = CONTROLS[field];
  const raw = String(known ? known.dataCy : field);
  const base = raw.replace(/-(UnCheckBox|CheckBox)$/i, '');
  return [`${base}-CheckBox`, `${base}-UnCheckBox`, raw].map((n) => n.toLowerCase());
}

// Finds the checkbox under whichever of its names is on the page right now and reads its
// real state (the native input, else the checked class, else the name itself).
function readCheckbox($body, names) {
  const $hits = $body.find('[data-cy]').filter((i, e) => names.includes(String(e.getAttribute('data-cy')).toLowerCase()));
  if ($hits.length === 0) {
    return null;
  }
  const $el = $hits.first();
  const name = String($el.attr('data-cy')).toLowerCase();
  const $input = $el.is('input') ? $el : $el.find('input[type="checkbox"]').first();
  let checked;
  if ($input.length) {
    checked = !!$input.prop('checked');
  } else if ($el.hasClass('mat-mdc-checkbox-checked') || $el.hasClass('mat-checkbox-checked')) {
    checked = true;
  } else {
    checked = !name.endsWith('-uncheckbox') && name.endsWith('-checkbox');
  }
  return { $el, $input, name, checked };
}

// ---- other field types (the picklist and input steps are in the QC template) ----
Then("I select {string} in the {string} combobox field", (value, field) => {
  if (!value) {
    return;
  }
  controlOf(field, '-ComboBox').click({ force: true });
  cy.contains('mat-option, .mat-mdc-option, [role="option"]', value, { timeout: 15000 }).click({ force: true });
});

// Dates are written in the feature as today, today+N or today-N (so the file never goes stale) and typed in the
// date format the screen shows (read from the screen's code; else taken from the field's own value).
const DATE_FORMAT = __DATE_FORMAT__;

function formatDate(d, fmt) {
  const months = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
  const two = (n) => String(n).padStart(2, '0');
  return String(fmt).replace(/yyyy|YYYY|yy|YY|MMMM|MMM|MM|M|dd|DD|d|D/g, (t) => ({
    yyyy: d.getFullYear(), YYYY: d.getFullYear(), yy: two(d.getFullYear() % 100), YY: two(d.getFullYear() % 100),
    MMMM: months[d.getMonth()], MMM: months[d.getMonth()].slice(0, 3), MM: two(d.getMonth() + 1), M: d.getMonth() + 1,
    dd: two(d.getDate()), DD: two(d.getDate()), d: d.getDate(), D: d.getDate(),
  })[t]);
}

// a format taken from a value the field already shows: 27/Oct/2026 -> dd/MMM/yyyy, 27-10-2026 -> dd-MM-yyyy
function inferDateFormat(shown) {
  const m = String(shown || '').trim().match(/^(\d{1,2})([\/\-. ])([A-Za-z]{3,9}|\d{1,2})\2(\d{4})$/);
  if (!m) {
    return '';
  }
  const mid = /^\d+$/.test(m[3]) ? 'MM' : (m[3].length > 3 ? 'MMMM' : 'MMM');
  return 'dd' + m[2] + mid + m[2] + 'yyyy';
}

function relDate(value, shown) {
  const m = String(value).trim().match(/^today\s*(?:([+-])\s*(\d+))?$/i);
  if (!m) {
    return String(value);
  }
  const d = new Date();
  d.setDate(d.getDate() + (m[1] ? (m[1] === '-' ? -1 : 1) * parseInt(m[2], 10) : 0));
  return formatDate(d, DATE_FORMAT || inferDateFormat(shown) || 'dd/MM/yyyy');
}

Then("I enter {string} in the {string} date field", (value, field) => {
  if (!value) {
    return;
  }
  controlOf(field, '-DatePicker').then(($el) => {
    const $input = $el.is('input') ? $el : $el.find('input').first();
    if (/^today$/i.test(value.trim()) && $input.val()) {
      return; // the field already holds today's date by default
    }
    const typed = relDate(value, $input.val());
    cy.wrap($input).click({ force: true }).clear({ force: true }).type(typed, { force: true, delay: 50 });
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
  const names = checkboxNames(field);
  // the same checkbox carries <name>-UnCheckBox while unchecked and <name>-CheckBox while checked
  cy.get('body', { timeout: 30000 }).should(($body) => {
    expect(readCheckbox($body, names), `checkbox "${field}" (${names.join(' / ')})`).to.not.equal(null);
  });
  cy.get('body').then(($body) => {
    const now = readCheckbox($body, names);
    if (now.checked === wanted) {
      return;                                       // already in the wanted state
    }
    cy.wrap(now.$input.length ? now.$input : now.$el).scrollIntoView().click({ force: true });
  });
  cy.get('body', { timeout: 10000 }).should(($body) => {
    const now = readCheckbox($body, names);
    expect(now, `checkbox "${field}" still on the page`).to.not.equal(null);
    expect(now.checked, `checkbox "${field}" is ${wanted ? 'checked' : 'unchecked'}`).to.equal(wanted);
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

// ---- existing records: picklist screens and list screens ----
// Screens differ in how an existing record is reached. Some select it in a picklist
// ("<field>-PickListDrop"); others show a list with a search box and an edit / delete icon on
// each row (the search box and the icons usually have no data-cy). These steps work out which
// kind of screen is open and do the matching thing.
const squashText = (t) => String(t || '').replace(/\s+/g, ' ').trim();
const ROW_ACTIONS = 'button, [role="button"], a, mat-icon, .mat-icon, [class*="btn"], [class*="icon"], i, svg';

function findSearchBox($body) {
  return $body.find('input').filter(':visible').filter((i, e) => {
    const dc = String(e.getAttribute('data-cy') || '');
    if (dc === 'PicklistSearchBar') {
      return false;
    }
    if (String(e.getAttribute('type') || '').toLowerCase() === 'search') {
      return true;
    }
    return /search/i.test([dc, e.getAttribute('placeholder'), e.getAttribute('aria-label'), e.getAttribute('name'), e.id].join(' '));
  }).first();
}

function pickFromPicklist(field, value) {
  cy.get(`[data-cy="${field}-PickListDrop"]`, { timeout: 30000 }).click({ force: true });
  cy.get("[data-cy='PicklistSearchBar']").should('be.visible').click().clear().type(value);
  cy.get('.ag-row', { timeout: 15000 }).should('exist');
  if (/^PLACEHOLDER_/i.test(String(value))) {
    cy.get('.ag-row-even > .ag-column-first', { timeout: 15000 }).first().click({ force: true });
  } else {
    cy.contains('.ag-row', String(value).trim(), { timeout: 15000 })
      .find('.ag-column-first')
      .first()
      .click({ force: true });
  }
}

function withRecordMode(field, value, ifPicklist, ifList) {
  const picklistOn = ($b) => $b.find(`[data-cy="${field}-PickListDrop"]`).filter(':visible').length > 0;
  cy.get('body', { timeout: 20000 }).should(($b) => {
    expect(picklistOn($b) || findSearchBox($b).length > 0 || squashText($b.text()).includes(value),
      `the "${field}" picklist, a search box or the record "${value}" on the screen`).to.equal(true);
  });
  cy.get('body').then(($b) => {
    if (picklistOn($b)) {
      ifPicklist();
    } else {
      ifList();
    }
  });
}

function searchList(value) {
  cy.get('body').then(($b) => {
    const $box = findSearchBox($b);
    if ($box.length) {
      cy.wrap($box).click({ force: true }).clear({ force: true }).type(value, { force: true, delay: 50 });
      cy.wait(1500);
    }
  });
}

// the row of a list that holds the value: climb from the text until an element holds the
// row's action icons
function rowOf($text) {
  let $el = $text;
  for (let i = 0; i < 10 && $el.length; i += 1) {
    if ($el.find(ROW_ACTIONS).length >= 2) {
      return $el;
    }
    $el = $el.parent();
  }
  return $text.parent();
}

function rowButton($row, word) {
  const re = word === 'edit' ? /edit|pencil|modify/i : /delete|trash|remove|bin/i;
  const skip = /drag|handle|grip|status|badge|chip|toggle/i;
  const hayOf = (e) => [e.getAttribute('data-cy'), e.getAttribute('title'), e.getAttribute('aria-label'), e.className && e.className.baseVal === undefined ? e.className : '', Cypress.$(e).text()].join(' ');
  const $all = $row.find(ROW_ACTIONS).filter(':visible').filter((i, e) => !skip.test(hayOf(e)));
  const $named = $all.filter((i, e) => re.test(hayOf(e)));
  if ($named.length) {
    return $named.first();
  }
  const $tops = $all.filter((i, e) => Cypress.$(e).parents(ROW_ACTIONS).filter((j, p) => $row.has(p).length > 0).length === 0);
  return word === 'edit' ? $tops.first() : $tops.last();
}

function rowAction(value, word) {
  cy.contains(value, { timeout: 15000 }).then(($text) => {
    const $btn = rowButton(rowOf($text), word);
    expect($btn.length, `a ${word} icon on the row of "${value}"`).to.be.greaterThan(0);
    Cypress.log({ name: 'row', message: `${word} icon of the row "${value}"` });
    cy.wrap($btn).scrollIntoView().click({ force: true });
  });
  cy.wait(1500);
}

Then("I open the existing record {string} using the {string} field", (value, field) => {
  const v = String(value || '').trim();
  expect(v, 'a record to open').to.not.equal('');
  withRecordMode(field, v,
    () => pickFromPicklist(field, v),
    () => {
      searchList(v);
      rowAction(v, 'edit');
    });
});

Then("I request to delete the record {string} using the {string} field", (value, field) => {
  const v = String(value || '').trim();
  expect(v, 'a record to delete').to.not.equal('');
  withRecordMode(field, v,
    () => {
      pickFromPicklist(field, v);
      cy.wait(2000);
      clickAction('delete');
    },
    () => {
      searchList(v);
      rowAction(v, 'delete');
    });
});

// the delete prompt is only checked here - it is NOT closed, so the next step can answer it
Then("I should see the delete prompt {string}", (value) => {
  cy.contains(squashText(value), { timeout: 15000 }).should('be.visible');
});

const DIALOG_BUTTONS = 'div.alertbutton, [role="dialog"] button, [role="alertdialog"] button, mat-dialog-container button, .mat-mdc-dialog-container button, .cdk-overlay-pane button';
const DIALOG_BOX = '.cdk-overlay-container, [role="dialog"], [role="alertdialog"], mat-dialog-container, .mat-mdc-dialog-container, [class*="dialog"], [class*="modal"], [class*="confirm"], [class*="alert"], [class*="popup"]';

// The Yes / No (OK / Cancel) button of the open prompt. First the usual dialog buttons are
// searched; when the prompt draws its buttons some other way (a div, a span), any visible
// element inside a dialog-like box whose whole text is the label is used.
function dialogButton($body, re) {
  const $hit = $body.find(DIALOG_BUTTONS).filter(':visible').filter((i, e) => re.test(squashText(Cypress.$(e).text()))).first();
  if ($hit.length) {
    return $hit;
  }
  return $body.find('button, div, span, a, p, label').filter(':visible').filter((i, e) => {
    return e.children.length === 0 && re.test(squashText(e.textContent)) && Cypress.$(e).closest(DIALOG_BOX).length > 0;
  }).last();
}

// Tries the labels in order of preference ("Yes" before "OK" ...), so a prompt that carries
// both an affirmative and a negative button always gets the right one.
function dialogChoice($body, labels) {
  for (const re of labels) {
    const $b = dialogButton($body, re);
    if ($b.length) {
      return $b;
    }
  }
  return Cypress.$();
}

const CONFIRM_LABELS = [/^yes$/i, /^(ok|confirm|proceed|delete)$/i];
const CANCEL_LABELS = [/^no$/i, /^(cancel|close)$/i];

Then("I confirm the delete", () => {
  cy.get('body', { timeout: 15000 }).should(($b) => {
    const any = dialogChoice($b, CONFIRM_LABELS).length > 0 || $b.find('div.alertbutton').filter(':visible').length > 0;
    expect(any, 'a visible Yes / OK button on the delete prompt').to.equal(true);
  });
  cy.get('body').then(($b) => {
    let $btn = dialogChoice($b, CONFIRM_LABELS);
    if (!$btn.length) {
      $btn = $b.find('div.alertbutton').filter(':visible').first();
    }
    Cypress.log({ name: 'delete', message: `answered the delete prompt with "${squashText($btn.text())}"` });
    cy.wrap($btn).click({ force: true });
  });
  cy.wait(2000);
});

Then("I cancel the delete", () => {
  cy.get('body', { timeout: 15000 }).should(($b) => {
    const any = dialogChoice($b, CANCEL_LABELS).length > 0
      || $b.find('.mat-mdc-dialog-component-host > .mat-icon').filter(':visible').length > 0;
    expect(any, 'a visible No / Cancel button (or close icon) on the delete prompt').to.equal(true);
  });
  cy.get('body').then(($b) => {
    let $btn = dialogChoice($b, CANCEL_LABELS);
    if (!$btn.length) {
      $btn = $b.find('.mat-mdc-dialog-component-host > .mat-icon').filter(':visible').first();
    }
    Cypress.log({ name: 'delete', message: `answered the delete prompt with "${squashText($btn.text()) || 'close icon'}"` });
    cy.wrap($btn).click({ force: true });
  });
  cy.wait(1500);
});

function recordInPicklist(field, value, shouldExist) {
  cy.get(`[data-cy="${field}-PickListDrop"]`, { timeout: 30000 }).click({ force: true });
  cy.get("[data-cy='PicklistSearchBar']").should('be.visible').click().clear().type(value);
  cy.wait(2000);
  cy.get('body', { timeout: 15000 }).should(($b) => {
    const has = $b.find('.ag-row').filter((i, e) => squashText(e.textContent).includes(value)).length > 0;
    expect(has, `the record "${value}" ${shouldExist ? 'is' : 'is not'} in the "${field}" picklist`).to.equal(shouldExist);
  });
}

Then("I should still see the record {string} using the {string} field", (value, field) => {
  const v = String(value || '').trim();
  withRecordMode(field, v,
    () => recordInPicklist(field, v, true),
    () => {
      searchList(v);
      cy.contains(v, { timeout: 15000 }).should('be.visible');
    });
});

Then("I should no longer see the record {string} using the {string} field", (value, field) => {
  const v = String(value || '').trim();
  closeOpenDialogs();
  withRecordMode(field, v,
    () => recordInPicklist(field, v, false),
    () => {
      cy.wait(1500);                                // the list refreshes a moment after the delete
      searchList(v);
      cy.contains(v, { timeout: 15000 }).should('not.exist');
    });
});
"""


def _date_format(project_analysis: dict) -> str:
    """The date format the screen's own code uses ('' = taken from the field's value at run time)."""
    try:
        return str(((project_analysis or {}).get("screen_spec") or {}).get("date_format") or "")
    except Exception:  # noqa: BLE001
        return ""


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
        .replace("__DATE_FORMAT__", json.dumps(_date_format(project_analysis)))
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
_BUTTON_STEP_RE = re.compile(
    r'^(?P<head>\s*(?:Given|When|Then|And|But|\*)\s+)I\s+click\s+(?:on\s+)?(?:the\s+)?'
    r'(?P<name>save|add\s*new|update|edit|delete)(?:\s+button)?\s*$',
    re.IGNORECASE,
)
# the exact button sentences the script defines (letter case matters to Cucumber)
_BUTTON_SENTENCES = {
    "save": "I click the save button",
    "addnew": "I click the Add New button",
    "update": "I click the Update button",
    "edit": "I click the Edit button",
    "delete": "I click the Delete button",
}
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

        # 1b) a button step worded a little differently ("Save", "save button", "delete button")
        line = _canonical_button_line(line)

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


def _canonical_button_line(line: str) -> str:
    """Rewrites a save / add new / update / edit / delete button step to the exact
    sentence the script defines. Any other line is returned unchanged."""
    m = _BUTTON_STEP_RE.match(line)
    if not m:
        return line
    key = re.sub(r"\s+", "", m.group("name").lower())
    sentence = _BUTTON_SENTENCES.get(key)
    return f"{m.group('head')}{sentence}" if sentence else line


def normalize_button_steps(feature_text: str) -> str:
    """Button wording only (no field rewriting). Used when the code-built script
    could not be used, so the feature still matches the step definitions."""
    if not feature_text:
        return feature_text
    out = [_canonical_button_line(ln) for ln in feature_text.splitlines()]
    text = "\n".join(out)
    return text + "\n" if feature_text.endswith("\n") else text


_BUTTON_FALLBACK_DEFS = {
    "I click the save button": "SaveForm",
    "I click the Add New button": "AddNewForm",
    "I click the Update button": "UpdateForm",
    "I click the Edit button": "EditForm",
}


def ensure_button_steps(feature_text: str, script_text: str) -> str:
    """Safety net for a script that was NOT built by code (the AI fallback): when
    the feature uses a button step the script has no definition for, a plain,
    self-contained definition is appended. A script that already defines the
    sentence is returned unchanged."""
    if not feature_text or not script_text:
        return script_text
    extra: list[str] = []
    for sentence, data_cy in _BUTTON_FALLBACK_DEFS.items():
        used = re.search(r"^\s*(?:Given|When|Then|And|But|\*)\s+" + re.escape(sentence) + r"\s*$",
                         feature_text, re.MULTILINE)
        defined = re.search(r"(?:Given|When|Then)\(\s*[\"']" + re.escape(sentence) + r"[\"']", script_text)
        if used and not defined:
            extra.append(
                f'Then("{sentence}", () => {{\n'
                f"  cy.get('[data-cy=\"{data_cy}\"]').filter(':visible').first().scrollIntoView()"
                f".should('not.be.disabled').click();\n"
                f"}});\n"
            )
    used_delete = re.search(r"^\s*(?:Given|When|Then|And|But|\*)\s+I click the Delete button\s*$",
                            feature_text, re.MULTILINE)
    defined_delete = re.search(r"(?:Given|When|Then)\(\s*[\"']I click the Delete button[\"']", script_text)
    if used_delete and not defined_delete:
        extra.append(
            'Then("I click the Delete button", () => {\n'
            "  cy.wait(5000);\n"
            "  cy.get('[data-cy=\"DeleteForm\"]').click();\n"
            "  cy.get('div.alertbutton').click();\n"
            "  cy.wait(5000);\n"
            "});\n"
        )
    if not extra:
        return script_text
    return script_text.rstrip("\n") + "\n\n// ---- button steps added because the feature uses them ----\n" + "\n".join(extra)


def _inside_block(already_out: list[str]) -> bool:
    """True when a Scenario / Background line has been seen already."""
    return any(_BLOCK_START_RE.match(l) for l in already_out)