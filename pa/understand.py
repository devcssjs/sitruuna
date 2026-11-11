"""Understands anything you type to Sitruuna.

One AI call sorts your message into:
  progress     "only did MUX", "finished DC motors", "skipped the workout"
  change_plan  "replace workout with DD study", "move reading to 11pm", "make today lighter"
  action       "restart", "replan", "check mail"
  answer       "yes" / "no" to the question Sitruuna just asked
  date_change  "EM quiz 2 moved to 18 Nov 5pm"   (handled by the email rules, asks ✅ first)
  question     "what's my plan tomorrow?"
  task / chat  anything else, answered in one friendly line

Progress is checked in code (real topic, real parts, real block) before it's saved.
Clear messages are applied at once with an ↩️ Undo button; unclear ones ask ✅ first.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .config import ROOT
from .extract import parse_json
from .llm import QuotaExhausted
from .pipeline import esc
from .planner import KIND_ICON, STUDY_KINDS, Planner, fmt

log = logging.getLogger(__name__)
SYSTEM = "You understand a student's chat messages to his study assistant. You reply with valid JSON only."
QUESTIONS = {"plan_today", "plan_tomorrow", "plan_week", "timetable_today", "timetable_tomorrow", "week",
             "deadlines", "progress", "status", "pending", "help"}
ROUTINES = {"workout", "reading", "dsa", "project"}
ACTIONS = {"restart", "replan", "check_mail", "rate", "status", "sync_calendar", "sync_notion", "notion", "help"}
KINDS = {"study", "workout", "reading", "dsa", "project"}
DEFAULT_MIN = {"study": 50, "workout": 60, "reading": 45, "dsa": 60, "project": 60}


@dataclass
class Reply:
    kind: str                       # progress, ask, date_change, question, chat, error
    text: str = ""
    buttons: list = field(default_factory=list)
    question: str | None = None     # for kind == question
    replan: bool = False            # the plan should be refreshed
    action: str | None = None       # for kind == action
    answer: str | None = None       # for kind == answer ("yes" / "no")
    replan_days: list = field(default_factory=list)   # days to re-plan (lighter / heavier / focus)


class Interpreter:
    def __init__(self, assistant, planner: Planner, cal_getter=None):
        self.a = assistant
        self.p = planner
        self.s = assistant.s
        self.store = assistant.store
        self.cal = cal_getter or (lambda: None)

    # ==================== short chat memory ====================
    def remember(self, who: str, text: str):
        """Keep the last few messages so 'yes' or 'make it 2 hours' make sense."""
        import re
        try:
            log_ = json.loads(self.store.get_kv("chatlog") or "[]")
        except ValueError:
            log_ = []
        clean = re.sub(r"<[^>]+>", "", text or "").strip()[:300]
        if clean:
            log_.append([who, clean, self.s.now().isoformat(timespec="minutes")])
            self.store.set_kv("chatlog", json.dumps(log_[-8:]))

    def _chat(self) -> str:
        try:
            log_ = json.loads(self.store.get_kv("chatlog") or "[]")
        except ValueError:
            return "(none)"
        cutoff = (self.s.now() - timedelta(hours=3)).isoformat(timespec="minutes")
        lines = [f"{'Him' if w == 'you' else 'You (Sitruuna)'} at {t[11:16]}: {m}" for w, m, t in log_ if t >= cutoff]
        return "\n".join(lines) or "(none)"

    # ==================== entry point ====================
    def handle(self, text: str, reply_block_id: int | None = None, reply_text: str | None = None) -> Reply:
        if not self.a.llm:
            return Reply("error", "The AI isn't set up, so I can only follow commands. Try /help.")
        if self.a.ai_paused():
            return Reply("error", self.a._pause_text("My AI's daily limit is used up, so I can only follow commands") +
                         "\nMeanwhile, use the buttons or /status.")
        try:
            raw = self.a.llm.ask(SYSTEM, self._prompt(text, reply_block_id, reply_text), [])
            data = parse_json(raw)
        except QuotaExhausted as e:
            self.a._pause_ai(e.retry_seconds)
            return Reply("error", self.a._pause_text("My AI's daily limit is used up"))
        except Exception as e:
            log.warning("Couldn't understand message: %s", str(e)[:200])
            return Reply("error", "⚠️ I couldn't reach my AI just now. Try again in a minute, or use /status.")
        if not isinstance(data, dict):
            data = {}
        intent = str(data.get("intent") or "chat").lower()
        log.info("Message: %r -> %s", text[:200], json.dumps(data)[:800])

        if intent == "change_plan":
            return self.change(data)
        if intent == "action":
            act = data.get("action")
            return Reply("action", action=act if act in ACTIONS else "help")
        if intent == "answer":
            ans = str(data.get("answer") or "").lower()
            return Reply("answer", answer=ans if ans in ("yes", "no") else None)

        if intent == "date_change":
            return Reply("date_change")
        if intent == "question":
            q = data.get("question")
            return Reply("question", question=q if q in QUESTIONS else "help")
        if intent == "progress":
            return self.progress(data, reply_block_id)
        msg = str(data.get("reply") or "").strip() or "👍"
        if intent == "task":
            msg += "\n<i>(A to-do list comes with the Notion step. If it has a course and a date, tell me and I'll add it as a deadline.)</i>"
        return Reply("chat", esc(msg) if "<i>" not in msg else msg)

    # ==================== the AI prompt ====================
    def _prompt(self, text, reply_block_id, reply_text) -> str:
        now = self.s.now()
        lines = []
        for b in self.p.recent_blocks(now.date()) + self.p.blocks_on(now.date() + timedelta(days=1)):
            lines.append(f"{b.id} | {b.date} {fmt(b.start)}-{fmt(b.end)} | {b.kind}: "
                         f"{(b.course + ' · ') if b.course else ''}{b.title}"
                         f"{(' [topic ' + b.topic_id + ']') if b.topic_id and not b.topic_id.startswith('ev:') else ''} | {b.status}")
        topics = []
        for t in self.p.topics:
            done = self.p.parts_done(t.id)
            extra = f" (already done: {', '.join(done)})" if done and self.p.status(t) != "d" else ""
            topics.append(f"{t.id} | {t.course} | {t.title} | {', '.join(t.parts) or '-'}{extra}")
        ctx = ""
        if reply_block_id:
            ctx = f"HE IS REPLYING TO THE MESSAGE ABOUT BLOCK {reply_block_id}."
        elif reply_text:
            ctx = f'HE IS REPLYING TO THIS MESSAGE OF YOURS: """{reply_text[:400]}"""'
        tpl = (ROOT / "prompts" / "understand.txt").read_text(encoding="utf-8")
        return tpl.format(name=self.s.get("your_name", default="the student"),
                          now=now.strftime("%A %d %B %Y, %H:%M"), blocks="\n".join(lines) or "(none)",
                          chat=self._chat(), reply_context=ctx, courses=", ".join(self.s.courses.entries.keys()),
                          topics="\n".join(topics), message=text[:2000])

    # ==================== progress ====================
    def clean(self, data: dict, reply_block_id: int | None) -> dict:
        """Keep only what's real: known topics, their own part names, recent blocks."""
        today = self.s.today()
        recent = {b.id: b for b in self.p.recent_blocks(today)}
        out = {"progress": [], "routines": []}
        for it in data.get("progress") or []:
            if not isinstance(it, dict):
                continue
            t = self.p.topic(str(it.get("topic_id") or "").strip())
            if not t:
                continue
            st = str(it.get("status") or "partial").lower()
            st = st if st in ("done", "partial", "not_done") else "partial"
            parts = self.p.match_parts(t, it.get("parts_done"))
            bid = it.get("block_id")
            bid = int(bid) if str(bid).isdigit() and int(bid) in recent and recent[int(bid)].topic_id == t.id else None
            if bid is None and reply_block_id in recent and recent[reply_block_id].topic_id == t.id:
                bid = reply_block_id
            if bid is None:      # the most recent block for this topic that isn't marked yet
                cands = [b for b in recent.values() if b.topic_id == t.id and b.status in ("planned", "started", "skipped")
                         and (b.date < today.isoformat() or b.start <= self._now_min())]
                bid = cands[-1].id if cands else None
            mins = it.get("minutes")
            mins = int(mins) if isinstance(mins, (int, float)) and 0 < mins <= 360 else None
            out["progress"].append({"topic_id": t.id, "status": st, "parts": parts, "minutes": mins, "block_id": bid})
        for it in data.get("routines") or []:
            if isinstance(it, dict) and str(it.get("kind")).lower() in ROUTINES:
                out["routines"].append({"kind": str(it["kind"]).lower(), "done": bool(it.get("done", True)),
                                        "amount": it.get("amount")})
        return out

    def progress(self, data: dict, reply_block_id: int | None) -> Reply:
        plan = self.clean(data, reply_block_id)
        if not plan["progress"] and not plan["routines"]:
            return Reply("chat", "🤔 I got that you're telling me about your study, but I couldn't match it to a topic.\n"
                                 "Try naming it, e.g. <i>“DD: only did MUX”</i>, or use /status.")
        if data.get("sure") is False:
            pid = self._next_id("prop")
            self.store.set_kv(f"prop:{pid}", json.dumps(plan))
            return Reply("ask", "🤔 <b>Did you mean:</b>\n" + "\n".join(self.describe(plan)),
                         [("✅ Yes", f"uok:{pid}"), ("❌ No", f"uno:{pid}")])
        return self.apply(plan)

    def apply(self, plan: dict) -> Reply:
        today = self.s.today()
        snap = self._snapshot(plan)
        for it in plan["progress"]:
            t = self.p.topic(it["topic_id"])
            used_parts = False
            if it["status"] == "done":
                self.p.set_topic_status(t.id, "d", today)
            elif it["status"] == "partial":
                if it["parts"] and t.parts:
                    self.p.set_parts(t.id, sorted(set(self.p.parts_done(t.id)) | set(it["parts"]),
                                                  key=t.parts.index), today)
                    used_parts = True
                elif it["minutes"]:
                    self.p.add_progress(t.id, it["minutes"], today)
                elif self.p.status(t) == "n":
                    self.p.set_topic_status(t.id, "h", today)
            if it["block_id"]:
                new = {"done": "done", "partial": "partial", "not_done": "skipped"}[it["status"]]
                if t.parts and self.p.parts_left(t) == [] and it["status"] != "not_done":
                    new = "done"
                self.p.set_status(it["block_id"], new, count=False)
            elif it["minutes"] and it["status"] == "done" and not used_parts:
                self.p.add_progress(t.id, it["minutes"], today)
        for r in plan["routines"]:
            b = next((x for x in reversed(self.p.recent_blocks(today, days=1))
                      if x.kind == r["kind"] and x.status in ("planned", "started", "skipped")), None)
            if b:
                self.p.set_status(b.id, "done" if r["done"] else "skipped")
                r["block_id"] = b.id
        uid = self._next_id("undo")
        self.store.set_kv(f"undo:{uid}", json.dumps(snap))
        return Reply("progress", "📝 <b>Got it:</b>\n" + "\n".join(self.describe(plan, applied=True)),
                     [("↩️ Undo", f"uundo:{uid}")], replan=True)

    def describe(self, plan: dict, applied: bool = False) -> list[str]:
        lines = []
        for it in plan["progress"]:
            t = self.p.topic(it["topic_id"])
            name = f"{t.course} · {t.title}"
            if it["status"] == "done":
                line = f"✅ {esc(name)}: done"
            elif it["status"] == "not_done":
                line = f"❌ {esc(name)}: not done"
            elif it["parts"]:
                line = f"🟡 {esc(name)}: {esc(', '.join(it['parts']))} done"
                if applied:
                    left = self.p.parts_left(t)
                    line += f"\n      left: {esc(', '.join(left))}" if left else "\n      all parts done ✅"
            else:
                line = f"🟡 {esc(name)}: partly done" + (f" ({it['minutes']} min)" if it["minutes"] else "")
            if it["block_id"]:
                b = self.p.get(it["block_id"])
                if b:
                    st = b.status if applied else {"not_done": "skipped"}.get(it["status"], it["status"])
                    word = {"done": "done", "partial": "partly done", "skipped": "not done"}.get(st, st)
                    line += f"\n      {b.date[5:]} {fmt(b.start)} block → {word}"
            lines.append(line)
        for r in plan["routines"]:
            icon = {"workout": "🏋", "reading": "📖", "dsa": "💻", "project": "🚀"}[r["kind"]]
            amt = f" ({esc(r['amount'])})" if r.get("amount") else ""
            lines.append(f"{icon} {r['kind'].upper() if r['kind'] == 'dsa' else r['kind'].capitalize()}{amt}: "
                         f"{'done' if r['done'] else 'skipped'}")
        return lines

    # ==================== plan changes ====================
    def _day(self, v) -> date | None:
        today = self.s.today()
        v = str(v or "today").strip().lower()
        if v in ("today", "tonight", ""):
            return today
        if v == "tomorrow":
            return today + timedelta(days=1)
        try:
            d = date.fromisoformat(v[:10])
        except ValueError:
            return None
        return d if today <= d <= today + timedelta(days=7) else None

    @staticmethod
    def _hm(v) -> int | None:
        import re
        m = re.match(r"^(\d{1,2}):(\d{2})", str(v or "").strip())
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            return None
        return int(m.group(1)) * 60 + int(m.group(2))

    def clean_changes(self, data: dict) -> tuple[list[dict], str | None]:
        """Checks every requested change against real blocks, kinds, courses and topics.
        Returns (changes, problem) — problem is a question to ask you when something is unclear."""
        courses = set(self.s.courses.entries.keys())
        out = []
        for c in data.get("changes") or []:
            if not isinstance(c, dict):
                continue
            op = str(c.get("op") or "").lower()
            day = self._day(c.get("day"))
            kind = str(c.get("kind") or "").lower() or None
            course = c.get("course") if c.get("course") in courses else None
            tid = c.get("topic_id") if self.p.topic(str(c.get("topic_id") or "")) else None
            if tid:
                course = self.p.topic(tid).course
            if course and not kind:
                kind = "study"
            if kind and kind not in KINDS:
                kind = None
            mins = c.get("minutes")
            mins = int(mins) if isinstance(mins, (int, float)) and 10 <= mins <= 300 else None
            start = self._hm(c.get("start"))
            b = None
            if str(c.get("block_id") or "").isdigit():
                b = self.p.get(int(c["block_id"]))
            if op in ("replace", "move", "remove"):
                if not b or b.status in ("done", "skipped") and op != "remove":
                    return [], ("Which block do you mean? Reply to that block's message, or say its time "
                                "(e.g. <i>“replace the 14:00 workout with DD”</i>).")
                if b.date < self.s.today().isoformat():
                    return [], "That block is already in the past, so I can't change it."
            if op == "replace":
                if not kind:
                    return [], "Replace it with what? e.g. <i>“DD study”</i>, <i>“reading”</i>."
                out.append({"op": op, "block_id": b.id, "kind": kind, "course": course, "topic_id": tid})
            elif op == "move":
                if start is None:
                    return [], "Move it to what time? e.g. <i>“move reading to 23:00”</i>."
                out.append({"op": op, "block_id": b.id, "start": start,
                            "day": (day or date.fromisoformat(b.date)).isoformat()})
            elif op == "add":
                if not kind or not day:
                    return [], "Add what, and when? e.g. <i>“add an hour of EM tonight”</i>."
                out.append({"op": op, "kind": kind, "course": course, "topic_id": tid, "day": day.isoformat(),
                            "start": start, "minutes": mins or DEFAULT_MIN[kind]})
            elif op == "remove":
                out.append({"op": op, "block_id": b.id})
            elif op in ("lighter", "heavier"):
                if not day:
                    return [], "Which day? Today or tomorrow?"
                out.append({"op": op, "day": day.isoformat(), "minutes": mins or 60})
            elif op == "focus":
                if not course:
                    return [], "Which course should get more time?"
                days = c.get("days")
                days = int(days) if isinstance(days, (int, float)) and 1 <= days <= 21 else 7
                out.append({"op": op, "course": course, "days": days})
        if not out:
            return [], "I got that you want to change your plan, but not what exactly. Could you say it another way?"
        return out, None

    def change(self, data: dict) -> Reply:
        changes, problem = self.clean_changes(data)
        if problem:
            self.remember("sitruuna", problem)
            return Reply("chat", "🤔 " + problem)
        plan = {"changes": changes}
        if data.get("sure") is False:
            pid = self._next_id("prop")
            self.store.set_kv(f"prop:{pid}", json.dumps(plan))
            text = "🤔 <b>Change your plan like this?</b>\n" + "\n".join(self._preview(changes))
            self.remember("sitruuna", text)
            return Reply("ask", text, [("✅ Yes", f"uok:{pid}"), ("❌ No", f"uno:{pid}")])
        return self.apply_changes(plan)

    def _preview(self, changes) -> list[str]:
        out = []
        for c in changes:
            b = self.p.get(c["block_id"]) if c.get("block_id") else None
            what = (f"{c['course']} study" if c.get("course") else c.get("kind") or "")
            if c["op"] == "replace":
                out.append(f"🔁 {fmt(b.start)}–{fmt(b.end)} {esc(b.title)} → {esc(what)}")
            elif c["op"] == "move":
                out.append(f"➡️ {esc(b.title)} → {fmt(c['start'])}")
            elif c["op"] == "add":
                out.append(f"➕ {c['minutes']} min {esc(what)}" + (f" at {fmt(c['start'])}" if c["start"] is not None else ""))
            elif c["op"] == "remove":
                out.append(f"➖ {fmt(b.start)} {esc(b.title)}")
            elif c["op"] in ("lighter", "heavier"):
                out.append(f"{'🪶 Lighter' if c['op'] == 'lighter' else '💪 Heavier'}: "
                           f"{'−' if c['op'] == 'lighter' else '+'}{c['minutes']} min study on {c['day']}")
            elif c["op"] == "focus":
                out.append(f"🎯 Extra priority for {c['course']} for {c['days']} days")
        return out

    def apply_changes(self, plan: dict) -> Reply:
        cal = self.cal()
        snap = {"type": "changes", "blocks": {}, "created": [], "kv": {}}
        lines, replan_days = [], set()

        def keep_row(bid):
            if str(bid) not in snap["blocks"]:
                r = self.store.conn.execute("SELECT * FROM plan_blocks WHERE id=?", (bid,)).fetchone()
                snap["blocks"][str(bid)] = dict(r) if r else None

        def keep_kv(k):
            if k not in snap["kv"]:
                snap["kv"][k] = self.store.get_kv(k)

        for c in plan["changes"]:
            op = c["op"]
            if op == "replace":
                old = self.p.get(c["block_id"])
                keep_row(old.id)
                keep_kv(f"nokind:{old.date}")
                b = self.p.replace_block(old.id, c["kind"], c.get("course"), c.get("topic_id"), cal)
                lines.append(f"🔁 {fmt(b.start)}–{fmt(b.end)}  {KIND_ICON.get(old.kind, '')} {esc(old.title)} → "
                             f"{KIND_ICON.get(b.kind, '')} {esc(self._name(b))}{self._left(b)}")
            elif op == "move":
                old = self.p.get(c["block_id"])
                keep_row(old.id)
                b = self.p.move_block(old.id, c["start"], cal)
                if c["day"] != old.date:
                    self.store.conn.execute("UPDATE plan_blocks SET date=? WHERE id=?", (c["day"], old.id))
                    self.store.conn.commit()
                    b = self.p.get(old.id)
                clash = self._clash(b)
                lines.append(f"➡️ {KIND_ICON.get(b.kind, '')} {esc(self._name(b))}: {fmt(old.start)} → "
                             f"{fmt(b.start)}–{fmt(b.end)}" + (f"\n      ⚠️ overlaps {clash}" if clash else ""))
            elif op == "add":
                d = date.fromisoformat(c["day"])
                after = self._now_min() if d == self.s.today() else 0
                b = self.p.add_block(d, c["kind"], c["minutes"], c["start"], c.get("course"), c.get("topic_id"),
                                     after=after, cal=cal)
                if not b:
                    lines.append(f"⚠️ No free {c['minutes']} min left on {c['day']} for that. Tell me a time to use.")
                    continue
                snap["created"].append(b.id)
                clash = self._clash(b)
                lines.append(f"➕ {b.date[5:]} {fmt(b.start)}–{fmt(b.end)}  {KIND_ICON.get(b.kind, '')} "
                             f"{esc(self._name(b))}{self._left(b)}" + (f"\n      ⚠️ overlaps {clash}" if clash else ""))
            elif op == "remove":
                old = self.p.get(c["block_id"])
                keep_row(old.id)
                keep_kv(f"nokind:{old.date}")
                self.p.remove_block(old.id, cal)
                lines.append(f"➖ {old.date[5:]} {fmt(old.start)}  {KIND_ICON.get(old.kind, '')} {esc(old.title)}: removed")
            elif op in ("lighter", "heavier"):
                k = f"capdelta:{c['day']}"
                keep_kv(k)
                delta = int(self.store.get_kv(k) or 0) + (-c["minutes"] if op == "lighter" else c["minutes"])
                self.store.set_kv(k, str(delta))
                replan_days.add(c["day"])
                lines.append(("🪶 Lighter" if op == "lighter" else "💪 Heavier") +
                             f" {c['day'][5:]}: {'−' if op == 'lighter' else '+'}{c['minutes']} min of study")
            elif op == "focus":
                k = f"focus:{c['course']}"
                keep_kv(k)
                until = (self.s.today() + timedelta(days=c["days"] - 1)).isoformat()
                self.store.set_kv(k, until)
                replan_days |= {self.s.today().isoformat(), (self.s.today() + timedelta(days=1)).isoformat()}
                lines.append(f"🎯 {c['course']} gets extra priority until {until[5:]}")
        uid = self._next_id("undo")
        self.store.set_kv(f"undo:{uid}", json.dumps(snap))
        text = "📅 <b>Plan changed:</b>\n" + "\n".join(lines) + "\n<i>Calendar updated. Your changes stay put when I re-plan.</i>"
        self.remember("sitruuna", text)
        return Reply("progress", text, [("↩️ Undo", f"uundo:{uid}")], replan_days=sorted(replan_days))

    def _undo_changes(self, snap: dict) -> Reply:
        cal = self.cal()
        c = self.store.conn
        for bid in snap["created"]:
            b = self.p.get(bid)
            if b and cal and b.gcal_event_id:
                cal.delete(b.gcal_event_id)
            c.execute("DELETE FROM plan_blocks WHERE id=?", (bid,))
        for bid, row in snap["blocks"].items():
            cur = self.p.get(int(bid))
            if cur and cal and cur.gcal_event_id:
                cal.delete(cur.gcal_event_id)
            if row:
                cols = [k for k in row if k not in ("id", "gcal_event_id")]
                c.execute(f"UPDATE plan_blocks SET {', '.join(k + '=?' for k in cols)}, gcal_event_id=NULL WHERE id=?",
                          (*[row[k] for k in cols], int(bid)))
        c.commit()
        for bid, row in snap["blocks"].items():
            b = self.p.get(int(bid))
            if b and cal and b.status in ("planned", "started"):
                self.p._cal_create(cal, b)
        days = set()
        for k, v in snap["kv"].items():
            self.store.set_kv(k, v or "")
            if k.startswith("capdelta:"):
                days.add(k.split(":", 1)[1])
        return Reply("progress", "↩️ Undone: your plan is back as it was.", replan_days=sorted(days))

    def _name(self, b) -> str:
        return f"{b.course} · {b.title}" if b.course and not b.title.startswith(b.course) else b.title

    def _left(self, b) -> str:
        t = self.p.topic(b.topic_id) if b.topic_id else None
        left = self.p.parts_left(t) if t else []
        return f"\n      left to do: {esc(', '.join(left))}" if left and len(left) < len(t.parts) else ""

    def _clash(self, b) -> str:
        d = date.fromisoformat(b.date)
        others = [x for x in self.p.blocks_on(d, statuses=("planned", "started")) if x.id != b.id
                  and x.start < b.end and b.start < x.end]
        busy = [(s_, e_) for s_, e_ in self.p.busy(d) if s_ < b.end and b.start < e_]
        if others:
            return f"{others[0].title} at {fmt(others[0].start)}"
        if busy:
            return f"a class or meal at {fmt(busy[0][0])}"
        return ""

    # ==================== confirm / undo ====================
    def confirm(self, pid: int) -> Reply:
        raw = self.store.get_kv(f"prop:{pid}")
        if not raw:
            return Reply("chat", "That was already handled.")
        self.store.set_kv(f"prop:{pid}", "")
        plan = json.loads(raw)
        return self.apply_changes(plan) if "changes" in plan else self.apply(plan)

    def reject(self, pid: int) -> Reply:
        self.store.set_kv(f"prop:{pid}", "")
        return Reply("chat", "👌 Ignored. Tell me again in other words, or use /status.")

    def undo(self, uid: int) -> Reply:
        raw = self.store.get_kv(f"undo:{uid}")
        if not raw:
            return Reply("chat", "Nothing to undo.")
        snap = json.loads(raw)
        if snap.get("type") == "changes":
            self.store.set_kv(f"undo:{uid}", "")
            return self._undo_changes(snap)
        c = self.store.conn
        for tid, row in snap["topics"].items():
            if row is None:
                c.execute("DELETE FROM topic_progress WHERE topic_id=?", (tid,))
            else:
                c.execute("INSERT INTO topic_progress(topic_id, done_min, last_studied, parts_done) VALUES(?,?,?,?) "
                          "ON CONFLICT(topic_id) DO UPDATE SET done_min=excluded.done_min, "
                          "last_studied=excluded.last_studied, parts_done=excluded.parts_done",
                          (tid, row["done_min"], row["last_studied"], row["parts_done"]))
        for bid, st in snap["blocks"].items():
            c.execute("UPDATE plan_blocks SET status=? WHERE id=?", (st, int(bid)))
        c.commit()
        self.store.set_kv(f"undo:{uid}", "")
        return Reply("progress", "↩️ Undone.", replan=True)

    # ==================== helpers ====================
    def _snapshot(self, plan: dict) -> dict:
        c = self.store.conn
        topics, blocks = {}, {}
        today = self.s.today()
        for it in plan["progress"]:
            r = c.execute("SELECT done_min, last_studied, parts_done FROM topic_progress WHERE topic_id=?",
                          (it["topic_id"],)).fetchone()
            topics[it["topic_id"]] = dict(r) if r else None
            if it["block_id"]:
                b = self.p.get(it["block_id"])
                blocks[it["block_id"]] = b.status if b else "planned"
        for b in self.p.recent_blocks(today, days=1):
            if b.kind in ROUTINES:
                blocks[b.id] = b.status
        return {"topics": topics, "blocks": {str(k): v for k, v in blocks.items()}}

    def _next_id(self, kind: str) -> int:
        n = int(self.store.get_kv(f"{kind}_seq") or 0) + 1
        self.store.set_kv(f"{kind}_seq", str(n))
        return n

    def _now_min(self) -> int:
        n = self.s.now()
        return n.hour * 60 + n.minute
