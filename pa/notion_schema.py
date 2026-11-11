"""The whole Notion workspace, written down as data.

`python main.py --setup-notion` reads this and builds (or repairs) the workspace.
Change a column or view here, run setup again: missing things are added, nothing of yours is deleted.

Look: Notion's own style, one lemon accent, colours per the style sheet
(DD blue, EM orange, EMT purple, ED green, M3 red, EDCC brown).
"""
from __future__ import annotations

# ---------- small helpers ----------
TITLE = {"title": {}}
TEXT = {"rich_text": {}}
CHECK = {"checkbox": {}}
DATE = {"date": {}}
URL = {"url": {}}


def num(fmt="number"):
    return {"number": {"format": fmt}}


def select(*opts):
    """opts: ("Name", "color")"""
    return {"select": {"options": [{"name": n, "color": c} for n, c in opts]}}


def status(*opts):
    """opts: ("Name", "color", "To-do" | "In progress" | "Complete")"""
    return {"status": {"options": [{"name": n, "color": c, "group": g} for n, c, g in opts]}}


COURSE_COLOR = {"DD": "blue", "EM": "orange", "EMT": "purple", "ED": "green", "M3": "red", "EDCC": "brown"}
# Notion's own flat covers (solid colours exist for these four only)
COVER = {"HQ": "https://www.notion.so/images/page-cover/solid_yellow.png",
         "DD": "https://www.notion.so/images/page-cover/solid_blue.png",
         "M3": "https://www.notion.so/images/page-cover/solid_red.png",
         "_": "https://www.notion.so/images/page-cover/solid_beige.png"}
HQ_ICON = (["lemon", "citrus", "sun"], "yellow", "🍋")

# ---------- the databases ----------
# key: (title, icon (native names to try, colour, emoji fallback), properties)
DATABASES = {
    "courses": {
        "title": "Courses",
        "icon": (["book", "book open", "library"], "blue", "📚"),
        "props": {
            "Name": TITLE,
            "Full name": TEXT,
            "Code": TEXT,
            "Takes attendance": CHECK,
            "Confidence": select(("1", "red"), ("2", "orange"), ("3", "yellow"), ("4", "green"), ("5", "green")),
            "Materials": TEXT,
            "Progress": TEXT,                 # ▓▓▓▓░░░░░░ 35%  (written by Sitruuna; charts need a paid plan)
            "Sitruuna ID": TEXT,
        },
    },
    "topics": {
        "title": "Topics",
        "icon": (["grid", "squares four", "puzzle"], "purple", "🧩"),
        "props": {
            "Topic": TITLE,
            "Status": status(("Not yet", "gray", "To-do"), ("Halfway", "yellow", "In progress"), ("Done", "green", "Complete")),
            "Parts done": TEXT,
            "Parts left": TEXT,
            "Lectures": TEXT,
            "Book sections": TEXT,
            "Practice": TEXT,
            "Done": CHECK,
            "Midsem syllabus": CHECK,
            "Behind": CHECK,
            "Minutes studied": num(),
            "Estimated minutes": num(),
            "Last studied": DATE,
            "Sitruuna ID": TEXT,
        },
    },
    "exams": {
        "title": "Exams & Deadlines",
        "icon": (["calendar", "calendar blank"], "red", "🗓️"),
        "props": {
            "Title": TITLE,
            "Type": select(("Quiz", "orange"), ("Midsem", "red"), ("Compre", "red"), ("Exam", "red"), ("Lab", "yellow"),
                           ("Deadline", "purple"), ("Extra class", "blue"), ("Event", "gray")),
            "When": DATE,
            "Session": select(("FN · morning", "blue"), ("AN · afternoon", "orange")),
            "Venue": TEXT,
            "Weight": num("percent"),
            "Status": status(("Upcoming", "gray", "To-do"), ("Waiting for ✓", "yellow", "In progress"),
                             ("Done", "green", "Complete"), ("Cancelled", "red", "Complete")),
            "Coming up": CHECK,
            "Readiness": TEXT,                # ▓▓░░░ 2 of 7 topics
            "Source": TEXT,
            "Sitruuna ID": TEXT,
        },
    },
    "sessions": {
        "title": "Study Sessions",
        "icon": (["clock", "timer", "stopwatch"], "orange", "⏱️"),
        "props": {
            "Title": TITLE,
            "When": DATE,
            "Kind": select(("Study", "orange"), ("Revise", "orange"), ("Past paper", "red"), ("Prep", "yellow"),
                           ("Workout", "green"), ("Reading", "brown"), ("DSA", "blue"), ("Project", "pink")),
            "Status": status(("Planned", "gray", "To-do"), ("Started", "yellow", "In progress"),
                             ("Partly done", "yellow", "In progress"), ("Done", "green", "Complete"),
                             ("Skipped", "red", "Complete"), ("Missed", "red", "Complete")),
            "What to do": TEXT,
            "Changed by you": CHECK,
            "Today": CHECK,
            "This week": CHECK,
            "Minutes": num(),
            "Sitruuna ID": TEXT,
        },
    },
    "tasks": {
        "title": "Tasks",
        "icon": (["checkmark square", "check square", "checkmark"], "green", "✅"),
        "props": {
            "Task": TITLE,
            "Due": DATE,
            "Priority": select(("High", "red"), ("Medium", "orange"), ("Low", "green")),
            "Area": select(("Academics", "blue"), ("Project", "pink"), ("Personal", "purple"), ("Club", "orange"), ("Admin", "gray")),
            "Status": status(("To do", "gray", "To-do"), ("Doing", "yellow", "In progress"), ("Done", "green", "Complete")),
            "Time needed (min)": num(),
            "Remind me": CHECK,
            "Added from": select(("Notion", "gray"), ("Telegram", "blue"), ("Email", "red")),
        },
    },
    "log": {
        "title": "Daily Log",
        "icon": (["notebook", "book closed", "journal", "document", "note", "pencil"], "brown", "📓"),
        "props": {
            "Day": TITLE,
            "Date": DATE,
            "Sleep": select(("Bad", "red"), ("OK", "blue"), ("Great", "green")),
            "Workout": CHECK,
            "Reading": CHECK,
            "DSA questions": num(),
            "Walk (min)": num(),
            "Mood": select(("Low", "gray"), ("Okay", "blue"), ("Good", "green")),
            "Plan kept": num("percent"),
            "Sitruuna read it": CHECK,
            "Sitruuna ID": TEXT,
        },
    },
    "weekly": {
        "title": "Weekly Reviews",
        "icon": (["arrow circle", "repeat", "arrows clockwise", "refresh", "rotate", "sync"], "blue", "🔁"),
        "props": {
            "Week": TITLE,
            "Starts": DATE,
            "Study hours": num(),
            "Plan kept": num("percent"),
            "Topics finished": num(),
            "Workouts": num(),
            "Reading days": num(),
            "DSA questions": num(),
        },
    },
    "projects": {
        "title": "Projects & Skills",
        "icon": (["sparkle", "sparkles", "star", "rocket", "lightning"], "pink", "🚀"),
        "props": {
            "Name": TITLE,
            "Type": select(("Project", "pink"), ("Skill", "purple")),
            "Status": status(("Idea", "gray", "To-do"), ("Active", "yellow", "In progress"), ("Done", "green", "Complete")),
            "Weekly target (min)": num(),
            "Next step": TEXT,
            "Link": URL,
        },
    },
    "reading": {
        "title": "Reading",
        "icon": (["book open", "open book", "bookmark", "book"], "green", "📖"),
        "props": {
            "Book": TITLE,
            "Author": TEXT,
            "Status": status(("Want to read", "gray", "To-do"), ("Reading", "yellow", "In progress"), ("Done", "green", "Complete")),
            "Started": DATE,
            "Finished": DATE,
            "Notes": TEXT,
        },
    },
}

# relations: (database, property, target database, name of the matching property on the target)
RELATIONS = [
    ("topics", "Course", "courses", "Topics"),
    ("exams", "Course", "courses", "Exams"),
    ("exams", "Syllabus", "topics", "Exams"),
    ("sessions", "Course", "courses", "Sessions"),
    ("sessions", "Topic", "topics", "Sessions"),
    ("tasks", "Course", "courses", "Tasks"),
]

# formulas added after the relations exist. A formula Notion refuses is skipped and reported, never fatal.
FORMULAS = [
    ("topics", "Progress",
     'if(prop("Estimated minutes") > 0, min(round(prop("Minutes studied") / (prop("Estimated minutes") * 0.6) * 100) / 100, 1), 0)'),
    ("exams", "Countdown",
     'if(empty(prop("When")), "", if(dateBetween(prop("When"), now(), "days") < 0, "past",'
     ' if(dateBetween(prop("When"), now(), "days") == 0, "today", "in " + format(dateBetween(prop("When"), now(), "days")) + " days")))'),
] 

# rollups: (database, property, through this link, the column to look at, how to sum it up)
# Notion refused formulas that filter linked pages, so "% done" is a rollup over each topic's Done tick.
ROLLUPS = [
    ("courses", "Syllabus done", "Topics", "Done", "percent_checked"),
    ("exams", "Ready", "Syllabus", "Done", "percent_checked"),
]

# ---------- views ----------
# Each: (database key, view name, view type, configuration builder, filter, sorts)
# Configuration builders get `pid(name)` -> property id. "table" default view already exists in a new database.


def views():
    return [
        ("courses", "Cards", "gallery", lambda pid: {"type": "gallery", "cover": {"type": "page_cover"}, "card_layout": "compact"}, None, None),
        ("topics", "Board", "board", lambda pid: {"type": "board", "group_by": {"type": "status", "property_id": pid("Status"),
                                                                                  "group_by": "option", "sort": {"type": "manual"}}},
         None, None),
        ("topics", "Behind", "table", None, {"property": "Behind", "checkbox": {"equals": True}},
         [{"property": "Midsem syllabus", "direction": "descending"}]),
        ("exams", "Upcoming", "table", None,
         {"and": [{"property": "Status", "status": {"does_not_equal": "Done"}},
                  {"property": "Status", "status": {"does_not_equal": "Cancelled"}}]},
         [{"property": "When", "direction": "ascending"}]),
        ("exams", "Calendar", "calendar", lambda pid: {"type": "calendar", "date_property_id": pid("When")}, None, None),
        ("sessions", "Calendar", "calendar", lambda pid: {"type": "calendar", "date_property_id": pid("When")}, None, None),
        ("sessions", "Today", "list", None, {"property": "Today", "checkbox": {"equals": True}},
         [{"property": "When", "direction": "ascending"}]),
        ("tasks", "Board", "board", lambda pid: {"type": "board", "group_by": {"type": "status", "property_id": pid("Status"),
                                                                                 "group_by": "option", "sort": {"type": "manual"}}},
         None, None),
        ("log", "Calendar", "calendar", lambda pid: {"type": "calendar", "date_property_id": pid("Date")}, None, None),
        ("projects", "Gallery", "gallery", lambda pid: {"type": "gallery", "cover": {"type": "page_cover"}}, None, None),
        ("reading", "Shelf", "gallery", lambda pid: {"type": "gallery", "cover": {"type": "page_cover"}}, None, None),
    ]


# HQ dashboard rows (indexes into hq_sections): side by side, at most 4 a row
HQ_ROWS = [[0, 1, 2], [3]]
# HQ sections that used charts (free Notion allows only one chart): removed, replaced by text bars in the banner
REMOVED_SECTIONS = ["Syllabus done", "Study this week"]
# Shortcut cards under the dashboard: (database key, label)
SHORTCUTS = [("log", "Daily Log"), ("courses", "Courses"), ("exams", "Calendar"), ("tasks", "Tasks"), ("weekly", "Weekly Reviews")]

# The HQ page, top to bottom: (heading, database key, view type, configuration builder, filter, sorts)
def hq_sections():
    return [
        ("Today's plan", "sessions", "list", None, {"property": "Today", "checkbox": {"equals": True}},
         [{"property": "When", "direction": "ascending"}]),
        ("Coming up · 14 days", "exams", "table", None, {"property": "Coming up", "checkbox": {"equals": True}},
         [{"property": "When", "direction": "ascending"}]),
        ("Open tasks", "tasks", "list", None, {"property": "Status", "status": {"does_not_equal": "Done"}},
         [{"property": "Due", "direction": "ascending"}]),
        ("Behind", "topics", "table", None, {"property": "Behind", "checkbox": {"equals": True}},
         [{"property": "Midsem syllabus", "direction": "descending"}]),
    ]
