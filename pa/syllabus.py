"""Quiz / exam syllabi.

Professors mail the syllabus shortly before a quiz. Until then the planner guesses
(topics taught since the last quiz or midsem). When a syllabus arrives (by email, or
you type it on Telegram) it is:
  1. attached to the right quiz on the timetable,
  2. matched to your topics by the AI, then checked in code (only real topics of that course),
  3. used at once if the match is clear (with ↩️ Undo); otherwise you're asked ✅ first.
Saved in kv 'syllabus:<event id>' as JSON: topics, text, short, status (set / ask / undone / rejected).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta

from . import validate
from .config import ROOT

log = logging.getLogger(__name__)
SYSTEM = "You match a university syllabus to a student's topic list. You reply with valid JSON only."
TYPES = {"quiz", "midsem", "compre", "exam", "lab"}


def _esc(v) -> str:
    import html
    return html.escape(str(v or ""), quote=False)


def _num(title: str) -> str | None:
    """'M3 Quiz 2' / 'Quiz-II' / 'Q2' -> '2'."""
    t = (title or "").lower()
    m = re.search(r"(?:quiz|test|q)\s*[-#]?\s*(\d+|i{1,3}|iv)\b", t)
    if not m:
        return None
    roman = {"i": "1", "ii": "2", "iii": "3", "iv": "4"}
    return roman.get(m.group(1), m.group(1))


class SyllabusDesk:
    def __init__(self, assistant):
        self.a = assistant
        self.s = assistant.s
        self.store = assistant.store

    # ---------- the planner (for topics and lecture estimates) ----------
    @property
    def planner(self):
        p = getattr(self.a, "planner", None)
        if p is None:
            from .planner import Planner
            p = self.a.planner = Planner(self.s, self.store)
        return p

    # ---------- saved state ----------
    def get(self, event_id: int) -> dict | None:
        raw = self.store.get_kv(f"syllabus:{event_id}")
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            return None

    def put(self, event_id: int, info: dict):
        self.store.set_kv(f"syllabus:{event_id}", json.dumps(info))

    # ---------- which quiz is this syllabus for? ----------
    def find_event(self, course: str, type_: str, title: str, day: str | None):
        rows = self.store.conn.execute(
            "SELECT * FROM events WHERE course=? AND date>=? AND status IN ('active','pending') ORDER BY date",
            (course, self.s.today().isoformat())).fetchall()
        rows = [r for r in rows if r["type"] == type_ and not validate.MAKEUP_RE.search(r["title"] or "")]
        if not rows:
            return None, False
        if day:
            same = [r for r in rows if r["date"] == day]
            if same:
                return same[0], True
        n = _num(title)
        if n:
            hit = [r for r in rows if _num(r["title"]) == n]
            if hit:
                return hit[0], True
        if len(rows) == 1:
            return rows[0], not day          # the only upcoming one; unsure if the dates disagree
        return rows[0], False                  # nearest one, but ask

    # ---------- AI match ----------
    def match(self, ev, text: str) -> tuple[list[str], str, bool]:
        p = self.planner
        course = ev["course"]
        topics = [t for t in p.topics if t.course == course]
        if not topics or not self.a.llm:
            return [], "", False
        qd = date.fromisoformat(ev["date"])
        now_l, then_l = p.lecture_at(course, self.s.today()), p.lecture_at(course, qd - timedelta(days=1))
        earlier = []
        for r in p.earlier_evals(course, qd):
            lec = p.lecture_at(course, date.fromisoformat(r["date"]))
            earlier.append(f"{r['title']} on {r['date']}" + (f" (about lecture {lec:.0f} by then)" if lec else ""))
        lines = [f"{t.id} | {t.title} | {t.lec_from}-{t.lec_to} | {'yes' if t.pre_midsem else 'no'} | "
                 f"{', '.join(t.parts) or '-'}" + (f" | refs: {t.refs}" if t.refs else "") for t in topics]
        tmpl = (ROOT / "prompts" / "syllabus.txt").read_text(encoding="utf-8")
        prompt = tmpl.format(exam=ev["title"], exam_date=ev["date"], course=course, today=self.s.today().isoformat(),
                             taught_now=f"{now_l:.0f}" if now_l is not None else "unknown",
                             taught_then=f"{then_l:.0f}" if then_l is not None else "unknown",
                             earlier="; ".join(earlier) or "none known (dates not on the timetable)",
                             text=text.strip()[:3000], topics="\n".join(lines))
        from .extract import parse_json
        data = parse_json(self.a.llm.ask(SYSTEM, prompt, []))
        if not isinstance(data, dict):
            return [], "", False
        valid = {t.id for t in topics}
        ids = [i for i in dict.fromkeys(str(x).strip() for x in data.get("topic_ids") or []) if i in valid]
        ids = [t.id for t in topics if t.id in ids]                      # keep course order
        invented = len(ids) < len(data.get("topic_ids") or [])
        sure = bool(data.get("sure")) and bool(ids) and not invented
        short = str(data.get("short") or "").strip()[:120] or ", ".join(
            t.title.split(" (")[0] for t in topics if t.id in ids)
        return ids, short, sure

    # ---------- a syllabus arrives ----------
    def receive(self, cand: dict, source_text: str, source: str, from_user: bool = False):
        """cand = one item of the extraction's 'syllabi' list. Returns a Notice or None."""
        from .pipeline import Notice
        course, mine = validate.canonical_course(cand.get("course"), self.s.courses)
        type_ = str(cand.get("type") or "quiz").lower()
        text = str(cand.get("syllabus") or "").strip()
        if not course or not mine or type_ not in TYPES or len(text) < 3:
            return None
        day = str(cand.get("date") or "").strip() or None
        if day:
            try:
                datetime.strptime(day, "%Y-%m-%d")
            except ValueError:
                day = None
        ev, sure_event = self.find_event(course, type_, str(cand.get("exam") or ""), day)
        if not ev:
            return None
        old = self.get(ev["id"])
        if old and old.get("text", "").strip().lower() == text.lower() and old.get("status") in ("set", "rejected"):
            return None                                             # same syllabus mailed again
        quote = str(cand.get("source_quote") or "")
        quoted = from_user or quote_ok(quote, source_text)
        try:
            ids, short, sure_match = self.match(ev, text)
        except Exception as e:
            if type(e).__name__ == "QuotaExhausted":
                raise
            log.warning("Syllabus match failed: %s", str(e)[:200])
            ids, short, sure_match = [], "", False
        if not ids:
            if type_ == "lab":                    # lab syllabi are experiments, not lecture topics: nothing to plan
                return None
            return Notice("info", ev["id"],
                          f"📘 Got the <b>{_esc(ev['title'])}</b> syllabus but couldn't match it to your topics:\n"
                          f"<i>{_esc(text[:300])}</i>\nI'll keep using my best guess. "
                          "Tell me the topics in words if you like, e.g. <i>“M3 quiz 2 syllabus is Legendre and Bessel”</i>.")
        info = {"topics": ids, "short": short, "text": text[:600], "source": source[:120],
                "at": self.s.now().isoformat(timespec="minutes"), "prev": old}
        names = self._names(ids)
        auto = sure_event and sure_match and quoted and not from_user and ev["status"] == "active"
        if auto:
            info["status"] = "set"
            self.put(ev["id"], info)
            return Notice("syllabus", ev["id"],
                          f"📘 <b>{_esc(ev['title'])}</b> syllabus is out: {_esc(short)}\n{names}\n"
                          "I've updated your plan to match." + _src(source),
                          [("↩️ Undo", f"sylundo:{ev['id']}")])
        info["status"] = "ask"
        self.put(ev["id"], info)
        why = []
        if not sure_event:
            why.append("not sure which quiz it's for")
        if not sure_match:
            why.append("the syllabus is a bit vague")
        if not quoted:
            why.append("couldn't find the exact line in the email")
        if from_user:
            why.append("from your message")
        return Notice("confirm", ev["id"],
                      f"📘 <b>{_esc(ev['title'])}</b> ({_nice(ev['date'])}) syllabus, please check:\n"
                      f"<i>{_esc(text[:300])}</i>\nMy match: {names}\n"
                      f"   Why I'm asking: {_esc(', '.join(why) or 'to be safe')}" + _src(source),
                      [("✅ Use this", f"sylok:{ev['id']}"), ("❌ Keep guessing", f"sylno:{ev['id']}")])

    def _names(self, ids: list[str]) -> str:
        p = self.planner
        return "\n".join(f"   • {_esc(t.title)}" for t in p.topics if t.id in ids)

    # ---------- buttons ----------
    def confirm(self, event_id: int):
        from .pipeline import Notice
        info = self.get(event_id)
        if not info or info.get("status") != "ask":
            return Notice("info", event_id, "That syllabus was already handled.")
        info["status"] = "set"
        self.put(event_id, info)
        return Notice("syllabus", event_id, f"✅ Using this syllabus ({_esc(info.get('short'))}). Plan updated.",
                      [("↩️ Undo", f"sylundo:{event_id}")])

    def reject(self, event_id: int):
        from .pipeline import Notice
        info = self.get(event_id)
        if not info or info.get("status") != "ask":
            return Notice("info", event_id, "That syllabus was already handled.")
        self._restore(event_id, info, "rejected")
        return Notice("info", event_id, "❌ OK, not using it. Tell me the topics in words if you like.")

    def undo(self, event_id: int):
        from .pipeline import Notice
        info = self.get(event_id)
        if not info or info.get("status") != "set":
            return Notice("info", event_id, "Nothing to undo.")
        back = self._restore(event_id, info, "undone")
        return Notice("syllabus", event_id, "↩️ Undone. " + (
            f"Back to the earlier syllabus ({_esc(back.get('short'))})." if back else "Back to my best guess."))

    def _restore(self, event_id: int, info: dict, status: str) -> dict | None:
        prev = info.get("prev")
        if prev and prev.get("status") == "set":
            self.put(event_id, prev)                 # the earlier syllabus comes back
            return prev
        info["status"] = status
        self.put(event_id, info)
        return None

    # ---------- once: quizzes that already have a syllabus line in their notes ----------
    def backfill(self) -> list:
        out = []
        for r in self.store.upcoming(self.s.today().isoformat()):
            if r["type"] not in ("quiz", "exam", "midsem") or not r["course"] or self.get(r["id"]) is not None:
                continue
            notes = r["notes"] or ""
            if not re.search(r"syllabus|portion|chapter|covers?|includ", notes, re.I):
                continue
            n = self.receive({"course": r["course"], "type": r["type"], "exam": r["title"], "date": r["date"],
                              "syllabus": notes, "source_quote": notes}, notes,
                             r["source_subject"] or "saved quiz notes")
            if n:
                out.append(n)
            elif self.get(r["id"]) is None:
                self.put(r["id"], {"status": "none", "text": notes[:600]})   # don't retry every start
        return out


def quote_ok(quote: str, source: str) -> bool:
    return bool(quote) and validate.quote_in_source(quote, source)


def _nice(d: str) -> str:
    try:
        return datetime.strptime(d, "%Y-%m-%d").strftime("%a %d %b")
    except ValueError:
        return d


def _src(source: str) -> str:
    return f"\n   <i>From: {_esc(source)}</i>" if source else ""
