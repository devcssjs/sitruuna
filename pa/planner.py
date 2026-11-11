"""The study planner.

Every day it:
  1. works out your free time (your day minus classes you attend, exams and meals),
  2. ranks topics by: how big the exam is x how soon x how weak you are x how much is left,
  3. fills your two study windows with 50-minute blocks (up to the day's limit),
  4. fits workout, reading, DSA and your project into the remaining free time.

The day before an exam is revision only. Topics come from data/topics.csv (built
from the course handouts); progress is kept in the database.
"""
from __future__ import annotations

import csv
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .config import DATA_DIR, Settings, data_file
from .gcal import DAYS, class_matches_cancel, load_weekly_classes
from .store import Store

STUDY_KINDS = {"study", "revise", "pyq", "prep"}
KIND_ICON = {"study": "📚", "revise": "🔁", "pyq": "📝", "prep": "🛠", "workout": "🏋",
             "reading": "📖", "dsa": "💻", "project": "🚀"}
# how much each kind of evaluation counts when ranking (roughly its % of the grade)
LEARNED = 0.6          # "Done" = 60% of a topic's time (learned); the rest is practice/revision
STATUS_ICON = {"d": "✅", "h": "🟡", "n": "❌"}
BLOCK_STATUSES = ("planned", "started", "partial", "done", "skipped")
WEIGHTS = {"compre": 40, "midsem": 30, "exam": 20, "quiz": 12, "lab": 8, "deadline": 6}
EXAM_TYPES = {"compre", "midsem", "exam", "quiz"}
PLANNER_VERSION = "2026-11-06-quizzes"   # change it when planning rules change: today/tomorrow are re-planned on start
QUIZ_GUARANTEE_DAYS = 6     # from this many days before a quiz: at least one block a day on its topics
UNGRADED = re.compile(r"not (?:be )?(?:a )?part of (?:the )?(?:course )?evaluation|not (?:be )?(?:graded|evaluated)"
                      r"|ungraded|no marks|won'?t (?:be )?(?:graded|evaluated|counted)", re.I)


# ---------------- small time helpers ----------------
def hm(s: str) -> int:
    h, m = str(s).strip().split(":")
    return int(h) * 60 + int(m)


def fmt(m: int) -> str:
    m = min(m, 24 * 60 - 1)
    return f"{m // 60:02d}:{m % 60:02d}"


def rng(s: str) -> tuple[int, int]:
    a, b = str(s).split("-")
    return hm(a), hm(b)


def subtract(intervals: list[tuple[int, int]], busy: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out = intervals
    for bs, be in busy:
        nxt = []
        for s, e in out:
            if be <= s or bs >= e:
                nxt.append((s, e))
            else:
                if bs > s:
                    nxt.append((s, bs))
                if be < e:
                    nxt.append((be, e))
        out = nxt
    return [(s, e) for s, e in out if e - s > 0]


def intersect(intervals, s0, e0):
    return [(max(s, s0), min(e, e0)) for s, e in intervals if min(e, e0) > max(s, s0)]


# ---------------- topics ----------------
@dataclass
class Topic:
    id: str
    course: str
    title: str
    lec_from: int
    lec_to: int
    refs: str
    practice: str
    pre_midsem: bool       # part of the midsem syllabus (quizzes after the midsem skip these)
    parts: list = field(default_factory=list)   # sub-topics, e.g. MSI = comparators, decoders, encoders, MUX, DEMUX

    @property
    def est_min(self) -> int:
        return max(60, (self.lec_to - self.lec_from + 1) * 60)


def load_topics(path=None) -> list[Topic]:
    p = path or data_file("topics.csv", DATA_DIR)
    if not p.exists():
        return []
    out = []
    with open(p, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            try:
                out.append(Topic(r["id"].strip(), r["course"].strip(), r["topic"].strip(),
                                 int(r["lec_from"]), int(r["lec_to"]), (r.get("refs") or "").strip(),
                                 (r.get("practice") or "").strip(),
                                 (r.get("midsem") or r.get("studied") or "").strip().lower() in ("yes", "y", "true", "1"),
                                 [x.strip() for x in (r.get("parts") or "").split("|") if x.strip()]))
            except (KeyError, ValueError):
                continue
    return out


@dataclass
class Block:
    date: str
    start: int
    end: int
    kind: str
    title: str
    course: str | None = None
    topic_id: str | None = None
    detail: str = ""
    status: str = "planned"
    id: int | None = None
    gcal_event_id: str | None = None
    locked: bool = False            # you changed/added it yourself: re-plans keep it

    @property
    def is_study(self) -> bool:
        return self.kind in STUDY_KINDS

    def line(self) -> str:
        icon = KIND_ICON.get(self.kind, "•")
        head = f"{self.course} · {self.title}" if self.course and not self.title.startswith(self.course) else self.title
        return f"{fmt(self.start)}–{fmt(self.end)}  {icon} {head}"


@dataclass
class Exam:
    id: int
    course: str
    type: str
    date: date
    title: str
    weight: float
    start: int | None = None        # minutes after midnight, if the time is known
    notes: str = ""


# ---------------- the planner ----------------
class Planner:
    def __init__(self, settings: Settings, store: Store, topics: list[Topic] | None = None):
        self.s = settings
        self.store = store
        self.p = settings.get("planner", default={}) or {}
        self.per = settings.get("personal", default={}) or {}
        self.topics = topics if topics is not None else load_topics()
        self.block = int(self.p.get("block_minutes", 50))
        self.brk = int(self.p.get("break_minutes", 10))

    # ======== settings ========
    def weekend(self, d: date) -> bool:
        return d.weekday() >= 5

    def crunch(self, d: date) -> bool:
        c = self.p.get("crunch_from")
        return bool(c) and d >= date.fromisoformat(str(c))

    def cap(self, d: date, sleep: str | None = None) -> int:
        m = self.p.get("max_study_minutes", {}) or {}
        base = int(m.get("weekend" if self.weekend(d) else "weekday", 240 if self.weekend(d) else 180))
        if self.crunch(d):
            base = max(base, int(m.get("crunch", 300)))
        if sleep == "bad":
            base -= 60
        base += int(self.store.get_kv(f"capdelta:{d.isoformat()}") or 0)      # "make today lighter"
        return max(base, self.block)

    def windows(self, d: date) -> list[tuple[int, int]]:
        w = self.p.get("windows", {}) or {}
        key = "weekend" if self.weekend(d) else "weekday"
        default = ["10:00-13:00", "17:00-19:00"] if key == "weekend" else ["18:00-20:00", "21:00-23:30"]
        return [rng(x) for x in (w.get(key) or default)]

    def day_bounds(self, d: date) -> tuple[int, int]:
        start = hm(self.p.get("day_start", "07:00"))
        if self.weekend(d):
            end = hm(self.p.get("weekend_day_end", "19:00"))   # weekend evenings stay free
        else:
            end = hm(self.p.get("day_end", "23:59"))
        return start, end

    # ======== progress & ratings ========
    def done_min(self, topic_id: str) -> int:
        r = self.store.conn.execute("SELECT done_min FROM topic_progress WHERE topic_id=?", (topic_id,)).fetchone()
        return int(r["done_min"]) if r else 0

    def topic(self, topic_id: str) -> Topic | None:
        return next((x for x in self.topics if x.id == topic_id), None)

    def parts_done(self, topic_id: str) -> list[str]:
        import json
        r = self.store.conn.execute("SELECT parts_done FROM topic_progress WHERE topic_id=?", (topic_id,)).fetchone()
        try:
            return json.loads(r["parts_done"]) if r and r["parts_done"] else []
        except ValueError:
            return []

    def match_parts(self, t: Topic, names) -> list[str]:
        """Your words -> the topic's own part names ('mux' -> 'MUX', 'decoder' -> 'Decoders')."""
        import re
        norm = lambda x: re.sub(r"[^a-z0-9 ]", "", str(x).lower()).strip().rstrip("s")
        out = []
        for n in names or []:
            k = norm(n)
            if not k:
                continue
            hits = [p for p in t.parts if norm(p) == k]                      # exact: "mux" = "MUX"
            if not hits:                                                     # whole words only: "mux" never = "DEMUX"
                hits = [p for p in t.parts if re.search(rf"\b{re.escape(k)}", norm(p))
                        or re.search(rf"\b{re.escape(norm(p))}\b", k)]
            out += [p for p in hits if p not in out]
        return out

    def set_parts(self, topic_id: str, done: list[str], day: date | None = None) -> list[str]:
        """Remember which parts are done; progress = share of parts done."""
        import json
        t = self.topic(topic_id)
        if not t or not t.parts:
            return []
        done = [p for p in t.parts if p in done]
        mins = int(LEARNED * t.est_min * len(done) / len(t.parts))
        if 0 < len(done) < len(t.parts):
            mins = min(mins, int(LEARNED * t.est_min) - 1)      # partly done never counts as fully done
        self.store.conn.execute(
            "INSERT INTO topic_progress(topic_id, done_min, last_studied, parts_done) VALUES(?,?,?,?) "
            "ON CONFLICT(topic_id) DO UPDATE SET done_min=excluded.done_min, parts_done=excluded.parts_done, "
            "last_studied=COALESCE(excluded.last_studied, last_studied)",
            (t.id, mins, day.isoformat() if day and done else None, json.dumps(done)))
        self.store.conn.commit()
        return done

    def parts_left(self, t: Topic) -> list[str]:
        if not t.parts:
            return []
        if self.status(t) == "d":
            return []
        done = self.parts_done(t.id)
        return [p for p in t.parts if p not in done]

    def last_studied(self, topic_id: str) -> date | None:
        r = self.store.conn.execute("SELECT last_studied FROM topic_progress WHERE topic_id=?", (topic_id,)).fetchone()
        return date.fromisoformat(r["last_studied"]) if r and r["last_studied"] else None

    def add_progress(self, topic_id: str, minutes: int, day: date):
        self.store.conn.execute(
            "INSERT INTO topic_progress(topic_id, done_min, last_studied) VALUES(?,?,?) "
            "ON CONFLICT(topic_id) DO UPDATE SET done_min=done_min+excluded.done_min, last_studied=excluded.last_studied",
            (topic_id, minutes, day.isoformat()))
        self.store.conn.commit()

    def confidence(self, course: str) -> int:
        v = self.store.get_kv(f"conf:{course}")
        return int(v) if v and v.isdigit() else 3

    def set_confidence(self, course: str, value: int):
        self.store.set_kv(f"conf:{course}", str(max(1, min(5, int(value)))))

    def remaining(self, t: Topic) -> int:
        return max(0, t.est_min - self.done_min(t.id))

    # ======== what's taught when ========
    def available(self, t: Topic, d: date) -> bool:
        taught = (self.p.get("taught_upto", {}) or {}).get(t.course)
        if taught is None or t.lec_from <= int(taught):
            return True
        last = int((self.p.get("last_lecture", {}) or {}).get(t.course, 42))
        today = self.s.today()
        end = date.fromisoformat(str(self.s.get("semester_end", default=(today + timedelta(days=28)).isoformat())))
        span = max(1, last - int(taught))
        frac = min(1.0, (t.lec_from - int(taught)) / span)
        return d >= today + timedelta(days=round(frac * max(0, (end - today).days)))

    # ======== exams ========
    def exams(self, d: date) -> list[Exam]:
        out = []
        for r in self.store.upcoming(d.isoformat()):
            if not r["course"] or r["type"] not in WEIGHTS:
                continue
            st = hm(r["start_time"]) if r["start_time"] else None
            out.append(Exam(r["id"], r["course"], r["type"], date.fromisoformat(r["date"]), r["title"],
                            WEIGHTS[r["type"]], st, r["notes"] or ""))
        return out

    def midsem_date(self, course: str) -> date | None:
        r = self.store.conn.execute(
            "SELECT date FROM events WHERE course=? AND type='midsem' AND status IN ('active','superseded') "
            "ORDER BY date DESC LIMIT 1", (course,)).fetchone()
        return date.fromisoformat(r["date"]) if r else None

    # ---- what a quiz covers: the professor's syllabus once it's mailed, a best guess before that ----
    def lecture_at(self, course: str, day: date) -> float | None:
        """Roughly how many lectures of a course are taught by `day` (spread evenly till semester end)."""
        taught = (self.p.get("taught_upto", {}) or {}).get(course)
        if taught is None:
            return None
        last = int((self.p.get("last_lecture", {}) or {}).get(course, 42))
        today = self.s.today()
        end = date.fromisoformat(str(self.s.get("semester_end", default=(today + timedelta(days=28)).isoformat())))
        rate = max(0, last - int(taught)) / max(1, (end - today).days)
        return max(0.0, min(float(last), int(taught) + rate * (day - today).days))

    def syllabus_info(self, event_id: int) -> dict | None:
        """The saved syllabus for an exam (or for the exam it replaced, if its date moved)."""
        seen = set()
        eid = event_id
        while eid and eid not in seen:
            seen.add(eid)
            raw = self.store.get_kv(f"syllabus:{eid}")
            if raw:
                try:
                    info = json.loads(raw)
                except ValueError:
                    info = None
                if info and info.get("status") == "set":
                    return info
            r = self.store.conn.execute("SELECT replaces_id FROM events WHERE id=?", (eid,)).fetchone()
            eid = r["replaces_id"] if r else None
        return None

    def syllabus(self, e: Exam) -> list[str] | None:
        info = self.syllabus_info(e.id)
        return list(info.get("topics") or []) if info else None

    def earlier_evals(self, course: str, before: date) -> list:
        rows = self.store.conn.execute(
            "SELECT * FROM events WHERE course=? AND type IN ('quiz','midsem') AND date<? "
            "AND status IN ('active','superseded') ORDER BY date", (course, before.isoformat())).fetchall()
        return [r for r in rows if not re.search(r"make[- ]?up", r["title"] or "", re.I)]

    def guess_covers(self, e: Exam, t: Topic) -> bool:
        """No syllabus yet: topics taught since the last quiz or midsem (whichever is later), up to the quiz."""
        if not self.available(t, e.date - timedelta(days=1)):
            return False
        if e.type != "quiz":
            return True
        if t.pre_midsem:
            return False
        for r in self.earlier_evals(t.course, e.date):
            info = self.syllabus_info(r["id"])
            if info and t.id in (info.get("topics") or []):
                return False                                   # an earlier quiz already covered it
            if r["type"] == "quiz" and r["date"] < e.date.isoformat():
                upto = self.lecture_at(t.course, date.fromisoformat(r["date"]))
                if upto is not None and t.lec_to <= upto:
                    return False
        return True

    def covers(self, e: Exam, t: Topic) -> bool:
        if e.course != t.course:
            return False
        syl = self.syllabus(e)
        if syl is not None:
            return t.id in syl
        if e.type not in EXAM_TYPES:
            return False
        if e.type in ("compre", "exam"):
            return True
        return self.guess_covers(e, t)

    def is_graded(self, e: Exam) -> bool:
        return not UNGRADED.search(e.notes or "")

    def guessing(self, e: Exam) -> bool:
        return e.type == "quiz" and self.syllabus(e) is None

    # ======== picking what to study ========
    def score(self, t: Topic, exams: list[Exam], d: date, rem: int) -> float:
        urgency = sum(e.weight / ((e.date - d).days + 2) for e in exams if self.covers(e, t) and e.date >= d)
        if urgency <= 0 or rem <= 0:
            return 0.0
        weak = (6 - self.confidence(t.course)) / 3
        need = min(1.0, rem / max(1, t.est_min))
        last = self.last_studied(t.id)
        stale = 1.2 if (last is None or (d - last).days >= 5) else 1.0
        until = self.store.get_kv(f"focus:{t.course}")                        # "more DD this week"
        focus = 2.0 if until and d.isoformat() <= until else 1.0
        return urgency * weak * need * stale * focus

    def backlog(self, t: Topic) -> bool:
        """Taught a while ago (6+ lectures back) but not learned yet -> catch up."""
        taught = (self.p.get("taught_upto", {}) or {}).get(t.course)
        return taught is not None and t.lec_to <= int(taught) - 6 and self.status(t) != "d"

    # ======== your study status (/status, weekly check-in, later Notion) ========
    def status(self, t: Topic) -> str:
        dm = self.done_min(t.id)
        return "d" if dm >= LEARNED * t.est_min else ("h" if dm > 0 else "n")

    def set_topic_status(self, topic_id: str, st: str, day: date | None = None) -> bool:
        t = next((x for x in self.topics if x.id == topic_id), None)
        if not t or st not in STATUS_ICON:
            return False
        import json
        cur = self.done_min(t.id)
        target = {"d": max(cur, int(LEARNED * t.est_min)), "h": int(0.3 * t.est_min), "n": 0}[st]
        parts = {"d": t.parts, "n": []}.get(st, self.parts_done(t.id))
        self.store.conn.execute(
            "INSERT INTO topic_progress(topic_id, done_min, last_studied, parts_done) VALUES(?,?,?,?) "
            "ON CONFLICT(topic_id) DO UPDATE SET done_min=excluded.done_min, parts_done=excluded.parts_done, "
            "last_studied=COALESCE(excluded.last_studied, last_studied)",
            (t.id, target, day.isoformat() if day and st != "n" else None, json.dumps(parts)))
        self.store.conn.commit()
        return True

    def taught(self, course: str, d: date) -> list[Topic]:
        return [t for t in self.topics if t.course == course and self.available(t, d)]

    def courses_to_check(self, d: date, only_open: bool = True) -> list[str]:
        """Courses for the check-in: anything not marked done yet, or newly taught topics."""
        order = list(dict.fromkeys(t.course for t in self.topics))
        out = []
        for c in order:
            ts = self.taught(c, d)
            if not ts:
                continue
            seen = set((self.store.get_kv(f"statusseen:{c}") or "").split(","))
            if not only_open or any(self.status(t) != "d" for t in ts) or any(t.id not in seen for t in ts):
                out.append(c)
        return out

    def mark_seen(self, course: str, d: date):
        self.store.set_kv(f"statusseen:{course}", ",".join(t.id for t in self.taught(course, d)))

    def status_text(self, course: str, d: date) -> str:
        from .pipeline import esc
        ts = self.taught(course, d)
        lines = [f"📋 <b>{esc(course)}: how far are you?</b>  (topics taught so far)"]
        for i, t in enumerate(ts, 1):
            got = self.parts_done(t.id) if self.status(t) == "h" else []
            lines.append(f"{i}. {STATUS_ICON[self.status(t)]} {esc(t.title)}"
                         + (f"  <i>(done: {esc(', '.join(got))})</i>" if got else ""))
        lines.append("\nTap ✅ done · 🟡 halfway · ❌ not yet")
        return "\n".join(lines)

    def detail(self, t: Topic) -> str:
        verb = "Learn + practice" if self.status(t) != "d" else "Practice / revise"
        bits = [f"{verb} · {t.refs}"]
        left, got = self.parts_left(t), self.parts_done(t.id)
        if got and left:
            bits.append(f"Left: {', '.join(left)} (done: {', '.join(got)})")
        if t.practice:
            bits.append(f"Practice: {t.practice}")
        return "\n".join(bits)

    def covers_note(self, e: Exam) -> str:
        info = self.syllabus_info(e.id)
        if info:
            return f"\n📘 {e.title} syllabus: {info.get('short') or 'from the professor'}"
        if e.type == "quiz":
            return f"\n❔ {e.title}: syllabus not out yet (best guess)"
        return ""

    def covers_line(self, e: Exam) -> str:
        covered = [t.title for t in self.topics if self.covers(e, t)]
        line = ("\nCovers: " + "; ".join(covered[:4]) + (" …" if len(covered) > 4 else "")) if covered else ""
        return line + self.covers_note(e)

    def catchup_wait(self, d: date, days: int = 14) -> dict:
        """{course: days since its last catch-up block} (courses never caught up are missing = waited longest)."""
        rows = self.store.conn.execute(
            "SELECT course, MAX(date) AS last FROM plan_blocks WHERE date>=? AND date<? AND title LIKE '%(catch-up)%' "
            "AND status IN ('planned','started','partial','done') GROUP BY course",
            ((d - timedelta(days=days)).isoformat(), d.isoformat())).fetchall()
        return {r["course"]: (d - date.fromisoformat(r["last"])).days for r in rows}

    def study_tasks(self, d: date, n: int, already: list | None = None) -> list[dict]:
        if n <= 0:
            return []
        exams = self.exams(d)
        tasks: list[dict] = []

        # 1. day before an exam -> revision only
        tomorrow = [e for e in exams if e.date == d + timedelta(days=1) and e.type in EXAM_TYPES]
        if tomorrow:
            for i in range(n):
                e = tomorrow[i % len(tomorrow)]
                detail = "Go over key formulas & results, redo problems you got wrong" + self.covers_line(e)
                tasks.append({"kind": "revise", "course": e.course, "topic_id": None,
                              "title": f"Revise for {e.title}", "detail": detail})
            return tasks

        # 2. labs / deadlines due within 2 days -> one prep block each
        for e in exams:
            if e.type in ("lab", "deadline") and 0 <= (e.date - d).days <= 2 and len(tasks) < n \
                    and self.is_graded(e) and not self.store.get_kv(f"prepdone:{e.id}"):
                tasks.append({"kind": "prep", "course": e.course, "topic_id": f"ev:{e.id}",
                              "title": f"Prepare: {e.title}", "detail": "Get it ready / finish it"})

        # 3. compre within 12 days -> a timed past paper every third day
        compres = sorted((e for e in exams if e.type == "compre" and 0 < (e.date - d).days <= 12), key=lambda e: e.date)
        if compres and d.toordinal() % 3 == 0 and len(tasks) < n:
            e = compres[0]
            tasks.append({"kind": "pyq", "course": e.course, "topic_id": None,
                          "title": "Past paper (timed)", "detail": f"Solve a previous year's {e.course} compre paper, then check answers"})

        # 4. topics, best score first (max 2 blocks per course / topic per day; relaxed if nothing else fits)
        sim: Counter = Counter()
        per_course: Counter = Counter()
        per_topic: Counter = Counter()
        for b in already or []:                     # blocks you kept/changed today count too
            if b.is_study and b.topic_id:
                per_course[b.course] += 1
                per_topic[b.topic_id] += 1
                sim[b.topic_id] += b.end - b.start
        limit = 2

        # 3a. quiz guarantee: from 6 days before a quiz (or a graded lab eval with a syllabus),
        #     at least one block a day on its topics, however confident you feel in that course
        soon = sorted((e for e in exams if 1 < (e.date - d).days <= QUIZ_GUARANTEE_DAYS and self.is_graded(e)
                       and (e.type == "quiz" or (e.type in ("lab", "exam") and self.syllabus(e)))),
                      key=lambda e: e.date)
        for e in soon:
            if len(tasks) >= n:
                break
            covered = [t for t in self.topics if self.covers(e, t)]
            if not covered or any(per_topic[t.id] for t in covered):
                continue                      # nothing to study for it, or a block today already does
            note = self.covers_note(e)
            left = [t for t in covered if self.remaining(t) - sim[t.id] > 0]
            if left:
                t = max(left, key=lambda t: (self.status(t) == "h", self.remaining(t) - sim[t.id], -t.lec_from))
                tasks.append({"kind": "study", "course": t.course, "topic_id": t.id,
                              "title": f"{t.title} (for {e.title})", "detail": self.detail(t) + note})
                sim[t.id] += self.block
            else:
                t = min(covered, key=lambda t: self.last_studied(t.id) or date.min)
                tasks.append({"kind": "revise", "course": t.course, "topic_id": t.id,
                              "title": f"Practise for {e.title}: {t.title}",
                              "detail": f"Practice problems · {t.practice or t.refs}" + note})
            per_course[t.course] += 1
            per_topic[t.id] += 1

        # 3b. catch-up: one block a day for topics taught long ago but not learned yet
        if len(tasks) < n and not any(per_topic[t.id] for t in self.topics if self.backlog(t)):
            behind = [(self.score(t, exams, d, self.remaining(t)), t) for t in self.topics
                      if self.available(t, d) and self.backlog(t)
                      and not any(e.type == "quiz" and e.date >= d and self.covers(e, t) for e in exams)]
            behind = [x for x in behind if x[0] > 0]
            if behind:
                # unfinished midsem syllabus first, topics you've started first, then by score
                # courses take turns: the course that waited longest for a catch-up block goes first
                waited = self.catchup_wait(d)
                t = max(behind, key=lambda x: (waited.get(x[1].course, 99), x[1].pre_midsem,
                                               self.status(x[1]) == "h", x[0], -x[1].lec_from))[1]
                tasks.append({"kind": "study", "course": t.course, "topic_id": t.id,
                              "title": t.title + " (catch-up)", "detail": self.detail(t)})
                sim[t.id] += self.block
                per_course[t.course] += 1
                per_topic[t.id] += 1

        while len(tasks) < n:
            best = None
            for t in self.topics:
                if not self.available(t, d) or per_topic[t.id] >= 2 or per_course[t.course] >= limit:
                    continue
                rem = self.remaining(t) - sim[t.id]
                sc = self.score(t, exams, d, rem)
                if sc > 0 and (best is None or sc > best[0]):
                    best = (sc, t)
            if best is None:
                if limit < 4:
                    limit = 4
                    continue
                break
            t = best[1]
            tasks.append({"kind": "study", "course": t.course, "topic_id": t.id, "title": t.title, "detail": self.detail(t)})
            sim[t.id] += self.block
            per_course[t.course] += 1
            per_topic[t.id] += 1
        return tasks

    # ======== your day ========
    def busy(self, d: date) -> list[tuple[int, int]]:
        busy = []
        skip = bool(self.p.get("skip_non_attendance_classes", True))
        cancels = [r for r in self.store.events_on(d.isoformat()) if r["status"] == "cancelled"]
        for c in load_weekly_classes():
            if c["day"] != DAYS[d.weekday()] or (skip and not c["must_attend"]):
                continue
            if any(class_matches_cancel(c, x) for x in cancels):
                continue
            busy.append((hm(c["start"]), hm(c["end"])))
        for r in self.store.events_on(d.isoformat(), statuses=("active",)):
            notes = (r["notes"] or "").upper()
            if r["start_time"]:
                s = hm(r["start_time"])
                e = hm(r["end_time"]) if r["end_time"] else s + (180 if r["type"] == "compre" else 60)
            elif r["type"] in EXAM_TYPES and ("FN" in notes or "AN" in notes):
                s, e = ((9 * 60, 12 * 60) if "FN" in notes else (14 * 60, 17 * 60))
            else:
                continue
            busy.append((s, e))
        for _, s, e in self.meals(d):
            busy.append((s, e))
        return busy

    MEAL_ICON = {"breakfast": "🍳", "lunch": "🍛", "dinner": "🍽"}
    DEFAULT_MEALS = {"weekday": {"breakfast": "07:30-08:30", "lunch": "13:00-14:00", "dinner": "20:00-21:00"},
                     "weekend": {"breakfast": "09:00-10:00", "lunch": "13:00-14:00", "dinner": "20:00-21:00"}}

    def meals(self, d: date) -> list[tuple[str, int, int]]:
        """[(name, start, end)] for the day. Kept free in the plan; never put on Google Calendar."""
        cfg = self.p.get("meals", self.DEFAULT_MEALS) or {}
        if isinstance(cfg, list):                       # old style: ["13:00-14:00", ...]
            return [("meal", *rng(m)) for m in cfg]
        day = cfg.get("weekend" if self.weekend(d) else "weekday") or {}
        return sorted(((name, *rng(r)) for name, r in day.items() if r), key=lambda x: x[1])

    def plan_day(self, d: date, sleep: str | None = None, from_min: int = 0,
                 keep: list[Block] | None = None) -> list[Block]:
        keep = keep or []
        ds, de = self.day_bounds(d)
        busy = self.busy(d) + [(b.start, b.end + (self.brk if b.is_study else 0)) for b in keep]
        free = subtract([(max(ds, from_min), de)], busy)
        out: list[Block] = []

        # ---- study ----
        used = sum(b.end - b.start for b in keep if b.is_study and b.status != "skipped")
        n = max(0, (self.cap(d, sleep) - used) // self.block)
        tasks = self.study_tasks(d, n, already=[b for b in keep if b.status != "skipped"])
        # a quiz later today (e.g. 7 pm): the blocks before it are last-minute revision for it
        quiz_today = sorted((e for e in self.exams(d) if e.date == d and e.start and e.type in EXAM_TYPES
                             and self.is_graded(e)), key=lambda e: e.start)
        for ws, we in self.windows(d):
            for fs, fe in intersect(free, ws, we):
                t = fs
                while tasks and t + self.block <= fe:
                    task = tasks.pop(0)
                    q = next((e for e in quiz_today if t + self.block <= e.start), None)
                    if q:
                        task = {"kind": "revise", "course": q.course, "topic_id": None,
                                "title": f"Revise for {q.title} (today {fmt(q.start)})",
                                "detail": "Key formulas & results, the problems you got wrong" + self.covers_line(q)}
                    out.append(Block(d.isoformat(), t, t + self.block, task["kind"], task["title"],
                                     task["course"], task["topic_id"], task["detail"]))
                    t += self.block + self.brk
        free = subtract(free, [(b.start, b.end + self.brk) for b in out])

        # ---- personal ----
        def place(kind, minutes, title, prefs, detail="", min_ok=None):
            nonlocal free
            for need in (minutes, min_ok) if min_ok else (minutes,):
                s = self.find_slot(free, need, prefs, ds, de)
                if s is not None:
                    out.append(Block(d.isoformat(), s, s + need, kind, title, detail=detail))
                    free = subtract(free, [(s, s + need)])
                    return True
            return False

        kept_kinds = Counter(b.kind for b in keep)
        for k in (self.store.get_kv(f"nokind:{d.isoformat()}") or "").split(","):   # "skip DSA today"
            if k:
                kept_kinds[k] += 1
        wk = self.per.get("workout", {}) or {}
        if wk.get("enabled", True) and DAYS[d.weekday()] in [x.upper()[:2] for x in wk.get("days", DAYS[:6])] \
                and not kept_kinds["workout"]:
            place("workout", int(wk.get("minutes", 60)), "Workout",
                  wk.get("prefer", ["16:00-18:00", "07:00-09:00", "12:00-16:00"]), min_ok=40)

        rd = self.per.get("reading", {}) or {}
        if rd.get("enabled", True) and not kept_kinds["reading"]:
            prefs = rd.get("prefer_weekend" if self.weekend(d) else "prefer",
                           ["14:00-17:00", "10:00-13:00"] if self.weekend(d) else ["22:00-23:59", "14:00-18:00"])
            place("reading", int(rd.get("minutes", 45)), "Reading", prefs, min_ok=int(rd.get("min_minutes", 40)))

        dsa = self.per.get("dsa", {}) or {}
        if dsa.get("enabled", True) and not kept_kinds["dsa"]:
            q = int(dsa.get("crunch_questions", 1) if self.crunch(d) else dsa.get("questions", 3))
            mins = int(dsa.get("crunch_minutes", 25) if self.crunch(d) else dsa.get("minutes", 60))
            place("dsa", mins, f"DSA ({q} question{'s' if q != 1 else ''})",
                  dsa.get("prefer", ["08:00-17:00", "22:00-23:59", "14:00-18:00"]), detail=f"Solve {q} DSA problem(s)")

        pj = self.per.get("project", {}) or {}
        weekly = int(pj.get("crunch_weekly_minutes", 0) if self.crunch(d) else pj.get("weekly_minutes", 180))
        if pj.get("enabled", True) and weekly > 0 and not kept_kinds["project"]:
            left = weekly - self.week_minutes(d, "project")
            mins = min(int(pj.get("max_per_day", 60)), left)
            if mins >= 30:
                place("project", mins, pj.get("name", "Personal project / skills"),
                      pj.get("prefer", ["08:00-17:00"]))

        return sorted(out, key=lambda b: b.start)

    def find_slot(self, free, minutes, prefs, ds, de):
        for p in prefs:
            a, b = rng(p)
            for s, e in intersect(free, a, b):
                if e - s >= minutes:
                    return s
        for s, e in intersect(free, max(ds, 9 * 60), de):
            if e - s >= minutes:
                return s
        return None

    def week_minutes(self, d: date, kind: str) -> int:
        monday = d - timedelta(days=d.weekday())
        r = self.store.conn.execute(
            "SELECT SUM(end_min-start_min) AS m FROM plan_blocks WHERE kind=? AND date>=? AND date<? "
            "AND status IN ('planned','partial','done')", (kind, monday.isoformat(), d.isoformat())).fetchone()
        return int(r["m"] or 0)

    # ======== saving / changing the plan ========
    def blocks_on(self, d: date, statuses=BLOCK_STATUSES) -> list[Block]:
        q = f"SELECT * FROM plan_blocks WHERE date=? AND status IN ({','.join('?' * len(statuses))}) ORDER BY start_min"
        return [self._row(r) for r in self.store.conn.execute(q, (d.isoformat(), *statuses)).fetchall()]

    def get(self, block_id: int) -> Block | None:
        r = self.store.conn.execute("SELECT * FROM plan_blocks WHERE id=?", (block_id,)).fetchone()
        return self._row(r) if r else None

    @staticmethod
    def _row(r) -> Block:
        return Block(r["date"], r["start_min"], r["end_min"], r["kind"], r["title"], r["course"], r["topic_id"],
                     r["detail"] or "", r["status"], r["id"], r["gcal_event_id"],
                     bool(r["locked"]) if "locked" in r.keys() else False)

    def save_day(self, d: date, sleep: str | None = None, from_min: int = 0, cal=None) -> list[Block]:
        """(Re)plan a day. Done/skipped blocks and blocks before `from_min` are kept;
        the rest is replaced. Calendar events are updated to match."""
        old = self.blocks_on(d)
        keep = [b for b in old if b.status != "planned" or b.start < from_min or b.locked]
        drop = [b for b in old if b not in keep]
        for b in drop:
            if cal and b.gcal_event_id:
                cal.delete(b.gcal_event_id)
            self.store.conn.execute("DELETE FROM plan_blocks WHERE id=?", (b.id,))
        self.store.conn.commit()
        new = self.plan_day(d, sleep=sleep, from_min=from_min, keep=keep)
        for b in new:
            cur = self.store.conn.execute(
                "INSERT INTO plan_blocks(date,start_min,end_min,kind,course,topic_id,title,detail,status,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (b.date, b.start, b.end, b.kind, b.course, b.topic_id, b.title, b.detail, "planned",
                 datetime.now().isoformat(timespec="seconds")))
            b.id = cur.lastrowid
        self.store.conn.commit()
        if cal:
            for b in new:
                self._cal_create(cal, b)
        return sorted(keep + new, key=lambda b: b.start)

    def _cal_create(self, cal, b: Block):
        try:
            gid = cal.create_block(b)
            self.store.conn.execute("UPDATE plan_blocks SET gcal_event_id=? WHERE id=?", (gid, b.id))
            self.store.conn.commit()
            b.gcal_event_id = gid
        except Exception:
            pass

    def set_status(self, block_id: int, status: str, cal=None, count: bool = True) -> Block | None:
        """count=False: don't add study minutes (used when progress was set by parts instead)."""
        b = self.get(block_id)
        if not b:
            return None
        self.store.conn.execute("UPDATE plan_blocks SET status=? WHERE id=?", (status, block_id))
        self.store.conn.commit()
        if count and status in ("done", "partial") and b.status not in ("done", "partial"):
            if b.kind == "study" and b.topic_id:
                mins = b.end - b.start if status == "done" else (b.end - b.start) // 2
                self.add_progress(b.topic_id, mins, date.fromisoformat(b.date))
        if status == "done" and b.status != "done":
            if b.kind == "prep" and b.topic_id and b.topic_id.startswith("ev:"):
                self.store.set_kv(f"prepdone:{b.topic_id[3:]}", "1")
        if status == "skipped" and cal and b.gcal_event_id:
            cal.delete(b.gcal_event_id)
            self.store.conn.execute("UPDATE plan_blocks SET gcal_event_id=NULL WHERE id=?", (block_id,))
            self.store.conn.commit()
        b.status = status
        return b

    def close_day(self, d: date, now_min: int = 24 * 60) -> tuple[int, int]:
        """End of day: started blocks count as done, planned ones that already ended as missed."""
        done = missed = 0
        for b in self.blocks_on(d, statuses=("planned", "started")):
            if b.end > now_min:
                continue
            if b.status == "started":
                self.set_status(b.id, "done")
                done += 1
            else:
                self.store.conn.execute("UPDATE plan_blocks SET status='skipped' WHERE id=?", (b.id,))
                missed += 1
        self.store.conn.commit()
        return done, missed

    def due_now(self, d: date, now_min: int, window: int = 5) -> list[Block]:
        """Planned blocks starting in the last few minutes (for the start reminder)."""
        return [b for b in self.blocks_on(d, statuses=("planned",)) if b.start <= now_min < b.start + window]

    def ending_now(self, d: date, now_min: int, window: int = 10) -> list[Block]:
        """Blocks that just ended and you haven't marked yet (to ask 'How did it go?')."""
        return [b for b in self.blocks_on(d, statuses=("planned", "started")) if b.end <= now_min < b.end + window]

    def recent_blocks(self, d: date, days: int = 2) -> list[Block]:
        out = []
        for i in range(days - 1, -1, -1):
            out += self.blocks_on(d - timedelta(days=i))
        return out

    # ======== your own changes ("replace workout with DD", "move reading to 11 pm") ========
    def pick_topic(self, course: str, d: date) -> Topic | None:
        """Best topic for 'some DD study': unfinished started ones first, then catch-up, then by score."""
        exams = self.exams(d)
        cands = [t for t in self.topics if t.course == course and self.available(t, d) and self.status(t) != "d"]
        if not cands:
            cands = [t for t in self.topics if t.course == course and self.available(t, d)]
        if not cands:
            return None
        return max(cands, key=lambda t: (self.status(t) == "h", self.backlog(t) and t.pre_midsem,
                                         self.score(t, exams, d, self.remaining(t)), -t.lec_from))

    def make_block(self, d: date, start: int, minutes: int, kind: str, course: str | None = None,
                   topic_id: str | None = None) -> Block:
        title = {"workout": "Workout", "reading": "Reading", "dsa": "DSA", "project": "Personal project / skills"}.get(kind, kind)
        detail = ""
        if kind in STUDY_KINDS:
            t = self.topic(topic_id) if topic_id else (self.pick_topic(course, d) if course else None)
            if t:
                course, topic_id, title, detail = t.course, t.id, t.title, self.detail(t)
            else:
                title = f"{course} study" if course else "Study"
        return Block(d.isoformat(), start, start + minutes, kind, title, course, topic_id, detail, "planned", locked=True)

    def _save_block(self, b: Block, cal=None) -> Block:
        cur = self.store.conn.execute(
            "INSERT INTO plan_blocks(date,start_min,end_min,kind,course,topic_id,title,detail,status,created_at,locked) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,1)",
            (b.date, b.start, b.end, b.kind, b.course, b.topic_id, b.title, b.detail, "planned",
             datetime.now().isoformat(timespec="seconds")))
        self.store.conn.commit()
        b.id = cur.lastrowid
        if cal:
            self._cal_create(cal, b)
        return b

    def replace_block(self, block_id: int, kind: str, course=None, topic_id=None, cal=None) -> Block | None:
        """Same time slot, new activity (e.g. workout -> DD study). Locked so re-plans keep it."""
        old = self.get(block_id)
        if not old:
            return None
        new = self.make_block(date.fromisoformat(old.date), old.start, old.end - old.start, kind, course, topic_id)
        if cal and old.gcal_event_id:
            cal.delete(old.gcal_event_id)
        self.store.conn.execute(
            "UPDATE plan_blocks SET kind=?, course=?, topic_id=?, title=?, detail=?, status='planned', "
            "gcal_event_id=NULL, locked=1 WHERE id=?",
            (new.kind, new.course, new.topic_id, new.title, new.detail, block_id))
        self.store.conn.commit()
        if old.kind not in STUDY_KINDS and old.kind != kind:
            self.no_kind(date.fromisoformat(old.date), old.kind)       # don't re-add it elsewhere today
        b = self.get(block_id)
        if cal:
            self._cal_create(cal, b)
        return b

    def move_block(self, block_id: int, start: int, cal=None) -> Block | None:
        b = self.get(block_id)
        if not b:
            return None
        length = b.end - b.start
        self.store.conn.execute("UPDATE plan_blocks SET start_min=?, end_min=?, status='planned', locked=1 WHERE id=?",
                                (start, start + length, block_id))
        self.store.conn.commit()
        if cal:
            if b.gcal_event_id:
                cal.delete(b.gcal_event_id)
            self._cal_create(cal, self.get(block_id))
        return self.get(block_id)

    def free_slot(self, d: date, minutes: int, study: bool, after: int = 0) -> int | None:
        ds, de = self.day_bounds(d)
        others = self.blocks_on(d, statuses=("planned", "started", "partial", "done"))
        busy = self.busy(d) + [(x.start, x.end + (self.brk if x.is_study else 0)) for x in others]
        free = subtract([(max(ds, after), de)], busy)
        spots = [x for ws, we in self.windows(d) for x in intersect(free, ws, we)] if study else []
        spots += free
        return next((s for s, e in spots if e - s >= minutes), None)

    def add_block(self, d: date, kind: str, minutes: int, start: int | None = None, course=None,
                  topic_id=None, after: int = 0, cal=None) -> Block | None:
        if start is None:
            start = self.free_slot(d, minutes, kind in STUDY_KINDS, after)
            if start is None:
                return None
        return self._save_block(self.make_block(d, start, minutes, kind, course, topic_id), cal)

    def remove_block(self, block_id: int, cal=None) -> Block | None:
        b = self.set_status(block_id, "skipped", cal)
        if b and b.kind not in STUDY_KINDS:
            self.no_kind(date.fromisoformat(b.date), b.kind)
        return b

    def no_kind(self, d: date, kind: str):
        key = f"nokind:{d.isoformat()}"
        kinds = set(filter(None, (self.store.get_kv(key) or "").split(","))) | {kind}
        self.store.set_kv(key, ",".join(sorted(kinds)))

    def move_later(self, block_id: int, now_min: int, cal=None) -> Block | None:
        """Push a block to the next free slot later today. None = no room (it's skipped)."""
        b = self.get(block_id)
        if not b:
            return None
        d = date.fromisoformat(b.date)
        ds, de = self.day_bounds(d)
        others = [x for x in self.blocks_on(d, statuses=("planned", "started", "partial", "done")) if x.id != b.id]
        busy = self.busy(d) + [(x.start, x.end + (self.brk if x.is_study else 0)) for x in others]
        free = subtract([(max(ds, now_min + 5), de)], busy)
        length = b.end - b.start
        if b.is_study:
            spots = [x for ws, we in self.windows(d) for x in intersect(free, ws, we)]
        else:
            spots = free
        start = next((s for s, e in spots if e - s >= length), None)
        if start is None:
            self.set_status(block_id, "skipped", cal)
            return None
        self.store.conn.execute("UPDATE plan_blocks SET start_min=?, end_min=? WHERE id=?",
                                (start, start + length, block_id))
        self.store.conn.commit()
        if cal:
            if b.gcal_event_id:
                cal.delete(b.gcal_event_id)
            b.start, b.end = start, start + length
            self._cal_create(cal, b)
        return self.get(block_id)

    # ======== summaries ========
    def format_day(self, d: date, blocks: list[Block] | None = None, html=True) -> str:
        from .pipeline import esc, _nice_date
        blocks = self.blocks_on(d) if blocks is None else blocks
        head = f"📅 <b>Plan for {_nice_date(d.isoformat())}</b>" if html else f"Plan for {d}"
        if not blocks:
            return head + "\n   Nothing planned."
        lines = [head]
        meals = self.meals(d)
        for b in blocks:
            while meals and meals[0][1] <= b.start:
                name, ms, me = meals.pop(0)
                lines.append(f"   {fmt(ms)}–{fmt(me)}  {self.MEAL_ICON.get(name, '🍽')} {name.capitalize()}")
            mark = {"done": " ✅", "skipped": " ⏭", "started": " ▶️", "partial": " 🟡"}.get(b.status, "")
            text = esc(b.line()) + mark
            lines.append(f"   <s>{text}</s>" if b.status == "skipped" else f"   {text}")
            if b.is_study and b.detail and b.status == "planned":
                dl = b.detail.splitlines()
                first = esc(next((x for x in dl if x.startswith("Left:")), None)
                            or (dl[-1] if "Practice:" in b.detail else dl[0]))
                lines.append(f"        <i>{first}</i>")
        for name, ms, me in meals:
            lines.append(f"   {fmt(ms)}–{fmt(me)}  {self.MEAL_ICON.get(name, '🍽')} {name.capitalize()}")
        study = sum(b.end - b.start for b in blocks if b.is_study and b.status != "skipped")
        lines.append(f"   ⏱ Study: {study // 60} h {study % 60:02d} min")
        return "\n".join(lines)

    def progress_text(self, d: date) -> str:
        from .pipeline import esc
        lines = ["📈 <b>Progress</b>"]
        by_course: dict[str, list[Topic]] = {}
        for t in self.topics:
            by_course.setdefault(t.course, []).append(t)
        for course, ts in by_course.items():
            est = sum(t.est_min for t in ts)
            done = sum(min(t.est_min, self.done_min(t.id)) for t in ts)
            pct = int(100 * done / est) if est else 0
            bar = "▓" * (pct // 10) + "░" * (10 - pct // 10)
            lines.append(f"   {esc(course):5s} {bar} {pct}%  (confidence {self.confidence(course)}/5)")
        monday = d - timedelta(days=d.weekday())
        rows = self.store.conn.execute(
            "SELECT kind, COUNT(*) n, SUM(end_min-start_min) m FROM plan_blocks WHERE status IN ('done','partial') "
            "AND date>=? AND date<=? GROUP BY kind", (monday.isoformat(), d.isoformat())).fetchall()
        got = {r["kind"]: (r["n"], r["m"] or 0) for r in rows}
        study_m = sum(got.get(k, (0, 0))[1] for k in STUDY_KINDS)
        lines.append(f"\n<b>This week</b>\n   📚 Study {study_m // 60} h {study_m % 60:02d} min")
        lines.append(f"   🏋 Workouts {got.get('workout', (0, 0))[0]}/6 · 📖 Reading {got.get('reading', (0, 0))[0]} days"
                     f" · 💻 DSA {got.get('dsa', (0, 0))[0]} days · 🚀 Project {got.get('project', (0, 0))[1]} min")
        return "\n".join(lines)
