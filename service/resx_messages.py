"""Turns C# references like  SuccessResponse.SaveSuccessWithCodeAndName  into the real message text.

Why this exists
---------------
The backend keeps its messages in .resx files (SuccessResponse.resx, ErrorResponse.resx).
C# code reads them through a generated class (SuccessResponse.Designer.cs) whose
property names are *cleaned* versions of the .resx keys. For example the .resx key
    SaveSuccesfullyCode&Name
is read in C# as the property
    SaveSuccesfullyCode_Name
(the "&" is not allowed in a C# name, so it becomes "_").

So a direct lookup of the C# name in the .resx fails for these keys. This module
builds the property -> key mapping so the lookup works for every message.

New file, nothing else in the project imports it yet. It never raises on bad input:
a message that cannot be resolved comes back as None.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

# public static string <Property> { get { return ResourceManager.GetString("<key>", ...
_DESIGNER_RE = re.compile(
    r'public\s+static\s+string\s+(\w+)\s*\{\s*get\s*\{\s*return\s+ResourceManager\.GetString\(\s*"([^"]+)"',
    re.S,
)
# SuccessResponse.Name  /  ErrorResponse.Name  used in C# code
_REF_RE = re.compile(r'\b(SuccessResponse|ErrorResponse)\.(\w+)')


def _clean_name(key: str) -> str:
    """Same idea Visual Studio uses: characters not allowed in a C# name become '_'."""
    name = re.sub(r'\W', '_', key)
    return '_' + name if name and name[0].isdigit() else name


def _read_resx(path: Path) -> dict[str, str]:
    root = ET.parse(path).getroot()
    out: dict[str, str] = {}
    for item in root.findall('data'):
        name = item.get('name')
        value = item.find('value')
        if name:
            out[name] = (value.text or '') if value is not None else ''
    return out


def _read_designer(path: Path) -> dict[str, str]:
    """property name -> resx key, exactly as the generated C# class maps them."""
    if not path.exists():
        return {}
    return {prop: key for prop, key in _DESIGNER_RE.findall(path.read_text(encoding='utf-8-sig', errors='replace'))}


class ResponseMessages:
    """Load once per run (the files are tiny), then call .text() / .render()."""

    def __init__(self, response_dir: str | Path):
        d = Path(response_dir)
        self._by_class: dict[str, dict[str, str]] = {}     # class -> {property: text}
        for cls in ('SuccessResponse', 'ErrorResponse'):
            resx = d / f'{cls}.resx'
            if not resx.exists():
                continue
            texts = _read_resx(resx)                          # key -> text
            prop_to_key = {_clean_name(k): k for k in texts}  # fallback: derive the mapping ourselves
            prop_to_key.update(_read_designer(d / f'{cls}.Designer.cs'))  # the real mapping wins
            self._by_class[cls] = {prop: texts[key] for prop, key in prop_to_key.items() if key in texts}

    @classmethod
    def from_sources(cls, sources: dict[str, tuple[str, str | None]]) -> "ResponseMessages":
        """Build from file TEXTS instead of a folder (the files come from GitLab, not the disk).
        sources = {"SuccessResponse": (resx_text, designer_text_or_None), "ErrorResponse": (...)}"""
        self = cls.__new__(cls)
        self._by_class = {}
        for name, (resx_text, designer_text) in (sources or {}).items():
            if not resx_text:
                continue
            root = ET.fromstring(resx_text.lstrip("\ufeff"))
            texts: dict[str, str] = {}
            for item in root.findall("data"):
                key = item.get("name")
                value = item.find("value")
                if key:
                    texts[key] = (value.text or "") if value is not None else ""
            prop_to_key = {_clean_name(k): k for k in texts}
            if designer_text:
                prop_to_key.update({p: k for p, k in _DESIGNER_RE.findall(designer_text)})
            self._by_class[name] = {p: texts[k] for p, k in prop_to_key.items() if k in texts}
        return self

    def text(self, cls: str, prop: str) -> str | None:
        """Raw template, e.g. 'Details Saved Successfully With Code: {0} and Name: {1}'."""
        return self._by_class.get(cls, {}).get(prop)

    def render(self, cls: str, prop: str, *args) -> str | None:
        """Template with {0}, {1} ... filled in. Returns the template unchanged if it has no slots."""
        template = self.text(cls, prop)
        if template is None:
            return None
        try:
            return template.format(*args)
        except (IndexError, KeyError, ValueError):
            return template

    def refs_in_code(self, cs_source: str) -> list[dict]:
        """Every SuccessResponse.X / ErrorResponse.X used in a C# file, with its resolved text."""
        seen, out = set(), []
        for cls, prop in _REF_RE.findall(cs_source or ''):
            if (cls, prop) in seen:
                continue
            seen.add((cls, prop))
            out.append({'class': cls, 'property': prop, 'text': self.text(cls, prop)})
        return out