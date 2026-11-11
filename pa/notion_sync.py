"""Builds the Notion workspace and keeps it up to date (Phase 1: Sitruuna -> Notion).

    ws = NotionWorkspace(settings, store, planner, client)
    ws.setup()   # once (safe to run again): databases, columns, links, views, the HQ page
    ws.sync()    # every 2 minutes: courses, topics, exams, study sessions, today's log, HQ banner

Only things that changed are sent (each item's last-sent version is remembered in `notion_map`).
Things removed in Sitruuna are moved to Notion's Trash, never deleted.
Problems are collected and reported; one bad item never stops the rest.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from . import notion_schema as S
from .gcal import load_weekly_classes
from .notion_client import NotionClient, NotionError, page_id_from
from .planner import EXAM_TYPES, Planner, fmt

log = logging.getLogger(__name__)

KIND_LABEL = {"study": "Study", "revise": "Revise", "pyq": "Past paper", "prep": "Prep", "workout": "Workout",
              "reading": "Reading", "dsa": "DSA", "project": "Project"}
BLOCK_STATUS = {"planned": "Planned", "started": "Started", "partial": "Partly done", "done": "Done", "skipped": "Skipped"}
TOPIC_STATUS = {"n": "Not yet", "h": "Halfway", "d": "Done"}
EXAM_TYPE = {"quiz": "Quiz", "midsem": "Midsem", "compre": "Compre", "exam": "Exam", "lab": "Lab",
             "deadline": "Deadline", "class": "Extra class", "event": "Event"}


@dataclass
class Report:
    created: int = 0
    updated: int = 0
    archived: int = 0
    unchanged: int = 0
    warnings: list = field(default_factory=list)

    def text(self) -> str:
        t = f"{self.created} added, {self.updated} updated, {self.archived} moved to Trash, {self.unchanged} unchanged"
        if self.warnings:
            t += f"; {len(self.warnings)} problem(s): " + "; ".join(self.warnings[:5])
        return t


# ---------- property value helpers ----------
def _rt(s):
    s = "" if s is None else str(s)
    return [{"type": "text", "text": {"content": s[:1990]}}] if s else []


def p_title(s): return {"title": _rt(s)}
def p_text(s): return {"rich_text": _rt(s)}
def p_check(b): return {"checkbox": bool(b)}
def p_num(n): return {"number": n}
def p_select(name): return {"select": {"name": name} if name else None}
def p_status(name): return {"status": {"name": name}}
def p_rel(ids): return {"relation": [{"id": i} for i in ids if i]}


def bar(frac: float, width: int = 10) -> str:
    """0.35 -> ▓▓▓▓░░░░░░ (text bars work on Notion's free plan; charts don't)."""
    n = max(0, min(width, round((frac or 0) * width)))
    return "▓" * n + "░" * (width - n)


def p_date(start, end=None):
    if not start:
        return {"date": None}
    d = {"start": start}
    if end:
        d["end"] = end
    return {"date": d}


class NotionWorkspace:
    def __init__(self, settings, store, planner: Planner, client: NotionClient | None = None):
        self.s = settings
        self.store = store
        self.p = planner
        self.c = client or NotionClient(settings.notion_token)
        self.report = Report()

    # ==================== helpers ====================
    @property
    def hq(self) -> str | None:
        return self.store.get_kv("notion:hq") or page_id_from(self.s.notion_hq_page)

    def db(self, key: str) -> dict | None:
        raw = self.store.get_kv(f"notion:db:{key}")
        return json.loads(raw) if raw else None

    def ds(self, key: str) -> str:
        return self.db(key)["data_source_id"]

    def _warn(self, what: str, e: Exception):
        msg = f"{what}: {getattr(e, 'message', None) or e}"[:200]
        log.warning("Notion: %s", msg)
        self.report.warnings.append(msg)

    def _bad_icons(self) -> set:
        return set(filter(None, (self.store.get_kv("notion:bad_icons") or "").split(",")))

    def _icon(self, spec):
        """Notion's own coloured icon (first name Notion hasn't refused before), else the emoji."""
        names, color, emoji = spec
        bad = self._bad_icons()
        name = next((n for n in names if n not in bad), None)
        if not name:
            return {"type": "emoji", "emoji": emoji}
        return {"type": "icon", "icon": {"name": name, "color": color}}

    def _with_icon_fallback(self, call, body: dict, spec):
        """Try each icon name in turn; a name Notion refuses is remembered and never tried again."""
        while True:
            try:
                return call(body)
            except NotionError as e:
                icon = body.get("icon") or {}
                if icon.get("type") != "icon" or e.status != 400:
                    raise
                name = icon["icon"]["name"]
                log.info("Notion has no icon called %r; trying the next one", name)
                self.store.set_kv("notion:bad_icons", ",".join(sorted(self._bad_icons() | {name})))
                body["icon"] = self._icon(spec)

    def _repair_icons(self):
        """Give every database and course page Notion's own icon where one exists (fixes emoji left by older setups)."""
        for key, dbspec in S.DATABASES.items():
            db, spec = self.db(key), dbspec["icon"]
            want = self._icon(spec)
            if not db or self.store.get_kv(f"notion:icon:{key}") == json.dumps(want):
                continue
            try:
                self._with_icon_fallback(lambda b: self.c.update_database(db["database_id"], **b), {"icon": want}, spec)
                self.store.set_kv(f"notion:icon:{key}", json.dumps(self._icon(spec)))
            except NotionError as e:
                self._warn(f"icon {dbspec['title']}", e)
        for short in self.s.courses.entries:
            page = self.page_of("course", short)
            spec = (["book"], S.COURSE_COLOR.get(short, "gray"), "📘")
            want = self._icon(spec)
            if not page or self.store.get_kv(f"notion:icon:course:{short}") == json.dumps(want):
                continue
            try:
                self._with_icon_fallback(lambda b: self.c.update_page(page, **b), {"icon": want}, spec)
                self.store.set_kv(f"notion:icon:course:{short}", json.dumps(self._icon(spec)))
            except NotionError as e:
                self._warn(f"icon {short}", e)

    # ==================== setup ====================
    # ---------- one at a time (the bot and a setup run must never fill Notion together) ----------
    def acquire(self, owner: str, minutes: int = 20) -> bool:
        raw = self.store.get_kv("notion:busy") or ""
        who, _, until = raw.partition("|")
        now = self.s.now()
        if raw and who != owner and until and datetime.fromisoformat(until) > now:
            return False
        self.store.set_kv("notion:busy", f"{owner}|{(now + timedelta(minutes=minutes)).isoformat()}")
        return True

    def release(self, owner: str):
        if (self.store.get_kv("notion:busy") or "").startswith(owner + "|"):
            self.store.set_kv("notion:busy", "")

    def setup(self, owner: str = "setup") -> Report:
        """Build or repair the workspace. Safe to run again."""
        import time as _time
        for _ in range(36):                                  # wait up to 3 min for a running sync to finish
            if self.acquire(owner):
                break
            _time.sleep(5)
        else:
            raise NotionError(0, "busy", "Sitruuna is updating Notion right now; try again in a few minutes")
        try:
            return self._setup()
        finally:
            self.release(owner)

    def _setup(self) -> Report:
        self.report = Report()
        hq = page_id_from(self.s.notion_hq_page)
        if not hq:
            raise NotionError(0, "config", "NOTION_HQ_PAGE in .env isn't a Notion page link")
        self.c.get_page(hq)                                # fails clearly if the page isn't shared with Sitruuna
        self.store.set_kv("notion:hq", hq)
        self.store.set_kv("notion:native_icons", "")      # old all-or-nothing switch: icons are now tried one by one
        try:
            self._with_icon_fallback(lambda b: self.c.update_page(hq, **b),
                                     {"icon": self._icon(S.HQ_ICON),
                                      "cover": {"type": "external", "external": {"url": S.COVER["HQ"]}}}, S.HQ_ICON)
        except NotionError as e:
            self._warn("HQ icon/cover", e)

        for key, spec in S.DATABASES.items():
            self._ensure_db(key, spec)
        for src, prop, dst, back in S.RELATIONS:
            self._ensure_relation(src, prop, dst, back)
        for key, prop, expr in S.FORMULAS:
            self._ensure_formula(key, prop, expr)
        for key, prop, rel, col, fn in S.ROLLUPS:
            self._ensure_rollup(key, prop, rel, col, fn)
        for key, name, vtype, conf, flt, sorts in S.views():
            self._ensure_view(key, name, vtype, conf, flt, sorts)
        self._ensure_hq_page()
        self._remove_chart_sections()
        self._repair_icons()
        self._dedupe()
        self.store.set_kv("notion:setup_at", self.s.now().isoformat(timespec="seconds"))
        return self.report

    def _props_of(self, key: str) -> dict:
        return self.c.get_data_source(self.ds(key)).get("properties", {})

    def _ensure_db(self, key: str, spec: dict):
        known = self.db(key)
        if known:
            try:
                have = self._props_of(key)
                missing = {n: cfg for n, cfg in spec["props"].items() if n not in have}
                if missing:
                    self.c.update_data_source(known["data_source_id"], missing)
                    self.report.updated += 1
                return
            except NotionError as e:
                if e.status != 404:
                    raise
                log.info("Database %s is gone in Notion; making it again", key)
        body = {"parent": {"type": "page_id", "page_id": self.hq},
                "title": _rt(spec["title"]),
                "icon": self._icon(spec["icon"]),
                "initial_data_source": {"properties": spec["props"]}}
        res = self._with_icon_fallback(self.c.create_database, body, spec["icon"])
        self.store.set_kv(f"notion:icon:{key}", json.dumps(body["icon"]))
        dbid = res["id"]
        sources = res.get("data_sources") or self.c.get_database(dbid).get("data_sources") or []
        if not sources:
            raise NotionError(0, "setup", f"Notion made {spec['title']} but returned no data source")
        self.store.set_kv(f"notion:db:{key}", json.dumps({"database_id": dbid, "data_source_id": sources[0]["id"]}))
        self.report.created += 1

    def _ensure_relation(self, src: str, prop: str, dst: str, back: str):
        try:
            if prop in self._props_of(src):
                return
            self.c.update_data_source(self.ds(src), {prop: {
                "type": "relation",
                "relation": {"data_source_id": self.ds(dst), "type": "dual_property",
                             "dual_property": {"synced_property_name": back}}}})
            self.report.updated += 1
        except NotionError as e:
            self._warn(f"link {src}.{prop} → {dst}", e)

    def _ensure_formula(self, key: str, prop: str, expr: str):
        try:
            if prop in self._props_of(key):
                return
            self.c.update_data_source(self.ds(key), {prop: {"type": "formula", "formula": {"expression": expr}}})
            self.report.updated += 1
        except NotionError as e:
            self._warn(f"formula {prop}", e)

    def _ensure_rollup(self, key, prop, rel, col, fn):
        try:
            if prop in self._props_of(key):
                return
            self.c.update_data_source(self.ds(key), {prop: {"type": "rollup", "rollup": {
                "function": fn, "relation_property_name": rel, "rollup_property_name": col}}})
            self.report.updated += 1
        except NotionError as e:
            self._warn(f"rollup {prop}", e)

    def _dedupe(self):
        """Moves extra copies of the same item (made when two fills overlapped) to Trash. Keeps the one Sitruuna knows."""
        for kind, key in (("course", "courses"), ("topic", "topics"), ("exam", "exams"), ("session", "sessions"), ("log", "log")):
            mapped = {r["local_id"]: r["page_id"] for r in
                      self.store.conn.execute("SELECT local_id, page_id FROM notion_map WHERE kind=?", (kind,)).fetchall()}
            cursor = None
            try:
                while True:
                    res = self.c.query(self.ds(key), cursor=cursor)
                    for pg in res.get("results", []):
                        if pg.get("in_trash") or pg.get("archived"):
                            continue
                        rt = ((pg.get("properties") or {}).get("Sitruuna ID") or {}).get("rich_text") or []
                        sid = "".join(t.get("plain_text") or t.get("text", {}).get("content", "") for t in rt)
                        keep = mapped.get(sid)
                        if sid and keep and keep.replace("-", "") != pg["id"].replace("-", ""):
                            self.c.archive_page(pg["id"])
                            self.report.archived += 1
                    if not res.get("has_more"):
                        break
                    cursor = res.get("next_cursor")
            except NotionError as e:
                self._warn(f"tidy duplicates in {key}", e)

    def _pid_fn(self, key: str):
        props = self._props_of(key)

        def pid(name):
            if name not in props:
                raise NotionError(0, "setup", f"column {name} missing in {key}")
            return props[name]["id"]
        return pid

    def _view_body(self, key, name, vtype, conf, flt, sorts) -> dict:
        body = {"data_source_id": self.ds(key), "name": name, "type": vtype}
        if conf:
            body["configuration"] = conf(self._pid_fn(key))
        if flt:
            body["filter"] = flt
        if sorts:
            body["sorts"] = sorts
        return body

    def _ensure_view(self, key, name, vtype, conf, flt, sorts):
        kvkey = f"notion:view:{key}:{name}"
        if self.store.get_kv(kvkey):
            return
        try:
            body = self._view_body(key, name, vtype, conf, flt, sorts)
            body["database_id"] = self.db(key)["database_id"]
            res = self.c.create_view(body)
            self.store.set_kv(kvkey, res.get("id", "?"))
            self.report.created += 1
        except NotionError as e:
            self._warn(f"view {S.DATABASES[key]['title']} · {name}", e)

    def _ensure_hq_page(self):
        """HQ = today's banner, then a dashboard (sections side by side), then shortcut cards.
        If Notion won't make a dashboard, the sections are stacked under headings instead."""
        hq = self.hq
        if not self.store.get_kv("notion:hq_built"):
            res = self.c.append_blocks(hq, [{"object": "block", "type": "callout", "callout": {
                "rich_text": _rt("Today: Sitruuna fills this in."), "color": "yellow_background",
                "icon": {"type": "emoji", "emoji": "☀️"}}}])
            ids = [b["id"] for b in res.get("results", [])]
            if ids:
                self.store.set_kv("notion:hq:callout", ids[0])
            self.store.set_kv("notion:hq_built", "1")
            self.store.set_kv("notion:hq:fresh", "1")          # nothing stacked to tidy away
        if self.s.get("notion", "hq_layout", default="stacked") == "dashboard" and self._ensure_dashboard(hq):
            self._remove_stacked(hq)
            self._ensure_shortcuts(hq)
            return
        self._drop_dashboard(hq)
        self._stacked_layout(hq)
        self._ensure_shortcuts(hq)

    def _remove_chart_sections(self):
        """Charts need a paid Notion plan: their HQ sections go to Trash (once). Progress shows as text bars instead."""
        if self.store.get_kv("notion:charts_removed"):
            return
        keep = {self.db(k)["database_id"].replace("-", "") for k in S.DATABASES if self.db(k)}
        kids = self.c.list_children(self.hq).get("results", [])
        for i, b in enumerate(kids):
            if b.get("type") != "heading_2":
                continue
            text = "".join(x.get("plain_text") or x.get("text", {}).get("content", "")
                           for x in (b.get("heading_2") or {}).get("rich_text", []))
            if text not in S.REMOVED_SECTIONS:
                continue
            doomed = [b["id"]]
            nxt = kids[i + 1] if i + 1 < len(kids) else None
            if nxt and nxt.get("type") == "child_database" and nxt["id"].replace("-", "") not in keep:
                doomed.append(nxt["id"])
            for bid in doomed:
                try:
                    self.c.delete_block(bid)
                    self.report.archived += 1
                except NotionError as e:
                    self._warn("remove chart section", e)
        heads = json.loads(self.store.get_kv("notion:hq:headings") or "{}")
        for name in S.REMOVED_SECTIONS:
            heads.pop(name, None)
            self.store.set_kv(f"notion:hq:sec:{name}", "")
            self.store.set_kv(f"notion:hq:w:{name}", "")
        self.store.set_kv("notion:hq:headings", json.dumps(heads) if heads else "")
        self.store.set_kv("notion:charts_removed", "1")

    def _drop_dashboard(self, hq):
        """Back to the linear HQ: the dashboard block goes to Trash (only when no stacked views are on the page)."""
        if not self.store.get_kv("notion:hq:dashboard"):
            return
        keep = {self.db(k)["database_id"].replace("-", "") for k in S.DATABASES if self.db(k)}
        kids = self.c.list_children(hq).get("results", [])
        if self.store.get_kv("notion:hq:stacked_removed") or not any(b.get("type") == "heading_2" for b in kids):
            for b in kids:
                if b.get("type") == "child_database" and b["id"].replace("-", "") not in keep:
                    try:
                        self.c.delete_block(b["id"])
                        self.report.archived += 1
                    except NotionError as e:
                        self._warn("remove dashboard", e)
        self.store.set_kv("notion:hq:dashboard", "")
        for h, *_ in S.hq_sections():
            self.store.set_kv(f"notion:hq:w:{h}", "")

    # ---------- dashboard ----------
    def _ensure_dashboard(self, hq) -> bool:
        dash = self.store.get_kv("notion:hq:dashboard")
        if not dash:
            body = {"data_source_id": self.ds("sessions"), "name": "Sitruuna HQ", "type": "dashboard",
                    "create_database": {"parent": {"type": "page_id", "page_id": hq}, "show_data_source_title": False}}
            callout = self.store.get_kv("notion:hq:callout")
            if callout:
                body["create_database"]["position"] = {"type": "after_block", "block_id": callout}
            try:
                dash = self.c.create_view(body)["id"]
            except NotionError as e:
                self._warn("HQ dashboard (sections are stacked instead; drag a view by its ⋮⋮ handle to the "
                           "right edge of another to put them side by side)", e)
                return False
            self.store.set_kv("notion:hq:dashboard", dash)
            self.report.created += 1
        sections = S.hq_sections()
        for r, row in enumerate(S.HQ_ROWS):
            row_has = any(self.store.get_kv(f"notion:hq:w:{sections[i][0]}") for i in row)
            for i in row:
                heading, key, vtype, conf, flt, sorts = sections[i]
                kvkey = f"notion:hq:w:{heading}"
                if self.store.get_kv(kvkey):
                    continue
                try:
                    body = self._view_body(key, heading, vtype, conf, flt, sorts)
                    body["view_id"] = dash
                    body["placement"] = ({"type": "existing_row", "row_index": r} if row_has
                                         else {"type": "new_row", "row_index": r})
                    res = self.c.create_view(body)
                    self.store.set_kv(kvkey, res.get("id", "?"))
                    row_has = True
                    self.report.created += 1
                except NotionError as e:
                    self._warn(f"HQ widget {heading}", e)
        return True

    def _remove_stacked(self, hq):
        """Moves the old stacked sections (from the first setup) to Trash. Your databases and the banner stay."""
        if self.store.get_kv("notion:hq:stacked_removed"):
            return
        if not self.store.get_kv("notion:hq:fresh"):
            names = {h for h, *_ in S.hq_sections()} | set(S.REMOVED_SECTIONS)
            keep = {self.db(k)["database_id"].replace("-", "") for k in S.DATABASES if self.db(k)}
            keep.add((self.store.get_kv("notion:hq:callout") or "").replace("-", ""))
            keep.add((self.store.get_kv("notion:hq:dashboard") or "").replace("-", ""))
            kids = self.c.list_children(hq).get("results", [])
            doomed = []
            for i, b in enumerate(kids):
                t = b.get("type")
                text = "".join(x.get("plain_text") or x.get("text", {}).get("content", "")
                               for x in (b.get(t) or {}).get("rich_text", [])) if t in ("heading_2", "paragraph") else ""
                if t == "heading_2" and text in names:
                    doomed.append(b["id"])
                    nxt = kids[i + 1] if i + 1 < len(kids) else None
                    if nxt and nxt.get("type") not in ("heading_2", "paragraph", "callout", "column_list") \
                            and nxt["id"].replace("-", "") not in keep:
                        doomed.append(nxt["id"])
                elif t == "paragraph" and text.startswith("Built by Sitruuna."):
                    doomed.append(b["id"])
            for bid in doomed:
                if bid.replace("-", "") in keep:
                    continue
                try:
                    self.c.delete_block(bid)
                    self.report.archived += 1
                except NotionError as e:
                    self._warn("tidy old HQ section", e)
        for h, *_ in S.hq_sections():
            self.store.set_kv(f"notion:hq:sec:{h}", "")
        self.store.set_kv("notion:hq:headings", "")
        self.store.set_kv("notion:hq:stacked_removed", "1")

    def _ensure_shortcuts(self, hq):
        if self.store.get_kv("notion:hq:shortcuts"):
            return
        cols = []
        for key, label in S.SHORTCUTS:
            db = self.db(key)
            if not db:
                continue
            cols.append({"object": "block", "type": "column", "column": {"children": [
                {"object": "block", "type": "link_to_page", "link_to_page": {"type": "database_id", "database_id": db["database_id"]}}]}})
        if len(cols) < 2:
            return
        try:
            self.c.append_blocks(hq, [
                {"object": "block", "type": "column_list", "column_list": {"children": cols}},
                {"object": "block", "type": "paragraph", "paragraph": {"rich_text": _rt(
                    "Built by Sitruuna · updates every 2 minutes · your own notes on any page are never touched.")}}])
            self.store.set_kv("notion:hq:shortcuts", "1")
            self.report.created += 1
        except NotionError as e:
            self._warn("HQ shortcuts", e)

    # ---------- fallback: stacked sections ----------
    def _stacked_layout(self, hq):
        sections = S.hq_sections()
        heads = json.loads(self.store.get_kv("notion:hq:headings") or "{}")
        if not heads and not self.store.get_kv("notion:hq:fresh") and not self.store.get_kv("notion:hq:stacked_removed"):
            heads = self._find_hq_headings(hq)
        if not heads:
            self._lift_shortcuts(hq)                       # sections go under the banner, shortcuts back to the bottom
            res = self.c.append_blocks(hq, [{"object": "block", "type": "heading_2", "heading_2": {"rich_text": _rt(h)}}
                                            for h, *_ in sections])
            ids = [b["id"] for b in res.get("results", [])]
            heads = {h: ids[i] for i, (h, *_) in enumerate(sections) if i < len(ids)}
            self.store.set_kv("notion:hq:headings", json.dumps(heads))
            self.store.set_kv("notion:hq:stacked_removed", "")
        self._add_hq_sections(hq, sections, heads)

    def _lift_shortcuts(self, hq):
        if not self.store.get_kv("notion:hq:shortcuts"):
            return
        for b in self.c.list_children(hq).get("results", []):
            t = b.get("type")
            text = "".join(x.get("plain_text") or x.get("text", {}).get("content", "")
                           for x in (b.get(t) or {}).get("rich_text", [])) if t == "paragraph" else ""
            if t == "column_list" or (t == "paragraph" and text.startswith("Built by Sitruuna")):
                try:
                    self.c.delete_block(b["id"])
                except NotionError as e:
                    self._warn("move shortcuts", e)
        self.store.set_kv("notion:hq:shortcuts", "")

    def _add_hq_sections(self, hq, sections, heads):
        for heading, key, vtype, conf, flt, sorts in sections:
            if self.store.get_kv(f"notion:hq:sec:{heading}"):
                continue
            try:
                body = self._view_body(key, heading, vtype, conf, flt, sorts)
                body["create_database"] = {"parent": {"type": "page_id", "page_id": hq},
                                           "show_data_source_title": False}
                if heads.get(heading):
                    body["create_database"]["position"] = {"type": "after_block", "block_id": heads[heading]}
                self.c.create_view(body)
                self.store.set_kv(f"notion:hq:sec:{heading}", "1")
                self.report.created += 1
            except NotionError as e:
                self._warn(f"HQ section {heading}", e)

    def _find_hq_headings(self, hq) -> dict:
        """For an HQ page built by an older setup: find each heading, and whether a view already sits under it."""
        kids = self.c.list_children(hq).get("results", [])
        heads = {}
        for i, b in enumerate(kids):
            if b.get("type") != "heading_2":
                continue
            text = "".join(t.get("plain_text") or t.get("text", {}).get("content", "")
                           for t in b["heading_2"].get("rich_text", []))
            heads[text] = b["id"]
            nxt = kids[i + 1]["type"] if i + 1 < len(kids) else None
            if nxt and nxt not in ("heading_2", "paragraph", "callout"):
                self.store.set_kv(f"notion:hq:sec:{text}", "1")      # its view is already there
        self.store.set_kv("notion:hq:headings", json.dumps(heads))
        return heads

    # ==================== sync (Sitruuna -> Notion) ====================
    def ready(self) -> bool:
        return all(self.db(k) for k in ("courses", "topics", "exams", "sessions", "log"))

    def sync(self, owner: str = "bot") -> Report:
        self.report = Report()
        if not self.acquire(owner, minutes=10):
            self.report.warnings.append("skipped: a setup run is filling Notion right now")
            return self.report
        try:
            return self._sync()
        finally:
            self.release(owner)

    def _sync(self) -> Report:
        if not self.ready():
            self.report.warnings.append("Notion isn't set up yet: run python main.py --setup-notion")
            return self.report
        self._sync_kind("course", "courses", self._courses())
        self._sync_kind("topic", "topics", self._topics())
        self._sync_kind("exam", "exams", self._exams())
        self._sync_kind("session", "sessions", self._sessions(), archive_missing=True)
        self._sync_kind("log", "log", self._today_log(), archive_missing=False)
        self._sync_banner()
        self.store.set_kv("notion:last_sync", self.s.now().isoformat(timespec="seconds"))
        return self.report

    def page_of(self, kind: str, local_id) -> str | None:
        r = self.store.conn.execute("SELECT page_id FROM notion_map WHERE kind=? AND local_id=?",
                                    (kind, str(local_id))).fetchone()
        return r["page_id"] if r else None

    def _sync_kind(self, kind: str, dbkey: str, items: dict, archive_missing: bool = True):
        """items: local_id -> {"props": {...}, "icon"?, "cover"?, "children"?}"""
        c = self.store.conn
        mapped = {r["local_id"]: (r["page_id"], r["hash"]) for r in
                  c.execute("SELECT * FROM notion_map WHERE kind=?", (kind,)).fetchall()}
        now = self.s.now().isoformat(timespec="seconds")
        for lid, item in items.items():
            lid = str(lid)
            h = hashlib.sha1(json.dumps(item["props"], sort_keys=True, default=str).encode()).hexdigest()
            try:
                if lid in mapped:
                    page_id, old = mapped[lid]
                    if old == h:
                        self.report.unchanged += 1
                        continue
                    try:
                        self.c.update_page(page_id, properties=item["props"])
                        self.report.updated += 1
                    except NotionError as e:
                        gone = e.status == 404 or (e.status == 400 and "archiv" in (e.message or "").lower())
                        if not gone:
                            raise
                        page_id = self._create(dbkey, item)        # you deleted it in Notion: make it again
                        self.report.created += 1
                else:
                    page_id = self._create(dbkey, item)
                    self.report.created += 1
                c.execute("INSERT INTO notion_map(kind, local_id, page_id, hash, synced_at) VALUES(?,?,?,?,?) "
                          "ON CONFLICT(kind, local_id) DO UPDATE SET page_id=excluded.page_id, hash=excluded.hash, "
                          "synced_at=excluded.synced_at", (kind, lid, page_id, h, now))
                c.commit()
            except NotionError as e:
                self._warn(f"{kind} {lid}", e)
        if archive_missing:
            for lid, (page_id, _) in mapped.items():
                if lid not in {str(k) for k in items}:
                    try:
                        self.c.archive_page(page_id)
                        self.report.archived += 1
                    except NotionError as e:
                        if e.status != 404:
                            self._warn(f"trash {kind} {lid}", e)
                            continue
                    c.execute("DELETE FROM notion_map WHERE kind=? AND local_id=?", (kind, lid))
                    c.commit()

    def _create(self, dbkey: str, item: dict) -> str:
        body = {"parent": {"type": "data_source_id", "data_source_id": self.ds(dbkey)}, "properties": item["props"]}
        if item.get("cover"):
            body["cover"] = {"type": "external", "external": {"url": item["cover"]}}
        if item.get("children"):
            body["children"] = item["children"]
        if item.get("icon"):
            body["icon"] = self._icon(item["icon"])
            return self._with_icon_fallback(self.c.create_page, body, item["icon"])["id"]
        return self.c.create_page(body)["id"]

    # ---------- what each kind looks like in Notion ----------
    def _courses(self) -> dict:
        attend = {c["course"] for c in load_weekly_classes() if c.get("must_attend")}
        root = str(self.s.get("notion", "materials_root", default="") or "")
        out = {}
        for short, e in self.s.courses.entries.items():
            conf = self.store.get_kv(f"conf:{short}")
            out[short] = {
                "props": {"Name": p_title(short), "Full name": p_text(e.get("name", "")), "Code": p_text(e.get("code", "")),
                          "Takes attendance": p_check(short in attend), "Confidence": p_select(conf if conf else None),
                          "Materials": p_text(f"{root}\\{short}" if root else ""), "Sitruuna ID": p_text(short),
                          "Progress": p_text(self._course_bar(short))},
                "cover": S.COVER.get(short, S.COVER["_"]),
                "icon": (["book"], S.COURSE_COLOR.get(short, "gray"), "📘"),
            }
        return out

    def _topics(self) -> dict:
        out = {}
        for t in self.p.topics:
            st = self.p.status(t)
            done = self.p.parts_done(t.id) if st != "d" else list(t.parts)
            left = self.p.parts_left(t)
            last = self.p.last_studied(t.id)
            out[t.id] = {"props": {
                "Topic": p_title(t.title), "Status": p_status(TOPIC_STATUS[st]), "Done": p_check(st == "d"),
                "Parts done": p_text(", ".join(done)), "Parts left": p_text(", ".join(left)),
                "Lectures": p_text(f"L{t.lec_from}" if t.lec_from == t.lec_to else f"L{t.lec_from}–{t.lec_to}"),
                "Book sections": p_text(t.refs), "Practice": p_text(t.practice),
                "Midsem syllabus": p_check(t.pre_midsem), "Behind": p_check(self.p.backlog(t)),
                "Minutes studied": p_num(self.p.done_min(t.id)), "Estimated minutes": p_num(t.est_min),
                "Last studied": p_date(last.isoformat() if last else None), "Sitruuna ID": p_text(t.id),
                "Course": p_rel([self.page_of("course", t.course)]),
            }}
        return out

    def _when(self, d: str, start: str | None, end: str | None):
        if not start:
            return p_date(d)
        tz = self.s.tz
        day = date.fromisoformat(d)
        s_ = datetime.combine(day, time.fromisoformat(start), tzinfo=tz).isoformat()
        e_ = datetime.combine(day, time.fromisoformat(end), tzinfo=tz).isoformat() if end else None
        return p_date(s_, e_)

    def _exams(self) -> dict:
        today = self.s.today()
        since = (today - timedelta(days=30)).isoformat()
        rows = self.store.conn.execute(
            "SELECT * FROM events WHERE date>=? AND status IN ('active','pending','cancelled') ORDER BY date", (since,)).fetchall()
        exams = {e.id: e for e in self.p.exams(today - timedelta(days=30))}
        out = {}
        for r in rows:
            if r["action"] == "cancel":
                continue                       # cancellation notices (e.g. a class off) stay on Telegram / Calendar
            d = date.fromisoformat(r["date"])
            status = {"pending": "Waiting for ✓", "cancelled": "Cancelled"}.get(r["status"],
                                                                                 "Done" if d < today else "Upcoming")
            notes = (r["notes"] or "")
            sess = "FN · morning" if re.search(r"\bFN\b", notes) else "AN · afternoon" if re.search(r"\bAN\b", notes) else None
            m = re.search(r"(\d{1,2})\s*%", notes)
            syllabus = []
            e = exams.get(r["id"])
            if e and r["type"] in EXAM_TYPES:
                syllabus = [self.page_of("topic", t.id) for t in self.p.topics if self.p.covers(e, t)]
            from .validate import sender_name
            src = " · ".join(x for x in [sender_name(r["source_sender"] or ""), r["source_subject"] or ""] if x)
            out[r["id"]] = {"props": {
                "Title": p_title(r["title"]), "Type": p_select(EXAM_TYPE.get(r["type"], "Event")),
                "When": self._when(r["date"], r["start_time"], r["end_time"]), "Session": p_select(sess),
                "Venue": p_text(r["venue"] or ""), "Weight": p_num(int(m.group(1)) / 100 if m else None),
                "Status": p_status(status), "Coming up": p_check(0 <= (d - today).days <= 14 and status != "Cancelled"),
                "Readiness": p_text(self._ready_bar(e) if e and r["type"] in EXAM_TYPES else ""),
                "Source": p_text(src), "Sitruuna ID": p_text(str(r["id"])),
                "Course": p_rel([self.page_of("course", r["course"])] if r["course"] else []),
                "Syllabus": p_rel(syllabus),
            }}
        return out

    def _sessions(self) -> dict:
        today = self.s.today()
        back = int(self.s.get("notion", "sessions_days_back", default=7))
        ahead = int(self.s.get("notion", "sessions_days_ahead", default=7))
        monday = today - timedelta(days=today.weekday())
        out = {}
        for i in range(-back, ahead + 1):
            d = today + timedelta(days=i)
            for b in self.p.blocks_on(d):
                name = f"{b.course} · {b.title}" if b.course and not b.title.startswith(b.course) else b.title
                topic = b.topic_id if b.topic_id and not b.topic_id.startswith("ev:") else None
                out[b.id] = {"props": {
                    "Title": p_title(name), "Kind": p_select(KIND_LABEL.get(b.kind, "Study")),
                    "When": self._when(b.date, fmt(b.start), fmt(b.end)),
                    "Status": p_status(BLOCK_STATUS.get(b.status, "Planned")),
                    "What to do": p_text(b.detail), "Changed by you": p_check(b.locked),
                    "Today": p_check(d == today), "This week": p_check(monday <= d < monday + timedelta(days=7)),
                    "Minutes": p_num(b.end - b.start), "Sitruuna ID": p_text(str(b.id)),
                    "Course": p_rel([self.page_of("course", b.course)] if b.course else []),
                    "Topic": p_rel([self.page_of("topic", topic)] if topic else []),
                }}
        return out

    def _today_log(self) -> dict:
        d = self.s.today()
        sleep = {"bad": "Bad", "ok": "OK", "great": "Great"}.get(self.store.get_kv(f"sleep:{d.isoformat()}") or "")
        blocks = self.p.blocks_on(d)
        kept = [b for b in blocks if b.status in ("done", "partial")]
        past = [b for b in blocks if b.status in ("done", "partial", "skipped")]
        item = {"props": {
            "Day": p_title(d.strftime("%a %d %b").replace(" 0", " ")), "Date": p_date(d.isoformat()),
            "Sleep": p_select(sleep), "Plan kept": p_num(round(len(kept) / len(past), 2) if past else None),
            "Sitruuna ID": p_text(d.isoformat()),
        }, "children": [
            {"object": "block", "type": "heading_2", "heading_2": {"rich_text": _rt("What I did")}},
            {"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": []}},
            {"object": "block", "type": "heading_2", "heading_2": {"rich_text": _rt("Notes")}},
            {"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": []}},
        ]}
        return {d.isoformat(): item}

    # ---------- text bars ----------
    def course_share(self, course: str) -> float | None:
        ts = [t for t in self.p.topics if t.course == course]
        est = sum(t.est_min for t in ts)
        return sum(min(t.est_min, self.p.done_min(t.id)) for t in ts) / est if est else None

    def _course_bar(self, course: str) -> str:
        f = self.course_share(course)
        return "" if f is None else f"{bar(f)} {round(f * 100)}%"

    def _ready_bar(self, e) -> str:
        ts = [t for t in self.p.topics if self.p.covers(e, t)]
        if not ts:
            return ""
        done = sum(1 for t in ts if self.p.status(t) == "d")
        return f"{bar(done / len(ts), 5)} {done} of {len(ts)} topics"

    def progress_lines(self) -> list[tuple[str, str]]:
        """The banner's lines as (style, text): style "b" = bold heading, "" = normal."""
        d = self.s.today()
        out = [("b", "Syllabus learned")]
        shares = [(c, self.course_share(c)) for c in self.s.courses.entries]
        for c, f in sorted([x for x in shares if x[1] is not None], key=lambda x: -x[1]):
            out.append(("", f"{bar(f)}  {round(f * 100):>2}%  {c}"))
        monday = d - timedelta(days=d.weekday())
        days, habits = [], {"workout": 0, "reading": 0, "dsa": 0, "project": 0}
        for i in range(7):
            day = monday + timedelta(days=i)
            bl = self.p.blocks_on(day)
            plan = sum(b.end - b.start for b in bl if b.is_study and b.status != "skipped") / 60
            done = sum((b.end - b.start) * (1 if b.status == "done" else 0.5) for b in bl
                       if b.is_study and b.status in ("done", "partial")) / 60
            for b in bl:
                if b.kind in habits and b.status in ("done", "partial"):
                    habits[b.kind] += 1
            name = day.strftime("%a")
            if not bl:
                days.append(f"{name} –")
            elif day <= d:
                days.append(f"{name} {done:.1f}/{plan:.1f}h")
            else:
                days.append(f"{name} {plan:.1f}h planned")
        out += [("b", "Study this week (done / planned)"), ("", " · ".join(days)),
                ("b", "Habits this week"),
                ("", f"Workout {habits['workout']}/6 · Reading {habits['reading']} days · DSA {habits['dsa']} days"
                     f" · Project {habits['project']} blocks")]
        return out

    def banner_text(self) -> str:
        d = self.s.today()
        blocks = self.p.blocks_on(d)
        study = sum(b.end - b.start for b in blocks if b.is_study and b.status != "skipped")
        sleep = {"bad": "Bad", "ok": "OK", "great": "Great"}.get(self.store.get_kv(f"sleep:{d.isoformat()}") or "", "not answered")
        nxt = next((e for e in self.p.exams(d) if e.type in EXAM_TYPES and e.date >= d), None)
        exam = f"{nxt.title} in {(nxt.date - d).days} days" if nxt else "none coming up"
        return (f"{d.strftime('%A %d %B').replace(' 0', ' ')} · Slept: {sleep} · Study planned: "
                f"{study // 60} h {study % 60:02d} min · Next exam: {exam}")

    def _sync_banner(self):
        bid = self.store.get_kv("notion:hq:callout")
        if not bid:
            return
        lines = self.progress_lines()
        text = self.banner_text() + "\n" + "\n".join(t for _, t in lines)
        if self.store.get_kv("notion:hq:banner") == text:
            return
        rich = [{"type": "text", "text": {"content": self.banner_text()}, "annotations": {"bold": True}}]
        for style, t in lines:
            rich.append({"type": "text", "text": {"content": ("\n\n" if style == "b" else "\n") + t},
                         "annotations": {"bold": style == "b"}})
        try:
            self.c.update_block(bid, {"callout": {"rich_text": rich}})
            self.store.set_kv("notion:hq:banner", text)
            self.report.updated += 1
        except NotionError as e:
            self._warn("HQ banner", e)

    def hq_link(self) -> str:
        hq = self.hq
        return f"https://www.notion.so/{hq.replace('-', '')}" if hq else ""
