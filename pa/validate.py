"""Decides if an item is HIGH confidence (auto-add) or LOW (ask you first).

The AI's own "high" is not trusted on its own. An item is only high if ALL of
these pass in code:
  - the date is a real date, today or later, within ~13 months
  - the course is one of yours (or the item is for everyone)
  - the quote the AI gave really appears in the email/attachment text
  - the day number of the date appears in that quote
  - the quote doesn't rely on relative words like "tomorrow" / "next week"
  - it wasn't read from an image or scanned PDF (those can't be double-checked)
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher

from .courses import Courses, course_codes  # noqa: F401  (course_codes re-exported)

VALID_TYPES = {"quiz", "midsem", "compre", "exam", "deadline", "class", "lab", "event"}
VALID_ACTIONS = {"add", "update", "cancel"}
SECTION_TAG_RE = re.compile(r"(?<![A-Za-z0-9])([LTP])\s?-?(\d{1,2})(?![A-Za-z0-9])")
BATCH_RE = re.compile(r"(?<![A-Za-z0-9])(?:batch\s*-?\s*)?B\s?-?(\d{1,2})(?![A-Za-z0-9])", re.I)
MAKEUP_RE = re.compile(r"\bmake[\s-]?up\b|\bmakeup\b|\bre-?test\b", re.I)
RELATIVE_WORDS = re.compile(
    r"\b(today|tomorrow|tonight|yesterday|next|this coming|coming|upcoming|day after)\b", re.I
)


# ---------------- course codes ----------------
def canonical_course(course: str | None, courses: Courses | list[str]) -> tuple[str | None, bool]:
    """Returns (short form like 'DD', is_it_yours). None course = for everyone."""
    if not isinstance(courses, Courses):
        courses = Courses.from_config({"my_courses": courses})
    if not course or str(course).strip().lower() in ("null", "none", "all", ""):
        return None, True
    if not courses:
        return str(course).strip(), True
    short = courses.short(course)
    return (short, True) if short else (str(course).strip(), False)


# ---------------- checks ----------------
def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def quote_in_source(quote: str, source: str) -> bool:
    q, s = _norm(quote), _norm(source)
    if len(q) < 4:
        return False
    if q in s:
        return True
    m = SequenceMatcher(None, s, q, autojunk=False).find_longest_match(0, len(s), 0, len(q))
    return m.size >= 0.85 * len(q)


def day_in_quote(d: date, quote: str) -> bool:
    # standalone 1-2 digit numbers only, so "F215" or "2026" don't count
    nums = {int(n) for n in re.findall(r"(?<!\d)\d{1,2}(?!\d)", quote or "")}
    return d.day in nums


def parse_time(t) -> str | None:
    if not t or str(t).lower() in ("null", "none"):
        return None
    m = re.match(r"^(\d{1,2}):(\d{2})", str(t).strip())
    if not m:
        return None
    h, mnt = int(m.group(1)), int(m.group(2))
    return f"{h:02d}:{mnt:02d}" if h < 24 and mnt < 60 else None


def check(cand: dict, source_text: str, viewed_files: list[str],
          courses, today: date, context: dict | None = None) -> dict | None:
    """Cleans one AI item. Returns None to drop it, else a dict ready for the database
    with 'confidence' and 'low_reason' filled in."""
    try:
        d = datetime.strptime(str(cand.get("date", "")).strip(), "%Y-%m-%d").date()
    except ValueError:
        return None
    if d < today or d > today + timedelta(days=400):
        return None

    course, mine = canonical_course(cand.get("course"), courses)
    if not mine:
        return None   # someone else's course in a big schedule

    # only academic items; course-less items must be explicitly marked academic
    acad = cand.get("academic")
    is_academic = (str(acad).strip().lower() == "true") if acad is not None else bool(course)
    if not is_academic:
        return None

    type_ = str(cand.get("type", "event")).lower()
    type_ = type_ if type_ in VALID_TYPES else "event"
    action = str(cand.get("action", "add")).lower()
    action = action if action in VALID_ACTIONS else "add"
    quote = str(cand.get("source_quote") or "").strip()
    src_file = str(cand.get("source_file") or "email body").strip()

    reasons = []
    if str(cand.get("confidence", "low")).lower() != "high":
        reasons.append("AI was unsure")
    from_image = any(f.lower() in src_file.lower() for f in viewed_files)
    if from_image:
        reasons.append("read from an image/scan")
    elif not quote_in_source(quote, source_text):
        reasons.append("couldn't find the quoted line in the email")
    if quote and not from_image and not day_in_quote(d, quote):
        reasons.append("date not written out in the quote")
    if RELATIVE_WORDS.search(quote):
        reasons.append("relative date (e.g. 'tomorrow')")
    if not course and type_ in ("quiz", "midsem", "compre", "exam", "lab"):
        reasons.append("no course code")
    title_raw = str(cand.get("title") or "")
    if type_ != "class" and MAKEUP_RE.search(" ".join([title_raw, str(cand.get("notes") or ""), quote])):
        reasons.append("make-up (only needed if you missed the original)")

    # ---- is it for YOUR section? (lecture L2, tutorial T2, lab P3 ... from your timetable) ----
    section = None
    if context and course:
        tags = section_tags(context.get("subject", ""), cand.get("section"))
        verdict, detail = section_verdict(course, tags, context.get("sections") or {})
        yours = _yours(course, context.get("sections") or {})
        if verdict == "other":
            if type_ in ("class", "lab"):
                return None          # another section's class: not for you
            reasons.append(f"sent to {detail} (your sections: {yours})")
            section = detail
        elif verdict == "mine":
            section = detail
        # verdict 'none': no section named -> meant for all sections, handled normally

    # ---- is it for YOUR lab batch? ("Batch B1", "B3  21.11.26") ----
    mine_b = str((context or {}).get("batch") or "").strip().upper().lstrip("B")
    if mine_b and course:
        found = {m.lstrip("0") for m in BATCH_RE.findall(" ".join([title_raw, quote, str(cand.get("section") or "")]))}
        if found and mine_b.lstrip("0") not in found:
            if type_ in ("class", "lab"):
                return None          # another batch's lab slot: not for you
            reasons.append(f"for batch B{'/B'.join(sorted(found))} (yours: B{mine_b})")

    title = str(cand.get("title") or "").strip() or f"{course or ''} {type_}".strip()
    if section and section.lower() not in title.lower():
        title = (f"{course} {section}{title[len(course):]}" if course and title.lower().startswith(course.lower())
                 else f"{section} {title}")
    return {
        "title": title[:120],
        "type": type_,
        "action": action,
        "course": course,
        "date": d.isoformat(),
        "start_time": parse_time(cand.get("start_time")),
        "end_time": parse_time(cand.get("end_time")),
        "venue": _clean(cand.get("venue")),
        "notes": _clean(cand.get("notes")),
        "source_file": src_file[:120],
        "source_quote": quote[:400],
        "section": section,
        "confidence": "low" if reasons else "high",
        "low_reason": "; ".join(reasons) or None,
    }


def _clean(v):
    if v is None:
        return None
    s = str(v).strip()
    return None if s.lower() in ("", "null", "none", "n/a") else s[:300]


# ---------------- sections ----------------
def sender_name(sender: str | None) -> str:
    """'"JANE DOE . (via BITS Goa)" <noreply@...>' -> 'Jane Doe'"""
    name = (sender or "").split("<")[0]
    name = re.sub(r"\(via[^)]*\)", "", name, flags=re.I)
    name = re.sub(r'["\.]', " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    if not name:
        m = re.search(r"<([^@>]+)@", sender or "")
        name = m.group(1) if m else ""
    return name.title() if name.isupper() or name.islower() else name


def section_tags(subject: str, ai_section) -> set[str]:
    """Section codes the mail is addressed to, e.g. {'L1'} from 'F214 ...: L1: Compensatory class'."""
    tags = {m[0].upper() + str(int(m[1])) for m in SECTION_TAG_RE.findall(subject or "")}
    if ai_section and str(ai_section).strip().lower() not in ("null", "none", ""):
        for m in SECTION_TAG_RE.findall(str(ai_section)):
            tags.add(m[0].upper() + str(int(m[1])))
    return tags


def section_verdict(course: str, tags: set[str], sections: dict) -> tuple[str, str]:
    """('mine'|'other'|'none', section). 'none' = no section named, so it's for everyone.
    Who sent the mail doesn't matter (course in-charges, project mails etc. are fine)."""
    mine = sections.get(course) or {}
    relevant = [t for t in tags if t[0] in mine]
    if not relevant:
        return "none", ""
    hit = [t for t in relevant if t in mine[t[0]]]
    return ("mine", hit[0]) if hit else ("other", sorted(relevant)[0])


def _yours(course: str, sections: dict) -> str:
    return "/".join(sorted(t for s in (sections.get(course) or {}).values() for t in s)) or "?"
