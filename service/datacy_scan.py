from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
_WORKSPACE = _BACKEND_ROOT / "cypress-workspace"
# Deliberately NOT under cypress/_runs: the project's step-definition glob is
# "cypress/_runs/**/*.js", so anything placed there would be loaded by real runs.
_SCAN_DIR = _WORKSPACE / "cypress" / "_scan"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_NPX = shutil.which("npx") or "npx"

LogFn = Callable[..., Awaitable[None]]

_scan_lock: asyncio.Lock | None = None
_fail_until: dict[str, float] = {}   # cache_key -> epoch seconds until which we do not retry


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------
def _flag(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() not in ("0", "false", "no", "off", "")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except ValueError:
        return default


def scan_enabled() -> bool:
    return _flag("SCAN_ENABLED", "1")


def _missing_settings() -> list[str]:
    return [
        name for name in ("SCAN_DB_NAME", "SCAN_USERNAME", "SCAN_PASSWORD")
        if not os.getenv(name, "").strip()
    ]


def _cache_dir() -> Path:
    custom = os.getenv("SCAN_CACHE_DIR", "").strip()
    return Path(custom) if custom else _BACKEND_ROOT / "cache" / "datacy_scan"


# --------------------------------------------------------------------------
# helpers on the analysis JSON
# --------------------------------------------------------------------------
def _first_page(project_analysis: dict) -> dict:
    modules = project_analysis.get("modules") or []
    pages = (modules[0].get("pages") or []) if modules else []
    return pages[0] if pages and isinstance(pages[0], dict) else {}


def _clean_path(navigation_path) -> list[str]:
    out: list[str] = []
    for part in navigation_path or []:
        if isinstance(part, dict):          # analysis hops: {"hop_label": "Advance", ...}
            part = part.get("hop_label") or part.get("label") or part.get("name") or ""
        s = str(part).replace('"', "").replace("'", "").strip()
        if s and s.lower() != "unknown":
            out.append(s)
    return out


def _norm(text) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def _iter_fields(page: dict):
    """Yields (field_dict, is_loop) for form fields and repeating-row fields."""
    for form in page.get("forms") or []:
        for field in (form.get("fields") or []) if isinstance(form, dict) else []:
            if isinstance(field, dict):
                yield field, False
    for loop in page.get("loops") or []:
        for field in (loop.get("fields") or []) if isinstance(loop, dict) else []:
            if isinstance(field, dict):
                yield field, True


def _field_is_verified(field: dict) -> bool:
    loc = field.get("automation_locator")
    return (
        isinstance(loc, dict)
        and loc.get("verified") is True
        and str(loc.get("strategy") or "").lower().replace("-", "_") == "data_cy"
        and bool(loc.get("value"))
        and loc.get("value") != "Unknown"
    )


def has_unresolved_fields(project_analysis: dict) -> bool:
    page = _first_page(project_analysis)
    return any(not _field_is_verified(f) for f, _ in _iter_fields(page))


# --------------------------------------------------------------------------
# the Cypress scan
# --------------------------------------------------------------------------
# The QC team's own login flow, copied as-is. Used only by the scan.
_LOGIN_BLOCK = r"""
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

_SCAN_STEPS = r"""
import { Given, When, Then } from "@badeball/cypress-cucumber-preprocessor";
const creds = Cypress.env('TestConnections').UILogin;

// The ERP page sometimes throws errors of its own (not Cypress errors). They
// must not stop the scan, which only reads attributes from the page.
Cypress.on('uncaught:exception', () => false);
""" + _LOGIN_BLOCK + r"""
// Finds the element whose data-cy is "<label><suffix>", ignoring case, spaces and
// punctuation, so folder-style names ("advancepayment") still find "Advance Payment".
function scanFind(suffix, label) {
  const squash = (s) => String(s || "").toLowerCase().replace(/[^a-z0-9]/g, "");
  return cy.get(`[data-cy$="${suffix}" i]`, { timeout: 60000 }).should('have.length.greaterThan', 0).then(($els) => {
    const want = squash(label);
    const hit = $els.filter((i, el) => squash(el.getAttribute("data-cy").slice(0, -suffix.length)) === want);
    expect(hit.length, `a "${suffix}" element matching "${label}"`).to.be.greaterThan(0);
    return cy.wrap(hit.first());
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

// ---- finding the screen in the REAL menu ---------------------------------------
// The menu is a tree. Its nodes carry data-cy values like "Advance-MenuFolder0"
// (a folder) and "AdvancePayment-MenuData1" (a screen); the number is the depth.
// Folders only show their children once opened, so we look for the screen and, if
// it is not there yet, open folders one by one until it appears. The folders that
// lead to it are read back from the page order (the menu is a flat, depth-first
// list) and saved, so the generated test can use the real path.
const navState = { module: "", path: [], matchedBy: "exact", matchedName: "", alternatives: [] };
const NAV_RE = /^(.*)-(MenuFolder|MenuData)(\d+)$/;
const squash = (s) => String(s || "").toLowerCase().replace(/[^a-z0-9]/g, "");

function navNodes(doc) {
  const out = [];
  doc.querySelectorAll('[data-cy*="-MenuFolder"], [data-cy*="-MenuData"]').forEach((el) => {
    const m = NAV_RE.exec(el.getAttribute("data-cy") || "");
    if (m) out.push({ el: el, name: m[1], kind: m[2], depth: parseInt(m[3], 10) });
  });
  return out;
}

// folders first, the node itself last: ["SkillTaxonomy", "SkillDomain"]
function pathTo(nodes, idx) {
  const path = [nodes[idx].name];
  let depth = nodes[idx].depth;
  for (let i = idx - 1; i >= 0 && depth > 0; i--) {
    if (nodes[i].kind === "MenuFolder" && nodes[i].depth === depth - 1) {
      path.unshift(nodes[i].name);
      depth -= 1;
    }
  }
  return path;
}

// ---- similar-name search ---------------------------------------------------------
// Used only when NO menu item has exactly the screen's name. The name in the source code is
// not always the name in the menu ("leaveondutyrequest" -> "LeaveRequest"), so the menu items
// are ranked by how much their name (or visible text) looks like the screen's name / title.
// The backend then checks the opened page really has this screen's fields, and asks for the
// next candidate if not (SCAN_PICK).
// <rank-start>
function splitWords(s) {
  return String(s || "")
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .replace(/([A-Z]+)([A-Z][a-z])/g, "$1 $2")
    .toLowerCase().split(/[^a-z0-9]+/).filter(Boolean);
}
function lcsLen(a, b) {
  const m = a.length, n = b.length;
  if (!m || !n) return 0;
  let prev = new Array(n + 1).fill(0);
  for (let i = 1; i <= m; i++) {
    const cur = [0];
    for (let j = 1; j <= n; j++) cur[j] = a[i - 1] === b[j - 1] ? prev[j - 1] + 1 : Math.max(prev[j], cur[j - 1]);
    prev = cur;
  }
  return prev[n];
}
function nameScore(menuName, targets) {
  const cand = squash(menuName);
  if (!cand) return 0;
  const cw = splitWords(menuName);
  const hasTitle = targets.some((t) => t.words.length > 1);   // a real title ("Leave Onduty Request") beats the run-together code name
  let best = 0;
  for (const t of targets) {
    if (!t.squashed) continue;
    if (cand === t.squashed) return 1;
    const dice = (2 * lcsLen(cand, t.squashed)) / (cand.length + t.squashed.length);
    let cover = 0;
    if (t.words.length > 1) {                       // a title with real words: whole words must match
      const set = new Set(t.words);
      cover = cw.length ? cw.filter((w) => set.has(w)).length / cw.length : 0;
    } else if (!hasTitle) {                         // only a run-together name: its words must appear inside it
      const sig = cw.filter((w) => w.length >= 3);
      cover = sig.length ? sig.filter((w) => t.squashed.includes(w)).length / sig.length : 0;
    }
    let score = 0.5 * dice + 0.5 * cover;
    if (cw.length && cw[0].length >= 3 && t.squashed.startsWith(cw[0])) score += 0.05;   // menu names often just drop middle words
    best = Math.max(best, Math.min(score, 0.99));
  }
  return best;
}
function rankMenu(items, screen, labels) {
  // items: [{ idx, name, text }]  ->  [{ idx, score }] best first, weak matches left out
  const targets = [screen].concat(labels || []).map((x) => ({ squashed: squash(x), words: splitWords(x) }));
  return items
    .map((it) => ({ idx: it.idx, score: Math.max(nameScore(it.name, targets), nameScore(it.text, targets)) }))
    .filter((r) => r.score >= 0.5)
    .sort((a, b) => b.score - a.score || a.idx - b.idx);
}
// <rank-end>

function clickNode(el) {
  const $el = Cypress.$(el);
  const $item = $el.children(".menu-item");
  cy.wrap($item.length ? $item.first() : $el.first()).scrollIntoView().should('be.visible').click({ force: true });
  dismissPopups({ quietMs: 800 });   // a popup here would hide the menu / the form that is read next
}

function findAndOpen(screen, hints, st) {
  const want = squash(screen);
  const hintKeys = hints.map(squash);
  cy.get("body").then(($body) => {
    const nodes = navNodes($body[0].ownerDocument);
    const hits = [];
    nodes.forEach((n, i) => { if (n.kind === "MenuData" && squash(n.name) === want) hits.push(i); });
    if (hits.length > 0) {
      const best = hits.find((i) => hintKeys.every((h) => pathTo(nodes, i).map(squash).includes(h)));
      const idx = best === undefined ? hits[0] : best;
      navState.path = pathTo(nodes, idx);
      navState.matchedBy = "exact";
      navState.matchedName = nodes[idx].name;
      clickNode(nodes[idx].el);
      return;
    }
    const folders = nodes
      .map((n, i) => ({ n: n, key: pathTo(nodes, i).join("/") }))
      .filter((f) => f.n.kind === "MenuFolder" && !st.clicked.has(f.key));
    if (folders.length === 0 && st.idle < 4) {
      // nothing left to open right now - the menu may still be loading, look again
      st.idle += 1;
      cy.wait(2000);
      findAndOpen(screen, hints, st);
      return;
    }
    if (folders.length === 0 || st.attempt >= 150) {
      // every folder is open and still no menu item has exactly this name: take the most similar one
      const items = [];
      nodes.forEach((n, i) => { if (n.kind === "MenuData") items.push({ idx: i, name: n.name, text: (n.el.innerText || "").trim() }); });
      const ranked = rankMenu(items, screen, Cypress.env("SCAN_LABELS") || []);
      const pick = parseInt(Cypress.env("SCAN_PICK") || 0, 10) || 0;
      if (ranked.length > pick) {
        const chosen = nodes[ranked[pick].idx];
        navState.path = pathTo(nodes, ranked[pick].idx);
        navState.matchedBy = "similar";
        navState.matchedName = chosen.name;
        navState.alternatives = ranked.slice(0, 5).map((r) => nodes[r.idx].name);
        Cypress.log({ name: "menu", message: `no menu item is named "${screen}" - opening the most similar one: "${chosen.name}"` });
        clickNode(chosen.el);
        return;
      }
    }
    const seen = nodes.slice(0, 10).map((n) => n.name + "-" + n.kind + n.depth).join(", ");
    expect(folders.length > 0 && st.attempt < 150,
      `the screen "${screen}" is in the menu (menu items on the page: ${nodes.length} [${seen}]; folders opened: ${Array.from(st.clicked).join(", ") || "none"})`).to.equal(true);
    st.idle = 0;
    st.attempt += 1;
    // folders named in the guessed path first, then the deepest (newly opened) ones
    folders.sort((a, b) =>
      ((hintKeys.includes(squash(b.n.name)) ? 1 : 0) - (hintKeys.includes(squash(a.n.name)) ? 1 : 0))
      || (b.n.depth - a.n.depth));
    st.clicked.add(folders[0].key);
    clickNode(folders[0].n.el);
    cy.wait(700);
    findAndOpen(screen, hints, st);
  });
}

Then("I open the screen {string} in the module {string} via {string}", (screen, module, via) => {
  dismissPopups();   // popups left over from login
  cy.get('[data-cy="ModuleList"] > .mat-icon').should('be.visible').and('not.be.disabled').dblclick()
  scanFind("-Module", module).then(($m) => {
    navState.module = String($m.attr("data-cy")).slice(0, -"-Module".length);
    cy.wrap($m).children('.card-image').scrollIntoView().should('be.visible').click();
  });
  dismissPopups();   // popups after opening the module, before the menu is read
  // wait for the side menu to be drawn (this is the wait the old step had)
  cy.get('[data-cy*="-MenuFolder"], [data-cy*="-MenuData"]', { timeout: 60000 }).should('have.length.greaterThan', 0);
  cy.wait(1000);
  const hints = String(via || "").split("|").map((x) => x.trim()).filter(Boolean);
  findAndOpen(screen, hints, { attempt: 0, idle: 0, clicked: new Set() });
})

// Master screens often show only a list until Add New is pressed.
Then("I open the entry form if needed", () => {
  cy.wait(3000);
  const tries = (Cypress.env("SCAN_OPEN_SELECTORS") || []).map((v) => `[data-cy="${v}"]`).concat(['[data-cy="AddNewForm"]', ".btn-add"]);
  cy.get("body").then(($body) => {
    const fieldSel = '[data-cy$="-Input"], [data-cy$="-PickList"], [data-cy$="-Textarea"], [data-cy$="-ComboBox"], ' +
      '[data-cy$="-CheckBox"], [data-cy$="-UnCheckBox"], [data-cy$="-DatePickerToggle"], [data-cy$="-RadioBox"]';
    if ($body.find(fieldSel).filter(':visible').length > 0) return;      // the form is already on the page
    const sel = tries.find((t) => $body.find(t).filter(':visible').length > 0);
    if (sel) {
      cy.get(sel).filter(':visible').first().click({ force: true });
      cy.wait(2500);
    }
  });
})

Then("I prepare the Material Management form", () => {
  const bizField = Cypress.env("SCAN_BIZTYPE_FIELD") || "BizTransactionTypeName";
  cy.wait(3000);
  cy.get("body").then(($body) => {
    if ($body.find('[data-cy="AddNewForm"]').length) {
      cy.get('[data-cy="AddNewForm"]').first().click({ force: true });
      cy.wait(2000);
    }
  });
  cy.get("body").then(($body) => {
    const drop = `[data-cy="${bizField}-PickListDrop"]`;
    if ($body.find(drop).length) {
      cy.get(drop).first().click({ force: true });
      cy.wait(2500);
      cy.get("body").then(($b2) => {
        if ($b2.find(".ag-row-even > .ag-column-first").length) {
          cy.get(".ag-row-even > .ag-column-first").first().click({ force: true });
          cy.wait(3000);
        }
      });
    }
  });
})

Then("I collect the data-cy attributes", () => {
  cy.wait(4000);
  cy.document().then((doc) => {
    const clean = (s) => (s || "").replace(/\s+/g, " ").trim().slice(0, 80);
    const labelFor = (el) => {
      const direct = el.getAttribute("aria-label") || el.getAttribute("placeholder") || el.getAttribute("title");
      if (direct) return clean(direct);
      const box = el.closest("mat-form-field, .mat-form-field, .form-group");
      const lab = box && box.querySelector("mat-label, label");
      if (lab && lab.textContent) return clean(lab.textContent);
      return clean(el.innerText || el.textContent);
    };
    const seen = new Set();
    const elements = [];
    doc.querySelectorAll("[data-cy]").forEach((el) => {
      const dc = el.getAttribute("data-cy");
      if (!dc || seen.has(dc)) return;
      seen.add(dc);
      const rect = el.getBoundingClientRect();
      const inner = el.matches("input, textarea, select") ? el : el.querySelector("input, textarea, select");
      elements.push({
        data_cy: dc,
        tag: el.tagName.toLowerCase(),
        type: inner ? (inner.getAttribute("type") || inner.tagName.toLowerCase()) : "",
        label: labelFor(el),
        visible: rect.width > 0 && rect.height > 0,
        interactive: !!inner || el.tagName === "BUTTON" || el.getAttribute("role") === "button",
        // a control the user cannot change, and what it shows (e.g. Employee Name filled in from the login)
        locked: !!(inner && (inner.disabled || inner.readOnly)) || !!el.closest(".mat-form-field-disabled, .mat-mdc-form-field-disabled"),
        value: inner ? String(inner.value || "").replace(/\s+/g, " ").trim().slice(0, 80) : "",
      });
    });
    cy.writeFile(Cypress.env("SCAN_OUT"), {
      url: doc.location.href, elements: elements,
      navigation: { module: navState.module, path: navState.path, matched_by: navState.matchedBy,
                    matched_name: navState.matchedName, alternatives: navState.alternatives },
    });
  });
})
"""


def _feature_text(nav: list[str], is_mm: bool = False) -> str:
    """nav = [module, (guessed folders...), screen]. Only the module and the screen
    are trusted; the guessed folders are just hints for which folder to open first."""
    module, screen = nav[0], nav[-1]
    via = "|".join(nav[1:-1])
    prepare = (
        "    And I prepare the Material Management form\n" if is_mm
        else "    And I open the entry form if needed\n"
    )
    return (
        "Feature: data-cy scan\n"
        "  Background:\n"
        f'    And I open the screen "{screen}" in the module "{module}" via "{via}"\n'
        "  Scenario: collect data-cy attributes\n"
        f"{prepare}"
        "    Then I collect the data-cy attributes\n"
    )


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=15)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _execute(cmd: list[str], cwd: Path, timeout: int, env: dict) -> tuple[int | None, str, bool]:
    """Runs a command; returns (exit_code, output, timed_out). Kills the whole
    process tree on timeout so no orphan Cypress/Electron process is left."""
    kwargs: dict = dict(
        cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", env=env,
    )
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, **kwargs)
    try:
        out, _ = proc.communicate(timeout=timeout)
        return proc.returncode, out or "", False
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            out, _ = proc.communicate(timeout=15)
        except Exception:
            out = ""
        return None, out or "", True


def _failure_reason(output: str, password: str, timed_out: bool, timeout: int) -> str:
    if timed_out:
        return f"timed out after {timeout}s"
    lines = [_ANSI_RE.sub("", ln).strip() for ln in (output or "").splitlines()]
    for i, ln in enumerate(lines):
        if "originated from your application code" in ln:
            detail = [x.lstrip("> ").strip() for x in lines[i + 1:i + 8] if x.strip()][:1]
            text = "the app threw an error: " + " | ".join(detail) if detail else "the app threw an error"
            return (text.replace(password, "***") if password else text)[:300]
    picks = [
        ln for ln in lines
        if ln and any(k in ln for k in ("Timed out", "AssertionError", "CypressError", "Error:", "no spec files"))
    ]
    text = " | ".join(picks[:3]) if picks else "no data-cy file was produced"
    if password:
        text = text.replace(password, "***")
    return text[:300]


def _open_form_selectors(project_analysis: dict) -> list[str]:
    """data-cy values of the screen's own "new / add / create" buttons, from the analysis."""
    out: list[str] = []
    for action in _first_page(project_analysis).get("business_actions") or []:
        if not isinstance(action, dict):
            continue
        name = _norm(action.get("action_name"))
        value = str(action.get("data_cy") or (action.get("automation_locator") or {}).get("value") or "")
        if value and value != "Unknown" and any(k in name for k in ("new", "add", "create")):
            out.append(value)
    return out


def _run_scan(module: str, screen: str, nav: list[str], is_mm: bool = False,
              biz_field: str = "", open_cy: list[str] | None = None,
              labels: list[str] | None = None, pick: int = 0) -> tuple[dict | None, str]:
    """Blocking. Returns (scan_dict, "") on success or (None, reason)."""
    if not _WORKSPACE.exists():
        return None, "cypress-workspace/ folder not found"
    if not (_WORKSPACE / "node_modules").exists():
        return None, "node_modules missing in cypress-workspace/ (run npm install there)"

    password = os.getenv("SCAN_PASSWORD", "")
    timeout = max(60, _int("SCAN_TIMEOUT_SECONDS", 240))
    scan_id = f"scan_{uuid.uuid4().hex[:10]}"
    run_dir = _SCAN_DIR / scan_id
    out_file = run_dir / "result.json"
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / f"{scan_id}.feature").write_text(_feature_text(nav, is_mm), encoding="utf-8")
        (run_dir / f"{scan_id}.js").write_text(_SCAN_STEPS, encoding="utf-8")

        rel_dir = f"cypress/_scan/{scan_id}"
        base_url = os.getenv("SCAN_BASE_URL", "").strip()
        config_pairs = [
            f"specPattern={rel_dir}/*.feature",
            "screenshotOnRunFailure=false",
            "video=false",
            "defaultCommandTimeout=30000",
        ]
        if base_url:
            config_pairs.append(f"baseUrl={base_url}")
        env_payload = {
            "TestConnections": {"UILogin": {
                "dbName": os.getenv("SCAN_DB_NAME", "").strip(),
                "userName": os.getenv("SCAN_USERNAME", "").strip(),
                "password": password,
            }},
            "SCAN_OUT": f"{rel_dir}/result.json",
            "SCAN_BIZTYPE_FIELD": biz_field or "BizTransactionTypeName",
            "SCAN_OPEN_SELECTORS": open_cy or [],
            "SCAN_LABELS": labels or [],       # the screen's title(s), to find a similarly named menu item
            "SCAN_PICK": int(pick or 0),       # 0 = most similar menu item, 1 = the next one, ...
            # Point the cucumber preprocessor at this scan's own step file only.
            "stepDefinitions": [f"{rel_dir}/*.js"],
        }
        cmd = [
            _NPX, "cypress", "run",
            "--spec", f"{rel_dir}/{scan_id}.feature",
            "--reporter", "spec",
            "--config", ",".join(config_pairs),
            "--env", json.dumps(env_payload),
        ]
        env = {**os.environ, "FORCE_COLOR": "0", "NO_COLOR": "1"}
        logger.info("data-cy scan: starting %s for %s / %s (path=%s, mm=%s)", scan_id, module, screen, nav, is_mm)
        try:
            code, output, timed_out = _execute(cmd, _WORKSPACE, timeout, env)
        except FileNotFoundError as e:
            return None, f"could not launch npx ({e})"

        if out_file.exists():
            try:
                data = json.loads(out_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                return None, f"scan result unreadable ({e})"
            elements = [e for e in (data.get("elements") or []) if isinstance(e, dict) and e.get("data_cy")]
            if not elements:
                return None, "page opened but no data-cy attributes were found on it"
            return {
                "module": module, "screen": screen, "navigation": nav, "mm": is_mm,
                "scanned_at": datetime.now(timezone.utc).isoformat(),
                "url": data.get("url"), "elements": elements,
                "found_navigation": data.get("navigation") or {},
            }, ""
        return None, _failure_reason(output, password, timed_out, timeout)
    except Exception as e:  # never let a scan problem reach Generate
        logger.exception("data-cy scan crashed")
        return None, f"scan crashed ({e})"
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


# --------------------------------------------------------------------------
# cache
# --------------------------------------------------------------------------
def _cache_key(module: str, screen: str, nav: list[str], is_mm: bool = False) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", f"{module}-{screen}".lower()).strip("-") or "screen"
    digest = hashlib.sha1(
        ("v3|" + "|".join(nav) + "|" + os.getenv("SCAN_BASE_URL", "") + ("|mm" if is_mm else "")).encode("utf-8")
    ).hexdigest()[:8]
    return f"{slug}-{digest}"


def _load_cache(key: str) -> dict | None:
    if _flag("SCAN_FORCE_REFRESH", "0"):
        return None
    ttl_hours = _int("SCAN_CACHE_HOURS", 168)
    if ttl_hours <= 0:
        return None
    path = _cache_dir() / f"{key}.json"
    try:
        if not path.exists() or (time.time() - path.stat().st_mtime) > ttl_hours * 3600:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if data.get("elements") else None
    except (OSError, json.JSONDecodeError):
        return None


def _save_cache(key: str, scan: dict) -> None:
    try:
        folder = _cache_dir()
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{key}.json").write_text(json.dumps(scan, indent=2), encoding="utf-8")
    except OSError as e:
        logger.warning("data-cy scan: could not save cache (%s)", e)


# --------------------------------------------------------------------------
# merge scan -> analysis
# --------------------------------------------------------------------------
_SUFFIX_RE = re.compile(r"^(?P<base>.+)-(?P<suffix>[A-Za-z]+)$")


def _group_by_base(elements: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for el in elements:
        dc = str(el.get("data_cy") or "")
        m = _SUFFIX_RE.match(dc)
        base = m.group("base") if m else dc
        groups.setdefault(_norm(base), []).append(el)
    return groups


def _pick_element(group: list[dict]) -> dict:
    # The shortest data-cy is the main control (ItemCode-PickList beats
    # ItemCode-PickListDrop); interactive ones first.
    return sorted(group, key=lambda e: (not e.get("interactive"), len(str(e.get("data_cy"))), str(e.get("data_cy"))))[0]


def _find_group(name_norm: str, groups: dict[str, list[dict]]) -> list[dict] | None:
    if not name_norm:
        return None
    if name_norm in groups:
        return groups[name_norm]
    # grid/loop controls carry a row index: ItemCode -> ItemCode0, ItemCode1 ...
    indexed = []
    for key, grp in groups.items():
        m = re.fullmatch(re.escape(name_norm) + r"(\d+)", key)
        if m:
            indexed.append((int(m.group(1)), grp))
    if indexed:
        indexed.sort(key=lambda t: t[0])
        return indexed[0][1]
    return None


def _set_locator(item: dict, value: str, trace: str) -> None:
    item["data_cy"] = value
    item["preferred_locator"] = value
    item["automation_locator"] = {
        "strategy": "data_cy", "value": value, "verified": True,
        "source": "live app scan",
    }
    item["resolution_trace"] = trace


def _clear_locator(item: dict, trace: str) -> None:
    """The analysis claimed a data-cy that is not on the real page: forget it, so
    no test step is written for this field."""
    item["data_cy"] = "Unknown"
    item["preferred_locator"] = "Unknown"
    item["automation_locator"] = {
        "strategy": "Unknown", "value": "Unknown", "verified": False,
        "target": "Unknown", "fallbacks": [],
    }
    item["resolution_trace"] = trace


def _apply_found_navigation(page: dict, scan: dict) -> str:
    """Replaces the guessed navigation path with the real menu path the scan walked.
    Returns a short note for the log ("" when there was nothing to apply)."""
    found = scan.get("found_navigation") or {}
    module = str(found.get("module") or "").strip()
    path = [str(p).strip() for p in (found.get("path") or []) if str(p).strip()]
    if not module or not path:
        return ""
    names = [module] + path
    if len(names) not in (2, 3):
        return (f"the real menu path has {len(names)} levels ({' > '.join(names)}); the test steps "
                f"only support 2 or 3, so the navigation was left as it was.")
    old = _clean_path(page.get("navigation_path"))
    hops = []
    for i, name in enumerate(names):
        if i == 0:
            hop_type, dc = "module", f"{name}-Module"
        elif i == len(names) - 1:
            hop_type, dc = "menu_leaf", f"{name}-MenuData{len(names) - 2}"
        else:
            hop_type, dc = "menu_folder", f"{name}-MenuFolder{i - 1}"
        hops.append({
            "hop_label": name, "hop_type": hop_type, "data_cy": dc, "id": "Unknown",
            "preferred_locator": dc,
            "automation_locator": {"strategy": "data_cy", "value": dc, "verified": True,
                                   "target": "Unknown", "fallbacks": [], "source": "live app scan"},
            "source_evidence": [],
        })
    page["navigation_path"] = hops
    if [_norm(x) for x in old] == [_norm(x) for x in names] and old == names:
        return ""
    return f"navigation is {' > '.join(names)} (the analysis had guessed {' > '.join(old) or 'nothing'})."


def _page_labels(project_analysis: dict) -> list[str]:
    """The screen's title(s) from the analysis - help to find a similarly named menu item."""
    page = _first_page(project_analysis)
    out: list[str] = []
    for v in (page.get("page_name"), (page.get("forms") or [{}])[0].get("form_name") if page.get("forms") else None):
        v = str(v or "").strip()
        if v and v not in out:
            out.append(v)
    return out


def _field_group(field: dict, groups: dict) -> list[dict] | None:
    """The scanned elements that belong to this analysis field, or None."""
    loc = field.get("automation_locator")
    existing = str(loc.get("value")) if isinstance(loc, dict) and loc.get("value") not in (None, "", "Unknown") else ""
    if existing:
        grp = groups.get(_norm(_SUFFIX_RE.sub(r"\g<base>", existing)))
        if grp:
            return grp
    for candidate in (field.get("control_name"), field.get("field_key"), field.get("label")):
        if candidate and str(candidate) != "Unknown":
            grp = _find_group(_norm(candidate), groups)
            if grp:
                return grp
    return None


def _scan_fit(project_analysis: dict, scan: dict) -> tuple[int, int]:
    """(fields of the analysis found on the scanned page, fields in the analysis). Read-only:
    used to check that a page opened by a SIMILAR menu name really is this screen."""
    page = _first_page(project_analysis)
    groups = _group_by_base(scan.get("elements") or [])
    total = found = 0
    for field, _is_loop in _iter_fields(page):
        total += 1
        if _field_group(field, groups):
            found += 1
    return found, total


def _mark_fixed_fields(project_analysis: dict, page: dict, groups: dict) -> list[str]:
    """Fields that are filled in AND locked on the real page (for example Employee Name, which
    comes from the login and cannot be changed). They are marked read-only in the analysis and in
    the screen spec, so the tests neither pick, clear nor 'require' them."""
    spec = project_analysis.get("screen_spec") if isinstance(project_analysis, dict) else None
    spec_fields = [f for f in (spec.get("fields") or []) if isinstance(f, dict)] if isinstance(spec, dict) else []
    names: list[str] = []
    for field, _is_loop in _iter_fields(page):
        grp = _field_group(field, groups)
        if not grp or not any(e.get("locked") and str(e.get("value") or "").strip() for e in grp):
            continue
        name = str(field.get("control_name") or field.get("field_key") or field.get("label") or "?")
        field["readonly"] = True
        field["auto_filled"] = True
        for sf in spec_fields:
            if _norm(sf.get("name")) in (_norm(field.get("control_name")), _norm(field.get("field_key"))):
                sf["readonly"] = True
                sf["auto_filled"] = True
        names.append(name)
    return names


def merge_scan_into_analysis(project_analysis: dict, scan: dict) -> dict:
    """Writes real data-cy values from the scan into the analysis. Returns
    counts: confirmed / filled / corrected / still_unresolved (names) / extra."""
    page = _first_page(project_analysis)
    elements = scan.get("elements") or []
    groups = _group_by_base(elements)
    scanned_values = {str(e.get("data_cy")) for e in elements}
    used_groups: set[str] = set()
    stats = {"confirmed": 0, "filled": 0, "corrected": 0, "still_unresolved": [], "extra": 0,
             "not_on_page": [], "navigation": "", "fixed": []}
    stats["navigation"] = _apply_found_navigation(page, scan)
    missing_on_page: list[tuple[dict, str]] = []   # (field, name) the analysis claims but the page lacks

    for field, _is_loop in _iter_fields(page):
        existing = None
        loc = field.get("automation_locator")
        if isinstance(loc, dict) and loc.get("value") not in (None, "", "Unknown"):
            existing = str(loc.get("value"))
        elif field.get("data_cy") not in (None, "", "Unknown"):
            existing = str(field.get("data_cy"))
        verified = _field_is_verified(field)

        if verified and existing in scanned_values:
            stats["confirmed"] += 1
            used_groups.add(_norm(_SUFFIX_RE.sub(r"\g<base>", existing)))
            continue

        group = None
        matched_name = ""
        for candidate in (field.get("control_name"), field.get("field_key"), field.get("label")):
            if candidate and str(candidate) != "Unknown":
                group = _find_group(_norm(candidate), groups)
                if group:
                    matched_name = _norm(candidate)
                    break
        if not group:
            fname = str(field.get("control_name") or field.get("label") or field.get("field_key") or "?")
            if existing and existing not in scanned_values:
                missing_on_page.append((field, fname))     # claimed by the analysis, not on the page
            elif not verified:
                stats["still_unresolved"].append(fname)
            continue

        chosen = _pick_element(group)
        value = str(chosen["data_cy"])
        # this field accounts for its own group and, for grid rows, ItemCode0/ItemCode1/...
        used_groups.update(
            k for k in groups
            if k == matched_name or re.fullmatch(re.escape(matched_name) + r"\d+", k)
        )
        if verified and existing and existing != value:
            stats["corrected"] += 1
        elif not verified:
            stats["filled"] += 1
        else:
            stats["confirmed"] += 1
            continue
        _set_locator(field, value, "Resolved from the live app scan")

    # Fields the analysis was sure about but the page does not have (for example a control that
    # only exists in the source code). Only trusted when the page clearly held the form, i.e. at
    # least one field was found on it - otherwise the form may simply not have been open.
    if missing_on_page:
        if stats["confirmed"] + stats["filled"] + stats["corrected"] > 0:
            for field, fname in missing_on_page:
                _clear_locator(field, "Not found on the live page by the scan")
                stats["not_on_page"].append(fname)
        else:
            stats["still_unresolved"].extend(name for _f, name in missing_on_page)

    # fields the page fills in itself and does not let the user change (e.g. Employee Name from the login)
    stats["fixed"] = _mark_fixed_fields(project_analysis, page, groups)

    # buttons: Save / Delete / ... with an unverified locator
    for action in page.get("business_actions") or []:
        if not isinstance(action, dict) or _field_is_verified(action):
            continue
        name = _norm(action.get("action_name"))
        if not name:
            continue
        for el in elements:
            dc = str(el.get("data_cy") or "")
            if _SUFFIX_RE.match(dc):
                continue  # control-style value, not a plain button
            plain = re.sub(r"(form|button|btn)$", "", _norm(dc))
            if plain and plain == name:
                _set_locator(action, dc, "Resolved from the live app scan")
                used_groups.add(_norm(dc))
                stats["filled"] += 1
                break

    stats["extra"] = sum(
        1 for key, grp in groups.items()
        if key not in used_groups and any(e.get("interactive") and e.get("visible") for e in grp)
    )
    return stats


# --------------------------------------------------------------------------
# entry point used by app.py
# --------------------------------------------------------------------------
def _biz_type_field(project_analysis: dict) -> str:
    """The control name of the Biz Transaction Type picklist, if the analysis has it."""
    for field, _ in _iter_fields(_first_page(project_analysis)):
        name = str(field.get("control_name") or "")
        if _norm(name).startswith("biztransactiontype"):
            return name
    return "BizTransactionTypeName"


_FIT_GOOD = 0.5          # a similar-name page with at least this share of the screen's fields is accepted at once
_FIT_MIN = 0.25          # ... and the best of the tried pages is still accepted down to this share
_MAX_SIMILAR_PICKS = 3   # how many similar menu items are opened, at most, before giving up


async def scan_and_merge(project_analysis: dict, module: str, screen: str, log: LogFn,
                         is_mm: bool = False) -> None:
    """`log(text, tone)` must be an async callable. Exceptions raised by `log`
    (for example a closed WebSocket) are NOT swallowed."""
    global _scan_lock
    if not scan_enabled():
        return

    async def say(text: str, tone: str = "secondary") -> None:
        logger.info("%s / %s: %s", module, screen, text)   # also visible in the uvicorn terminal
        await log(text, tone)

    missing = _missing_settings()
    if missing:
        await say(f"data-cy scan skipped: set {', '.join(missing)} in .env to enable it.", "muted")
        return

    if _flag("SCAN_ONLY_WHEN_UNRESOLVED", "0") and not has_unresolved_fields(project_analysis):
        await say("data-cy scan not needed - every field already has a verified data-cy.", "muted")
        return

    nav = _clean_path(_first_page(project_analysis).get("navigation_path"))
    if len(nav) < 2:
        await say(f"data-cy scan skipped: navigation path {nav or 'is empty'} needs at least a module and a screen.", "muted")
        return

    key = _cache_key(module, screen, nav, is_mm)
    scan = _load_cache(key)
    if scan:
        await say(f"data-cy scan: using saved scan from {str(scan.get('scanned_at', ''))[:16]} "
                  f"({len(scan.get('elements') or [])} element(s)).", "secondary")
    else:
        if _fail_until.get(key, 0) > time.time():
            await say("data-cy scan skipped: it failed recently for this screen - retrying later.", "muted")
            return
        kind = "MM screen: will also pick the first Biz Transaction Type" if is_mm else "normal screen"
        await say(f"data-cy scan ({kind}): opening the real app at {' > '.join(nav)} "
                  f"(login + read data-cy, up to {_int('SCAN_TIMEOUT_SECONDS', 240)}s)...", "secondary")
        if _scan_lock is None:
            _scan_lock = asyncio.Lock()
        biz_field = _biz_type_field(project_analysis) if is_mm else ""
        open_cy = _open_form_selectors(project_analysis)
        labels = _page_labels(project_analysis)

        async def run(pick: int):
            return await asyncio.to_thread(_run_scan, module, screen, nav, is_mm, biz_field, open_cy, labels, pick)

        async with _scan_lock:
            scan, reason = await run(0)
            # No menu item had exactly this screen's name, so the closest-looking one was opened.
            # Check it really is this screen (its fields are on the page); if not, try the next one.
            best = None
            pick = 0
            while scan is not None and (scan.get("found_navigation") or {}).get("matched_by") == "similar":
                fn = scan["found_navigation"]
                found, total = _scan_fit(project_analysis, scan)
                ratio = (found / total) if total else 1.0
                if best is None or ratio > best["ratio"]:
                    best = {"ratio": ratio, "scan": scan, "name": fn.get("matched_name") or "?", "found": found, "total": total}
                if ratio >= _FIT_GOOD:
                    break
                alternatives = fn.get("alternatives") or []
                if pick + 1 >= min(len(alternatives), _MAX_SIMILAR_PICKS):
                    break                                   # nothing left to try
                pick += 1
                await say(f"data-cy scan: '{fn.get('matched_name')}' has only {found} of {total} of this screen's fields - "
                          f"trying the next closest menu item ('{alternatives[pick]}')...", "secondary")
                scan, reason = await run(pick)
            if best is not None:
                if best["ratio"] >= _FIT_MIN:
                    scan = best["scan"]
                    await say(f"data-cy scan: no menu item is named '{screen}', so the closest one was used - '{best['name']}' "
                              f"({best['found']} of {best['total']} fields found on that page). Please check it is the right screen.", "secondary")
                else:
                    scan = None
                    reason = (f"no menu item is named '{screen}' and the closest one ('{best['name']}') does not look like "
                              f"this screen (only {best['found']} of {best['total']} fields on its page)")
        if scan is None:
            cooldown = _int("SCAN_FAIL_COOLDOWN_MINUTES", 30)
            if cooldown > 0:
                _fail_until[key] = time.time() + cooldown * 60
            logger.warning("data-cy scan failed for %s / %s: %s", module, screen, reason)
            await say(f"data-cy scan skipped: {reason}. Generating without it.", "muted")
            return
        _save_cache(key, scan)

    stats = merge_scan_into_analysis(project_analysis, scan)
    parts = [f"{stats['confirmed']} confirmed", f"{stats['filled']} filled in", f"{stats['corrected']} corrected"]
    msg = f"data-cy scan: {len(scan.get('elements') or [])} element(s) read from the page - " + ", ".join(parts) + "."
    if stats["still_unresolved"]:
        shown = ", ".join(stats["still_unresolved"][:8])
        more = "" if len(stats["still_unresolved"]) <= 8 else f" (+{len(stats['still_unresolved']) - 8} more)"
        msg += f" Still unknown: {shown}{more}."
    if stats["not_on_page"]:
        msg += f" Not on the page (no test steps will be written): {', '.join(stats['not_on_page'][:8])}."
    await say(msg, "success" if not (stats["still_unresolved"] or stats["not_on_page"]) else "secondary")
    if stats["navigation"]:
        await say(f"data-cy scan: {stats['navigation']}", "secondary")
    if stats["fixed"]:
        verb = "is" if len(stats["fixed"]) == 1 else "are"
        await say(f"data-cy scan: {', '.join(stats['fixed'])} {verb} pre-filled on the page and cannot be changed "
                  f"(for example from the login) - the tests will not pick, clear or edit {'it' if verb == 'is' else 'them'}.", "secondary")
    if stats["extra"]:
        await say(f"data-cy scan: {stats['extra']} visible control(s) on the page were not in the analysis "
                  f"(not added to the tests).", "muted")