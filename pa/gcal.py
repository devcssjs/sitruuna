"""Google Calendar sync. Your iPhone shows these events once the same Google
account is added on the phone (Settings > Calendar > Accounts > Add Account > Google).
"""
from __future__ import annotations

import csv
import json
import logging
import re
from datetime import date, datetime, timedelta

from googleapiclient.discovery import build

from .config import DATA_DIR, Settings, data_file
from .courses import same_course, with_course
from .google_auth import CALENDAR_SCOPES, CALENDAR_TOKEN, get_creds

log = logging.getLogger(__name__)

# Google Calendar colour IDs
COLOURS = {"midsem": "11", "compre": "11", "exam": "11", "quiz": "6",
           "deadline": "5", "lab": "7", "class": "9", "event": "2"}
DAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]


class Calendar:
    def __init__(self, settings: Settings):
        self.s = settings
        self.cal_id = settings.get("google_calendar", "calendar_id", default="primary")
        self.tzname = settings.get("timezone", default="Asia/Kolkata")
        creds = get_creds(CALENDAR_TOKEN, CALENDAR_SCOPES)
        self.svc = build("calendar", "v3", credentials=creds, cache_discovery=False)

    # ---------- one-off items (exams, deadlines...) ----------
    def _body(self, ev) -> dict:
        title = with_course(ev["course"], ev["title"])
        desc = [f"Type: {ev['type']}"]
        if ev["notes"]:
            desc.append(f"Notes: {ev['notes']}")
        desc.append(f"Source: {ev['source_subject'] or ''} ({ev['source_file'] or 'email'})")
        if ev["source_quote"]:
            desc.append(f'Quote: "{ev["source_quote"]}"')
        desc.append("Added by Sitruuna")

        body = {
            "summary": title,
            "location": ev["venue"] or "",
            "description": "\n".join(desc),
            "colorId": COLOURS.get(ev["type"], "2"),
            "reminders": {"useDefault": False, "overrides": [
                {"method": "popup", "minutes": int(m)}
                for m in self.s.get("google_calendar", "alerts_minutes", default=[1440, 60])
            ]},
        }
        d = date.fromisoformat(ev["date"])
        if ev["start_time"]:
            start = datetime.combine(d, datetime.strptime(ev["start_time"], "%H:%M").time())
            if ev["end_time"]:
                end = datetime.combine(d, datetime.strptime(ev["end_time"], "%H:%M").time())
                if end <= start:
                    end = start + timedelta(minutes=60)
            else:
                mins = int(self.s.get("google_calendar", "default_duration_minutes", default=60))
                end = start + timedelta(minutes=mins)
            body["start"] = {"dateTime": start.isoformat(), "timeZone": self.tzname}
            body["end"] = {"dateTime": end.isoformat(), "timeZone": self.tzname}
        else:
            body["start"] = {"date": d.isoformat()}
            body["end"] = {"date": (d + timedelta(days=1)).isoformat()}
        return body

    def create(self, ev) -> str:
        created = self.svc.events().insert(calendarId=self.cal_id, body=self._body(ev)).execute()
        return created["id"]

    def delete(self, gcal_id: str | None) -> None:
        if not gcal_id:
            return
        try:
            self.svc.events().delete(calendarId=self.cal_id, eventId=gcal_id).execute()
        except Exception as e:  # already deleted by hand, etc.
            log.warning("Calendar delete failed for %s: %s", gcal_id, e)

    # ---------- study planner blocks ----------
    def create_block(self, b) -> str:
        """One planner block (study / workout / reading...). Study = blue, personal = grey-ish."""
        from .planner import KIND_ICON, fmt
        d = date.fromisoformat(b.date)
        start = datetime.combine(d, datetime.strptime(fmt(b.start), "%H:%M").time())
        end = start + timedelta(minutes=b.end - b.start)
        head = f"{b.course} · {b.title}" if b.course and not b.title.startswith(b.course) else b.title
        alert = int(self.s.get("planner", "block_alert_minutes", default=10))
        body = {
            "summary": f"{KIND_ICON.get(b.kind, '')} {head}".strip(),
            "description": (b.detail or "") + "\n\nPlanned by Sitruuna (moves automatically when the plan changes)",
            "colorId": "3" if b.is_study else "8",
            "start": {"dateTime": start.isoformat(), "timeZone": self.tzname},
            "end": {"dateTime": end.isoformat(), "timeZone": self.tzname},
            "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": alert}]},
        }
        return self.svc.events().insert(calendarId=self.cal_id, body=body).execute()["id"]

    # ---------- weekly classes from data/timetable.csv ----------
    def sync_weekly_classes(self, store) -> int:
        """Puts your weekly classes on Google Calendar as repeating events until
        semester_end. Running it again replaces the old ones (no duplicates)."""
        for m in weekly_map(store):
            self.delete(m["id"])
        for gid in filter(None, (store.get_kv("weekly_class_gcal_ids") or "").split(",")):
            self.delete(gid)          # entries made by the older version

        until = date.fromisoformat(str(self.s.get("semester_end", default="2026-12-31")))
        until_str = until.strftime("%Y%m%dT235959Z")
        today = self.s.today()
        new_map = []
        for row in load_weekly_classes():
            day_idx = DAYS.index(row["day"])
            first = today + timedelta(days=(day_idx - today.weekday()) % 7)
            start = datetime.combine(first, datetime.strptime(row["start"], "%H:%M").time())
            end = datetime.combine(first, datetime.strptime(row["end"], "%H:%M").time())
            star = " ⭐" if row["must_attend"] else ""
            body = {
                "summary": f"{row['course']} {row['kind']}{star}".strip(),
                "location": row["venue"],
                "description": (f"Section {row['title']}\n" if row["title"] else "") + "Added by Sitruuna (weekly class)",
                "colorId": COLOURS["class"],
                "start": {"dateTime": start.isoformat(), "timeZone": self.tzname},
                "end": {"dateTime": end.isoformat(), "timeZone": self.tzname},
                "recurrence": [f"RRULE:FREQ=WEEKLY;UNTIL={until_str}"],
                "reminders": {"useDefault": False, "overrides": (
                    [{"method": "popup", "minutes": 15}] if row["must_attend"] else [])},
            }
            gid = self.svc.events().insert(calendarId=self.cal_id, body=body).execute()["id"]
            new_map.append({"id": gid, "course": row["course"], "day": row["day"],
                            "start": row["start"], "kind": row["kind"]})
        store.set_kv("weekly_class_map", json.dumps(new_map))
        store.set_kv("weekly_class_gcal_ids", "")
        return len(new_map)

    # ---------- one cancelled/moved lecture ----------
    def cancel_weekly(self, store, ev) -> list[str]:
        """Removes just that day's copy of a weekly class from Google Calendar.
        Returns the removed copies' IDs (so Undo can bring them back)."""
        d = date.fromisoformat(ev["date"])
        day_start = datetime.combine(d, datetime.min.time()).isoformat() + "+05:30"
        day_end = datetime.combine(d + timedelta(days=1), datetime.min.time()).isoformat() + "+05:30"
        removed = []
        for m in weekly_map(store):
            if m["day"] != DAYS[d.weekday()] or not class_matches_cancel(m, ev):
                continue
            items = self.svc.events().instances(calendarId=self.cal_id, eventId=m["id"],
                                                timeMin=day_start, timeMax=day_end).execute().get("items", [])
            for inst in items:
                self.svc.events().patch(calendarId=self.cal_id, eventId=inst["id"],
                                        body={"status": "cancelled"}).execute()
                removed.append(inst["id"])
        return removed

    def restore_instances(self, ids: list[str]) -> None:
        for iid in ids:
            try:
                self.svc.events().patch(calendarId=self.cal_id, eventId=iid,
                                        body={"status": "confirmed"}).execute()
            except Exception as e:
                log.warning("Couldn't restore class %s: %s", iid, e)


def load_weekly_classes() -> list[dict]:
    """Reads data/timetable.csv -> list of classes. Bad rows are skipped."""
    path = data_file("timetable.csv", DATA_DIR)
    if not path.exists():
        return []
    rows = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            day = (r.get("day") or "").strip().upper()[:2]
            if day not in DAYS:
                continue
            try:
                start = datetime.strptime((r.get("start") or "").strip(), "%H:%M").strftime("%H:%M")
                end = datetime.strptime((r.get("end") or "").strip(), "%H:%M").strftime("%H:%M")
            except ValueError:
                continue
            rows.append({
                "day": day,
                "start": start,
                "end": end,
                "course": (r.get("course") or "").strip(),
                "title": (r.get("title") or "").strip(),
                "kind": (r.get("kind") or "lecture").strip(),
                "venue": (r.get("venue") or "").strip(),
                "must_attend": (r.get("must_attend") or "").strip().lower() in ("yes", "y", "1", "true"),
            })
    return rows


def weekly_map(store) -> list[dict]:
    """Which Google Calendar repeating event belongs to which weekly class."""
    try:
        return json.loads(store.get_kv("weekly_class_map") or "[]")
    except json.JSONDecodeError:
        return []


def class_matches_cancel(cls: dict, c) -> bool:
    """Does cancellation `c` (a DB row or dict) hit weekly class `cls`?
    - no course on the cancel  -> every class that day (holiday / suspension)
    - lab vs lecture vs tutorial is respected ("DD lecture cancelled" keeps the DD lab)
    - if the cancel has a start time, only the class starting then"""
    if c["type"] not in ("class", "lab", "event"):
        return False
    if not c["course"]:
        return True
    if not same_course(c["course"], cls["course"]):
        return False
    text = (c["title"] or "").lower()
    kind = (cls.get("kind") or "lecture").lower()
    if c["type"] == "lab" or re.search(r"\blabs?\b|laboratory|practical", text):
        if kind != "lab":
            return False
    elif re.search(r"\btut(orial)?s?\b", text):
        if kind != "tutorial":
            return False
    elif re.search(r"\blectures?\b", text):
        if kind != "lecture":
            return False
    elif kind == "lab":
        return False        # plain "class cancelled" doesn't cancel the lab
    if c["start_time"] and c["start_time"] != cls["start"]:
        return False
    return True


def my_sections() -> dict[str, dict[str, set[str]]]:
    """From data/timetable.csv: {'ED': {'L': {'L2'}, 'T': {'T2'}}, 'DD': {'L': {'L1'}, 'T': {'T1'}, 'P': {'P3'}}, ...}"""
    out: dict[str, dict[str, set[str]]] = {}
    for row in load_weekly_classes():
        m = re.fullmatch(r"([LTP])\s*-?\s*(\d{1,2})", (row["title"] or "").strip().upper())
        if m and row["course"]:
            tag = m.group(1) + str(int(m.group(2)))
            out.setdefault(row["course"], {}).setdefault(m.group(1), set()).add(tag)
    return out
