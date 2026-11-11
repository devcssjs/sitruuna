"""The brain: new mail -> AI -> checks -> timetable + Google Calendar -> messages for you.

This file knows nothing about Telegram. It returns plain "Notice" objects, so the
same brain can later feed your own app instead of the Telegram bot.
"""
from __future__ import annotations

import html
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import validate
from .config import Settings
from .courses import with_course
from .extract import extract
from .gmail_fetch import Email, sender_allowed
from .llm import QuotaExhausted
from .store import ACTIVE, PENDING, Store, _similar

log = logging.getLogger(__name__)
ONE_PER_COURSE = {"midsem", "compre"}   # a course has only one of these, so a new date = a change
NO_INTERNET_AFTER = 5                    # this many downloads failing in a row = internet is down
MAX_RETRIES = 3                          # give up on an email after this many failed tries


@dataclass
class Notice:
    kind: str                 # added, changed, cancelled, confirm, info, error, syllabus
    event_id: int | None
    text: str
    buttons: list[tuple[str, str]] = field(default_factory=list)   # (label, callback_data)


class Assistant:
    def __init__(self, settings: Settings, store: Store, llm=None, calendar=None, gmail=None):
        self.s = settings
        self.store = store
        self.llm = llm
        self.cal = calendar
        self.gmail = gmail
        self.rescanning = False
        self.planner = None          # set by the bot; the syllabus desk makes one if needed
        self._syllabus = None

    @property
    def syllabus(self):
        if self._syllabus is None:
            from .syllabus import SyllabusDesk
            self._syllabus = SyllabusDesk(self)
        return self._syllabus

    def _take_syllabi(self, ex, source: str, from_user: bool = False) -> list[Notice]:
        out = []
        for cand in ex.syllabi:
            try:
                n = self.syllabus.receive(cand, ex.source_text, source, from_user=from_user)
            except QuotaExhausted:
                raise
            except Exception:
                log.exception("Syllabus handling failed")
                n = None
            if n:
                out.append(n)
        return out

    # ==================== mail checking ====================
    def check_mail(self) -> list[Notice]:
        if not self.gmail:
            return [Notice("error", None, "Gmail isn't connected. Run: python main.py --login")]
        if not self.llm:
            return [Notice("error", None, "The AI isn't set up. Check the API key in your .env file.")]
        if self.ai_paused():
            return []          # daily AI limit used up; checks resume (with no mail lost) after the reset
        last = self.store.get_kv("last_mail_check")
        since = datetime.fromisoformat(last) if last else None
        started = self.s.now()
        query = self.gmail.build_query(since)
        new_ids = [i for i in self.gmail.list_ids(query) if not self.store.is_processed(i)]
        for i in self._retry_list():                 # tidy: drop ones read in the meantime
            if self.store.is_processed(i):
                self._retry_remove(i)
        retry = [i for i in self._retry_list() if i not in new_ids]
        log.info("Mail check: %d new emails, %d to retry (query: %s)", len(new_ids), len(retry), query)

        notices, finished = self._read_emails(retry + new_ids)
        if finished:
            self.store.set_kv("last_mail_check", started.isoformat())
            notices += self.retry_calendar_sync()
        return notices

    def rescan(self, days: int) -> list[Notice]:
        """Re-read the last `days` days of mail with the current rules.
        Nothing is duplicated and things you ignored/undid/removed stay gone.
        If the AI's daily limit runs out, it stops and keeps what it read (no resuming later)."""
        if not self.gmail:
            return [Notice("error", None, "Gmail isn't connected. Run: python main.py --login")]
        if not self.llm:
            return [Notice("error", None, "The AI isn't set up. Check the API key in your .env file.")]
        if self.ai_paused():
            return [Notice("stop", None, self._pause_text("Can't rescan right now"))]
        ids = self.gmail.list_ids(self.gmail.build_query(None, days=days), limit=2000)
        log.info("Rescan: %d emails from the last %d days", len(ids), days)
        self.rescanning = True
        try:
            notices, _ = self._read_emails(ids)
        finally:
            self.rescanning = False
        notices += self.retry_calendar_sync()
        return notices

    def _read_emails(self, ids: list[str]) -> tuple[list[Notice], bool]:
        """Reads emails one by one. Returns (notices, finished_all).
        Stops early (keeping everything read so far) if the AI limit runs out or the internet drops."""
        notices: list[Notice] = []
        fails_in_a_row = 0
        done = 0
        for msg_id in ids:
            try:
                email = self.gmail.fetch(msg_id)
                fails_in_a_row = 0
            except Exception as e:
                fails_in_a_row += 1
                log.warning("Couldn't download email %s: %s", msg_id, str(e)[:120])
                self._retry_add(msg_id)
                if fails_in_a_row >= NO_INTERNET_AFTER:
                    notices.append(Notice("stop", None,
                        f"📶 Internet seems down, so I stopped after {done} email(s). "
                        "Everything read so far is saved; the rest will be checked when you're back online."))
                    return notices, False
                continue
            try:
                notices += self.process_email(email)
                done += 1
                if self.store.is_processed(msg_id):
                    self._retry_remove(msg_id)
            except QuotaExhausted as e:
                self._pause_ai(e.retry_seconds)
                notices.append(Notice("stop", None, self._pause_text(
                    f"⛽ The AI's free daily limit is used up. I stopped after {done} email(s) "
                    "and kept everything I found so far")))
                return notices, False
        return notices, True

    # ---------- AI daily-limit pause ----------
    def ai_paused(self) -> bool:
        until = self.store.get_kv("ai_paused_until")
        return bool(until) and datetime.fromisoformat(until) > self.s.now()

    def _pause_ai(self, retry_seconds: int | None):
        until = self.s.now() + timedelta(seconds=retry_seconds or 6 * 3600)
        self.store.set_kv("ai_paused_until", until.isoformat())
        log.warning("AI daily limit reached; pausing until %s", until)

    def _pause_text(self, lead: str) -> str:
        until = self.store.get_kv("ai_paused_until")
        when = datetime.fromisoformat(until).strftime("%I:%M %p").lstrip("0") if until else "later"
        return (f"{lead}.\nMail checks pause until about {when}, when the limit resets, "
                "then carry on with new mail as usual.")

    # ---------- retry list (emails that failed because of internet / one-off errors) ----------
    def _retry_map(self) -> dict:
        try:
            return json.loads(self.store.get_kv("retry_ids") or "{}")
        except json.JSONDecodeError:
            return {}

    def _retry_list(self) -> list[str]:
        return list(self._retry_map())

    def _retry_add(self, msg_id: str):
        m = self._retry_map()
        m[msg_id] = m.get(msg_id, 0) + 1
        if m[msg_id] > MAX_RETRIES:
            m.pop(msg_id)
            log.warning("Giving up on email %s after %d tries", msg_id, MAX_RETRIES)
        self.store.set_kv("retry_ids", json.dumps(m))

    def _retry_remove(self, msg_id: str):
        m = self._retry_map()
        if m.pop(msg_id, None) is not None:
            self.store.set_kv("retry_ids", json.dumps(m))

    def process_email(self, email: Email) -> list[Notice]:
        if not sender_allowed(email.sender, self.s):
            self.store.mark_processed(email.msg_id, email.sender, email.subject, "skipped sender")
            return []
        try:
            ex = extract(email, self.llm, self.s)
        except QuotaExhausted:
            raise
        except Exception as e:
            log.warning("AI failed on '%s': %s", email.subject, str(e)[:200])
            self._retry_add(email.msg_id)        # tried again on the next check
            return [Notice("error", None, f"⚠️ Couldn't read “{esc(email.subject)}” right now. I'll try again on the next check.")]

        notices = []
        from .gcal import my_sections
        context = {"subject": email.subject, "sender": email.sender, "sections": my_sections(),
                   "batch": self.s.get("my_lab_batch")}
        for cand in ex.candidates:
            item = validate.check(cand, ex.source_text, ex.viewed_files, self.s.courses, self.s.today(), context)
            if item is None:
                continue
            item.update(source_msg_id=email.msg_id, source_subject=email.subject[:200],
                        source_sender=email.sender[:200])
            n = self.apply(item)
            if n:
                notices.append(n)

        notices += self._take_syllabi(ex, email.subject)   # after the items, so a new quiz exists first

        if ex.skipped_files:
            notices.append(Notice("info", None,
                f"📎 In “{esc(email.subject)}”, I couldn't read: {esc(', '.join(ex.skipped_files))}"))
        self.store.mark_processed(email.msg_id, email.sender, email.subject,
                                  f"{len(ex.candidates)} items | {ex.summary}"[:500])
        return notices

    def from_user_text(self, text: str) -> list[Notice]:
        """You tell Sitruuna about a change in plain words ("EM quiz 2 moved to 18 Nov 5pm").
        The AI reads it like an email; every item is shown with ✅/❌ before it's applied."""
        if not self.llm:
            return [Notice("error", None, "The AI isn't set up. Check the API key in your .env file.")]
        if self.ai_paused():
            return [Notice("stop", None, self._pause_text("Can't read your message right now"))]
        now = self.s.now()
        email = Email(msg_id=f"tg-{now.strftime('%Y%m%d%H%M%S')}", sender="You (Telegram)",
                      subject="Your message", sent_at=now, body=text)
        try:
            ex = extract(email, self.llm, self.s)
        except QuotaExhausted as e:
            self._pause_ai(e.retry_seconds)
            return [Notice("stop", None, self._pause_text("The AI's daily limit is used up"))]
        except Exception as e:
            log.warning("AI failed on your message: %s", str(e)[:200])
            return [Notice("error", None, "⚠️ Couldn't read that right now. Try again in a minute.")]
        notices = []
        for cand in ex.candidates:
            item = validate.check(cand, ex.source_text, [], self.s.courses, self.s.today(),
                                  {"subject": "", "sender": "", "sections": {}})
            if item is None:
                continue
            item.update(confidence="low", low_reason="from your message — tap ✅ to apply",
                        source_msg_id=email.msg_id, source_subject="Your message", source_sender="You (Telegram)",
                        source_file="your message")
            n = self.apply(item)
            if n or not ex.syllabi:
                notices.append(n or Notice("info", None, f"👍 Already on your timetable: {esc(item['title'])}"))
        notices += self._take_syllabi(ex, "your message", from_user=True)
        if not notices:
            notices.append(Notice("info", None,
                "🤔 I didn't find a date change or syllabus for your courses in that.\n"
                "Try e.g. <i>“EM quiz 2 moved to 18 Nov, 5 pm”</i> or <i>“DD lab on 13 Nov cancelled”</i>.\n"
                "Commands: /help"))
        return notices

    # ==================== deciding what to do with an item ====================
    def find_target(self, item: dict):
        """For update/cancel: which existing event is this about?"""
        if not item["course"]:
            return None
        rows = self.store.future_for_course(item["course"], self.s.today().isoformat())
        same_type = [r for r in rows if r["type"] == item["type"]]
        if item["type"] in ("class", "lab"):
            # extra/moved classes: only the one on that exact day (and time, if given)
            for r in same_type:
                if r["date"] == item["date"] and (not item["start_time"] or r["start_time"] == item["start_time"]):
                    return r
            return None
        if item["type"] in ONE_PER_COURSE and same_type:
            return same_type[0]
        for r in same_type:
            if _similar(r["title"], item["title"]):
                return r
        if item["action"] == "cancel":
            for r in same_type:
                if r["date"] == item["date"]:
                    return r
        return None

    def apply(self, item: dict) -> Notice | None:
        # exactly this item is already known (on the timetable, or you ignored/undid/removed it)
        if item["action"] != "cancel" and self.store.same_event_exists(
            item["course"], item["type"], item["date"], item["start_time"], item["title"],
            history=self.rescanning,
        ):
            return None

        # Midsem/compre with a different date than the one stored = treat as a change
        target = None
        if item["action"] in ("update", "cancel") or item["type"] in ONE_PER_COURSE:
            target = self.find_target(item)
            if target and item["action"] == "add":
                if target["date"] == item["date"] and (target["start_time"] or None) == item["start_time"]:
                    return None   # same thing announced again
                item["action"] = "update"

        if item["action"] == "add" and self.store.same_event_exists(
            item["course"], item["type"], item["date"], item["start_time"], item["title"]
        ):
            return None

        if item["action"] == "cancel" and not target and self.store.conn.execute(
            "SELECT 1 FROM events WHERE action='cancel' AND IFNULL(course,'')=IFNULL(?,'') AND date=? "
            "AND IFNULL(start_time,'')=IFNULL(?,'') AND type=? AND status IN ('cancelled','pending')",
            (item["course"], item["date"], item["start_time"], item["type"]),
        ).fetchone():
            return None   # this cancellation is already recorded

        if target:
            item["replaces_id"] = target["id"]

        if item["confidence"] == "high":
            item["status"] = ACTIVE if item["action"] != "cancel" else "cancelled"
            eid = self.store.add_event(**item)
            return self._activate(eid, announce=True)

        item["status"] = PENDING
        eid = self.store.add_event(**item)
        return self._ask(eid)

    # ==================== actions (also used by the bot buttons) ====================
    def confirm(self, event_id: int) -> Notice:
        ev = self.store.get_event(event_id)
        if ev and ev["status"] == "expired":
            return Notice("info", event_id, "⌛ This one expired (its time passed), so I skipped it.")
        if not ev or ev["status"] != PENDING:
            return Notice("info", event_id, "That item was already handled.")
        self.store.update_event(event_id, status=ACTIVE if ev["action"] != "cancel" else "cancelled")
        return self._activate(event_id, announce=True)

    def ignore(self, event_id: int) -> Notice:
        ev = self.store.get_event(event_id)
        if ev and ev["status"] == PENDING:
            self.store.update_event(event_id, status="ignored")
            return Notice("info", event_id, f"❌ Ignored: {esc(ev['title'])}")
        return Notice("info", event_id, "That item was already handled.")

    def undo(self, event_id: int) -> Notice:
        ev = self.store.get_event(event_id)
        if not ev or ev["status"] not in (ACTIVE, "cancelled"):
            return Notice("info", event_id, "Nothing to undo.")
        self._cal_delete(ev["gcal_event_id"])
        self.store.update_event(event_id, status="undone", gcal_event_id=None)
        restored = ""
        if ev["replaces_id"]:
            old = self.store.get_event(ev["replaces_id"])
            if old and old["status"] in ("superseded", "cancelled"):
                self.store.update_event(old["id"], status=ACTIVE)
                self._cal_create(old["id"])
                restored = f"\n↩️ Restored: {esc(old['title'])} on {_nice_date(old['date'])}"
        return Notice("info", event_id, f"↩️ Undone: {esc(ev['title'])}{restored}")

    def edit(self, event_id: int, new_date: str, new_time: str | None) -> Notice:
        ev = self.store.get_event(event_id)
        if not ev:
            return Notice("info", event_id, "Item not found.")
        was_active = ev["status"] == ACTIVE
        if was_active:
            self._cal_delete(ev["gcal_event_id"])
        self.store.update_event(event_id, date=new_date, start_time=new_time,
                                end_time=None, confidence="high", low_reason="edited by you",
                                reminded_prep=0, reminded_soon=0, gcal_event_id=None)
        if was_active:
            self._cal_create(event_id)
            return Notice("info", event_id, "✏️ Updated:\n" + describe(self.store.get_event(event_id)))
        return self.confirm(event_id) if ev["status"] == PENDING else \
            Notice("info", event_id, "✏️ Updated:\n" + describe(self.store.get_event(event_id)))

    # ==================== helpers ====================
    def _activate(self, event_id: int, announce: bool) -> Notice:
        ev = self.store.get_event(event_id)
        old = self.store.get_event(ev["replaces_id"]) if ev["replaces_id"] else None
        if old and old["status"] == ACTIVE:
            self._cal_delete(old["gcal_event_id"])
            self.store.update_event(old["id"], status="cancelled" if ev["action"] == "cancel" else "superseded",
                                    gcal_event_id=None)

        if ev["action"] == "cancel":
            what = old["title"] if old else ev["title"]
            if not old and ev["type"] in ("class", "lab") and self.cal:
                # a regular weekly class: remove just that day's copy from Google Calendar
                try:
                    ids = self.cal.cancel_weekly(self.store, ev)
                    if ids:
                        self.store.update_event(event_id, gcal_event_id="inst:" + ",".join(ids))
                except Exception as e:
                    log.warning("Couldn't remove class from calendar: %s", e)
            text = f"🚫 Cancelled: {esc(with_course(ev['course'], what))} on {_nice_date(ev['date'])}{_t(ev['start_time'])}"
            return Notice("cancelled", event_id, text + _source(ev), [("↩️ Undo", f"undo:{event_id}")])

        synced = self._cal_create(event_id)
        ev = self.store.get_event(event_id)
        cal_note = "" if synced or not self.cal else "\n(⚠️ Calendar sync failed, will retry)"
        if old:
            text = (f"🔁 Changed: {esc(with_course(ev['course'], ev['title']))}\n"
                    f"   was {_nice_date(old['date'])}{_t(old['start_time'])} → now {_nice_date(ev['date'])}{_t(ev['start_time'])}")
            kind = "changed"
        else:
            text = "✅ Added:\n" + describe(ev)
            kind = "added"
        return Notice(kind, event_id, text + cal_note + _source(ev), [("↩️ Undo", f"undo:{event_id}")])

    def _ask(self, event_id: int) -> Notice:
        ev = self.store.get_event(event_id)
        verb = {"update": "Change", "cancel": "Cancel"}.get(ev["action"], "Found")
        extra = ""
        if ev["replaces_id"]:
            old = self.store.get_event(ev["replaces_id"])
            if old:
                extra = f"\n   (replaces {_nice_date(old['date'])}{_t(old['start_time'])})"
        text = (f"🤔 {verb}: please check\n{describe(ev)}{extra}\n"
                f"   Why I'm asking: {esc(ev['low_reason'])}" + _source(ev))
        return Notice("confirm", event_id, text,
                      [("✅ Add", f"add:{event_id}"), ("✏️ Edit", f"edit:{event_id}"), ("❌ Ignore", f"ignore:{event_id}")])

    def _cal_create(self, event_id: int) -> bool:
        if not self.cal:
            return False
        ev = self.store.get_event(event_id)
        try:
            gid = self.cal.create(ev)
            self.store.update_event(event_id, gcal_event_id=gid)
            return True
        except Exception as e:
            log.warning("Calendar create failed: %s", e)
            return False

    def _cal_delete(self, gid):
        if not (self.cal and gid):
            return
        if gid.startswith("inst:"):      # undoing a class cancellation -> bring the class back
            self.cal.restore_instances([i for i in gid[5:].split(",") if i])
        else:
            self.cal.delete(gid)

    # ==================== pending items: expire / nudge ====================
    def _starts_at(self, ev) -> datetime:
        d = datetime.fromisoformat(ev["date"]).date()
        t = datetime.strptime(ev["start_time"], "%H:%M").time() if ev["start_time"] else datetime.max.time()
        return datetime.combine(d, t, tzinfo=self.s.tz)

    def expire_pending(self) -> list:
        """Pending items whose time has passed are auto-declined (status 'expired')."""
        now, gone = self.s.now(), []
        for ev in self.store.pending():
            if self._starts_at(ev) <= now:
                self.store.update_event(ev["id"], status="expired")
                gone.append(ev)
        return gone

    def pending_due_soon(self, hours: float = 3) -> list:
        """Pending items starting within `hours` that haven't been nudged yet."""
        now, out = self.s.now(), []
        for ev in self.store.pending():
            start = self._starts_at(ev)
            if not ev["start_time"]:
                start = datetime.combine(start.date(), datetime.strptime("09:00", "%H:%M").time(), tzinfo=self.s.tz)
            if now < start <= now + timedelta(hours=hours) and not self.store.get_kv(f"nudged:{ev['id']}"):
                self.store.set_kv(f"nudged:{ev['id']}", "1")
                out.append(ev)
        return out

    # ==================== /cleanup ====================
    def cleanup_candidates(self):
        """Items with no course (where the old, looser rule let in fests, placements etc.)."""
        return self.store.conn.execute(
            "SELECT * FROM events WHERE course IS NULL AND status IN ('active','pending') ORDER BY date"
        ).fetchall()

    def cleanup_apply(self) -> int:
        rows = self.cleanup_candidates()
        for r in rows:
            if r["status"] == ACTIVE:
                self._cal_delete(r["gcal_event_id"])
            self.store.update_event(r["id"], status="removed", gcal_event_id=None)
        return len(rows)

    def retry_calendar_sync(self) -> list[Notice]:
        if not self.cal:
            return []
        for ev in self.store.upcoming(self.s.today().isoformat()):
            if not ev["gcal_event_id"] and ev["status"] == ACTIVE:
                self._cal_create(ev["id"])
        return []


# ==================== formatting ====================
TYPE_ICON = {"midsem": "📝", "compre": "📝", "exam": "📝", "quiz": "✏️", "deadline": "⏰",
             "class": "🏫", "lab": "🔬", "event": "📌"}


def esc(v) -> str:
    """Makes text safe for Telegram HTML messages."""
    return html.escape(str(v or ""), quote=False)


def describe(ev) -> str:
    icon = TYPE_ICON.get(ev["type"], "📌")
    line = f"{icon} <b>{esc(with_course(ev['course'], ev['title']))}</b>\n   📅 {_nice_date(ev['date'])}{_t(ev['start_time'])}"
    if ev["end_time"]:
        line += f"–{ev['end_time']}"
    if ev["venue"]:
        line += f"  📍 {esc(ev['venue'])}"
    if ev["notes"]:
        line += f"\n   📝 {esc(ev['notes'])}"
    return line


def _source(ev) -> str:
    from .validate import sender_name
    src = ev["source_file"] or "email"
    subj = ev["source_subject"] or ""
    who = sender_name(ev["source_sender"]) if "source_sender" in ev.keys() else ""
    who = f"{esc(who)} · " if who else ""
    return f"\n   📧 {who}“{esc(subj[:70])}”" + (f" ({esc(src)})" if src and src != "email body" else "")


def _nice_date(d: str) -> str:
    return datetime.fromisoformat(d).strftime("%a %d %b")


def _t(t) -> str:
    return f", {t}" if t else ""
