"""Offline tests for the Notion builder and sync, against a fake Notion (no token, no internet).

    python tests/test_notion.py
"""
import asyncio
import json
import io
import contextlib
import os
import sys
import uuid
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["SITRUUNA_EXAMPLES"] = "1"           # use the example config/timetable/topics
sys.path.insert(0, str(Path(__file__).resolve().parent))

_exit = sys.exit
sys.exit = lambda *a: None
with contextlib.redirect_stdout(io.StringIO()):
    import test_planner as T                      # reuse its settings/store helpers (its own tests run quietly)
sys.exit = _exit

from pa import notion_schema as S                   # noqa: E402
from pa.notion_client import NotionError, page_id_from  # noqa: E402
from pa.notion_sync import NotionWorkspace           # noqa: E402
from pa.planner import Planner                       # noqa: E402
from pa.seed import seed, seed_progress              # noqa: E402

FAILS = []


def ok(cond, msg):
    print(("  ✅ " if cond else "  ❌ ") + msg)
    if not cond:
        FAILS.append(msg)


class FakeNotion:
    """Behaves like the bits of Notion's API Sitruuna uses, and records every call."""

    def __init__(self, refuse_native_icons=False, refuse_view=None, refuse_dashboard=False):
        self.calls = []
        self.pages = {}                    # id -> {"parent", "properties", "in_trash", ...}
        self.dbs = {}                      # database id -> data source id
        self.ds = {}                       # data source id -> {"properties": {name: {"id", ...}}}
        self.blocks = {}
        self.views = []
        self.db_icons = {}
        self.hq_children = []
        self.refuse_native_icons = refuse_native_icons
        self.refuse_view = refuse_view
        self.refuse_dashboard = refuse_dashboard
        self.dash = {}                     # dashboard id -> rows (lists of widget names)
        self.deleted = []
        self.hq = str(uuid.uuid4())
        self.pages[self.hq] = {"title": "Sitruuna HQ"}

    def _id(self):
        return str(uuid.uuid4())

    def _log(self, name, body=None):
        self.calls.append((name, body))

    def _check_icon(self, body):
        icon = body.get("icon") or {}
        if icon.get("type") == "icon":
            name = icon["icon"]["name"]
            if self.refuse_native_icons is True or name in (self.refuse_native_icons or ()):
                raise NotionError(400, "validation_error", f'Invalid icon name "{name}". Use a valid Notion API icon name.')

    def me(self):
        return {"name": "Sitruuna"}

    def get_page(self, pid):
        if pid not in self.pages:
            raise NotionError(404, "object_not_found", "Could not find page")
        return self.pages[pid]

    def update_page(self, pid, **fields):
        self._log("update_page", fields)
        self._check_icon(fields)
        p = self.pages.get(pid)
        if not p:
            raise NotionError(404, "object_not_found", "Could not find page")
        if p.get("in_trash") and "in_trash" not in fields:
            raise NotionError(400, "validation_error", "Can't edit block that is archived.")
        p.update(fields)
        return p

    def archive_page(self, pid):
        return self.update_page(pid, in_trash=True)

    def create_page(self, body):
        self._log("create_page", body)
        self._check_icon(body)
        ds = body["parent"]["data_source_id"]
        for name in body["properties"]:
            assert name in self.ds[ds]["properties"], f"unknown column {name}"
        pid = self._id()
        self.pages[pid] = dict(body)
        return {"id": pid}

    def create_database(self, body):
        self._log("create_database", body)
        self._check_icon(body)
        dbid, dsid = self._id(), self._id()
        self.dbs[dbid] = dsid
        self.db_icons[dbid] = body.get("icon")
        self.hq_children.append({"id": dbid, "type": "child_database"})
        self.ds[dsid] = {"properties": {n: {"id": self._id()[:4], **cfg} for n, cfg in body["initial_data_source"]["properties"].items()}}
        return {"id": dbid, "data_sources": [{"id": dsid, "name": body["title"][0]["text"]["content"]}]}

    def get_database(self, dbid):
        return {"data_sources": [{"id": self.dbs[dbid]}]}

    def update_database(self, dbid, **fields):
        self._log("update_database", fields)
        self._check_icon(fields)
        self.db_icons[dbid] = fields.get("icon")
        return {}

    def list_children(self, block_id):
        return {"results": list(self.hq_children)}

    def delete_block(self, bid):
        self._log("delete_block", bid)
        assert bid not in self.dbs, "must never delete a real database"
        self.deleted.append(bid)
        self.hq_children = [b for b in self.hq_children if b["id"] != bid]
        return {}

    def get_data_source(self, dsid):
        if dsid not in self.ds:
            raise NotionError(404, "object_not_found", "no data source")
        return self.ds[dsid]

    def update_data_source(self, dsid, properties):
        self._log("update_data_source", properties)
        for name, cfg in properties.items():
            self.ds[dsid]["properties"][name] = {"id": self._id()[:4], **cfg}
            rel = cfg.get("relation")
            if rel and rel.get("type") == "dual_property":
                back = rel["dual_property"]["synced_property_name"]
                self.ds[rel["data_source_id"]]["properties"][back] = {"id": self._id()[:4], "relation": {}}
        return {}

    def append_blocks(self, block_id, children):
        self._log("append_blocks", children)
        res = []
        for ch in children:
            bid = self._id()
            self.blocks[bid] = ch
            res.append({"id": bid, "type": ch["type"]})
            if block_id == self.hq:
                self.hq_children.append({"id": bid, "type": ch["type"], **{ch["type"]: ch.get(ch["type"], {})}})
        return {"results": res}

    def update_block(self, bid, body):
        self._log("update_block", body)
        self.blocks[bid].update(body)
        return {}

    def create_view(self, body):
        self._log("create_view", body)
        if self.refuse_view and body["name"] == self.refuse_view:
            raise NotionError(400, "validation_error", "chart configuration is invalid")
        assert sum(k in body for k in ("database_id", "view_id", "create_database")) == 1
        if body["type"] == "dashboard" and self.refuse_dashboard:
            raise NotionError(400, "validation_error", "dashboard views are not available")
        vid = self._id()
        if "view_id" in body:                                   # a widget on a dashboard
            rows, pl = self.dash[body["view_id"]], body["placement"]
            if pl["type"] == "new_row":
                rows.insert(pl.get("row_index", len(rows)), [body["name"]])
            else:
                assert pl["row_index"] < len(rows) and len(rows[pl["row_index"]]) < 4
                rows[pl["row_index"]].append(body["name"])
        if body["type"] == "dashboard":
            self.dash[vid] = []
        if "create_database" in body:                           # a linked view block on the HQ page
            blk = {"id": self._id(), "type": "child_database"}
            after = (body["create_database"].get("position") or {}).get("block_id")
            idx = next((i + 1 for i, b in enumerate(self.hq_children) if b["id"] == after), len(self.hq_children))
            self.hq_children.insert(idx, blk)
        self.views.append(body)
        return {"id": vid}

    def query(self, ds, filter=None, cursor=None):
        res = [{"id": pid, "in_trash": bool(pg.get("in_trash")), "properties": pg.get("properties", {})}
               for pid, pg in self.pages.items() if (pg.get("parent") or {}).get("data_source_id") == ds]
        return {"results": res, "has_more": False}

    def count(self, name):
        return sum(1 for c, _ in self.calls if c == name)


def make(fake, today=date(2026, 11, 4), layout="stacked"):
    s, store, a, p = T.make(today)
    s.raw = dict(s.raw, notion=dict(s.raw.get("notion") or {}, hq_layout=layout))
    s.now = lambda: datetime(2026, 11, 4, 13, 30, tzinfo=T.TZ)
    s.notion_token = "ntn_test"
    s.notion_hq_page = f"https://www.notion.so/Sitruuna-HQ-{fake.hq.replace('-', '')}?pvs=4"
    seed(a)
    seed_progress(p)
    p.set_topic_status("DD05", "n")
    p.set_parts("DD05", ["MUX"])
    p.save_day(today)
    return s, store, a, p, NotionWorkspace(s, store, p, fake)


print("\n1. Page links")
ok(page_id_from("https://www.notion.so/Sitruuna-HQ-1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d?pvs=4") == "1a2b3c4d-5e6f-7a8b-9c0d-1e2f3a4b5c6d",
   "the HQ id is read from a 'Copy link' URL")
ok(page_id_from("not a link") is None, "a wrong link is caught")

print("\n2. Building the workspace")
fake = FakeNotion()
s, store, a, p, ws = make(fake)
r = ws.setup()
ok(fake.count("create_database") == 9, f"9 databases made ({fake.count('create_database')})")
course_props = fake.ds[ws.ds("courses")]["properties"]
ok(all(x in course_props for x in ("Topics", "Exams", "Sessions", "Tasks")), "courses link back to topics, exams, sessions, tasks")
ok("Syllabus" in fake.ds[ws.ds("exams")]["properties"] and "Exams" in fake.ds[ws.ds("topics")]["properties"],
   "exams link to the topics they cover")
ok(all(f in fake.ds[ws.ds(k)]["properties"] for k, f, _ in S.FORMULAS), "progress / countdown / ready formulas added")
db_views = [v for v in fake.views if "database_id" in v]
ok(len(db_views) == len(S.views()), f"{len(db_views)} database views (board, calendar, Behind, gallery...)")
board = next(v for v in db_views if v["name"] == "Board" and v["data_source_id"] == ws.ds("topics"))
ok(board["configuration"]["group_by"]["property_id"] == fake.ds[ws.ds("topics")]["properties"]["Status"]["id"],
   "topic board is grouped by Status (by its real column id)")
ok(all(fake.ds[ws.ds(k)]["properties"].get(n, {}).get("rollup", {}).get("function") == "percent_checked"
       for k, n, *_ in S.ROLLUPS) and "Done" in fake.ds[ws.ds("topics")]["properties"],
   "'Syllabus done' and 'Ready' are rollups over each topic's Done tick (no failing formulas)")
kinds = [b["type"] for b in fake.hq_children if b["id"] not in fake.dbs]
ok(kinds == ["callout"] + ["heading_2", "child_database"] * 4 + ["column_list", "paragraph"],
   "HQ (free plan): banner, then Today's plan / Coming up / Open tasks / Behind, shortcut cards at the bottom")
ok(not any(v["type"] == "chart" for v in fake.views), "no charts (free Notion allows only one)")
ok(not store.get_kv("notion:hq:dashboard") and not any("type" in v and v["type"] == "dashboard" for v in fake.views),
   "no dashboard is attempted on the free-plan layout")
links = next(b for b in fake.hq_children if b["type"] == "column_list")["column_list"]["children"]
ok(len(links) == 5 and all(c["column"]["children"][0]["type"] == "link_to_page" for c in links),
   "5 shortcut cards in columns (Daily Log, Courses, Calendar, Tasks, Weekly Reviews)")

print("\n2b. Dashboard layout (paid Notion plan)")
faked = FakeNotion()
sd, stored, ad, pd, wsd = make(faked, layout="dashboard")
wsd.setup()
dash_id = stored.get_kv("notion:hq:dashboard")
ok(dash_id and faked.dash[dash_id] == [["Today's plan", "Coming up · 14 days", "Open tasks"], ["Behind"]],
   "hq_layout: dashboard -> sections side by side in rows")
ok(fake.pages[fake.hq].get("cover") and fake.pages[fake.hq]["icon"]["type"] == "icon", "HQ gets the lemon icon and cover")
ok(not r.warnings, f"no problems ({r.warnings})")

print("\n3. Running setup again changes nothing")
before = len(fake.calls)
r2 = ws.setup()
new = [c for c, _ in fake.calls[before:] if c in ("create_database", "create_view", "append_blocks")]
ok(not new and r2.created == 0, "no databases, views or blocks made twice")

print("\n4. Things Notion refuses don't stop the build")
fake2 = FakeNotion(refuse_native_icons={"notebook", "book closed"}, refuse_view="Open tasks")  # stacked
s2, store2, a2, p2, ws2 = make(fake2)
r = ws2.setup()
log_icon = fake2.db_icons[ws2.db("log")["database_id"]]
ok(len(fake2.dbs) == 9 and log_icon == {"type": "icon", "icon": {"name": "journal", "color": "brown"}},
   "icon names Notion doesn't know are skipped one by one (notebook ✗, book closed ✗ -> journal)")
ok(fake2.db_icons[ws2.db("reading")["database_id"]]["type"] == "icon", "the databases after it still get Notion icons (no emoji switch)")
ok(any("Open tasks" in w for w in r.warnings) and len([v for v in fake2.views if "create_database" in v]) == 3,
   "a refused view is reported; the other sections are still made")
fake2.refuse_view = None
before = len(fake2.views)
r = ws2.setup()
added = fake2.views[before:]
heads2 = json.loads(store2.get_kv("notion:hq:headings"))
ok(len(added) == 1 and added[0]["name"] == "Open tasks"
   and added[0]["create_database"]["position"]["block_id"] == heads2["Open tasks"],
   "running setup again adds just the missing section, under its heading")

fake5 = FakeNotion(refuse_dashboard=True)
s5, store5, a5, p5, ws5 = make(fake5, layout="dashboard")
r = ws5.setup()
heads5 = [b for b in fake5.hq_children if b["type"] == "heading_2"]
ok(len(heads5) == 4 and len([v for v in fake5.views if "create_database" in v]) == 4 and any("⋮⋮" in w for w in r.warnings),
   "no dashboard in this Notion -> stacked sections + the drag tip")
ok(not any("filter(" in e for k, n, e in S.FORMULAS), "no formulas that filter linked pages (Notion refused those)")

print("\n4b. Free plan: the dashboard (needs a paid plan) -> back to the linear HQ")
fake3 = FakeNotion()
s3, store3, a3, p3, ws3 = make(fake3, layout="dashboard")
ws3.setup()
dash_block = next(b["id"] for b in fake3.hq_children if b["type"] == "child_database" and b["id"] not in fake3.dbs)
ws3.s.raw = dict(ws3.s.raw, notion=dict(ws3.s.raw["notion"], hq_layout="stacked"))
real = [b for b in fake3.hq_children if b["id"] in fake3.dbs]
ws3.setup()
left = [b["type"] for b in fake3.hq_children if b["id"] not in fake3.dbs]
ok(dash_block in fake3.deleted and left == ["callout"] + ["heading_2", "child_database"] * 4 + ["column_list", "paragraph"],
   f"dashboard to Trash; banner, 4 headings with views, then shortcut cards ({len(left)} blocks)")
ok(all(b["id"] in [x["id"] for x in fake3.hq_children] for b in real), "your 9 databases are never touched")
before = len(fake3.calls)
ws3.setup()
ok(not [c for c, _ in fake3.calls[before:] if c in ("create_view", "append_blocks", "delete_block")], "running it again changes nothing")

print("\n4c. Repairing a stacked HQ from the very first version")
fake3 = FakeNotion()
s3, store3, a3, p3, ws3 = make(fake3, layout="dashboard")
ws3.setup()
for k in S.DATABASES:                                     # pretend: emoji icons, no section memory
    store3.set_kv(f"notion:icon:{k}", "")
# what your HQ looks like now: banner, then 6 headings each with a stacked view ('Syllabus done' missing), footer
for k in [k for k in store3.conn.execute("SELECT key FROM kv WHERE key LIKE 'notion:hq:%'").fetchall()]:
    store3.set_kv(k[0], "")
store3.set_kv("notion:hq:callout", "c0")
real = [b for b in fake3.hq_children if b["id"] in fake3.dbs]
kids = list(real) + [{"id": "c0", "type": "callout"}]
OLD6 = ["Today's plan", "Coming up · 14 days", "Open tasks", "Syllabus done", "Study this week", "Behind"]
for i, sec in enumerate([(n,) for n in OLD6]):
    kids.append({"id": f"h{i}", "type": "heading_2", "heading_2": {"rich_text": [{"plain_text": sec[0]}]}})
    if sec[0] != "Syllabus done":
        kids.append({"id": f"v{i}", "type": "child_database"})
kids.append({"id": "p0", "type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "Built by Sitruuna. Exams, topics..."}]}})
fake3.hq_children = kids
before_db = fake3.count("update_database")
ws3.setup()
left = [b["type"] for b in fake3.hq_children if b["id"] not in fake3.dbs]
ok(left == ["callout", "child_database", "column_list", "paragraph"] and len(fake3.deleted) == 12,
   f"old stacked headings + views moved to Trash, dashboard + shortcuts in their place ({left})")
ok(all(b["id"] in [x["id"] for x in fake3.hq_children] for b in real), "your 9 databases are never touched")
ok(fake3.count("update_database") - before_db == 9, "all 9 database icons are set again (emoji ones become Notion icons)")

print("\n5. Filling it in")
r = ws.sync()
pages = [b for c, b in fake.calls if c == "create_page"]
by_ds = lambda k: [b for b in pages if b["parent"]["data_source_id"] == ws.ds(k)]  # noqa: E731
ok(len(by_ds("courses")) == 6 and len(by_ds("topics")) == 68, "6 courses and 68 topics")
dd05 = next(b for b in by_ds("topics") if b["properties"]["Sitruuna ID"]["rich_text"][0]["text"]["content"] == "DD05")
ok(dd05["properties"]["Status"]["status"]["name"] == "Halfway"
   and dd05["properties"]["Parts done"]["rich_text"][0]["text"]["content"] == "MUX"
   and "DEMUX" in dd05["properties"]["Parts left"]["rich_text"][0]["text"]["content"],
   "DD MSI: Halfway, MUX done, DEMUX etc. left")
ok(dd05["properties"]["Course"]["relation"][0]["id"] == ws.page_of("course", "DD"), "topics link to their course page")
exams = by_ds("exams")
m3q = next(b for b in exams if b["properties"]["Title"]["title"][0]["text"]["content"] == "M3 Quiz 2")
ok(m3q["properties"]["When"]["date"]["start"].startswith("2026-11-13T19:00") and m3q["properties"]["Coming up"]["checkbox"],
   "M3 Quiz 2 on 13 Nov 19:00, marked 'coming up'")
ok(len(m3q["properties"]["Syllabus"]["relation"]) > 0, "quiz links to the topics it covers")
verilog = next(b for b in exams if "Verilog" in b["properties"]["Title"]["title"][0]["text"]["content"])
ok(verilog["properties"]["Status"]["status"]["name"] == "Waiting for ✓", "tentative Verilog eval shows 'Waiting for ✓'")
compre = next(b for b in exams if b["properties"]["Title"]["title"][0]["text"]["content"] == "ED Compre")
ok(compre["properties"]["Session"]["select"]["name"] == "FN · morning" and compre["properties"]["Weight"]["number"] == 0.4,
   "compre: morning session, 40%")
sess = by_ds("sessions")
ok(sess and all(b["properties"]["Today"]["checkbox"] for b in sess if b["properties"]["When"]["date"]["start"].startswith("2026-11-04")),
   f"today's {len(sess)} study sessions, ticked 'Today'")
logs = by_ds("log")
ok(len(logs) == 1 and logs[0]["children"][0]["heading_2"]["rich_text"][0]["text"]["content"] == "What I did",
   "today's log page made with 'What I did' / 'Notes'")
ok(fake.count("update_block") == 1 and "Next exam: M3 Quiz 2 in 9 days" in str(list(fake.blocks.values())[0]),
   "HQ banner says today's facts")
ok(not r.warnings, f"no problems ({r.warnings})")

em = next(b for b in by_ds("courses") if b["properties"]["Name"]["title"][0]["text"]["content"] == "M3")
ok(em["properties"]["Progress"]["rich_text"][0]["text"]["content"].startswith("▓"), "course cards show a text progress bar (▓▓▓░░░ 35%)")
ok("topics" in m3q["properties"]["Readiness"]["rich_text"][0]["text"]["content"], "exams show readiness (▓░░░░ 1 of 4 topics)")
banner = str(list(fake.blocks.values())[0])
ok("Syllabus learned" in banner and "Study this week" in banner and "Habits this week" in banner and "▓" in banner,
   "HQ banner: today + syllabus bars per course + study this week + habits")

print("\n6. Only changes are sent")
before = len(fake.calls)
r = ws.sync()
ok(len(fake.calls) == before and r.unchanged > 70, "second sync sends nothing")
p.set_topic_status("EM08", "d")
before = len(fake.calls)
r = ws.sync()
upd = [b for c, b in fake.calls[before:] if c == "update_page"]
topic_upd = [u for u in upd if "Topic" in u["properties"]]
course_upd = [u for u in upd if "Code" in u["properties"]]
ok(len(topic_upd) == 1 and topic_upd[0]["properties"]["Status"]["status"]["name"] == "Done" and len(course_upd) == 1,
   "marking one topic done -> that topic + its course's progress bar updated, nothing else")

print("\n7. Re-plans and deletions")
blocks_before = {b.id for b in p.blocks_on(date(2026, 11, 4))}
p.save_day(date(2026, 11, 4), from_min=14 * 60)          # a re-plan replaces the later blocks
gone = blocks_before - {b.id for b in p.blocks_on(date(2026, 11, 4))}
r = ws.sync()
ok(r.archived == len(gone) and r.archived > 0, f"replaced study blocks go to Notion's Trash ({r.archived})")
pid = ws.page_of("topic", "EM09")
fake.pages.pop(pid)                                         # you deleted it by hand in Notion
p.set_topic_status("EM09", "h")
r = ws.sync()
ok(r.created == 1 and ws.page_of("topic", "EM09") != pid, "a page you deleted in Notion is made again when it changes")

print("\n7a. Your HQ now (with the two paywalled charts) -> charts removed")
fake6 = FakeNotion()
s6, store6, a6, p6, ws6 = make(fake6)
ws6.setup()
cal6 = store6.get_kv("notion:hq:callout")
real6 = [b for b in fake6.hq_children if b["id"] in fake6.dbs]
kids6 = list(real6) + [{"id": cal6, "type": "callout"}]
for i, name in enumerate(["Today's plan", "Coming up · 14 days", "Open tasks", "Syllabus done", "Study this week", "Behind"]):
    kids6 += [{"id": f"h{i}", "type": "heading_2", "heading_2": {"rich_text": [{"plain_text": name}]}},
              {"id": f"v{i}", "type": "child_database"}]
kids6 += [{"id": "cl", "type": "column_list"}, {"id": "ft", "type": "paragraph", "paragraph": {"rich_text": []}}]
fake6.hq_children = kids6
store6.set_kv("notion:charts_removed", "")
ws6.setup()
ok(set(fake6.deleted) == {"h3", "v3", "h4", "v4"}, f"only the two chart sections go to Trash ({sorted(fake6.deleted)})")
ok(all(b["id"] in [x["id"] for x in fake6.hq_children] for b in real6), "databases untouched")

print("\n7b. Duplicates and taking turns")
dup = fake.create_page({"parent": {"type": "data_source_id", "data_source_id": ws.ds("topics")},
                        "properties": {"Topic": {"title": []}, "Sitruuna ID": {"rich_text": [{"type": "text", "text": {"content": "DD05"}}]}}})
keep = ws.page_of("topic", "DD05")
ws.setup()
ok(fake.pages[dup["id"]].get("in_trash") and not fake.pages[keep].get("in_trash"),
   "an extra copy of a topic goes to Trash; the one Sitruuna knows stays")
ws.acquire("setup")
r = ws.sync(owner="bot")
ok(any("setup run" in w for w in r.warnings), "the bot skips its update while a setup run is filling Notion")
ws.release("setup")
r = ws.sync(owner="bot")
ok(not r.warnings, "and goes ahead once setup is done")

print("\n8. Telegram")
from pa.telegram_bot import Bot  # noqa: E402
s.telegram_token, s.telegram_chat_id = "1:T", "42"
bot = Bot(a)
bot.notion = ws
sent = []


async def _send(text, buttons=None, rows=None):
    sent.append(text)


bot.send = _send
asyncio.run(bot.notion_info())
ok("Open Sitruuna HQ in Notion" in sent[-1] and fake.hq.replace("-", "") in sent[-1], "/notion sends the HQ link")
asyncio.run(bot.sync_now())
ok(sent[-1].startswith("✅ Notion updated"), "/sync updates now and reports")
bot.notion = None
asyncio.run(bot.notion_info())
ok("--setup-notion" in sent[-1], "without Notion set up, it says what to run")

print("\n" + ("ALL NOTION TESTS PASSED" if not FAILS else f"{len(FAILS)} FAILED"))
sys.exit(1 if FAILS else 0)
