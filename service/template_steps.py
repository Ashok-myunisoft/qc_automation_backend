"""Fixed (code-written) definitions for the EXTENDED step vocabulary.

Why: asking the AI for ~25 step definitions in one block is the step that fails most often
(the guard rejects the whole block when one definition is missing or one rule is broken).
These definitions are plain, reviewed JavaScript. Only the screen facts (modal selector,
filter class, sortable columns, counters, empty state ...) are filled in from the facts that
service/screen_behavior.py read from the screen's own HTML/TS.

Use:  block, covered = build_block(behavior, missing_sentences)
      -> `block` is ONE JavaScript block (no imports, no beforeEach) to append to the code-built
         script, `covered` are the step sentences it defines.  Anything not covered is left to
         the agent (service/extended_steps.py guards that block exactly as before).

Only helpers of the QC template / code-built script are used:
  CONTROLS, ACTIONS, DEFAULT_ACTIONS, actionCandidates, clickAction, controlOf, readCheckbox,
  checkboxNames, findSearchBox, searchList, squashText
Every helper that is new here has the prefix `tpl`, so nothing can clash with the base script.
"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path

from service import step_checker

INSERT_NOTE = "// ---- fixed extended step definitions (code-written) ----"


# --------------------------------------------------------------------------- facts -> TPL config
def _norm(t) -> str:
    return re.sub(r"[^a-z0-9]", "", str(t or "").lower())


def _first_class(c) -> str:
    c = str(c or "").strip()
    return c.split()[0] if c else ""


def load_aliases() -> dict:
    """config/option_aliases.json: {"<Field>": {"<option in the form>": "<same option as the list / filter shows it>"}}.
    The list and the filter may name an option differently from the form (Production in the form, Manufacturing in the list)."""
    path = Path(__file__).resolve().parent.parent / "config" / "option_aliases.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return {k: {str(a): str(b) for a, b in v.items()} for k, v in data.items()
            if isinstance(v, dict) and not str(k).startswith("_")}


def _clean_label(t) -> str:
    """'Code &#9650; &#9660;' (header with sort arrows) -> 'Code'."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w ]", " ", html.unescape(str(t or "")))).strip()


def build_config(behavior: dict | None, spec: dict | None = None, aliases: dict | None = None) -> dict:
    """The small JSON object the JavaScript reads. Missing facts stay empty and the matching
    definitions then fall back to looking controls up by title / text."""
    b = behavior or {}
    aliases = aliases if aliases is not None else load_aliases()
    roles = {}
    for k, v in (b.get("roles") or {}).items():
        if isinstance(v, dict):
            roles[k] = {"title": v.get("title", ""), "class": _first_class(v.get("class", ""))}
    containers = b.get("containers") or {}
    flt = (b.get("filters") or [{}])[0] if b.get("filters") else {}
    counters = b.get("counters") or {}
    empty = b.get("empty_state") or {}
    cols = [{"field": c.get("field", ""), "label": _clean_label(c.get("label", ""))} for c in b.get("sortable_columns") or []]
    ranks = {}
    # 1) rank maps found in the screen's TS (matched to a column by name)
    for rank_name, mapping in (b.get("sort_ranks") or {}).items():
        for c in cols:
            if c["field"] and (_norm(c["field"]) in _norm(rank_name) or _norm(rank_name) in _norm(c["field"])):
                ranks[c["field"]] = {str(k).upper(): v for k, v in mapping.items()}
    # 2) a dropdown column is sorted by the option's STORED number (0, 1, 2 ...), not alphabetically:
    #    rank = the option key of the form spec; the alias (list / filter wording) gets the same rank
    for c in cols:
        if not c["field"] or c["field"] in ranks:
            continue
        f = next((x for x in (spec or {}).get("fields") or []
                  if isinstance(x, dict) and _norm(x.get("name")) == _norm(c["field"])), None)
        if not f or f.get("kind") != "combobox":
            continue
        m = {}
        for idx, o in enumerate(f.get("options") or []):
            if not isinstance(o, dict) or o.get("blank"):
                continue
            try:
                rank = int(str(o.get("key")).strip())
            except (TypeError, ValueError):
                rank = idx
            txt = str(o.get("text", "")).strip()
            if txt:
                m[txt.upper()] = rank
                alias = (aliases.get(f["name"]) or {}).get(txt)
                if alias:
                    m[alias.upper()] = rank
        if m:
            ranks[c["field"]] = m
    return {
        "modal": (b.get("modal") or {}).get("selector", ""),
        "roles": roles,
        "list": {
            "filter": {"class": _first_class(flt.get("class", "")), "all_option": flt.get("all_option", "")},
            "sortable_columns": cols,
            "sort_ranks": ranks,
            "views": [{"mode": v.get("mode", ""), "title": v.get("title", "")} for v in b.get("views") or []],
            "list_container": _first_class((containers.get("list") or {}).get("class", "")),
            "card_container": _first_class((containers.get("card") or {}).get("class", "")),
            "counter_label_class": _first_class(counters.get("label_class", "")),
            "counter_number_class": _first_class(counters.get("number_class", "")),
            "empty_state": {"class": _first_class(empty.get("class", "")), "message": empty.get("message", "")},
        },
    }


# --------------------------------------------------------------------------- JavaScript
_HELPERS = r'''
const TPL = __TPL__;
const TPL_LIST = TPL.list;
const tplSq = (t) => String(t == null ? '' : t).replace(/\s+/g, ' ').trim();
const tplDot = (c) => (c ? '.' + String(c).trim().split(/\s+/)[0] : '');
const tplEsc = (s) => String(s).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
const tplAlias = (label) => 'tplcounter_' + String(label).replace(/\W+/g, '_');

// the CONTROLS entry of a field, whatever the case / suffix of the name written in the feature
function tplControl(field) {
  const want = String(field || '').replace(/-(PickListDrop|PickList|ComboBox|Input|Textarea|DatePicker|RadioBox|CheckBox|UnCheckBox)$/i, '').toLowerCase();
  const key = Object.keys(CONTROLS).find((k) => k.toLowerCase() === want);
  return key ? Object.assign({ name: key }, CONTROLS[key]) : { name: String(field), dataCy: String(field) + '-Input', kind: 'input' };
}
function tplDataCys(field) {
  const c = tplControl(field);
  const base = c.name;
  return [c.dataCy, base + '-PickListDrop', base + '-PickList', base + '-ComboBox', base + '-Input', base + '-Textarea', base + '-DatePicker', base + '-RadioBox']
    .filter((v, i, a) => v && a.indexOf(v) === i);
}
function tplFindEl($b, field) {
  const hit = tplDataCys(field).find((n) => $b.find('[data-cy="' + n + '"]').filter(':visible').length > 0);
  return hit ? $b.find('[data-cy="' + hit + '"]').filter(':visible').first() : Cypress.$();
}
function tplValueOf($el) {
  if (!$el || !$el.length) return '';
  const $i = $el.is('input, textarea') ? $el : $el.find('input, textarea').filter(':visible').first();
  if ($i.length && $i.attr('type') !== 'checkbox') return tplSq($i.val());
  return tplSq($el.text());
}
function tplFormVisible($b) {
  if (TPL.modal) return $b.find(TPL.modal).filter(':visible').length;
  // a screen without a modal: the form is open when one of its controls is on screen
  return Object.keys(CONTROLS).filter((k) => tplFindEl($b, k).length > 0).length;
}
function tplClickFirst(label, selectors) {
  cy.get('body', { timeout: 30000 }).should(($b) => {
    expect(selectors.some((s) => $b.find(s).filter(':visible').length > 0), 'a visible "' + label + '" control').to.equal(true);
  });
  cy.get('body').then(($b) => {
    const sel = selectors.find((s) => $b.find(s).filter(':visible').length > 0);
    cy.wrap($b.find(sel).filter(':visible').first()).scrollIntoView().click({ force: true });
  });
  cy.wait(1000);
}
function tplListRoot($b) {
  const sel = [tplDot(TPL_LIST.list_container), tplDot(TPL_LIST.card_container)].filter(Boolean).join(', ');
  const $r = sel ? $b.find(sel).filter(':visible') : $b;
  return $r.length ? $r : $b;
}
function tplActionKey(title) {
  const t = String(title || '').toLowerCase().replace(/form$/, '').replace(/[^a-z]/g, '');
  return ({ save: 'save', update: 'update', delete: 'delete', edit: 'edit', addnew: 'addNew', add: 'addNew', new: 'addNew' })[t] || '';
}
function tplCounter($b, label) {
  const $l = $b.find('.' + TPL_LIST.counter_label_class)
    .filter((i, e) => tplSq(e.textContent).toLowerCase() === String(label).toLowerCase()).first();
  return parseInt(tplSq($l.parent().find('.' + TPL_LIST.counter_number_class).first().text()), 10);
}
'''

# (expression, js) - expression is the exact Cucumber expression of the definition
_DEFS: list[tuple[str, str]] = [
    ('I enter {string} in the {string} new record field', r'''
Then("I enter {string} in the {string} new record field", (value, field) => {
  if (!value) return;
  cy.get('body', { timeout: 30000 }).should(($b) => {
    expect(tplFindEl($b, field).length, 'the "' + field + '" control').to.be.greaterThan(0);
  });
  cy.get('body').then(($b) => {
    const $el = tplFindEl($b, field);
    const $input = $el.is('input, textarea') ? $el : $el.find('input, textarea').filter(':visible').first();
    if ($input.length) {
      cy.wrap($input).scrollIntoView().click({ force: true }).clear({ force: true }).type(value, { force: true, delay: 50 });
      return;
    }
    // a key picklist of a NEW record: open it and type the new value in its search bar
    cy.wrap($el).click({ force: true });
    cy.get("[data-cy='PicklistSearchBar']").should('be.visible').click().clear().type(value);
    cy.wait(1500);
    cy.get('body').then(($b2) => {
      const $new = $b2.find('[data-cy*="AddNew"], .addnew, [class*="add-new"]').filter(':visible').first();
      if ($new.length) cy.wrap($new).click({ force: true });
    });
  });
});'''),

    ('I clear the {string} field', r'''
Then("I clear the {string} field", (field) => {
  const kind = tplControl(field).kind;
  if (kind === 'combobox') {
    controlOf(field, '-ComboBox').click({ force: true });
    cy.get('mat-option, .mat-mdc-option, [role="option"]', { timeout: 15000 }).then(($o) => {
      const $blank = $o.filter(':visible').filter((i, e) => /^\s*(select\b.*|-+.*|)\s*$/i.test(tplSq(e.textContent))).first();
      expect($blank.length, 'a blank / "Select ..." option in ' + field).to.be.greaterThan(0);
      cy.wrap($blank).click({ force: true });
    });
    return;
  }
  cy.get('body', { timeout: 30000 }).should(($b) => {
    expect(tplFindEl($b, field).length, 'the "' + field + '" control').to.be.greaterThan(0);
  });
  cy.get('body').then(($b) => {
    const $el = tplFindEl($b, field);
    const $i = $el.is('input, textarea') ? $el : $el.find('input, textarea').filter(':visible').first();
    if ($i.length) {
      cy.wrap($i).scrollIntoView().click({ force: true }).clear({ force: true });
      return;
    }
    const $x = $el.find('[class*="clear"], [class*="close"], .mat-icon').filter(':visible').first();
    Cypress.log({ name: 'clear', message: 'no text box in "' + field + '" - used its clear icon' });
    expect($x.length, 'a way to clear "' + field + '"').to.be.greaterThan(0);
    cy.wrap($x).click({ force: true });
  });
});'''),

    ('the form should be open', r'''
Then("the form should be open", () => {
  cy.get('body', { timeout: 15000 }).should(($b) => { expect(tplFormVisible($b), 'a visible form').to.be.greaterThan(0); });
});'''),

    ('the form should still be open', r'''
Then("the form should still be open", () => {
  cy.get('body', { timeout: 15000 }).should(($b) => { expect(tplFormVisible($b), 'a visible form').to.be.greaterThan(0); });
});'''),

    ('the form should be closed', r'''
Then("the form should be closed", () => {
  cy.wait(800);
  cy.get('body', { timeout: 15000 }).should(($b) => { expect(tplFormVisible($b), 'visible forms').to.equal(0); });
});'''),

    ('the form fields should be empty', r'''
Then("the form fields should be empty", () => {
  cy.get('body', { timeout: 15000 }).should(($b) => {
    Object.keys(CONTROLS).forEach((k) => {
      const kind = CONTROLS[k].kind;
      if (kind === 'checkbox' || kind === 'radio') return;
      const $el = tplFindEl($b, k);
      if (!$el.length) return;
      const v = tplValueOf($el);
      const blank = v === '' || (kind !== 'input' && kind !== 'textarea' && /^(select\b.*|-+.*)$/i.test(v));
      expect(blank, 'the field "' + k + '" is empty (shows "' + v + '")').to.equal(true);
    });
  });
});'''),

    ('the {string} action should be {string}', r'''
Then("the {string} action should be {string}", (title, state) => {
  const want = /^visible$/i.test(state);
  const key = tplActionKey(title);
  const re = new RegExp('^\\s*' + tplEsc(title) + '\\s*$', 'i');
  cy.get('body', { timeout: 10000 }).should(($b) => {
    let n = 0;
    if (key) {
      n = [ACTIONS[key], DEFAULT_ACTIONS[key]].filter((v, i, a) => v && a.indexOf(v) === i)
        .reduce((sum, v) => sum + $b.find('[data-cy="' + v + '"]').filter(':visible').length, 0);
    }
    if (n === 0) {
      const $scope = TPL.modal && $b.find(TPL.modal).filter(':visible').length ? $b.find(TPL.modal).filter(':visible').first() : $b;
      n = $scope.find('*').filter(':visible').filter((i, e) => {
        const t = String(e.getAttribute('title') || e.getAttribute('aria-label') || '').trim();
        return re.test(t) || (e.children.length === 0 && re.test(Cypress.$(e).text()));
      }).length;
    }
    if (want) expect(n, 'the "' + title + '" action is visible').to.be.greaterThan(0);
    else expect(n, 'the "' + title + '" action is hidden').to.equal(0);
  });
});'''),

    ('the {string} field should show {string}', r'''
Then("the {string} field should show {string}", (field, value) => {
  const c = tplControl(field);
  if (c.kind === 'checkbox') {
    const wanted = /^(true|yes|checked|on|1)$/i.test(String(value).trim());
    cy.get('body', { timeout: 15000 }).should(($b) => {
      const now = readCheckbox($b, checkboxNames(field));
      expect(now, 'checkbox "' + field + '"').to.not.equal(null);
      expect(now.checked, 'checkbox "' + field + '" is ' + (wanted ? 'checked' : 'unchecked')).to.equal(wanted);
    });
    return;
  }
  cy.get('body', { timeout: 15000 }).should(($b) => {
    const $el = tplFindEl($b, field);
    expect($el.length, 'the "' + field + '" control').to.be.greaterThan(0);
    const now = tplValueOf($el);
    if (/^\(not empty\)$/i.test(tplSq(value))) {
      expect(now, 'the calculated field "' + field + '" is not empty').to.not.equal('');
      return;
    }
    const want = (typeof relDate === 'function' && /^today\s*([+-]\s*\d+)?$/i.test(tplSq(value))) ? relDate(value) : tplSq(value);
    expect(now.toLowerCase(), 'the value of "' + field + '"').to.include(want.toLowerCase());
  });
});'''),

    ('I click the Reset button', r'''
Then("I click the Reset button", () => {
  const r = TPL.roles.reset || {};
  const m = TPL.modal ? TPL.modal + ' ' : '';
  tplClickFirst('Reset', [
    r.class ? m + tplDot(r.class) : '', r.title ? m + '[title="' + r.title + '"]' : '',
    '[data-cy="ResetForm"]', m + '[title="Reset"]', m + '[title="New"]', m + '[aria-label="Reset"]',
  ].filter(Boolean));
});'''),

    ('I click the Close button', r'''
Then("I click the Close button", () => {
  const r = TPL.roles.close || {};
  const m = TPL.modal ? TPL.modal + ' ' : '';
  tplClickFirst('Close', [
    r.class ? m + tplDot(r.class) : '', r.title ? m + '[title="' + r.title + '"]' : '',
    '[data-cy="CloseForm"]', m + '[title="Close"]', m + '[aria-label="Close"]', m + '[class*="close"]',
    '.mat-mdc-dialog-component-host > .mat-icon',
  ].filter(Boolean));
});'''),

    ('I request to delete the open record', r'''
Then("I request to delete the open record", () => {
  // only presses Delete: the prompt is answered by "I confirm the delete" / "I cancel the delete"
  cy.wait(1000);
  clickAction('delete');
});'''),

    ('I search the list for {string}', r'''
Then("I search the list for {string}", (value) => {
  cy.get('body', { timeout: 15000 }).should(($b) => { expect(findSearchBox($b).length, 'a search box').to.be.greaterThan(0); });
  cy.get('body').then(($b) => {
    cy.wrap(findSearchBox($b)).click({ force: true }).clear({ force: true }).type(value, { force: true, delay: 30 });
    cy.wait(1200);
  });
});'''),

    ('I clear the list search', r'''
Then("I clear the list search", () => {
  cy.get('body', { timeout: 15000 }).should(($b) => { expect(findSearchBox($b).length, 'a search box').to.be.greaterThan(0); });
  cy.get('body').then(($b) => {
    cy.wrap(findSearchBox($b)).click({ force: true }).clear({ force: true });
    cy.wait(1200);
  });
});'''),

    ('I should see the record {string} in the list', r'''
Then("I should see the record {string} in the list", (value) => {
  cy.get('body', { timeout: 15000 }).should(($b) => {
    expect(tplSq(tplListRoot($b).text()).toLowerCase().includes(tplSq(value).toLowerCase()), '"' + value + '" is in the list').to.equal(true);
  });
});'''),

    ('I should not see the record {string} in the list', r'''
Then("I should not see the record {string} in the list", (value) => {
  cy.wait(1000);
  cy.get('body', { timeout: 15000 }).should(($b) => {
    expect(tplSq(tplListRoot($b).text()).toLowerCase().includes(tplSq(value).toLowerCase()), '"' + value + '" is NOT in the list').to.equal(false);
  });
});'''),

    ('the list row {string} should show {string}', r'''
Then("the list row {string} should show {string}", (record, text) => {
  cy.get('body', { timeout: 15000 }).should(($b) => {
    const $leaf = tplListRoot($b).find('*').filter(':visible')
      .filter((i, e) => e.children.length === 0 && tplSq(e.textContent).includes(record)).first();
    expect($leaf.length, 'a list entry for "' + record + '"').to.be.greaterThan(0);
    let $row = $leaf.closest('tr');
    if (!$row.length) $row = $leaf.closest('[class*="card"]:not([class*="grid"]):not([class*="header"]):not([class*="body"])');
    expect($row.length, 'the row of "' + record + '"').to.be.greaterThan(0);
    expect(tplSq($row.text()).toLowerCase()).to.include(tplSq(text).toLowerCase());
  });
});'''),

    ('I filter the list by type {string}', r'''
Then("I filter the list by type {string}", (value) => {
  const cls = tplDot(TPL_LIST.filter.class);
  cy.get('select' + cls, { timeout: 15000 }).filter(':visible').first().select(value);
  cy.wait(800);
});'''),

    ('I sort the list by the {string} column in {string} order', r'''
Then("I sort the list by the {string} column in {string} order", (label, order) => {
  const clicks = /^asc/i.test(order) ? 1 : 2;       // a column starts ascending; a second click reverses it
  for (let i = 0; i < clicks; i += 1) {
    cy.get('th', { timeout: 15000 }).filter(':visible')
      .filter((j, e) => tplSq(e.textContent).toLowerCase().includes(String(label).toLowerCase())).first().click({ force: true });
    cy.wait(400);
  }
});'''),

    ('the list should be sorted by {string} in {string} order', r'''
Then("the list should be sorted by {string} in {string} order", (label, order) => {
  const dir = /^desc/i.test(order) ? -1 : 1;
  const col = TPL_LIST.sortable_columns.find((c) => tplSq(c.label).toLowerCase().includes(String(label).toLowerCase()));
  const ranks = (col && TPL_LIST.sort_ranks[col.field]) || null;
  cy.wait(600);
  cy.get('body', { timeout: 15000 }).should(($b) => {
    const $th = $b.find('th').filter(':visible')
      .filter((i, e) => tplSq(e.textContent).toLowerCase().includes(String(label).toLowerCase())).first();
    expect($th.length, 'a table header "' + label + '" to check the order of').to.be.greaterThan(0);
    const idx = $th.index();
    const vals = $th.closest('table').find('tbody tr').filter(':visible')
      .map((i, tr) => tplSq(Cypress.$(tr).children().eq(idx).text())).get();
    expect(vals.length, 'at least two rows to check the order of "' + label + '"').to.be.greaterThan(1);
    const key = (v) => (ranks && ranks[v.toUpperCase()] !== undefined ? ranks[v.toUpperCase()] : v.toLowerCase());
    const cmp = (a, b) => {
      const x = key(a), y = key(b);
      if (typeof x === 'number' && typeof y === 'number') return x - y;
      const nx = parseFloat(x), ny = parseFloat(y);
      if (!Number.isNaN(nx) && !Number.isNaN(ny) && String(nx) === String(x) && String(ny) === String(y)) return nx - ny;
      return String(x) < String(y) ? -1 : (String(x) > String(y) ? 1 : 0);   // character-code order, like the app
    };
    for (let i = 1; i < vals.length; i += 1) {
      expect(cmp(vals[i - 1], vals[i]) * dir, '"' + vals[i - 1] + '" before "' + vals[i] + '" in ' + order + ' order').to.be.at.most(0);
    }
  });
});'''),

    ('I switch to the {string} view', r'''
Then("I switch to the {string} view", (view) => {
  const mode = /card/i.test(view) ? 'card' : 'list';
  const v = TPL_LIST.views.find((x) => x.mode === mode) || {};
  tplClickFirst(view + ' view', [
    v.title ? '[title="' + v.title + '"]' : '', '[title="' + (mode === 'card' ? 'Card View' : 'List View') + '"]',
    '[aria-label="' + (mode === 'card' ? 'Card View' : 'List View') + '"]',
  ].filter(Boolean));
});'''),

    ('I should see the {string} view', r'''
Then("I should see the {string} view", (view) => {
  const mode = /card/i.test(view) ? 'card' : 'list';
  const cls = tplDot(mode === 'card' ? TPL_LIST.card_container : TPL_LIST.list_container);
  expect(cls, 'a known container class for the ' + mode + ' view (see TPL.list in the script)').to.not.equal('');
  cy.get('body', { timeout: 15000 }).should(($b) => {
    expect($b.find(cls).filter(':visible').length, 'the ' + mode + ' view is shown').to.be.greaterThan(0);
  });
});'''),

    ('I should see the empty state {string}', r'''
Then("I should see the empty state {string}", (message) => {
  const sel = tplDot(TPL_LIST.empty_state.class);
  const key = tplSq(message).toLowerCase().slice(0, 40);
  cy.get('body', { timeout: 15000 }).should(($b) => {
    const $e = sel ? $b.find(sel).filter(':visible') : Cypress.$();
    const text = tplSq($e.length ? $e.text() : $b.text()).toLowerCase();
    expect(text.includes(key), 'the empty state "' + message + '"').to.equal(true);
  });
});'''),

    ('I remember the number shown for {string}', r'''
Then("I remember the number shown for {string}", (label) => {
  cy.get('body', { timeout: 15000 }).should(($b) => { expect(Number.isNaN(tplCounter($b, label)), 'a number for "' + label + '"').to.equal(false); });
  cy.get('body').then(($b) => { cy.wrap(tplCounter($b, label)).as(tplAlias(label)); });
});'''),

    ('the number shown for {string} should have changed by {string}', r'''
Then("the number shown for {string} should have changed by {string}", (label, delta) => {
  cy.get('@' + tplAlias(label)).then((before) => {
    cy.get('body', { timeout: 15000 }).should(($b) => {
      expect(tplCounter($b, label), '"' + label + '" changed by ' + delta).to.equal(before + parseInt(delta, 10));
    });
  });
});'''),
]


def known_expressions() -> list[str]:
    return [e for e, _ in _DEFS]


def _matches(expression: str, sentence: str) -> bool:
    probe = re.sub(r"<[^<>]+>", "x", sentence)
    rx = step_checker.expression_to_regex(expression)
    return rx.fullmatch(probe) is not None or rx.fullmatch(sentence) is not None


def build_block(behavior: dict | None, missing: list[str], spec: dict | None = None) -> tuple[str, list[str]]:
    """(javascript block, step sentences it defines) for the missing sentences it knows. ("", []) when none."""
    chosen: list[str] = []
    covered: list[str] = []
    for expr, js in _DEFS:
        hits = [s for s in missing if _matches(expr, s)]
        if hits:
            chosen.append(js.strip("\n"))
            covered.extend(h for h in hits if h not in covered)
    if not chosen:
        return "", []
    helpers = _HELPERS.replace("__TPL__", json.dumps(build_config(behavior, spec), indent=2, ensure_ascii=False))
    return INSERT_NOTE + helpers + "\n" + "\n\n".join(chosen) + "\n", covered