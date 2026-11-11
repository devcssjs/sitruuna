"""Telegram bot: commands, Add/Edit/Ignore/Undo buttons, and scheduled messages.

This is the swappable 'notifier'. When you build your own app, replace this
file; pipeline.py (the brain) stays the same.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime, time, timedelta, timezone

from dateutil import parser as dateparser
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, filters)

from . import reminders, timetable
from .google_auth import LoginNeeded
from .pipeline import Assistant, Notice, esc
from .planner import Planner, fmt
from .understand import Interpreter

log = logging.getLogger(__name__)

HELP = (
    "<b>Sitruuna commands</b>\n"
    "/timetable: today's classes and events\n"
    "/timetable tomorrow  or  /timetable 24 oct\n"
    "/week: the next 7 days\n"
    "/deadlines: all upcoming exams and deadlines\n"
    "/pending: items waiting for your ✅\n"
    "/check: check email right now\n"
    "/rescan: re-read the last 60 days of mail (/rescan 30 for fewer)\n"
    "/synctimetable: put your weekly classes on Google Calendar\n"
    "/cleanup: remove non-course items (fests, placements…)\n"
    "\n<b>Study plan</b>\n"
    "/plan: today's study + routine plan (/plan tomorrow, /plan week)\n"
    "/replan: re-plan the rest of today\n"
    "/rate: rate how confident you feel per course (1-5)\n"
    "/progress: syllabus covered + this week's routines\n"
    "/status: mark which topics you've done (✅ / 🟡 / ❌)\n"
    "\n💬 <b>Or just write to me</b>, e.g.\n"
    "<i>“only did MUX today”</i> · <i>“finished DC motors, skipped workout”</i>\n"
    "<i>“EM quiz 2 moved to 18 Nov 5pm”</i> · <i>“what's my plan tomorrow?”</i>\n"
    "Reply to a block's message to talk about that block.\n"
    "/notion: link to your Notion HQ · /sync: update Notion now\n"
    "/restart: restart Sitruuna (back in ~10 s)\n"
    "/help: this list"
)


class Bot:
    def _course_order(self) -> list[str]:
        """Courses to rate / check: planner.course_order in config.yaml, else every course
        that has topics in topics.csv (in config order)."""
        order = self.s.get("planner", "course_order", default=None)
        if order:
            return list(order)
        with_topics = {t.course for t in self.planner.topics} if self.planner else set()
        return [c for c in self.s.courses.entries if not with_topics or c in with_topics]

    def __init__(self, assistant: Assistant):
        self.a = assistant
        self.s = assistant.s
        self.chat_id = self.s.telegram_chat_id
        self._mail_lock = asyncio.Lock()
        self.exit_code = 0                       # 3 = "please restart me" (start_assistant.bat runs me again)
        self.started_at = datetime.now(timezone.utc)
        self.planner = Planner(self.s, assistant.store) if self.s.get("planner", "enabled", default=True) else None
        assistant.planner = self.planner          # the syllabus desk uses the same topics
        self.interp = Interpreter(assistant, self.planner, cal_getter=lambda: self._cal()) if self.planner else None
        self.notion = None
        if self.planner and self.s.notion_token and self.s.get("notion", "enabled", default=True):
            from .notion_sync import NotionWorkspace
            self.notion = NotionWorkspace(self.s, assistant.store, self.planner)
        self._notion_lock = asyncio.Lock()
        self.app = Application.builder().token(self.s.telegram_token).post_init(self._startup).build()
        self._register()

    # ==================== setup ====================
    def _register(self):
        add = self.app.add_handler
        add(CommandHandler("start", self.cmd_start))
        add(CommandHandler("help", self.cmd_help))
        add(CommandHandler(["timetable", "today"], self.cmd_timetable))
        add(CommandHandler("week", self.cmd_week))
        add(CommandHandler("deadlines", self.cmd_deadlines))
        add(CommandHandler("pending", self.cmd_pending))
        add(CommandHandler("check", self.cmd_check))
        add(CommandHandler("synctimetable", self.cmd_sync))
        add(CommandHandler("cleanup", self.cmd_cleanup))
        add(CommandHandler("rescan", self.cmd_rescan))
        add(CommandHandler("restart", self.cmd_restart))
        add(CommandHandler("notion", self.cmd_notion))
        add(CommandHandler("sync", self.cmd_sync_notion))
        add(CommandHandler("plan", self.cmd_plan))
        add(CommandHandler("replan", self.cmd_replan))
        add(CommandHandler("rate", self.cmd_rate))
        add(CommandHandler("progress", self.cmd_progress))
        add(CommandHandler("status", self.cmd_status))
        add(CallbackQueryHandler(self.on_button))
        add(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))
        self.app.add_error_handler(self.on_error)

        if not self.chat_id:
            return  # no schedules until you've linked your chat
        jq = self.app.job_queue
        tz = self.s.tz
        every = int(self.s.get("gmail", "check_every_minutes", default=30)) * 60
        jq.run_repeating(self.job_mail, interval=every, first=15, name="mail")
        jq.run_daily(self.job_digest, time=_hhmm(self.s.get("reminders", "digest_time", default="08:00"), tz))
        jq.run_daily(self.job_prep, time=_hhmm(self.s.get("reminders", "prep_time", default="19:00"), tz))
        jq.run_repeating(self.job_soon, interval=60, first=20, name="soon")   # every minute
        if self.planner:
            jq.run_daily(self.job_replan, time=_hhmm(self.s.get("planner", "replan_time", default="22:00"), tz))
            if self.notion:
                every_n = int(self.s.get("notion", "sync_every_minutes", default=2)) * 60
                jq.run_repeating(self.job_notion, interval=every_n, first=60, name="notion")
            # weekly "how far are you?" check-in (PTB days: 0 = Sunday)
            jq.run_daily(self.job_status, time=_hhmm(self.s.get("planner", "status_check_time", default="19:30"), tz),
                         days=(0,))

    async def on_error(self, update, ctx):
        """Short log line for brief internet drops instead of long error dumps."""
        log.warning("Telegram: %s", str(ctx.error)[:200])

    def run(self) -> int:
        """Runs until stopped. Returns 3 after /restart, else 0."""
        log.info("Bot running. Press Ctrl+C to stop.")
        self.app.run_polling(allowed_updates=Update.ALL_TYPES)
        self.a.store.set_kv("running", "0")      # clean stop (not a crash)
        return self.exit_code

    # ==================== Notion ====================
    async def notion_sync(self):
        """Runs one sync in the background. Returns the report, or None if one is already running."""
        if not self.notion or self._notion_lock.locked():
            return None
        async with self._notion_lock:
            return await asyncio.to_thread(self.notion.sync)

    async def job_notion(self, ctx):
        try:
            r = await self.notion_sync()
        except Exception as e:
            log.warning("Notion sync failed: %s", str(e)[:200])
            r = None
            key = f"notion:alerted:{self.s.today().isoformat()}"
            if not self.a.store.get_kv(key):           # tell you at most once a day
                self.a.store.set_kv(key, "1")
                await self.send(f"⚠️ Couldn't update Notion: {esc(str(e)[:200])}\nI'll keep trying every few minutes.")
        if r and (r.created or r.updated or r.archived):
            log.info("Notion sync: %s", r.text())

    async def cmd_notion(self, update, ctx):
        if self._is_owner(update):
            await self.notion_info()

    async def notion_info(self):
        if not self.notion:
            await self.send("Notion isn't connected yet. Add NOTION_TOKEN and NOTION_HQ_PAGE to .env, "
                            "then run <code>python main.py --setup-notion</code> on your computer.")
            return
        if not self.notion.ready():
            await self.send("Notion is connected but not built yet. On your computer run:\n<code>python main.py --setup-notion</code>")
            return
        last = self.a.store.get_kv("notion:last_sync") or "not yet"
        await self.send(f"🍋 <a href=\"{self.notion.hq_link()}\">Open Sitruuna HQ in Notion</a>\nLast update: {esc(last[11:16] or last)}")

    async def cmd_sync_notion(self, update, ctx):
        if self._is_owner(update):
            await self.sync_now()

    async def sync_now(self):
        if not self.notion or not self.notion.ready():
            await self.notion_info()
            return
        await self.send("🔄 Updating Notion…")
        try:
            r = await self.notion_sync()
        except Exception as e:
            await self.send(f"⚠️ Notion update failed: {esc(str(e)[:300])}")
            return
        await self.send("✅ Notion updated: " + esc(r.text()) if r else "⏳ An update is already running; it'll finish in a moment.")

    async def cmd_restart(self, update, ctx):
        if not self._is_owner(update):
            return
        if update.message.date < self.started_at - timedelta(seconds=5):
            log.info("Ignoring an old /restart sent before this start")
            return
        if self._mail_lock.locked():
            await self.send("⏳ Finishing the mail check first, then restarting…")
        await self._do_restart()

    async def _do_restart(self):
        async with self._mail_lock:
            log.info("Restart asked from Telegram")
            await self.send("🔄 Restarting… back in about 10 seconds.")
            self.a.store.set_kv("restart_notify", "1")
            self.exit_code = 3
            self._stop()

    def _stop(self):
        self.app.stop_running()     # run_polling() returns, then main.py exits with code 3

    # ==================== safety: only you ====================
    def _is_owner(self, update: Update) -> bool:
        return bool(self.chat_id) and str(update.effective_chat.id) == str(self.chat_id)

    # ==================== sending ====================
    async def send_block_msg(self, block_id: int, text: str, buttons):
        msg = await self.send(text, buttons)
        if msg is not None:
            self.a.store.set_kv(f"msgblock:{msg.message_id}", str(block_id))
        return msg

    async def send(self, text: str, buttons=None, rows=None):
        markup = _markup_rows(rows) if rows else _markup(buttons)
        msg = None
        for i, chunk in enumerate(_chunks(text)):
            msg = await self.app.bot.send_message(
                chat_id=self.chat_id, text=chunk, parse_mode=ParseMode.HTML,
                reply_markup=markup if i == len(_chunks(text)) - 1 else None,
                disable_web_page_preview=True,
            )
        # remember "please check" messages, so they can be marked ⌛ Expired later
        if self.interp and text:
            self.interp.remember("sitruuna", text)
        yes = next((d for _, d in buttons or [] if d.startswith(("add:", "uok:", "sylok:"))), None)
        no = next((d for _, d in buttons or [] if d.startswith(("ignore:", "uno:", "sylno:"))), None)
        if yes:
            import json
            self.a.store.set_kv("last_ask", json.dumps({"yes": yes, "no": no, "at": self.s.now().isoformat()}))
        for _, data in buttons or []:
            if data.startswith("add:") and msg is not None:
                key = f"askmsg:{data[4:]}"
                ids = (self.a.store.get_kv(key) or "").split(",")
                self.a.store.set_kv(key, ",".join(filter(None, ids + [str(msg.message_id)])))
        return msg

    async def send_notices(self, notices: list[Notice]):
        for n in notices:
            await self.send(n.text, n.buttons)
        await self.refresh_plan_if_changed(notices)

    # ==================== study plan helpers ====================
    def _cal(self):
        return self.a.cal if self.s.get("planner", "calendar_blocks", default=True) else None

    def _now_min(self) -> int:
        n = self.s.now()
        return n.hour * 60 + n.minute

    async def replan(self, d: date, from_now: bool = False) -> str:
        sleep = self.a.store.get_kv(f"sleep:{d.isoformat()}")
        frm = self._now_min() if from_now and d == self.s.today() else 0
        blocks = await asyncio.to_thread(self.planner.save_day, d, sleep, frm, self._cal())
        return self.planner.format_day(d, blocks)

    async def refresh_plan_if_changed(self, notices: list[Notice]):
        if not self.planner or not any(n.kind in ("added", "changed", "cancelled", "syllabus") for n in notices):
            return
        today = self.s.today()
        await self.replan(today, from_now=True)
        await self.replan(today + timedelta(days=1))
        if any(n.kind in ("added", "changed", "cancelled") for n in notices):
            await self.send("📅 Your dates changed, so I updated today's and tomorrow's plan → /plan")
        else:
            await self.send("📘 Plan for today and tomorrow now follows the syllabus → /plan")

    async def _startup(self, app):
        """When Sitruuna starts: load known dates once, make sure today/tomorrow have a plan."""
        if self.chat_id:
            st = self.a.store
            try:
                if st.get_kv("restart_notify") == "1":
                    await self.send("✅ Back online.")
                elif st.get_kv("running") == "1":
                    await self.send("⚠️ Sitruuna wasn't stopped cleanly last time (a crash, or the window was closed). "
                                    "It's running again; nothing is lost.")
            except Exception:
                log.exception("Startup message failed")
            st.set_kv("restart_notify", "")
            st.set_kv("running", "1")
        if not self.chat_id or not self.planner:
            return
        try:
            from .seed import seed, seed_progress
            notices = await asyncio.to_thread(seed, self.a)
            await asyncio.to_thread(seed_progress, self.planner)
            if notices:
                await self._send_summary("📌 Loaded your known quiz/compre dates", notices)
            from .seed import seed_syllabi
            await asyncio.to_thread(seed_syllabi, self.planner)
            today = self.s.today()
            from .planner import PLANNER_VERSION
            old_version = self.a.store.get_kv("planner_version")
            if old_version != PLANNER_VERSION:      # new planning rules -> redo today/tomorrow
                self.a.store.set_kv("planner_version", PLANNER_VERSION)
                await self.replan(today, from_now=True)
                await self.replan(today + timedelta(days=1))
            if old_version and old_version != PLANNER_VERSION:      # only tell people who used an older version
                await self.send("🆕 Quizzes are planned better now:\n"
                                "• from 6 days before a quiz, at least one block a day on its topics\n"
                                "• before an evening quiz, that day's blocks are revision for it\n"
                                "• when the professor mails the syllabus, I switch to it and re-plan "
                                "(you can also type it, e.g. <i>“M3 quiz 2 syllabus is Legendre and Bessel”</i>)\n"
                                "• no prep blocks for ungraded sessions; catch-up blocks take turns between courses")
            try:
                syl = await asyncio.to_thread(self.a.syllabus.backfill)
            except Exception:
                log.exception("Syllabus backfill failed")
                syl = []
            if syl:
                await self.send_notices(syl)
            for d in (today, today + timedelta(days=1)):
                if not self.planner.blocks_on(d):
                    await self.replan(d, from_now=True)
            if not any(self.a.store.get_kv(f"conf:{c}") for c in self._course_order()):
                await self.send("👋 New: <b>study planner</b>. First, how confident do you feel in each course?")
                await self._send_rate()
            if not self.a.store.get_kv("status_asked_v1"):
                self.a.store.set_kv("status_asked_v1", "1")
                await self.send("📋 Let's get your plan right: mark how far you are in each course. "
                                "Midsem topics start as ✅ (except EM DC machines).")
                await self._start_status(self.planner.courses_to_check(today, only_open=False))
        except Exception:
            log.exception("Planner startup failed")

    # ==================== commands ====================
    async def cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self.chat_id:
            await update.message.reply_text(
                f"Hi! Your chat ID is {update.effective_chat.id}\n\n"
                "Put it in your .env file as TELEGRAM_CHAT_ID, then restart the assistant."
            )
            return
        if not self._is_owner(update):
            return
        await update.message.reply_html("👋 Sitruuna is running.\n\n" + HELP)

    async def cmd_help(self, update, ctx):
        if self._is_owner(update):
            await update.message.reply_html(HELP)

    async def cmd_timetable(self, update, ctx):
        if not self._is_owner(update):
            return
        d = self.s.today()
        if ctx.args:
            arg = " ".join(ctx.args).lower()
            if arg in ("tomorrow", "tmrw", "tom"):
                d += timedelta(days=1)
            else:
                try:
                    d = dateparser.parse(arg, dayfirst=True, default=datetime.combine(d, time())).date()
                except (ValueError, OverflowError):
                    await update.message.reply_text("I couldn't read that date. Try: /timetable 24 oct")
                    return
        await self.send(timetable.format_day(self.a.store, d))

    async def cmd_week(self, update, ctx):
        if self._is_owner(update):
            await self.send(timetable.format_week(self.a.store, self.s.today()))

    async def cmd_deadlines(self, update, ctx):
        if self._is_owner(update):
            await self.send(timetable.format_deadlines(self.a.store, self.s.today()))

    async def cmd_pending(self, update, ctx):
        if not self._is_owner(update):
            return
        rows = self.a.store.pending()
        if not rows:
            await self.send("Nothing waiting. 👍")
            return
        for r in rows:
            n = self.a._ask(r["id"])
            await self.send(n.text, n.buttons)

    async def cmd_check(self, update, ctx):
        if not self._is_owner(update):
            return
        await update.message.reply_text("📬 Checking email…")
        found = await self._run_mail_check()
        if found == -1:
            await self.send("⏳ Already reading mail (maybe a /rescan). I'll report when it's done.")
        elif found == 0:
            await self.send("No new timetable items.")

    async def cmd_sync(self, update, ctx):
        if not self._is_owner(update):
            return
        if not self.a.cal:
            await self.send("Google Calendar isn't connected. Run: python main.py --login")
            return
        try:
            n = await asyncio.to_thread(self.a.cal.sync_weekly_classes, self.a.store)
            await self.send(f"📆 Put {n} weekly classes on Google Calendar (repeating until semester end).")
        except Exception as e:
            await self.send(f"⚠️ Calendar sync failed: {e}")

    async def cmd_rescan(self, update, ctx):
        if not self._is_owner(update):
            return
        days = int(self.s.get("gmail", "backfill_days", default=60))
        if ctx.args and ctx.args[0].isdigit():
            days = max(1, min(int(ctx.args[0]), 365))
        if self._mail_lock.locked():
            await self.send("⏳ I'm already reading mail. Try /rescan again in a bit.")
            return
        async with self._mail_lock:
            await self.send(f"🔄 Re-reading the last {days} days of mail with the new rules…\n"
                            "This can take about an hour on the free AI. I'll message you when done; "
                            "everything else keeps working.")
            try:
                notices = await asyncio.to_thread(self.a.rescan, days)
            except LoginNeeded as e:
                await self.send(f"🔑 {e}")
                return
            except Exception as e:
                log.exception("Rescan failed")
                await self.send(f"⚠️ Rescan stopped: {esc(str(e)[:300])}")
                return
            stopped = any(n.kind == "stop" for n in notices)
            await self._send_summary("⏸ Rescan stopped early" if stopped else "✅ Rescan done!", notices)

    async def cmd_plan(self, update, ctx):
        if not self._is_owner(update) or not self.planner:
            return
        d = self.s.today()
        arg = " ".join(ctx.args).lower() if ctx.args else ""
        if arg in ("week", "w"):
            parts = []
            for i in range(7):
                day = d + timedelta(days=i)
                blocks = self.planner.blocks_on(day) if i < 2 else self.planner.plan_day(day)
                parts.append(self.planner.format_day(day, blocks))
            await self.send("\n\n".join(parts) + "\n\n<i>Days after tomorrow are a rough preview; "
                            "they're re-planned each night from what you actually did.</i>")
            return
        if arg in ("tomorrow", "tmrw", "tom"):
            d += timedelta(days=1)
        elif arg:
            try:
                d = dateparser.parse(arg, dayfirst=True, default=datetime.combine(d, time())).date()
            except (ValueError, OverflowError):
                await self.send("I couldn't read that date. Try: /plan tomorrow")
                return
        if not self.planner.blocks_on(d) and d <= self.s.today() + timedelta(days=1):
            await self.send(await self.replan(d, from_now=True))
        else:
            blocks = self.planner.blocks_on(d) or self.planner.plan_day(d)
            await self.send(self.planner.format_day(d, blocks))

    async def cmd_replan(self, update, ctx):
        if self._is_owner(update) and self.planner:
            await self.send("🔄 Re-planned the rest of today:\n" + await self.replan(self.s.today(), from_now=True))

    async def cmd_rate(self, update, ctx):
        if self._is_owner(update) and self.planner:
            await self._send_rate()

    async def _send_rate(self):
        rows = [[(f"{c} ({self.planner.confidence(c)})", "noop:0")] + [(str(i), f"rate:{c}:{i}") for i in range(1, 6)]
                for c in self._course_order()]
        rows.append([("✅ Done – make my plan", "ratedone:0")])
        await self.send("📊 <b>How confident are you in each course?</b>\n"
                        "1 = lost · 3 = okay · 5 = solid. Weaker courses get more study time.", rows=rows)

    async def cmd_status(self, update, ctx):
        if self._is_owner(update) and self.planner:
            await self._start_status(self.planner.courses_to_check(self.s.today(), only_open=False))

    async def job_status(self, ctx):
        courses = self.planner.courses_to_check(self.s.today(), only_open=True)
        if not courses:
            await self.send("📋 Weekly check: you're up to date on every topic taught so far. 💪")
            return
        await self.send("📋 <b>Weekly check-in</b>: how far did you get this week?")
        await self._start_status(courses)

    def _status_view(self, idx: int):
        import json
        courses = json.loads(self.a.store.get_kv("statusflow") or "[]")
        if not courses:
            return "Nothing to check.", None
        idx = max(0, min(idx, len(courses) - 1))
        c, today = courses[idx], self.s.today()
        rows = []
        for i, t in enumerate(self.planner.taught(c, today), 1):
            mark = {"d": "✅", "h": "🟡", "n": "❌"}[self.planner.status(t)]
            rows.append([(f"{i}. {mark}", "noop:0"), ("✅", f"tstat:{idx}:{t.id}:d"),
                         ("🟡", f"tstat:{idx}:{t.id}:h"), ("❌", f"tstat:{idx}:{t.id}:n")])
        nav = []
        if idx > 0:
            nav.append(("◀ Back", f"tnext:{idx - 1}"))
        if idx < len(courses) - 1:
            nav.append((f"Next: {courses[idx + 1]} ▶", f"tnext:{idx + 1}"))
        else:
            nav.append(("💾 Save & re-plan", "tsave:0"))
        rows.append(nav)
        text = self.planner.status_text(c, today) + f"\n<i>Course {idx + 1} of {len(courses)}</i>"
        return text, rows

    async def _start_status(self, courses: list[str]):
        import json
        if not courses:
            await self.send("No topics taught yet to check.")
            return
        self.a.store.set_kv("statusflow", json.dumps(courses))
        text, rows = self._status_view(0)
        await self.send(text, rows=rows)

    async def _show_status(self, q, idx: int):
        text, rows = self._status_view(idx)
        try:
            await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=_markup_rows(rows) if rows else None)
        except Exception:
            pass   # "message is not modified" when tapping the same mark twice

    async def cmd_progress(self, update, ctx):
        if self._is_owner(update) and self.planner:
            await self.send(self.planner.progress_text(self.s.today()))

    async def _send_summary(self, title: str, notices: list[Notice]):
        added = [n for n in notices if n.kind in ("added", "changed")]
        cancelled = [n for n in notices if n.kind == "cancelled"]
        ask = [n for n in notices if n.kind == "confirm"]
        errors = [n for n in notices if n.kind == "error"]
        stops = [n for n in notices if n.kind == "stop"]
        unread = [n for n in notices if n.kind == "info"]
        lines = [f"<b>{title}</b>",
                 f"✅ Added {len(added)} item(s)" + (" (also on Google Calendar)" if self.a.cal else ""),
                 f"🚫 {len(cancelled)} class cancellation(s)",
                 f"🤔 {len(ask)} need your check → /pending"]
        if errors:
            lines.append(f"⚠️ {len(errors)} email(s) couldn't be read (they'll be retried on the next check)")
        if unread:
            lines.append(f"📎 {len(unread)} email(s) had attachments I couldn't open")
        lines.append("📋 See everything → /deadlines")
        await self.send("\n".join(lines))
        for n in stops:
            await self.send(n.text)
        for n in (added + cancelled)[:15]:      # show the actual items (up to 15)
            await self.send(n.text, n.buttons)
        if len(added + cancelled) > 15:
            await self.send(f"…and {len(added + cancelled) - 15} more. See /deadlines")

    async def cmd_cleanup(self, update, ctx):
        if not self._is_owner(update):
            return
        rows = self.a.cleanup_candidates()
        if not rows:
            await self.send("Nothing to clean up. 👍")
            return
        lines = [f"🧹 <b>{len(rows)} items without a course</b> (fests, placements, competitions…):"]
        for r in rows[:40]:
            mark = "📅" if r["status"] == "active" else "🤔"
            lines.append(f"   {mark} {r['date']}  {esc(r['title'][:70])}")
        if len(rows) > 40:
            lines.append(f"   …and {len(rows) - 40} more")
        lines.append("\n📅 = on your Google Calendar, 🤔 = waiting in /pending\nRemove them all?")
        await self.send("\n".join(lines), [("🗑 Remove all", "cleanup:0"), ("Keep", "cleanupno:0")])

    # ==================== buttons ====================
    async def on_button(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        if not self._is_owner(update):
            await q.answer()
            return
        await q.answer()
        action, _, raw_id = (q.data or "").partition(":")
        if action in ("sleep", "rate", "ratedone", "noop", "tstat", "tnext", "tsave", "bstart", "blater", "bskip", "bdone", "bnot",
                      "bpart", "ptog", "psave", "uok", "uno", "uundo"):
            await self.on_plan_button(q, action, raw_id)
            return
        if not raw_id.isdigit():
            return
        eid = int(raw_id)

        if action == "edit":
            ctx.user_data["editing"] = eid
            await self.send("✏️ Send the correct date and time, for example:\n"
                            "<code>24 Oct 14:00</code>  or  <code>24/10</code> (date only)")
            return

        if action in ("cleanup", "cleanupno"):
            if action == "cleanup":
                n = await asyncio.to_thread(self.a.cleanup_apply)
                result = f"🗑 Removed {n} items (and from Google Calendar)."
            else:
                result = "👍 Kept everything."
            await q.edit_message_text((q.message.text_html or "") + "\n\n" + result,
                                      parse_mode=ParseMode.HTML)
            return

        desk = self.a.syllabus
        func = {"add": self.a.confirm, "ignore": self.a.ignore, "undo": self.a.undo,
                "sylok": desk.confirm, "sylno": desk.reject, "sylundo": desk.undo}.get(action)
        if not func:
            return
        n = await asyncio.to_thread(func, eid)
        if action in ("add", "undo"):
            await self.refresh_plan_if_changed([Notice("changed", eid, "")])
        elif n.kind == "syllabus":
            await self.refresh_plan_if_changed([n])
        try:
            await q.edit_message_text(
                (q.message.text_html or "") + "\n\n" + n.text,
                parse_mode=ParseMode.HTML, reply_markup=_markup(n.buttons),
                disable_web_page_preview=True,
            )
        except Exception:  # message too old/long to edit -> send a new one
            await self.send(n.text, n.buttons)

    async def _append(self, q, extra: str, keep_buttons=False):
        try:
            await q.edit_message_text((q.message.text_html or "") + "\n\n" + extra, parse_mode=ParseMode.HTML,
                                      reply_markup=q.message.reply_markup if keep_buttons else None,
                                      disable_web_page_preview=True)
        except Exception:
            await self.send(extra)

    async def on_plan_button(self, q, action: str, raw: str):
        if not self.planner or action == "noop":
            return
        today = self.s.today()
        if action == "sleep":
            self.a.store.set_kv(f"sleep:{today.isoformat()}", raw)
            if raw == "bad":
                plan = await self.replan(today, from_now=True)
                await self._append(q, "😴 Got it, I made today lighter (1 hour less study).")
                await self.send(plan)
            else:
                await self._append(q, {"ok": "👍 Noted. Plan stays as is.", "great": "💪 Nice! Plan stays as is."}.get(raw, "👍"))
            return
        if action == "rate":
            course, _, val = raw.partition(":")
            if val.isdigit():
                self.planner.set_confidence(course, int(val))
                rows = [[(f"{c} ({self.planner.confidence(c)})", "noop:0")] + [(str(i), f"rate:{c}:{i}") for i in range(1, 6)]
                        for c in self._course_order()] + [[("✅ Done – make my plan", "ratedone:0")]]
                try:
                    await q.edit_message_reply_markup(reply_markup=_markup_rows(rows))
                except Exception:
                    pass
            return
        if action in ("ptog", "psave"):
            await self.on_parts_button(q, action, raw)
            return
        if action == "tstat":
            idx, _, rest = raw.partition(":")
            tid, _, st = rest.partition(":")
            self.planner.set_topic_status(tid, st, today)
            await self._show_status(q, int(idx) if idx.isdigit() else 0)
            return
        if action == "tnext":
            await self._show_status(q, int(raw) if raw.isdigit() else 0)
            return
        if action == "tsave":
            import json
            for c in json.loads(self.a.store.get_kv("statusflow") or "[]"):
                self.planner.mark_seen(c, today)
            try:
                await q.edit_message_text("💾 Saved your study status. Re-planning…", parse_mode=ParseMode.HTML)
            except Exception:
                pass
            plan = await self.replan(today, from_now=True)
            await self.replan(today + timedelta(days=1))
            await self.send(plan)
            return
        if action == "ratedone":
            await self._append(q, "Thanks! Making your plan…")
            await self.send(await self.replan(today, from_now=True))
            await self.replan(today + timedelta(days=1))
            return
        if not raw.isdigit():
            return
        bid = int(raw)
        cal = self._cal()
        if action == "bstart":
            b = await asyncio.to_thread(self.planner.set_status, bid, "started")
            await self._append(q, "▶️ Started. I'll ask how it went when it ends." if b else "Block not found.")
        elif action == "bskip":
            b = await asyncio.to_thread(self.planner.set_status, bid, "skipped", cal)
            await self._append(q, "⏭ Skipped. It'll come back in a later plan." if b else "Block not found.")
        elif action == "blater":
            b = await asyncio.to_thread(self.planner.move_later, bid, self._now_min(), cal)
            if b:
                await self._append(q, f"⏰ Moved to {fmt(b.start)}–{fmt(b.end)}" + (" (Calendar updated)." if cal else "."))
            else:
                await self._append(q, "No free time left today, so I skipped it. It'll come back in a later plan.")
        elif action == "bdone":
            b = await asyncio.to_thread(self.planner.set_status, bid, "done")
            await self._append(q, "✅ Nice work! Counted." if b else "Block not found.")
        elif action == "bnot":
            b = await asyncio.to_thread(self.planner.set_status, bid, "skipped")
            await self._append(q, "👌 Okay, not counted. It'll come back in a later plan." if b else "Block not found.")
        elif action == "bpart":
            b = self.planner.get(bid)
            t = self.planner.topic(b.topic_id) if b and b.topic_id else None
            if t and t.parts:
                import json
                self.a.store.set_kv(f"psel:{bid}", json.dumps(self.planner.parts_done(t.id)))
                await self._show_parts(q, bid)
            else:
                b = await asyncio.to_thread(self.planner.set_status, bid, "partial")
                await self._append(q, "🟡 Counted as partly done. The rest comes back in a later plan." if b else "Block not found.")
                if b:
                    await self.replan(today, from_now=True)
        elif action == "uok":
            await self._after_interp(q, await asyncio.to_thread(self.interp.confirm, bid))
        elif action == "uno":
            await self._after_interp(q, await asyncio.to_thread(self.interp.reject, bid))
        elif action == "uundo":
            await self._after_interp(q, await asyncio.to_thread(self.interp.undo, bid))

    async def _after_interp(self, q, r):
        await self._append(q, r.text)
        if r.buttons:
            await self.send("↩️ Changed your mind?", r.buttons)
        await self._replan_after(r)

    async def _replan_after(self, r):
        """Progress -> refresh today+tomorrow. 'Lighter' / 'focus' -> re-plan those days. Your own block
        changes (replace / move / add / remove) are locked, so nothing else needs re-planning."""
        today = self.s.today()
        days = {date.fromisoformat(x) for x in r.replan_days}
        if r.replan:
            days |= {today, today + timedelta(days=1)}
        for d in sorted(days):
            await self.replan(d, from_now=(d == today))
        if days:
            await self.send("📅 Plan updated → /plan")

    async def run_action(self, act: str):
        if act == "restart":
            await self._do_restart()
        elif act == "replan" and self.planner:
            await self.send("🔄 Re-planned the rest of today:\n" + await self.replan(self.s.today(), from_now=True))
        elif act == "check_mail":
            await self.send("📬 Checking email…")
            found = await self._run_mail_check()
            if found == 0:
                await self.send("No new timetable items.")
            elif found == -1:
                await self.send("⏳ Already reading mail; I'll report when done.")
        elif act == "rate" and self.planner:
            await self._send_rate()
        elif act == "status" and self.planner:
            await self._start_status(self.planner.courses_to_check(self.s.today(), only_open=False))
        elif act == "sync_notion":
            await self.sync_now()
        elif act == "notion":
            await self.notion_info()
        elif act == "sync_calendar" and self.a.cal:
            n = await asyncio.to_thread(self.a.cal.sync_weekly_classes, self.a.store)
            await self.send(f"📆 Put {n} weekly classes on Google Calendar.")
        else:
            await self.send(HELP)

    async def do_answer(self, ans: str, quiet: bool = False) -> bool:
        """'yes' / 'no' typed after a ✅ / ❌ question = tapping that button. False = nothing was asked."""
        import json
        raw = self.a.store.get_kv("last_ask")
        if not raw:
            return False
        ask = json.loads(raw)
        if datetime.fromisoformat(ask["at"]) < self.s.now() - timedelta(minutes=60):
            return False
        data = ask.get("yes") if ans == "yes" else ask.get("no")
        self.a.store.set_kv("last_ask", "")
        if not data:
            return False
        action, _, raw_id = data.partition(":")
        if not raw_id.isdigit():
            return False
        i = int(raw_id)
        if action in ("add", "ignore"):
            n = await asyncio.to_thread(self.a.confirm if action == "add" else self.a.ignore, i)
            await self.send(n.text, n.buttons)
            if action == "add":
                await self.refresh_plan_if_changed([n])
        elif action in ("sylok", "sylno"):
            desk = self.a.syllabus
            n = await asyncio.to_thread(desk.confirm if action == "sylok" else desk.reject, i)
            await self.send(n.text, n.buttons)
            await self.refresh_plan_if_changed([n])
        elif action in ("uok", "uno"):
            r = await asyncio.to_thread(self.interp.confirm if action == "uok" else self.interp.reject, i)
            await self.send(r.text, r.buttons)
            await self._replan_after(r)
        return True

    async def _show_parts(self, q, bid: int):
        import json
        b = self.planner.get(bid)
        t = self.planner.topic(b.topic_id)
        sel = json.loads(self.a.store.get_kv(f"psel:{bid}") or "[]")
        rows = [[(("✅ " if p in sel else "⬜ ") + p, f"ptog:{bid}:{i}")] for i, p in enumerate(t.parts)]
        rows.append([("💾 Save", f"psave:{bid}")])
        text = (f"🟡 <b>Which parts of {esc(t.title)} did you finish?</b>\nTap to tick, then Save.")
        try:
            await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=_markup_rows(rows))
        except Exception:
            pass

    async def on_parts_button(self, q, action: str, raw: str):
        import json
        bid_s, _, idx = raw.partition(":")
        bid = int(bid_s)
        b = self.planner.get(bid)
        t = self.planner.topic(b.topic_id) if b and b.topic_id else None
        if not t:
            return
        key = f"psel:{bid}"
        sel = json.loads(self.a.store.get_kv(key) or "[]")
        if action == "ptog" and idx.isdigit() and int(idx) < len(t.parts):
            p = t.parts[int(idx)]
            sel = [x for x in sel if x != p] if p in sel else sel + [p]
            self.a.store.set_kv(key, json.dumps(sel))
            await self._show_parts(q, bid)
            return
        # psave
        done = await asyncio.to_thread(self.planner.set_parts, t.id, sel, self.s.today())
        left = self.planner.parts_left(t)
        await asyncio.to_thread(self.planner.set_status, bid, "partial" if left else "done", None, False)
        msg = (f"🟡 Saved: {esc(', '.join(done)) or 'nothing'} done.\nLeft: {esc(', '.join(left))}. "
               "The next block will cover what's left." if left else "✅ All parts done. Nice!")
        try:
            await q.edit_message_text(msg, parse_mode=ParseMode.HTML)
        except Exception:
            await self.send(msg)
        await self.replan(self.s.today(), from_now=True)
        await self.replan(self.s.today() + timedelta(days=1))
    async def answer_question(self, q: str):
        today, st = self.s.today(), self.a.store
        tom = today + timedelta(days=1)
        if q == "plan_today" and self.planner:
            await self.send(self.planner.format_day(today))
        elif q == "plan_tomorrow" and self.planner:
            await self.send(self.planner.format_day(tom, self.planner.blocks_on(tom) or self.planner.plan_day(tom)))
        elif q == "plan_week" and self.planner:
            parts = [self.planner.format_day(today + timedelta(days=i), self.planner.blocks_on(today + timedelta(days=i))
                                             or self.planner.plan_day(today + timedelta(days=i))) for i in range(7)]
            await self.send("\n\n".join(parts))
        elif q == "timetable_today":
            await self.send(timetable.format_day(st, today))
        elif q == "timetable_tomorrow":
            await self.send(timetable.format_day(st, tom))
        elif q == "week":
            await self.send(timetable.format_week(st, today))
        elif q == "deadlines":
            await self.send(timetable.format_deadlines(st, today))
        elif q == "progress" and self.planner:
            await self.send(self.planner.progress_text(today))
        elif q == "status" and self.planner:
            await self._start_status(self.planner.courses_to_check(today, only_open=False))
        elif q == "pending":
            rows = st.pending()
            if not rows:
                await self.send("Nothing waiting. 👍")
            for r in rows:
                n = self.a._ask(r["id"])
                await self.send(n.text, n.buttons)
        else:
            await self.send(HELP)

    async def on_text(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_owner(update):
            return
        eid = ctx.user_data.get("editing")
        if not eid:
            text = update.message.text
            t = text.lower().strip(" ?!.")
            if self.interp:
                self.interp.remember("you", text)
            shortcut = {"plan": "plan_today", "today": "timetable_today", "timetable": "timetable_today",
                        "tomorrow": "timetable_tomorrow", "week": "week", "deadlines": "deadlines",
                        "progress": "progress", "pending": "pending"}.get(t)
            action = {"restart": "restart", "replan": "replan", "re-plan": "replan", "check": "check_mail",
                      "check mail": "check_mail", "rate": "rate", "status": "status", "help": "help",
                      "sync": "sync_notion", "notion": "notion"}.get(t)
            answer = ("yes" if t in ("yes", "y", "yeah", "yep", "ok", "okay", "sure", "do it", "yes please") else
                      "no" if t in ("no", "n", "nope", "nah", "don't", "dont", "no thanks") else None)
            if answer and await self.do_answer(answer, quiet=True):
                return
            if action:
                await self.run_action(action)
                return
            if shortcut or not self.interp:
                if shortcut:
                    await self.answer_question(shortcut)
                    return
            else:
                rep = update.message.reply_to_message
                rb = self.a.store.get_kv(f"msgblock:{rep.message_id}") if rep else None
                await update.message.reply_text("🧠 …")
                r = await asyncio.to_thread(self.interp.handle, text, int(rb) if rb and rb.isdigit() else None,
                                            (rep.text or rep.caption) if rep else None)
                if r.kind == "date_change":
                    notices = await asyncio.to_thread(self.a.from_user_text, text)
                    await self.send_notices(notices)
                elif r.kind == "question":
                    await self.answer_question(r.question)
                elif r.kind == "action":
                    await self.run_action(r.action)
                elif r.kind == "answer":
                    if not await self.do_answer(r.answer):
                        await self.send("🤔 Yes/no to what? I'm not waiting on an answer right now.")
                else:
                    await self.send(r.text, r.buttons)
                    await self._replan_after(r)
                return
            if "plan" in t and self.planner:
                d = self.s.today() + timedelta(days=1 if "tomorrow" in t else 0)
                await self.send(self.planner.format_day(d))
            elif "tomorrow" in t:
                await self.send(timetable.format_day(self.a.store, self.s.today() + timedelta(days=1)))
            elif "week" in t:
                await self.send(timetable.format_week(self.a.store, self.s.today()))
            elif any(w in t for w in ("deadline", "exam", "quiz", "upcoming")):
                await self.send(timetable.format_deadlines(self.a.store, self.s.today()))
            elif any(w in t for w in ("timetable", "today", "schedule", "classes")):
                await self.send(timetable.format_day(self.a.store, self.s.today()))
            else:
                await update.message.reply_html("Not sure what you mean. " + HELP)
            return
        new_date, new_time = parse_user_datetime(update.message.text, self.s.today())
        if not new_date:
            await update.message.reply_text("I couldn't read that. Try: 24 Oct 14:00")
            return
        ctx.user_data.pop("editing", None)
        n = await asyncio.to_thread(self.a.edit, eid, new_date, new_time)
        await self.send(n.text, n.buttons)

    # ==================== scheduled jobs ====================
    async def _run_mail_check(self) -> int:
        if self._mail_lock.locked():
            return -1
        async with self._mail_lock:
            first_run = self.a.store.get_kv("last_mail_check") is None
            if first_run:
                await self.send("📚 First run: reading the last "
                                f"{self.s.get('gmail', 'backfill_days', default=60)} days of mail. "
                                "This can take a while on the free AI tier…")
            try:
                notices = await asyncio.to_thread(self.a.check_mail)
            except LoginNeeded as e:
                await self.send(f"🔑 {e}")
                return 0
            except Exception as e:
                log.exception("Mail check failed")
                if e.__class__.__name__ == "RefreshError":
                    await self.send("🔑 Your Google login expired. On your computer run:\n"
                                    "<code>python main.py --login</code>  then restart me.")
                else:
                    await self.send(f"⚠️ Mail check failed: {esc(str(e)[:300])}")
                return 0

            if first_run and len(notices) > 5:
                await self._send_summary("✅ First read done!", notices)
            else:
                await self.send_notices(notices)
            return len(notices)

    async def job_mail(self, ctx):
        await self._run_mail_check()

    async def job_digest(self, ctx):
        text = reminders.morning_digest(self.a.store, self.s)
        if not self.planner:
            await self.send(text)
            return
        today = self.s.today()
        if not self.planner.blocks_on(today):
            await self.replan(today)
        text += "\n\n" + self.planner.format_day(today) + "\n\n😴 <b>How did you sleep?</b>"
        await self.send(text, [("😫 Bad", "sleep:bad"), ("🙂 OK", "sleep:ok"), ("💪 Great", "sleep:great")])

    async def job_replan(self, ctx):
        """22:00: close today, plan tomorrow (Calendar updated), send it."""
        today = self.s.today()
        done, missed = await asyncio.to_thread(self.planner.close_day, today, self._now_min())
        plan = await self.replan(today + timedelta(days=1))
        head = "🌙 <b>Tomorrow's plan</b> (made from what you did today)"
        if missed:
            head += f"\n   {missed} block(s) missed today; they're back in the plan."
        await self.send(head + "\n\n" + plan)

    async def job_blocks(self):
        """Every minute: 'time to start' and 'how did it go?' for plan blocks."""
        if not self.planner:
            return
        today, now = self.s.today(), self._now_min()
        st = self.a.store
        for b in self.planner.due_now(today, now):
            key = f"blk:{b.id}:{b.start}"
            if st.get_kv(key):
                continue
            st.set_kv(key, "1")
            text = f"⏰ <b>Now</b>: {esc(b.line())}"
            if b.detail:
                text += f"\n<i>{esc(b.detail)}</i>"
            await self.send_block_msg(b.id, text, [("▶️ Start", f"bstart:{b.id}"), ("⏰ Later", f"blater:{b.id}"),
                                                   ("⏭ Skip", f"bskip:{b.id}")])
        for b in self.planner.ending_now(today, now):
            key = f"bend:{b.id}"
            if st.get_kv(key):
                continue
            st.set_kv(key, "1")
            await self.send_block_msg(
                b.id, f"⏱ <b>How did it go?</b> {esc(b.line())}\n"
                      "<i>Tap below, or reply to this message in your own words (e.g. “only did MUX”).</i>",
                [("✅ Done", f"bdone:{b.id}"), ("🟡 Part of it", f"bpart:{b.id}"), ("❌ Didn't", f"bnot:{b.id}")])

    async def job_prep(self, ctx):
        text = reminders.prep_reminder(self.a.store, self.s)
        if text:
            await self.send(text)

    async def job_pending(self):
        """Every minute: auto-decline pending items whose time passed; nudge ones starting within 3 h."""
        for ev in await asyncio.to_thread(self.a.expire_pending):
            note = "⌛ <b>Expired</b>: its time passed, so I skipped it."
            for mid in filter(None, (self.a.store.get_kv(f"askmsg:{ev['id']}") or "").split(",")):
                try:
                    await self.app.bot.edit_message_reply_markup(chat_id=self.chat_id, message_id=int(mid),
                                                                 reply_markup=None)
                    await self.app.bot.send_message(chat_id=self.chat_id, text=note, parse_mode=ParseMode.HTML,
                                                    reply_to_message_id=int(mid))
                    break   # one note is enough
                except Exception as e:
                    log.info("Couldn't update old message %s: %s", mid, e)
            else:
                await self.send(f"⌛ Skipped (time passed): {esc(ev['title'])}")
        for ev in await asyncio.to_thread(self.a.pending_due_soon):
            n = self.a._ask(ev["id"])
            await self.send("⏰ <b>Still waiting for you</b> (starts soon):\n" + n.text, n.buttons)

    async def job_soon(self, ctx):
        try:
            await self.job_pending()
        except Exception:
            log.exception("Pending-items check failed")
        try:
            await self.job_blocks()
        except Exception:
            log.exception("Plan block reminders failed")
        for text in reminders.soon_reminders(self.a.store, self.s):
            await self.send(text)


# ==================== small helpers ====================
def parse_user_datetime(text: str, today: date) -> tuple[str | None, str | None]:
    """'24 Oct 14:00' -> ('2026-10-24', '14:00'); '24/10' -> ('2026-10-24', None)"""
    has_time = bool(re.search(r"\d{1,2}[:.]\d{2}|\d\s*(am|pm)\b", text, re.I))
    try:
        dt = dateparser.parse(text.replace(".", ":") if has_time else text,
                              dayfirst=True, default=datetime.combine(today, time()))
    except (ValueError, OverflowError):
        return None, None
    d = dt.date()
    if d < today and not re.search(r"\b20\d{2}\b", text):
        d = d.replace(year=d.year + 1)   # "5 Jan" in December means next year
    return d.isoformat(), (dt.strftime("%H:%M") if has_time else None)


def _markup(buttons):
    if not buttons:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton(lbl, callback_data=data) for lbl, data in buttons]])


def _markup_rows(rows):
    return InlineKeyboardMarkup([[InlineKeyboardButton(lbl, callback_data=data) for lbl, data in row]
                                 for row in rows])


def _chunks(text: str, size: int = 3900) -> list[str]:
    if len(text) <= size:
        return [text]
    out, cur = [], ""
    for block in text.split("\n\n"):
        if len(cur) + len(block) + 2 > size and cur:
            out.append(cur)
            cur = ""
        cur += ("\n\n" if cur else "") + block
    if cur:
        out.append(cur)
    return out


def _hhmm(value: str, tz) -> time:
    h, m = str(value).split(":")
    return time(int(h), int(m), tzinfo=tz)
