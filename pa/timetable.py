"""Builds the timetable you see with /timetable, /week and the morning digest:
weekly classes (data/timetable.csv) + dated items from emails, merged by time."""
from __future__ import annotations

from datetime import date, timedelta

from .gcal import DAYS, class_matches_cancel, load_weekly_classes
from .pipeline import TYPE_ICON, _nice_date, esc
from .store import Store
from .courses import same_course, with_course


def _class_cancelled(cls: dict, cancels) -> bool:
    return any(class_matches_cancel(cls, c) for c in cancels)


def day_items(store: Store, d: date) -> list[dict]:
    """Everything on one day as simple dicts, sorted by time (all-day items first)."""
    rows = store.events_on(d.isoformat())
    cancels = [r for r in rows if r["status"] == "cancelled"]
    items = []
    weekly = load_weekly_classes()
    for cls in weekly:
        if cls["day"] != DAYS[d.weekday()]:
            continue
        items.append({
            "time": cls["start"], "end": cls["end"], "kind": "class",
            "text": f"{cls['course']} {cls['kind']}".strip() + (f" ({cls['title']})" if cls["title"] else ""),
            "venue": cls["venue"], "must": cls["must_attend"],
            "cancelled": _class_cancelled(cls, cancels), "id": None, "course": cls["course"],
        })
    for r in rows:
        if r["status"] != "active":
            continue
        title = with_course(r["course"], r["title"])
        items.append({
            "time": r["start_time"], "end": r["end_time"], "kind": r["type"], "text": title,
            "venue": r["venue"], "must": _must(r, weekly),
            "cancelled": False, "id": r["id"], "course": r["course"],
        })
    items.sort(key=lambda i: (i["time"] is not None, i["time"] or ""))
    return items


def _must(r, weekly) -> bool:
    """Exams/quizzes/labs always; an extra or moved class gets ⭐ if that course takes attendance."""
    if r["type"] in ("midsem", "compre", "exam", "quiz", "lab"):
        return True
    if r["type"] == "class":
        return any(w["must_attend"] and same_course(w["course"], r["course"]) for w in weekly)
    return False


def format_day(store: Store, d: date, heading: bool = True) -> str:
    items = day_items(store, d)
    lines = [f"🗓 <b>{_nice_date(d.isoformat())}</b>"] if heading else []
    if not items:
        lines.append("   Nothing scheduled 🎉")
    for i in items:
        icon = "🏫" if i["kind"] == "class" else TYPE_ICON.get(i["kind"], "📌")
        when = i["time"] or "All day"
        if i["time"] and i["end"]:
            when += f"–{i['end']}"
        star = " ⭐" if i["must"] else ""
        venue = f" · {esc(i['venue'])}" if i["venue"] else ""
        text = f"{when}  {icon} {esc(i['text'])}{venue}{star}"
        if i["cancelled"]:
            text = f"<s>{text}</s> 🚫 cancelled"
        lines.append("   " + text)
    return "\n".join(lines)


def format_week(store: Store, start: date, days: int = 7) -> str:
    return "\n\n".join(format_day(store, start + timedelta(days=k)) for k in range(days))


def format_deadlines(store: Store, today: date, limit: int = 25) -> str:
    rows = [r for r in store.upcoming(today.isoformat()) if r["type"] != "class"][:limit]
    if not rows:
        return "No upcoming exams or deadlines found yet."
    lines = ["📋 <b>Coming up</b>"]
    for r in rows:
        days_left = (date.fromisoformat(r["date"]) - today).days
        when = "TODAY" if days_left == 0 else "tomorrow" if days_left == 1 else f"in {days_left} days"
        title = with_course(r["course"], r["title"])
        t = f", {r['start_time']}" if r["start_time"] else ""
        lines.append(f"{TYPE_ICON.get(r['type'], '📌')} {esc(title)}\n     {_nice_date(r['date'])}{t} — {when}")
    return "\n".join(lines)
