"""Your courses and their short forms (DD, EM, M3...).

Course codes come in many spellings:
  "EEE F215", "ECE/EEE/INSTR F215", "1166_ECE_INSTR_EEE_ECOM F215", "INSTR_EEE_ECE_F214"
They all get broken into simple codes like {"EEEF215", "ECEF215", ...}, and two
spellings mean the same course if they share at least one code.
"""
from __future__ import annotations

import re

# a run of prefixes (joined by / or _) followed by F/C/G + 3 digits
CODE_RE = re.compile(r"((?:[A-Z]{2,5}\s*/\s*)*[A-Z]{2,5})[\s/\-]*([FCG]\s*\d{3})")


def course_codes(text: str | None) -> set[str]:
    """'1166_ECE_INSTR F215' -> {'ECEF215', 'INSTRF215'}"""
    t = (text or "").upper().replace("_", "/")
    out: set[str] = set()
    for prefixes, num in CODE_RE.findall(t):
        num = num.replace(" ", "")
        for p in re.split(r"\s*/\s*", prefixes):
            p = p.strip()
            if p:
                out.add(p + num)
    return out


class Courses:
    """short form -> {code, name}. Build with Courses.from_config(settings.raw)."""

    def __init__(self, entries: dict[str, dict]):
        self.entries = entries                       # {"DD": {"code": "...", "name": "..."}}
        self._codes = {short: course_codes(e["code"]) | course_codes(short)
                       for short, e in entries.items()}

    @classmethod
    def from_config(cls, raw: dict) -> "Courses":
        entries: dict[str, dict] = {}
        for short, val in (raw.get("courses") or {}).items():
            if isinstance(val, dict):
                entries[str(short)] = {"code": str(val.get("code", "")), "name": str(val.get("name", ""))}
            else:
                entries[str(short)] = {"code": str(val), "name": ""}
        for code in raw.get("my_courses") or []:      # old-style list still works
            entries.setdefault(str(code), {"code": str(code), "name": ""})
        return cls(entries)

    def __bool__(self):
        return bool(self.entries)

    def short(self, text: str | None) -> str | None:
        """Any spelling (or the short form itself) -> short form, or None if not yours."""
        if not text:
            return None
        t = str(text).strip()
        for short in self.entries:
            if t.lower() == short.lower():
                return short
        found = course_codes(t)
        if not found:
            return None
        for short, codes in self._codes.items():
            if found & codes:
                return short
        return None

    def same(self, a: str | None, b: str | None) -> bool:
        if not a or not b:
            return False
        sa, sb = self.short(a), self.short(b)
        if sa and sb:
            return sa == sb
        return a.strip().lower() == b.strip().lower() or bool(course_codes(a) & course_codes(b))

    def for_prompt(self) -> str:
        lines = []
        for short, e in self.entries.items():
            name = f" ({e['name']})" if e["name"] else ""
            code = f" = {e['code']}" if e["code"] and e["code"] != short else ""
            lines.append(f"- {short}{code}{name}")
        return "\n".join(lines)


def with_course(course: str | None, title: str, sep: str = " ") -> str:
    """'DD', 'Quiz 2' -> 'DD Quiz 2'; leaves 'DD Quiz 2' alone (whole-word check,
    so 'EM' isn't fooled by 'EMT' and 'DD' isn't fooled by 'additional')."""
    title = title or ""
    if not course or re.search(rf"(?<![A-Za-z0-9]){re.escape(course)}(?![A-Za-z0-9])", title, re.I):
        return title
    return f"{course}{sep}{title}"


def same_course(a: str | None, b: str | None) -> bool:
    """'DD' == 'dd'; 'EEE F215' == 'ECE/EEE F215'."""
    if not a or not b:
        return False
    return a.strip().lower() == b.strip().lower() or bool(course_codes(a) & course_codes(b))
