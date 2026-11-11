"""Offline tests for the study planner (no internet, no keys).

    python tests/test_planner.py
"""
import os
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["SITRUUNA_EXAMPLES"] = "1"           # use the example config/timetable/topics

from pa.config import ROOT, Settings          # noqa: E402
from pa.pipeline import Assistant              # noqa: E402
from pa.planner import Planner, fmt, hm, load_topics  # noqa: E402
from pa.seed import seed                       # noqa: E402
from pa.store import Store                     # noqa: E402

TZ = ZoneInfo("Asia/Kolkata")
FAILS = []


def ok(cond, msg):
    print(("  ✅ " if cond else "  ❌ ") + msg)
    if not cond:
        FAILS.append(msg)


class FixedSettings(Settings):
    """Settings with a fake 'today', so tests give the same answer any day."""
    fixed: date = date(2026, 11, 4)

    def now(self):
        return datetime.combine(self.fixed, datetime.min.time(), TZ).replace(hour=7)

    def today(self):
        return self.fixed


class FakeCal:
    def __init__(self):
        self.events, self.n = {}, 0

    def create_block(self, b):
        self.n += 1
        self.events[f"b{self.n}"] = (b.date, b.start, b.title)
        return f"b{self.n}"

    def create(self, ev):
        self.n += 1
        self.events[f"e{self.n}"] = ev["title"]
        return f"e{self.n}"

    def delete(self, gid):
        self.events.pop(gid, None)


def make(today=date(2026, 11, 4)):
    raw = yaml.safe_load(open(ROOT / "config.example.yaml", encoding="utf-8"))
    s = FixedSettings(raw=raw, tz=TZ, llm_provider="fake", gemini_key="", anthropic_key="",
                      telegram_token="", telegram_chat_id="")
    s.fixed = today
    store = Store(Path(tempfile.mkdtemp()) / "t.db")
    a = Assistant(s, store, llm=None, calendar=None, gmail=None)
    return s, store, a, Planner(s, store)


def overlaps(blocks):
    bs = sorted(blocks, key=lambda b: b.start)
    return any(bs[i].end > bs[i + 1].start for i in range(len(bs) - 1))


print("\n1. Topics file")
topics = load_topics()
ok(len(topics) == 68, f"68 topics loaded (got {len(topics)})")
ok({t.course for t in topics} == {"DD", "EM", "ED", "EMT", "M3"}, "all 5 exam courses present")
m311 = next(t for t in topics if t.id == "M311")
ok("p462" in m311.practice and m311.est_min == 240, "Laplace has handout problems and ~4 h estimate")

print("\n2. Seeding known dates")
s, store, a, p = make()
notices = seed(a)
rows = {r["title"]: r for r in store.upcoming("2026-11-04", statuses=("active", "pending"))}
ok(rows.get("M3 Quiz 2", {})["date"] == "2026-11-13" and rows["M3 Quiz 2"]["start_time"] == "19:00", "M3 Quiz 2 on 13 Nov 7 pm")
ok(rows.get("EM Quiz 2", {})["date"] == "2026-11-16", "EM Quiz 2 on 16 Nov")
ok(rows.get("ED Quiz 3", {})["date"] == "2026-11-19", "ED Quiz 3 on 19 Nov")
ok(rows.get("DD Verilog evaluation (tentative)", {})["status"] == "pending", "tentative Verilog eval asks first")
ok(sum(1 for r in rows.values() if r["type"] == "compre") == 5, "5 compres added")
ok(seed(a) == [], "seed runs only once")

s2, store2, a2, _ = make()
store2.add_event(title="M3 Compre", type="compre", action="add", course="M3", date="2026-12-03",
                 status="active", confidence="high", notes="AN")
seed(a2)
m3c = [r for r in store2.upcoming("2026-11-04") if r["course"] == "M3" and r["type"] == "compre"]
ok(len(m3c) == 1 and m3c[0]["date"] == "2026-12-03", "a compre date from email is NOT overwritten by the handout")

print("\n3. A weekday plan (Wed 4 Nov)")
blocks = p.save_day(date(2026, 11, 4))
study = [b for b in blocks if b.is_study]
mins = sum(b.end - b.start for b in study)
ok(mins <= 180 and len(study) == 3, f"3 study blocks, ≤ 3 h (got {len(study)} / {mins} min)")
win = p.windows(date(2026, 11, 4))
ok(all(any(ws <= b.start and b.end <= we for ws, we in win) for b in study), "study only inside 18–20 / 21–23:30")
ok(not overlaps(blocks), "no blocks overlap")
busy = p.busy(date(2026, 11, 4))
ok(all(not (b.start < e and s_ < b.end) for b in blocks for s_, e in busy), "nothing clashes with ⭐ classes / meals")
ok(hm("16:00") in [s_ for s_, _ in busy] and hm("08:00") not in [s_ for s_, _ in busy],
   "M3 lecture (⭐) is busy; non-attendance classes are free time")
kinds = {b.kind for b in blocks}
ok({"workout", "reading", "dsa"} <= kinds, f"workout, reading and DSA placed (got {sorted(kinds)})")
ok(all(b.end <= 24 * 60 - 1 for b in blocks), "nothing after midnight")
ok(all(b.detail for b in study if b.kind == "study"), "study blocks say what to read and practise")
wed_meals = p.meals(date(2026, 11, 4))
ok([m[0] for m in wed_meals] == ["breakfast", "lunch", "dinner"] and wed_meals[0][1] == hm("07:30"),
   "weekday meals: breakfast 07:30, lunch, dinner")
ok(all(not (b.start < me and ms < b.end) for b in blocks for _, ms, me in wed_meals), "nothing planned over a meal")
txt = p.format_day(date(2026, 11, 4), blocks)
ok("🍳 Breakfast" in txt and "🍛 Lunch" in txt and "🍽 Dinner" in txt, "meals are shown in the plan")
print("     " + txt.replace("\n", "\n     "))

print("\n4. Weekend + Sunday")
sat = p.plan_day(date(2026, 11, 7))
ok(sum(b.end - b.start for b in sat if b.is_study) <= 240, "Saturday ≤ 4 h study")
ok(all(b.end <= hm("19:00") for b in sat), "weekend evening after 19:00 stays free")
ok(any(b.kind == "workout" for b in sat), "Saturday has a workout")
sun = p.plan_day(date(2026, 11, 8))
ok(not any(b.kind == "workout" for b in sun), "Sunday: no workout")
sat_meals = p.meals(date(2026, 11, 7))
ok(sat_meals[0][1] == hm("09:00") and all(not (b.start < me and ms < b.end) for b in sat for _, ms, me in sat_meals),
   "weekend breakfast 09:00, nothing planned over meals")

print("\n5. Day before a quiz = revision only")
day_before = p.plan_day(date(2026, 11, 12))
st = [b for b in day_before if b.is_study]
ok(st and all(b.kind == "revise" and b.course == "M3" for b in st), "12 Nov: all study blocks revise M3 for Quiz 2")

print("\n6. Quiz covers newer topics, compre covers everything")
quiz_topics = [b.topic_id for b in p.plan_day(date(2026, 11, 10)) if b.kind == "study"]
first = [t for t in topics if t.id in quiz_topics]
ok(first and any(not t.pre_midsem for t in first), f"new (post-midsem) topics get time before quizzes ({quiz_topics})")
em_quiz = next(e for e in p.exams(date(2026, 11, 10)) if e.course == "EM" and e.type == "quiz")
ok(not p.covers(em_quiz, next(t for t in topics if t.id == "EM09")), "EM Quiz 2 does NOT cover DC machines (midsem syllabus)")

print("\n7. Bad sleep = lighter day")
light = p.plan_day(date(2026, 11, 5), sleep="bad")
ok(sum(b.end - b.start for b in light if b.is_study) <= 120, "bad sleep: ≤ 2 h study on a weekday")

print("\n8. Crunch (from 28 Nov)")
sc, storec, ac, pc = make(date(2026, 11, 28))
seed(ac)
crunch = pc.plan_day(date(2026, 11, 28))
dsa = [b for b in crunch if b.kind == "dsa"]
ok(sum(b.end - b.start for b in crunch if b.is_study) > 240, "crunch Saturday > 4 h study (cap 5 h)")
ok(dsa and "1 question" in dsa[0].title, "DSA drops to 1 question")
ok(not any(b.kind == "project" for b in crunch), "no project time in crunch")
ok(any(b.kind == "workout" for b in crunch) and any(b.kind == "reading" for b in crunch), "workout + reading stay")

print("\n9. Done / Later / Skip and re-planning")
cal = FakeCal()
s3, store3, a3, p3 = make()
seed(a3)
bl = p3.save_day(date(2026, 11, 4), cal=cal)
ok(len(cal.events) == len(bl), "every block is on Google Calendar")
ok(not any("Breakfast" in str(v) or "Lunch" in str(v) or "Dinner" in str(v) for v in cal.events.values()),
   "meals are NOT put on Google Calendar")
first_study = next(b for b in bl if b.kind == "study")
p3.set_status(first_study.id, "started")
p3.set_status(first_study.id, "done")
ok(p3.done_min(first_study.topic_id) == 50, "Done adds 50 min to the topic")
later = next(b for b in bl if b.kind == "study" and b.id != first_study.id)
moved = p3.move_later(later.id, now_min=later.start, cal=cal)
ok(moved is None or moved.start > later.start, "Later moves the block to a later free slot (or skips it)")
reading = next(b for b in bl if b.kind == "reading")
p3.set_status(reading.id, "skipped", cal)
ok(reading.gcal_event_id not in cal.events, "Skip removes it from Google Calendar")
again = p3.save_day(date(2026, 11, 4), from_min=hm("19:00"), cal=cal)
ok(any(b.id == first_study.id and b.status == "done" for b in again), "re-plan keeps done blocks")
ok(not overlaps([b for b in again if b.status != "skipped"]), "re-planned day has no overlaps")
ok(len([g for g in cal.events if g.startswith("b")]) == len([b for b in again if b.gcal_event_id and b.status != "skipped"]),
   "old Calendar blocks are removed when the plan changes")

print("\n10. End of day + progress")
s4, store4, a4, p4 = make()
bl4 = p4.save_day(date(2026, 11, 4))
p4.set_status(bl4[0].id, "started")
done, missed = p4.close_day(date(2026, 11, 4))
ok(done == 1 and missed == len(bl4) - 1, f"started → done, untouched → missed ({done}/{missed})")
txt = p4.progress_text(date(2026, 11, 4))
ok("M3" in txt and "Workouts" in txt, "progress text shows courses and routines")
p4.set_confidence("DD", 9)
ok(p4.confidence("DD") == 5, "confidence clamps to 1–5")

print("\n11. Telling Sitruuna a change in plain words")


class FakeLLM:
    name, model = "fake", "fake"

    def ask(self, system, prompt, files):
        return ('{"summary":"x","events":[{"action":"update","type":"quiz","course":"EM","title":"EM Quiz 2",'
                '"date":"2026-11-18","start_time":"17:00","source_quote":"EM quiz 2 moved to 18 Nov 5pm",'
                '"confidence":"high"}]}')


s5, store5, a5, p5 = make()
seed(a5)
a5.llm = FakeLLM()
ns = a5.from_user_text("EM quiz 2 moved to 18 Nov 5pm")
ok(len(ns) == 1 and ns[0].kind == "confirm", "your message is shown with ✅/❌ first (not applied yet)")
em = [r for r in store5.upcoming("2026-11-04") if r["title"] == "EM Quiz 2"]
ok(em and em[0]["date"] == "2026-11-16", "old date stays until you tap ✅")
a5.confirm(ns[0].event_id)
em = [r for r in store5.upcoming("2026-11-04") if r["title"] == "EM Quiz 2"]
ok(len(em) == 1 and em[0]["date"] == "2026-11-18", "after ✅ the quiz moves to 18 Nov (old one replaced)")

print("\n12. Study status (/status, weekly check-in)")
from pa.seed import seed_progress  # noqa: E402
s6, store6, a6, p6 = make()
seed(a6)
n = seed_progress(p6)
st = {t.id: p6.status(t) for t in p6.topics}
ok(st["EM01"] == "d" and st["M306"] == "d" and n > 0, "midsem topics start as done")
ok(st["EM08"] == st["EM09"] == st["EM10"] == "n", "EM DC machines start as NOT done (you said so)")
ok(seed_progress(p6) == 0, "progress seeding runs only once")
ok(p6.backlog(next(t for t in p6.topics if t.id == "EM09")), "DC machines count as backlog (taught long ago, not done)")
soon = [b.topic_id for day in range(4, 8) for b in p6.plan_day(date(2026, 11, day)) if b.kind == "study"]
ok(any(x in ("EM08", "EM09", "EM10") for x in soon), f"DC machines get study blocks this week ({soon})")
p6.set_topic_status("EM08", "h")
ok(p6.status(next(t for t in p6.topics if t.id == "EM08")) == "h", "🟡 halfway is saved")
p6.set_topic_status("EM08", "d")
ok(p6.status(next(t for t in p6.topics if t.id == "EM08")) == "d", "✅ done is saved")
txt = p6.status_text("EM", date(2026, 11, 4))
ok("✅" in txt and "❌" in txt and "DC" in txt, "status message lists taught topics with marks")
ok("EM" in p6.courses_to_check(date(2026, 11, 4)), "check-in includes courses with unfinished topics")
for c in ("DD", "EM", "ED", "EMT", "M3"):
    for t in p6.taught(c, date(2026, 11, 4)):
        p6.set_topic_status(t.id, "d")
    p6.mark_seen(c, date(2026, 11, 4))
ok(p6.courses_to_check(date(2026, 11, 4)) == [], "nothing to ask when all caught up")
ok(p6.courses_to_check(date(2026, 11, 27)) != [], "new lectures taught later -> asks again")

print("\n13. Understanding what you write (\"only did MUX\")")
import json as _json  # noqa: E402
from pa.understand import Interpreter  # noqa: E402


class ScriptLLM:
    """Fake AI: returns the next prepared answer and remembers the prompt."""
    name, model = "fake", "fake"

    def __init__(self, answers):
        self.answers, self.prompts = list(answers), []

    def ask(self, system, prompt, files):
        self.prompts.append(prompt)
        return _json.dumps(self.answers.pop(0))


s7, store7, a7, p7 = make(date(2026, 11, 3))
seed(a7)
seed_progress(p7)
p7.set_topic_status("DD05", "n")          # like your real data: you marked MSI ❌
bl7 = p7.save_day(date(2026, 11, 3))
dd = next(b for b in bl7 if b.topic_id == "DD05") if any(b.topic_id == "DD05" for b in bl7) else None
if dd is None:   # make sure a DD MSI block exists for the test
    store7.conn.execute("INSERT INTO plan_blocks(date,start_min,end_min,kind,course,topic_id,title,detail,status) "
                        "VALUES('2026-11-03',1080,1130,'study','DD','DD05','MSI components','', 'planned')")
    store7.conn.commit()
    dd = next(b for b in p7.blocks_on(date(2026, 11, 3)) if b.topic_id == "DD05")
t5 = p7.topic("DD05")
ok(t5.parts == ["Comparators", "Decoders", "Encoders", "MUX", "DEMUX"], "MSI topic has 5 parts")
ok(p7.match_parts(t5, ["mux"]) == ["MUX"] and p7.match_parts(t5, ["demux"]) == ["DEMUX"],
   "'mux' matches MUX only, not DEMUX")
ok(p7.match_parts(t5, ["decoder", "Encoders"]) == ["Decoders", "Encoders"], "'decoder' = Decoders (plural ok)")
ok(p7.ending_now(date(2026, 11, 3), dd.end) and dd.id in [b.id for b in p7.ending_now(date(2026, 11, 3), dd.end)],
   "asks 'how did it go?' even if you never tapped Start")
a7.llm = ScriptLLM([{"intent": "progress", "sure": True,
                     "progress": [{"topic_id": "DD05", "parts_done": ["mux"], "status": "partial", "block_id": None}],
                     "routines": []}])
it7 = Interpreter(a7, p7)
s7.now = lambda: datetime(2026, 11, 3, 21, 0, tzinfo=TZ)
r = it7.handle("i was only able to complete the mux thing")
ok(r.kind == "progress" and "MUX" in r.text and "Comparators" in r.text, f"'only the mux thing' understood ({r.kind})")
ok(p7.parts_done("DD05") == ["MUX"] and p7.status(t5) == "h", "MUX saved; topic is 🟡 halfway")
ok(p7.get(dd.id).status == "partial", "the DD block is marked partly done (not missed)")
ok("DD05" in a7.llm.prompts[0] and "MUX" in a7.llm.prompts[0] and str(dd.id) in a7.llm.prompts[0],
   "the AI sees your topics, parts and recent blocks")
ok("Left: Comparators, Decoders, Encoders, DEMUX" in p7.detail(t5), "next block says what's left")
uid = int(r.buttons[0][1].split(":")[1])
it7.undo(uid)
ok(p7.parts_done("DD05") == [] and p7.get(dd.id).status == "planned", "↩️ Undo puts everything back")

a7.llm = ScriptLLM([{"intent": "progress", "sure": False,
                     "progress": [{"topic_id": "DD05", "parts_done": ["Decoders"], "status": "partial"}]}])
r = it7.handle("did some of the digital stuff")
ok(r.kind == "ask" and r.buttons[0][1].startswith("uok:"), "unsure -> asks ✅ first")
ok(p7.parts_done("DD05") == [], "nothing changes before ✅")
it7.confirm(int(r.buttons[0][1].split(":")[1]))
ok(p7.parts_done("DD05") == ["Decoders"], "after ✅ it's saved")

a7.llm = ScriptLLM([{"intent": "progress", "sure": True,
                     "progress": [{"topic_id": "DD99", "parts_done": ["x"], "status": "done"},
                                  {"topic_id": "DD05", "parts_done": ["Flux capacitor"], "status": "partial"}]}])
before = p7.done_min("DD05")
it7.handle("blah")
ok(p7.parts_done("DD05") == ["Decoders"], "made-up topics and parts are ignored")

a7.llm = ScriptLLM([{"intent": "progress", "sure": True, "progress": [],
                     "routines": [{"kind": "workout", "done": True}]}])
r = it7.handle("went to the gym")
ok("Workout: done" in r.text, "routines understood ('went to the gym' -> workout done)")

a7.llm = ScriptLLM([{"intent": "date_change"}, ])
ok(it7.handle("EM quiz 2 moved to 18 nov").kind == "date_change", "date changes go to the ✅-first email rules")
a7.llm = ScriptLLM([{"intent": "question", "question": "plan_tomorrow"}])
ok(it7.handle("what's on tomorrow?").question == "plan_tomorrow", "questions are answered")
a7.llm = ScriptLLM([{"intent": "chat", "reply": "Anytime!"}])
ok(it7.handle("thanks").text == "Anytime!", "small talk gets a short reply (never 'couldn't read')")

print("\n15. /restart")
import asyncio  # noqa: E402
from datetime import timezone  # noqa: E402
from pa.telegram_bot import Bot  # noqa: E402
s9, store9, a9, p9 = make()
s9.telegram_token, s9.telegram_chat_id = "1:T", "42"
bot9 = Bot(a9)
sent9, stopped = [], []


async def _send(text, buttons=None, rows=None):
    sent9.append(text)


bot9.send = _send
bot9._stop = lambda: stopped.append(1)


class _Upd:
    class effective_chat:
        id = 42

    class message:
        date = datetime.now(timezone.utc)


asyncio.run(bot9.cmd_restart(_Upd, None))
ok(bot9.exit_code == 3 and stopped and "Restarting" in sent9[-1], "/restart says so, stops, exit code 3")
ok(store9.get_kv("restart_notify") == "1", "remembers to say 'Back online'")
_Upd.message.date = datetime.now(timezone.utc) - timedelta(minutes=5)
bot9b = Bot(a9)
bot9b.send, bot9b._stop = _send, lambda: stopped.append(2)
asyncio.run(bot9b.cmd_restart(_Upd, None))
ok(bot9b.exit_code == 0 and 2 not in stopped, "an old /restart (sent before this start) is ignored: no loop")
sent9.clear()
asyncio.run(bot9b._startup(None))
ok(any("Back online" in t for t in sent9) and store9.get_kv("running") == "1", "after restart: '✅ Back online'")
sent9.clear()
bot9c = Bot(a9)
bot9c.send = _send
asyncio.run(bot9c._startup(None))
ok(any("wasn't stopped cleanly" in t for t in sent9), "after a crash / closed window: says so on next start")
bat = open(ROOT / "start_assistant.bat", encoding="utf-8").read()
ok("ERRORLEVEL%==3" in bat and "goto run" in bat and "GEQ 5" in bat, "start_assistant.bat restarts on code 3 and crashes (max 5)")
sh = open(ROOT / "start_assistant.sh", encoding="utf-8").read()
ok("-eq 3" in sh and "-ge 5" in sh and "--notify" in sh and "\r" not in sh, "start_assistant.sh (Mac/Linux) does the same")

print("\n16. Changing the plan in words")
s10, store10, a10, p10 = make(date(2026, 11, 4))
seed(a10)
seed_progress(p10)
p10.set_topic_status("DD05", "n")
p10.set_parts("DD05", ["MUX"])
s10.now = lambda: datetime(2026, 11, 4, 12, 36, tzinfo=TZ)
cal10 = FakeCal()
day = date(2026, 11, 4)
p10.save_day(day, cal=cal10)
it10 = Interpreter(a10, p10, cal_getter=lambda: cal10)
wk = next(b for b in p10.blocks_on(day) if b.kind == "workout")
rd = next(b for b in p10.blocks_on(day) if b.kind == "reading")
ds = next(b for b in p10.blocks_on(day) if b.kind == "dsa" and b.start >= 12 * 60 + 36) if any(
    b.kind == "dsa" and b.start >= 756 for b in p10.blocks_on(day)) else None

# what the AI wrongly answered on 4 Nov (read as progress) must not touch the workout block any more
a10.llm = ScriptLLM([{"intent": "progress", "sure": True, "progress": [
    {"topic_id": "DD06", "parts_done": [], "status": "not_done", "block_id": wk.id}], "routines": []}])
it10.handle("could you replace workout with the DD study today")
ok(p10.get(wk.id).status == "planned", "a progress item can't touch a block of another topic (old bug)")

a10.llm = ScriptLLM([{"intent": "change_plan", "sure": True, "changes": [
    {"op": "replace", "block_id": wk.id, "kind": "study", "course": "DD", "day": "today"}]}])
r = it10.handle("could you replace workout with the DD study today")
nb = p10.get(wk.id)
ok(nb.kind == "study" and nb.course == "DD" and nb.start == wk.start and nb.locked,
   f"'replace workout with DD study' -> same slot, DD study, locked ({nb.title})")
ok(nb.topic_id == "DD05" and "Comparators" in r.text, "picks the DD topic you started (MSI, 4 parts left)")
ok(nb.gcal_event_id in cal10.events and wk.gcal_event_id not in cal10.events, "Google Calendar updated")
again = p10.save_day(day, from_min=13 * 60, cal=cal10)
ok(any(b.id == wk.id and b.kind == "study" for b in again), "re-plan keeps your change")
ok(not any(b.kind == "workout" and b.status == "planned" for b in again), "workout isn't re-added elsewhere today")
it10.undo(int(r.buttons[0][1].split(":")[1]))
ok(p10.get(wk.id).kind == "workout" and not p10.get(wk.id).locked, "↩️ Undo brings the workout back")
rd = next(b for b in p10.blocks_on(day) if b.kind == "reading" and b.status == "planned")

a10.llm = ScriptLLM([{"intent": "change_plan", "sure": True, "changes": [
    {"op": "move", "block_id": rd.id, "start": "23:00", "day": "today"}]}])
it10.handle("move reading to 11pm")
ok(p10.get(rd.id).start == 23 * 60 and p10.get(rd.id).locked, "'move reading to 11pm' -> 23:00, locked")
ok(any(b.id == rd.id and b.start == 23 * 60 for b in p10.save_day(day, from_min=13 * 60)), "re-plan keeps the move")

a10.llm = ScriptLLM([{"intent": "change_plan", "sure": True, "changes": [
    {"op": "add", "kind": "study", "course": "EM", "minutes": 60, "day": "today", "start": None}]}])
r = it10.handle("add an hour of EM tonight")
added = [b for b in p10.blocks_on(day) if b.locked and b.course == "EM"]
ok(added and added[0].end - added[0].start == 60 and added[0].start >= 12 * 60 + 36, f"'add an hour of EM' -> a free slot ({r.text[:60]})")

dsa_b = next((b for b in p10.blocks_on(day) if b.kind == "dsa"), None)
if dsa_b:
    a10.llm = ScriptLLM([{"intent": "change_plan", "sure": True, "changes": [
        {"op": "remove", "block_id": dsa_b.id, "day": "today"}]}])
    it10.handle("skip DSA today")
    ok(p10.get(dsa_b.id).status == "skipped", "'skip DSA today' -> removed")
    ok(not any(b.kind == "dsa" and b.status == "planned" for b in p10.save_day(day, from_min=13 * 60)),
       "re-plan doesn't bring DSA back today")

before = p10.cap(date(2026, 11, 5))
a10.llm = ScriptLLM([{"intent": "change_plan", "sure": True, "changes": [
    {"op": "lighter", "day": "tomorrow", "minutes": 60}]}])
r = it10.handle("I'm tired, make tomorrow lighter")
ok(p10.cap(date(2026, 11, 5)) == before - 60 and r.replan_days == ["2026-11-05"], "'make tomorrow lighter' -> 1 h less study")

a10.llm = ScriptLLM([{"intent": "change_plan", "sure": True, "changes": [{"op": "focus", "course": "DD", "days": 7}]}])
it10.handle("more DD this week")
ok(store10.get_kv("focus:DD") == "2026-11-10", "'more DD this week' -> DD priority for 7 days")

a10.llm = ScriptLLM([{"intent": "change_plan", "sure": True, "changes": [{"op": "replace", "block_id": None, "kind": "study"}]}])
r = it10.handle("swap it")
ok(r.kind == "chat" and "Which block" in r.text, "unclear -> asks which block instead of guessing")

a10.llm = ScriptLLM([{"intent": "change_plan", "sure": False, "changes": [
    {"op": "move", "block_id": rd.id, "start": "22:30", "day": "today"}]}])
r = it10.handle("push reading a bit earlier")
ok(r.kind == "ask" and p10.get(rd.id).start == 23 * 60, "unsure -> asks ✅ first, nothing moved yet")

a10.llm = ScriptLLM([{"intent": "action", "action": "restart"}])
ok(it10.handle("can you restart yourself").action == "restart", "'restart yourself' -> the restart command")
a10.llm = ScriptLLM([{"intent": "action", "action": "replan"}])
ok(it10.handle("replan please").action == "replan", "'replan' -> really re-plans (not just shows)")
it10.remember("sitruuna", "Change your plan like this? Reading -> 22:30")
a10.llm = ScriptLLM([{"intent": "answer", "answer": "yes"}])
r = it10.handle("yep go ahead")
ok(r.kind == "answer" and r.answer == "yes" and "Reading -> 22:30" in a10.llm.prompts[0],
   "remembers the chat, so 'yep go ahead' answers the last question")

print("\n17. Typed words in Telegram")
s11, store11, a11, p11 = make(date(2026, 11, 4))
s11.telegram_token, s11.telegram_chat_id = "1:T", "42"
s11.now = lambda: datetime(2026, 11, 4, 12, 40, tzinfo=TZ)
seed(a11)
bot11 = Bot(a11)
sent11, stops = [], []


async def _send11(text, buttons=None, rows=None):
    sent11.append((text, buttons))


bot11.send, bot11._stop = _send11, lambda: stops.append(1)


class _U:
    class effective_chat:
        id = 42

    class message:
        text = "restart"
        reply_to_message = None

        @staticmethod
        async def reply_text(t):
            pass


class _C:
    user_data = {}


asyncio.run(bot11.on_text(_U, _C))
ok(stops and bot11.exit_code == 3, "typing 'restart' restarts (no AI needed)")
_U.message.text = "replan"
sent11.clear()
asyncio.run(bot11.on_text(_U, _C))
ok(any("Re-planned" in t for t, _ in sent11), "typing 'replan' re-plans")
pid = it_pid = None
store11.set_kv("last_ask", __import__("json").dumps({"yes": "uok:7", "no": "uno:7", "at": s11.now().isoformat()}))
store11.set_kv("prop:7", __import__("json").dumps({"progress": [], "routines": [{"kind": "reading", "done": True, "amount": None}]}))
_U.message.text = "yes"
sent11.clear()
asyncio.run(bot11.on_text(_U, _C))
ok(any("Got it" in t for t, _ in sent11) and not store11.get_kv("prop:7"), "typing 'yes' = tapping ✅ on the last question")

print("\n19. Quizzes: syllabus first, best guess before it, a block a day in the last 6 days")
from pa.seed import seed_syllabi  # noqa: E402
s13, store13, a13, p13 = make(date(2026, 11, 7))
seed(a13)
seed_progress(p13)
for c, v in {"M3": 4, "DD": 1, "EM": 2, "ED": 2, "EMT": 4}.items():
    p13.set_confidence(c, v)
ev = lambda title: store13.conn.execute("SELECT * FROM events WHERE title=? AND status='active'", (title,)).fetchone()
m3q = ev("M3 Quiz 2")
E = lambda r: next(e for e in p13.exams(date(2026, 11, 7)) if e.id == r["id"])
covered = [t.id for t in p13.topics if p13.covers(E(m3q), t)]
ok(covered and all(not p13.topic(i).pre_midsem for i in covered), f"no syllabus yet: guess = topics since the midsem ({covered})")
ok("best guess" in p13.covers_line(E(m3q)), "the plan says it's a best guess")


def m3_blocks(d):
    s13.fixed = d
    return [b for b in p13.plan_day(d) if b.course == "M3" and b.is_study]


days = [date(2026, 11, 7) + timedelta(days=i) for i in range(5)]      # 7-11 Nov (quiz on 13th)
ok(all(m3_blocks(d) for d in days), "M3 (rated 4/5) still gets ≥1 block every day in the 6 days before its quiz")
ok(all(b.topic_id in covered for d in days for b in m3_blocks(d)), "those blocks are quiz topics")
s13.fixed = date(2026, 11, 13)
q_day = [b for b in p13.plan_day(date(2026, 11, 13)) if b.is_study]
before = [b for b in q_day if b.end <= 19 * 60]
ok(before and all(b.kind == "revise" and b.course == "M3" for b in before),
   f"quiz day (7 pm): blocks before it are M3 revision ({[(fmt(b.start), b.kind) for b in before]})")
ok(any(b.start >= 20 * 60 for b in q_day if b.kind != "revise" or b.course != "M3") or len(q_day) == len(before),
   "blocks after the quiz go back to normal")

store13.set_kv(f"syllabus:{m3q['id']}", _json.dumps({"topics": ["M310"], "short": "Bessel", "status": "set"}))
ok([t.id for t in p13.topics if p13.covers(E(m3q), t)] == ["M310"], "syllabus mailed: only its topics count")
ok("📘" in p13.covers_line(E(m3q)) and "best guess" not in p13.covers_line(E(m3q)), "revision says 'syllabus' not 'guess'")
s13.fixed = date(2026, 11, 9)
ok({b.topic_id for b in m3_blocks(date(2026, 11, 9))} == {"M310"}, "quiz blocks switch to the syllabus topic")
new_id = store13.add_event(title="M3 Quiz 2", type="quiz", action="update", course="M3", date="2026-11-14",
                           start_time="19:00", status="active", replaces_id=m3q["id"])
store13.update_event(m3q["id"], status="superseded")
moved = next(e for e in p13.exams(date(2026, 11, 7)) if e.id == new_id)
ok(p13.syllabus(moved) == ["M310"], "quiz date moved -> its syllabus moves with it")

lab = store13.add_event(title="EM Lab practice session", type="lab", action="add", course="EM", date="2026-11-08",
                        start_time="10:00", status="active", notes="Practice sessions will not be part of course evaluation")
lab2 = store13.add_event(title="EM Lab test", type="lab", action="add", course="EM", date="2026-11-08",
                         start_time="15:00", status="active", notes="10%")
s13.fixed = date(2026, 11, 7)
preps = [b.topic_id for b in p13.plan_day(date(2026, 11, 7)) if b.kind == "prep"]
ok(f"ev:{lab}" not in preps and f"ev:{lab2}" in preps, "no prep for ungraded sessions; graded labs still get prep")

ok(seed_syllabi(p13) and not seed_syllabi(p13), "Verilog eval syllabus seeded once")
ver = store13.conn.execute("SELECT * FROM events WHERE title LIKE 'DD Verilog%'").fetchone()
store13.update_event(ver["id"], status="active")                        # you tapped ✅
s13.fixed = date(2026, 11, 9)
dd9 = [b for b in p13.plan_day(date(2026, 11, 9)) if b.course == "DD" and b.is_study]
ok(any(b.topic_id in ("DD06", "DD12") for b in dd9), f"confirmed Verilog eval: Verilog study in its last 6 days ({[b.topic_id for b in dd9]})")

s14, store14, a14, p14 = make(date(2026, 11, 7))
seed(a14)
seed_progress(p14)
d1, d2 = date(2026, 11, 21), date(2026, 11, 22)
cu = lambda d: {b.course for b in p14.blocks_on(d) if "(catch-up)" in b.title}
for d in (d1, d2):
    s14.fixed = d
    p14.save_day(d)
ok(cu(d1) and cu(d2) and cu(d1) != cu(d2), f"catch-up blocks take turns between courses ({cu(d1)} then {cu(d2)})")

print("\n20. Syllabus from the professor's mail (or your message)")
from pa.pipeline import Assistant as _A  # noqa: E402,F401
s15, store15, a15, p15 = make(date(2026, 11, 7))
seed(a15)
a15.planner = p15
desk = a15.syllabus
q15 = store15.conn.execute("SELECT * FROM events WHERE title='M3 Quiz 2'").fetchone()
mail = "Dear students, Quiz 2 will cover Legendre polynomials and Bessel functions only."
cand = {"course": "M3", "type": "quiz", "exam": "Quiz 2", "date": None,
        "syllabus": "Legendre polynomials and Bessel functions", "source_quote": "cover Legendre polynomials and Bessel functions"}
a15.llm = ScriptLLM([{"topic_ids": ["M309", "M310"], "sure": True, "short": "Legendre, Bessel"}])
n = desk.receive(cand, mail, "M3 Quiz 2 syllabus")
ok(n and n.kind == "syllabus" and "Legendre" in n.text, "clear syllabus mail -> used at once, you're told")
ok("M310 | Bessel" in a15.llm.prompts[0] and "about" in a15.llm.prompts[0], "the AI sees your topics and lecture estimates")
e15 = next(e for e in p15.exams(date(2026, 11, 7)) if e.id == q15["id"])
ok([t.id for t in p15.topics if p15.covers(e15, t)] == ["M309", "M310"], "planner now uses exactly those topics")
ok(desk.receive(cand, mail, "again") is None, "same syllabus mailed twice -> nothing new")
n = desk.undo(q15["id"])
ok(n.kind == "syllabus" and p15.syllabus(e15) is None, "↩️ Undo -> back to the best guess")

a15.llm = ScriptLLM([{"topic_ids": ["M309", "M399"], "sure": True, "short": "x"}])
n = desk.receive({**cand, "syllabus": "Legendre and the new stuff"}, mail + " Legendre and the new stuff", "mail")
info = desk.get(q15["id"])
ok(n.kind == "confirm" and info["topics"] == ["M309"] and p15.syllabus(e15) is None,
   "made-up topic ids dropped; asks ✅ first, not used yet")
ok(any(d.startswith("sylok:") for _, d in n.buttons), "✅ / ❌ buttons")
desk.confirm(q15["id"])
ok(p15.syllabus(e15) == ["M309"], "after ✅ it's used")

a15.llm = ScriptLLM([{"topic_ids": [], "sure": False, "short": ""}])
n = desk.receive({**cand, "syllabus": "stuff from class"}, mail + " stuff from class", "mail")
ok(n.kind == "info" and p15.syllabus(e15) == ["M309"], "can't match -> says so, keeps what it had")

a15.llm = ScriptLLM([
    {"summary": "syllabus", "events": [], "syllabi": [{"course": "M3", "type": "quiz", "exam": "M3 Quiz 2", "date": None,
                                                       "syllabus": "Legendre and Bessel", "source_quote": "Legendre and Bessel"}]},
    {"topic_ids": ["M309", "M310"], "sure": True, "short": "Legendre, Bessel"}])
ns = a15.from_user_text("M3 quiz 2 syllabus is Legendre and Bessel")
ok(len(ns) == 1 and ns[0].kind == "confirm" and "sylok" in ns[0].buttons[0][1], "typed on Telegram -> asks ✅ first")
desk.confirm(q15["id"])
ok(p15.syllabus(e15) == ["M309", "M310"], "after ✅ your typed syllabus is used")
desk.reject(q15["id"])

s16, store16, a16, p16 = make(date(2026, 11, 7))
seed(a16)
a16.planner = p16
q16 = store16.conn.execute("SELECT * FROM events WHERE title='M3 Quiz 2'").fetchone()
store16.update_event(q16["id"], notes="Syllabus will begin with topics covered after Quiz 1 and include all material taught up to November 13")
a16.llm = ScriptLLM([{"topic_ids": ["M307", "M308", "M309", "M310"], "sure": False, "short": "after Quiz 1"}])
ns = a16.syllabus.backfill()
ok(len(ns) == 1 and ns[0].kind == "confirm", "a syllabus line already saved with a quiz is matched once on start (asks ✅)")
ok(a16.syllabus.backfill() == [] and not a16.llm.answers, "...and only once")

print("\n" + ("ALL PLANNER TESTS PASSED" if not FAILS else f"{len(FAILS)} FAILED"))
sys.exit(1 if FAILS else 0)
