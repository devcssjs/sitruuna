# 🍋 Sitruuna: a personal AI assistant for college

Sitruuna ("lemon" in Finnish) reads your college email, finds anything with a date
(exams, quizzes, deadlines, cancelled or extra classes), puts it on your timetable and
Google Calendar, plans your study time, and reminds you on **Telegram**. If you want,
it also keeps a **Notion** workspace up to date.

It runs on your own computer (**Windows or Mac**) and only you can talk to it.

**What it does**
- Reads your college Gmail every 30 minutes (read-only: it can never send or delete mail)
- Uses an AI (Google Gemini, free) to find dated items, even inside PDFs and images
- **Checks the AI's answer with code.** Clear items are added automatically with an **Undo** button.
  Unclear items ask you first with **Add / Edit / Ignore**
- Handles changes: a moved midsem replaces the old date, and a cancelled lecture is struck out
- Skips clubs, fests, placements and talks, and skips mail for other sections
- Sends a morning summary at 8 am and a "prep for tomorrow" at 7 pm, plus alerts 60 and 15 minutes before exams
- **Study planner:** daily study blocks based on your exams, your topics and how confident you feel,
  plus time for workout, reading and other routines
- **Just text it:** "only did MUX", "EM quiz moved to 18 Nov 5pm", "make tomorrow lighter"

> The example settings are for a BITS Pilani Goa student (course codes, sections, timetable).
> The app works for any college. See [Make it yours](#7-make-it-yours) for what to change.

---

## Contents
1. [What you need](#1-what-you-need)
2. [Download and install](#2-download-and-install)
3. [Get a free AI key (Gemini)](#3-get-a-free-ai-key-gemini)
4. [Connect Gmail and Google Calendar](#4-connect-gmail-and-google-calendar)
5. [Create your Telegram bot](#5-create-your-telegram-bot)
6. [First start](#6-first-start)
7. [Make it yours](#7-make-it-yours)
8. [Notion (optional)](#8-notion-optional)
9. [Keep it running](#9-keep-it-running)
10. [Daily use](#10-daily-use)
11. [Troubleshooting](#11-troubleshooting)
12. [Privacy and safety](#12-privacy-and-safety)

---

## 1. What you need
- A Windows or Mac computer that stays on while Sitruuna runs
- **Python 3.12 or newer** (tested on 3.13)
- A Google account for your email (a college Google Workspace account or normal Gmail)
- Telegram on your phone
- About 30–45 minutes for setup

---

## 2. Download and install

### Install Python
- **Windows:** download it from https://www.python.org/downloads/. When installing,
  **tick "Add python.exe to PATH"**.
- **Mac:** download it from https://www.python.org/downloads/ or, if you use Homebrew, run `brew install python`.

### Get the code
Click **Code → Download ZIP** on this page and unzip it, or run:
```
git clone https://github.com/<your-username>/sitruuna.git
```

### Open a terminal in the folder
- **Windows:** open the folder in File Explorer, click the address bar, type `cmd`, press Enter.
- **Mac:** open **Terminal**, type `cd ` (with a space), drag the folder into the window, press Enter.

### Install the libraries
| Windows | Mac |
|---|---|
| `python -m venv .venv` | `python3 -m venv .venv` |
| `.venv\Scripts\activate` | `source .venv/bin/activate` |
| `pip install -r requirements.txt` | `pip install -r requirements.txt` |

> **Every time you open a new terminal**, run the second line (activate) first.
> Otherwise you'll see `No module named 'yaml'`.

In the commands below, Mac users type `python3` where it says `python`. Once the venv is active, `python` also works.

### Make your own copies of the settings
| Windows | Mac |
|---|---|
| `copy .env.example .env` | `cp .env.example .env` |
| `copy config.example.yaml config.yaml` | `cp config.example.yaml config.yaml` |

`.env` holds your keys. `config.yaml` holds your settings. Neither is ever uploaded to git.

### Check that everything works (no keys needed)
```
python tests/test_pipeline.py
python tests/test_planner.py
python tests/test_notion.py
```
Each one should end with **ALL … TESTS PASSED**.

---

## 3. Get a free AI key (Gemini)
1. Go to https://aistudio.google.com/apikey and click **Create API key**.
2. Open `.env` in a text editor and paste the key after `GEMINI_API_KEY=`.

The free tier allows about 500 emails a day, which is plenty.
If you'd rather use Claude, see [Switching to Claude](#switching-to-claude).

---

## 4. Connect Gmail and Google Calendar
You create your own small "Google app" so Sitruuna can read your mail. It's free and stays private to you.

1. Go to https://console.cloud.google.com and create a project called **Sitruuna**.
2. Go to **APIs & Services → Library** and enable **Gmail API** and **Google Calendar API**.
3. Go to **Google Auth Platform** (the OAuth consent screen):
   - **Branding:** app name *Sitruuna*, plus your email as the support and developer email.
   - **Audience:** choose **External** and add your own email address(es) under **Test users**.
     *(If your college account owns the project, you may see **Internal** instead. That's fine and even simpler.)*
4. Go to **Clients → Create client → Desktop app**, then download the JSON file.
5. Rename it to **`credentials.json`** and put it in the Sitruuna folder.
6. Go to **Audience → Publish app**. If you skip this, Google makes you sign in again every 7 days.
   When you sign in you'll see *"Google hasn't verified this app"*. Click **Advanced → Go to Sitruuna**.
   It's your own app, so this is safe.
7. Sign in:
   ```
   python main.py --login
   ```
   A browser opens **twice**: first pick the account whose **email** to read, then the account whose **calendar** to use.

> **"Access blocked by your admin"?** Some colleges block outside apps on college accounts.
> In that case, forward your college mail to a personal Gmail (Gmail → Settings → Forwarding)
> and sign in with the personal account.

**See it on an iPhone:** Settings → Apps → Calendar → Calendar Accounts → Add Account → Google.
Sign in with the calendar account and turn on Calendars. **On a Mac:** System Settings →
Internet Accounts → Google. On Android, Google Calendar shows the events already.

---

## 5. Create your Telegram bot
1. In Telegram, message **@BotFather**, send `/newbot` and follow the steps. Copy the **token** it gives you.
2. Paste the token in `.env` after `TELEGRAM_BOT_TOKEN=`.
3. Start Sitruuna: `python main.py`
4. Open your new bot in Telegram and send `/start`. It replies with your **chat ID**.
5. Paste the chat ID in `.env` after `TELEGRAM_CHAT_ID=`.
6. Stop Sitruuna (**Ctrl+C**) and start it again.

The bot only answers **your** chat ID, so nobody else can use it.

---

## 6. First start
```
python main.py
```
On the first run, Sitruuna:
- reads the last 60 days of mail and sends you one summary,
- loads any dates you know from `data/known_dates.csv`,
- asks you to rate each course from 1 to 5, and to mark which topics you've done (`/status`),
- makes today's and tomorrow's plan.

Then send `/synctimetable` to put your weekly classes on Google Calendar.

**Check accuracy before you trust it:**
```
python tests/check_20.py
```
This reads your 20 newest emails and writes what it found to `data/check_20_results.txt`
(nothing is saved). Compare it with the real emails. If 18 or more are right, you're good to go.

---

## 7. Make it yours
Until you make your own copies, Sitruuna uses the example files (`*.example.*`).
Copy each one, drop `.example` from the name, and edit the copy. Your copies are never uploaded to git.

| File | What to put in it |
|---|---|
| `config.yaml` | Your name, time zone, courses, semester end date, which senders to read, reminder times, study windows, routines |
| `data/timetable.csv` | Your weekly classes |
| `data/topics.csv` | Topics per course, for the study planner |
| `data/known_dates.csv` | Exam and quiz dates you already know (optional) |

You can open the CSV files in Excel, Numbers or Google Sheets. **Save them as CSV (UTF-8)**.

### config.yaml: the important parts
- `your_name` – used in the morning greeting.
- `timezone` – for example `Asia/Kolkata`, `Europe/London` or `America/New_York`.
- `courses` – a short form for each course, with its official code and name:
  ```yaml
  courses:
    CALC: {code: "MATH 101", name: Calculus I}
    PHY:  {code: "PHYS 110", name: Physics}
  ```
  Any spelling of the code in an email matches ("MATH-101", "Math 101").
- `semester_end` – the last day of classes.
- `gmail.allowed_senders` – only read mail from these senders, e.g. your college domain (`myuni.edu`).
  This saves your free AI limit. Leave it empty to read everything.
- `my_lab_batch` – your lab batch, if your college uses batches. Otherwise leave it empty.
- `planner.course_order`, `taught_upto`, `last_lecture` – use your own course short forms here.
  Update `taught_upto` every couple of weeks.
- `planner.assume_midsem_done` / `not_done_yet` – on first start, count topics marked `midsem=yes` as done
  (except the ones listed). Set it to `false` if that doesn't fit you.
- `notion.materials_root` – optional. A folder on your computer with your course files, shown on each Notion course page.

### data/timetable.csv
```
day,start,end,course,kind,title,venue,must_attend
MO,09:00,10:00,CALC,lecture,L1,Room 101,yes
WE,14:00,16:00,PHY,lab,P2,Physics Lab,yes
```
- `day`: MO TU WE TH FR SA SU
- `kind`: lecture, tutorial or lab
- `title`: your section (L1, T2, P3…). Mail meant for another section is skipped.
- `must_attend`: `yes` gives a ⭐ and alerts. With `planner.skip_non_attendance_classes`, `no` classes count as free time.

### data/topics.csv
```
course,id,topic,lec_from,lec_to,refs,practice,midsem,parts
CALC,CALC01,Limits,1,3,"Stewart 2.1-2.4",Worksheet 1,yes,One-sided limits|Limit laws|Squeeze theorem
```
- `id`: any unique id. `lec_from` / `lec_to`: which lectures cover it (about 1 hour of study per lecture).
- `midsem`: `yes` if it was in the midsem syllabus. `parts`: sub-topics separated by `|` (optional).

### data/known_dates.csv (optional)
```
source,course,type,title,date,start,end,notes,confidence,syllabus
handout,CALC,compre,CALC Final,2026-12-10,09:00,12:00,closed book,high,
told,PHY,quiz,PHY Quiz 2,2026-11-20,,,,high,PHY03|PHY04
```
- `source`: `told` (you were told it; it wins over handouts) or `handout`.
- `type`: quiz, midsem, compre, exam, lab, deadline, class or event.
- `confidence`: `high` adds it straight away, `low` asks you first.
- `syllabus`: optional topic ids from topics.csv, separated by `|`.

### The AI instructions (`prompts/`)
`prompts/extract.txt` tells the AI what to look for. It mentions BITS terms (midsem, compre, sections, Quanta).
Most colleges don't need changes, but you can edit the wording to match your college's terms.

---

## 8. Notion (optional)
Sitruuna can build and keep up to date a Notion workspace with 9 databases: Courses, Topics,
Exams & Deadlines, Study Sessions, Tasks, Daily Log, Weekly Reviews, Projects & Skills, and Reading.
It also keeps a home page ("HQ") with today's plan, upcoming exams and progress bars.
The free Notion plan is enough.

1. In Notion, create an empty page called **Sitruuna HQ**.
2. Create a connection (Notion also calls these "integrations"):
   - Open https://www.notion.so/profile/integrations and click **New integration** (or **Create a new connection**).
   - Name it **Sitruuna**, pick your workspace, and choose **Internal**.
   - Under **Capabilities / Configuration**, allow **Read content**, **Update content** and **Insert content**.
   - Copy the **Internal Integration Secret** (the token).
3. Give the connection access to your page. On **Sitruuna HQ**, click **••• → Connections → + Add connection → Sitruuna**.
4. Copy the page link (**Share → Copy link**).
5. In `.env`, add:
   ```
   NOTION_TOKEN=paste-the-token-here
   NOTION_HQ_PAGE=paste-the-page-link-here
   ```
6. Test the connection:
   ```
   python main.py --check-notion
   ```
7. Build the workspace. This takes about 5 minutes the first time, and it's safe to run again:
   ```
   python main.py --setup-notion
   ```
8. Restart Sitruuna. From then on, Notion updates by itself every 2 minutes.
   In Telegram, `/notion` sends the link and `/sync` updates it right away.

**Good to know**
- Sitruuna only writes to Notion for now (Sitruuna → Notion). Edits you make in Notion aren't read back yet.
- The Tasks, Weekly Reviews, Projects and Reading databases are there for you to fill in yourself.
- `notion.hq_layout`: `stacked` (default, works on the free plan) or `dashboard` (side-by-side layout, needs a paid plan).
  Run `--setup-notion` again after changing it.
- Notion's API can refuse some icons, views or formulas. Setup skips those and tells you. Nothing is ever deleted.
- **Never share your Notion token.**

---

## 9. Keep it running
Sitruuna only works while your computer is on and it is running.

### Windows
- **Start:** double-click **`start_assistant.bat`**. It restarts Sitruuna after `/restart` or a crash.
  After 5 crashes in a row it stops and tells you on Telegram. To stop it, close the window.
- **Start automatically when you log in:** open **Task Scheduler → Create Basic Task → When I log on →
  Start a program** and pick `start_assistant.bat`.
- **Stop the laptop sleeping while it's plugged in:** Settings → System → Power → Screen and sleep → *When plugged in, put my device to sleep after* → **Never**.

### Mac
- **Start:** in Terminal, inside the folder, run:
  ```
  ./start_assistant.sh
  ```
  It restarts Sitruuna after `/restart` or a crash, just like the Windows version, and it keeps the Mac
  awake while running (it uses the built-in `caffeinate` command). Press **Ctrl+C** to stop.
  If you see "permission denied", run `chmod +x start_assistant.sh` once.
- **Start with a double-click:** make a copy called `start_assistant.command`:
  ```
  cp start_assistant.sh start_assistant.command
  ```
  Double-clicking the `.command` file opens Terminal and starts Sitruuna.
- **Start automatically when you log in:** System Settings → General → **Login Items** → **+** → pick `start_assistant.command`.
- A MacBook still sleeps when you **close the lid** (unless it's plugged into an external display). Keep it open, or plugged in with a display.

Changes to code or `config.yaml` take effect only after a restart (send `/restart` in Telegram).

---

## 10. Daily use

### Commands
| Command | What you get |
|---|---|
| `/timetable` (or `/today`) | Today's classes and items; cancelled ones are struck out |
| `/timetable tomorrow` · `/timetable 24 oct` | Another day |
| `/week` | The next 7 days |
| `/deadlines` | Every upcoming exam and deadline, soonest first |
| `/pending` | Items waiting for your Add / Ignore |
| `/check` | Check mail now (normally every 30 minutes) |
| `/rescan` · `/rescan 30` | Re-read the last 60 (or 30) days of mail with the current rules. Uses AI quota |
| `/synctimetable` | Put weekly classes on Google Calendar (again) |
| `/cleanup` | Remove leftover items with no course (shows the list first) |
| `/restart` | Restart Sitruuna from your phone |
| `/plan` · `/plan tomorrow` · `/plan week` | Your study and routine plan |
| `/replan` | Re-plan the rest of today |
| `/rate` | How confident you feel per course (1–5). Weaker courses get more time |
| `/status` | Mark each topic ✅ done / 🟡 halfway / ❌ not yet |
| `/progress` | Syllabus covered and this week's study time |
| `/notion` · `/sync` | Notion link · update Notion now |

### Just write to it
- **Progress:** "only did MUX", "finished chapter 3", "went to the gym"
- **Date changes:** "Physics quiz 2 moved to 18 Nov 5pm" (it asks ✅ first)
- **Plan changes:** "replace workout with calculus", "move reading to 11pm", "make tomorrow lighter"
- **Questions:** "what's my plan tomorrow?"
- "yes" / "no" answer its last question. Everything has an ↩️ Undo button.

### Each day
- **8 am:** morning summary and plan, plus "How did you sleep?" (a bad night means a lighter day).
- **At each study block:** Start / Later / Skip. When it ends: Done / Part of it / Didn't.
- **7 pm:** prep for tomorrow. **10 pm:** tomorrow's plan, based on what you actually did.

### Command-line options
| Command | What it does |
|---|---|
| `python main.py` | Start Sitruuna |
| `python main.py --login` | Sign in to Google again |
| `python main.py --check-once` | Read mail once and print the results (no Telegram) |
| `python main.py --rescan 10` | Re-read 10 days of mail and print the results |
| `python main.py --check-notion` | Test the Notion connection |
| `python main.py --setup-notion` | Build or repair the Notion workspace |

### Switching to Claude
In `.env`, set `LLM_PROVIDER=claude` and fill in `ANTHROPIC_API_KEY` (from https://platform.claude.com).
Restart. Nothing else changes. Claude is paid (roughly $0.10 a month for one student's mail), and
it doesn't use your mail to train its models.

---

## 11. Troubleshooting
| Problem | Fix |
|---|---|
| `No module named 'yaml'` | Activate the venv first (step 2), then run again |
| `python` not found (Mac) | Use `python3` |
| `python` not found (Windows) | Reinstall Python and tick **Add python.exe to PATH** |
| `credentials.json not found` | Redo step 4 and put the file in the Sitruuna folder |
| "Google login expired" | `python main.py --login`, then restart. If it happens every week, do step 4.6 (Publish app) |
| Gemini says the model doesn't exist | Pick a current model at https://ai.google.dev/gemini-api/docs/models and set `ai.gemini_model` in `config.yaml` |
| Gemini "429" / daily limit | It pauses and carries on after the limit resets. Raise `ai.seconds_between_calls` if it keeps happening |
| An email was missed | `/rescan 10`. Check that the sender matches `gmail.allowed_senders` |
| A wrong item was added | Tap ↩️ Undo, or ✏️ Edit |
| `./start_assistant.sh: Permission denied` (Mac) | `chmod +x start_assistant.sh` |
| Notion check fails or says the page is not found | Add the Sitruuna connection to the HQ page (section 8, step 3) and check the link in `NOTION_HQ_PAGE` |
| Start completely fresh | Stop Sitruuna, delete `data/assistant.db`, start again |

Logs are in `data/logs/assistant.log`.

---

## 12. Privacy and safety
- Gmail access is **read-only**, and Calendar access covers **events only**.
- Your keys and logins (`.env`, `credentials.json`, `token_*.json`) and your data (`data/assistant.db`, logs)
  stay on your computer. `.gitignore` keeps them, and your personal copies of the settings, out of git.
  **Never upload or share them.**
- On Gemini's **free** tier, Google may use the content it reads to improve its products, and human reviewers may see it.
  If your mail is sensitive, use a paid Gemini key or switch to Claude.
- The Telegram bot only answers your own chat ID.

---

## Project layout
```
main.py                     start here
config.example.yaml         example settings (copy to config.yaml)
.env.example                example keys file (copy to .env)
start_assistant.bat         Windows launcher (auto-restart)
start_assistant.sh          Mac/Linux launcher (auto-restart, keeps the Mac awake)
data/*.example.csv          example timetable, topics and known dates
pa/pipeline.py              the "brain": reads mail, decides, stores
pa/telegram_bot.py          Telegram commands, buttons and scheduled jobs
pa/planner.py               the study planner
pa/notion_*.py              Notion builder and sync
pa/validate.py              the checks on every AI answer
prompts/                    the instructions the AI follows
tests/                      offline tests + check_20.py (accuracy check on your real mail)
```

## Contributing
Ideas and fixes are welcome. Open an issue or a pull request. Please run the three test files before sending changes.

## Licence
MIT. You can use, change and share this freely. See [LICENSE](LICENSE).
