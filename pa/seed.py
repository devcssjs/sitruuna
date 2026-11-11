"""One-time setup: puts dates you already know on the timetable.

Dates come from data/known_dates.csv (or data/known_dates.example.csv until you
make your own). Columns:
  source      told = you were told it (applied as a change, wins over handouts)
              handout = from a course handout (added only if nothing similar is stored)
  course, type, title, date (YYYY-MM-DD), start, end (HH:MM, optional), notes,
  confidence  high = added straight away, low = Sitruuna asks you first
  syllabus    optional topic ids from topics.csv, separated by |  (e.g. DD06|DD12)
Emails always win over handouts. Runs once (remembered in the database).
"""
from __future__ import annotations

import csv
import json

from .config import DATA_DIR, data_file
from .pipeline import Notice

SEED_KEY = "seeded_v1"


def load_known_dates(path=None) -> list[dict]:
    p = path or data_file("known_dates.csv", DATA_DIR)
    if not p.exists():
        return []
    with open(p, encoding="utf-8-sig", newline="") as f:
        rows = []
        for r in csv.DictReader(f):
            r = {k: (v or "").strip() for k, v in r.items() if k}
            if r.get("course") and r.get("type") and r.get("title") and r.get("date"):
                rows.append(r)
        return rows


def seed_progress(planner) -> int:
    """Once: count the midsem syllabus as learned (planner.assume_midsem_done),
    except the topic ids listed in planner.not_done_yet."""
    st = planner.store
    if st.get_kv("progress_seeded_v1"):
        return 0
    st.set_kv("progress_seeded_v1", "1")
    if not planner.s.get("planner", "assume_midsem_done", default=False):
        return 0
    not_done = set(planner.s.get("planner", "not_done_yet", default=[]) or [])
    n = 0
    for t in planner.topics:
        if t.pre_midsem and t.id not in not_done and planner.status(t) != "d":
            planner.set_topic_status(t.id, "d")
            n += 1
    return n


def seed_syllabi(planner) -> bool:
    """Once: known dates with a 'syllabus' column get that syllabus attached."""
    st = planner.store
    if st.get_kv("syllabus_seeded_v1"):
        return False
    st.set_kv("syllabus_seeded_v1", "1")
    from .store import _similar
    known = {t.id for t in planner.topics}
    done = False
    for r in load_known_dates():
        ids = [i.strip() for i in r.get("syllabus", "").split("|") if i.strip() in known]
        if not ids:
            continue
        rows = st.conn.execute("SELECT id, title FROM events WHERE course=? AND type=? "
                               "AND status IN ('active','pending') ORDER BY date", (r["course"], r["type"])).fetchall()
        row = next((x for x in rows if _similar(x["title"], r["title"])), None)
        if not row or st.get_kv(f"syllabus:{row['id']}"):
            continue
        st.set_kv(f"syllabus:{row['id']}", json.dumps({
            "topics": ids, "short": ", ".join(ids), "text": f"{r['title']} (known dates file)",
            "source": "known dates file", "status": "set"}))
        done = True
    return done


def _item(r, action, source):
    conf = r.get("confidence") or "high"
    return {"title": r["title"], "type": r["type"], "action": action, "course": r["course"], "date": r["date"],
            "start_time": r.get("start") or None, "end_time": r.get("end") or None, "venue": None,
            "notes": r.get("notes") or None, "source_file": source, "source_quote": None, "section": None,
            "confidence": conf, "low_reason": None if conf == "high" else "date from handout, may change",
            "source_msg_id": None, "source_subject": source, "source_sender": "Sitruuna setup"}


def _exists(store, course, type_, title) -> bool:
    from .store import _similar
    rows = store.conn.execute(
        "SELECT title FROM events WHERE course=? AND type=? AND status IN ('active','pending','ignored','undone','removed')",
        (course, type_)).fetchall()
    return any(type_ in ("compre", "midsem") or _similar(r["title"], title) for r in rows)


def seed(assistant, force: bool = False) -> list[Notice]:
    st = assistant.store
    if st.get_kv(SEED_KEY) and not force:
        return []
    today = assistant.s.today().isoformat()
    rows = [r for r in load_known_dates() if r["date"] >= today]
    notices = []
    for r in rows:                                   # dates you were told first: they win
        if r.get("source", "").lower() == "told":
            n = assistant.apply(_item(r, "update", "Your message"))
            if n:
                notices.append(n)
    for r in rows:
        if r.get("source", "").lower() != "told" and not _exists(st, r["course"], r["type"], r["title"]):
            n = assistant.apply(_item(r, "add", "Course handout"))
            if n:
                notices.append(n)
    st.set_kv(SEED_KEY, today)
    return notices
