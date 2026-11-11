"""The timetable database (one SQLite file in data/)."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from .config import DATA_DIR

SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_emails (
    msg_id       TEXT PRIMARY KEY,
    processed_at TEXT,
    sender       TEXT,
    subject      TEXT,
    result       TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT NOT NULL,
    type          TEXT,            -- quiz, midsem, compre, exam, deadline, class, lab, event
    action        TEXT,            -- add, update, cancel
    course        TEXT,
    date          TEXT,            -- YYYY-MM-DD
    start_time    TEXT,            -- HH:MM or NULL (all day)
    end_time      TEXT,
    venue         TEXT,
    notes         TEXT,
    status        TEXT NOT NULL,   -- pending, active, ignored, undone, cancelled, superseded, removed, expired
    confidence    TEXT,            -- high, low
    low_reason    TEXT,
    source_msg_id TEXT,
    source_subject TEXT,
    source_sender TEXT,
    source_file   TEXT,
    source_quote  TEXT,
    replaces_id   INTEGER,
    section       TEXT,
    gcal_event_id TEXT,
    reminded_prep INTEGER DEFAULT 0,
    reminded_soon INTEGER DEFAULT 0,
    created_at    TEXT,
    updated_at    TEXT
);

CREATE TABLE IF NOT EXISTS plan_blocks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    date          TEXT NOT NULL,   -- YYYY-MM-DD
    start_min     INTEGER NOT NULL,-- minutes after midnight
    end_min       INTEGER NOT NULL,
    kind          TEXT NOT NULL,   -- study, revise, pyq, prep, workout, reading, dsa, project
    course        TEXT,
    topic_id      TEXT,
    title         TEXT,
    detail        TEXT,
    status        TEXT NOT NULL,   -- planned, done, skipped
    gcal_event_id TEXT,
    created_at    TEXT,
    locked        INTEGER DEFAULT 0 -- 1 = you changed/added it: re-plans leave it alone
);

CREATE TABLE IF NOT EXISTS topic_progress (
    topic_id      TEXT PRIMARY KEY,
    done_min      INTEGER DEFAULT 0,
    confidence    INTEGER,          -- 1-5, optional per-topic rating
    last_studied  TEXT,
    parts_done    TEXT              -- JSON list of finished parts, e.g. ["MUX"]
);

CREATE TABLE IF NOT EXISTS notion_map (
    kind          TEXT NOT NULL,   -- course, topic, exam, session, log
    local_id      TEXT NOT NULL,   -- Sitruuna's id for it
    page_id       TEXT NOT NULL,   -- the Notion page
    hash          TEXT,            -- what was last sent (only changes are sent again)
    synced_at     TEXT,
    PRIMARY KEY (kind, local_id)
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

ACTIVE = "active"
PENDING = "pending"


class Store:
    def __init__(self, path: Path | None = None):
        self.path = path or (DATA_DIR / "assistant.db")
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(events)")}
        if "section" not in cols:                    # databases made by older versions
            self.conn.execute("ALTER TABLE events ADD COLUMN section TEXT")
        bcols = {r["name"] for r in self.conn.execute("PRAGMA table_info(plan_blocks)")}
        if "locked" not in bcols:
            self.conn.execute("ALTER TABLE plan_blocks ADD COLUMN locked INTEGER DEFAULT 0")
        tcols = {r["name"] for r in self.conn.execute("PRAGMA table_info(topic_progress)")}
        if "parts_done" not in tcols:
            self.conn.execute("ALTER TABLE topic_progress ADD COLUMN parts_done TEXT")
        self.conn.commit()

    # ---------- key/value ----------
    def get_kv(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_kv(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()

    # ---------- emails ----------
    def is_processed(self, msg_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM processed_emails WHERE msg_id=?", (msg_id,)
        ).fetchone() is not None

    def mark_processed(self, msg_id: str, sender: str, subject: str, result: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO processed_emails VALUES(?,?,?,?,?)",
            (msg_id, _now(), sender, subject, result),
        )
        self.conn.commit()

    def forget_email(self, msg_id: str) -> None:
        """Lets an email be read again (used after an AI error)."""
        self.conn.execute("DELETE FROM processed_emails WHERE msg_id=?", (msg_id,))
        self.conn.commit()

    # ---------- events ----------
    def add_event(self, **fields) -> int:
        fields.setdefault("created_at", _now())
        fields["updated_at"] = _now()
        cols = ",".join(fields)
        marks = ",".join("?" for _ in fields)
        cur = self.conn.execute(
            f"INSERT INTO events({cols}) VALUES({marks})", tuple(fields.values())
        )
        self.conn.commit()
        return cur.lastrowid

    def update_event(self, event_id: int, **fields) -> None:
        fields["updated_at"] = _now()
        sets = ",".join(f"{k}=?" for k in fields)
        self.conn.execute(
            f"UPDATE events SET {sets} WHERE id=?", (*fields.values(), event_id)
        )
        self.conn.commit()

    def get_event(self, event_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()

    def events_on(self, day: str, statuses=(ACTIVE, "cancelled")) -> list[sqlite3.Row]:
        q = f"SELECT * FROM events WHERE date=? AND status IN ({_marks(statuses)}) ORDER BY start_time IS NULL, start_time"
        return self.conn.execute(q, (day, *statuses)).fetchall()

    def events_between(self, start: str, end: str, statuses=(ACTIVE,)) -> list[sqlite3.Row]:
        q = (
            f"SELECT * FROM events WHERE date>=? AND date<=? AND status IN ({_marks(statuses)}) "
            "ORDER BY date, start_time IS NULL, start_time"
        )
        return self.conn.execute(q, (start, end, *statuses)).fetchall()

    def upcoming(self, from_day: str, statuses=(ACTIVE,)) -> list[sqlite3.Row]:
        q = (
            f"SELECT * FROM events WHERE date>=? AND status IN ({_marks(statuses)}) "
            "ORDER BY date, start_time IS NULL, start_time"
        )
        return self.conn.execute(q, (from_day, *statuses)).fetchall()

    def pending(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM events WHERE status='pending' ORDER BY date"
        ).fetchall()

    def future_for_course(self, course: str, from_day: str) -> list[sqlite3.Row]:
        """Active upcoming events for one course (used to spot date changes)."""
        return self.conn.execute(
            "SELECT * FROM events WHERE course=? AND date>=? AND status='active'",
            (course, from_day),
        ).fetchall()

    def same_event_exists(self, course: str | None, type_: str, date: str,
                          start_time: str | None, title: str, history: bool = False) -> bool:
        """True if this item is already stored (avoids duplicates). Things you ❌ ignored,
        ↩️ undid or 🗑 removed also count, so they never come back.
        history=True (used by /rescan) also counts old versions of changed items."""
        statuses = ["active", "pending", "ignored", "undone", "removed"]
        if history:
            statuses.append("superseded")
        rows = self.conn.execute(
            "SELECT title, start_time FROM events WHERE IFNULL(course,'')=IFNULL(?,'') "
            f"AND type=? AND date=? AND status IN ({_marks(statuses)})",
            (course, type_, date, *statuses),
        ).fetchall()
        for r in rows:
            if (r["start_time"] or None) == (start_time or None) or _similar(r["title"], title):
                return True
        return False


def _similar(a: str, b: str) -> bool:
    from difflib import SequenceMatcher
    return SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio() > 0.75


def _marks(items) -> str:
    return ",".join("?" for _ in items)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")
