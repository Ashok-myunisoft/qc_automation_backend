"""Realistic, unique test values for the scenario matrix (service/scenario_matrix.py). Pure code, no AI call
(an optional AI call can hand in better words: service/matrix_llm.py).

Rules the values obey:
  * every code / name used by a Create row is different from every other one in the file, and carries a short
    per-run tag, so re-running the suite on the same database does not hit "already exists";
  * every value fits the field's MaxLength from the formjson and never contains a double quote or a pipe;
  * dropdown / radio values are the visible option text, checkbox values are Checked / Unchecked;
  * no PLACEHOLDER_ text unless a lookup field has no real data at all.
"""
from __future__ import annotations

import re
import time

_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"

NAME_BANKS = {
    "skill": ["Machining Operations", "Welding and Fabrication", "Quality Inspection", "Preventive Maintenance",
              "Assembly Line Skills", "Safety and Compliance", "CNC Programming", "Electrical Wiring",
              "Hydraulics and Pneumatics", "Packaging Operations", "Material Handling", "Lean Manufacturing",
              "Tool Room Practices", "Production Planning", "Supply Chain Basics", "Equipment Calibration",
              "Surface Finishing", "Casting and Forging", "Process Documentation", "Shop Floor Leadership",
              "Root Cause Analysis", "Warehouse Operations", "Industrial Automation", "Energy Management"],
    "generic": ["Northern Region", "Prime Supplies", "Global Logistics", "Metro Services", "Summit Industries",
                "Bluewave Trading", "Evergreen Foods", "Crestview Retail", "Pioneer Engineering", "Harbor Freight",
                "Orchid Healthcare", "Silverline Textiles", "Apex Components", "Riverside Traders", "Unity Hardware",
                "Lakeside Packaging", "Redwood Furnishings", "Sunrise Pharma", "Granite Works", "Maple Stationery",
                "Delta Fabricators", "Vertex Electricals", "Horizon Plastics", "Cedar Agro"],
}
DESC_BANKS = {
    "skill": ["Covers CNC machining, tool setting and first-piece inspection for the shop floor.",
              "Groups the safety, compliance and audit skills required for every production shift.",
              "Skills needed to inspect, measure and release finished goods against drawings.",
              "Planned and breakdown maintenance skills for rotating and electrical equipment.",
              "Skills used on the assembly line, including torque control and fixture handling.",
              "Welding, cutting and fabrication skills with their certification requirements.",
              "Material movement, storage and issue skills for stores and line feeding.",
              "Continuous improvement skills such as 5S, kaizen and root cause analysis."],
    "generic": ["Standard record maintained by the operations team and reviewed every month.",
                "Created for regression testing of the save, update and delete behaviour.",
                "Used by the finance team for grouping and reporting of similar entries.",
                "Applies to all active branches and is reviewed at the start of each quarter.",
                "Reference entry that is shared between purchase, stores and production teams.",
                "Covers the regular day to day activities handled by the department.",
                "Maintained by the administrator and visible to all authorised users.",
                "Approved by the department head and effective from the current period."],
}
_SPECIAL_NAMES = ["Safety & Compliance - Level 1", "Quality (QA/QC) Checks", "R&D Prototype #2", "Night-Shift / Weekend Ops"]
_SPECIAL_DESC = ["Covers tools (CNC/VMC), safety & PPE - 100% mandatory; review monthly.",
                 "Includes items A/B/C: pre-start check, in-process check & final sign-off."]


def _n(text) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def clean(value) -> str:
    """No double quotes, pipes, angle brackets or line breaks in an Examples cell."""
    return re.sub(r"[\"|<>\r\n]+", " ", str(value)).strip()


def run_tag() -> tuple[str, str]:
    """(letters, digits) that change every minute: 'K7', '047'."""
    minute = int(time.time() // 60)
    n = minute % 1296
    return (_B36[n // 36] + _B36[n % 36]).upper(), f"{minute % 1000:03d}"


def _fit(text: str, mx: int | None) -> str:
    if not mx or len(text) <= mx:
        return text
    return text[:mx].rstrip()


def _kind_of(f: dict) -> str:
    s = f"{f.get('name', '')} {f.get('label', '')}".lower()
    if f.get("kind") == "textarea" or re.search(r"desc|remark|note|comment", s):
        return "description"
    if re.search(r"e-?mail", s):
        return "email"
    if re.search(r"phone|mobile|contact\s*no", s):
        return "phone"
    if re.search(r"code|number|\bno\b", s):
        return "code"
    if re.search(r"name|title", s):
        return "name"
    return "text"


class ValueFactory:
    def __init__(self, spec: dict, business_context: dict | None = None, llm_values: dict | None = None):
        self.spec = spec
        self.ctx = business_context if isinstance(business_context, dict) else {}
        self.llm = {k: list(v) for k, v in (llm_values or {}).items() if isinstance(v, list)}
        self.tag, self.digits = run_tag()
        screen = spec.get("screen_name") or spec.get("formname") or "Item"
        screen = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", str(screen))
        self.screen = re.sub(r"\s+", " ", re.sub(r"[^A-Za-z0-9 ]+", " ", screen)).strip() or "Item"
        initials = "".join(w[0] for w in re.findall(r"[A-Za-z]+", self.screen)).upper()
        self.prefix = (initials if len(initials) >= 2 else (self.screen[:2].upper() or "QA"))[:3]
        low = (self.screen + " " + str(spec.get("formname", ""))).lower()
        self.bank = "skill" if re.search(r"skill|training|competen|domain", low) else "generic"
        self._seq: dict[str, int] = {}
        self._bank_i: dict[str, int] = {}
        self.used: set[str] = set()

    # ------------------------------------------------------------------ helpers
    def _next(self, key: str) -> int:
        self._seq[key] = self._seq.get(key, 0) + 1
        return self._seq[key]

    def is_unique_field(self, f: dict) -> bool:
        return bool(f.get("key_field")) or _kind_of(f) == "code"

    def _unique(self, value: str, f: dict) -> str:
        """A value that was handed out before gets a number so two rows never save the same text."""
        mx = f.get("max_length")
        base, i = value, 1
        while _n(value) in self.used:
            i += 1
            suffix = f" {i}" if _kind_of(f) != "code" else str(i)
            value = _fit(base, (mx - len(suffix)) if mx else None) + suffix
        self.used.add(_n(value))
        return value

    _MODIFIERS = ("", "Advanced ", "Basic ", "Senior ", "Core ", "Applied ")

    def _from_bank(self, key: str, bank: list[str], modify: bool = False) -> str:
        i = self._bank_i.get(key, 0)
        self._bank_i[key] = i + 1
        item = bank[i % len(bank)]
        if modify:
            item = self._MODIFIERS[(i // len(bank)) % len(self._MODIFIERS)] + item
        return item

    # ------------------------------------------------------------------ code / name / description
    def code(self, f: dict, style: str = "valid") -> str:
        mx = f.get("max_length")
        seq = self._next("code:" + f["name"])
        if style == "digits":
            raw = f"{self.digits}{seq:03d}"
        elif style == "mixed":
            raw = f"{self.prefix.title()}{self.tag.lower()}{seq:02d}"
        elif style == "short":
            raw = f"{self.tag[0]}{seq % 10}"
        elif style == "max":
            head = f"{self.prefix}{self.tag}"
            if mx and mx > len(head) + 4:
                filler = ("ABCDEFGHJKLMNPQRSTUVWXYZ0123456789" * (mx // 30 + 2))[: mx - len(head) - 3]
                raw = head + filler + f"{seq:03d}"
            elif mx:
                raw = (head + str(seq).zfill(mx))[-mx:] if len(head) + 2 > mx else head + str(seq).zfill(mx - len(head))
            else:
                raw = head + f"{seq:04d}"
        else:
            raw = f"{self.prefix}{self.tag}{seq:02d}"
        if mx and len(raw) > mx:
            raw = raw[-mx:]
        return self._unique(clean(raw), f) if style != "max" else clean(raw)

    def name(self, f: dict, style: str = "valid") -> str:
        mx = f.get("max_length")
        if style == "special":
            return self._unique(_fit(clean(self._from_bank("special:" + f["name"], _SPECIAL_NAMES)) + f" {self.digits[-2:]}", mx), f)
        if style == "short":
            return self._unique(clean(f"Q{self.tag}"[:max(3, 1)]), f)
        if style == "max":
            if not mx:
                return self.name(f)
            seq = self._next("maxname:" + f["name"])
            core = ((self._from_bank("maxw", NAME_BANKS[self.bank]) + " Programme ") * 12)[:max(mx - 5, 1)]
            if core.endswith(" "):
                core = core[:-1] + "x"
            return clean((core + f" {self.digits[-1]}{seq:03d}")[:mx])
        llm = self.llm.get(f["name"])
        if llm:
            base = clean(llm.pop(0))
        else:
            base = self._from_bank("name:" + f["name"], NAME_BANKS[self.bank], modify=True)
        suffix = f" {self.digits[-2:]}"
        return self._unique(_fit(base, (mx - len(suffix)) if mx else None) + suffix, f)

    def description(self, f: dict, style: str = "valid") -> str:
        mx = f.get("max_length")
        if style == "special":
            return clean(_fit(self._from_bank("sdesc:" + f["name"], _SPECIAL_DESC), mx))
        if style == "short":
            return "A"
        if style == "max":
            if not mx:
                return self.description(f)
            seq = self._next("maxdesc:" + f["name"])
            text = (" ".join(DESC_BANKS[self.bank]) + " ") * (mx // 40 + 3)
            text = text[:mx - 4].rstrip()
            return clean((text + f" #{seq:02d}")[:mx]).ljust(mx, "x")[:mx]
        llm = self.llm.get(f["name"])
        text = clean(llm.pop(0)) if llm else self._from_bank("desc:" + f["name"], DESC_BANKS[self.bank])
        return _fit(text, mx)

    # ------------------------------------------------------------------ everything else
    def _ctx_value(self, f: dict):
        wanted = {_n(f["name"]), _n(f.get("label", ""))}
        wanted.discard("")
        for k, v in self.ctx.items():
            nk = _n(k)
            if nk and any(nk == w or nk in w or w in nk for w in wanted) and v not in (None, "", [], {}):
                return str(v if not isinstance(v, (list, tuple)) else v[0])
        return None

    def text(self, f: dict, style: str = "valid") -> str:
        kind = _kind_of(f)
        mx = f.get("max_length")
        if kind == "code":
            return self.code(f, style)
        if kind == "name":
            return self.name(f, style)
        if kind == "description":
            return self.description(f, style)
        if kind == "email":
            return _fit(f"qa.user{self._next('mail'):02d}.{self.digits}@example.com", mx)
        if kind == "phone":
            return _fit(f"98{self.digits}{self._next('ph'):03d}0"[:10], mx)
        label = re.sub(r"[^A-Za-z0-9 ]+", " ", f.get("label") or f["name"]).strip()
        if style == "max" and mx:
            return clean((f"{label} entry " * (mx // 8 + 2))[:mx]).rstrip().ljust(mx, "x")[:mx]
        if style == "special":
            return clean(_fit(f"{label} A&B/{self._next('sp'):02d}", mx))
        if style == "short":
            return "A"
        return clean(_fit(f"{label} {self.tag}{self._next('t:' + f['name']):02d}", mx))

    def number(self, f: dict, style: str = "valid") -> str:
        mx = f.get("max_length")
        if style == "max":
            return ("9" * mx) if mx else "99999"
        if style == "short":
            return "1"
        s = f"{f['name']} {f.get('label', '')}".lower()
        if re.search(r"percent|%|rate|tax|discount", s):
            val = "12"
        elif re.search(r"price|amount|cost|value", s):
            val = "1250"
        elif re.search(r"qty|quantity|count|days|hours", s):
            val = "10"
        else:
            val = "25"
        return val if (not mx or len(val) <= mx) else "5"

    def valid(self, f: dict, style: str = "valid") -> str:
        """Value for a field in a fully valid row. style: valid | short | max | special | digits | mixed."""
        kind = f.get("kind")
        opts = [o["text"] for o in f.get("options", []) if not o.get("blank") and str(o.get("text", "")).strip()]
        if kind in ("combobox", "radio") and opts:
            return opts[0]
        if kind == "checkbox":
            return "Checked"
        if kind == "date":
            return "today"
        if f.get("lookup") or (kind == "picklist" and not f.get("key_field") and not f.get("add_new")):
            real = self._ctx_value(f)
            return real if real else "PLACEHOLDER_" + re.sub(r"[^A-Za-z0-9]+", "_", f.get("label") or f["name"]).strip("_")
        if f.get("type") in ("number", "percentage", "int", "decimal") and not f.get("key_field") and _kind_of(f) != "name":
            return self.number(f, style)
        return self.text(f, style if style in ("short", "max", "special", "digits", "mixed") else "valid")