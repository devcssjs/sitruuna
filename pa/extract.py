"""Sends one email (+ attachments) to the AI and gets back timetable items."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .attachments import read_attachments
from .config import ROOT, Settings
from .gmail_fetch import Email
from .llm import BaseLLM

SYSTEM = "You extract dated academic events from emails. You reply with valid JSON only."


@dataclass
class Extraction:
    summary: str
    candidates: list[dict]
    source_text: str                  # everything the AI read as text (used to verify quotes)
    viewed_files: list[str] = field(default_factory=list)   # files the AI looked at as images/scans
    skipped_files: list[str] = field(default_factory=list)
    syllabi: list[dict] = field(default_factory=list)      # "Quiz 2 covers ch 5-6" (see pa/syllabus.py)


def build_prompt(email: Email, settings: Settings) -> tuple[str, str, list, list[str], list[str]]:
    template = (ROOT / "prompts" / "extract.txt").read_text(encoding="utf-8")
    header = template.format(
        my_courses=settings.courses.for_prompt() or "(not set: include everything)",
        today=settings.today().isoformat(),
        sent_at=email.sent_at.strftime("%A %d %B %Y, %H:%M"),
    )
    att = read_attachments(email.attachments)
    max_chars = int(settings.get("ai", "max_chars", default=60000))

    blocks = [f"FROM: {email.sender}\nSUBJECT: {email.subject}\n\n=== EMAIL BODY ===\n{email.body}"]
    for name, text in att.texts:
        blocks.append(f"=== ATTACHMENT: {name} ===\n{text}")
    if att.files:
        blocks.append("Also attached (shown to you as files): " + ", ".join(n for n, _, _ in att.files))
    source_text = "\n\n".join(blocks)[:max_chars]

    prompt = header + "\n\n---------------- SOURCE ----------------\n" + source_text
    return prompt, source_text, att.files, [n for n, _, _ in att.files], att.skipped


def extract(email: Email, llm: BaseLLM, settings: Settings) -> Extraction:
    prompt, source_text, files, viewed, skipped = build_prompt(email, settings)
    raw = llm.ask(SYSTEM, prompt, files)
    data = parse_json(raw)
    events = data.get("events", []) if isinstance(data, dict) else data
    if not isinstance(events, list):
        events = []
    return Extraction(
        summary=(data.get("summary") if isinstance(data, dict) else "") or "",
        candidates=[e for e in events if isinstance(e, dict)],
        source_text=source_text,
        viewed_files=viewed,
        skipped_files=skipped,
        syllabi=[x for x in (data.get("syllabi") or []) if isinstance(x, dict)] if isinstance(data, dict) else [],
    )


def parse_json(raw: str):
    """Reads the AI's JSON even if it wrapped it in ```json fences or added chatter."""
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for open_c, close_c in (("{", "}"), ("[", "]")):
        start, end = text.find(open_c), text.rfind(close_c)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue
    return {"summary": "", "events": []}
