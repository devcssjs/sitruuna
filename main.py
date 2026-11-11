"""Start Sitruuna.

  python main.py --login        one-time Google sign-in (Gmail + Calendar)
  python main.py                run the assistant (Telegram bot + reminders)
  python main.py --check-once   read new mail once and print what it found (no Telegram)
  python main.py --notify TEXT  send one Telegram message and exit (used by start_assistant.bat / .sh)
  python main.py --check-notion test the Notion connection
  python main.py --setup-notion build (or repair) the Notion workspace, then fill it in

Exit code 3 = "restart me" (after /restart); start_assistant.bat (Windows) or start_assistant.sh (Mac/Linux) then runs it again.
"""
from __future__ import annotations

import argparse
import logging
import sys

from pa.config import DATA_DIR, load_settings
from pa.store import Store


def setup_logging():
    log_dir = DATA_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(log_dir / "assistant.log", encoding="utf-8"),
                  logging.StreamHandler(sys.stdout)],
    )
    for noisy in ("httpx", "googleapiclient", "telegram", "apscheduler", "google_genai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("pypdf").setLevel(logging.ERROR)   # harmless "startxref" warnings


def build_assistant(settings, store):
    from pa.llm import make_llm
    from pa.pipeline import Assistant

    log = logging.getLogger("main")
    llm = gmail = cal = None
    try:
        llm = make_llm(settings)
        log.info("AI: %s (%s)", llm.name, llm.model)
    except Exception as e:
        log.error("AI not ready: %s", e)
    try:
        from pa.gmail_fetch import GmailReader
        gmail = GmailReader(settings)
        log.info("Gmail: connected as %s", gmail.account)
    except Exception as e:
        log.error("Gmail not ready: %s", e)
    if settings.get("google_calendar", "enabled", default=True):
        try:
            from pa.gcal import Calendar
            cal = Calendar(settings)
            log.info("Google Calendar: connected (%s)", cal.cal_id)
        except Exception as e:
            log.error("Google Calendar not ready: %s", e)
    return Assistant(settings, store, llm=llm, calendar=cal, gmail=gmail)


def main():
    ap = argparse.ArgumentParser(description="Sitruuna personal assistant")
    ap.add_argument("--login", action="store_true", help="sign in to Google (one time)")
    ap.add_argument("--check-once", action="store_true", help="read mail once, print results, exit")
    ap.add_argument("--rescan", type=int, metavar="DAYS", help="re-read the last DAYS days of mail, print results, exit")
    ap.add_argument("--notify", metavar="TEXT", help="send one Telegram message and exit")
    ap.add_argument("--check-notion", action="store_true", help="test the Notion connection")
    ap.add_argument("--setup-notion", action="store_true", help="build the Notion workspace and fill it in")
    args = ap.parse_args()

    settings = load_settings()
    setup_logging()

    if args.notify:
        notify(settings, args.notify)
        return

    if args.login:
        from pa.google_auth import login_all
        login_all(settings.get("google_calendar", "enabled", default=True))
        return

    store = Store()
    if args.check_notion or args.setup_notion:
        sys.exit(notion_cli(settings, store, setup=args.setup_notion))
    assistant = build_assistant(settings, store)

    if args.check_once or args.rescan:
        import re
        results = assistant.rescan(args.rescan) if args.rescan else assistant.check_mail()
        for n in results:
            print(re.sub(r"</?\w+>", "", n.text), "\n")
        return

    if not settings.telegram_token:
        sys.exit("TELEGRAM_BOT_TOKEN is empty in .env (see README step 5).")
    from pa.telegram_bot import Bot
    if not settings.telegram_chat_id:
        print("\n>>> Open Telegram, send /start to your bot to get your chat ID,")
        print(">>> put it in .env as TELEGRAM_CHAT_ID, then restart.\n")
    sys.exit(Bot(assistant).run() or 0)


def notion_cli(settings, store, setup: bool) -> int:
    from pa.notion_client import NotionClient, NotionError, page_id_from
    from pa.notion_sync import NotionWorkspace
    from pa.planner import Planner
    if not settings.notion_token or not settings.notion_hq_page:
        print("NOTION_TOKEN and NOTION_HQ_PAGE must both be in .env (see the Notion plan, setup steps).")
        return 1
    client = NotionClient(settings.notion_token)
    try:
        me = client.me()
        print(f"Connected to Notion as: {me.get('name') or me.get('bot', {}).get('workspace_name') or 'Sitruuna'}")
        hq = page_id_from(settings.notion_hq_page)
        client.get_page(hq)
        print("Sitruuna HQ page: found and shared with Sitruuna. ✓")
    except NotionError as e:
        print(f"Problem: {e}")
        if e.status == 401:
            print("-> The token is wrong or was reset. Copy it again from the connection's Configuration tab.")
        elif e.status == 404:
            print("-> Sitruuna can't see the page. On Sitruuna HQ: ••• > Connections > + Add connection > Sitruuna.")
        return 1
    if not setup:
        return 0
    ws = NotionWorkspace(settings, store, Planner(settings, store), client)
    print("Building your workspace (about 5 minutes the first time)...")
    try:
        r = ws.setup()
    except NotionError as e:
        print(f"Setup stopped: {e}\nRun it again to continue; finished parts are kept.")
        return 1
    print("Workspace: " + r.text())
    print("Filling it in (courses, topics, exams, this week's plan)...")
    r2 = ws.sync(owner="setup")
    print("Data: " + r2.text())
    for w in r.warnings + r2.warnings:
        print("  ! " + w)
    print("Done. Open: " + ws.hq_link())
    return 0


def notify(settings, text: str):
    """One plain Telegram message, no bot running (e.g. 'crashed 5 times')."""
    import json
    import urllib.parse
    import urllib.request
    if not (settings.telegram_token and settings.telegram_chat_id):
        return
    data = urllib.parse.urlencode({"chat_id": settings.telegram_chat_id, "text": text}).encode()
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{settings.telegram_token}/sendMessage", data, timeout=15)
    except Exception as e:
        logging.getLogger("main").warning("Couldn't send Telegram message: %s", e)


if __name__ == "__main__":
    main()
