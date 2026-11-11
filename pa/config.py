"""Loads config.yaml (or config.example.yaml if you haven't made one yet) and .env into one settings object."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"


def data_file(name: str, folder: Path | None = None) -> Path:
    """Your own file (e.g. data/timetable.csv) if it exists, else the shipped example
    (data/timetable.example.csv), so a fresh copy runs before you fill anything in."""
    folder = folder or DATA_DIR
    own = folder / name
    stem, dot, ext = name.rpartition(".")
    example = folder / f"{stem}.example.{ext}"
    if os.getenv("SITRUUNA_EXAMPLES") == "1" and example.exists():   # tests always use the examples
        return example
    return own if own.exists() else example


@dataclass
class Settings:
    raw: dict
    tz: ZoneInfo
    llm_provider: str
    gemini_key: str
    anthropic_key: str
    telegram_token: str
    telegram_chat_id: str
    notion_token: str = ""
    notion_hq_page: str = ""

    # --- shortcuts -------------------------------------------------
    def get(self, *keys, default=None):
        node = self.raw
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node

    @property
    def courses(self):
        """Your courses with short forms (see 'courses:' in config.yaml)."""
        from .courses import Courses
        return Courses.from_config(self.raw)

    @property
    def my_courses(self) -> list[str]:
        return [e["code"] for e in self.courses.entries.values()]

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def today(self) -> date:
        return self.now().date()


def load_settings(config_path: Path | None = None) -> Settings:
    load_dotenv(ROOT / ".env")
    path = config_path or data_file("config.yaml", ROOT)
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    DATA_DIR.mkdir(exist_ok=True)
    return Settings(
        raw=raw,
        tz=ZoneInfo(raw.get("timezone", "Asia/Kolkata")),
        llm_provider=os.getenv("LLM_PROVIDER", "gemini").strip().lower(),
        gemini_key=os.getenv("GEMINI_API_KEY", "").strip(),
        anthropic_key=os.getenv("ANTHROPIC_API_KEY", "").strip(),
        telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        notion_token=os.getenv("NOTION_TOKEN", "").strip(),
        notion_hq_page=os.getenv("NOTION_HQ_PAGE", "").strip(),
    )
