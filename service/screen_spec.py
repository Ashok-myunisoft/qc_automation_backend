"""Builds a "screen spec" for one screen by CODE (no AI):

  frontend : formjson  (type / required / max length / regex / dropdown options / picklist)
             intersected with the fields the screen's HTML really renders,
             real labels from gbhost i18n, the frontend's own validation messages,
             calculated / read-only fields (formjson + HTML + TS) and the date format the screen shows
  backend  : every file of the screen's backend entity, condensed into a small digest of the Save / Delete
             rules and messages (when the backend repository can be reached)

Everything here is best-effort: every public function returns None / {} on any problem and
the caller carries on with the old behaviour.

Optional settings (repo paths / a safety cap, all with defaults in code):
  I18N_PATH, SAVEURL_PATH   repo paths of the label file / the route map
  BACKEND_ENTITY_MAX_FILES  safety cap on fetched backend files (default 40)
"""
from __future__ import annotations

import json
import logging
import os
import re
import time

from service import calculated_fields

logger = logging.getLogger(__name__)

def _n(text) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


# --------------------------------------------------------------------------- tiny cache
_CACHE: dict[str, tuple[float, object]] = {}
_TTL = 3600.0


def _cached(key: str, loader):
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1]
    value = loader()
    if value is not None:
        _CACHE[key] = (time.time(), value)
    return value


# --------------------------------------------------------------------------- 1. form reference
_GB_RE = re.compile(
    r"""GBBaseFormGroup\s*\(\s*[^,()]+,\s*['"]([^'"]+)['"]\s*,\s*[^,()]+,\s*['"]([^'"]+)['"]""")
_FORMCALL_RE = re.compile(r"""form(?:save|load|delete)service\s*\(\s*['"]([^'"]+)['"]""", re.I)
_PROJECT_RE = re.compile(r"(?:^|/)projects/([^/]+)/")


_GBVAR_RE = re.compile(
    r"""GBBaseFormGroup\s*\(\s*[^,()]+,\s*([^,()]+?)\s*,\s*[^,()]+,\s*['"]([^'"]+)['"]""")


def form_candidates(source_files: list[dict], primary_dir: str) -> tuple[list[str], str]:
    """Possible formjson names, best first, and the module.
       new GBBaseFormGroup(this.injector, 'skilldomain', this.MenuId, 'skillmanagement')   -> ['skilldomain']
       new GBBaseFormGroup(this.injector, jsonname, this.MenuId, "ess")  with
       let jsonname = ... ? a : ... ? b : "leaverequest"                                   -> ['leaverequest']
       then the names used in formsaveservice('x') calls, then the screen folder name."""
    pm = _PROJECT_RE.search(primary_dir or "")
    module = pm.group(1) if pm else ""
    names: list[str] = []
    ts_files = [f for f in source_files or []
                if str(f.get("path", "")).endswith(".ts")
                and not str(f.get("path", "")).endswith((".spec.ts", ".module.ts"))
                and not str(f.get("path", "")).startswith("[backend]")]
    for f in ts_files:
        text = f.get("content") or ""
        m = _GBVAR_RE.search(text)
        if not m:
            continue
        arg, mod = m.group(1).strip(), m.group(2)
        module = mod or module
        if arg[:1] in "'\"":
            names.append(arg.strip("'\""))
        else:
            var = re.sub(r"^this\.", "", arg)
            d = re.search(r"(?:let|const|var)\s+%s\s*(?::[^=]+)?=([^;]*);" % re.escape(var), text, re.S)
            if d:
                names += [x for x in re.findall(r"""['"]([A-Za-z0-9_]+)['"]""", d.group(1)) if x.lower() != "none"]
    for f in ts_files:
        names += _FORMCALL_RE.findall(f.get("content") or "")
    folder = (primary_dir or "").rstrip("/").split("/")[-1]
    if folder:
        names.append(folder)
    seen, out = set(), []
    for n in names:
        if n and n.lower() not in seen:
            seen.add(n.lower())
            out.append(n)
    return out, module


def find_form_ref(source_files: list[dict], primary_dir: str) -> tuple[str, str] | None:
    names, module = form_candidates(source_files, primary_dir)
    return (names[0], module) if names else None


# --------------------------------------------------------------------------- 2. formjson
def _ci(d: dict, key: str, default=None):
    for k, v in d.items():
        if str(k).lower() == key.lower():
            return v
    return default


def _truthy(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes", "y")
    return bool(v)


def _to_int(v):
    try:
        i = int(str(v).strip())
        return i if i > 0 else None
    except (TypeError, ValueError):
        return None


def _locate_formjson(src, tree: list[str] | None, module: str, formname: str) -> str | None:
    wanted = f"projects/{module}/public/formjson/{formname}.json".lower()
    if tree:
        idx = {p.lower(): p for p in tree}
        if wanted in idx:
            return idx[wanted]
        tail = f"/public/formjson/{formname}.json".lower()
        for low, real in idx.items():          # module name differs: any module with that file
            if low.endswith(tail):
                return real
        return None
    return wanted if src.fetch_file(wanted) is not None else None


def load_formjson(src, tree: list[str] | None, module: str, formname: str) -> tuple[str, list[dict]] | None:
    path = _locate_formjson(src, tree, module, formname)
    if not path:
        return None
    text = src.fetch_file(path)
    if not text:
        return None
    try:
        data = json.loads(text.lstrip("\ufeff"))
    except json.JSONDecodeError:
        logger.warning("screen_spec: %s is not valid JSON", path)
        return None
    fields = data if isinstance(data, list) else (_ci(data, "ObjectFields") or [])
    fields = [f for f in fields if isinstance(f, dict) and _ci(f, "Name")]
    return (path, fields) if fields else None


def _parse_field(raw: dict) -> dict:
    options = []
    for p in _ci(raw, "PickLists") or []:
        if isinstance(p, dict):
            key, text = _ci(p, "key"), _ci(p, "value")
            options.append({"key": key, "text": "" if text is None else str(text),
                            "blank": key in ("", None)})
    return {
        "name": str(_ci(raw, "Name")),
        "label_key": str(_ci(raw, "Label") or ""),
        "type": str(_ci(raw, "Type") or "string").lower(),
        "required": _truthy(_ci(raw, "Required")),
        "max_length": _to_int(_ci(raw, "MaxLength")),
        "regex": str(_ci(raw, "ValidRegEx") or ""),
        "default": _ci(raw, "DefaultValue"),
        "placeholder": str(_ci(raw, "PlaceHolder") or _ci(raw, "Placeholder") or ""),
        "options": options,
        "picklist_json": str(_ci(raw, "PicklistJSON") or ""),
        "picklist_url": str(_ci(raw, "PicklistUrl") or ""),
        "key_field": _truthy(_ci(raw, "isKeyField")),
        "add_new": _truthy(_ci(raw, "AddNew")),
        "link_id": str(_ci(raw, "LinkId") or ""),
        "picklist_title": str(_ci(raw, "PicklistTitle") or ""),
        "readonly": (_truthy(_ci(raw, "ReadOnly")) or _truthy(_ci(raw, "IsReadOnly")) or _truthy(_ci(raw, "Disabled"))
                     or _truthy(_ci(raw, "IsDisabled")) or str(_ci(raw, "Type") or "").lower() == "readonly"),
        "grid": bool(_ci(raw, "Grid")),
        "post_data": _ci(raw, "PostData", True),
    }


# --------------------------------------------------------------------------- 3. rendered HTML
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_TAG_RE = re.compile(r"<([A-Za-z][\w-]*)((?:\s[^<>]*)?)>", re.S)
_FCN_RE = re.compile(r"""(?<![\w\[])formControlName\s*=\s*["']([\w.]+)["']""")
_DATACY_RE = re.compile(r"""(?<![\w\[])data-cy\s*=\s*["']([^"']+)["']""")


def _kind_from_tag(tag: str) -> str:
    t = tag.lower()
    for needle, kind in (("newpicklist", "picklist"), ("picklist", "picklist"), ("combobox", "combobox"),
                         ("checkbox", "checkbox"), ("textarea", "textarea"), ("radio", "radio"),
                         ("date", "date"), ("input", "input"), ("number", "input"), ("select", "combobox")):
        if needle in t:
            return kind
    return ""


def rendered_from_html(source_files: list[dict], primary_dir: str) -> dict:
    """{'controls': {name: tag}, 'buttons': [data-cy...], 'found': bool}  (HTML comments removed)"""
    controls: dict[str, str] = {}
    cy: list[str] = []
    html_parts: list[str] = []
    seen_html = False
    for f in source_files or []:
        path = str(f.get("path", ""))
        if not path.endswith(".html") or path.startswith("[backend]"):
            continue
        if primary_dir and not path.startswith(primary_dir + "/"):
            continue
        seen_html = True
        html = _COMMENT_RE.sub("", f.get("content") or "")
        html_parts.append(html)
        for tag, attrs in _TAG_RE.findall(html):
            m = _FCN_RE.search(attrs)
            if m:
                controls.setdefault(m.group(1).lower(), tag)
        for name in _FCN_RE.findall(html):
            controls.setdefault(name.lower(), "")
        for v in _DATACY_RE.findall(html):
            if v not in cy:
                cy.append(v)
    return {"controls": controls, "buttons": cy, "found": seen_html and bool(controls), "html": "\n".join(html_parts)}


# --------------------------------------------------------------------------- 4. labels (i18n)
_I18N_DEFAULT = "projects/gbhost/public/i18n/English.json"


def _load_i18n(src) -> dict:
    path = os.getenv("I18N_PATH", _I18N_DEFAULT)

    def load():
        text = src.fetch_file(path)
        if not text:
            return None
        try:
            return json.loads(text.lstrip("\ufeff"))
        except json.JSONDecodeError:
            return None

    return _cached("i18n:" + path, load) or {}


def translate(i18n: dict, key: str) -> str:
    """Same as Transloco: walk the dotted key; if it is not found the key itself is shown."""
    if not key or not key.strip():
        return ""
    node = i18n
    for part in key.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, dict):
            hit = next((k for k in node if k.strip().lower() == part.strip().lower()), None)
            if hit is None:
                return key
            node = node[hit]
        else:
            return key
    return node if isinstance(node, str) else key


# --------------------------------------------------------------------------- 5. fields
def _kind(f: dict, tag: str) -> str:
    if f["grid"]:
        return "grid"
    kind = _kind_from_tag(tag)
    if kind:
        return kind
    if f["options"]:
        return "combobox"
    if f["type"] in ("boolean", "bool", "checkbox"):
        return "checkbox"
    default = str(f["default"] or "").lower()
    if f["type"] in ("date", "datetime") or default.startswith("today"):
        return "date"
    if f["picklist_json"] or f["picklist_url"]:
        return "picklist"
    if re.search(r"(date|till|expiry|dob)$", f["name"], re.I) and f["type"] == "string" and not f["max_length"]:
        return "date"
    return "input"


def _mentioned(name: str, html: str) -> bool:
    return bool(re.search(r"(?<![\w])%s(?![\w])" % re.escape(name), html, re.I))


def _build_fields(raw_fields: list[dict], rendered: dict, i18n: dict, formname: str, module: str = "") -> list[dict]:
    out = []
    controls = rendered["controls"]
    html = rendered.get("html", "")
    # per-field bindings (formControlName) are the best evidence; else a field name written in the template
    by_name = bool(html) and not controls and sum(_mentioned(str(_ci(r, "Name")), html) for r in raw_fields) >= 2
    known = rendered["found"] or by_name
    for raw in raw_fields:
        f = _parse_field(raw)
        lname = f["name"].lower()
        text = translate(i18n, f["label_key"]) if f["label_key"].strip() else ""
        label = text.strip()
        ui = bool(label)
        if rendered["found"]:
            in_html = lname in controls
        elif by_name:
            in_html = _mentioned(f["name"], html)
        else:
            in_html = None
        candidate = in_html if known else ui
        tag = controls.get(lname, "")
        f["kind"] = _kind(f, tag)
        f["tag"] = tag
        f["label_text"] = label
        f["label"] = label.rstrip("*").strip() or f["name"]
        f["rendered"] = in_html
        f["candidate"] = bool(candidate) and bool(ui or in_html)
        # the frontend builds:  translate(Label) + " Should not be Empty"
        f["required_message"] = f"{label} Should not be Empty" if (f["required"] and label) else ""
        # a picklist that is not the screen's own key field looks up ANOTHER entity
        own = _n(f["picklist_json"]) == _n(formname) or f["key_field"]
        f["_own"] = own
        f["alternate_of"] = ""
        out.append(f)

    # A "record picker" opens an EXISTING record of this very screen (e.g. the leave request number): its link
    # id is the screen's own id (TLeaveId - many other fields start with TLeave), its column is a number / code,
    # and it cannot add new values.
    for f in out:
        stem = f["link_id"][:-2].lower() if f["link_id"].lower().endswith("id") else ""
        siblings = sum(1 for o in out if o is not f and stem and o["name"].lower().startswith(stem))
        f["record_picker"] = (f["kind"] == "picklist" and not f["key_field"] and not f["add_new"] and bool(stem)
                              and siblings >= 3 and bool(re.search(r"(number|code|no)$", f["picklist_title"], re.I)))
        f["lookup"] = (f["kind"] == "picklist" and not f.pop("_own") and not f["record_picker"]
                       and bool(f["picklist_json"] or f["picklist_url"]))

    # Fields that are alternates of each other (same link + same picklist column, e.g. the 5 "Employee Name"
    # picklists or LeaveTypeName / LeaveTypeNameEss): only ONE is on the page - which one depends on the
    # module and the user's role, so the live scan decides. Until it does, guess one.
    groups: dict[tuple, list[dict]] = {}
    for f in out:
        if f["candidate"] and f["link_id"] and f["kind"] == "picklist":
            groups.setdefault((f["link_id"].lower(), f["picklist_title"].lower()), []).append(f)
    for members in groups.values():
        if len(members) < 2:
            continue
        head = None
        if module.lower() == "ess":
            head = next((m for m in members if m["name"].lower().endswith("ess")), None)
        head = head or next((m for m in members if "erp" not in m["name"].lower()), members[0])
        for m in members:
            if m is not head:
                m["alternate_of"] = head["name"]
    for f in out:
        f["active"] = bool(f["candidate"]) and not f["alternate_of"]
    return out


# --------------------------------------------------------------------------- 5b. messages written in the component
_LIT = r"""(['"`])((?:(?!\1)[^\n]){1,160}?)\1"""
_REQ_RE = re.compile(r"""(['"`])((?:(?!\1)[^\n]){1,100}?)\s+Should not be Empty\1""", re.I)
_DIALOG_RE = re.compile(r"""\bmessage\s*[:=]\s*(['"`])((?:(?!\1)[^\n]){6,300}?)\1""")


def component_messages(source_files: list[dict], primary_dir: str) -> dict:
    """The validation texts the screen's own TypeScript shows, e.g. alertdata.push('Reason* Should not be Empty')
    or dialog.open(... message: 'From Date must be earlier ...').  They are the real messages."""
    required: dict[str, str] = {}
    dialogs: list[dict] = []
    for f in source_files or []:
        path = str(f.get("path", ""))
        if not path.endswith(".ts") or path.endswith((".spec.ts", ".module.ts")) or path.startswith("[backend]"):
            continue
        if primary_dir and not path.startswith(primary_dir + "/"):
            continue
        text = f.get("content") or ""
        for _q, label in _REQ_RE.findall(text):
            var = re.fullmatch(r"\$\{(\w+)\}", label.strip())
            if var:    # `${employeeFieldDisplayName} Should not be Empty`
                d = re.search(r"(?:let|const|var)\s+%s\s*=\s*['\"]([^'\"]+)['\"]" % re.escape(var.group(1)), text)
                if not d:
                    continue
                label = d.group(1)
            required.setdefault(_n(label), f"{label.strip()} Should not be Empty")
        for _q, msg in _DIALOG_RE.findall(text):
            if re.search(r"should not be empty", msg, re.I):
                continue
            fragment = msg.split("${")[0].strip()
            if len(fragment) >= 8 and fragment not in [d["fragment"] for d in dialogs]:
                dialogs.append({"message": msg, "fragment": fragment})
    return {"required": required, "dialogs": dialogs}


# --------------------------------------------------------------------------- 5c. the live scan decides
def sync_spec_with_analysis(spec: dict, project_analysis: dict) -> dict:
    """After the data-cy scan: a field is on the page when the scan resolved it. Only applied when the scan
    clearly worked (it resolved at least one field), exactly like the scan's own "not on the page" rule."""
    fields = []
    try:
        for module in project_analysis.get("modules") or []:
            for page in module.get("pages") or []:
                for form in page.get("forms") or []:
                    fields += [x for x in form.get("fields") or [] if isinstance(x, dict)]
    except Exception:
        return {"scan_worked": False}
    by_name = {_n(x.get("control_name") or x.get("field_key")): x for x in fields}

    def resolved(x: dict) -> bool:
        loc = x.get("automation_locator")
        return isinstance(loc, dict) and loc.get("source") == "live app scan" and bool(loc.get("verified"))

    scan_worked = any(resolved(x) for x in fields)
    stats = {"scan_worked": scan_worked, "on_page": [], "not_on_page": []}
    if not scan_worked:
        return stats
    for f in spec["fields"]:
        if not f.get("candidate"):
            continue
        a = by_name.get(_n(f["name"]))
        if a is None:
            continue
        f["active"] = resolved(a)
        f["alternate_of"] = "" if f["active"] else f.get("alternate_of", "")
        (stats["on_page"] if f["active"] else stats["not_on_page"]).append(f["name"])
    spec["messages"]["required"] = [f["required_message"] for f in spec["fields"] if f["active"] and f["required_message"]] \
        + [m for m in spec["messages"].get("required", []) if m not in
           [f["required_message"] for f in spec["fields"] if f["required_message"]]]
    spec["scan_checked"] = True
    return stats


# --------------------------------------------------------------------------- 6. backend (entity)
_SAVEURL_DEFAULT = "projects/gbhost/public/formactionurls/saveurl.ts"
_SAVEURL_RE = re.compile(r"""public\s+static\s+(\w+)\s*=\s*['"]([^'"]*)['"]""")
_SERVICE_KEY_RE = re.compile(
    r"""['"]([A-Z][A-Za-z0-9]*(?:\.[A-Z][A-Za-z0-9]*)+)\.(?:Save|Get|GetList|List|Delete|Update)['"]""")


def _saveurl_map(src) -> dict[str, str]:
    path = os.getenv("SAVEURL_PATH", _SAVEURL_DEFAULT)

    def load():
        text = src.fetch_file(path)
        if not text:
            return None
        return {m.group(1).lower(): m.group(2).strip() for m in _SAVEURL_RE.finditer(text)}

    return _cached("saveurl:" + path, load) or {}


def entity_from_route(route: str) -> str:
    """'/prs/Leave.svc/' -> Leave   '/fms/AssetType/SaveAssetType/' -> AssetType
       '/WorkflowAssignment/SaveWorkflowAssignment' -> WorkflowAssignment   '/MM/SaveHeatMelting' -> HeatMelting"""
    parts = [p.strip() for p in (route or "").split("/") if p.strip()]
    for p in parts:
        if p.lower().endswith(".svc"):
            return p[:-4].strip()
    for p in parts:
        if p.startswith("Save") and len(p) > 4:
            return p[4:]
    if len(parts) >= 2:
        return parts[1]
    return parts[0] if parts else ""


def entity_candidates(source_files: list[dict], formname: str, saveurl: dict[str, str]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    route = saveurl.get(formname.lower())
    if route:
        ent = entity_from_route(route)
        if ent:
            out.append((ent, "saveurl"))
    for f in source_files or []:
        if str(f.get("path", "")).endswith(".ts"):
            for m in _SERVICE_KEY_RE.finditer(f.get("content") or ""):
                ent = m.group(1).replace(".", "")
                if (ent, "service url keys") not in out:
                    out.append((ent, "service url keys"))
    out.append((formname, "form name"))
    seen, uniq = set(), []
    for ent, how in out:
        if _n(ent) not in seen:
            seen.add(_n(ent))
            uniq.append((ent, how))
    return uniq


def find_entity_files(backend_tree: list[str], entity: str, fe_module: str = "") -> dict | None:
    """All .cs files of one entity: SL/EndPoints/<Entity>/*, BLL/<Entity>/*, DAL/**/<Entity>/*.
    No suffix filter and no cap of 5 - the folder is what defines 'the screen's files'."""
    want = _n(entity)
    by_module: dict[str, dict[str, list[str]]] = {}
    for path in backend_tree or []:
        if not path.lower().endswith(".cs"):
            continue
        seg = path.split("/")
        if len(seg) < 5 or seg[0].lower() != "gb5solution":
            continue
        module, layer_dir = seg[1], seg[2].lower()
        dirs = [_n(s) for s in seg[3:-1]]
        layer = ""
        if layer_dir.endswith("sl"):
            if len(seg) > 5 and _n(seg[3]) == "endpoints" and _n(seg[4]) == want:
                layer = "sl"
        elif layer_dir.endswith("bll"):
            if want in dirs:
                layer = "bll"
        elif layer_dir.endswith("dal"):
            if want in dirs:
                layer = "dal"
        if layer:
            by_module.setdefault(module, {}).setdefault(layer, []).append(path)
    by_module = {m: l for m, l in by_module.items() if "sl" in l}   # must have real endpoints
    if not by_module:
        return None
    mods = sorted(by_module)
    pick = next((m for m in mods if fe_module and (_n(m) in _n(fe_module) or _n(fe_module) in _n(m))), mods[0])
    layers = {k: sorted(set(v)) for k, v in by_module[pick].items()}
    return {"entity": entity, "module": pick, "layers": layers, "other_modules": [m for m in mods if m != pick]}


# ---- C# reading helpers
def _method_bodies(src_text: str, name_re: str) -> list[tuple[str, str, str]]:
    """[(method name, 'Type var' of first parameter, body)] for methods whose name matches name_re."""
    out = []
    sig = re.compile(r"\b(?:public|private|internal|protected)\s+(?:async\s+)?[\w<>\[\],.? ]+?\s+(%s)\s*\(([^)]*)\)" % name_re)
    for m in sig.finditer(src_text):
        i = src_text.find("{", m.end())
        j = src_text.find(";", m.end())
        if i < 0 or (0 <= j < i):           # expression-bodied or abstract: skip
            continue
        depth, k = 0, i
        while k < len(src_text):
            c = src_text[k]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        out.append((m.group(1), m.group(2).strip(), src_text[i:k + 1]))
    return out


def _first_param_var(params: str) -> str:
    m = re.match(r"\s*[\w<>\[\].?]+\s+(\w+)", params or "")
    return m.group(1) if m else "dto"


def _clean_literal(lit: str) -> str:
    lit = lit.strip()
    if lit.startswith("$"):
        lit = lit[1:]
    return lit[1:-1] if len(lit) >= 2 and lit[0] == '"' and lit[-1] == '"' else lit


def digest_entity(files: dict[str, str], messages) -> dict:
    """files: {path: C# text} -> small digest of Save/Delete rules. `messages` = ResponseMessages or None."""
    required, other_checks, server_set, auto_numbers, rules = [], {}, [], [], []
    seen_rules: set[str] = set()
    routes = []
    refs: dict[tuple[str, str], list[str]] = {}
    uses_handle_exception = False

    for path, text in files.items():
        low = path.lower()
        if "/endpoints/" in low:
            for m in re.finditer(r"\b(Post|Get|Put|Delete|Patch)\(\s*\"([^\"]+)\"", text):
                routes.append({"method": m.group(1).upper(), "path": m.group(2)})
        if "handleexception" in text.lower():
            uses_handle_exception = True
        for cls, prop, args in re.findall(
                r"string\.Format\(\s*(SuccessResponse|ErrorResponse)\.(\w+)\s*,\s*([^;]*?)\)\s*;", text, re.S):
            refs[(cls, prop)] = re.findall(r"\w+\.(\w+)", args)
        for cls, prop in re.findall(r"\b(SuccessResponse|ErrorResponse)\.(\w+)", text):
            refs.setdefault((cls, prop), [])

    for path, text in files.items():
        if "bll/" not in path.lower().replace("\\", "/") and "bll\\" not in path.lower():
            continue
        for _name, params, body in _method_bodies(text, r"(?:Save|Insert|Update|Delete)\w*"):
            var = _first_param_var(params)
            for fld in re.findall(r"NotEmpty\(\s*\w+\.(\w+)", body):
                if fld not in required:
                    required.append(fld)
            for vname, fld in re.findall(r"_validation\.(\w+)\(\s*\w+\.(\w+)", body):
                if vname != "NotEmpty":
                    other_checks.setdefault(vname, [])
                    if fld not in other_checks[vname]:
                        other_checks[vname].append(fld)
            for fld in re.findall(r"\b%s\.(\w+)\s*=(?!=)" % re.escape(var), body):
                if fld not in server_set:
                    server_set.append(fld)
            for key in re.findall(r"GetNumberAsync\(\s*[^,]+,\s*([^,)]+)", body):
                key = key.strip().strip('"')
                if key not in auto_numbers:
                    auto_numbers.append(key)
            for m in re.finditer(r"throw\s+new\s+\w+\s*\(\s*(\$?\"(?:[^\"\\]|\\.)*\")", body):
                msg = _clean_literal(m.group(1))
                before = body[max(0, m.start() - 240):m.start()]
                conds = re.findall(r"\bif\s*\((.*?)\)\s*\{?\s*$", before.strip(), re.S)
                cond = re.sub(r"\s+", " ", conds[-1]).strip() if conds else ""
                if msg not in seen_rules:
                    seen_rules.add(msg)
                    # a message with {placeholders} is matched by its fixed part only (the text before the first {)
                    fragment = msg.split("{")[0].strip()
                    rules.append({"message": fragment, "template": msg, "condition": cond})

    msgs: dict[str, dict] = {}
    for (cls, prop), args in refs.items():
        role = ""
        if cls == "SuccessResponse" and prop.startswith("Save"):
            role = "save"
        elif cls == "SuccessResponse" and prop.startswith("Update"):
            role = "update"
        elif cls == "SuccessResponse" and prop.startswith("Delete"):
            role = "delete"
        elif cls == "ErrorResponse" and "NotFound" in prop and prop.startswith("Delete"):
            role = "delete_not_found"
        elif cls == "ErrorResponse" and "NotFound" in prop and prop.startswith("Update"):
            role = "update_not_found"
        if not role or role in msgs:
            continue
        template = messages.text(cls, prop) if messages else None
        if template is None:
            continue
        prefix = template.split("{")[0].strip()
        msgs[role] = {"source": f"{cls}.{prop}", "template": template, "fields": args, "fragment": prefix or template}

    digest = {
        "routes": routes,
        "required_fields": required,
        "other_checks": other_checks,
        "server_set_fields": server_set,
        "auto_number_keys": auto_numbers,
        "rules": rules,
        "messages": msgs,
    }
    if uses_handle_exception:
        # HandleException returns the raw SQL Server text for SQL errors (the "context message" is only logged)
        digest["sql_error_fragments"] = {
            "duplicate": "Cannot insert duplicate key",
            "in_use": "conflicted with the REFERENCE constraint",
            "verified": False,
        }
    return digest


def _load_response_messages(backend, backend_tree: list[str]):
    from service.resx_messages import ResponseMessages

    def load():
        idx = {p.lower(): p for p in backend_tree}
        found = {}
        for cls in ("SuccessResponse", "ErrorResponse"):
            resx = next((real for low, real in idx.items() if low.endswith(f"/resource/response/{cls.lower()}.resx")), None)
            if not resx:
                continue
            designer = idx.get(resx.lower()[:-5] + ".designer.cs")
            found[cls] = (backend.fetch_file(resx), backend.fetch_file(designer) if designer else None)
        return ResponseMessages.from_sources(found) if found else None

    return _cached("resp:" + str(len(backend_tree)), load)


def build_backend_digest(backend, entity_list: list[tuple[str, str]], fe_module: str) -> dict | None:
    def load_tree():
        return backend.get_repo_tree()

    tree = load_tree()
    if not tree:
        return None
    for entity, how in entity_list:
        found = find_entity_files(tree, entity, fe_module)
        if not found:
            continue
        key = f"digest:{found['module']}:{_n(entity)}"
        hit = _CACHE.get(key)
        if hit and time.time() - hit[0] < _TTL:
            return hit[1]
        cap = int(os.getenv("BACKEND_ENTITY_MAX_FILES", "40"))
        # the query builder (QB) and DTO files are large and carry no rules we use
        wanted = [p for layer in ("sl", "bll", "dal") for p in found["layers"].get(layer, [])
                  if "/query/" not in p.lower() and "/dto/" not in p.lower()][:cap]
        texts = {}
        for p in wanted:
            t = backend.fetch_file(p)
            if t:
                texts[p] = t
        if not texts:
            continue
        messages = _load_response_messages(backend, tree)
        digest = digest_entity(texts, messages)
        digest.update({"entity": entity, "found_by": how, "module": found["module"],
                       "files": sorted(texts), "other_modules_with_same_entity": found["other_modules"]})
        _CACHE[key] = (time.time(), digest)
        return digest
    return None


# --------------------------------------------------------------------------- 7. public builder
def build_screen_spec(src, resolved_dir: str, source_files: list[dict], source_tree: list[str] | None,
                      backend=None) -> dict | None:
    """Returns the spec dict, or None when the screen has no formjson (the caller keeps today's path)."""
    names, module = form_candidates(source_files, resolved_dir)
    loaded, formname = None, ""
    for name in names:
        loaded = load_formjson(src, source_tree, module, name)
        if loaded:
            formname = name
            break
    if not loaded:
        return None
    path, raw_fields = loaded
    rendered = rendered_from_html(source_files, resolved_dir)
    i18n = _load_i18n(src)
    fields = _build_fields(raw_fields, rendered, i18n, formname, module)
    calc_info = {"calculated": [], "date_format": "", "date_format_source": ""}
    try:
        calc_info = calculated_fields.apply(fields, rendered, source_files, resolved_dir)
    except Exception:
        logger.exception("screen_spec: calculated-field detection failed - continuing without it")
    msgs = component_messages(source_files, resolved_dir)

    # the screen's own code is the truth about what is required and what the message says
    by_label = dict(msgs["required"])
    for f in fields:
        hit = by_label.get(_n(f["label"]))
        if hit and f["label"] != f["name"]:
            f["required"] = True
            f["required_message"] = hit

    spec = {
        "formname": formname,
        "module": module,
        "formjson_path": path,
        "fields": fields,
        "buttons": rendered["buttons"],
        "rendered_html_found": bool(rendered["found"]),
        "not_rendered": [f["name"] for f in fields if f["rendered"] is False],
        "labels_resolved": bool(i18n),
        "calculated_fields": calc_info.get("calculated") or [],
        "date_format": calc_info.get("date_format") or "",
        "date_format_source": calc_info.get("date_format_source") or "",
        "messages_by_label": by_label,
        "messages": {
            "required": list(dict.fromkeys([f["required_message"] for f in fields if f["active"] and f["required_message"]]
                                           + list(by_label.values()))),
            "frontend": [d["fragment"] for d in msgs["dialogs"]],
        },
    }

    if backend is not None:
        try:
            entities = entity_candidates(source_files, formname, _saveurl_map(src))
            digest = build_backend_digest(backend, entities, module)
            if digest:
                spec["backend"] = digest
                for role, m in digest.get("messages", {}).items():
                    spec["messages"].setdefault(role, []).append(m["fragment"])
                for r in digest.get("rules", []):
                    spec["messages"].setdefault("backend_rules", []).append(r["message"])
                if digest.get("sql_error_fragments"):
                    sef = digest["sql_error_fragments"]
                    spec["messages"].setdefault("duplicate", []).append(sef["duplicate"])
                    spec["messages"].setdefault("in_use", []).append(sef["in_use"])
        except Exception:
            logger.exception("screen_spec: backend digest failed - continuing with the frontend spec only")
    return spec


# --------------------------------------------------------------------------- 8. text for the agents
def compact(spec: dict) -> dict:
    """Smaller copy for the analysis agent."""
    out = {k: v for k, v in spec.items() if k not in ("fields",)}
    out["fields"] = [
        {k: f[k] for k in ("name", "label", "kind", "type", "required", "max_length", "regex", "active", "candidate",
                           "alternate_of", "readonly", "record_picker", "rendered", "lookup", "required_message",
                           "calculated", "computed_from", "calculated_rule", "calculated_origin") if k in f}
        | ({"options": [o["text"] for o in f["options"] if not o["blank"]]} if f.get("options") else {})
        for f in spec["fields"]
    ]
    return out


def rules_for(kind: str) -> str:
    """Extra instructions for an agent when a spec exists. kind = 'ANALYSIS' or 'TEST_CASES'."""
    try:
        from pathlib import Path
        text = (Path(__file__).resolve().parent.parent / "prompts" / "screen_spec_rules.txt").read_text(encoding="utf-8")
        m = re.search(r"### %s\s*\n(.*?)(?=\n### |\Z)" % re.escape(kind.upper()), text, re.S)
        return m.group(1).strip() if m else ""
    except Exception:
        return ""


def summary_line(spec: dict) -> str:
    act = [f for f in spec["fields"] if f["active"]]
    alt = [f for f in spec["fields"] if f.get("alternate_of")]
    s = f"screen spec: formjson '{spec['formname']}' - {len(spec['fields'])} field(s), {len(act)} active"
    if alt:
        s += f" ({len(alt)} alternate field(s) - only one is shown per role/module, the live scan decides)"
    if spec.get("not_rendered"):
        s += f", {len(spec['not_rendered'])} not on the page"
    if spec.get("calculated_fields"):
        s += f", {len(spec['calculated_fields'])} calculated/read-only ({', '.join(c['name'] for c in spec['calculated_fields'][:5])})"
    if spec.get("date_format"):
        s += f", date format {spec['date_format']}"
    b = spec.get("backend")
    if b:
        s += f"; backend entity {b['entity']} ({b['module']}, {len(b['files'])} file(s), via {b['found_by']})"
    return s


def coverage_report(feature_text: str, spec: dict) -> dict:
    """Which active fields never appear in the feature (as an Examples column or a quoted field name)."""
    text = feature_text or ""
    low = text.lower()
    missing, used = [], []
    for f in spec["fields"]:
        if not f["active"]:
            continue
        names = {f["label"].lower(), f["name"].lower(), f["label_text"].lower()}
        names.discard("")
        if any(n in low for n in names):
            used.append(f["name"])
        else:
            missing.append(f["name"])
    total = len(used) + len(missing)
    return {"used": used, "missing": missing, "total": total, "share": (len(used) / total) if total else 1.0}