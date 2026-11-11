"""Morning digest, evening 'tomorrow you have...' prep reminder, and the
heads-up before exams / must-attend classes."""
from __future__ import annotations

from datetime import datetime, timedelta

from .config import Settings
from .courses import with_course
from .pipeline import TYPE_ICON, _nice_date, esc
from .store import Store
from .timetable import day_items, format_day

PREP_TYPES = ("midsem", "compre", "exam", "quiz", "deadline", "lab")


def morning_digest(store: Store, s: Settings) -> str:
    today = s.today()
    week_days = int(s.get("reminders", "week_ahead_days", default=7))
    name = esc(s.get("your_name", default="") or "")
    hello = f"Good morning, {name}!" if name else "Good morning!"
    parts = [f"☀️ <b>{hello}</b>\n\n<b>Today</b>\n{format_day(store, today, heading=False)}"]

    tomorrow = today + timedelta(days=1)
    due_tmrw = [r for r in store.events_on(tomorrow.isoformat()) if r["status"] == "active"]
    if due_tmrw:
        parts.append("<b>Tomorrow</b>\n" + "\n".join(f"   {_line(r)}" for r in due_tmrw))

    later = [r for r in store.events_between((today + timedelta(days=2)).isoformat(),
                                              (today + timedelta(days=week_days)).isoformat())
             if r["type"] != "class"]
    if later:
        parts.append(f"<b>Next {week_days} days</b>\n" + "\n".join(
            f"   {_nice_date(r['date'])}: {_line(r)}" for r in later))

    pending = store.pending()
    if pending:
        parts.append(f"🤔 {len(pending)} item(s) waiting for you to check → /pending")
    return "\n\n".join(parts)


def prep_reminder(store: Store, s: Settings) -> str | None:
    """Sent in the evening: exams/deadlines tomorrow + must-attend classes tomorrow."""
    tomorrow = s.today() + timedelta(days=1)
    rows = [r for r in store.events_on(tomorrow.isoformat())
            if r["status"] == "active" and r["type"] in PREP_TYPES and not r["reminded_prep"]]
    must = [i for i in day_items(store, tomorrow) if i["kind"] == "class" and i["must"] and not i["cancelled"]]
    waiting = [r for r in store.pending() if r["date"] == tomorrow.isoformat()]
    if not rows and not must and not waiting:
        return None
    lines = [f"🌙 <b>Prep for tomorrow ({_nice_date(tomorrow.isoformat())})</b>"]
    for r in rows:
        lines.append(f"   {_line(r)}")
        if r["notes"]:
            lines.append(f"      📝 {esc(r['notes'])}")
        store.update_event(r["id"], reminded_prep=1)
    for c in must:
        lines.append(f"   🏫 {c['time']} {esc(c['text'])} ⭐ don't miss")
    for r in waiting:
        lines.append(f"   🤔 {_line(r)} — still needs your ✅/❌ (see /pending)")
    if rows:
        lines.append("\nGet your notes ready tonight 💪")
    return "\n".join(lines)


def soon_reminders(store: Store, s: Settings) -> list[str]:
    """Called every minute. Warns before exams and must-attend classes, once per
    alert time in config (default 60 and 15 minutes before)."""
    now = s.now()
    raw = s.get("reminders", "before_event_minutes", default=[60, 15])
    alerts = sorted({int(x) for x in (raw if isinstance(raw, list) else [raw])}, reverse=True)
    out = []
    for i in day_items(store, now.date()):
        if not i["time"] or not i["must"] or i["cancelled"]:
            continue
        start = datetime.combine(now.date(), datetime.strptime(i["time"], "%H:%M").time(), tzinfo=s.tz)
        mins = (start - now).total_seconds() / 60
        due = [a for a in alerts if 0 < mins <= a]
        if not due:
            continue
        nearest = min(due)       # e.g. bot started 10 min before: send the 15-min alert only
        base = f"soon:{now.date()}:{i['id'] or i['course']}:{i['time']}"
        if store.get_kv(f"{base}:{nearest}"):
            continue
        for a in due:            # mark this and any bigger alerts as done
            store.set_kv(f"{base}:{a}", "1")
        venue = f" at {esc(i['venue'])}" if i["venue"] else ""
        left = max(1, int(round(mins)))
        if nearest == alerts[-1] and len(alerts) > 1:
            out.append(f"🔔 In {left} min: {esc(i['text'])}{venue} ({i['time']}) — leave now!")
        else:
            out.append(f"⏳ In {left} min: {esc(i['text'])}{venue} ({i['time']})")
    return out


def _line(r) -> str:
    title = with_course(r["course"], r["title"])
    t = f" {r['start_time']}" if r["start_time"] else ""
    v = f" · {r['venue']}" if r["venue"] else ""
    return f"{TYPE_ICON.get(r['type'], '📌')}{t} {esc(title)}{esc(v)}"
