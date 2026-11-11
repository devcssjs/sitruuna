"""Accuracy check: reads your 20 newest college emails with the real AI and
prints what it found, WITHOUT saving anything or touching your calendar.

    python tests/check_20.py

Open data/check_20_results.txt, compare each item with the real email,
and count how many are right. Aim for 18+ out of 20 before trusting it.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pa import validate                      # noqa: E402
from pa.config import DATA_DIR, load_settings  # noqa: E402
from pa.extract import extract               # noqa: E402
from pa.gmail_fetch import GmailReader, sender_allowed  # noqa: E402
from pa.llm import make_llm                  # noqa: E402


def main(n: int = 20):
    s = load_settings()
    gmail, llm = GmailReader(s), make_llm(s)
    ids = gmail.list_ids(gmail.build_query(None), limit=200)[-n:]
    out = [f"AI: {llm.name} ({llm.model})   Emails checked: {len(ids)}\n"]
    for k, msg_id in enumerate(reversed(ids), start=1):
        email = gmail.fetch(msg_id)
        out.append(f"\n#{k}  {email.sent_at:%d %b %Y}  {email.subject}\n    from: {email.sender}")
        if not sender_allowed(email.sender, s):
            out.append("    (skipped: sender not in allowed list)")
            continue
        try:
            ex = extract(email, llm, s)
        except Exception as e:
            out.append(f"    AI ERROR: {e}")
            continue
        out.append(f"    summary: {ex.summary}")
        if not ex.candidates:
            out.append("    -> nothing for the timetable")
        for c in ex.candidates:
            item = validate.check(c, ex.source_text, ex.viewed_files, s.courses, s.today())
            if item is None:
                out.append(f"    -> DROPPED (past / not your course / bad date): {c.get('title')} {c.get('date')}")
                continue
            tag = "AUTO-ADD" if item["confidence"] == "high" else f"ASK YOU ({item['low_reason']})"
            out.append(f"    -> [{tag}] {item['action']} {item['type']} | {item['course']} | {item['title']} | "
                       f"{item['date']} {item['start_time'] or ''} | {item['venue'] or ''}")
            out.append(f"       quote: \"{item['source_quote']}\"")
        if ex.skipped_files:
            out.append(f"    unread attachments: {ex.skipped_files}")
        print(f"  checked {k}/{len(ids)}")

    text = "\n".join(out)
    path = DATA_DIR / "check_20_results.txt"
    path.write_text(text, encoding="utf-8")
    print(text)
    print(f"\nSaved to {path}")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 20)
