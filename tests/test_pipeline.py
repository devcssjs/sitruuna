"""Offline test with fake emails and a fake AI (no internet, no keys needed).

    python tests/test_pipeline.py
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["SITRUUNA_EXAMPLES"] = "1"           # use the example config/timetable/topics

import pa.gcal as gcal_mod                       # noqa: E402
from pa.attachments import read_attachments      # noqa: E402
from pa.config import Settings                   # noqa: E402
from pa.gmail_fetch import Attachment, Email, html_to_text  # noqa: E402
from pa.pipeline import Assistant                # noqa: E402
from pa.reminders import morning_digest, prep_reminder  # noqa: E402
from pa.store import Store                       # noqa: E402
from pa.telegram_bot import parse_user_datetime  # noqa: E402
from pa.timetable import format_day, format_deadlines  # noqa: E402
from pa.courses import with_course                # noqa: E402
from pa.validate import canonical_course, course_codes  # noqa: E402

TZ = ZoneInfo("Asia/Kolkata")
TODAY = datetime.now(TZ).date()


def d(days: int) -> str:
    return (TODAY + timedelta(days=days)).isoformat()


def make_pdf(lines: list[str]) -> bytes:
    """Tiny valid text PDF, so we can test the midsem-schedule PDF path."""
    stream = "BT /F1 10 Tf 40 800 Td 14 TL " + " ".join(f"({l}) '" for l in lines) + " ET"
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = "%PDF-1.4\n", []
    for i, o in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs)+1}\n0000000000 65535 f \n" + "".join(f"{x:010d} 00000 n \n" for x in offsets)
    out += f"trailer\n<< /Size {len(objs)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF"
    return out.encode("latin-1")


class FakeLLM:
    """Returns pre-written AI answers, picked by a keyword in the email."""
    name, model = "fake", "fake"

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def ask(self, system, prompt, files):
        self.calls.append((prompt, files))
        source = prompt.split("---------------- SOURCE ----------------", 1)[-1]   # the email only
        for key, ans in self.answers.items():
            if key in source:
                return "```json\n" + json.dumps(ans) + "\n```"
        return '{"summary": "nothing", "events": []}'


class FakeCalendar:
    def __init__(self):
        self.events, self.n = {}, 0
        self.cancelled_instances, self.restored = [], []

    def cancel_weekly(self, store, ev):
        iid = f"inst-{ev['course']}-{ev['date']}"
        self.cancelled_instances.append(iid)
        return [iid]

    def restore_instances(self, ids):
        self.restored += ids

    def create(self, ev):
        self.n += 1
        gid = f"g{self.n}"
        self.events[gid] = (ev["title"], ev["date"], ev["start_time"])
        return gid

    def delete(self, gid):
        self.events.pop(gid, None)


def email(subject, body, atts=None, days_ago=0):
    return Email(msg_id=subject.replace(" ", "_"), sender="Timetable Division <td@goa.bits-pilani.ac.in>",
                 subject=subject, sent_at=datetime.now(TZ) - timedelta(days=days_ago), body=body,
                 attachments=atts or [])


def main():
    tmp = Path(tempfile.mkdtemp())
    (tmp / "timetable.csv").write_text(
        "day,start,end,course,kind,title,venue,must_attend\n"
        + "\n".join(f"{day},09:00,09:50,DD,lecture,L1,LT2,yes"
                    for day in ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]), encoding="utf-8")
    gcal_mod.DATA_DIR = tmp

    s = Settings(raw={"courses": {
                          "DD": {"code": "1166_ECE_INSTR_EEE_ECOM F215", "name": "Digital Design"},
                          "EM": {"code": "1166_INSTR_EEE_ECE F211", "name": "Electrical Machines"},
                          "ED": {"code": "1166_INSTR_EEE_ECOM_ECE_F214 SNS F242", "name": "Electronic Devices"},
                          "M3": {"code": "MATH F211", "name": "Mathematics III"}},
                      "timezone": "Asia/Kolkata",
                      "gmail": {"allowed_senders": ["bits-pilani.ac.in"]}},
                 tz=TZ, llm_provider="fake", gemini_key="", anthropic_key="",
                 telegram_token="", telegram_chat_id="1")
    store = Store(tmp / "test.db")
    cal = FakeCalendar()

    # ---- course code matching ----
    assert course_codes("EEE/ECE/INSTR F215") == {"EEEF215", "ECEF215", "INSTRF215"}
    assert course_codes("1166_ECE_INSTR_EEE_ECOM F215") == {"ECEF215", "INSTRF215", "EEEF215", "ECOMF215"}
    assert course_codes("1166_INSTR_EEE_ECOM_ECE_F214 SNS F242") == {"INSTRF214", "EEEF214", "ECOMF214", "ECEF214", "SNSF242"}
    c = s.courses
    assert canonical_course("ECE/EEE F215", c) == ("DD", True)
    assert canonical_course("INSTR F211", c) == ("EM", True)
    assert canonical_course("MATH F211", c) == ("M3", True)      # same number as EM, different course
    assert canonical_course("SNS F242", c) == ("ED", True)
    assert canonical_course("dd", c) == ("DD", True)
    assert canonical_course("BIO F211", c)[1] is False
    assert with_course("DD", "Quiz 2") == "DD Quiz 2" and with_course("DD", "DD Quiz 2") == "DD Quiz 2"
    assert with_course("EM", "EMT Quiz") == "EM EMT Quiz" and with_course("DD", "Additional class") == "DD Additional class"
    print("✓ course codes + short forms (DD, EM, M3, ED)")

    # ---- attachments: text PDF ----
    midsem_date = (TODAY + timedelta(days=9))
    pdf = make_pdf(["MID SEMESTER TEST SCHEDULE",
                    f"EEE/ECE/INSTR F215 DIGITAL DESIGN {midsem_date:%d/%m/%Y} 2:00 PM - 3:30 PM",
                    f"BIO F211 BIOLOGICAL CHEMISTRY {midsem_date:%d/%m/%Y} 9:00 AM - 10:30 AM",
                    f"MATH F211 MATHEMATICS III {(TODAY + timedelta(days=11)):%d/%m/%Y} 9:00 AM - 10:30 AM"])
    res = read_attachments([Attachment("Midsem_Schedule.pdf", "application/pdf", pdf),
                            Attachment("photo.jpg", "image/jpeg", b"\xff\xd8fakejpeg"),
                            Attachment("archive.zip", "application/zip", b"PK")])
    assert res.texts and "DIGITAL DESIGN" in res.texts[0][1], res
    assert res.files[0][0] == "photo.jpg" and res.skipped == ["archive.zip"]
    print("✓ attachments (PDF text read locally, image sent to AI, zip skipped)")
    assert "Quiz" in html_to_text("<p>Quiz <b>2</b></p><style>x{}</style>")
    print("✓ html email to text")

    md = midsem_date.strftime("%d/%m/%Y")
    m2 = (TODAY + timedelta(days=11)).strftime("%d/%m/%Y")
    quiz_day = TODAY + timedelta(days=3)
    llm = FakeLLM({
        "MID SEMESTER TEST SCHEDULE": {"summary": "Midsem schedule", "events": [
            {"action": "add", "type": "midsem", "course": "EEE/ECE/INSTR F215", "title": "Digital Design Midsem",
             "date": midsem_date.isoformat(), "start_time": "14:00", "end_time": "15:30", "venue": None,
             "source_file": "Midsem_Schedule.pdf, page 1",
             "source_quote": f"EEE/ECE/INSTR F215 DIGITAL DESIGN {md} 2:00 PM - 3:30 PM", "confidence": "high"},
            {"action": "add", "type": "midsem", "course": "BIO F211", "title": "Biochem Midsem",
             "date": midsem_date.isoformat(), "start_time": "09:00", "source_quote": "BIO F211", "confidence": "high"},
            {"action": "add", "type": "midsem", "course": "MATH F211", "title": "Maths III Midsem",
             "date": (TODAY + timedelta(days=11)).isoformat(), "start_time": "09:00", "end_time": "10:30",
             "source_file": "Midsem_Schedule.pdf, page 1",
             "source_quote": f"MATH F211 MATHEMATICS III {m2} 9:00 AM - 10:30 AM", "confidence": "high"},
        ]},
        "Quiz 2 announcement": {"summary": "DD quiz", "events": [
            {"action": "add", "type": "quiz", "course": "EEE F215", "title": "Digital Design Quiz 2",
             "date": quiz_day.isoformat(), "start_time": "17:00", "venue": "F105",
             "source_quote": f"Quiz 2 will be held on {quiz_day:%d %B} at 5 PM in F105", "confidence": "high"}]},
        "Assignment reminder": {"summary": "assignment", "events": [
            {"action": "add", "type": "deadline", "course": "MATH F211", "title": "Assignment 1 submission",
             "date": d(1), "start_time": "23:59", "source_quote": "submit by tomorrow midnight",
             "confidence": "high"}]},
        "Midsem rescheduled": {"summary": "change", "events": [
            {"action": "update", "type": "midsem", "course": "EEE F215", "title": "Digital Design Midsem",
             "date": d(10), "start_time": "14:00",
             "source_quote": f"DD midsem is moved to {(TODAY + timedelta(days=10)):%d %b}, same time",
             "confidence": "high"}]},
        "No class": {"summary": "cancel", "events": [
            {"action": "cancel", "type": "class", "course": "EEE F215", "title": "Digital Design lecture",
             "date": d(2), "start_time": "09:00",
             "source_quote": f"There will be no class on {(TODAY + timedelta(days=2)):%d %b}",
             "confidence": "high"}]},
    })
    a = Assistant(s, store, llm=llm, calendar=cal)

    # ---- 1. Midsem PDF: only my courses, auto-added ----
    body_and_pdf = email("Midsem schedule", "Please find the schedule attached.",
                         [Attachment("Midsem_Schedule.pdf", "application/pdf", pdf)])
    n = a.process_email(body_and_pdf)
    assert [x.kind for x in n] == ["added", "added"], [x.text for x in n]
    titles = {r["title"] for r in store.upcoming(TODAY.isoformat())}
    assert {r["course"] for r in store.upcoming(TODAY.isoformat())} == {"DD", "M3"}
    assert titles == {"Digital Design Midsem", "Maths III Midsem"}, titles
    assert len(cal.events) == 2
    print("✓ midsem PDF: BIO F211 dropped, my 2 midsems auto-added + on calendar")

    # ---- 2. clear quiz email -> auto-add ----
    n = a.process_email(email("Quiz 2 announcement", f"Quiz 2 will be held on {quiz_day:%d %B} at 5 PM in F105."))
    assert n[0].kind == "added" and n[0].buttons[0][1].startswith("undo:")
    print("✓ clear quiz email auto-added with Undo button")

    # ---- 3. relative date -> asks first ----
    n = a.process_email(email("Assignment reminder", "Reminder: submit by tomorrow midnight."))
    assert n[0].kind == "confirm" and "relative date" in n[0].text, n[0].text
    pid = n[0].event_id
    assert store.get_event(pid)["status"] == "pending"
    n2 = a.confirm(pid)
    assert store.get_event(pid)["status"] == "active" and n2.kind == "added"
    print("✓ vague email asked first; ✅ tap added it")

    # ---- 4. same email again -> no duplicate ----
    llm.calls.clear()
    assert a.process_email(email("Quiz 2 announcement", f"Quiz 2 will be held on {quiz_day:%d %B} at 5 PM in F105.")) == []
    print("✓ duplicate announcement ignored")

    # ---- 4b. syllabus mailed later for the known quiz (no date in the mail) -> attached, not 'duplicate' ----
    llm.answers = {"THE COURSE'S TOPICS": {"topic_ids": ["DD05", "DD06"], "sure": True, "short": "MSI, Verilog"},
                   "Quiz 2 syllabus": {"summary": "syllabus", "events": [], "syllabi": [
                       {"course": "EEE F215", "type": "quiz", "exam": "DD Quiz 2", "date": None,
                        "syllabus": "MSI components and combinational Verilog",
                        "source_quote": "Quiz 2 covers MSI components and combinational Verilog"}]},
                   **llm.answers}
    n = a.process_email(email("Quiz 2 syllabus", "Dear all, Quiz 2 covers MSI components and combinational Verilog."))
    qid = next(r["id"] for r in store.upcoming(TODAY.isoformat()) if r["title"] == "Digital Design Quiz 2")
    assert [x.kind for x in n] == ["syllabus"] and n[0].event_id == qid, [x.text for x in n]
    assert json.loads(store.get_kv(f"syllabus:{qid}"))["topics"] == ["DD05", "DD06"]
    assert any(c[1] == "sylundo:" + str(qid) for c in n[0].buttons)
    print("✓ syllabus mail for a known quiz: matched to topics, used, Undo button")

    # ---- 4c. lab batches: another batch's slot is skipped ----
    from pa.validate import check
    lab = lambda b, t: {"action": "add", "type": t, "course": "INSTR F211", "title": f"EM Lab Compre - P2 Batch {b}",
                        "date": d(12), "start_time": "11:00", "source_quote": f"{b}\t18.11.26 (11.00 AM)", "confidence": "high"}
    ctx = {"subject": "Lab Comprehensive Examination", "sender": "x", "sections": {}, "batch": "B2"}
    src = "B1\t18.11.26 (11.00 AM)\nB2\t18.11.26 (11.45 AM)\nB3\t18.11.26 (11.00 AM)"
    assert check(lab("B1", "lab"), src, [], s.courses, TODAY, ctx) is None
    assert check(lab("B2", "lab"), src, [], s.courses, TODAY, ctx) is not None
    assert "batch B3" in check(lab("B3", "exam"), src, [], s.courses, TODAY, ctx)["low_reason"]
    assert check(lab("B1", "lab"), src, [], s.courses, TODAY, {**ctx, "batch": None}) is not None
    print("✓ lab batches: other batches' lab slots skipped (your batch kept; exams for another batch ask first)")

    # ---- 5. midsem moved -> old replaced, calendar updated ----
    n = a.process_email(email("Midsem rescheduled", f"DD midsem is moved to {(TODAY + timedelta(days=10)):%d %b}, same time."))
    assert n[0].kind == "changed", n[0].text
    dd = [r for r in store.upcoming(TODAY.isoformat()) if r["course"] == "DD" and r["type"] == "midsem"]
    assert len(dd) == 1 and dd[0]["date"] == d(10)
    assert (dd[0]["title"], d(10), "14:00") in cal.events.values()
    assert not any(v[1] == midsem_date.isoformat() and "Digital" in v[0] for v in cal.events.values())
    print("✓ midsem date change: old removed, new added (calendar too)")

    # ---- 6. undo the change -> old date back ----
    a.undo(n[0].event_id)
    dd = [r for r in store.upcoming(TODAY.isoformat()) if r["course"] == "DD" and r["type"] == "midsem"]
    assert len(dd) == 1 and dd[0]["date"] == midsem_date.isoformat()
    print("✓ undo restored the original midsem date")

    # ---- 7. class cancelled -> timetable shows it struck through ----
    a.process_email(email("L1: No class", f"There will be no class on {(TODAY + timedelta(days=2)):%d %b}."))
    day2 = format_day(store, TODAY + timedelta(days=2))
    assert "<s>" in day2 and "cancelled" in day2, day2
    print("✓ cancelled class shown as cancelled in /timetable")

    # ---- 8. edit flow ----
    nd, nt = parse_user_datetime("24 Oct 14:00", TODAY)
    assert nd.endswith("-10-24") and nt == "14:00"
    assert parse_user_datetime("24/10", TODAY)[1] is None
    quiz = [r for r in store.upcoming(TODAY.isoformat()) if r["type"] == "quiz"][0]
    a.edit(quiz["id"], d(4), "16:00")
    q2 = store.get_event(quiz["id"])
    assert q2["date"] == d(4) and q2["start_time"] == "16:00" and q2["gcal_event_id"] in cal.events
    print("✓ edit: date/time changed and calendar re-synced")

    # ---- 9. views ----
    print("\n----- /timetable (today) -----\n" + format_day(store, TODAY))
    print("\n----- /deadlines -----\n" + format_deadlines(store, TODAY))
    print("\n----- 8 am digest -----\n" + morning_digest(store, s))
    print("\n----- 7 pm prep -----\n" + str(prep_reminder(store, s)))

    # ---- 10. the example timetable (data/timetable.example.csv) ----
    gcal_mod.DATA_DIR = Path(__file__).resolve().parent.parent / "data"
    real = gcal_mod.load_weekly_classes()
    assert len(real) == 25, len(real)
    assert sum(r["must_attend"] for r in real) == 16   # M3, ED, EDCC, EMT lectures + labs
    assert all(r["course"] in ("DD", "EM", "EMT", "ED", "M3", "EDCC") for r in real)
    assert not any("LTC" in r["venue"] for r in real)
    monday = TODAY - timedelta(days=TODAY.weekday())
    s.raw["courses"].update({"EMT": {"code": "1166_ECE_EEE_INSTR F212"}, "EDCC": {"code": "GS F212"}})
    print("\n----- your real Monday -----\n" + format_day(Store(tmp / "empty.db"), monday))
    print("\n----- your real Friday -----\n" + format_day(Store(tmp / "empty.db"), monday + timedelta(days=4)))
    print("✓ real timetable: 25 classes, 16 must-attend, short forms, no LTC")

    # ---- 11. lecture cancelled / postponed / holiday, fests filtered, /cleanup ----
    store2, cal2 = Store(tmp / "classes.db"), FakeCalendar()
    s.raw["courses"].update({"EMT": {"code": "1166_ECE_EEE_INSTR F212"}, "EDCC": {"code": "GS F212"}})
    nxt = TODAY + timedelta(days=7 - TODAY.weekday())          # next Monday
    fri, sat, tue = nxt + timedelta(days=4), nxt + timedelta(days=5), nxt + timedelta(days=1)
    q = lambda d_: d_.strftime("%d %B")
    llm2 = FakeLLM({
        "Naukri": {"summary": "placement", "events": [
            {"action": "add", "type": "deadline", "course": None, "title": "Naukri EROH registration",
             "date": d(5), "source_quote": "x", "academic": False, "confidence": "high"},
            {"action": "add", "type": "event", "course": None, "title": "USHUS fest",
             "date": d(6), "source_quote": "x", "confidence": "high"}]},
        "Registration": {"summary": "academic", "events": [
            {"action": "add", "type": "deadline", "course": None, "title": "Course withdrawal deadline",
             "date": d(6), "source_quote": f"withdrawal closes on {(TODAY + timedelta(days=6)):%d %B}",
             "academic": True, "confidence": "high"}]},
        "DD lecture cancelled": {"summary": "cancel", "events": [
            {"action": "cancel", "type": "class", "course": "DD", "title": "DD lecture cancelled",
             "date": fri.isoformat(), "start_time": None,
             "source_quote": f"There will be no DD lecture on {q(fri)}", "confidence": "high"}]},
        "M3 postponed": {"summary": "moved", "events": [
            {"action": "cancel", "type": "class", "course": "M3", "title": "M3 lecture cancelled",
             "date": nxt.isoformat(), "start_time": "16:00",
             "source_quote": f"The M3 lecture on {q(nxt)} is postponed", "confidence": "high"},
            {"action": "add", "type": "class", "course": "M3", "title": "M3 lecture (moved)",
             "date": sat.isoformat(), "start_time": "10:00", "end_time": "11:00", "venue": "LT1",
             "notes": f"moved from {q(nxt)}",
             "source_quote": f"It will now be held on {q(sat)} at 10 AM in LT1", "confidence": "high"}]},
        "Holiday": {"summary": "holiday", "events": [
            {"action": "cancel", "type": "class", "course": None, "title": "Classes suspended (holiday)",
             "date": tue.isoformat(), "academic": True,
             "source_quote": f"No classes on {q(tue)} on account of the holiday", "confidence": "high"}]},
    })
    a2 = Assistant(s, store2, llm=llm2, calendar=cal2)

    assert a2.process_email(email("Naukri campus drive", "...")) == []
    n = a2.process_email(email("Registration notice", f"withdrawal closes on {(TODAY + timedelta(days=6)):%d %B}"))
    assert n and n[0].kind == "added"
    print("✓ fests/placements skipped; academic notice for everyone kept")

    n = a2.process_email(email("L1: DD lecture cancelled", f"There will be no DD lecture on {q(fri)}."))
    assert n[0].kind == "cancelled" and cal2.cancelled_instances
    fri_view = format_day(store2, fri)
    assert "<s>10:00–11:00  🏫 DD lecture" in fri_view and "<s>11:00–13:00" not in fri_view, fri_view
    print("✓ 'DD lecture cancelled' strikes Friday's lecture but NOT the DD lab")
    assert a2.process_email(email("L1: DD lecture cancelled again", f"There will be no DD lecture on {q(fri)}.")) == []
    print("✓ repeated cancellation email ignored")

    n = a2.process_email(email("L2: M3 postponed", f"The M3 lecture on {q(nxt)} is postponed. It will now be held on {q(sat)} at 10 AM in LT1."))
    assert [x.kind for x in n] == ["cancelled", "added"], [x.text for x in n]
    mon_view, sat_view = format_day(store2, nxt), format_day(store2, sat)
    assert "<s>16:00–17:00  🏫 M3 lecture" in mon_view and "<s>14:00" not in mon_view, mon_view
    assert "10:00–11:00  🏫 M3 L2 lecture (moved) · LT1 ⭐" in sat_view, sat_view
    print("✓ postponed M3: Monday struck, Saturday slot added with ⭐ (M3 takes attendance)")
    print("\n----- next Monday -----\n" + mon_view + "\n----- next Saturday -----\n" + sat_view + "\n")

    a2.process_email(email("Holiday notice", f"No classes on {q(tue)} on account of the holiday"))
    tue_view = format_day(store2, tue)
    assert tue_view.count("<s>") == 5, tue_view
    print("✓ holiday: all 5 Tuesday classes struck")

    cancel_id = n[0].event_id
    a2.undo(cancel_id)
    assert cal2.restored and "<s>16:00" not in format_day(store2, nxt)
    print("✓ undo a cancellation: class back in timetable and on Google Calendar")

    store2.add_event(title="Naukri EROH (old junk)", type="event", action="add", course=None,
                     date=d(3), status="active", confidence="high", gcal_event_id="junk1")
    cal2.events["junk1"] = ("junk", d(3), None)
    cands = a2.cleanup_candidates()
    assert any(r["title"].startswith("Naukri") for r in cands)
    removed = a2.cleanup_apply()
    assert removed == len(cands) and "junk1" not in cal2.events and not a2.cleanup_candidates()
    print(f"✓ /cleanup removed {removed} course-less items (and from Google Calendar)")

    # ---- 12. heads-up alerts at 60 and 15 min ----
    from unittest import mock
    from pa.reminders import soon_reminders
    store3 = Store(tmp / "soon.db")
    mon_class = datetime.combine(nxt, datetime.strptime("16:00", "%H:%M").time(), tzinfo=TZ)   # M3 ⭐
    def at(minutes_before):
        with mock.patch.object(Settings, "now", lambda self: mon_class - timedelta(minutes=minutes_before)):
            return soon_reminders(store3, s)
    assert at(75) == []
    a60 = at(60); assert len(a60) == 1 and a60[0].startswith("⏳ In 60 min: M3 lecture"), a60
    assert at(59) == [] and at(30) == []
    a15 = at(15); assert len(a15) == 1 and a15[0].startswith("🔔 In 15 min: M3 lecture") and "leave now" in a15[0], a15
    assert at(14) == [] and at(1) == []
    store4 = Store(tmp / "soon2.db")
    with mock.patch.object(Settings, "now", lambda self: mon_class - timedelta(minutes=10)):
        late = soon_reminders(store4, s)
    assert len(late) == 1 and late[0].startswith("🔔 In 10 min"), late
    print("✓ alerts: 60 min ⏳ + 15 min 🔔, each once; late start sends only the 15-min one")
    print("   " + a60[0] + "\n   " + a15[0])

    # ---- 13. make-ups ask first; /rescan doesn't duplicate or bring back ignored items ----
    mk = (TODAY + timedelta(days=6))
    store5 = Store(tmp / "rescan.db")
    llm5 = FakeLLM({
        "Make-up quiz": {"summary": "makeup", "events": [
            {"action": "add", "type": "quiz", "course": "DD", "title": "DD Quiz-1 make-up",
             "date": mk.isoformat(), "start_time": "18:00",
             "source_quote": f"The make-up for Quiz-1 will be on {mk:%d %B} at 6 PM", "confidence": "high"}]},
        "Make-up class": {"summary": "makeup class", "events": [
            {"action": "add", "type": "class", "course": "EM", "title": "EM make-up lecture",
             "date": mk.isoformat(), "start_time": "09:00",
             "source_quote": f"Make-up lecture on {mk:%d %B} at 9 AM", "confidence": "high"}]},
        "EMT assignment": {"summary": "assignment", "events": [
            {"action": "add", "type": "deadline", "course": "EMT", "title": "EMT Assignment 2",
             "date": d(8), "start_time": "23:59",
             "source_quote": f"Submit Assignment 2 by {(TODAY + timedelta(days=8)):%d %B}, 11:59 PM", "confidence": "high"}]},
        "ED quiz": {"summary": "quiz", "events": [
            {"action": "add", "type": "quiz", "course": "ED", "title": "ED Quiz 2",
             "date": d(9), "start_time": "17:00",
             "source_quote": f"ED Quiz 2 on {(TODAY + timedelta(days=9)):%d %B} at 5 PM", "confidence": "high"}]},
    })
    mails = [email("Make-up quiz", f"The make-up for Quiz-1 will be on {mk:%d %B} at 6 PM"),
             email("L2: Make-up class", f"Make-up lecture on {mk:%d %B} at 9 AM"),
             email("EMT assignment", f"Submit Assignment 2 by {(TODAY + timedelta(days=8)):%d %B}, 11:59 PM"),
             email("ED quiz", f"ED Quiz 2 on {(TODAY + timedelta(days=9)):%d %B} at 5 PM")]

    class FakeGmail:
        def build_query(self, since, days=None): return f"newer_than:{days}d"
        def list_ids(self, q, limit=500): return [m.msg_id for m in mails]
        def fetch(self, i): return next(m for m in mails if m.msg_id == i)

    a5 = Assistant(s, store5, llm=llm5, calendar=FakeCalendar(), gmail=FakeGmail())
    first = a5.rescan(60)
    kinds = {x.event_id and store5.get_event(x.event_id)["title"]: x.kind for x in first}
    assert kinds["DD Quiz-1 make-up"] == "confirm", kinds          # make-up quiz asks first
    assert kinds["EM L2 make-up lecture"] == "added", kinds           # make-up LECTURE is a normal class
    assert kinds["EMT Assignment 2"] == "added" and kinds["ED Quiz 2"] == "added"
    print("✓ make-up quiz asks first; make-up lecture added normally")
    a5.ignore([x for x in first if x.kind == "confirm"][0].event_id)       # you ❌ the make-up
    a5.undo([x for x in first if "ED Quiz 2" in x.text][0].event_id)        # and ↩️ undo ED Quiz 2
    again = a5.rescan(60)
    assert again == [], [x.text for x in again]
    active = [r["title"] for r in store5.upcoming(TODAY.isoformat())]
    assert sorted(active) == ["EM L2 make-up lecture", "EMT Assignment 2"], active
    print("✓ /rescan twice: no duplicates; ignored make-up and undone quiz stay gone")

    # ---- 14. AI limit: stop + keep what was read; retry list for internet failures ----
    from pa.llm import QuotaExhausted

    class LimitedLLM(FakeLLM):
        def __init__(self, answers, allowed):
            super().__init__(answers); self.allowed = allowed
        def ask(self, system, prompt, files):
            if len(self.calls) >= self.allowed:
                raise QuotaExhausted("gemini: daily limit reached", 3600)
            return super().ask(system, prompt, files)

    store6 = Store(tmp / "quota.db")
    a6 = Assistant(s, store6, llm=LimitedLLM(llm5.answers, allowed=2), calendar=FakeCalendar(), gmail=FakeGmail())
    out = a6.rescan(60)
    stop = [x for x in out if x.kind == "stop"]
    assert len(stop) == 1 and "daily limit" in stop[0].text and "stopped after 2" in stop[0].text, [x.text for x in out]
    assert len(store6.conn.execute("select * from processed_emails").fetchall()) == 2      # kept the 2 it read
    assert store6.get_kv("retry_ids") in (None, "{}")                                    # nothing queued to resume
    assert a6.ai_paused() and a6.check_mail() == []                                     # checks pause, no AI calls
    print("✓ AI limit hit: stopped after 2, kept them, no resume queue, checks paused until reset")
    print("   " + stop[0].text.replace(chr(10), " "))

    # internet failure -> retry next check
    class FlakyGmail(FakeGmail):
        down = {mails[2].msg_id}
        def build_query(self, since, days=None): return "q"
        def fetch(self, i):
            if i in self.down:
                raise OSError("getaddrinfo failed")
            return super().fetch(i)
    store7 = Store(tmp / "retry.db")
    g = FlakyGmail()
    a7 = Assistant(s, store7, llm=FakeLLM(llm5.answers), calendar=FakeCalendar(), gmail=g)
    a7.check_mail()
    assert mails[2].msg_id in a7._retry_list() and not store7.is_processed(mails[2].msg_id)
    g.down = set()
    store7.set_kv("last_mail_check", (datetime.now(TZ) + timedelta(days=1)).isoformat())  # it's NOT in the new-mail search any more
    class OnlyNewGmail(FlakyGmail):
        def list_ids(self, q, limit=500): return []
    a7.gmail = OnlyNewGmail(); a7.gmail.down = set()
    a7.check_mail()
    assert store7.is_processed(mails[2].msg_id) and a7._retry_list() == []
    assert any(r["title"] == "EMT Assignment 2" for r in store7.upcoming(TODAY.isoformat()))
    print("✓ email that failed to download is retried on the next check and added")

    class DeadGmail(FakeGmail):
        def fetch(self, i): raise OSError("getaddrinfo failed")
    a8 = Assistant(s, Store(tmp / "dead.db"), llm=FakeLLM({}), calendar=FakeCalendar(),
                   gmail=type("G", (DeadGmail,), {"list_ids": lambda self, q, limit=500: [f"x{k}" for k in range(20)]})())
    out = a8.check_mail()
    assert len(out) == 1 and out[0].kind == "stop" and "Internet" in out[0].text
    assert a8.store.get_kv("last_mail_check") is None          # not advanced -> no new mail lost
    print("✓ internet down: stops after 5 failures (no 67-error spam), new mail not lost")

    def validate_item(day):
        return {"title": "ED Compensatory class", "type": "class", "action": "add", "course": "ED",
                "date": day.isoformat(), "start_time": "18:00", "end_time": None, "venue": "DLT08", "notes": None,
                "source_file": "email body", "source_quote": "x", "section": None}

    # ---- 15. section check (the ED compensatory-class case) + expiring pending items ----
    from pa.validate import sender_name, section_tags
    assert sender_name('"JANE DOE . (via BITS Goa)" <noreply@bits-pilani.ac.in>') == "Jane Doe"
    assert section_tags("1166_INSTR_EEE_ECOM_ECE_F214 SNS F242: L1: Compensatory class today", None) == {"L1"}
    store9 = Store(tmp / "sections.db")
    comp_day = TODAY + timedelta(days=2)
    comp = {"summary": "comp", "events": [
        {"action": "add", "type": "class", "course": "ED", "title": "ED Compensatory class",
         "date": comp_day.isoformat(), "start_time": "18:00", "venue": "DLT08",
         "source_quote": f"we will have the make up session on {comp_day:%d %B} at 6pm in DLT08", "confidence": "high"}]}
    a9 = Assistant(s, store9, llm=FakeLLM({"Compensatory": comp}), calendar=FakeCalendar())
    def comp_mail(subject, sender='"JANE DOE . (via BITS Goa)" <noreply@bits-pilani.ac.in>'):
        e = email(subject, f"we will have the make up session on {comp_day:%d %B} at 6pm in DLT08")
        e.msg_id += sender[:5]; e.sender = sender
        return e
    assert a9.process_email(comp_mail("1166_INSTR_EEE_ECOM_ECE_F214 SNS F242: L1: Compensatory class")) == []
    print("✓ 'L1: Compensatory class' skipped: you're in ED L2")
    n = a9.process_email(comp_mail("1166_INSTR_EEE_ECOM_ECE_F214 SNS F242: L2: Compensatory class"))
    assert n[0].kind == "added" and "ED L2 Compensatory class" in n[0].text and "Jane Doe" in n[0].text, n[0].text
    print("✓ 'L2: Compensatory class' added as 'ED L2 Compensatory class', shows who sent it")
    store9b = Store(tmp / "sections2.db"); a9.store = store9b
    n = a9.process_email(comp_mail("ED: Compensatory class"))
    assert n[0].kind == "added", n[0].text
    print("✓ no section named -> meant for everyone, added normally (any professor)")
    store9c = Store(tmp / "sections3.db"); a9.store = store9c
    quiz_other = {"summary": "quiz", "events": [
        {"action": "add", "type": "quiz", "course": "ED", "title": "ED Quiz 3", "date": comp_day.isoformat(),
         "start_time": "17:00", "source_quote": f"Quiz 3 on {comp_day:%d %B} at 5 PM", "confidence": "high"}]}
    a9.llm = FakeLLM({"Quiz 3": quiz_other})
    e = email("1166_INSTR_EEE_ECOM_ECE_F214 SNS F242: L1: Quiz 3", f"Quiz 3 on {comp_day:%d %B} at 5 PM")
    n = a9.process_email(e)
    assert n[0].kind == "confirm" and "sent to L1" in n[0].text, n[0].text
    print("✓ quiz addressed to another section -> asks you first ('sent to L1')")

    # make a pending item to test expiry / nudges
    store9b = Store(tmp / "sections4.db"); a9.store = store9b
    n = [a9.apply({**validate_item(comp_day), "confidence": "low", "low_reason": "test"})]
    # pending expires once its time passes; nudged within 3 h
    pid = n[0].event_id
    a9.store = store9b
    start = datetime.combine(comp_day, datetime.strptime("18:00", "%H:%M").time(), tzinfo=TZ)
    with mock.patch.object(Settings, "now", lambda self: start - timedelta(hours=5)):
        assert a9.pending_due_soon() == [] and a9.expire_pending() == []
    with mock.patch.object(Settings, "now", lambda self: start - timedelta(hours=2)):
        due = a9.pending_due_soon(); assert [r["id"] for r in due] == [pid]
        assert a9.pending_due_soon() == []                       # only nudged once
    with mock.patch.object(Settings, "now", lambda self: start + timedelta(minutes=1)):
        gone = a9.expire_pending()
    assert [r["id"] for r in gone] == [pid] and store9b.get_event(pid)["status"] == "expired"
    assert "expired" in a9.confirm(pid).text and store9b.pending() == []
    print("✓ pending item: nudged once 2 h before, auto-declined (⌛ expired) when its time passed")

    # ---- 16. Telegram bot builds ----
    from pa.telegram_bot import Bot
    s.telegram_token = "123456:TEST-TOKEN-not-real"
    Bot(a)
    print("\n✓ Telegram bot built with all commands and scheduled jobs")
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
