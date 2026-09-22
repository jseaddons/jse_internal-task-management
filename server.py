#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Daily task-entry web form for the team — writes straight into tasklog.db.

No dependencies (Python standard library only). Run it on ONE machine; teammates
open the printed URL in a browser. Dropdowns are served live from the database,
so `add_project.py` instantly updates everyone's choices.

    python server.py                       # serves http://<this-pc>:8000
    python server.py --db tasklog.db --port 8000

IMPORTANT: keep the .db file on the host machine's LOCAL disk, not the network
share — SQLite with several simultaneous writers over a share can corrupt.
Reports still export to the shared drive via export_reports.py.
"""
import os, sys, json, sqlite3, argparse, datetime as dt, html, socket, tempfile, unicodedata, re
import threading, time, importlib
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse, quote
import forecast as forecast_app
from export_reports import (
    daily_fill_status, next_working_day, on_team_that_day, project_team_people,
    range_fill_status, with_managers_excluded,
)
from bulk_import import parse_level_name, upsert_project_level

_forecast_mtime = None

def _reload_forecast_if_changed():
    """Pick up forecast.py edits without restarting the whole server."""
    global forecast_app, _forecast_mtime
    path = getattr(forecast_app, "__file__", None)
    if not path:
        return
    try:
        m = os.path.getmtime(path)
    except OSError:
        return
    if _forecast_mtime is None:
        _forecast_mtime = m
        return
    if m != _forecast_mtime:
        forecast_app = importlib.reload(forecast_app)
        _forecast_mtime = m

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "tasklog.db")
# A company-wide tool, so the shortcut lives in a project-neutral share.
# It used to sit in the 24139 project folder, which was wrong: people on
# other projects have no reason - or rights - to look there.
PUBLISH_DIR = r"\\192.168.0.7\Timesheet"

# The Task level is a fixed three-item list. Anything more specific than these
# is a Sub Task, scoped to the Category it belongs to.
CORE_TASKS = ("REVIEW COMMENTS", "CHECK MODEL HEALTH", "CHECK UPDATES")
REVIEW_TASK = "REVIEW COMMENTS"


def _fold_name(name):
    """Uppercase, collapse space, strip accents so FACADE and FAÇADE match."""
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", str(name))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return " ".join(s.upper().split())


def _compact_id(value):
    return re.sub(r"[^A-Za-z0-9]", "", value or "").upper()


SIGNIN_NAME_ALIASES = {
    "VENKATAKRISHNAN": "VENKATAKRISHNA",
    "VENKATKRISHNAN": "VENKATAKRISHNA",
    "VENKATKRISHNA": "VENKATAKRISHNA",
    "CHANDRASEKAR": "CHANDRASEKHAR",
    "CHANDRASEKARAN": "CHANDRASEKHAR",
}


def find_signin_person(token):
    """Emp ID, then employee name (spaces and common spelling variants allowed)."""
    token = (token or "").strip()
    if not token:
        return None
    con = connect()
    row = con.execute(
        "SELECT id FROM people WHERE emp_code=? COLLATE NOCASE", (token,)).fetchone()
    if row:
        con.close()
        return row
    compact = _compact_id(token)
    if compact:
        for r in con.execute("SELECT id, emp_code FROM people WHERE emp_code IS NOT NULL"):
            if _compact_id(r["emp_code"]) == compact:
                con.close()
                return r
    row = con.execute(
        "SELECT id FROM people WHERE name=? COLLATE NOCASE", (token,)).fetchone()
    if row:
        con.close()
        return row
    want = SIGNIN_NAME_ALIASES.get(compact, compact)
    hits = []
    for r in con.execute("SELECT id, name FROM people"):
        if _compact_id(r["name"]) == want:
            hits.append(r)
    con.close()
    return hits[0] if len(hits) == 1 else None


def _table_exists(con, name):
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _merge_category(con, keep_id, drop_id):
    """Move every use of drop_id onto keep_id, then delete the extra category."""
    if keep_id == drop_id:
        return
    for row in con.execute(
        "SELECT master_task_id, task_name_id FROM category_tasks WHERE category_id=?",
        (drop_id,),
    ):
        con.execute(
            "INSERT OR IGNORE INTO category_tasks(master_task_id, category_id, task_name_id) "
            "VALUES (?,?,?)",
            (row["master_task_id"], keep_id, row["task_name_id"]),
        )
    con.execute("DELETE FROM category_tasks WHERE category_id=?", (drop_id,))

    for row in con.execute(
        "SELECT id, task_name_id, name FROM task_sub_tasks WHERE category_id=?",
        (drop_id,),
    ):
        exists = con.execute(
            "SELECT id FROM task_sub_tasks WHERE task_name_id=? AND name=? AND id!=?",
            (row["task_name_id"], row["name"], row["id"]),
        ).fetchone()
        if exists:
            con.execute("UPDATE tasks SET task_sub_task_id=? WHERE task_sub_task_id=?",
                        (exists["id"], row["id"]))
            con.execute("UPDATE task_sub_task_links SET task_sub_task_id=? WHERE task_sub_task_id=?",
                        (exists["id"], row["id"]))
            if _table_exists(con, "forecast_assignments"):
                con.execute(
                    "UPDATE forecast_assignments SET task_sub_task_id=? WHERE task_sub_task_id=?",
                    (exists["id"], row["id"]))
            con.execute("DELETE FROM task_sub_tasks WHERE id=?", (row["id"],))
        else:
            con.execute("UPDATE task_sub_tasks SET category_id=? WHERE id=?",
                        (keep_id, row["id"]))

    con.execute("UPDATE tasks SET category_id=? WHERE category_id=?", (keep_id, drop_id))
    con.execute(
        "INSERT OR IGNORE INTO task_categories(task_id, category_id) "
        "SELECT task_id, ? FROM task_categories WHERE category_id=?",
        (keep_id, drop_id),
    )
    con.execute("DELETE FROM task_categories WHERE category_id=?", (drop_id,))
    if _table_exists(con, "forecast_assignments"):
        con.execute("UPDATE forecast_assignments SET category_id=? WHERE category_id=?",
                    (keep_id, drop_id))
    con.execute("DELETE FROM categories WHERE id=?", (drop_id,))


def _cleanup_catalog(con):
    """Fix list mistakes that show in both Daily Task and Forecast."""
    groups = {}
    for r in con.execute("SELECT id, name FROM categories"):
        key = _fold_name(r["name"])
        if not key:
            continue
        groups.setdefault(key, []).append((r["id"], r["name"]))
    for key, rows in groups.items():
        if len(rows) < 2:
            continue
        keep = next((rid for rid, n in rows if n == key), None)
        if keep is None:
            keep = min(rid for rid, n in rows)
        for rid, _name in rows:
            if rid != keep:
                _merge_category(con, keep, rid)
        con.execute("UPDATE categories SET name=? WHERE id=?", (key, keep))

    for r in list(con.execute("SELECT id, project_code, name FROM buildings")):
        if _fold_name(r["name"]) != "MAIN HOSPITAL":
            continue
        all_row = con.execute(
            "SELECT id FROM buildings WHERE project_code=? AND is_all=1",
            (r["project_code"],),
        ).fetchone()
        dest = all_row["id"] if all_row else None
        con.execute("UPDATE tasks SET building_id=? WHERE building_id=?", (dest, r["id"]))
        if _table_exists(con, "forecast_assignments"):
            con.execute("UPDATE forecast_assignments SET building_id=? WHERE building_id=?",
                        (dest, r["id"]))
        con.execute("DELETE FROM buildings WHERE id=?", (r["id"],))

    for r in list(con.execute("SELECT id, project_code, name FROM buildings")):
        if (r["name"] or "").strip():
            continue
        all_row = con.execute(
            "SELECT id FROM buildings WHERE project_code=? AND is_all=1",
            (r["project_code"],),
        ).fetchone()
        dest = all_row["id"] if all_row else None
        con.execute("UPDATE tasks SET building_id=? WHERE building_id=?", (dest, r["id"]))
        if _table_exists(con, "forecast_assignments"):
            con.execute("UPDATE forecast_assignments SET building_id=? WHERE building_id=?",
                        (dest, r["id"]))
        con.execute("DELETE FROM buildings WHERE id=?", (r["id"],))


def migrate_levels_to_projects(con):
    """Each level belongs to one project. Same label on two projects is two rows.

    A new project starts with no levels. Adding F06 on 24139 must not appear on
    5.2 or on the next project.
    """
    con.execute("DROP TABLE IF EXISTS levels_by_project")
    con.execute("DROP TABLE IF EXISTS _level_id_map")
    if not _table_exists(con, "levels"):
        con.execute("""
            CREATE TABLE levels (
                id INTEGER PRIMARY KEY,
                project_code TEXT NOT NULL REFERENCES projects(code) ON DELETE CASCADE,
                number INTEGER,
                label TEXT NOT NULL,
                is_all INTEGER NOT NULL DEFAULT 0,
                UNIQUE(project_code, label)
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS ix_levels_proj ON levels(project_code)")
        return
    cols = {r[1] for r in con.execute("PRAGMA table_info(levels)")}
    if "project_code" in cols:
        con.execute("CREATE INDEX IF NOT EXISTS ix_levels_proj ON levels(project_code)")
        con.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_levels_proj_label "
            "ON levels(project_code, label)")
        return

    seen = set()
    pairs = []

    def add_pair(project, old_id, number, label, is_all):
        if not project or old_id is None or not label:
            return
        key = (project, old_id)
        if key in seen:
            return
        seen.add(key)
        pairs.append((project, old_id, number, label, is_all or 0))

    if _table_exists(con, "project_levels"):
        for r in con.execute("""
            SELECT pl.project_code, l.id, l.number, l.label, l.is_all
              FROM project_levels pl JOIN levels l ON l.id = pl.level_id
        """):
            add_pair(r[0], r[1], r[2], r[3], r[4])
    for r in con.execute("""
        SELECT t.project_code, l.id, l.number, l.label, l.is_all
          FROM tasks t JOIN levels l ON l.id = t.level_id
         WHERE t.project_code IS NOT NULL AND t.level_id IS NOT NULL
    """):
        add_pair(r[0], r[1], r[2], r[3], r[4])
    for table in ("forecast_assignments", "forecast_subtask_budget"):
        if not _table_exists(con, table):
            continue
        for r in con.execute(
            f"SELECT f.project_code, l.id, l.number, l.label, l.is_all "
            f"FROM {table} f JOIN levels l ON l.id = f.level_id "
            f"WHERE f.project_code IS NOT NULL AND f.level_id IS NOT NULL"
        ):
            add_pair(r[0], r[1], r[2], r[3], r[4])
    if _table_exists(con, "lead_scope"):
        for r in con.execute("""
            SELECT s.project_code, l.id, l.number, l.label, l.is_all
              FROM lead_scope s JOIN levels l ON l.id = s.item_id
             WHERE s.kind='level'
        """):
            add_pair(r[0], r[1], r[2], r[3], r[4])

    con.commit()
    views = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='view'")]
    for name in views:
        con.execute('DROP VIEW IF EXISTS "%s"' % name.replace('"', '""'))
    con.execute("PRAGMA foreign_keys = OFF")
    con.execute("""
        CREATE TABLE levels_by_project (
            id INTEGER PRIMARY KEY,
            project_code TEXT NOT NULL,
            number INTEGER,
            label TEXT NOT NULL,
            is_all INTEGER NOT NULL DEFAULT 0,
            UNIQUE(project_code, label)
        )
    """)
    old_to_new = {}
    for project, old_id, number, label, is_all in pairs:
        con.execute(
            "INSERT OR IGNORE INTO levels_by_project"
            "(project_code,number,label,is_all) VALUES (?,?,?,?)",
            (project, number, label, is_all))
        new_id = con.execute(
            "SELECT id FROM levels_by_project WHERE project_code=? AND label=?",
            (project, label)).fetchone()[0]
        old_to_new[(project, old_id)] = new_id

    for (project, old_id), new_id in old_to_new.items():
        if old_id == new_id:
            continue
        con.execute(
            "UPDATE tasks SET level_id=? WHERE project_code=? AND level_id=?",
            (new_id, project, old_id))
        if _table_exists(con, "forecast_assignments"):
            con.execute(
                "UPDATE forecast_assignments SET level_id=? "
                "WHERE project_code=? AND level_id=?",
                (new_id, project, old_id))
        if _table_exists(con, "forecast_subtask_budget"):
            con.execute(
                "UPDATE forecast_subtask_budget SET level_id=? "
                "WHERE project_code=? AND level_id=?",
                (new_id, project, old_id))
        con.execute(
            "UPDATE lead_scope SET item_id=? "
            "WHERE project_code=? AND kind='level' AND item_id=?",
            (new_id, project, old_id))

    new_ids = [n for n in old_to_new.values()]
    if new_ids:
        q = ",".join("?" * len(new_ids))
        con.execute(f"UPDATE tasks SET level_id=NULL WHERE level_id IS NOT NULL AND level_id NOT IN ({q})", new_ids)
        if _table_exists(con, "forecast_assignments"):
            con.execute(
                f"UPDATE forecast_assignments SET level_id=NULL "
                f"WHERE level_id IS NOT NULL AND level_id NOT IN ({q})", new_ids)
        if _table_exists(con, "forecast_subtask_budget"):
            con.execute(
                f"UPDATE forecast_subtask_budget SET level_id=NULL "
                f"WHERE level_id IS NOT NULL AND level_id NOT IN ({q})", new_ids)
        con.execute(
            f"DELETE FROM lead_scope WHERE kind='level' "
            f"AND item_id NOT IN ({q})", new_ids)

    if _table_exists(con, "project_levels"):
        con.execute("DELETE FROM project_levels")
        con.execute(
            "INSERT INTO project_levels(project_code, level_id) "
            "SELECT project_code, id FROM levels_by_project")
    con.execute("DROP TABLE levels")
    con.execute("ALTER TABLE levels_by_project RENAME TO levels")
    con.execute("CREATE INDEX IF NOT EXISTS ix_levels_proj ON levels(project_code)")
    con.commit()
    con.execute("PRAGMA foreign_keys = ON")


def ensure_project_levels(con):
    """If a project never got a level list, copy whatever its tasks already use.

    Never copies another project's catalog onto an empty project.
    """
    if not _table_exists(con, "project_levels"):
        return
    for row in con.execute("SELECT code FROM projects"):
        code = row["code"]
        n = con.execute(
            "SELECT COUNT(*) FROM project_levels WHERE project_code=?", (code,)
        ).fetchone()[0]
        if n:
            continue
        for lid in con.execute(
            "SELECT DISTINCT level_id FROM tasks "
            "WHERE project_code=? AND level_id IS NOT NULL", (code,),
        ):
            con.execute(
                "INSERT OR IGNORE INTO project_levels(project_code,level_id) VALUES (?,?)",
                (code, lid[0]))


def remap_level_series_to_f(con, project_code):
    """Level 1 → F01, Level 2 → F02, … on this project only.

    Other projects keep the Level 1 / Level 2 names. Logged rows on this
    project are moved onto the matching F / B label so reports stay correct.
    """
    if not project_code or not _table_exists(con, "levels"):
        return
    cols = {r[1] for r in con.execute("PRAGMA table_info(levels)")}
    if "project_code" not in cols:
        return
    old_rows = list(con.execute(
        "SELECT id, number, label FROM levels "
        "WHERE project_code=? AND is_all=0 AND label LIKE 'Level %'",
        (project_code,),
    ))
    for old in old_rows:
        num = old["number"]
        if num is None:
            m = re.search(r"-?\d+", old["label"] or "")
            if not m:
                continue
            num = int(m.group())
        dest_label = ("B%02d" % abs(num)) if num < 0 else ("F%02d" % num)
        dest_id = upsert_project_level(con, project_code, dest_label, num, 0)
        old_id = old["id"]
        if not dest_id or old_id == dest_id:
            continue
        con.execute(
            "UPDATE tasks SET level_id=? WHERE project_code=? AND level_id=?",
            (dest_id, project_code, old_id))
        if _table_exists(con, "forecast_assignments"):
            con.execute(
                "UPDATE forecast_assignments SET level_id=? "
                "WHERE project_code=? AND level_id=?",
                (dest_id, project_code, old_id))
        if _table_exists(con, "forecast_subtask_budget"):
            con.execute(
                "UPDATE forecast_subtask_budget SET level_id=? "
                "WHERE project_code=? AND level_id=?",
                (dest_id, project_code, old_id))
        con.execute(
            "UPDATE lead_scope SET item_id=? "
            "WHERE project_code=? AND kind='level' AND item_id=?",
            (dest_id, project_code, old_id))
        con.execute(
            "DELETE FROM project_levels WHERE project_code=? AND level_id=?",
            (project_code, old_id))
        unused = con.execute(
            "SELECT 1 FROM tasks WHERE level_id=?", (old_id,)).fetchone()
        if not unused:
            con.execute("DELETE FROM levels WHERE id=? AND project_code=?",
                        (old_id, project_code))


def ensure_hierarchy_schema():
        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row
        migrate_levels_to_projects(con)
        con.execute("PRAGMA foreign_keys = ON")
        con.executescript("""
        CREATE TABLE IF NOT EXISTS categories (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
        CREATE TABLE IF NOT EXISTS sub_tasks (
            id INTEGER PRIMARY KEY, master_task_id INTEGER NOT NULL REFERENCES master_tasks(id) ON DELETE CASCADE,
            name TEXT NOT NULL, UNIQUE(master_task_id, name));
        CREATE TABLE IF NOT EXISTS sub_sub_tasks (
            id INTEGER PRIMARY KEY, sub_task_id INTEGER NOT NULL REFERENCES sub_tasks(id) ON DELETE CASCADE,
            name TEXT NOT NULL, UNIQUE(sub_task_id, name));
        CREATE TABLE IF NOT EXISTS task_sub_tasks (
            id INTEGER PRIMARY KEY, task_name_id INTEGER NOT NULL REFERENCES task_names(id) ON DELETE CASCADE,
            category_id INTEGER REFERENCES categories(id) ON DELETE CASCADE,
            name TEXT NOT NULL, UNIQUE(task_name_id, name));
        CREATE TABLE IF NOT EXISTS category_tasks (
            master_task_id INTEGER NOT NULL REFERENCES master_tasks(id) ON DELETE CASCADE,
            category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
            task_name_id INTEGER NOT NULL REFERENCES task_names(id) ON DELETE CASCADE,
            PRIMARY KEY(master_task_id, category_id, task_name_id));
        CREATE TABLE IF NOT EXISTS project_levels (
            project_code TEXT NOT NULL REFERENCES projects(code) ON DELETE CASCADE,
            level_id INTEGER NOT NULL REFERENCES levels(id) ON DELETE CASCADE,
            PRIMARY KEY(project_code, level_id));
        CREATE TABLE IF NOT EXISTS master_task_tasks (
            master_task_id INTEGER NOT NULL REFERENCES master_tasks(id) ON DELETE CASCADE,
            task_name_id INTEGER NOT NULL REFERENCES task_names(id) ON DELETE CASCADE,
            PRIMARY KEY(master_task_id, task_name_id));
        CREATE TABLE IF NOT EXISTS project_people (
            id INTEGER PRIMARY KEY,
            project_code TEXT NOT NULL REFERENCES projects(code) ON DELETE CASCADE,
            person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
            role_id INTEGER REFERENCES roles(id),
            joined_on TEXT NOT NULL,
            left_on TEXT);
        CREATE TABLE IF NOT EXISTS lead_scope (
            project_code TEXT NOT NULL REFERENCES projects(code) ON DELETE CASCADE,
            person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
            kind TEXT NOT NULL CHECK (kind IN ('zone','level','category')),
            item_id INTEGER NOT NULL,
            PRIMARY KEY(project_code, person_id, kind, item_id));
        CREATE TABLE IF NOT EXISTS lead_support (
            project_code TEXT NOT NULL REFERENCES projects(code) ON DELETE CASCADE,
            lead_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
            support_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
            PRIMARY KEY(project_code, lead_id, support_id),
            UNIQUE(project_code, support_id));
        CREATE TABLE IF NOT EXISTS task_categories (
            task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
            PRIMARY KEY(task_id, category_id));
        CREATE TABLE IF NOT EXISTS task_sub_task_links (
            task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            task_sub_task_id INTEGER NOT NULL REFERENCES task_sub_tasks(id) ON DELETE CASCADE,
            PRIMARY KEY(task_id, task_sub_task_id));
        """)
        cols = {r[1] for r in con.execute("PRAGMA table_info(tasks)")}
        project_cols = {r[1] for r in con.execute("PRAGMA table_info(projects)")}
        if "no_sheet_type" not in project_cols:
            con.execute("ALTER TABLE projects ADD COLUMN no_sheet_type INTEGER NOT NULL DEFAULT 0")
        if "project_status" not in project_cols:
            con.execute("ALTER TABLE projects ADD COLUMN project_status TEXT NOT NULL DEFAULT 'Active'")
        if "no_zone" not in project_cols:
            con.execute("ALTER TABLE projects ADD COLUMN no_zone INTEGER NOT NULL DEFAULT 0")
        if "use_teams" not in project_cols:
            con.execute("ALTER TABLE projects ADD COLUMN use_teams INTEGER NOT NULL DEFAULT 0")
        if "sub_task_id" not in cols:
                con.execute("ALTER TABLE tasks ADD COLUMN sub_task_id INTEGER REFERENCES sub_tasks(id)")
        if "sub_sub_task_id" not in cols:
                con.execute("ALTER TABLE tasks ADD COLUMN sub_sub_task_id INTEGER REFERENCES sub_sub_tasks(id)")
        if "task_sub_task_id" not in cols:
            con.execute("ALTER TABLE tasks ADD COLUMN task_sub_task_id INTEGER REFERENCES task_sub_tasks(id)")
        if "category_id" not in cols:
            con.execute("ALTER TABLE tasks ADD COLUMN category_id INTEGER REFERENCES categories(id)")
        if "entered_by" not in cols:
            con.execute("ALTER TABLE tasks ADD COLUMN entered_by INTEGER REFERENCES people(id)")
            con.execute("CREATE INDEX IF NOT EXISTS ix_tasks_entered_by ON tasks(entered_by)")
        pp_cols = {r[1] for r in con.execute("PRAGMA table_info(project_people)")}
        if "role_id" not in pp_cols:
            con.execute("ALTER TABLE project_people ADD COLUMN role_id INTEGER REFERENCES roles(id)")
        con.execute(
            """
            UPDATE project_people
               SET role_id = (SELECT role_id FROM people WHERE people.id = project_people.person_id)
             WHERE role_id IS NULL
            """)
        ensure_project_people_history(con)
        people_cols = {r[1] for r in con.execute("PRAGMA table_info(people)")}
        if "skill_rating" not in people_cols:
            con.execute("ALTER TABLE people ADD COLUMN skill_rating INTEGER")
        if "experience_years" not in people_cols:
            con.execute("ALTER TABLE people ADD COLUMN experience_years REAL")
        if "location" not in people_cols:
            con.execute("ALTER TABLE people ADD COLUMN location TEXT")
        sub_cols = {r[1] for r in con.execute("PRAGMA table_info(task_sub_tasks)")}
        if "category_id" not in sub_cols:
            con.execute("ALTER TABLE task_sub_tasks ADD COLUMN category_id INTEGER REFERENCES categories(id)")
        for category in ("WALL", "FLOOR", "CEILING", "SIGNAGE", "DOOR", "WINDOW", "HANDRAIL", "FACADE"):
            con.execute("INSERT OR IGNORE INTO categories(name) VALUES (?)", (category,))
        for task in CORE_TASKS:
            con.execute("INSERT OR IGNORE INTO task_names(name) VALUES (?)", (task,))
        con.execute("INSERT OR IGNORE INTO roles(name) VALUES (?)", ("Senior Team Leader",))
        _cleanup_catalog(con)
        ensure_project_levels(con)
        remap_level_series_to_f(con, "24139")
        con.execute("UPDATE projects SET hours_per_day=8.25 WHERE hours_per_day IS NULL OR ABS(hours_per_day-8)<0.001")
        forecast_app.ensure_public_holidays(con)
        views_sql = os.path.join(HERE, "views.sql")
        if os.path.isfile(views_sql):
            with open(views_sql, encoding="utf-8") as fh:
                con.executescript(fh.read())
        con.commit()
        con.close()


# --------------------------------------------------------------------------- #
# data access
# --------------------------------------------------------------------------- #
def connect():
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA busy_timeout = 15000")
    con.execute("""
        CREATE TABLE IF NOT EXISTS leave_days (
            person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
            work_date TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (person_id, work_date)
        )
    """)
    con.row_factory = sqlite3.Row
    return con


# Manage Lists is restricted to these roles. People sign in by picking their name
# (no password); identity is remembered in a cookie and works from any PC.
ADMIN_ROLES = ("Manager", "Team Leader", "Team leader", "Assistant team leader",
               "Senior Team Leader", "Coordinator")
ADMIN_ROLE_FOLD = {r.strip().lower() for r in ADMIN_ROLES}
OFFICE_LOCATIONS = ("Chennai", "Vizag")


def _cookie(handler, key):
    for part in (handler.headers.get("Cookie", "") or "").split(";"):
        part = part.strip()
        if part.startswith(key + "="):
            return part[len(key) + 1:].strip()
    return None


def signed_in_person(handler):
    """The employee identified by the sign-in cookie, or None."""
    pid = _cookie(handler, "uid")
    if not pid:
        return None
    con = connect()
    row = con.execute("SELECT pe.id, pe.name, r.name AS role FROM people pe "
                      "LEFT JOIN roles r ON r.id=pe.role_id WHERE pe.id=?", (pid,)).fetchone()
    con.close()
    return row


def _any_admin_exists():
    con = connect()
    n = con.execute(
        "SELECT COUNT(*) FROM people pe JOIN roles r ON r.id=pe.role_id "
        "WHERE lower(trim(r.name)) IN (%s)" % ",".join("?" * len(ADMIN_ROLE_FOLD)),
        tuple(ADMIN_ROLE_FOLD)).fetchone()[0]
    con.close()
    return n > 0


def is_admin(handler):
    row = signed_in_person(handler)
    if row and (row["role"] or "").strip().lower() in ADMIN_ROLE_FOLD:
        return True
    return not _any_admin_exists()     # first-time setup: no admin exists yet, allow so you can create one


def is_manager(handler):
    row = signed_in_person(handler)
    return bool(row and (row["role"] or "").strip().lower() == "manager")


def whoami_banner(handler):
    row = signed_in_person(handler)
    if row:
        role = row["role"] or "no role"
        return (f'<div class="whoami">You are: <b>{html.escape(row["name"])}</b> &mdash; '
                f'{html.escape(role)} &nbsp;·&nbsp; <a href="/signout">sign out</a></div>')
    return ('<div class="whoami">You are: <b>not signed in</b> &nbsp;·&nbsp; '
            '<a href="/signin">sign in</a></div>')


def today_iso():
    return dt.date.today().isoformat()


def parse_iso_date(raw):
    s = (raw or "").strip()
    if not s:
        return None
    try:
        return dt.date.fromisoformat(s[:10]).isoformat()
    except ValueError:
        return None


def fmt_day(iso):
    if not iso:
        return ""
    try:
        return dt.date.fromisoformat(str(iso)[:10]).strftime("%d %b %Y")
    except ValueError:
        return str(iso)


def mark_leave(person_id, start_date, end_date=None):
    start = parse_iso_date(start_date)
    end = parse_iso_date(end_date or start_date)
    if not start or not end or end < start:
        raise ValueError("Choose a valid leave From and To date.")
    con = connect()
    day = dt.date.fromisoformat(start)
    last = dt.date.fromisoformat(end)
    while day <= last:
        if day.weekday() < 5:
            con.execute("INSERT OR IGNORE INTO leave_days(person_id, work_date) VALUES (?, ?)",
                        (int(person_id), day.isoformat()))
        day += dt.timedelta(days=1)
    con.commit()
    con.close()
    return start, end


def remove_leave(person_id, work_date):
    day = parse_iso_date(work_date)
    if not day:
        raise ValueError("Choose a valid leave date.")
    con = connect()
    con.execute("DELETE FROM leave_days WHERE person_id=? AND work_date=?",
                (int(person_id), day))
    con.commit()
    con.close()
    return day


def leave_days_for(person_id):
    con = connect()
    rows = [r[0] for r in con.execute(
        "SELECT work_date FROM leave_days WHERE person_id=? ORDER BY work_date DESC",
        (int(person_id),))]
    con.close()
    return rows


def ensure_project_people_history(con):
    """Keep add/remove dates so Who didn't fill uses the team as of that day."""
    if not _table_exists(con, "project_people"):
        return
    cols = {r[1] for r in con.execute("PRAGMA table_info(project_people)")}
    if "id" in cols and "joined_on" in cols and "left_on" in cols:
        con.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_pp_open "
            "ON project_people(project_code, person_id) WHERE left_on IS NULL")
        con.execute("CREATE INDEX IF NOT EXISTS ix_project_people_person ON project_people(person_id)")
        return
    con.commit()
    con.execute("PRAGMA foreign_keys = OFF")
    con.execute("DROP TABLE IF EXISTS project_people_hist")
    con.execute("""
        CREATE TABLE project_people_hist (
            id INTEGER PRIMARY KEY,
            project_code TEXT NOT NULL,
            person_id INTEGER NOT NULL,
            role_id INTEGER,
            joined_on TEXT NOT NULL,
            left_on TEXT
        )
    """)
    if "joined_on" in cols:
        con.execute("""
            INSERT INTO project_people_hist(project_code,person_id,role_id,joined_on,left_on)
            SELECT project_code, person_id, role_id,
                   COALESCE(joined_on, '2000-01-01'), left_on
              FROM project_people
        """)
    else:
        con.execute("""
            INSERT INTO project_people_hist(project_code,person_id,role_id,joined_on,left_on)
            SELECT pp.project_code, pp.person_id, pp.role_id,
                   COALESCE(
                     (SELECT MIN(t.work_date) FROM tasks t
                        JOIN task_assignees ta ON ta.task_id = t.id
                       WHERE t.project_code = pp.project_code AND ta.person_id = pp.person_id),
                     (SELECT MIN(t.work_date) FROM tasks t WHERE t.project_code = pp.project_code),
                     '2000-01-01'
                   ),
                   NULL
              FROM project_people pp
        """)
    con.execute("DROP TABLE project_people")
    con.execute("ALTER TABLE project_people_hist RENAME TO project_people")
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_pp_open "
        "ON project_people(project_code, person_id) WHERE left_on IS NULL")
    con.execute("CREATE INDEX IF NOT EXISTS ix_project_people_person ON project_people(person_id)")
    con.commit()
    con.execute("PRAGMA foreign_keys = ON")


def drop_from_project(cur, project_code, person_id, left_on=None):
    """Mark left_on as their last day. They drop off from the next working day."""
    left_on = parse_iso_date(left_on)
    if not left_on:
        return "Enter a Left on date to take this person off the team."
    row = cur.execute(
        "SELECT joined_on FROM project_people "
        "WHERE project_code=? AND person_id=? AND left_on IS NULL",
        (project_code, person_id)).fetchone()
    if not row:
        return None
    joined = row[0] or left_on
    if left_on < joined:
        return (f"Exit date {fmt_day(left_on)} is before the join date "
                f"{fmt_day(joined)}. Correct the dates and try again.")
    cur.execute(
        "UPDATE project_people SET left_on=? "
        "WHERE project_code=? AND person_id=? AND left_on IS NULL",
        (left_on, project_code, person_id))
    cur.execute("DELETE FROM lead_scope WHERE project_code=? AND person_id=?",
                (project_code, person_id))
    cur.execute(
        "DELETE FROM lead_support WHERE project_code=? AND (lead_id=? OR support_id=?)",
        (project_code, person_id, person_id))
    return None


def upsert_project_person(cur, project_code, person_id, role_id=None, joined_on=None):
    joined_on = parse_iso_date(joined_on) or today_iso()
    cur.execute(
        "INSERT OR IGNORE INTO project_people"
        "(project_code, person_id, role_id, joined_on) VALUES (?,?,?,?)",
        (project_code, person_id, role_id, joined_on))
    if role_id is not None:
        cur.execute(
            "UPDATE project_people SET role_id=? "
            "WHERE project_code=? AND person_id=? AND left_on IS NULL",
            (role_id, project_code, person_id))


def parse_request_body(handler, raw):
    """Parse normal forms and the small multipart upload used by bulk import."""
    content_type = handler.headers.get("Content-Type", "")
    if not content_type.startswith("multipart/form-data"):
        return parse_qs(raw.decode("utf-8"), keep_blank_values=True), {}
    message = BytesParser(policy=policy.default).parsebytes(
        (f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n").encode("ascii") + raw)
    fields, files = {}, {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        payload = part.get_payload(decode=True) or b""
        filename = part.get_filename()
        if filename:
            files[name] = (filename, payload)
        else:
            fields.setdefault(name, []).append(payload.decode("utf-8", errors="replace"))
    return fields, files


def reference_data():
    """Everything the dropdowns need, filtered client-side by the chosen project."""
    con = connect()
    q = con.execute
    ref = {
        "projects":  [dict(r) for r in q("SELECT code,name,no_sheet_type,no_zone,project_status,hours_per_day,day_start,day_end,use_teams FROM projects WHERE project_status='Active' ORDER BY code")],
        "buildings": [dict(r) for r in q(
            "SELECT id,project_code,name,is_all FROM buildings WHERE TRIM(COALESCE(name,''))<>'' ORDER BY name")],
        "models":    [dict(r) for r in q(
            "SELECT id,project_code,name FROM models WHERE TRIM(COALESCE(name,''))<>'' ORDER BY name")],
        "master_tasks": [dict(r) for r in q(
            "SELECT id,project_code,sheet_type_id,name FROM master_tasks ORDER BY name")],
        "sub_tasks": [dict(r) for r in q("SELECT id,master_task_id,name FROM sub_tasks ORDER BY name")],
        "sub_sub_tasks": [dict(r) for r in q("SELECT id,sub_task_id,name FROM sub_sub_tasks ORDER BY name")],
        "task_sub_tasks": [dict(r) for r in q("SELECT id,task_name_id,category_id,name FROM task_sub_tasks ORDER BY name")],
        "master_task_tasks": [dict(r) for r in q("SELECT master_task_id,task_name_id FROM master_task_tasks")],
        "sheet_types": [dict(r) for r in q("SELECT id,name FROM sheet_types ORDER BY name")],
        "levels":    [dict(r) for r in q("SELECT l.id,l.label,pl.project_code FROM levels l JOIN project_levels pl ON pl.level_id=l.id ORDER BY (l.number IS NULL), l.number")],
        "task_names":[dict(r) for r in q("SELECT id,name FROM task_names ORDER BY name")],
        "categories":[dict(r) for r in q("SELECT id,name FROM categories ORDER BY name")],
        "category_tasks":[dict(r) for r in q("SELECT master_task_id,category_id,task_name_id FROM category_tasks")],
        "people":    [dict(r) for r in q("SELECT pe.id, pe.name, pe.emp_code, r.name AS role FROM people pe LEFT JOIN roles r ON r.id=pe.role_id ORDER BY pe.name")],
        "project_people": [dict(r) for r in q(
            "SELECT pp.project_code, pp.person_id, r.name AS role, pp.joined_on, pp.left_on "
            "FROM project_people pp "
            "LEFT JOIN roles r ON r.id=pp.role_id")],
        "lead_scope": [dict(r) for r in q(
            "SELECT project_code,person_id,kind,item_id FROM lead_scope")],
        "lead_support": [dict(r) for r in q(
            "SELECT project_code,lead_id,support_id FROM lead_support")],
        "statuses":  [dict(r) for r in q("SELECT id,name FROM statuses ORDER BY name")],
        "breaks":    [dict(r) for r in q("SELECT project_code,start_time,end_time FROM break_windows")],
    }
    con.close()
    return ref


def project_breaks(con, code):
    rows = con.execute("SELECT start_time,end_time FROM break_windows WHERE project_code=?",
                       (code,)).fetchall()
    out = []
    for r in rows:
        h1, m1 = map(int, r["start_time"].split(":"))
        h2, m2 = map(int, r["end_time"].split(":"))
        out.append((h1 * 60 + m1, h2 * 60 + m2))
    return out or [(765, 810), (675, 690), (975, 990)]


def to_min(hhmm):
    if not hhmm:
        return None
    h, m = str(hhmm).split(":")
    return int(h) * 60 + int(m)


DAY_END_MIN = 18 * 60 + 30  # 18:30 — a full-day out time or later


def _form_assignee_ids(form):
    ids = []
    for v in form.get("assignees", []):
        v = (v or "").strip()
        if not v:
            continue
        try:
            ids.append(int(v))
        except ValueError:
            continue
    return ids


def own_day_slices(person_id, since_days=21):
    """This person's saved In/Out slices, used to stop a second full-day save."""
    if not person_id:
        return []
    since = (dt.date.today() - dt.timedelta(days=since_days)).isoformat()
    con = connect()
    rows = con.execute(
        "SELECT t.id, t.work_date, t.start_time, t.end_time "
        "FROM tasks t JOIN task_assignees ta ON ta.task_id=t.id "
        "WHERE ta.person_id=? AND t.work_date>=? "
        "ORDER BY t.work_date DESC, t.id DESC",
        (int(person_id), since)).fetchall()
    con.close()
    return [{"id": r["id"], "date": r["work_date"],
             "start": r["start_time"] or "", "end": r["end_time"] or ""} for r in rows]


def blocked_full_day_entry(form, exclude_task_id=None):
    """Block a second save when this person already has a task that day
    ending at 18:30 or later. Split-day slices (14:00 then 16:00 then 18:30)
    are allowed because the earlier Out times are before 18:30."""
    work_date = (form.get("work_date", [""])[0] or "").strip()
    people_ids = _form_assignee_ids(form)
    if not work_date or not people_ids:
        return None
    exclude = None
    if exclude_task_id:
        try:
            exclude = int(exclude_task_id)
        except (TypeError, ValueError):
            exclude = None
    con = connect()
    try:
        for pid in people_ids:
            rows = con.execute(
                "SELECT t.id, t.end_time, pe.name FROM tasks t "
                "JOIN task_assignees ta ON ta.task_id=t.id "
                "JOIN people pe ON pe.id=ta.person_id "
                "WHERE ta.person_id=? AND t.work_date=?",
                (pid, work_date)).fetchall()
            others = [r for r in rows if exclude is None or int(r["id"]) != exclude]
            full = [r for r in others
                    if to_min(r["end_time"]) is not None
                    and to_min(r["end_time"]) >= DAY_END_MIN]
            if not full:
                continue
            name = full[0]["name"]
            until = full[0]["end_time"]
            return (f"{name} already has a task on {work_date} that runs to "
                    f"{until} (18:30 or later). Only one entry is allowed in that "
                    f"case. To log several tasks, end the earlier ones before 18:30 "
                    f"(for example 14:00, then 16:00, then 18:30).")
    finally:
        con.close()
    return None


def blocked_off_team_entry(form):
    """Block assigning someone after their next working day following Left on."""
    project = (form.get("project", [""])[0] or "").strip()
    work_date = (form.get("work_date", [""])[0] or "").strip()
    people_ids = _form_assignee_ids(form)
    if not project or not work_date or not people_ids:
        return None
    con = connect()
    try:
        has_team = con.execute(
            "SELECT 1 FROM project_people WHERE project_code=? LIMIT 1",
            (project,)).fetchone()
        if not has_team:
            return None
        team, _ = project_team_people(con, project, work_date)
        allowed = {int(r[0]) for r in team}
        for pid in people_ids:
            if pid in allowed:
                continue
            who = con.execute("SELECT name FROM people WHERE id=?", (pid,)).fetchone()
            name = who[0] if who else str(pid)
            row = con.execute(
                "SELECT joined_on, left_on FROM project_people "
                "WHERE project_code=? AND person_id=? "
                "ORDER BY left_on IS NULL DESC, joined_on DESC",
                (project, pid)).fetchone()
            if row and row["left_on"] and not on_team_that_day(
                    row["joined_on"], row["left_on"], work_date):
                nxt = next_working_day(row["left_on"])
                return (f"{name} left {project} on {row['left_on']} and is not available "
                        f"from the next working day ({nxt}). Pick a date through {row['left_on']}, "
                        f"or choose someone still on the team.")
            if row and row["joined_on"] and work_date < row["joined_on"]:
                return (f"{name} joined {project} on {row['joined_on']} and is not available "
                        f"before that date.")
            return (f"{name} is not on the {project} team on {work_date}.")
    finally:
        con.close()
    return None


def recent_rows(limit=50, project=None):
    """The most recent entries, newest first. `project` scopes them to one
    project so the entry form can show only what is relevant to the job in hand."""
    con = connect()
    if project:
        rows = con.execute(
            "SELECT id, work_date, start_time, end_time, hours, entered_by FROM tasks "
            "WHERE project_code=? ORDER BY id DESC LIMIT ?", (project, limit)).fetchall()
    else:
        rows = con.execute(
            "SELECT id, work_date, start_time, end_time, hours, entered_by FROM tasks "
            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    # attach person + task name
    out = []
    for r in rows:
        who = con.execute(
            "SELECT GROUP_CONCAT(pe.name,', ') n FROM task_assignees ta "
            "JOIN people pe ON pe.id=ta.person_id WHERE ta.task_id=?", (r["id"],)).fetchone()["n"]
        tn = con.execute(
            "SELECT tn.name n FROM tasks t LEFT JOIN task_names tn ON tn.id=t.task_name_id "
            "WHERE t.id=?", (r["id"],)).fetchone()["n"]
        out.append({"id": r["id"], "date": r["work_date"], "who": who or "",
                    "task": tn or "", "time": f'{r["start_time"] or ""}-{r["end_time"] or ""}',
                    "hours": f'{r["hours"]:.2f}', "entered_by": r["entered_by"]})
    con.close()
    return out


def _task_columns(form, con):
    """Compute the tasks-table column values from the submitted form (shared by
    insert and update). Break: manual override if given, else standard-window overlap."""
    code = form.get("project", [""])[0]
    start = form.get("start_time", [""])[0]
    end = form.get("end_time", [""])[0]
    sm, em = to_min(start), to_min(end)
    override = form.get("break_override", [""])[0].strip()
    if override:
        brk = float(override)
    else:
        wins = project_breaks(con, code)
        brk = sum(max(0.0, min(em, b) - max(sm, a)) for (a, b) in wins) if (sm is not None and em is not None) else 0.0
    hours = max(0.0, ((em - sm) % 1440) / 60.0 - brk / 60.0) if (sm is not None and em is not None) else 0.0

    def one(name):
        v = form.get(name, [""])[0].strip()
        return v or None

    return {
        "work_date": one("work_date"), "start_time": start or None, "end_time": end or None,
        "break_mins": round(brk, 2), "hours": round(hours, 4),
        "permission": 1 if form.get("permission") else 0,
        "grouped": 1 if form.get("grouped") else 0,
        "project_code": code, "sheet_type_id": one("sheet_type_id"),
        "master_task_id": one("master_task_id"), "building_id": one("building_id"),
        "sub_task_id": one("sub_task_id"), "sub_sub_task_id": one("sub_sub_task_id"),
        "task_sub_task_id": _first(form, "task_sub_task_ids") or one("task_sub_task_id"),
        "category_id": _first(form, "category_ids") or one("category_id"),
        "level_id": one("level_id"), "model_id": one("model_id"),
        "task_name_id": one("task_name_id"), "description": one("description"),
        "status_id": one("status_id"), "pct_complete": one("pct_complete"),
        "notes": one("notes"),
    }


def _int_or_none(value, low, high):
    """A blank field clears the value; anything out of range is ignored."""
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return None
    return n if low <= n <= high else None


def _float_or_none(value, low, high):
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if low <= n <= high else None


def _first(form, key):
    """First ticked value of a multi-select checkbox group, or None."""
    for v in form.get(key, []):
        if v.strip():
            return int(v)
    return None


def _set_links(cur, table, col, task_id, form, key):
    """Replace a task's rows in a junction table with what the form ticked."""
    cur.execute(f"DELETE FROM {table} WHERE task_id=?", (task_id,))
    for v in form.get(key, []):
        if v.strip():
            cur.execute(f"INSERT OR IGNORE INTO {table}(task_id,{col}) VALUES (?,?)",
                        (task_id, int(v)))


def _set_hierarchy(cur, task_id, form):
    _set_links(cur, "task_categories", "category_id", task_id, form, "category_ids")
    _set_links(cur, "task_sub_task_links", "task_sub_task_id", task_id, form, "task_sub_task_ids")


def _set_assignees(cur, task_id, form):
    cur.execute("DELETE FROM task_assignees WHERE task_id=?", (task_id,))
    for pid in form.get("assignees", []):
        if pid.strip():
            cur.execute("INSERT OR IGNORE INTO task_assignees(task_id,person_id) VALUES (?,?)",
                        (task_id, int(pid)))


def insert_task(form, entered_by=None):
    con = connect()
    cur = con.cursor()
    cols = _task_columns(form, con)
    # Whose work it is lives in task_assignees; who sat at the keyboard lives
    # here. They differ when an admin logs an entry on someone else's behalf.
    cols["entered_by"] = entered_by
    keys = list(cols.keys())
    cur.execute(f"INSERT INTO tasks({','.join(keys)}) VALUES ({','.join('?' * len(keys))})",
                [cols[k] for k in keys])
    task_id = cur.lastrowid
    _set_assignees(cur, task_id, form)
    _set_hierarchy(cur, task_id, form)
    con.commit()
    con.close()
    return task_id


def update_task(task_id, form):
    con = connect()
    cur = con.cursor()
    cols = _task_columns(form, con)
    keys = list(cols.keys())
    cur.execute(f"UPDATE tasks SET {','.join(k + '=?' for k in keys)} WHERE id=?",
                [cols[k] for k in keys] + [int(task_id)])
    _set_assignees(cur, int(task_id), form)
    _set_hierarchy(cur, int(task_id), form)
    con.commit()
    con.close()
    return int(task_id)


def delete_task(task_id, entered_by):
    con = connect()
    cur = con.execute(
        "DELETE FROM tasks WHERE id=? AND entered_by=?",
        (int(task_id), int(entered_by)),
    )
    con.commit()
    con.close()
    return cur.rowcount > 0


def get_task_for_edit(task_id):
    con = connect()
    r = con.execute("SELECT * FROM tasks WHERE id=?", (int(task_id),)).fetchone()
    if not r:
        con.close()
        return None
    assignees = [row["person_id"] for row in
                 con.execute("SELECT person_id FROM task_assignees WHERE task_id=?", (int(task_id),)).fetchall()]
    category_ids = [row[0] for row in
                    con.execute("SELECT category_id FROM task_categories WHERE task_id=?", (int(task_id),)).fetchall()]
    sub_task_ids = [row[0] for row in
                    con.execute("SELECT task_sub_task_id FROM task_sub_task_links WHERE task_id=?", (int(task_id),)).fetchall()]
    con.close()
    return {
        "id": r["id"], "work_date": r["work_date"], "project": r["project_code"],
        "sheet_type_id": r["sheet_type_id"], "master_task_id": r["master_task_id"],
        "building_id": r["building_id"], "level_id": r["level_id"], "model_id": r["model_id"],
        "task_name_id": r["task_name_id"], "start_time": r["start_time"], "end_time": r["end_time"],
        "break_override": r["break_mins"], "status_id": r["status_id"],
        "description": r["description"], "pct_complete": r["pct_complete"], "notes": r["notes"],
        "permission": r["permission"], "grouped": r["grouped"], "assignees": assignees,
        "category_ids": category_ids or ([r["category_id"]] if r["category_id"] else []),
        "task_sub_task_ids": sub_task_ids or ([r["task_sub_task_id"]] if r["task_sub_task_id"] else []),
    }


def can_edit_task(task_id, person_id, manager=False):
    if manager:
        return True
    try:
        task_id = int(task_id)
        person_id = int(person_id)
    except (TypeError, ValueError):
        return False
    con = connect()
    row = con.execute("SELECT entered_by FROM tasks WHERE id=?", (task_id,)).fetchone()
    con.close()
    return bool(row and row["entered_by"] == person_id)


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #
PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Daily Task Entry</title>
<style>
  :root{{--ink:#1a2230;--muted:#5b6675;--rule:#d5dbe3;--accent:#1f5a86;--ok:#1f7a4d;--bg:#f5f7fa;--card:#fff}}
  *{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--ink);
    font:15px/1.5 "Segoe UI",system-ui,Arial,sans-serif}}
  .wrap{{max-width:860px;margin:0 auto;padding:24px 18px 60px}}
  h1{{font-size:20px;margin:0 0 4px}} .sub{{color:var(--muted);font-size:13px;margin:0 0 18px}}
  .ok{{background:#e6f4ec;border:1px solid var(--ok);color:var(--ok);padding:10px 14px;
    border-radius:8px;margin-bottom:16px;font-weight:600}}
  .err{{background:#fde8e8;border:1px solid #a33;color:#a33;padding:10px 14px;
    border-radius:8px;margin-bottom:16px;font-weight:600}}
  form{{background:var(--card);border:1px solid var(--rule);border-radius:10px;padding:18px}}
  .grid{{display:grid;grid-template-columns:1fr 1fr;gap:14px 18px}}
  .full{{grid-column:1/-1}}
  label{{display:block;font-size:12px;letter-spacing:.02em;color:var(--muted);
    text-transform:uppercase;margin-bottom:4px;font-weight:600}}
  input,select,textarea{{width:100%;padding:9px 10px;border:1px solid var(--rule);
    border-radius:7px;font:inherit;background:#fff;color:var(--ink)}}
  input:focus,select:focus,textarea:focus{{outline:2px solid var(--accent);border-color:var(--accent)}}
  .people{{display:flex;flex-wrap:wrap;gap:8px 14px}}
  .people label{{display:flex;align-items:center;gap:6px;text-transform:none;font-size:14px;
    color:var(--ink);font-weight:400;margin:0}}
  .people input{{width:auto}}
  .bulk{{margin-left:10px;display:inline-flex;gap:6px;vertical-align:middle}}
  .bulk button{{background:none;border:1px solid var(--rule);color:var(--accent);
    border-radius:4px;padding:1px 8px;font-size:11px;cursor:pointer;text-transform:none;
    letter-spacing:0;font-weight:600}}
  .bulk button:hover{{background:var(--accent);color:#fff;border-color:var(--accent)}}
  .ticks{{max-height:190px;overflow-y:auto;border:1px solid var(--rule);border-radius:6px;
    padding:8px 10px;background:#fff}}
  .ticks .grp{{width:100%;margin:6px 0 2px;font-size:11px;letter-spacing:.04em;
    text-transform:uppercase;color:var(--muted)}}
  .ticks .grp:first-child{{margin-top:0}}
  .ticks .none{{color:var(--muted);font-size:13px}}
  .none{{color:var(--muted)}}
  h2 .hint{{font-weight:400;text-transform:none;letter-spacing:0;font-size:12px}}
  .flags{{display:flex;gap:22px;align-items:center}}
  .flags label{{display:flex;align-items:center;gap:7px;text-transform:none;font-size:14px;
    color:var(--ink);font-weight:400;margin:0}} .flags input{{width:auto}}
  .actions{{margin-top:18px;display:flex;gap:12px;align-items:center}}
  button{{background:var(--accent);color:#fff;border:0;border-radius:8px;padding:11px 22px;
    font-weight:600;font-size:15px;cursor:pointer}} button:hover{{filter:brightness(1.08)}}
  .hint{{color:var(--muted);font-size:12px}}
  .nav{{margin-bottom:14px;font-size:13px}} .nav a{{color:var(--accent);text-decoration:none}}
  .nav b{{color:var(--ink)}}
  .whoami{{font-size:12px;color:var(--muted);margin:0 0 14px;padding:6px 10px;background:var(--card);
    border:1px solid var(--rule);border-radius:6px;display:inline-block}} .whoami b{{color:var(--ink)}}
  table{{width:100%;border-collapse:collapse;margin-top:26px;background:var(--card);
    border:1px solid var(--rule);border-radius:10px;overflow:hidden;font-size:13px}}
  th,td{{text-align:left;padding:8px 10px;border-bottom:1px solid var(--rule)}}
  th{{background:#eef2f6;color:var(--muted);text-transform:uppercase;font-size:11px;letter-spacing:.04em}}
  h2{{font-size:14px;margin:26px 0 0}}
</style></head><body><div class="wrap">
  <div class="nav"><b>Task Entry</b> &nbsp;·&nbsp; <a href="/forecast">Forecast</a>
    &nbsp;·&nbsp; <a href="/admin">Manage Lists</a>
    &nbsp;·&nbsp; <a href="/reports">Reports &rsaquo;</a></div>
  {whoami}
  <h1>Daily Task Entry</h1>
  <p class="sub">One task per row. Times drive the hours: a full day is <b>8.25 hrs</b>
     (09:00–18:30 minus 75 min breaks). Time after 18:30 is extra. If a task’s Out time is
     <b>18:30 or later</b>, that person may save <b>only that one entry</b> for the day.
     To log several tasks, end the earlier ones before 18:30 (for example 14:00, then 16:00,
     then 18:30). After <b>Save</b>, change times and fields before saving another slice.</p>
  {ok}
  <form method="POST" action="/add">
    <input type="hidden" name="edit_id" id="edit_id" value="{edit_id}">
    <div class="grid">
            <div><label>Task date</label><input type="date" name="work_date" value="{today}" required></div>
      <div><label>Project code</label><select name="project" id="project" required></select></div>
      <div><label>Project name</label><input id="project_name" readonly></div>

    <div><label>Sheet Type</label><select name="sheet_type_id" id="sheet_type_id"></select></div>
      <div><label>Master Task</label><select name="master_task_id" id="master_task_id"></select></div>
    <div><label>Task</label><select name="task_name_id" id="task_name_id"></select></div>

      <div class="full"><label>Categories <span class="hint">— tick every one you worked on</span>
           <span class="bulk"><button type="button" data-tick-all="category_ids">All</button>
           <button type="button" data-tick-none="category_ids">None</button></span></label>
           <div class="people ticks" id="category_ids"></div></div>

      <div class="full"><label>Sub Tasks <span class="hint">— everything under the ticked Categories</span>
           <span class="bulk"><button type="button" data-tick-all="task_sub_task_ids">All</button>
           <button type="button" data-tick-none="task_sub_task_ids">None</button></span></label>
           <div class="people ticks" id="task_sub_task_ids"></div></div>

    <div><label>Zone</label><select name="building_id" id="building_id"></select></div>
      <div><label>Level</label><select name="level_id" id="level_id"></select></div>

            <div><label>Model</label><select name="model_id" id="model_id"></select></div>
      <div><label>Start (In)</label><input type="time" name="start_time" value="09:00" required></div>
      <div><label>End (Out)</label><input type="time" name="end_time" value="18:30" required></div>

      <div><label>Status</label><select name="status_id" id="status_id"></select></div>
      <div><label>Break override (mins) <span class="hint">— leave blank to auto-calc</span></label>
           <input type="number" name="break_override" min="0" step="5" placeholder="auto"></div>
      <div class="full"><p class="hint" id="hours_live" style="margin:0">Hours update as you set In / Out.</p></div>

      <div class="full"><label>Task Description</label>
           <textarea name="description" rows="2"></textarea></div>

      {assignees}

      <div class="flags full">
        <label><input type="checkbox" name="permission" value="1"> Permission (left early)</label>
        <label><input type="checkbox" name="grouped" value="1"> Group &mdash; several tasks I did today</label>
        <div style="margin-left:auto"><label style="text-transform:none;color:var(--muted)">% Complete</label>
             <input type="number" name="pct_complete" min="0" max="100" style="width:90px"></div>
      </div>

      <div class="full"><label>Notes</label><textarea name="notes" rows="2"></textarea></div>
    </div>
        <div class="actions"><button type="submit">Save task</button>
            <button type="button" onclick="clearForm()" style="background:#6b7280">Clear form</button>
            <span class="hint">Leave dates are weekdays only. Task date is separate.</span></div>
  </form>

    {leave_html}

  <h2>Last <span id="recent_count">0</span> entries
      <span class="hint" id="recent_scope"></span></h2>
  <table><thead><tr><th>Date</th><th>Who</th><th>Task</th><th>Time</th><th>Hours</th>
    <th>Edit</th><th>Remove</th></tr></thead>
  {recent}</table>
</div>
<script>
const REF = {ref};
const EDIT = {edit_json};
const ME = {me_json};
const MY_DAY = {my_day};
const DAY_END_MIN = 18 * 60 + 30;
function isLeadRole(p, proj){{
  proj = proj || (document.getElementById("project")||{{}}).value;
  const pid = (p && p.id != null) ? p.id : p;
  const row = membershipOnDay(pid, proj);
  if(row && row.role) return String(row.role).toUpperCase() === "QC REVIEWER";
  return ((p && p.role) || "").toUpperCase() === "QC REVIEWER";
}}
function isSupportRole(p, proj){{
  proj = proj || (document.getElementById("project")||{{}}).value;
  const pid = (p && p.id != null) ? p.id : p;
  const row = membershipOnDay(pid, proj);
  if(row && row.role) return String(row.role).toUpperCase() === "QC SUPPORT";
  return ((p && p.role) || "").toUpperCase() === "QC SUPPORT";
}}
function projectTeamed(){{
  const p = document.getElementById("project").value;
  const project = REF.projects.find(x=>x.code==p);
  return !!(project && Number(project.use_teams));
}}
function nextWorkingDay(iso){{
  if(!iso) return "";
  const d=new Date(iso+"T12:00:00");
  d.setDate(d.getDate()+1);
  while(d.getDay()===0 || d.getDay()===6) d.setDate(d.getDate()+1);
  const m=d.getMonth()+1, day=d.getDate();
  return d.getFullYear()+"-"+(m<10?"0":"")+m+"-"+(day<10?"0":"")+day;
}}
function onTeamThatDay(row, day){{
  day = day || "";
  if(row.joined_on && day && row.joined_on > day) return false;
  if(!row.left_on) return true;
  if(!day) return false;
  const nxt=nextWorkingDay(row.left_on);
  return nxt && day < nxt;
}}
function workDay(){{
  return ((document.getElementById("work_date")||{{}}).value)||"";
}}
function membershipOnDay(pid, proj){{
  proj = proj || (document.getElementById("project")||{{}}).value;
  const day=workDay();
  const rows=(REF.project_people||[]).filter(x=>x.project_code==proj
    && String(x.person_id)===String(pid) && onTeamThatDay(x, day));
  rows.sort(function(a,b){{ return (a.left_on?1:0)-(b.left_on?1:0); }});
  return rows[0]||null;
}}
function teamRoster(){{
  const proj=document.getElementById("project").value;
  const day=workDay();
  const rows=(REF.project_people||[]).filter(x=>x.project_code==proj);
  if(!rows.length) return REF.people;
  const seen={{}};
  const ids=[];
  rows.forEach(function(x){{
    const id=String(x.person_id);
    if(seen[id] || !onTeamThatDay(x, day)) return;
    seen[id]=1;
    ids.push(id);
  }});
  return REF.people.filter(p=>ids.indexOf(String(p.id))>=0);
}}
function currentLeadId(){{
  const proj=document.getElementById("project").value;
  function leadForPerson(pid){{
    if(!pid) return "";
    const p=REF.people.find(x=>String(x.id)===String(pid));
    if(p && isLeadRole(p, proj)) return String(pid);
    const m=(REF.lead_support||[]).find(r=>r.project_code==proj
      && String(r.support_id)===String(pid));
    return m ? String(m.lead_id) : "";
  }}
  const ticks=[].slice.call(document.querySelectorAll(
    '#people input[name=assignees]:checked')).map(c=>c.value);
  for(const id of ticks){{
    const lid=leadForPerson(id);
    if(lid) return lid;
  }}
  const hidden=document.querySelector('input[name=assignees][type=hidden]');
  if(hidden && hidden.value) return leadForPerson(hidden.value);
  if(ME && ME.id) return leadForPerson(ME.id);
  return "";
}}
function leadScopeIds(kind, leadId, project){{
  if(!leadId) return [];
  return (REF.lead_scope||[]).filter(r=>r.project_code==project
    && String(r.person_id)==String(leadId) && r.kind==kind).map(r=>String(r.item_id));
}}
function scopedItems(items, idKey, kind, project){{
  if(!projectTeamed()) return items;
  const lead=currentLeadId();
  if(!lead) return items;
  const ids=leadScopeIds(kind, lead, project);
  const any=(REF.lead_scope||[]).some(r=>r.project_code==project && r.kind==kind);
  if(!ids.length) return any ? [] : items;
  if(kind==="zone"){{
    const allIds=items.filter(i=>Number(i.is_all)).map(i=>String(i.id));
    if(ids.some(id=>allIds.indexOf(id)>=0)) return items;
  }}
  return items.filter(i=>ids.indexOf(String(i[idKey]))>=0);
}}
function opts(sel, items, val, txt, placeholder){{
  const e = document.getElementById(sel); if(!e) return;
  e.innerHTML = (placeholder?`<option value="">— ${{placeholder}} —</option>`:"")
    + items.map(i=>`<option value="${{i[val]}}">${{i[txt]}}</option>`).join("");
}}
function fill(){{
  const p = document.getElementById("project").value;
  const st = document.getElementById("sheet_type_id").value;
    const project = REF.projects.find(x=>x.code==p);
    const pn = document.getElementById("project_name");
    if(pn) pn.value = project ? (project.name||"") : "";
    const sheetType = document.getElementById("sheet_type_id");
    sheetType.disabled = !!(project && project.no_sheet_type);
    sheetType.style.backgroundColor = sheetType.disabled ? "#e5e7eb" : "#fff";
    if(sheetType.disabled) sheetType.value = "";
    const zone = document.getElementById("building_id");
    zone.disabled = !!(project && project.no_zone);
    zone.style.backgroundColor = zone.disabled ? "#e5e7eb" : "#fff";
    if(zone.disabled) zone.value = "";
  fillPeople();
  opts("building_id", scopedItems(REF.buildings.filter(b=>b.project_code==p), "id","zone", p), "id","name","choose");
    opts("level_id", scopedItems(REF.levels.filter(l=>l.project_code==p), "id","level", p), "id","label","choose");
  opts("model_id",    REF.models.filter(m=>m.project_code==p), "id","name","choose");
  opts("master_task_id",
    REF.master_tasks.filter(m=>m.project_code==p && (!st || String(m.sheet_type_id)==st)),
    "id","name","choose");
  fillRecent();
}}
// The entry list is scoped to the chosen project — the other projects' rows are
// rendered but hidden, so switching needs no page reload.
function fillRecent(){{
  const proj=document.getElementById("project").value;
  let shown=0;
  document.querySelectorAll("tbody.recent").forEach(t=>{{
    const match = t.getAttribute("data-proj")===proj;
    t.hidden = !match;
    if(match) shown = Number(t.getAttribute("data-count")||0);
  }});
  const c=document.getElementById("recent_count");
  const sc=document.getElementById("recent_scope");
  if(c) c.textContent = shown;
  if(sc) sc.textContent = proj ? "— for project " + proj : "";
}}
opts("project", REF.projects, "code","code","");
opts("sheet_type_id", REF.sheet_types, "id","name","any");
opts("level_id", [], "id","label","choose");
opts("task_name_id", REF.task_names, "id","name","choose");
opts("status_id", REF.statuses, "id","name","choose");
// Assignees are limited to the project's team. A project with no team set
// offers everyone, so entry is never blocked before the team is filled in.
function fillPeople(){{
  const roster=teamRoster();
  const box=document.getElementById("people");
  if(!box) return;            // team members have a fixed assignee, no picker
  const list=roster;
  const on=new Set([].slice.call(
    document.querySelectorAll('#people input[name=assignees]:checked')).map(c=>c.value));
  box.innerHTML = list.length
    ? list.map(p=>`<label><input type="checkbox" name="assignees" value="${{p.id}}"`
        + (on.has(String(p.id))?" checked":"") + `>${{p.name}}</label>`).join("")
    : `<span class="none">No one is on this project's team yet — add them in Manage Lists.</span>`;
}}
fillPeople();
// Auto-fill Building + Level from the chosen Master Task (same rules as the Excel
// formulas). Still editable afterwards. Building: "Block X", or "..._B#_" -> "B#",
// else "All Blocks". Level: "_L##" -> "Level ##", else "All levels".
function selectByText(id, text){{
  const e=document.getElementById(id); if(!e) return;
  for(let i=0;i<e.options.length;i++){{ if(e.options[i].text===text){{ e.selectedIndex=i; return; }} }}
}}
function autofill(){{
  const s=document.getElementById("master_task_id"); if(!s||!s.value) return;
  const name=s.options[s.selectedIndex].text;
  let b="All Blocks";
  let m=name.match(/Block ([A-Za-z0-9])/);
  if(m){{ b="Block "+m[1]; }}
  else {{ let m2=name.match(/_B([0-9]+)_/); if(m2){{ b="B"+m2[1]; }} }}
  selectByText("building_id", b);
  let lvl="All levels";
  let ml=name.match(/_L(-?[0-9]+)/);
  if(ml){{ lvl="Level "+parseInt(ml[1],10); }}
  selectByText("level_id", lvl);
}}
// Categories and Sub Tasks are multi-select: a person works across several
// Categories in a day and ticks off several Sub Tasks. Ticking a Category adds
// its Sub Tasks to the panel below; unticking removes them.
function ticked(id){{
  return [].slice.call(document.querySelectorAll(`#${{id}} input:checked`)).map(c=>c.value);
}}
function fillTicks(id, name, groups, keep){{
  const box=document.getElementById(id); if(!box) return;
  const on=new Set((keep||ticked(id)).map(String));
  let html="";
  groups.forEach(g=>{{
    if(!g.items.length) return;
    if(g.label) html+=`<div class="grp">${{g.label}}</div>`;
    html+=g.items.map(i=>`<label><input type="checkbox" name="${{name}}" value="${{i.id}}"`
      + (on.has(String(i.id))?" checked":"") + `>${{i.name}}</label>`).join("");
  }});
  box.innerHTML = html || `<span class="none">${{emptyHint(id)}}</span>`;
}}
function emptyHint(id){{
  if(id=="category_ids") return "No Categories for this Master Task yet.";
  return ticked("category_ids").length
    ? "No Sub Tasks under the ticked Categories for this Task."
    : "Tick a Category above to see its Sub Tasks.";
}}
function fillHierarchy(keepCats, keepSubs){{
    const master=document.getElementById("master_task_id").value;
    const p=document.getElementById("project").value;
    const cats=scopedItems(REF.categories.filter(c=>REF.category_tasks.some(
        x=>String(x.master_task_id)==master&&String(x.category_id)==String(c.id))),
        "id","category", p);
    fillTicks("category_ids","category_ids",[{{label:"",items:cats}}],keepCats);
    fillTasks(keepSubs);
}}
function fillTasks(keepSubs){{
    const master=document.getElementById("master_task_id").value;
    const cats=ticked("category_ids");
    // Every Category offers the same fixed Task list, so one ticked Category is
    // enough to populate it; with none ticked, offer whatever this Master Task has.
    const rows=REF.category_tasks.filter(x=>String(x.master_task_id)==master
        && (!cats.length || cats.includes(String(x.category_id))));
    const taskIds=[...new Set(rows.map(x=>String(x.task_name_id)))];
    const keepTask=document.getElementById("task_name_id").value;
    opts("task_name_id", REF.task_names.filter(t=>taskIds.includes(String(t.id))), "id","name","choose");
    if(taskIds.includes(String(keepTask))) document.getElementById("task_name_id").value=keepTask;
    fillSubTasks(keepSubs);
}}
function fillSubTasks(keepSubs){{
    const task=document.getElementById("task_name_id").value;
    const cats=ticked("category_ids");
    const byId=Object.fromEntries(REF.categories.map(c=>[String(c.id),c.name]));
    // One group per ticked Category, so a union across FLOOR + CEILING + WALL
    // still reads as three labelled blocks rather than one flat list.
    const groups=cats.map(cid=>({{label:byId[cid]||"",
        items:REF.task_sub_tasks.filter(s=>String(s.task_name_id)==task
            && String(s.category_id)==String(cid))}}));
    const loose=REF.task_sub_tasks.filter(s=>String(s.task_name_id)==task && s.category_id==null);
    if(loose.length) groups.push({{label:"",items:loose}});
    fillTicks("task_sub_task_ids","task_sub_task_ids",groups,keepSubs);
}}
document.getElementById("master_task_id").addEventListener("change", autofill);
document.getElementById("master_task_id").addEventListener("change", ()=>fillHierarchy());
document.getElementById("category_ids").addEventListener("change", ()=>fillTasks());
document.getElementById("task_name_id").addEventListener("change", ()=>fillSubTasks());
document.getElementById("project").onchange = fill;
document.getElementById("sheet_type_id").onchange = fill;
(function(){{
  const wd=document.getElementById("work_date");
  if(wd) wd.addEventListener("change", function(){{ fillPeople(); fill(); }});
}})();
(function(){{
  const people=document.getElementById("people");
  if(people) people.addEventListener("change", function(){{ fill(); fillHierarchy(); }});
}})();
document.getElementById("project").selectedIndex = 0; fill();
fillHierarchy();
function toMin(t){{
  if(!t) return null;
  var p = t.split(":");
  if(p.length<2) return null;
  return parseInt(p[0],10)*60 + parseInt(p[1],10);
}}
function fullDayBlocked(){{
  var form=document.querySelector("form");
  if(!form) return "";
  var date=form.work_date.value, end=toMin(form.end_time.value);
  var edit=(document.getElementById("edit_id")||{{}}).value||"";
  var mine=(MY_DAY||[]).filter(function(t){{
    return t.date===date && String(t.id)!==String(edit);
  }});
  var full=mine.filter(function(t){{ var m=toMin(t.end); return m!=null && m>=DAY_END_MIN; }});
  if(full.length){{
    return "You already have a task on "+date+" that runs to "+(full[0].end||"18:30")
      +" or later. Only one entry is allowed then. Split the day only when earlier "
      +"tasks finish before 18:30 (for example 14:00, then 16:00, then 18:30).";
  }}
  return "";
}}
function breakMins(code, sm, em){{
  var wins = (REF.breaks||[]).filter(function(b){{ return b.project_code==code; }});
  if(!wins.length) wins = [
    {{start_time:"11:15", end_time:"11:30"}},
    {{start_time:"12:45", end_time:"13:30"}},
    {{start_time:"16:15", end_time:"16:30"}}
  ];
  var brk = 0;
  wins.forEach(function(w){{
    var a = toMin(w.start_time), b = toMin(w.end_time);
    brk += Math.max(0, Math.min(em,b) - Math.max(sm,a));
  }});
  return brk;
}}
function updateHours(){{
  var form = document.querySelector("form");
  var el = document.getElementById("hours_live");
  if(!form || !el) return;
  var sm = toMin(form.start_time.value), em = toMin(form.end_time.value);
  if(sm==null || em==null){{ el.textContent = "Set In and Out to see hours."; return; }}
  var ov = (form.break_override.value||"").trim();
  var brk = ov!=="" ? parseFloat(ov) : breakMins(form.project.value, sm, em);
  if(isNaN(brk)) brk = 0;
  var hours = Math.max(0, ((em - sm + 1440) % 1440) / 60 - brk / 60);
  var proj = REF.projects.find(function(x){{ return x.code==form.project.value; }});
  var day = (proj && proj.hours_per_day) ? Number(proj.hours_per_day) : 8.25;
  var md = day ? hours / day : 0;
  el.innerHTML = "Hours: <b>" + hours.toFixed(2) + "</b> &nbsp;·&nbsp; Man-day: <b>"
    + md.toFixed(3) + "</b> &nbsp;(" + day + " hrs = 1 day: 09:00–18:30 minus 75 min breaks)."
    + (hours > day + 0.001 ? " Extra time after 18:30 is counted." : "");
}}
document.querySelector("form").addEventListener("input", function(ev){{
  if(ev.target && (ev.target.name==="start_time" || ev.target.name==="end_time"
      || ev.target.name==="break_override" || ev.target.name==="project")) updateHours();
}});
document.getElementById("project").addEventListener("change", updateHours);
updateHours();
{sticky}
</script><script>
document.addEventListener("click", function(ev){{
  var b = ev.target.closest ? ev.target.closest("[data-tick-all],[data-tick-none]") : null;
  if(!b) return;
  var on = b.hasAttribute("data-tick-all");
  var id = b.getAttribute(on ? "data-tick-all" : "data-tick-none");
  var box = document.getElementById(id);
  if(!box) return;
  var boxes = box.querySelectorAll("input[type=checkbox]");
  [].forEach.call(boxes, function(c){{ c.checked = on; }});
  // Ticking Categories changes which Sub Tasks exist, so let the cascade rerun.
  if(id === "category_ids" && typeof fillTasks === "function") fillTasks();
}});
</script></body></html>"""


# Sticky form: remember this browser's last entry so the next task starts pre-filled
# (each teammate keeps their own last values). Injected as-is (not through .format).
STICKY_JS = r"""
(function(){
  var form=document.querySelector("form");
  var STATE_VERSION = 3;   // bump when the entry form's fields change shape
  function ticks(name){
    return [].slice.call(form.querySelectorAll('input[name='+name+']:checked'))
             .map(function(c){return c.value;});
  }
  function setIfPresent(name,val){
    var el=form.elements[name]; if(!el||val==null||val==="") return false;
    var found=[].slice.call(el.options||[]).some(function(o){return o.value===String(val);});
    if(found){ el.value=String(val); return true; }
    el.value=""; return false;          // stale id — fall back to the placeholder
  }
  function retick(name,vals){
    if(!vals) return;
    var want=vals.map(String);
    [].slice.call(form.querySelectorAll('input[name='+name+']'))
      .forEach(function(c){c.checked=want.indexOf(c.value)>=0;});
  }
  // Categories and Sub Tasks are rebuilt by the cascade, so the ticks have to be
  // restored through fillHierarchy rather than set on elements that don't exist yet.
  function restoreHierarchy(d){
    if(typeof fillHierarchy!=="function") return;
    fillHierarchy(d.category_ids||[], d.task_sub_task_ids||[]);
    if(setIfPresent("task_name_id", d.task_name_id) && typeof fillSubTasks==="function")
      fillSubTasks(d.task_sub_task_ids||[]);
  }
    var FIELDS=["work_date","project","sheet_type_id","master_task_id","building_id","level_id",
        "model_id","task_name_id","start_time","end_time","status_id","break_override",
    "description","pct_complete","notes"];
  function save(){
    var d={};
    FIELDS.forEach(function(n){var el=form.elements[n]; if(el) d[n]=el.value;});
    d.permission=form.elements["permission"].checked;
    d.grouped=form.elements["grouped"].checked;
    d.v=STATE_VERSION;
    d.assignees=ticks("assignees");
    d.category_ids=ticks("category_ids");
    d.task_sub_task_ids=ticks("task_sub_task_ids");
    try{localStorage.setItem("lastTask",JSON.stringify(d));}catch(e){}
  }
  form.addEventListener("submit", function(ev){
    save();
    if(typeof fullDayBlocked==="function"){
      var msg=fullDayBlocked();
      if(msg){ ev.preventDefault(); alert(msg); }
    }
  });
  window.clearForm=function(){try{localStorage.removeItem("lastTask");}catch(e){} location.href="/";};
  function restore(){
    var raw; try{raw=localStorage.getItem("lastTask");}catch(e){return;}
    if(!raw) return; var d; try{d=JSON.parse(raw);}catch(e){return;}
    if(d.v!==STATE_VERSION){   // saved by an older form — start clean
      try{localStorage.removeItem("lastTask");}catch(e){}
      return;
    }
    if(d.project) form.elements["project"].value=d.project;
    if(d.sheet_type_id) form.elements["sheet_type_id"].value=d.sheet_type_id;
    if(typeof fillPeople==="function") fillPeople();
    if(typeof fill==="function") fill();
    ["master_task_id","building_id","level_id","model_id","status_id"]
      .forEach(function(n){setIfPresent(n,d[n]);});
    ["work_date","start_time","end_time","break_override","description","pct_complete","notes"]
      .forEach(function(n){if(d[n]!==undefined&&form.elements[n])form.elements[n].value=d[n];});
    restoreHierarchy(d);
    form.elements["permission"].checked=!!d.permission;
    form.elements["grouped"].checked=!!d.grouped;
    retick("assignees", d.assignees);
    if(typeof fillPeople==="function") fillPeople();
  }
  function applyEdit(){
    var d=EDIT; if(!d) return;
    if(d.project) form.elements["project"].value=d.project;
    if(d.sheet_type_id!=null) form.elements["sheet_type_id"].value=d.sheet_type_id;
    if(typeof fillPeople==="function") fillPeople();
    if(typeof fill==="function") fill();
    ["master_task_id","building_id","level_id","model_id","status_id"]
      .forEach(function(n){setIfPresent(n,d[n]);});
    ["work_date","start_time","end_time","break_override","description","pct_complete","notes"]
      .forEach(function(n){if(d[n]!=null&&form.elements[n])form.elements[n].value=d[n];});
    restoreHierarchy(d);
    form.elements["permission"].checked=!!d.permission;
    form.elements["grouped"].checked=!!d.grouped;
    retick("assignees", d.assignees);
    if(typeof fillPeople==="function") fillPeople();
    var btn=form.querySelector('button[type=submit]'); if(btn) btn.textContent="Update task #"+d.id;
  }
  if (typeof EDIT !== "undefined" && EDIT) { applyEdit(); } else { restore(); }
  if (typeof updateHours === "function") updateHours();
})();
"""


# --------------------------------------------------------------------------- #
# Admin page — add projects / buildings / models / master tasks / people / etc.
# --------------------------------------------------------------------------- #
ADMIN_STYLE = """<style>
  :root{--ink:#1a2230;--muted:#5b6675;--rule:#d5dbe3;--accent:#1f5a86;--ok:#1f7a4d;--bg:#f5f7fa;--card:#fff}
  *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--ink);
    font:15px/1.5 "Segoe UI",system-ui,Arial,sans-serif}
  .wrap{max-width:1100px;margin:0 auto;padding:24px 18px 60px}
  h1{font-size:20px;margin:0 0 4px} .sub{color:var(--muted);font-size:13px;margin:0 0 18px}
  .nav{margin-bottom:14px;font-size:13px} .nav a{color:var(--accent);text-decoration:none} .nav b{color:var(--ink)}
  .cardnav{position:sticky;top:0;z-index:30;background:var(--bg);padding:8px 0 12px;margin:0 0 14px;
    border-bottom:1px solid var(--rule)}
  .cardnav .groups,.cardnav .pills{display:flex;flex-wrap:wrap;gap:6px}
  .cardnav .groups{margin-bottom:8px}
  .cardnav button{background:#fff;color:var(--accent);border:1px solid var(--rule);border-radius:999px;
    padding:6px 12px;font:inherit;font-size:13px;font-weight:600;cursor:pointer}
  .cardnav button:hover{filter:brightness(1.04)}
  .cardnav button.on{background:var(--accent);color:#fff;border-color:var(--accent)}
  .card.hidden-card{display:none}
  .whoami{font-size:12px;color:var(--muted);margin:0 0 14px;padding:6px 10px;background:var(--card);
    border:1px solid var(--rule);border-radius:6px;display:inline-block} .whoami b{color:var(--ink)}
  .ok{background:#e6f4ec;border:1px solid var(--ok);color:var(--ok);padding:10px 14px;border-radius:8px;margin-bottom:16px;font-weight:600}
  .card{background:var(--card);border:1px solid var(--rule);border-radius:10px;padding:14px 16px;margin-bottom:14px}
  .card h2{font-size:15px;margin:0 0 4px} .card .now{color:var(--muted);font-size:12.5px;margin:0 0 10px}
  .bulk{margin-left:10px;display:inline-flex;gap:6px;vertical-align:middle}
  .bulk button{background:none;border:1px solid var(--rule);color:var(--accent);
    border-radius:4px;padding:1px 8px;font-size:11px;cursor:pointer;text-transform:none;
    letter-spacing:0;font-weight:600}
  .bulk button:hover{background:var(--accent);color:#fff;border-color:var(--accent)}
  .teamwrap .teamlist{display:block}
  .teamwrap .teamlist[hidden]{display:none !important}
  .teamwrap .count{font-size:11px;text-transform:uppercase;letter-spacing:.04em;
    color:var(--muted);display:block;margin-bottom:6px}
  .teamwrap .tgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));
    gap:10px 16px;width:100%}
  .teamwrap .trow{display:flex;flex-wrap:wrap;align-items:end;gap:4px 8px;
    border:1px solid var(--rule);border-radius:8px;padding:8px}
  .teamwrap .tname{flex:1 1 100%;font-size:13.5px;font-weight:600;color:var(--text);
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .teamwrap .tdatefld{flex:1 1 110px;min-width:110px}
  .teamwrap .tdatefld label{margin-bottom:2px}
  .teamwrap .tdatefld input[type=date]{width:100%;padding:4px 6px;font-size:12.5px}
  .teamwrap .trow select{flex:1 1 120px;min-width:0;width:auto;padding:3px 6px;font-size:12.5px}
  .teamwrap .trow.gone{background:#f4f6f8;opacity:.95}
  .teamwrap .lefttag{font-size:11px;font-weight:600;color:var(--muted);
    text-transform:uppercase;letter-spacing:.04em;margin-left:8px}
  .teamwrap .abbrev{font-size:12px;color:var(--muted);margin:10px 0 0}
  .teamwrap .rostersec{margin-top:16px;padding-top:12px;border-top:1px solid var(--rule)}
  .teamwrap .rostersec h3{font-size:13px;margin:0 0 6px}
  .teamwrap .roster{width:100%;border-collapse:collapse;font-size:13.5px;margin:4px 0 8px}
  .teamwrap .roster th,.teamwrap .roster td{border:1px solid var(--rule);padding:6px 8px;
    text-align:left;vertical-align:middle}
  .teamwrap .roster th{background:#eef3f7;font-size:11px;letter-spacing:.04em;
    text-transform:uppercase;color:var(--muted)}
  .teamwrap .roster select,.teamwrap .roster input[type=date]{width:100%;padding:4px 6px;
    font-size:12.5px}
  .catgrid{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0 8px}
  .catchip{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--rule);
    border-radius:999px;padding:3px 4px 3px 10px;background:#fff;font-size:13px;max-width:100%}
  .catchip .nm{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:280px}
  .catchip form{display:inline;margin:0}
  .catchip button.rm{background:#a33;padding:2px 8px;font-size:11px;letter-spacing:.04em;
    min-width:32px;border-radius:999px}
  .catchip.locked{padding-right:10px;color:var(--muted)}
  .catnote{font-size:12px;color:var(--muted);margin:0 0 8px}
  label .hint{font-weight:400;text-transform:none;letter-spacing:0;font-size:11.5px;
    color:var(--muted);margin-left:6px}
  .stars{display:flex;align-items:center;gap:2px}
  .stars .star{background:none;border:0;padding:0 2px;cursor:pointer;font-size:22px;
    line-height:1;color:var(--rule)}
  .stars .star.on{color:#e0a400}
  .stars .star:hover{color:#e0a400}
  .stars .star.clear{font-size:16px;color:var(--muted);margin-left:6px}
  .stars .star.clear:hover{color:#a33}
  .rating{color:#e0a400;letter-spacing:1px}
  form.row{display:flex;flex-wrap:wrap;gap:8px;align-items:end}
  form.row .full{flex:1 1 100%;width:100%}
  .maptable{width:100%;border-collapse:collapse;font-size:13.5px;margin:4px 0 8px}
  .maptable th,.maptable td{border:1px solid var(--rule);padding:6px 8px;text-align:left;vertical-align:top}
  .maptable th{background:#eef3f7;font-size:11px;letter-spacing:.04em;text-transform:uppercase;color:var(--muted)}
  .maptable td.acts{white-space:nowrap;width:1%}
  .maptable button.rm{background:#a33;padding:3px 8px;font-size:11px;letter-spacing:.04em;min-width:32px}
  .mapedit{display:flex;flex-wrap:wrap;gap:8px;align-items:end;margin:0 0 8px}
  .mapedit button[type=button]{background:#1f7a4d}
  .visually-hidden{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0,0,0,0)}
  details.excelmap{margin-top:12px;border-top:1px solid var(--rule);padding-top:10px}
  details.excelmap summary{cursor:pointer;color:var(--accent);font-weight:600;font-size:13px}
  label{display:block;font-size:11px;letter-spacing:.02em;color:var(--muted);text-transform:uppercase;margin-bottom:3px;font-weight:600}
  input,select{padding:8px 9px;border:1px solid var(--rule);border-radius:7px;font:inherit;background:#fff;color:var(--ink)}
  input:focus,select:focus{outline:2px solid var(--accent);border-color:var(--accent)}
  button{background:var(--accent);color:#fff;border:0;border-radius:7px;padding:9px 16px;font-weight:600;cursor:pointer}
  button:hover{filter:brightness(1.08)} .grow{flex:1;min-width:180px}
  .qcmap{display:flex;gap:12px;align-items:stretch;width:100%;margin-top:8px;flex-wrap:wrap}
  .qcmap-col{flex:1;min-width:0}
  .qcmap-col.poolcol{flex:0 0 180px;min-width:160px}
  .qcmap-col.midcol{flex:2 1 360px;min-width:280px}
  .qchd{font-size:11px;letter-spacing:.04em;text-transform:uppercase;color:var(--muted);
    font-weight:700;margin:0 0 6px}
  .qcrow{display:flex;flex-direction:column;border:1px solid var(--rule);border-radius:8px;
    margin-bottom:10px;background:#fff;overflow:hidden}
  .qcrow.on{border-color:var(--accent);box-shadow:0 0 0 2px rgba(31,90,134,.18)}
  .qclead{background:#1f5a86;color:#fff;padding:8px 12px;font-weight:700;font-size:14px;
    text-align:left;line-height:1.25}
  .qcrow-body{display:grid;grid-template-columns:1fr 1fr;min-height:58px}
  .qcrow.noz .qcrow-body{grid-template-columns:1fr}
  .qcdrop-lab{display:block;width:100%;flex:1 0 100%;font-size:10px;letter-spacing:.04em;
    text-transform:uppercase;color:var(--muted);font-weight:700;margin:0 0 4px}
  .qcarrow{display:none}
  .qcdrop,.qcpool{padding:8px;display:flex;flex-wrap:wrap;gap:6px;align-content:flex-start;
    min-height:52px;background:#fafbfc}
  .qcdrop{border-top:1px solid var(--rule)}
  .qcdrop + .qcdrop{border-left:1px solid var(--rule)}
  .qcdrop.over,.qcpool.over{outline:2px dashed var(--accent);background:#eef5fb}
  .qcpool{border:1px dashed var(--rule);border-radius:8px;min-height:160px}
  .qcchip{display:inline-flex;align-items:center;gap:4px;background:#fff;border:1px solid var(--accent);
    color:var(--ink);border-radius:16px;padding:3px 8px 3px 10px;font-size:12.5px;cursor:grab;
    user-select:none}
  .qcchip.zone{border-color:#1f7a4d;background:#f3faf6}
  .qcchip:active{cursor:grabbing}
  .qcx{all:unset;cursor:pointer;color:var(--muted);font-size:16px;padding:0 2px;line-height:1}
  .qcx:hover{color:#a33}
  .qcempty{color:var(--muted);font-size:12.5px;font-style:italic}
  .people.ticks{max-height:160px;overflow-y:auto;border:1px solid var(--rule);border-radius:6px;
    padding:8px;display:flex;flex-wrap:wrap;gap:6px 12px}
  .people.ticks label{display:flex;align-items:center;gap:6px;text-transform:none;font-size:13.5px;
    letter-spacing:0;color:var(--ink);font-weight:500;margin:0}
  .people.ticks input{width:auto}
</style>"""

# Mapping board: Zone → QC Reviewer → QC Support. JSON vars are prepended in render_admin.
QC_MAP_JS = r"""
var QC_ASSIGN={};
var QC_ZONES={};
var QC_SEL="";
function esc(s){return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/"/g,"&quot;");}
function qcIds(sel){return [].filter.call(document.querySelectorAll(sel),function(b){return b.checked;}).map(function(b){return b.value;});}
function qcStaff(){
  var seen={}, out=[];
  (LEAD_PEOPLE||[]).concat(SUPP_PEOPLE||[]).forEach(function(p){
    var id=String(p.id); if(seen[id]) return; seen[id]=1; out.push(p);
  });
  return out;
}
function qcName(list,id){var x=(list&&list.length?list:qcStaff()).filter(function(p){return String(p.id)===String(id);})[0];return x?x.name:id;}
function qcNoZone(){var p=document.getElementById("qc_project").value;return !!(typeof NOZONE!=="undefined" && NOZONE[p]);}
function qcChip(kind,id,label,lead){
  return '<span class="qcchip '+(kind==="zone"?"zone":"")+'" draggable="true" data-kind="'+kind
    +'" data-id="'+id+'" data-lead="'+(lead||'')+'">'
    +esc(label)+'<button type="button" class="qcx" title="Unassign">&times;</button></span>';
}
function qcHasZone(lead,zid){
  return !!(QC_ZONES[String(lead)] && QC_ZONES[String(lead)][String(zid)]);
}
function qcAddZone(lead,zid){
  var L=String(lead), z=String(zid);
  if(!QC_ZONES[L]) QC_ZONES[L]={};
  QC_ZONES[L][z]=true;
}
function qcRemoveZone(lead,zid){
  var L=String(lead);
  if(QC_ZONES[L]) delete QC_ZONES[L][String(zid)];
}
function writePairs(){
  var box=document.getElementById("qc_pair_inputs"); if(!box) return;
  var h="";
  Object.keys(QC_ASSIGN).forEach(function(sid){if(QC_ASSIGN[sid]) h+='<input type="hidden" name="pairs" value="'+QC_ASSIGN[sid]+':'+sid+'">';});
  Object.keys(QC_ZONES).forEach(function(lid){
    Object.keys(QC_ZONES[lid]||{}).forEach(function(zid){
      h+='<input type="hidden" name="zone_pairs" value="'+lid+':'+zid+'">';
    });
  });
  box.innerHTML=h;
}
function loadAssign(){
  var p=document.getElementById("qc_project").value;
  QC_ASSIGN={}; QC_ZONES={}; QC_SEL="";
  LEAD_SUPPORT.filter(function(r){return r.p===p;}).forEach(function(r){QC_ASSIGN[String(r.support)]=String(r.lead);});
  (typeof ZONE_MAP!=="undefined"?ZONE_MAP:[]).filter(function(r){return r.p===p;}).forEach(function(r){
    var L=String(r.lead), z=String(r.id);
    if(!QC_ZONES[L]) QC_ZONES[L]={};
    QC_ZONES[L][z]=true;
  });
}
function projectZones(){
  var p=document.getElementById("qc_project").value;
  return (typeof ZONE_BLD!=="undefined"?ZONE_BLD:[]).filter(function(b){
    return b.p===p && String(b.name||"").trim();
  }).slice().sort(function(a,b){
    var aa=Number(a.all)||0, bb=Number(b.all)||0;
    if(aa!==bb) return bb-aa;
    return String(a.name).localeCompare(String(b.name), undefined, {numeric:true});
  });
}
function teamByRole(role){
  var p=document.getElementById("qc_project").value;
  var ids=TEAM.filter(function(t){
    return t.p===p && String(t.role||"").toUpperCase()===role;
  }).map(function(t){return String(t.id);});
  var picked=qcStaff().filter(function(p){return ids.indexOf(String(p.id))>=0;});
  return picked.length?picked:qcStaff();
}
function fillMapSelects(){
  function fill(sel, items, empty){
    if(!sel) return;
    var keep=sel.value;
    sel.innerHTML='<option value="">'+empty+'</option>'+(items||[]).map(function(it){
      return '<option value="'+it.id+'">'+esc(it.name)+'</option>';
    }).join("");
    if(keep && [].some.call(sel.options, function(o){return o.value===keep;})) sel.value=keep;
  }
  fill(document.getElementById("map_zone"), projectZones(), "choose zone");
  fill(document.getElementById("map_reviewer"), teamByRole("QC REVIEWER"), "choose reviewer");
  fill(document.getElementById("map_support"), teamByRole("QC SUPPORT"), "choose support");
  fill(document.getElementById("map_support_lead"), teamByRole("QC REVIEWER"), "choose reviewer");
}
function supportIdsFor(lid){
  return Object.keys(QC_ASSIGN).filter(function(sid){return String(QC_ASSIGN[sid])===String(lid);});
}
function renderMapTable(){
  var box=document.getElementById("map_now"); if(!box) return;
  var staff=qcStaff();
  var zones=projectZones();
  if(!zones.length){
    box.innerHTML='<p class="now">No zones on this project yet. Add them under Zones, then come back here.</p>';
    writePairs(); return;
  }
  var rows=[];
  zones.forEach(function(b){
    var zid=String(b.id);
    var leads=[];
    Object.keys(QC_ZONES).forEach(function(lid){
      if(QC_ZONES[lid] && QC_ZONES[lid][zid]) leads.push(lid);
    });
    if(!leads.length){
      rows.push({lid:"", zid:zid, zone:b.name, lead:""});
      return;
    }
    leads.forEach(function(lid){
      rows.push({lid:lid, zid:zid, zone:b.name, lead:qcName(LEAD_PEOPLE,lid)});
    });
  });
  box.innerHTML='<table class="maptable"><thead><tr><th>Zone</th><th>QC Reviewer</th><th>QC Support</th><th></th></tr></thead><tbody>'
    +rows.map(function(r){
      var chips=r.lid?supportIdsFor(r.lid).map(function(sid){
        return '<span class="qcchip">'+esc(qcName(staff,sid))
          +'<button type="button" class="qcx" title="Remove support" data-kind="support" data-id="'+sid+'" data-lead="'+r.lid+'">&times;</button></span>';
      }).join(" "):"";
      if(!chips) chips='<span class="qcempty">none</span>';
      var lead=r.lead?esc(r.lead):'<span class="qcempty">none</span>';
      var rm=r.lid
        ?'<button type="button" class="rm maprm" data-kind="zone" data-id="'+r.zid+'" data-lead="'+r.lid+'" title="Remove this zone from this reviewer">RM</button>'
        :"";
      return '<tr><td>'+esc(r.zone)+'</td><td>'+lead+'</td><td>'+chips+'</td><td class="acts">'+rm+'</td></tr>';
    }).join("")+'</tbody></table>'
    +'<p class="catnote">Every zone on this project is listed. <b>RM</b> = take a mapped zone off that reviewer. '
    +'<b>&times;</b> on a name removes that QC Support. Assign missing rows below, then <b>Save mapping</b>.</p>';
  writePairs();
}
function renderQcMap(){
  var staff=qcStaff();
  var leadIds=qcIds("#qc_leads input[name=lead_ids]");
  var suppIds=staff.map(function(p){return String(p.id);});
  if(!leadIds.length) leadIds=LEAD_PEOPLE.map(function(p){return String(p.id);});
  Object.keys(QC_ASSIGN).forEach(function(sid){
    if(suppIds.indexOf(sid)<0) delete QC_ASSIGN[sid];
    else if(QC_ASSIGN[sid] && leadIds.indexOf(String(QC_ASSIGN[sid]))<0) delete QC_ASSIGN[sid];
    if(String(QC_ASSIGN[sid])===String(sid)) delete QC_ASSIGN[sid];
  });
  var zids=projectZones().map(function(b){return String(b.id);});
  Object.keys(QC_ZONES).forEach(function(lid){
    if(leadIds.indexOf(String(lid))<0){ delete QC_ZONES[lid]; return; }
    Object.keys(QC_ZONES[lid]||{}).forEach(function(zid){
      if(zids.indexOf(zid)<0) delete QC_ZONES[lid][zid];
    });
  });
  fillMapSelects();
  renderMapTable();
}
function loadQcTeam(){
  var p=document.getElementById("qc_project").value;
  var hint=document.getElementById("qc_hint");
  var teamed=!!USE_TEAMS[p];
  if(hint) hint.innerHTML=teamed
    ?"This project is grouped in teams. The table is the live mapping."
    :"This project is not grouped in teams yet — tick that on the Project card, then save. You can still map below.";
  var on=TEAM.filter(function(t){return t.p===p;});
  var leadOn=on.filter(function(t){return String(t.role||"").toUpperCase()==="QC REVIEWER";}).map(function(t){return String(t.id);});
  var suppOn=on.filter(function(t){return String(t.role||"").toUpperCase()==="QC SUPPORT";}).map(function(t){return String(t.id);});
  (typeof ZONE_MAP!=="undefined"?ZONE_MAP:[]).filter(function(r){return r.p===p;}).forEach(function(r){
    var id=String(r.lead); if(leadOn.indexOf(id)<0) leadOn.push(id);
  });
  LEAD_SUPPORT.filter(function(r){return r.p===p;}).forEach(function(r){
    if(leadOn.indexOf(String(r.lead))<0) leadOn.push(String(r.lead));
    if(suppOn.indexOf(String(r.support))<0) suppOn.push(String(r.support));
  });
  if(!leadOn.length && !suppOn.length){
    leadOn=on.map(function(t){return String(t.id);});
    suppOn=leadOn.slice();
  }
  [].forEach.call(document.querySelectorAll("#qc_leads input[name=lead_ids]"),function(b){b.checked=leadOn.indexOf(b.value)>=0;});
  [].forEach.call(document.querySelectorAll("#qc_support input[name=support_ids]"),function(b){b.checked=suppOn.indexOf(b.value)>=0;});
  [].forEach.call(document.querySelectorAll('select[name=project]'), function(sel){
    if(sel.id==="qc_project") return;
    var card=sel.closest && sel.closest("#team_map_excel");
    if(card && sel.value!==p) sel.value=p;
  });
  loadAssign(); renderQcMap();
}
function qcTick(boxId, id){
  var b=document.querySelector("#"+boxId+' input[value="'+id+'"]');
  if(b) b.checked=true;
}
function qcPlace(kind,id,lead,fromLead){
  if(kind==="support" && String(id)===String(lead)) return;
  if(kind==="zone"){
    if(lead) qcAddZone(lead, id);
    else qcRemoveZone(fromLead || QC_SEL, id);
  } else {
    if(lead) QC_ASSIGN[id]=lead; else delete QC_ASSIGN[id];
  }
  if(lead){ QC_SEL=lead; qcTick("qc_leads", lead); if(kind==="support") qcTick("qc_support", id); }
  renderQcMap();
}
(function(){
  var proj=document.getElementById("qc_project");
  if(!proj) return;
  proj.addEventListener("change",loadQcTeam);
  var leads=document.getElementById("qc_leads");
  if(leads) leads.addEventListener("change",renderQcMap);
  var supp=document.getElementById("qc_support");
  if(supp) supp.addEventListener("change",renderQcMap);
  var addZ=document.getElementById("map_add_zone");
  if(addZ) addZ.addEventListener("click",function(){
    var z=document.getElementById("map_zone").value;
    var lead=document.getElementById("map_reviewer").value;
    if(!z||!lead){ alert("Choose a Zone and a QC Reviewer."); return; }
    qcPlace("zone", z, lead);
  });
  var addS=document.getElementById("map_add_supp");
  if(addS) addS.addEventListener("click",function(){
    var sid=document.getElementById("map_support").value;
    var lead=document.getElementById("map_support_lead").value;
    if(!sid||!lead){ alert("Choose QC Support and the reviewer they work with."); return; }
    if(String(sid)===String(lead)){ alert("A person cannot be their own QC Support."); return; }
    qcPlace("support", sid, lead);
  });
  var table=document.getElementById("map_now");
  if(table) table.addEventListener("click",function(ev){
    var rm=ev.target.closest && ev.target.closest(".maprm,.qcx");
    if(!rm) return;
    qcPlace(rm.getAttribute("data-kind"), rm.getAttribute("data-id"), "", rm.getAttribute("data-lead"));
  });
  var team=document.getElementById("team_project");
  if(team){
    if(team.value) proj.value=team.value;
    team.addEventListener("change", function(){
      proj.value=team.value;
      loadQcTeam();
    });
  }
  loadQcTeam();
})();
"""


def _field(label, name, kind="text", opts=None, grow=False, extra=""):
    cls = ' class="grow"' if grow else ""
    if kind == "select":
        body = f'<select name="{name}"{cls} {extra}>{opts}</select>'
    else:
        body = f'<input type="{kind}" name="{name}"{cls} {extra}>'
    return f'<div{cls if grow else ""}><label>{label}</label>{body}</div>'


def render_admin(flash="", banner="", handler=None):
    con = connect()
    projects = con.execute("SELECT code,name,day_start,day_end,hours_per_day,"
                           "no_sheet_type,no_zone,project_status,use_teams FROM projects ORDER BY code").fetchall()
    sheet_types = con.execute("SELECT id,name FROM sheet_types ORDER BY name").fetchall()
    proj_opts = "".join(f'<option value="{html.escape(p["code"])}">{html.escape(p["code"])} — {html.escape(p["name"])}</option>' for p in projects if p["project_status"] == "Active")
    all_proj_opts = "".join(f'<option value="{html.escape(p["code"])}">{html.escape(p["code"])} — {html.escape(p["name"])} ({html.escape(p["project_status"])})</option>' for p in projects)
    project_json = json.dumps([dict(p) for p in projects])
    st_opts = "".join(f'<option value="{s["id"]}">{html.escape(s["name"])}</option>' for s in sheet_types)
    master_rows = con.execute("SELECT id,project_code,name FROM master_tasks ORDER BY project_code,name").fetchall()
    master_opts = "".join(
        f'<option value="{m["id"]}" data-proj="{html.escape(m["project_code"])}">'
        f'{html.escape(m["name"])}</option>'
        for m in master_rows)

    cat_rows = con.execute("SELECT id,name FROM categories ORDER BY name").fetchall()
    cat_opts = "".join(f'<option value="{c["id"]}">{html.escape(c["name"])}</option>' for c in cat_rows)
    core_rows = con.execute(
        "SELECT id,name FROM task_names WHERE name IN (?,?,?)", CORE_TASKS).fetchall()
    core_opts = "".join(f'<option value="{t["id"]}">{html.escape(t["name"])}</option>' for t in core_rows)

    roles = con.execute("SELECT id, name FROM roles ORDER BY name").fetchall()
    role_opts = "".join(f'<option value="{r["id"]}">{html.escape(r["name"])}</option>' for r in roles)
    role_opts_any = '<option value="">(no role)</option>' + role_opts
    ppl = con.execute("SELECT pe.id, pe.name, pe.role_id, r.name rn, pe.skill_rating, "
                      "pe.experience_years, pe.location FROM people pe "
                      "LEFT JOIN roles r ON r.id=pe.role_id ORDER BY pe.name").fetchall()
    people_opts = "".join(f'<option value="{p["id"]}">{html.escape(p["name"])}</option>' for p in ppl)

    def _person_summary(p):
        bits = [html.escape(p["rn"]) if p["rn"] else "no role"]
        if p["location"]:
            bits.append(html.escape(p["location"]))
        if p["skill_rating"]:
            n = int(p["skill_rating"])
            bits.append(f'<span class="rating">{"&#9733;" * n}{"&#9734;" * (5 - n)}</span>')
        if p["experience_years"] is not None:
            years = float(p["experience_years"])
            bits.append(f'{years:g} yr' + ("" if years == 1 else "s"))
        return f'{html.escape(p["name"])} ({", ".join(bits)})'

    people_with_roles = ", ".join(_person_summary(p) for p in ppl) or "<i>none</i>"
    emps = con.execute("SELECT id,name,role_id,emp_code,category,skillset,bim_id,ip_address,email,contact,"
                       "skill_rating,experience_years,location FROM people ORDER BY name").fetchall()
    emp_json = json.dumps([dict(e) for e in emps])

    def items(table, col="name"):
        rows = con.execute(f"SELECT {col} FROM {table} ORDER BY {col}").fetchall()
        return ", ".join(html.escape(str(r[0])) for r in rows) or "<i>none yet</i>"

    forecast_app.ensure_public_holidays(con)
    holiday_chips = []
    for r in con.execute(
        "SELECT day, COALESCE(name,'') AS name FROM public_holidays ORDER BY day"
    ):
        qday = html.escape(r["day"], quote=True)
        label = html.escape(fmt_day(r["day"]))
        if r["name"]:
            label += " — " + html.escape(r["name"])
        holiday_chips.append(
            '<span class="catchip">'
            f'<span class="nm">{label}</span>'
            f'<form method="POST" action="/admin">'
            f'<input type="hidden" name="action" value="delete_holiday">'
            f'<input type="hidden" name="day" value="{qday}">'
            f'<button type="submit" class="rm" title="Remove this holiday" '
            f"onclick=\"return confirm('Remove {qday} from public holidays?')\">RM</button></form></span>"
        )
    holiday_list = (
        f'<div class="catgrid">{"".join(holiday_chips)}</div>'
        if holiday_chips else '<i>none yet</i>'
    )

    def removable_scoped(table, action, kind_label):
        """Per-project chips with RM, one hidden block each — the card's Project
        select reveals the matching one."""
        extra = ", is_all" if table == "buildings" else ""
        out = []
        for p in projects:
            code = p["code"]
            rows = con.execute(
                f"SELECT id, name{extra} FROM {table} WHERE project_code=? ORDER BY name",
                (code,)).fetchall()
            if rows:
                chips = []
                for r in rows:
                    name = r["name"] or ""
                    if table == "buildings" and r["is_all"]:
                        chips.append(
                            f'<span class="catchip locked"><span class="nm">'
                            f'{html.escape(name)}</span></span>')
                        continue
                    qname = html.escape(name, quote=True)
                    chips.append(
                        '<span class="catchip">'
                        f'<span class="nm">{html.escape(name)}</span>'
                        f'<form method="POST" action="/admin">'
                        f'<input type="hidden" name="action" value="{action}">'
                        f'<input type="hidden" name="id" value="{r["id"]}">'
                        f'<input type="hidden" name="project" value="{html.escape(code)}">'
                        f'<button type="submit" class="rm" title="Remove this {kind_label}" '
                        f"onclick=\"return confirm('Remove {qname} from {html.escape(code)}? "
                        f"Logged tasks that used it stay.')\">RM</button></form></span>")
                body = (f'<div class="catgrid">{"".join(chips)}</div>'
                        f'<p class="catnote"><b>RM</b> = Remove this {kind_label}. '
                        'A name already used on a task cannot be removed.</p>')
            else:
                body = ' <i>none</i>'
            out.append(f'<div class="projlist" data-proj="{html.escape(code)}" hidden>'
                       f'<b>{html.escape(code)}</b>{body}</div>')
        return "".join(out) or "<i>no projects yet</i>"

    def removable_levels():
        out = []
        for p in projects:
            code = p["code"]
            rows = con.execute(
                "SELECT l.id, l.label, l.is_all FROM levels l "
                "JOIN project_levels pl ON pl.level_id=l.id "
                "WHERE pl.project_code=? "
                "ORDER BY (l.number IS NULL), l.number, l.label",
                (code,)).fetchall()
            if rows:
                chips = []
                for r in rows:
                    name = r["label"] or ""
                    if r["is_all"]:
                        chips.append(
                            f'<span class="catchip locked"><span class="nm">'
                            f'{html.escape(name)}</span></span>')
                        continue
                    qname = html.escape(name, quote=True)
                    chips.append(
                        '<span class="catchip">'
                        f'<span class="nm">{html.escape(name)}</span>'
                        f'<form method="POST" action="/admin">'
                        f'<input type="hidden" name="action" value="delete_level">'
                        f'<input type="hidden" name="id" value="{r["id"]}">'
                        f'<input type="hidden" name="project" value="{html.escape(code)}">'
                        f'<button type="submit" class="rm" title="Remove this Level from this project" '
                        f"onclick=\"return confirm('Remove {qname} from {html.escape(code)}? "
                        f"Logged tasks that used it stay.')\">RM</button></form></span>")
                body = (f'<div class="catgrid">{"".join(chips)}</div>'
                        f'<p class="catnote"><b>RM</b> = take this Level off this project. '
                        'A name already used on a task cannot be removed.</p>')
            else:
                body = ' <i>none</i>'
            out.append(f'<div class="projlist" data-proj="{html.escape(code)}" hidden>'
                       f'<b>{html.escape(code)}</b>{body}</div>')
        return "".join(out) or "<i>no projects yet</i>"

    mt = con.execute("SELECT project_code, COUNT(*) c FROM master_tasks GROUP BY project_code").fetchall()
    mt_summary = ", ".join(f'{html.escape(r["project_code"])} = {r["c"]}' for r in mt) or "none yet"

    # A project can set Sheet Type or Zone to "None". When no Active project
    # uses one of them, its Manage Lists card is just clutter, so drop it.
    active = [p for p in projects if p["project_status"] == "Active"]
    hide_sheet_type = bool(active) and all(p["no_sheet_type"] for p in active)
    hide_zone = bool(active) and all(p["no_zone"] for p in active)

    def card(title, now, form_inner, action):
        return (f'<div class="card"><h2>{title}</h2><p class="now">{now}</p>'
                f'<form class="row" method="POST" action="/admin">'
                f'<input type="hidden" name="action" value="{action}">{form_inner}'
                f'<button type="submit">Add</button></form></div>')

    first_logged = {}
    for r in con.execute(
        "SELECT t.project_code, ta.person_id, MIN(t.work_date) AS first_day "
        "FROM tasks t JOIN task_assignees ta ON ta.task_id = t.id "
        "WHERE ta.person_id IS NOT NULL "
        "GROUP BY t.project_code, ta.person_id"
    ):
        first_logged[(r["project_code"], r["person_id"])] = r["first_day"]

    all_team_rows = con.execute(
        "SELECT pp.id, pp.project_code, pp.person_id, pp.role_id, pp.joined_on, pp.left_on, "
        "COALESCE(r.name,'') AS role "
        "FROM project_people pp LEFT JOIN roles r ON r.id=pp.role_id").fetchall()
    current_team = [r for r in all_team_rows if not r["left_on"]]
    team_json = json.dumps([{"p": r["project_code"], "id": r["person_id"],
                             "role": r["role"] or ""} for r in current_team])
    team_boxes = "".join(
        f'<label><input type="checkbox" name="person_ids" value="{p["id"]}">{html.escape(p["name"])}</label>'
        for p in ppl)

    by_person = {p["id"]: p for p in ppl}
    by_proj = {}
    for r in all_team_rows:
        by_proj.setdefault(r["project_code"], []).append({
            "id": r["id"], "person_id": r["person_id"], "role_id": r["role_id"],
            "joined_on": r["joined_on"], "left_on": r["left_on"], "role": r["role"] or "",
            "fresh": False,
        })

    def member_card(r):
        person = by_person.get(r["person_id"])
        if not person:
            return ""
        rid = r.get("id")
        pid = r["person_id"]
        name = person["name"]
        qname = html.escape(name, quote=True)
        join_val = html.escape(r.get("joined_on") or "")
        left_val = html.escape(r.get("left_on") or "")
        role_html = (
            f'<select name="role_{pid}">'
            + "".join(
                f'<option value="{role["id"]}"'
                f'{" selected" if r.get("role_id") == role["id"] else ""}>{html.escape(role["name"])}</option>'
                for role in roles)
            + f'<option value=""{" selected" if not r.get("role_id") else ""}>(no role)</option>'
            '</select>'
        )
        rm = (
            f'<button type="submit" name="remove_row" value="{rid}" class="rm" '
            f'title="Remove from this project" '
            f"onclick=\"var x=this.form.elements['exitrow_{rid}'];"
            f"if(!x||!x.value){{alert('Enter Left on first. Leave it blank while they are still on this project.');return false;}}"
            f"return confirm('Remove {qname} from this project from '+x.value+'? "
            f"Logged tasks stay. They remain an employee for other projects.')\">"
            "RM</button>"
        )
        return (
            f'<div class="trow">'
            f'<span class="tname">{html.escape(name)}</span>'
            f'<div class="tdatefld"><label>Joined on</label>'
            f'<input type="date" name="joinrow_{rid}" value="{join_val}" required></div>'
            f'<div class="tdatefld"><label>Left on</label>'
            f'<input type="date" name="exitrow_{rid}" value="{left_val}"></div>'
            f'{role_html}{rm}</div>'
        )

    def other_row(r):
        person = by_person.get(r["person_id"])
        if not person:
            return ""
        rid = r.get("id")
        pid = r["person_id"]
        former = bool(r.get("left_on")) and not r.get("fresh")
        join_name = f"joinrow_{rid}" if rid else f"joinnew_{pid}"
        exit_name = f"exitrow_{rid}" if rid else f"exitnew_{pid}"
        st_name = f"strow_{rid}" if rid else f"stnew_{pid}"
        join_val = html.escape(r.get("joined_on") or "")
        left_val = html.escape(r.get("left_on") or "")
        if former:
            status = (
                f'<select name="{st_name}">'
                '<option value="left" selected>Left</option>'
                '<option value="joined">Joined</option>'
                '</select>'
            )
        else:
            status = (
                f'<select name="{st_name}">'
                '<option value="">—</option>'
                '<option value="joined">Joined</option>'
                '<option value="left">Left</option>'
                '</select>'
            )
        return (
            f'<tr>'
            f'<td>{html.escape(person["name"])}</td>'
            f'<td>{status}</td>'
            f'<td><input type="date" name="{join_name}" value="{join_val}"></td>'
            f'<td><input type="date" name="{exit_name}" value="{left_val}"></td>'
            f'</tr>'
        )

    def person_name(pid):
        p = by_person.get(pid)
        return ((p["name"] if p else "") or "").lower()

    team_saved = ""
    for proj in projects:
        code = proj["code"]
        members = list(by_proj.get(code, []))
        seen = {m["person_id"] for m in members}
        for p in ppl:
            if p["id"] in seen:
                continue
            members.append({
                "id": None, "person_id": p["id"], "role_id": None,
                "joined_on": first_logged.get((code, p["id"])) or "",
                "left_on": "", "role": "", "fresh": True,
            })
        assigned = [m for m in members if not m.get("left_on") and not m.get("fresh")]
        others = [m for m in members if m.get("left_on") or m.get("fresh")]
        assigned.sort(key=lambda r: person_name(r["person_id"]))
        others.sort(key=lambda r: person_name(r["person_id"]))
        cards = "".join(member_card(r) for r in assigned)
        rows = "".join(other_row(r) for r in others)
        parts = []
        if cards:
            parts.append(
                f'<span class="count">{len(assigned)} assigned</span>'
                f'<div class="tgrid">{cards}</div>'
                '<p class="abbrev">People still on the project keep <b>Left on</b> empty. '
                'To take someone off now, fill Left on and click <b>RM</b>, or use the table below.</p>')
        else:
            parts.append('<span class="none">Nobody assigned yet on this project.</span>')
        if rows:
            parts.append(
                f'<div class="rostersec">'
                f'<h3>Not on this project</h3>'
                f'<span class="count">{len(others)} people</span>'
                '<table class="roster"><thead><tr>'
                '<th>Name</th><th>Status</th><th>Joined on</th><th>Left on</th>'
                '</tr></thead><tbody>'
                f'{rows}</tbody></table>'
                '<p class="abbrev">Set status to <b>Joined</b> or <b>Left</b>, fill the matching date(s), '
                'then <b>Save roles and dates</b>. Leave status as — if you are not recording them.</p>'
                '</div>')
        body = "".join(parts)
        team_saved += (f'<div class="teamlist" data-proj="{html.escape(code)}" hidden>'
                       f'{body}</div>')
    team_card = (
        '<div class="card"><h2>Project team</h2>'
        '<p class="now">Who appears in <b>Assignees</b> on the entry form. '
        'Role here is <b>for this project only</b> (QC Reviewer, QC Support, …). '
        'Put the <b>real join date</b> when you add someone. Leave <b>Left on</b> empty while they continue. '
        'Fill Left on only when they leave — even if you update this list a day or two late. '
        'They stay on the team through that date and drop off from the <b>next working day</b> '
        '(Saturday and Sunday skipped). Left on the 5th (Saturday) means they are gone from Monday the 7th. '
        '<b>Who didn\'t fill</b> uses those dates, not the day you clicked Save. '
        'Do not delete the employee. Logged tasks stay. '
        'Map <b>zones to QC Reviewer and QC Support</b> on the <b>Team mapping</b> card in this same People group.</p>'
        '<form class="row" method="POST" action="/admin">'
        '<input type="hidden" name="action" value="project_team">'
        f'<div><label>Project</label><select name="project" id="team_project" required>{all_proj_opts}</select></div>'
        f'<div><label>Joined on (new ticks)</label><input type="date" name="joined_on" value="{today_iso()}" required></div>'
        f'<div><label>Left on (only if you untick someone)</label><input type="date" name="left_on"></div>'
        '<div class="full"><label>Team members'
        '<span class="bulk"><button type="button" data-tick-all="team_people">All</button>'
        '<button type="button" data-tick-none="team_people">None</button></span></label>'
        f'<div class="people ticks" id="team_people">{team_boxes}</div></div>'
        '<button type="submit">Save team</button></form>'
        '<form class="row" method="POST" action="/admin">'
        '<input type="hidden" name="action" value="team_roles">'
        '<input type="hidden" name="project" id="roles_project" value="">'
        '<div class="full"><label>Saved team &mdash; role and dates on this project'
        '<span class="hint">(QC Reviewer / QC Support apply only to the project selected above)</span>'
        f'</label><div class="teamwrap">{team_saved}</div></div>'
        '<button type="submit" id="save_roles">Save roles and dates</button></form>'
        f'<script>var TEAM={team_json};'
        'function loadTeam(){'
        'var p=document.getElementById("team_project").value;'
        'var rp=document.getElementById("roles_project"); if(rp) rp.value=p;'
        'var on=TEAM.filter(function(t){return t.p===p;}).map(function(t){return String(t.id);});'
        'var bs=document.querySelectorAll("#team_people input[name=person_ids]");'
        '[].forEach.call(bs,function(b){b.checked=on.indexOf(b.value)>=0;});'
        'var ls=document.querySelectorAll(".teamlist");'
        '[].forEach.call(ls,function(d){var show=d.getAttribute("data-proj")===p;'
        'd.hidden=!show;'
        '[].forEach.call(d.querySelectorAll("select,button.rm,input"),function(sl){sl.disabled=!show;});});'
        'var sr=document.getElementById("save_roles");'
        'if(sr){var vis=false;'
        '[].forEach.call(ls,function(d){if(!d.hidden&&(d.querySelector(".trow")||d.querySelector(".roster")))vis=true;});'
        'sr.style.display=vis?"":"none";}}'
        'document.getElementById("team_project").addEventListener("change",loadTeam);'
        'loadTeam();</script>'
        '</div>')

    lead_boxes = "".join(
        f'<label><input type="checkbox" name="lead_ids" value="{p["id"]}">{html.escape(p["name"])}</label>'
        for p in ppl) or '<span class="none">No employees yet.</span>'
    supp_boxes = "".join(
        f'<label><input type="checkbox" name="support_ids" value="{p["id"]}">{html.escape(p["name"])}</label>'
        for p in ppl) or '<span class="none">No employees yet.</span>'
    use_teams_json = json.dumps({p["code"]: int(p["use_teams"] or 0) for p in projects})
    leads_json = json.dumps([{"id": p["id"], "name": p["name"]} for p in ppl])
    supps_json = json.dumps([{"id": p["id"], "name": p["name"]} for p in ppl])
    map_json = json.dumps([{"p": r["project_code"], "lead": r["lead_id"], "support": r["support_id"]}
                           for r in con.execute(
                               "SELECT project_code,lead_id,support_id FROM lead_support")])
    bld_json = json.dumps([{"p": r["project_code"], "id": r["id"], "name": r["name"],
                            "all": int(r["is_all"] or 0)}
                           for r in con.execute(
                               "SELECT id,project_code,name,is_all FROM buildings "
                               "WHERE TRIM(COALESCE(name,''))<>'' "
                               "ORDER BY is_all DESC, name")])
    zone_map_json = json.dumps([{"p": r["project_code"], "lead": r["person_id"], "id": r["item_id"]}
                                for r in con.execute(
                                    "SELECT project_code,person_id,item_id FROM lead_scope WHERE kind='zone'")])
    no_zone_json = json.dumps({p["code"]: int(p["no_zone"] or 0) for p in projects})
    teams_card = ""
    map_excel_forms = (
        '<form class="row" method="GET" action="/team-map-template.xlsx">'
        f'<div><label>Project</label><select name="project" required>{all_proj_opts}</select></div>'
        '<button type="submit">Download mapping template</button></form>'
        '<form class="row" method="POST" action="/bulk-team-map" enctype="multipart/form-data">'
        '<div><label>Mapping Excel file</label><input type="file" name="xlsx" accept=".xlsx" required></div>'
        f'<div><label>Project</label><select name="project" required>{all_proj_opts}</select></div>'
        '<button type="submit">Upload mapping</button></form>'
    )
    map_excel_card = (
        '<div class="card" id="team_map_excel"><h2>Team mapping</h2>'
        '<p class="now">Live <b>Zone → QC Reviewer → QC Support</b> for the project. '
        'Change it here, then <b>Save mapping</b>. Excel is optional (download / upload at the bottom).</p>'
        '<form class="row" method="POST" action="/admin" id="qc_team_form">'
        '<input type="hidden" name="action" value="qc_team">'
        f'<div><label>Project</label><select name="project" id="qc_project" required>{all_proj_opts}</select></div>'
        '<p class="now" id="qc_hint"></p>'
        '<div class="full"><label>Current mapping</label><div id="map_now"></div></div>'
        '<div class="full"><label>Assign a zone</label>'
        '<div class="mapedit">'
        '<div><label>Zone</label><select id="map_zone"></select></div>'
        '<div><label>QC Reviewer</label><select id="map_reviewer"></select></div>'
        '<button type="button" id="map_add_zone">Assign zone</button>'
        '</div></div>'
        '<div class="full"><label>Assign QC Support</label>'
        '<div class="mapedit">'
        '<div><label>QC Support</label><select id="map_support"></select></div>'
        '<div><label>Works with reviewer</label><select id="map_support_lead"></select></div>'
        '<button type="button" id="map_add_supp">Assign support</button>'
        '</div></div>'
        '<div id="qc_pair_inputs"></div>'
        '<div class="visually-hidden">'
        f'<div class="people ticks" id="qc_leads">{lead_boxes}</div>'
        f'<div class="people ticks" id="qc_support">{supp_boxes}</div>'
        '</div>'
        '<button type="submit">Save mapping</button></form>'
        '<details class="excelmap"><summary>Excel download / upload</summary>'
        '<p class="now">Columns: <b>Zone</b> | <b>QC Reviewer</b> | <b>QC Support</b>. '
        'Use Emp ID if two people share a name.</p>'
        + map_excel_forms +
        '</details>'
        f'<script>var TEAM={team_json};var USE_TEAMS={use_teams_json};'
        f'var LEAD_PEOPLE={leads_json};var SUPP_PEOPLE={supps_json};var LEAD_SUPPORT={map_json};'
        f'var ZONE_BLD={bld_json};var ZONE_MAP={zone_map_json};var NOZONE={no_zone_json};'
        f'{QC_MAP_JS}</script>'
        '</div>'
    )

    lead_opts = "".join(
        f'<option value="{p["id"]}">{html.escape(p["name"])}</option>'
        for p in ppl) or '<option value="">No employees yet</option>'
    cat_boxes = "".join(
        f'<label><input type="checkbox" name="category_ids" value="{c["id"]}">{html.escape(c["name"])}</label>'
        for c in cat_rows) or '<span class="none">No categories yet.</span>'
    lvl_json = json.dumps([{"p": r["project_code"], "id": r["id"], "name": r["label"]}
                           for r in con.execute(
                               "SELECT l.id, l.label, pl.project_code FROM levels l "
                               "JOIN project_levels pl ON pl.level_id=l.id "
                               "ORDER BY (l.number IS NULL), l.number")])
    scope_json = json.dumps([{"p": r["project_code"], "lead": r["person_id"],
                              "kind": r["kind"], "id": r["item_id"]}
                             for r in con.execute(
                                 "SELECT project_code,person_id,kind,item_id FROM lead_scope")])
    no_zone_json = json.dumps({p["code"]: int(p["no_zone"] or 0) for p in projects})
    lead_ids_json = json.dumps([p["id"] for p in ppl])
    scope_card = (
        '<div class="card"><h2>Lead responsibilities</h2>'
        '<p class="now"><b>Zones</b> are mapped in Team mapping. '
        'Here, set each lead’s <b>levels</b> and <b>task sets</b>. Leave a group clear '
        'if that lead can work across all of that kind.</p>'
        '<form class="row" method="POST" action="/admin">'
        '<input type="hidden" name="action" value="lead_scope">'
        f'<div><label>Project</label><select name="project" id="scope_project" required>{all_proj_opts}</select></div>'
        f'<div><label>Team lead</label><select name="person_id" id="scope_lead" required>{lead_opts}</select></div>'
        '<p class="now" id="scope_hint"></p>'
        '<div class="full"><label>Levels'
        '<span class="bulk"><button type="button" data-tick-all="scope_levels">All</button>'
        '<button type="button" data-tick-none="scope_levels">None</button></span></label>'
        '<div class="people ticks" id="scope_levels"></div></div>'
        '<div class="full"><label>Task sets (categories)'
        '<span class="bulk"><button type="button" data-tick-all="scope_cats">All</button>'
        '<button type="button" data-tick-none="scope_cats">None</button></span></label>'
        f'<div class="people ticks" id="scope_cats">{cat_boxes}</div></div>'
        '<button type="submit">Save responsibilities</button></form>'
        f'<script>var TEAM={team_json};var SCOPE={scope_json};var BLD={bld_json};'
        f'var LVL={lvl_json};var NOZONE={no_zone_json};var LEAD_IDS={lead_ids_json};'
        'function ticks(id,name,items,on){'
        'var box=document.getElementById(id); if(!box) return;'
        'box.innerHTML=items.length'
        '?items.map(function(i){return "<label><input type=\\"checkbox\\" name=\\""+name+"\\" value=\\""+i.id+"\\""'
        '+(on.indexOf(String(i.id))>=0?" checked":"")+">"+i.name+"</label>";}).join("")'
        ':"<span class=\\"none\\">None on this project.</span>";}'
        'function loadLeadScope(){'
        'var p=document.getElementById("scope_project").value;'
        'var team=TEAM.filter(function(t){return t.p===p;}).map(function(t){return String(t.id);});'
        'var sel=document.getElementById("scope_lead");'
        'var keep=sel.value;'
        'var leads=TEAM.filter(function(t){return t.p===p && String(t.role||"").toUpperCase()==="QC REVIEWER";}).map(function(t){return String(t.id);});'
        'if(!leads.length) leads=TEAM.filter(function(t){return t.p===p;}).map(function(t){return String(t.id);});'
        'if(!leads.length) leads=LEAD_IDS.slice();'
        '[].forEach.call(sel.options,function(o){'
        'var show=!o.value || leads.some(function(id){return String(id)===String(o.value);});'
        'o.hidden=!show;});'
        'if(keep && [].some.call(sel.options,function(o){return o.value===keep && !o.hidden;})) sel.value=keep;'
        'else {var first=[].filter.call(sel.options,function(o){return o.value && !o.hidden;})[0];'
        'if(first) sel.value=first.value;}'
        'var lead=sel.value;'
        'var on=function(kind){return SCOPE.filter(function(s){return s.p===p && String(s.lead)===String(lead) && s.kind===kind;}).map(function(s){return String(s.id);});};'
        'ticks("scope_levels","level_ids",LVL.filter(function(l){return l.p===p;}).map(function(l){return {id:l.id,name:l.name};}), on("level"));'
        '[].forEach.call(document.querySelectorAll("#scope_cats input[name=category_ids]"),'
        'function(b){b.checked=on("category").indexOf(b.value)>=0;});'
        'var hint=document.getElementById("scope_hint");'
        'if(hint) hint.textContent="Zones are mapped on Team mapping. Tick levels and task sets here.";}'
        'document.getElementById("scope_project").addEventListener("change",loadLeadScope);'
        'document.getElementById("scope_lead").addEventListener("change",loadLeadScope);'
        'loadLeadScope();</script>'
        '</div>')

    zone_bulk = ('<form class="row" method="POST" action="/bulk-zones" enctype="multipart/form-data">'
                 '<div><label>Zone Excel file</label><input type="file" name="xlsx" accept=".xlsx" required></div>'
                 '<div><label>Project</label><select name="project" required>' + proj_opts + '</select></div>'
                 '<button type="submit">Upload Zones</button></form>'
                 '<p class="now">Excel: one Zone column, one zone per row.</p>'
                 if handler is not None and is_manager(handler) else '')
    zone_card = "" if hide_zone else (
        '<div class="card" data-scoped="1"><h2>Zone</h2>'
        f'<div class="now">{removable_scoped("buildings", "delete_zone", "Zone")}</div>'
        '<form class="row" method="POST" action="/admin">'
        '<input type="hidden" name="action" value="building">'
        f'{_field("Project", "project", "select", proj_opts)} {_field("Zone name", "name", grow=True)}'
        '<button type="submit">Add</button></form>'
        f'{zone_bulk}'
        '</div>')

    sheet_type_card = "" if hide_sheet_type else card(
        "Sheet Type (shared)", "Now: " + items("sheet_types"),
        _field("Name", "name", grow=True), "sheet_type")

    employee_card = (
        '<div class="card"><h2>Employee</h2>'
        '<p class="now">Add a new employee, or pick one to edit. <b>Emp name</b> is required. '
        '<b>Role</b> here is app access (Manager, Team Leader). '
        'QC Reviewer / QC Support are set per project on Project team. Now: ' + people_with_roles + '</p>'
        '<form class="row" method="POST" action="/admin">'
        '<input type="hidden" name="action" value="employee">'
        '<input type="hidden" name="person_id" id="emp_pid" value="">'
        '<div><label>Load existing</label><select id="emp_load"><option value="">— new —</option>'
        + people_opts + '</select></div>'
        '<div class="grow"><label>Emp name *</label><input name="name" id="emp_name"></div>'
        '<div><label>Emp ID</label><input name="emp_code" id="emp_code"></div>'
        '<div><label>App access role</label><select name="role_id" id="emp_role">' + role_opts_any + '</select></div>'
        '<div><label>Location</label><select name="location" id="emp_location">'
        '<option value="">— choose —</option>'
        + "".join(f'<option value="{html.escape(loc)}">{html.escape(loc)}</option>'
                  for loc in OFFICE_LOCATIONS)
        + '</select></div>'
        '<div><label>Category</label><input name="category" id="emp_cat"></div>'
        '<div class="grow"><label>Skillset / Specialisation</label><input name="skillset" id="emp_skill"></div>'
        '<div><label>BIM ID</label><input name="bim_id" id="emp_bim"></div>'
        '<div><label>IP address</label><input name="ip_address" id="emp_ip"></div>'
        '<div><label>Email</label><input name="email" id="emp_email"></div>'
        '<div><label>Contact number</label><input name="contact" id="emp_contact"></div>'
        '<div><label>Experience (years)</label>'
        '<input name="experience_years" id="emp_exp" type="number" min="0" max="60" step="0.5"'
        ' placeholder="e.g. 4.5" style="width:120px"></div>'
        '<div><label>Skill rating</label><div class="stars" id="emp_stars">'
        '<input type="hidden" name="skill_rating" id="emp_rating" value="">'
        '<button type="button" class="star" data-v="1" aria-label="1 star">&#9733;</button>'
        '<button type="button" class="star" data-v="2" aria-label="2 stars">&#9733;</button>'
        '<button type="button" class="star" data-v="3" aria-label="3 stars">&#9733;</button>'
        '<button type="button" class="star" data-v="4" aria-label="4 stars">&#9733;</button>'
        '<button type="button" class="star" data-v="5" aria-label="5 stars">&#9733;</button>'
        '<button type="button" class="star clear" data-v="" title="Clear rating">&times;</button>'
        '</div></div>'
        '<button type="submit">Save employee</button>'
        '</form>'
        '<p class="now" style="margin-top:14px">Set <b>Location</b> for everyone from Excel. '
        'Download fills Location from IP (<b>192.168.1.x = Chennai</b>, '
        '<b>192.168.50.x = Vizag</b>). Check the sheet, then upload.</p>'
        '<form class="row" method="GET" action="/employee-locations.xlsx">'
        '<button type="submit">Download location sheet</button></form>'
        '<form class="row" method="POST" action="/bulk-employee-locations" enctype="multipart/form-data">'
        '<div><label>Location Excel file</label><input type="file" name="xlsx" accept=".xlsx" required></div>'
        '<button type="submit">Upload locations</button></form>'
        + (('<form class="row" method="POST" action="/admin" onsubmit="return confirm(\'Delete this employee? This cannot be undone.\')">'
            '<input type="hidden" name="action" value="delete_employee">'
            '<div><label>Delete employee</label><select name="person_id" required><option value="">Choose employee</option>'
            + people_opts + '</select></div><button type="submit" style="background:#a33">Delete Employee</button></form>')
              if handler is not None and is_manager(handler) else '')
          + '<script>var EMP=' + emp_json + ';document.getElementById("emp_load").addEventListener("change",'
        'function(){var e=EMP.filter(function(x){return String(x.id)===this.value;}.bind(this))[0]||{};'
        'function set(id,v){document.getElementById(id).value=v||"";}'
        'document.getElementById("emp_pid").value=e.id||"";set("emp_name",e.name);set("emp_code",e.emp_code);'
        'document.getElementById("emp_role").value=e.role_id||"";set("emp_location",e.location);'
        'set("emp_cat",e.category);'
        'set("emp_skill",e.skillset);set("emp_bim",e.bim_id);set("emp_ip",e.ip_address);'
        'set("emp_email",e.email);set("emp_contact",e.contact);set("emp_exp",e.experience_years);'
        'setStars(e.skill_rating);});'
        'function setStars(v){var has=(v!==null&&v!==undefined&&v!=="");'
        'document.getElementById("emp_rating").value=has?v:"";'
        'var bs=document.getElementById("emp_stars").querySelectorAll(".star");'
        '[].forEach.call(bs,function(b){var bv=b.getAttribute("data-v");'
        'if(bv==="")return;'
        'if(has&&Number(bv)<=Number(v))b.classList.add("on");else b.classList.remove("on");});}'
        'document.getElementById("emp_stars").addEventListener("click",function(ev){'
        'var b=ev.target;while(b&&!b.classList.contains("star"))b=b.parentElement;'
        'if(!b)return;var v=b.getAttribute("data-v");setStars(v===""?"":Number(v));});'
        'setStars("");</script>'
        '</div>')

    ok = f'<div class="ok">{html.escape(flash)}</div>' if flash else ""
    body = f"""
    <div class="nav"><a href="/">&lsaquo; Task Entry</a> &nbsp;·&nbsp; <a href="/forecast">Forecast</a>
      &nbsp;·&nbsp; <b>Manage Lists</b>
      &nbsp;·&nbsp; <a href="/reports">Reports</a></div>
    {banner}
    <h1>Manage Lists</h1>
    <p class="sub">One set of cards for every project. Pick a group, then a card — no need to scroll to find it.
       Where a list is per project, pick the project on that card. Shared lists apply everywhere.</p>
    <div class="cardnav" id="cardnav">
      <div class="groups" id="cardnav-groups"></div>
      <div class="pills" id="cardnav-pills"></div>
    </div>
    {ok}

        <div class="card"><h2>Project</h2><p class="now">Now: {items("projects", "code")}</p>
            <form class="row" method="POST" action="/admin">
                <input type="hidden" name="action" value="project">
                <div><label>Load existing project</label><select id="project_load"><option value="">New project</option>{all_proj_opts}</select></div>
                {_field("Project code", "code")} {_field("Project name", "name", grow=True)}
                {_field("Day end", "day_end", "time", extra='value="18:30"')}
                {_field("Hours/day", "hours_per_day", "number", extra='value="8.25" step="0.25" style="width:90px" title="09:00-18:30 minus 75 min breaks = 8.25"')}
                <div><label>Sheet Type for this project</label><select name="no_sheet_type"><option value="0">Use Sheet Type</option><option value="1">None</option></select></div>
                <div><label>Zone for this project</label><select name="no_zone"><option value="0">Use Zone</option><option value="1">None</option></select></div>
                <div><label>Project Status</label><select name="project_status"><option>Active</option><option>Completed</option></select></div>
                <div class="full"><label style="text-transform:none;font-weight:600;color:var(--ink)">
                    <input type="checkbox" name="use_teams" value="1" id="use_teams">
                    Grouped in teams</label>
                    <p class="now" style="margin:4px 0 0">When ticked, map <b>zones → QC Reviewer → QC Support</b>
                    in <b>Team mapping</b> for that project. Then set levels and task sets in Lead responsibilities.</p></div>
                <button type="submit">Add / Update Project</button>
            </form>
            <script>var PROJECTS={project_json};document.getElementById("project_load").addEventListener("change",function(){{
                var p=PROJECTS.find(function(x){{return x.code===this.value;}}.bind(this))||{{}};
                document.querySelector('[name=code]').value=p.code||"";document.querySelector('[name=name]').value=p.name||"";
                document.querySelector('[name=day_end]').value=p.day_end||"18:30";document.querySelector('[name=hours_per_day]').value=p.hours_per_day||8.25;
                document.querySelector('[name=no_sheet_type]').value=p.no_sheet_type?"1":"0";document.querySelector('[name=no_zone]').value=p.no_zone?"1":"0";
                document.querySelector('[name=project_status]').value=p.project_status||"Active";
                document.getElementById("use_teams").checked=!!Number(p.use_teams);}});</script>
        </div>

        {zone_card}

        <div class="card" data-scoped="1"><h2>Model</h2>
            <div class="now">{removable_scoped("models", "delete_model", "Model")}</div>
            <form class="row" method="POST" action="/admin">
                <input type="hidden" name="action" value="model">
                {_field("Project", "project", "select", proj_opts)}
                {_field("Model name", "name", grow=True)}
                <button type="submit">Add</button>
            </form>
            {('<form class="row" method="POST" action="/bulk-models">'
                '<input type="hidden" name="project" id="bulk_model_project">'
                '<input type="hidden" name="model_names" id="model_names" value="[]">'
                '<div><label>Or add RVT files</label><input type="file" id="model_files" multiple accept=".rvt"></div>'
                '<div id="model_count" class="now">Select RVT files to add them to the chosen project.</div>'
                '<button type="submit">Add selected RVT files</button></form>'
                '<script>var modelNames=[];var modelInput=document.getElementById("model_files");'
                'var projectInput=document.querySelector("form[action=\\"/admin\\"] select[name=project]");'
                'var bulkProject=document.getElementById("bulk_model_project");'
                'projectInput.addEventListener("change",function(){bulkProject.value=this.value;});'
                'bulkProject.value=projectInput.value;modelInput.addEventListener("change",function(){'
                'Array.prototype.forEach.call(this.files,function(file){if(/\\.rvt$/i.test(file.name)){'
                'var n=file.name.replace(/\\.rvt$/i,"").trim();if(n&&!modelNames.includes(n))modelNames.push(n);}});'
                'document.getElementById("model_names").value=JSON.stringify(modelNames);'
                'document.getElementById("model_count").textContent=modelNames.length+' 
                '" RVT file"+(modelNames.length===1?"":"s")+" ready to add.";});</script></form>')
             if handler is not None and is_manager(handler) else ''}
        </div>

    {card("Master Task / MIDP Sheet (per project + sheet type)", "Counts: " + mt_summary,
      _field("Project", "project", "select", proj_opts)
      + _field("Sheet type", "sheet_type_id", "select", st_opts)
      + _field("Master task name", "name", grow=True),
      "master_task")}

        {('<div class="card"><h2>Categories and Sub Tasks (Manager only)</h2>'
            '<p class="now">Pick the <b>project</b> first. Only that project’s Master Tasks appear. '
            'Then upload a workbook: underlined rows become Categories; numbered rows beneath them become Sub Tasks.</p>'
            '<form class="row" method="POST" action="/bulk-category-tasks" enctype="multipart/form-data">'
            f'<div><label>Project</label><select id="cat_mt_project">{all_proj_opts}</select></div>'
            '<div class="grow"><label>Master Task</label>'
            '<select name="master_task_id" id="cat_master_task" required>'
            '<option value="">choose project first</option>' + master_opts + '</select></div>'
            '<div><label>Workbook</label><input type="file" name="xlsx" accept=".xlsx" required></div>'
            '<button type="submit">Load Categories and Sub Tasks</button></form>'
            '<script>(function(){'
            'var p=document.getElementById("cat_mt_project");'
            'var s=document.getElementById("cat_master_task");'
            'if(!p||!s) return;'
            'function filterMt(){'
            'var code=p.value, keep=s.value, first="";'
            '[].forEach.call(s.options,function(o){'
            'if(!o.value){ o.hidden=false; o.disabled=false; return; }'
            'var show=o.getAttribute("data-proj")===code;'
            'o.hidden=!show; o.disabled=!show;'
            'if(show && !first) first=o.value;'
            '});'
            'var cur=[].filter.call(s.options,function(o){return o.value===keep && !o.hidden;})[0];'
            's.value=cur?keep:(first||"");'
            '}'
            'p.addEventListener("change",filterMt); filterMt();'
            '})();</script></div>'
            if handler is not None and is_manager(handler) else '')}

    {sheet_type_card}

    {employee_card}

    {team_card}

    {map_excel_card}

    {scope_card}

    {card("Role (shared)", "Now: " + items("roles"),
      _field("Name", "name", grow=True), "role")}

        {card("Category (shared)", "Now: " + items("categories"),
            _field("Name", "name", grow=True), "category")}

        <div class="card"><h2>Sub Task</h2><p class="now">The Task list is fixed at {" / ".join(CORE_TASKS)}. Everything more specific is a Sub Task, filed under a Category and one of those Tasks.</p>
            <form class="row" method="POST" action="/admin">
                <input type="hidden" name="action" value="sub_task">
                <div><label>Category</label><select name="category_id" required>{cat_opts}</select></div>
                <div><label>Task</label><select name="task_name_id" required>{core_opts}</select></div>
            {_field("Sub Task Name", "name", grow=True)}
                <button type="submit">Add</button>
            </form>
            {('<form class="row" method="POST" action="/bulk-subtasks" enctype="multipart/form-data">'
                '<div><label>Category</label><select name="category_id" required>' + cat_opts + '</select></div>'
                '<div><label>Task</label><select name="task_name_id" required>' + core_opts + '</select></div>'
                '<div><label>Sub Task workbook</label><input type="file" name="xlsx" accept=".xlsx" required></div>'
                '<button type="submit">Upload Sub Tasks</button></form>'
                '<p class="now">Put one Sub Task per row in column A.</p>'
                if handler is not None and is_manager(handler) else '')}
        </div>

        <div class="card" data-scoped="1"><h2>Level</h2>
            <div class="now">{removable_levels()}</div>
            <form class="row" method="POST" action="/admin"><input type="hidden" name="action" value="level">
                {_field("Project", "project", "select", proj_opts, extra="required")}
                {_field("Level name", "name", grow=True, extra='placeholder="F06, L01, B02, or 6"')}
                <button type="submit">Add</button>
            </form>
            <p class="catnote">Levels belong to the selected project only. A new project starts with none.</p>
            {('<form class="row" method="POST" action="/bulk-levels" enctype="multipart/form-data">'
                '<div><label>Level Excel file</label><input type="file" name="xlsx" accept=".xlsx" required></div>'
                f'<div><label>Project</label><select name="project" required>{proj_opts}</select></div>'
                '<button type="submit">Upload Levels</button></form>'
                '<p class="now">Excel: one Level column, one level per row. Names such as F06 are allowed.</p>'
                if handler is not None and is_manager(handler) else '')}
        </div>

    {card("Status (shared)", "Now: " + items("statuses"),
      _field("Name", "name", grow=True), "status")}

    <div class="card"><h2>Public holidays</h2>
      <p class="now">Weekly offs are always <b>Sunday</b> and the <b>2nd &amp; 4th Saturday</b>
        (1st, 3rd and 5th Saturday are working days). Add other public holidays here.
        The loading plan skips these dates.</p>
      {holiday_list}
      <form class="row" method="POST" action="/admin">
        <input type="hidden" name="action" value="holiday">
        <div><label>Date</label><input type="date" name="day" required></div>
        {_field("Name (optional)", "name", grow=True, extra='placeholder="e.g. Gandhi Jayanti"')}
        <button type="submit">Add</button>
      </form>
    </div>
    """
    con.close()
    return ("<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            "<title>Manage Lists</title>" + ADMIN_STYLE + "</head><body><div class='wrap'>"
            + body + "</div>" + SCOPED_LIST_JS + BULK_TICK_JS + CARD_NAV_JS + "</body></html>")


BULK_TICK_JS = """<script>
document.addEventListener("click", function(ev){
  var b = ev.target.closest ? ev.target.closest("[data-tick-all],[data-tick-none]") : null;
  if(!b) return;
  var on = b.hasAttribute("data-tick-all");
  var id = b.getAttribute(on ? "data-tick-all" : "data-tick-none");
  var box = document.getElementById(id);
  if(!box) return;
  var boxes = box.querySelectorAll("input[type=checkbox]");
  [].forEach.call(boxes, function(c){ c.checked = on; });
  // Ticking Categories changes which Sub Tasks exist, so let the cascade rerun.
  if(id === "category_ids" && typeof fillTasks === "function") fillTasks();
  if((id === "qc_leads" || id === "qc_support") && typeof renderQcMap === "function") renderQcMap();
});
</script>"""


# Jump list at the top of Manage Lists: group → card, one card on screen.
CARD_NAV_JS = """<script>
(function(){
  var nav = document.getElementById("cardnav");
  if(!nav) return;
  var wrap = document.querySelector(".wrap");
  var cards = [].slice.call(document.querySelectorAll(".wrap > .card"));
  if(!cards.length) return;
  function titleOf(card){
    var h = card.querySelector("h2");
    return h ? h.textContent.replace(/\\s+/g," ").trim() : "Card";
  }
  function labelOf(title){
    return title.replace(/\\s*\\([^)]*\\)\\s*/g," ").replace(/\\s+/g," ").trim();
  }
  function groupOf(title){
    var t = title.toLowerCase();
    if(/employee|project team|team mapping|lead/.test(t)) return "People";
    if(/role|category|sub task|level|status|holiday/.test(t)) return "Lists";
    return "Project";
  }
  var items = cards.map(function(card, i){
    var title = titleOf(card);
    return {card:card, title:title, label:labelOf(title), group:groupOf(title), i:i};
  });
  var groups = ["Project","People","Lists","All"];
  var gbox = document.getElementById("cardnav-groups");
  var pbox = document.getElementById("cardnav-pills");
  var curGroup = "Project";
  var curI = 0;
  try{
    var saved = JSON.parse(sessionStorage.getItem("adminCard")||"null");
    if(saved && items[saved.i] && items[saved.i].label===saved.label){
      curGroup = saved.group; curI = saved.i;
    }
  }catch(e){}
  function save(){
    try{sessionStorage.setItem("adminCard", JSON.stringify({i:curI,group:curGroup,label:items[curI].label}));}catch(e){}
  }
  function showCard(i){
    curI = i;
    var all = curGroup==="All";
    items.forEach(function(it, n){
      it.card.classList.toggle("hidden-card", !all && n!==i);
    });
    [].forEach.call(pbox.querySelectorAll("button"), function(b){
      b.classList.toggle("on", Number(b.getAttribute("data-i"))===i);
    });
    save();
    if(!all) window.scrollTo(0, nav.offsetTop);
  }
  function showGroup(name){
    curGroup = name;
    [].forEach.call(gbox.querySelectorAll("button"), function(b){
      b.classList.toggle("on", b.getAttribute("data-g")===name);
    });
    var list = name==="All" ? items : items.filter(function(it){return it.group===name;});
    pbox.innerHTML = list.map(function(it){
      return '<button type="button" data-i="'+it.i+'">'+it.label.replace(/</g,"")+'</button>';
    }).join("");
    [].forEach.call(pbox.querySelectorAll("button"), function(b){
      b.addEventListener("click", function(){ showCard(Number(b.getAttribute("data-i"))); });
    });
    var pick = list.filter(function(it){return it.i===curI;})[0] || list[0];
    if(pick) showCard(pick.i);
    else items.forEach(function(it){ it.card.classList.remove("hidden-card"); });
  }
  gbox.innerHTML = groups.map(function(g){
    return '<button type="button" data-g="'+g+'">'+g+'</button>';
  }).join("");
  [].forEach.call(gbox.querySelectorAll("button"), function(b){
    b.addEventListener("click", function(){ showGroup(b.getAttribute("data-g")); });
  });
  showGroup(curGroup);
})();
</script>"""
SCOPED_LIST_JS = """<script>
(function(){
  document.querySelectorAll('.card[data-scoped]').forEach(function(card){
    var blocks = card.querySelectorAll('.projlist');
    if(!blocks.length) return;
    var selects = card.querySelectorAll('select[name=project]');
    function show(code){
      var any = false;
      blocks.forEach(function(b){
        var match = b.getAttribute('data-proj') === code;
        b.hidden = !match;
        any = any || match;
      });
      if(!any && blocks.length) blocks[0].hidden = false;   // nothing selected yet
    }
    selects.forEach(function(sel){
      sel.addEventListener('change', function(){ show(sel.value); });
    });
    show(selects.length ? selects[0].value : null);
  });
})();
</script>"""


def _count_col(con, table, col, item_id):
    if not _table_exists(con, table):
        return 0
    return con.execute(
        f"SELECT COUNT(*) FROM {table} WHERE {col}=?", (item_id,)).fetchone()[0]


def delete_project_catalog(cur, con, kind, item_id, project):
    """Remove a mistyped Zone or Model. Refuse if daily/forecast rows still point at it."""
    if kind == "zone":
        table, col, label = "buildings", "building_id", "Zone"
    elif kind == "model":
        table, col, label = "models", "model_id", "Model"
    else:
        return "Unknown list."
    try:
        item_id = int(item_id)
    except (TypeError, ValueError):
        return f"Choose a {label} to remove."
    extra = ", is_all" if kind == "zone" else ""
    row = cur.execute(
        f"SELECT id, name, project_code{extra} FROM {table} WHERE id=?",
        (item_id,)).fetchone()
    if not row:
        return f"{label} not found."
    if project and row["project_code"] != project:
        return f"That {label} is not on {project}."
    if kind == "zone" and row["is_all"]:
        return "All zones cannot be removed."
    used = []
    n_tasks = _count_col(con, "tasks", col, item_id)
    if n_tasks:
        used.append(f"{n_tasks} task(s)")
    n_fc = _count_col(con, "forecast_assignments", col, item_id)
    if n_fc:
        used.append(f"{n_fc} forecast row(s)")
    if kind == "zone":
        n_b = _count_col(con, "forecast_subtask_budget", col, item_id)
        if n_b:
            used.append(f"{n_b} forecast hour row(s)")
    if used:
        return (f"Cannot remove {label} '{row['name']}': it is used on "
                + " and ".join(used) + ".")
    if kind == "zone":
        cur.execute(
            "DELETE FROM lead_scope WHERE kind='zone' AND item_id=?", (item_id,))
    try:
        cur.execute(f"DELETE FROM {table} WHERE id=?", (item_id,))
    except sqlite3.IntegrityError:
        return (f"Cannot remove {label} '{row['name']}': it is still linked "
                "to saved work.")
    return f"Removed {label} '{row['name']}' from {row['project_code']}."


def delete_level(cur, con, item_id, project=None):
    """Take a Level off one project, or delete it from the catalog if unused."""
    try:
        item_id = int(item_id)
    except (TypeError, ValueError):
        return "Choose a Level to remove."
    row = cur.execute(
        "SELECT id, label, is_all, project_code FROM levels WHERE id=?",
        (item_id,)).fetchone()
    if not row:
        return "Level not found."
    if row["is_all"]:
        return "All levels cannot be removed."
    project = project or row["project_code"]
    if project:
        n_tasks = cur.execute(
            "SELECT COUNT(*) FROM tasks WHERE level_id=? AND project_code=?",
            (item_id, project)).fetchone()[0]
        n_fc = 0
        if _table_exists(con, "forecast_assignments"):
            n_fc = cur.execute(
                "SELECT COUNT(*) FROM forecast_assignments "
                "WHERE level_id=? AND project_code=?",
                (item_id, project)).fetchone()[0]
        if n_tasks or n_fc:
            return (f"Cannot remove Level '{row['label']}' from {project}: "
                    f"it is used on {n_tasks + n_fc} saved row(s).")
        cur.execute(
            "DELETE FROM lead_scope WHERE project_code=? AND kind='level' AND item_id=?",
            (project, item_id))
        cur.execute(
            "DELETE FROM project_levels WHERE project_code=? AND level_id=?",
            (project, item_id))
        try:
            cur.execute("DELETE FROM levels WHERE id=? AND project_code=?",
                        (item_id, project))
        except sqlite3.IntegrityError:
            pass
        return f"Removed Level '{row['label']}' from {project}."
    used = []
    n_tasks = _count_col(con, "tasks", "level_id", item_id)
    if n_tasks:
        used.append(f"{n_tasks} task(s)")
    n_fc = _count_col(con, "forecast_assignments", "level_id", item_id)
    if n_fc:
        used.append(f"{n_fc} forecast row(s)")
    n_b = _count_col(con, "forecast_subtask_budget", "level_id", item_id)
    if n_b:
        used.append(f"{n_b} forecast hour row(s)")
    if used:
        return (f"Cannot remove Level '{row['label']}': it is used on "
                + " and ".join(used) + ".")
    cur.execute("DELETE FROM lead_scope WHERE kind='level' AND item_id=?", (item_id,))
    try:
        cur.execute("DELETE FROM levels WHERE id=?", (item_id,))
    except sqlite3.IntegrityError:
        return (f"Cannot remove Level '{row['label']}': it is still linked "
                "to saved work.")
    return f"Removed Level '{row['label']}'."


def admin_insert(form):
    action = form.get("action", [""])[0]
    con = connect()
    cur = con.cursor()
    g = lambda k: (form.get(k, [""])[0] or "").strip()
    msg = "Nothing to add."
    if action == "project":
        code = g("code")
        if not code:
            con.close(); return "Project code is required."
        cur.execute("""INSERT INTO projects(code,name,client_code,internal_code,
                     day_start,day_end,hours_per_day,no_sheet_type,project_status,no_zone,use_teams) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                     ON CONFLICT(code) DO UPDATE SET name=excluded.name,
                       day_end=excluded.day_end,hours_per_day=excluded.hours_per_day,
                       no_sheet_type=excluded.no_sheet_type,project_status=excluded.project_status,
                       no_zone=excluded.no_zone,use_teams=excluded.use_teams""",
                    (code, g("name") or code, None, None, "09:00",
                     g("day_end") or "18:30", float(g("hours_per_day") or 8.25),
                     1 if form.get("no_sheet_type", ["0"])[0] == "1" else 0,
                     g("project_status") or "Active",
                     1 if form.get("no_zone", ["0"])[0] == "1" else 0,
                     1 if form.get("use_teams", ["0"])[0] == "1" else 0))
        for bn, bs, be in (("Lunch", "12:45", "13:30"), ("AM", "11:15", "11:30"), ("PM", "16:15", "16:30")):
            cur.execute("INSERT OR IGNORE INTO break_windows(project_code,name,start_time,end_time) "
                        "VALUES (?,?,?,?)", (code, bn, bs, be))
        msg = f"Project {code} added (with standard breaks & config)."
    elif action == "building":
        nm = g("name")
        if not nm:
            con.close(); return "Zone name is required."
        cur.execute("INSERT OR IGNORE INTO buildings(project_code,name,is_all) VALUES (?,?,?)",
                    (g("project"), nm, 1 if nm.lower().startswith("all") else 0))
        msg = f"Building '{nm}' added to {g('project')}."
    elif action == "delete_zone":
        msg = delete_project_catalog(cur, con, "zone", g("id"), g("project"))
    elif action == "model":
        nm = g("name")
        if not nm:
            con.close(); return "Model name is required."
        cur.execute("INSERT OR IGNORE INTO models(project_code,name) VALUES (?,?)", (g("project"), nm))
        msg = f"Model '{nm}' added to {g('project')}."
    elif action == "delete_model":
        msg = delete_project_catalog(cur, con, "model", g("id"), g("project"))
    elif action == "delete_level":
        msg = delete_level(cur, con, g("id"), g("project"))
    elif action == "master_task":
        # A blank name renders as an empty row in the entry form's dropdown, which
        # makes the list look broken. 24139 picked one up from an early import.
        if not g("name").strip():
            con.close(); return "Master task name is required."
        cur.execute("INSERT OR IGNORE INTO master_tasks(project_code,sheet_type_id,name) VALUES (?,?,?)",
                    (g("project"), int(g("sheet_type_id")), g("name").strip()))
        msg = f"Master task added to {g('project')}."
    elif action == "sheet_type":
        cur.execute("INSERT OR IGNORE INTO sheet_types(name) VALUES (?)", (g("name"),)); msg = f"Sheet type '{g('name')}' added."
    elif action == "employee":
        if not g("name"):
            con.close(); return "Employee name is required."
        fields = {
            "name": g("name"), "role_id": g("role_id") or None, "emp_code": g("emp_code") or None,
            "category": g("category") or None, "skillset": g("skillset") or None,
            "bim_id": g("bim_id") or None, "ip_address": g("ip_address") or None,
            "email": g("email") or None, "contact": g("contact") or None,
            "location": (g("location") if g("location") in OFFICE_LOCATIONS else None),
            "skill_rating": _int_or_none(g("skill_rating"), 1, 5),
            "experience_years": _float_or_none(g("experience_years"), 0, 60),
        }
        pid = g("person_id")
        if pid:
            cur.execute(f"UPDATE people SET {','.join(k+'=?' for k in fields)} WHERE id=?",
                        list(fields.values()) + [int(pid)])
            msg = f"Employee '{fields['name']}' updated."
        else:
            cur.execute(f"INSERT OR IGNORE INTO people({','.join(fields)}) "
                        f"VALUES ({','.join('?' * len(fields))})", list(fields.values()))
            msg = f"Employee '{fields['name']}' added."
    elif action == "team_roles":
        code = g("project")
        remove_row = g("remove_row")
        if remove_row:
            if not code:
                con.close(); return "Choose a project."
            try:
                rid = int(remove_row)
            except ValueError:
                con.close(); return "Choose a person to remove."
            row = cur.execute(
                "SELECT id, person_id FROM project_people "
                "WHERE id=? AND project_code=? AND left_on IS NULL",
                (rid, code)).fetchone()
            if not row:
                con.close(); return "That person is not on this project."
            left_on = parse_iso_date(g("exitrow_" + str(row["id"])))
            if not left_on:
                con.close(); return "Enter Left on first. Leave it blank while they are still on this project."
            who = cur.execute("SELECT name FROM people WHERE id=?", (row["person_id"],)).fetchone()
            err = drop_from_project(cur, code, int(row["person_id"]), left_on)
            if err:
                con.close(); return err
            name = who[0] if who else row["person_id"]
            msg = (f"Removed {name} from {code} through {fmt_day(left_on)}. Logged tasks stay. "
                   "They remain an employee for other projects. "
                   "Who didn't fill will not list them from the next working day after this date.")
        else:
            if not code:
                con.close(); return "Choose a project."
            changed = 0
            dated = 0
            left_n = 0
            for key, values in form.items():
                raw = (values[0] if values else "").strip()
                if key.startswith("joinrow_"):
                    try:
                        rid = int(key[8:])
                    except ValueError:
                        continue
                    joined = parse_iso_date(raw)
                    left = parse_iso_date(g("exitrow_" + str(rid)))
                    row = cur.execute(
                        "SELECT id, person_id, joined_on, left_on FROM project_people "
                        "WHERE id=? AND project_code=?", (rid, code)).fetchone()
                    if not row or not joined:
                        continue
                    if row["left_on"]:
                        continue
                    if row["joined_on"] != joined:
                        cur.execute(
                            "UPDATE project_people SET joined_on=? WHERE id=? AND left_on IS NULL",
                            (joined, rid))
                        dated += 1
                    if left:
                        err = drop_from_project(cur, code, int(row["person_id"]), left)
                        if err:
                            con.close(); return err
                        left_n += 1
                    continue
                if key.startswith("stnew_") or key.startswith("strow_"):
                    is_new = key.startswith("stnew_")
                    try:
                        ident = int(key[6:])
                    except ValueError:
                        continue
                    status = raw.strip().lower()
                    if status not in ("joined", "left"):
                        continue
                    if is_new:
                        person_id = ident
                        joined = parse_iso_date(g("joinnew_" + str(person_id)))
                        left = parse_iso_date(g("exitnew_" + str(person_id)))
                        row = None
                    else:
                        row = cur.execute(
                            "SELECT id, person_id, joined_on, left_on FROM project_people "
                            "WHERE id=? AND project_code=?", (ident, code)).fetchone()
                        if not row:
                            continue
                        person_id = int(row["person_id"])
                        joined = parse_iso_date(g("joinrow_" + str(ident))) or row["joined_on"]
                        left = parse_iso_date(g("exitrow_" + str(ident))) or row["left_on"]
                    if status == "joined":
                        if not joined:
                            con.close()
                            return ("Enter Joined on for each person you mark as Joined.")
                        open_row = cur.execute(
                            "SELECT 1 FROM project_people "
                            "WHERE project_code=? AND person_id=? AND left_on IS NULL",
                            (code, person_id)).fetchone()
                        if not open_row:
                            upsert_project_person(cur, code, person_id, None, joined)
                            dated += 1
                    else:
                        if not left:
                            con.close()
                            return ("Enter Left on for each person you mark as Left.")
                        joined = joined or left
                        if left < joined:
                            con.close()
                            return (f"Exit date {fmt_day(left)} is before the join date "
                                    f"{fmt_day(joined)}. Correct the dates and try again.")
                        open_row = cur.execute(
                            "SELECT 1 FROM project_people "
                            "WHERE project_code=? AND person_id=? AND left_on IS NULL",
                            (code, person_id)).fetchone()
                        if open_row:
                            err = drop_from_project(cur, code, person_id, left)
                            if err:
                                con.close(); return err
                            left_n += 1
                        elif row and row["left_on"]:
                            if row["joined_on"] != joined or row["left_on"] != left:
                                cur.execute(
                                    "UPDATE project_people SET joined_on=?, left_on=? WHERE id=?",
                                    (joined, left, ident))
                                dated += 1
                        else:
                            cur.execute(
                                "INSERT INTO project_people"
                                "(project_code,person_id,role_id,joined_on,left_on) "
                                "VALUES (?,?,NULL,?,?)",
                                (code, person_id, joined, left))
                            left_n += 1
                    continue
                if not key.startswith("role_"):
                    continue
                try:
                    person_id = int(key[5:])
                except ValueError:
                    continue
                role_id = int(raw) if raw else None
                on_team = cur.execute(
                    "SELECT role_id FROM project_people "
                    "WHERE project_code=? AND person_id=? AND left_on IS NULL",
                    (code, person_id)).fetchone()
                if on_team is None:
                    continue
                if on_team[0] == role_id:
                    continue
                cur.execute(
                    "UPDATE project_people SET role_id=? "
                    "WHERE project_code=? AND person_id=? AND left_on IS NULL",
                    (role_id, code, person_id))
                changed += 1
            bits = []
            if dated:
                bits.append(f"{dated} date(s)")
            if left_n:
                bits.append(f"{left_n} left")
            if changed:
                bits.append(f"{changed} role(s)")
            msg = (f"{' and '.join(bits)} updated on {code}." if bits
                   else f"No role or date changes to save on {code}.")
    elif action == "project_team":
        code = g("project")
        if not code:
            con.close(); return "Choose a project."
        old = {int(r[0]): r[1] for r in cur.execute(
            "SELECT person_id, role_id FROM project_people "
            "WHERE project_code=? AND left_on IS NULL", (code,)).fetchall()}
        chosen = [int(v) for v in form.get("person_ids", []) if v.strip()]
        removed = set(old) - set(chosen)
        added = set(chosen) - set(old)
        joined_on = parse_iso_date(g("joined_on")) or today_iso()
        left_on = parse_iso_date(g("left_on"))
        if removed and not left_on:
            con.close(); return ("Enter Left on for the people you unticked. "
                                 "Leave it blank if nobody is leaving.")
        for person_id in added:
            upsert_project_person(cur, code, person_id, old.get(person_id), joined_on)
        for person_id in removed:
            err = drop_from_project(cur, code, person_id, left_on)
            if err:
                con.close(); return err
        msg = (f"{len(chosen)} employee(s) set as the team for {code}." if chosen
               else f"Team cleared for {code} — all employees are offered again.")
        if added:
            msg += f" Added {len(added)} from {fmt_day(joined_on)}."
        if removed:
            msg += f" Removed {len(removed)} from {fmt_day(left_on)} (logged tasks stay)."
    elif action == "qc_team":
        code = g("project")
        if not code:
            con.close(); return "Choose a project."
        lead_ids = {int(v) for v in form.get("lead_ids", []) if v.strip()}
        support_ids = {int(v) for v in form.get("support_ids", []) if v.strip()}
        reviewer_row = cur.execute("SELECT id FROM roles WHERE name='QC REVIEWER'").fetchone()
        support_row = cur.execute("SELECT id FROM roles WHERE name='QC SUPPORT'").fetchone()
        reviewer_role = reviewer_row[0] if reviewer_row else None
        support_role = support_row[0] if support_row else None
        current_rows = cur.execute(
            "SELECT pp.person_id, pp.role_id, COALESCE(r.name,'') AS rn "
            "FROM project_people pp LEFT JOIN roles r ON r.id=pp.role_id "
            "WHERE pp.project_code=? AND pp.left_on IS NULL", (code,)).fetchall()
        old_roles = {int(r["person_id"]): r["role_id"] for r in current_rows}
        keep = [int(r["person_id"]) for r in current_rows
                if (r["rn"] or "").strip().upper() not in ("QC REVIEWER", "QC SUPPORT")]
        new_ids = []
        for pid in keep + sorted(lead_ids) + sorted(support_ids):
            if pid not in new_ids:
                new_ids.append(pid)
        dropped = set(old_roles) - set(new_ids)
        for person_id in dropped:
            drop_from_project(cur, code, person_id, today_iso())
        for person_id in new_ids:
            if person_id in lead_ids:
                role_id = reviewer_role
            elif person_id in support_ids:
                role_id = support_role
            else:
                role_id = old_roles.get(person_id)
            upsert_project_person(cur, code, person_id, role_id)
        msg = (f"{code}: {len(lead_ids)} team lead(s), {len(support_ids)} QC Support. "
               "Roles apply only on this project.")
        if lead_ids:
            ph = ",".join("?" * len(lead_ids))
            cur.execute(
                f"DELETE FROM lead_scope WHERE project_code=? AND person_id NOT IN ({ph})",
                [code] + list(lead_ids))
        else:
            cur.execute("DELETE FROM lead_scope WHERE project_code=?", (code,))
        cur.execute("DELETE FROM lead_support WHERE project_code=?", (code,))
        n_pairs = 0
        seen_support = set()
        for raw in form.get("pairs", []):
            parts = str(raw).split(":")
            if len(parts) != 2:
                continue
            try:
                lid, sid = int(parts[0]), int(parts[1])
            except ValueError:
                continue
            if lid not in lead_ids or sid == lid or sid in seen_support:
                continue
            if sid not in support_ids and sid not in lead_ids:
                continue
            cur.execute(
                "INSERT OR IGNORE INTO lead_support(project_code,lead_id,support_id) VALUES (?,?,?)",
                (code, lid, sid))
            seen_support.add(sid)
            n_pairs += 1
        msg += f" {n_pairs} support-to-reviewer assignment(s)."
        cur.execute("DELETE FROM lead_scope WHERE project_code=? AND kind='zone'", (code,))
        n_zones = 0
        seen_pair = set()
        for raw in form.get("zone_pairs", []):
            parts = str(raw).split(":")
            if len(parts) != 2:
                continue
            try:
                lid, zid = int(parts[0]), int(parts[1])
            except ValueError:
                continue
            if lid not in lead_ids or (lid, zid) in seen_pair:
                continue
            cur.execute(
                "INSERT OR IGNORE INTO lead_scope(project_code,person_id,kind,item_id) "
                "VALUES (?,?,?,?)",
                (code, lid, "zone", zid))
            seen_pair.add((lid, zid))
            n_zones += 1
        msg += f" {n_zones} zone-to-reviewer assignment(s)."
    elif action == "lead_scope":
        code = g("project")
        pid = g("person_id")
        if not code or not pid:
            con.close(); return "Choose a project and a team lead."
        person_id = int(pid)
        cur.execute("DELETE FROM lead_scope WHERE project_code=? AND person_id=? AND kind IN ('level','category')",
                    (code, person_id))
        n = 0
        for kind, key in (("level", "level_ids"), ("category", "category_ids")):
            for raw in form.get(key, []):
                if not str(raw).strip():
                    continue
                cur.execute(
                    "INSERT OR IGNORE INTO lead_scope(project_code,person_id,kind,item_id) "
                    "VALUES (?,?,?,?)",
                    (code, person_id, kind, int(raw)))
                n += 1
        who = cur.execute("SELECT name FROM people WHERE id=?", (person_id,)).fetchone()
        name = who[0] if who else pid
        msg = (f"{code}: saved {n} responsibility tick(s) for {name}. "
               "A clear group means that kind is unrestricted for this lead.")
    elif action == "delete_employee":
        pid = g("person_id")
        if not pid:
            con.close(); return "Choose an employee to delete."
        row = cur.execute("SELECT name FROM people WHERE id=?", (int(pid),)).fetchone()
        if not row:
            con.close(); return "Employee not found."
        used = cur.execute("SELECT COUNT(*) FROM task_assignees WHERE person_id=?", (int(pid),)).fetchone()[0]
        if used:
            con.close(); return f"Cannot delete '{row[0]}': it is linked to {used} task assignment(s) and must remain for reporting history."
        cur.execute("DELETE FROM people WHERE id=?", (int(pid),))
        msg = f"Employee '{row[0]}' deleted."
    elif action == "person":
        role_id = g("role_id") or None
        cur.execute("INSERT OR IGNORE INTO people(name, role_id) VALUES (?,?)", (g("name"), role_id))
        msg = f"Person '{g('name')}' added."
    elif action == "role":
        cur.execute("INSERT OR IGNORE INTO roles(name) VALUES (?)", (g("name"),)); msg = f"Role '{g('name')}' added."
    elif action == "set_role":
        pid = g("person_id")
        if pid:
            cur.execute("UPDATE people SET role_id=? WHERE id=?", (g("role_id") or None, int(pid)))
            msg = "Person's role updated."
        else:
            msg = "Pick a person first."
    elif action == "task_name":
        cur.execute("INSERT OR IGNORE INTO task_names(name) VALUES (?)", (g("name"),))
        task_id = cur.execute("SELECT id FROM task_names WHERE name=?", (g("name"),)).fetchone()[0]
        cur.execute("INSERT OR IGNORE INTO master_task_tasks(master_task_id,task_name_id) VALUES (?,?)",
                    (int(g("master_task_id")), task_id))
        msg = f"Task '{g('name')}' added under the Master Task."
    elif action == "category":
        cur.execute("INSERT OR IGNORE INTO categories(name) VALUES (?)", (g("name").upper(),))
        msg = f"Category '{g('name').upper()}' added."
    elif action == "sub_task":
        cur.execute("INSERT OR IGNORE INTO task_sub_tasks(task_name_id,category_id,name) VALUES (?,?,?)",
                    (int(g("task_name_id")), int(g("category_id")), g("name")))
        msg = f"Sub Task '{g('name')}' added."
    elif action == "status":
        cur.execute("INSERT OR IGNORE INTO statuses(name) VALUES (?)", (g("name"),)); msg = f"Status '{g('name')}' added."
    elif action == "holiday":
        forecast_app.ensure_public_holidays(con)
        day = parse_iso_date(g("day"))
        if not day:
            con.close(); return "Pick a holiday date."
        name = g("name")
        cur.execute(
            "INSERT OR REPLACE INTO public_holidays(day, name) VALUES (?,?)",
            (day, name or None))
        msg = f"Public holiday {fmt_day(day)} saved."
    elif action == "delete_holiday":
        forecast_app.ensure_public_holidays(con)
        day = parse_iso_date(g("day"))
        if not day:
            con.close(); return "Pick a holiday to remove."
        cur.execute("DELETE FROM public_holidays WHERE day=?", (day,))
        msg = f"Removed public holiday {fmt_day(day)}."
    elif action == "level":
        num, label, is_all = parse_level_name(g("name") or g("number"))
        if not label:
            con.close(); return "Enter a level such as F06, L01, B02, or 6."
        project = g("project")
        if not project:
            con.close(); return "Choose a project. Levels belong to one project only."
        if not upsert_project_level(cur, project, label, num, is_all):
            con.close(); return f"Could not save level '{label}'."
        msg = f"Level '{label}' added to {project}."
    con.commit()
    con.close()
    return msg


def run_tool(tool, extra_args=None):
    """Run a maintenance script and capture its output for display."""
    import subprocess
    scripts = {"export": "export_reports.py", "sync": "sync_lists.py", "import": "import_xlsx.py"}
    names = {"export": "Export reports to Excel", "sync": "Sync lists from Excel workbook",
             "import": "Rebuild database from Excel"}
    script = scripts.get(tool)
    if not script:
        return ("Unknown tool", "Unknown tool.")
    try:
        r = subprocess.run([sys.executable, os.path.join(HERE, script)] + (extra_args or []),
                           cwd=HERE, capture_output=True, text=True, timeout=180)
        out = (r.stdout or "")
        if r.stderr:
            out += "\n[messages]\n" + r.stderr
        if r.returncode != 0:
            out = f"(program exited with code {r.returncode})\n" + out
    except subprocess.TimeoutExpired:
        out = "Timed out after 180s."
    except Exception as e:
        out = f"Failed to run: {e}"
    return (names.get(tool, tool), out.strip() or "(no output)")


# Finished /run results, keyed by a short id. /run redirects to /result so the
# result page is a GET: refreshing it re-reads the stored output instead of
# re-submitting the POST. Without this, F5 on the result page either fails with
# ERR_CACHE_MISS or silently re-runs the tool - and one of those tools rebuilds
# the database from Excel.
RUN_RESULTS = {}
RUN_SEQ = [0]


def store_result(title, output, download):
    RUN_SEQ[0] += 1
    rid = str(RUN_SEQ[0])
    RUN_RESULTS[rid] = (title, output, download)
    for old in sorted(RUN_RESULTS, key=int)[:-20]:   # keep the last 20
        RUN_RESULTS.pop(old, None)
    return rid


def render_result(title, output, download=None):
    dl = ""
    if download:
        dl = (f'<p><a href="/download?file={quote(download)}" '
              'style="display:inline-block;background:#1f7a4d;color:#fff;padding:11px 18px;'
              'border-radius:8px;text-decoration:none;font-weight:600">&#11015; Open the Excel file</a>'
              f' &nbsp;<span style="color:#5b6675;font-size:13px">{html.escape(download)}</span></p>')
    return ("<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            f"<title>{html.escape(title)}</title>" + ADMIN_STYLE +
            "<style>pre{background:#0e1621;color:#d7e2ee;padding:14px 16px;border-radius:10px;"
            "overflow:auto;font:12.5px/1.5 ui-monospace,Consolas,monospace}</style></head>"
            "<body><div class='wrap'>"
            "<div class='nav'><a href='/admin'>&lsaquo; Manage Lists</a> &nbsp;·&nbsp; "
            "<a href='/'>Task Entry</a> &nbsp;·&nbsp; <a href='/forecast'>Forecast</a> "
            "&nbsp;·&nbsp; <a href='/reports'>Reports</a></div>"
            f"<h1>{html.escape(title)}</h1>"
            + dl +
            f"<pre>{html.escape(output)}</pre>"
            "<p><a href='/admin'>&lsaquo; Back to Manage Lists</a></p>"
            "</div></body></html>")


REPORTS_CSS = """<style>
  .filters{display:flex;flex-wrap:wrap;gap:10px;align-items:end;background:var(--card);
    border:1px solid var(--rule);border-radius:10px;padding:14px 16px}
  .filters .grow{flex:1;min-width:200px}
  .filters .brk{flex-basis:100%;height:0;margin:0;padding:0}
  .filters .wide{flex:1 1 100%}
  .filters .wide select{width:100%}
  table.rep{width:100%;border-collapse:collapse;margin-top:16px;font-size:13px;
    background:var(--card);border:1px solid var(--rule)}
  table.rep th,table.rep td{border:1px solid var(--rule);padding:6px 9px;text-align:left;vertical-align:top}
  table.rep th{background:#eef2f6;color:var(--muted);text-transform:uppercase;font-size:11px;letter-spacing:.03em}
  table.rep tr.total td{font-weight:700;background:#f3f6f9}
  table.rep td.num{text-align:right}
  table.rep td.okh{background:#e6f4ec}
  table.rep td.short{background:#fff6e5;color:#8a5a00}
  .hint{color:var(--muted);font-size:13px}
  .scroll{overflow-x:auto;margin-top:8px}
  a.btn{display:inline-block;background:var(--accent);color:#fff;border-radius:7px;
    padding:9px 16px;text-decoration:none;font-weight:600}
  a.btn.green{background:#1f7a4d}
  /* Zone / Level progress grid — one column per sub task, so it scrolls. */
  .matrixwrap{overflow:auto;max-height:75vh;border:1px solid var(--rule);border-radius:6px}
  table.matrix{border-collapse:separate;border-spacing:0;font-size:12px}
  table.matrix th,table.matrix td{border-bottom:1px solid var(--rule);
    border-right:1px solid var(--rule);padding:3px 5px}
  table.matrix thead th{position:sticky;top:0;background:#eef2f6;z-index:2}
  table.matrix thead tr:nth-child(2) th{top:26px}
  table.matrix th.band{text-align:center;font-size:11px;letter-spacing:.04em;background:#e2e8f0}
  table.matrix th.vert{height:170px;vertical-align:bottom;padding:4px 2px;background:#eef2f6}
  table.matrix th.vert span{writing-mode:vertical-rl;transform:rotate(180deg);
    white-space:nowrap;font-weight:600;text-transform:none;letter-spacing:0;font-size:11px;
    display:block;max-height:162px;overflow:hidden;text-overflow:ellipsis}
  table.matrix th.rowhead{position:sticky;background:#f7f9fb;text-align:left;
    white-space:nowrap;z-index:1}
  /* Each frozen column needs its own offset, or they stack on top of each
     other as soon as the grid scrolls sideways. */
  table.matrix th.rowhead:nth-child(1){left:0;min-width:44px}
  table.matrix th.rowhead:nth-child(2){left:44px;min-width:48px}
  table.matrix th.rowhead.who{left:92px;min-width:150px;max-width:190px;
    white-space:normal;font-weight:600;border-right:2px solid var(--rule)}
  table.matrix th.rowhead.who:empty::after{content:"4";color:var(--muted)}
  table.matrix thead th.rowhead{z-index:3}
  table.matrix td.cell{text-align:center;font-weight:700;width:26px}
  .cell.done{color:#1f7a4d;background:#e7f5ed}
  .cell.wip{color:#8a5a00;background:#fdf3dc;font-size:9px;font-weight:700}
  .cell.hold{color:#7a4b9c;background:#f1e9f8}
  .cell.todo{color:#a33;background:#fdecec}
  .cell.na{color:#c3cbd4;background:#fff}
  .legend .cell{display:inline-block;padding:1px 6px;border:1px solid var(--rule);
    border-radius:3px;margin-right:2px}
  @media print{.matrixwrap{max-height:none;overflow:visible}}
  @media print{.filters,.nav,button{display:none} h1{margin-top:0}}
</style>"""


# Status shorthand for the zone/level matrix, matching the tracker sheet the
# team already reads: green tick done, WIP in progress, red cross not started.
MATRIX_MARKS = {
    "Completed":   ("done", "&#10004;", "Completed"),
    "Ongoing":     ("wip",  "WIP",      "Work in progress"),
    "On Hold":     ("hold", "H",        "On hold"),
    "Not Started": ("todo", "&#10007;", "Not started"),
}


def matrix_data(project, zone="", level="", dfrom="", dto=""):
    """Latest status per (zone, level, sub task) for one project.

    The tracker sheet is a grid of zone/level against sub task; the database
    holds time entries. One entry can carry several sub tasks, so this expands
    them and keeps the most recent entry for each cell.
    """
    con = connect()
    where, params = ["t.project_code=?"], [project]
    if zone:
        where.append("b.name=?"); params.append(zone)
    if level:
        where.append("l.label=?"); params.append(level)
    if dfrom:
        where.append("t.work_date>=?"); params.append(dfrom)
    if dto:
        where.append("t.work_date<=?"); params.append(dto)
    rows = con.execute(f"""
        SELECT b.name AS zone, l.label AS level, l.number AS lnum,
               COALESCE(c.name,'') AS category, ts.name AS sub_task,
               COALESCE(st.name,'') AS status, t.work_date, t.id AS tid,
               (SELECT GROUP_CONCAT(pe.name,', ') FROM task_assignees ta
                  JOIN people pe ON pe.id=ta.person_id WHERE ta.task_id=t.id) AS who
          FROM tasks t
          JOIN task_sub_task_links tl ON tl.task_id=t.id
          JOIN task_sub_tasks ts ON ts.id=tl.task_sub_task_id
          LEFT JOIN categories c ON c.id=ts.category_id
          LEFT JOIN buildings b ON b.id=t.building_id
          LEFT JOIN levels l ON l.id=t.level_id
          LEFT JOIN statuses st ON st.id=t.status_id
         WHERE {' AND '.join(where)}
         ORDER BY t.work_date, t.id""", params).fetchall()

    # Columns: every sub task in the project's catalog, grouped by category, so
    # the grid keeps its shape even on a day when nothing was logged.
    cols = con.execute("""
        SELECT DISTINCT COALESCE(c.name,'') AS category, ts.name AS sub_task
          FROM task_sub_tasks ts
          LEFT JOIN categories c ON c.id=ts.category_id
         WHERE ts.category_id IN (SELECT DISTINCT category_id FROM category_tasks ct
                                    JOIN master_tasks mt ON mt.id=ct.master_task_id
                                   WHERE mt.project_code=?)
         ORDER BY 1, 2""", (project,)).fetchall()
    zones = [r[0] for r in con.execute(
        "SELECT name FROM buildings WHERE project_code=? AND TRIM(name)<>'' ORDER BY name",
        (project,))]
    levels = [(r[0], r[1]) for r in con.execute(
        """SELECT l.label, l.number FROM project_levels pl JOIN levels l ON l.id=pl.level_id
            WHERE pl.project_code=? ORDER BY (l.number IS NULL), l.number""", (project,))]
    con.close()

    # Later rows win: the query is ordered oldest first.
    cell, who = {}, {}
    for r in rows:
        if not r["zone"] or not r["level"]:
            continue
        cell[(r["zone"], r["level"], r["category"], r["sub_task"])] = {
            "status": r["status"], "date": r["work_date"], "who": r["who"] or ""}
        # Everyone who logged against this zone/level, in first-seen order. A row
        # can carry more than one name (a reviewer and a support); an unassigned
        # entry leaves it empty rather than guessing.
        names = who.setdefault((r["zone"], r["level"]), [])
        for n in (r["who"] or "").split(", "):
            if n and n not in names:
                names.append(n)
    return {"cols": [(c[0], c[1]) for c in cols], "zones": zones,
            "levels": levels, "cell": cell, "who": who}


def render_matrix(data, project):
    """The zone/level x sub-task grid, laid out like the tracker sheet."""
    cols, zones, levels, cell = data["cols"], data["zones"], data["levels"], data["cell"]
    who = data.get("who", {})
    if not cols:
        return "<p class='sub'>No Sub Tasks are set up for this project yet.</p>"

    # Two header rows: category band above, sub task beneath.
    band, seen = "", None
    for cat, _ in cols:
        if cat != seen:
            span = sum(1 for c, _ in cols if c == cat)
            band += f'<th colspan="{span}" class="band">{html.escape(cat or "—")}</th>'
            seen = cat
    subs = "".join(
      f'<th class="vert" title="{html.escape(cat)} &mdash; {html.escape(sub)}">'
      f'<span>{html.escape(sub)}</span></th>' for cat, sub in cols)

    body, shown = "", 0
    for zone in zones:
        for label, _ in levels:
            marks = [cell.get((zone, label, cat, sub)) for cat, sub in cols]
            if not any(marks):
                continue                      # nothing logged for this zone/level
            shown += 1
            tds = ""
            for m in marks:
                if not m:
                    tds += '<td class="cell na">&middot;</td>'
                    continue
                cls, glyph, title = MATRIX_MARKS.get(m["status"], ("todo", "&#10007;", "Not started"))
                tds += (f'<td class="cell {cls}" title="{html.escape(title)} &mdash; '
                        f'{html.escape(m["date"] or "")} {html.escape(m["who"])}">{glyph}</td>')
            names = ", ".join(who.get((zone, label), []))
            body += (f'<tr><th class="rowhead">{html.escape(zone)}</th>'
                     f'<th class="rowhead">{html.escape(label)}</th>'
                     f'<th class="rowhead who">'
                     f'{html.escape(names) if names else "&mdash;"}</th>{tds}</tr>')

    if not shown:
        return ("<p class='sub'>Nothing logged yet for this project, so every zone/level "
                "row would be empty. Log some tasks, or widen the date range.</p>")

    legend = ('<p class="sub legend">'
              '<span class="cell done">&#10004;</span> Completed &nbsp; '
              '<span class="cell wip">WIP</span> Work in progress &nbsp; '
              '<span class="cell hold">H</span> On hold &nbsp; '
              '<span class="cell todo">&#10007;</span> Not started &nbsp; '
              '<span class="cell na">&middot;</span> nothing logged'
              '</p>')
    return (f'{legend}<div class="matrixwrap"><table class="rep matrix">'
            f'<thead><tr><th class="rowhead" rowspan="2">Zone</th>'
            f'<th class="rowhead" rowspan="2">Level</th>'
            f'<th class="rowhead" rowspan="2">Assignee</th>{band}</tr>'
            f'<tr>{subs}</tr></thead><tbody>{body}</tbody></table></div>'
            f'<p class="sub">{shown} zone/level row(s) with entries &mdash; '
            f'{len(cols)} sub tasks across {len(zones)} zones and {len(levels)} levels '
            f'in project {html.escape(project)}.</p>')


def render_reports(q, banner=""):
    getv = lambda k: (q.get(k, [""])[0] or "")
    con = connect()
    proj_rows = con.execute(
        "SELECT code, name, no_sheet_type, no_zone FROM projects ORDER BY code").fetchall()
    projects = [r[0] for r in proj_rows]
    proj_flags = {
        r["code"]: {
            "no_sheet_type": int(r["no_sheet_type"] or 0),
            "no_zone": int(r["no_zone"] or 0),
        }
        for r in proj_rows
    }
    people_rows = con.execute(
        "SELECT id, name, emp_code FROM people ORDER BY name").fetchall()
    sheet_types = [r[0] for r in con.execute("SELECT name FROM sheet_types ORDER BY name")]
    task_names = [r[0] for r in con.execute("SELECT name FROM task_names ORDER BY name")]
    categories = [r[0] for r in con.execute("SELECT name FROM categories ORDER BY name")]
    sub_tasks = [r[0] for r in con.execute("SELECT DISTINCT name FROM task_sub_tasks ORDER BY name")]
    # Reports: only master tasks that have actually been logged (you can't report on a
    # sheet nobody worked on). This is why the entry form uses the full catalog but the
    # Reports filter does not.
    mts = con.execute("SELECT DISTINCT project_code, COALESCE(sheet_type,''), master_task "
                      "FROM v_task_effort WHERE master_task IS NOT NULL AND master_task<>'' "
                      "ORDER BY master_task").fetchall()
    mt_json = json.dumps([{"p": r[0], "st": r[1], "n": r[2]} for r in mts])
    blds = con.execute("SELECT project_code, name FROM buildings ORDER BY name").fetchall()
    bld_json = json.dumps([{"p": r[0], "n": r[1]} for r in blds])
    lvls = con.execute(
        "SELECT pl.project_code, l.label FROM project_levels pl "
        "JOIN levels l ON l.id=pl.level_id "
        "ORDER BY (l.number IS NULL), l.number").fetchall()
    lvl_json = json.dumps([{"p": r[0], "n": r[1]} for r in lvls])
    flags_json = json.dumps(proj_flags)
    report = getv("report") or "manday"
    # Daily report always has a project selected; the filter form may be (any).
    # Visibility follows the filter project when set, otherwise the daily one.
    effective_proj = getv("project") or (proj_rows[0]["code"] if proj_rows else "")
    effective_flags = proj_flags.get(effective_proj, {"no_sheet_type": 0, "no_zone": 0})
    show_sheet = not effective_flags["no_sheet_type"]
    show_zone = not effective_flags["no_zone"]
    zone_hide = "" if show_zone else ' style="display:none"'
    sheet_hide = "" if show_sheet else ' style="display:none"'

    def opts(items, sel):
        s = '<option value="">(any)</option>'
        for it in items:
            s += f'<option value="{html.escape(str(it))}"{" selected" if str(it)==sel else ""}>{html.escape(str(it))}</option>'
        return s

    def rsel(v, label):
        return f'<option value="{v}"{" selected" if report==v else ""}>{label}</option>'

    daily_date = getv("dfrom") or dt.date.today().isoformat()
    daily_form = (
        '<form method="POST" action="/run" class="filters" style="margin-bottom:12px">'
        '<input type="hidden" name="tool" value="export">'
        '<div><label>Project code</label><select name="project" id="daily_project" required>'
        + "".join(
            f'<option value="{html.escape(r[0])}" data-name="{html.escape(r[1] or "")}"'
            f'{" selected" if r[0]==getv("project") else ""}>{html.escape(r[0])}</option>'
            for r in proj_rows)
        + '</select></div>'
        '<div class="grow"><label>Project name</label>'
        '<input id="daily_project_name" readonly></div>'
        f'<div><label>Daily report date</label><input type="date" name="dfrom" value="{html.escape(daily_date)}"></div>'
        f'<div><label>Location</label><select name="location">{opts(OFFICE_LOCATIONS, getv("location"))}</select></div>'
        '<button type="submit" name="report" value="daily">Export Daily Report</button>'
        '<button type="submit" name="report" value="notfilled" formaction="/reports" formmethod="get" '
        'style="background:#8a3d00">Who didn\'t fill</button></form>'
        '<p class="sub">Daily Report Excel lists people who logged a task. '
        '<b>Who didn\'t fill</b> lists the rest of the project team for that date. '
        'The Excel file also has a <b>Not filled</b> sheet.</p>'
        '<script>(function(){var s=document.getElementById("daily_project");'
        'var n=document.getElementById("daily_project_name");if(!s||!n)return;'
        'function sync(){var o=s.options[s.selectedIndex];n.value=o?o.getAttribute("data-name")||"":"";}'
        's.addEventListener("change",sync);sync();})();</script>')
    filt = (
        '<form method="GET" action="/reports" class="filters">'
        '<div><label>Report</label><select name="report">'
        + rsel("manday", "Man-day Summary") + rsel("search", "Search")
        + rsel("internal", "Internal (effort by date)")
        + rsel("compare", "Forecast vs Actual")
        + rsel("matrix", "Zone / Level progress grid")
        + rsel("notfilled", "Who didn't fill (date range)") + '</select></div>'
        '<div><label>Project code</label><select name="project" id="rep_project">'
        '<option value="">(any)</option>'
        + "".join(
            f'<option value="{html.escape(r[0])}"{" selected" if r[0]==getv("project") else ""}>'
            f'{html.escape(r[0])} — {html.escape(r[1] or r[0])}</option>'
            for r in proj_rows)
        + '</select></div>'
        f'<div id="filt_zone"{zone_hide}>'
        '<label>Zone</label><select name="building" id="bldsel"><option value="">(any)</option></select></div>'
        '<div><label>Level</label><select name="level" id="lvlsel"><option value="">(any)</option></select></div>'
        f'<div><label>Person (name or Emp ID)</label>'
        f'<input name="person" list="people_list" value="{html.escape(getv("person"))}" '
        f'placeholder="Type name or Emp ID" autocomplete="off">'
        f'<datalist id="people_list">'
        + "".join(
            f'<option value="{html.escape(r[1])}">{html.escape(r[1])}'
            f'{(" (" + html.escape(r[2]) + ")") if r[2] else ""}</option>'
            + (f'<option value="{html.escape(r[2])}">{html.escape(r[1])} ({html.escape(r[2])})</option>'
               if r[2] else "")
            for r in people_rows)
        + '</datalist></div>'
        f'<div><label>Location</label><select name="location">{opts(OFFICE_LOCATIONS, getv("location"))}</select></div>'
        f'<div id="filt_sheet"{sheet_hide}>'
        f'<label>Sheet Type</label><select name="sheet_type">{opts(sheet_types, getv("sheet_type"))}</select></div>'
        '<div class="brk"></div>'
        '<div class="wide"><label>Master Task (pick Project first)</label>'
        '<select name="master_task" id="mtsel"><option value="">(any)</option></select></div>'
        '<div class="brk"></div>'
        f'<div><label>Category</label><select name="category">{opts(categories, getv("category"))}</select></div>'
        f'<div><label>Task Name</label><select name="task_name">{opts(task_names, getv("task_name"))}</select></div>'
        f'<div class="wide"><label>Sub Task</label><select name="sub_task">{opts(sub_tasks, getv("sub_task"))}</select></div>'
        '<div class="brk"></div>'
        f'<div><label>From</label><input type="date" name="dfrom" value="{html.escape(getv("dfrom"))}"></div>'
        f'<div><label>To</label><input type="date" name="dto" value="{html.escape(getv("dto"))}"></div>'
        '<button type="submit">Run report</button>'
        '<button type="submit" formaction="/run" formmethod="post" name="tool" value="export" '
        'style="background:#1f7a4d">Export to Excel</button>'
        '<button type="button" onclick="window.print()" style="background:#555">Print</button>'
        '</form>')

    results = "<p class='sub'>Choose a report, set any filters (blank = ignore) and Run.</p>"
    if q:
        filters, params = [], []

        def add(col, key, like=False):
            v = getv(key)
            if v:
                filters.append(f"{col} LIKE ?" if like else f"{col}=?")
                params.append(f"%{v}%" if like else v)

        add("project_code", "project"); add("location", "location")
        if getv("person"):
            who = find_signin_person(getv("person"))
            if who:
                filters.append("person_id=?")
                params.append(who["id"])
            else:
                add("person", "person")
        run_flags = proj_flags.get(getv("project"), {})
        if not run_flags.get("no_sheet_type"):
            add("sheet_type", "sheet_type")
        add("task_name", "task_name")
        add("category", "category", like=True); add("sub_task", "sub_task", like=True)
        if getv("master_task"):
            add("master_task", "master_task")   # a specific master task already pins zone & level
        else:
            if not run_flags.get("no_zone"):
                add("building", "building")
            add("level", "level")
        if getv("dfrom"):
            filters.append("work_date>=?"); params.append(getv("dfrom"))
        if getv("dto"):
            filters.append("work_date<=?"); params.append(getv("dto"))
        where = ("WHERE " + " AND ".join(filters)) if filters else ""

        if report == "notfilled":
            proj = getv("project")
            start = getv("dfrom") or getv("dto") or dt.date.today().isoformat()
            end = getv("dto") or start
            loc = getv("location") or None
            if not proj:
                results = ("<p class='sub' style='color:#a33'><b>Pick a project code</b> "
                           "to see who on the team did not log a task in the date range.</p>")
            else:
                status = range_fill_status(con, start, end, proj, loc)
                headers = ["Date", "Person", "Emp ID", "Role", "Location"]
                body = "".join(
                    "<tr>" + "".join(
                        f"<td>{html.escape('' if p[k] is None else str(p[k]))}</td>"
                        for k in ("missing_date", "name", "emp_code", "role", "location")) + "</tr>"
                    for p in status["missing"])
                hint = ""
                if not status["expected"]:
                    hint = ("<p class='sub' style='color:#a33'><b>No project team.</b> "
                            "Add people in Manage Lists, then try again.</p>")
                elif not status["missing"]:
                    hint = ("<p class='sub' style='color:#1f7a4d'><b>Everyone on the team "
                            "logged a task</b> on this date.</p>")
                loc_bit = f" · Location {html.escape(loc)}" if loc else ""
                results = (
                    hint +
                    f"<p class='sub'><b>Who didn't fill</b> — {html.escape(proj)} · {html.escape(start)} to {html.escape(end)}"
                    f"{loc_bit} · compared against {html.escape(status['source'] or 'team')} · "
                    f"{status['expected']} expected person/dates · "
                    f"{status['filled']} filled person/dates · "
                    f"<b>{len(status['missing'])} missing person/dates</b></p>"
                    "<p class='sub'>Only weekdays are checked. People added after a date are not listed. "
                    "People stay listed through Left on and drop off from the next working day "
                    "(Saturday and Sunday skipped). "
                    "<b>Managers</b> are not listed — they cover several projects and their work is supervisory.</p>"
                    f"<table class='rep'><thead><tr>"
                    + "".join(f"<th>{h}</th>" for h in headers) +
                    f"</tr></thead><tbody>{body}</tbody></table>")
            rows = None
        elif report == "matrix":
            proj = getv("project") or (projects[0] if projects else "")
            results = render_matrix(
                matrix_data(proj, getv("building"), getv("level"),
                            getv("dfrom"), getv("dto")), proj)
            rows = None
        elif report == "compare":
            proj = getv("project")
            if not proj:
                results = ("<p class='sub' style='color:#a33'><b>Pick a project code</b> "
                           "and a date range, then Run to see Forecast vs Actual.</p>")
            else:
                start = getv("dfrom") or dt.date.today().isoformat()
                end = getv("dto") or start
                qwin = {"project": [proj], "start": [start], "end": [end],
                        "person": [getv("person")]}
                project, start, days = forecast_app.window_from(qwin, reference_data(), DB_PATH)
                person_id = forecast_app.resolve_person_id(DB_PATH, getv("person")) if getv("person") else None
                qs = forecast_app.window_qs(project, start, days, person=getv("person"),
                                            end=forecast_app.window_end(start, days))
                cmp = forecast_app.compare_forecast_actual(
                    DB_PATH, project, start, days, person_id)
                results = (
                    f"<p class='sub'><b>Forecast vs Actual</b> — {html.escape(project)} "
                    f"· {html.escape(start)} to {html.escape(forecast_app.window_end(start, days))}"
                    + (f" · {html.escape(getv('person'))}" if getv("person") else "")
                    + "</p>"
                    + forecast_app._compare_html(cmp, qs, table_class="rep"))
            rows = None
        elif report == "search":
            headers = ["Date", "Person", "Project", "Zone", "Level", "Master Task",
                       "Category", "Task", "Sub Task", "Description", "Status", "Hours"]
            sql = (f"SELECT work_date,person,project_code,building,level,master_task,category,task_name,"
                   f"sub_task,description,status,ROUND(hours,2) FROM v_task_effort {where} ORDER BY work_date,person")
            total_cols = []
        elif report == "internal":
            headers = ["Person", "Tasks", "Hours", "Man-days"]
            where = with_managers_excluded(where)
            sql = (f"SELECT person,COUNT(*),ROUND(SUM(spent_hours),2),ROUND(SUM(norm_manday),3) "
                   f"FROM v_task_effort {where} GROUP BY person ORDER BY person")
            total_cols = [1, 2, 3]
        else:
            report = "manday"
            headers = ["Person", "Category", "Task", "Tasks", "Hours", "Man-days"]
            where = with_managers_excluded(where)
            sql = (f"SELECT person,COALESCE(category,''),task_name,COUNT(*),ROUND(SUM(spent_hours),2),"
                   f"ROUND(SUM(norm_manday),3) FROM v_task_effort {where} "
                   f"GROUP BY person,category,task_name ORDER BY person,category,task_name")
            total_cols = [3, 4, 5]
        if report in ("matrix", "notfilled", "compare"):
            rows = None          # these reports build their own markup, not a flat table
        else:
            try:
                rows = con.execute(sql, params).fetchall()
            except Exception as e:
                con.close()
                results = f"<p class='sub'>Query error: {html.escape(str(e))}</p>"
                rows = None
        if rows is not None:
            body = ""
            totals = {}
            for row in rows:
                body += "<tr>" + "".join(
                    f"<td>{html.escape('' if v is None else str(v))}</td>" for v in row) + "</tr>"
                for ci in total_cols:
                    totals[ci] = totals.get(ci, 0) + (row[ci] or 0)
            trow = ""
            if total_cols and rows:
                cells = []
                for i in range(len(headers)):
                    if i == 0:
                        cells.append("<td><b>Total</b></td>")
                    elif i in totals:
                        cells.append(f"<td><b>{round(totals[i], 2)}</b></td>")
                    else:
                        cells.append("<td></td>")
                trow = "<tr class='total'>" + "".join(cells) + "</tr>"
            head = "".join(f"<th>{h}</th>" for h in headers)
            hint = ""
            if not rows:
                if getv("master_task"):
                    hint = ("<p class='sub' style='color:#a33'><b>No matching tasks.</b> "
                            "You picked a specific <b>Master Task</b> that this person/date "
                            "didn't log. Set <b>Master Task</b> to <b>(any)</b> and use "
                            "Level / Zone for a broader slice.</p>")
                else:
                    hint = ("<p class='sub' style='color:#a33'><b>No matching tasks.</b> "
                            "Try a wider date range or clear a filter (set it to (any)).</p>")
            results = (hint + f"<p class='sub'>{len(rows)} row(s)</p>"
                       f"<table class='rep'><thead><tr>{head}</tr></thead>"
                       f"<tbody>{body}{trow}</tbody></table>")
            con.close()

    # Master Task / Zone / Level follow the chosen project. Sheet Type and Zone
    # visibility follow Manage Lists (no_sheet_type / no_zone) for that project.
    mt_script = (
        "<script>var MTS=" + mt_json + ";var MTCUR=" + json.dumps(getv("master_task")) + ";"
        "var BLDS=" + bld_json + ";var BLDCUR=" + json.dumps(getv("building")) + ";"
        "var LVLS=" + lvl_json + ";var LVLCUR=" + json.dumps(getv("level")) + ";"
        "var PROJFLAGS=" + flags_json + ";"
        "function gval(sq){var e=document.querySelector(sq);return e?e.value:'';}"
        "function setSel(sq,v){var el=document.querySelector(sq);if(!el)return;"
        "for(var i=0;i<el.options.length;i++){if(el.options[i].value===v){el.selectedIndex=i;return;}}}"
        "function reportProject(){var r=document.getElementById('rep_project');"
        "if(r&&r.value)return r.value;"
        "var d=document.getElementById('daily_project');return d?d.value:'';}"
        "function showFilt(id,on){var el=document.getElementById(id);if(!el)return;"
        "el.style.display=on?'':'none';var inp=el.querySelector('select,input');"
        "if(inp){inp.disabled=!on;if(!on)inp.value='';}}"
        "function applyProjectFilters(){var p=reportProject();var f=PROJFLAGS[p]||{};"
        "showFilt('filt_sheet',!Number(f.no_sheet_type||0));"
        "showFilt('filt_zone',!Number(f.no_zone||0));}"
        "function dB(n){var m=n.match(/Block ([A-Za-z0-9])/);if(m)return 'Block '+m[1];"
        "var m2=n.match(/_B([0-9]+)_/);if(m2)return 'B'+m2[1];return 'All Blocks';}"
        "function dL(n){var ml=n.match(/_L(-?[0-9]+)/);return ml?('Level '+parseInt(ml[1],10)):'All levels';}"
        "function fillBld(){var p=reportProject(),sel=document.getElementById('bldsel');"
        "if(!sel)return;var keep=sel.value||BLDCUR;var items=BLDS.filter(function(x){return (!p||x.p===p);});"
        "sel.innerHTML='<option value=\"\">(any)</option>'+items.map(function(x){"
        "var s=(x.n===keep)?' selected':'';"
        "return '<option value=\"'+x.n.replace(/\"/g,'&quot;')+'\"'+s+'>'+x.n+'</option>';}).join('');}"
        "function fillLvl(){var p=reportProject(),sel=document.getElementById('lvlsel');"
        "if(!sel)return;var keep=sel.value||LVLCUR;var items=LVLS.filter(function(x){return (!p||x.p===p);});"
        "sel.innerHTML='<option value=\"\">(any)</option>'+items.map(function(x){"
        "var s=(x.n===keep)?' selected':'';"
        "return '<option value=\"'+x.n.replace(/\"/g,'&quot;')+'\"'+s+'>'+x.n+'</option>';}).join('');}"
        "function fillMT(){var p=reportProject(),st=gval('[name=sheet_type]'),"
        "b=gval('[name=building]'),l=gval('[name=level]'),sel=document.getElementById('mtsel');"
        "if(!sel)return;var keep=sel.value||MTCUR;"
        "var items=MTS.filter(function(m){return (!p||m.p===p)&&(!st||m.st===st)"
        "&&(!b||dB(m.n)===b)&&(!l||dL(m.n)===l);});"
        "sel.innerHTML='<option value=\"\">(any)</option>'+items.map(function(m){"
        "var s=(m.n===keep)?' selected':'';"
        "return '<option value=\"'+m.n.replace(/\"/g,'&quot;')+'\"'+s+'>'+m.n+'</option>';}).join('');}"
        "function autofillBL(){var sel=document.getElementById('mtsel');if(!sel||!sel.value)return;"
        "setSel('[name=building]',dB(sel.value));setSel('[name=level]',dL(sel.value));}"
        "function refreshFilters(){applyProjectFilters();fillBld();fillLvl();fillMT();}"
        "var pe=document.getElementById('rep_project');"
        "if(pe)pe.addEventListener('change',refreshFilters);"
        "var de=document.getElementById('daily_project');"
        "if(de)de.addEventListener('change',refreshFilters);"
        "var se=document.querySelector('[name=sheet_type]');if(se)se.addEventListener('change',fillMT);"
        "var be=document.querySelector('[name=building]');if(be)be.addEventListener('change',fillMT);"
        "var le=document.querySelector('[name=level]');if(le)le.addEventListener('change',fillMT);"
        "var mt=document.getElementById('mtsel');if(mt)mt.addEventListener('change',autofillBL);"
        "refreshFilters();</script>")
    return ("<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            "<title>Reports</title>" + ADMIN_STYLE + REPORTS_CSS + "</head><body><div class='wrap'>"
            "<div class='nav'><a href='/'>Task Entry</a> &nbsp;·&nbsp; "
            "<a href='/forecast'>Forecast</a> &nbsp;·&nbsp; "
            "<a href='/admin'>Manage Lists</a> &nbsp;·&nbsp; <b>Reports</b></div>"
            + banner +
            "<h1>Reports</h1>"
            "<p class='sub'>Daily Report (who filled), Who didn't fill, Forecast vs Actual, Man-day Summary, Search and Internal effort — set filters and Run. "
            "1 man-day = <b>8.25 hours</b> (09:00–18:30 minus 75 min breaks). Extra time after 18:30 is counted. "
            "<b>Managers are not counted</b> in Who didn't fill, Man-day Summary, Internal effort, Daily Report, or Forecast vs Actual. "
            "Grouped tasks that copy the same In/Out share that day's hours. Use Print for a clean copy.</p>"
            + daily_form + filt + results + mt_script + "</div></body></html>")


def render_denied():
    return ("<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            "<title>Restricted</title>" + ADMIN_STYLE + "</head><body><div class='wrap'>"
            "<div class='nav'><a href='/'>Task Entry</a> &nbsp;·&nbsp; "
            "<a href='/forecast'>Forecast</a> &nbsp;·&nbsp; <a href='/reports'>Reports</a></div>"
            "<h1>Manage Lists is restricted</h1>"
            "<p class='sub'>This page is for <b>Managers</b>, <b>Team Leaders</b> and "
            "<b>Senior Team Leaders</b> only. "
            "You're signed in with a different role. Ask an admin to change your role if you need access. "
            "&nbsp;·&nbsp; <a href='/signout'>sign in as someone else</a></p>"
            "</div></body></html>")


def render_signin(next_url="/", message=""):
    warn = f'<p class="sub" style="color:#a33">{html.escape(message)}</p>' if message else ""
    return ("<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            "<title>Sign in</title>" + ADMIN_STYLE + "</head><body><div class='wrap'>"
            "<div class='nav'><a href='/'>Task Entry</a> &nbsp;·&nbsp; "
            "<a href='/forecast'>Forecast</a> &nbsp;·&nbsp; <a href='/reports'>Reports</a></div>"
            "<h1>Sign in</h1>"
            "<p class='sub'>Enter your <b>Emp ID</b> or your <b>name</b> — no password. It's remembered on "
            "this browser until you change it.</p>" + warn +
            "<form class='row' method='POST' action='/signin'>"
            f"<input type='hidden' name='next' value='{html.escape(next_url)}'>"
            "<div class='grow'><label>Emp ID or name</label><input name='emp_id' autofocus></div>"
            "<button type='submit'>Continue</button></form>"
            "</div></body></html>")


class Handler(BaseHTTPRequestHandler):
    def _send(self, body, code=200, ctype="text/html; charset=utf-8"):
        b = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        # Every page embeds the current dropdown data, so a cached copy serves
        # stale lists. Without this a browser can keep showing the old form
        # across refreshes after the server is updated.
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        _reload_forecast_if_changed()
        parsed = urlparse(self.path)
        path = parsed.path
        q = parse_qs(parsed.query)
        if path == "/signin":
            self._send(render_signin(q.get("next", ["/"])[0]))
            return
        if path == "/signout":
            self.send_response(303)
            self.send_header("Set-Cookie", "uid=; Path=/; Max-Age=0")
            self.send_header("Location", "/signin")
            self.end_headers()
            return
        if path == "/admin":
            try:
                if is_admin(self):
                    self._send(render_admin(q.get("msg", [""])[0], whoami_banner(self), self))
                elif signed_in_person(self):
                    self._send(render_denied(), 403)
                else:
                    self._send(render_signin("/admin", "Manage Lists is for Managers, Team Leaders and Senior Team Leaders — sign in."))
            except Exception as e:
                log("Manage Lists failed: %s" % e)
                self._send(
                    "<!doctype html><meta charset='utf-8'><title>Manage Lists</title>"
                    "<p>Manage Lists could not load. Refresh and try again.</p>"
                    f"<pre>{html.escape(str(e))}</pre>",
                    500)
            return
        if path == "/team-map-template.xlsx":
            if not is_admin(self):
                self._send(render_denied() if signed_in_person(self) else render_signin("/admin"), 403)
                return
            project = (q.get("project", [""])[0] or "").strip()
            try:
                from bulk_import import team_map_workbook_bytes
                data = team_map_workbook_bytes(DB_PATH, project or None)
            except Exception as e:
                self._send(f"Could not build the mapping template: {html.escape(str(e))}", 400, "text/plain")
                return
            fname = f"team_mapping_{project or 'template'}.xlsx"
            self.send_response(200)
            self.send_header("Content-Type",
                             "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/employee-locations.xlsx":
            if not is_admin(self):
                self._send(render_denied() if signed_in_person(self) else render_signin("/admin"), 403)
                return
            try:
                from bulk_import import location_workbook_bytes
                data = location_workbook_bytes(DB_PATH)
            except Exception as e:
                self._send(f"Could not build the location sheet: {html.escape(str(e))}", 400, "text/plain")
                return
            self.send_response(200)
            self.send_header("Content-Type",
                             "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.send_header("Content-Disposition",
                             'attachment; filename="employee_locations.xlsx"')
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/result":
            if not signed_in_person(self):
                self._send(render_signin("/reports", "Sign in to view report results."))
                return
            got = RUN_RESULTS.get(q.get("id", [""])[0])
            if not got:
                # Server restarted, or the result aged out of the last 20.
                self._send(render_result("That result is no longer available",
                                         "Run the report again from the Reports page."))
                return
            self._send(render_result(*got))
            return
        if path == "/reports":
            if not signed_in_person(self):
                self._send(render_signin("/reports", "Sign in with your Emp ID to view reports."))
                return
            self._send(render_reports(q, whoami_banner(self)))
            return
        if path == "/forecast":
            if not signed_in_person(self):
                self._send(render_signin("/forecast", "Sign in with your Emp ID to open Forecast."))
                return
            flash = q.get("msg", [""])[0]
            page = forecast_app.render_forecast(
                DB_PATH, q, whoami_banner(self), is_admin(self),
                reference_data(), flash=flash)
            self._send(page)
            return
        if path == "/forecast/export":
            if not signed_in_person(self):
                self._send(render_signin("/forecast", "Sign in to export Forecast reports."))
                return
            project, start, days = forecast_app.window_from(q, reference_data(), DB_PATH)
            kind = (q.get("kind", ["modelling"])[0] or "modelling").strip().lower()
            if kind not in ("modelling", "sheet", "compare", "progress", "loading"):
                kind = "modelling"
            if not project:
                self._send("Choose a project first.", 400, "text/plain")
                return
            person_id = forecast_app.resolve_person_id(DB_PATH, forecast_app.person_from(q))
            try:
                fname, data = forecast_app.export_report(
                    DB_PATH, project, start, days, kind, person_id)
            except Exception as e:
                self._send(f"Could not export: {html.escape(str(e))}", 400, "text/plain")
                return
            self.send_response(200)
            self.send_header("Content-Type",
                             "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/forecast/subtask-hours.xlsx":
            if not signed_in_person(self):
                self._send(render_signin("/forecast", "Sign in to download the hours template."))
                return
            project = (q.get("project", [""])[0] or "").strip()
            if not project:
                self._send("Choose a project first.", 400, "text/plain")
                return
            try:
                fname, data = forecast_app.subtask_hours_workbook_bytes(DB_PATH, project)
            except Exception as e:
                self._send(f"Could not build the hours sheet: {html.escape(str(e))}", 400, "text/plain")
                return
            self.send_response(200)
            self.send_header("Content-Type",
                             "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/download":
            fname = os.path.basename(q.get("file", [""])[0])
            fpath = os.path.join(HERE, fname)
            if fname.lower().endswith(".xlsx") and os.path.isfile(fpath):
                with open(fpath, "rb") as fh:
                    data = fh.read()
                self.send_response(200)
                self.send_header("Content-Type",
                                 "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self._send("File not found", 404, "text/plain")
            return
        if path not in ("/", "/index.html"):
            self._send("Not found", 404, "text/plain")
            return
        # Entry is attributed work, so identity comes first. Without this the
        # form saved rows with no assignee at all - owned by nobody.
        if not signed_in_person(self):
            self._send(render_signin("/", "Enter your Emp ID to start logging tasks."))
            return

        me = signed_in_person(self)
        manager = is_manager(self)
        edit_json, edit_id, ok = "null", "", ""
        if "edit" in q:
            edit_task_id = q["edit"][0]
            d = (get_task_for_edit(edit_task_id)
                 if can_edit_task(edit_task_id, me["id"], manager) else None)
            if d:
                edit_json, edit_id = json.dumps(d), str(d["id"])
                ok = (f'<div class="ok">Editing task #{html.escape(edit_id)} — change fields and '
                      f'Update, or Clear form to cancel.</div>')
            else:
                ok = '<div class="err">You can edit only your own task entries. Managers can edit any entry.</div>'
        elif "ok" in q:
            tid = html.escape(q["ok"][0])
            ok = (f'<div class="ok">Updated task #{tid}.</div>' if "upd" in q else
                  f'<div class="ok">Saved task #{tid}. Change In/Out if you are logging another '
                  f'slice of the same day; a task that already runs to 18:30 or later cannot be '
                  f'saved again today.</div>')
        elif "leave" in q:
            ok = (f'<div class="ok">Leave recorded for {html.escape(fmt_day(q["leave"][0]))}'
                f' to {html.escape(fmt_day(q["leave"][1] if len(q["leave"]) > 1 else q["leave"][0]))}.'
                f' You will not appear in Who didn\'t fill for those dates.</div>')
        elif "err" in q:
            ok = f'<div class="err">{html.escape(q["err"][0])}</div>'
        elif "del" in q:
            ok = f'<div class="ok">Removed task #{html.escape(q["del"][0])}.</div>'

        def _row_html(r):
            tid = r["id"]
            edit = (f'<td><a href="/?edit={tid}">Edit</a></td>'
                    if manager or r["entered_by"] == me["id"] else "<td></td>")
            remove = (
                f'<td><form method="POST" action="/delete" style="margin:0" '
                f"onsubmit=\"return confirm('Remove this task?')\">"
                f'<input type="hidden" name="task_id" value="{tid}">'
                f'<button type="submit" style="background:#a33;padding:4px 10px">Remove</button>'
                "</form></td>"
                if r["entered_by"] == me["id"] else "<td></td>"
            )
            return (
                "<tr>"
                f'<td>{html.escape(str(r["date"]))}</td>'
                f'<td>{html.escape(str(r["who"]))}</td>'
                f'<td>{html.escape(str(r["task"]))}</td>'
                f'<td>{html.escape(str(r["time"]))}</td>'
                f'<td>{html.escape(str(r["hours"]))}</td>'
                f"{edit}"
                f"{remove}</tr>")

        con = connect()
        codes = [r[0] for r in con.execute(
            "SELECT code FROM projects WHERE project_status='Active' ORDER BY code")]
        con.close()
        recent = ""
        for code in codes:
            rows = recent_rows(project=code)
            body = "".join(_row_html(r) for r in rows) or (
                '<tr><td colspan="7" class="none">No entries for this project yet.</td></tr>')
            recent += (f'<tbody class="recent" data-proj="{html.escape(code)}" '
                       f'data-count="{len(rows)}" hidden>{body}</tbody>')
        # People sign in by Emp ID, so a team member's entry is their own —
        # showing them the whole roster invites logging work against someone
        # else. Admins still need the picker to log on another's behalf.
        if is_admin(self):
            assignees = (
                '<div class="full" id="assignees_flat"><label>Assignees'
                '<span class="bulk"><button type="button" data-tick-all="people">All</button>'
                '<button type="button" data-tick-none="people">None</button></span></label>'
                '<div class="people" id="people"></div></div>')
        elif me:
            assignees = (
                '<div class="full" id="assignees_flat"><label>Assignee</label>'
                f'<div class="whoami" style="margin:0">This task will be logged for '
                f'<b>{html.escape(me["name"])}</b>'
                f'{" &mdash; " + html.escape(me["role"]) if me["role"] else ""}.</div>'
                f'<input type="hidden" name="assignees" value="{me["id"]}"></div>')
        else:   # unreachable: GET / redirects to sign-in, but fail closed anyway
            assignees = (
                '<div class="full"><label>Assignee</label>'
                '<div class="whoami" style="margin:0">You are not signed in &mdash; '
                '<a href="/signin">sign in</a> so the task is logged against your name.</div></div>')
        leave_edit = parse_iso_date(q.get("leave_edit", [""])[0]) if q.get("leave_edit") else None
        leave_from = leave_edit or today_iso()
        leave_to = leave_edit or today_iso()
        leave_rows = "".join(
            f'<tr><td>{html.escape(fmt_day(day))}</td>'
            f'<td><a href="/?leave_edit={html.escape(day)}">Edit</a></td>'
            f'<td><form method="POST" action="/leave-remove" style="margin:0" '
            f'onsubmit="return confirm(\'Remove leave for {html.escape(day)}?\')">'
            f'<input type="hidden" name="work_date" value="{html.escape(day)}">'
            f'<button type="submit" style="background:#a33;padding:4px 10px">Remove</button></form></td></tr>'
            for day in leave_days_for(me["id"]))
        leave_html = (
            '<section style="background:#fff7ed;border:1px solid #f0c28b;border-radius:10px;padding:14px 18px;margin:0 0 18px">'
            '<h2 style="margin:0 0 4px">Leave management</h2>'
            '<p class="sub">Only your own leave dates are shown. Enter a range, then save, edit, or remove dates here.</p>'
            '<form method="POST" action="/leave" style="background:transparent;border:0;padding:0;margin:0 0 10px">'
            '<div class="grid">'
            f'<div><label>Leave From</label><input type="date" name="leave_from" value="{html.escape(leave_from)}" required></div>'
            f'<div><label>Leave To</label><input type="date" name="leave_to" value="{html.escape(leave_to)}" required></div>'
            '</div><div class="actions" style="margin-top:10px">'
            '<button type="submit" style="background:#8a3d00">Save leave</button>'
            '<span class="hint">Weekends are skipped automatically.</span></div></form>'
            '<table style="margin-top:10px"><thead><tr><th>Date</th><th>Edit</th><th>Remove</th></tr></thead><tbody>'
            + (leave_rows or '<tr><td colspan="3" class="none">No leave dates recorded.</td></tr>')
            + '</tbody></table></section>')
        page = PAGE.format(assignees=assignees, ok=ok, today=dt.date.today().isoformat(),
                           leave_from=html.escape(leave_from), leave_to=html.escape(leave_to),
                           leave_html=leave_html,
                           ref=json.dumps(reference_data()), recent=recent,
                           sticky=STICKY_JS, edit_json=edit_json, edit_id=edit_id,
                           whoami=whoami_banner(self),
                           me_json=json.dumps({"id": me["id"], "role": (me["role"] or "")} if me else None),
                           my_day=json.dumps(own_day_slices(me["id"]) if me else []))
        self._send(page)

    def do_POST(self):
        _reload_forecast_if_changed()
        length = int(self.headers.get("Content-Length", 0))
        form, files = parse_request_body(self, self.rfile.read(length))

        if self.path == "/signin":
            emp = form.get("emp_id", [""])[0].strip()
            nxt = form.get("next", ["/"])[0] or "/"
            row = find_signin_person(emp)
            if not row:
                self._send(render_signin(nxt, f"'{emp}' was not found — use your Emp ID or the name on Manage Lists."))
                return
            self.send_response(303)
            self.send_header("Set-Cookie", f"uid={row['id']}; Path=/; Max-Age=315360000")  # ~10 years
            self.send_header("Location", nxt)
            self.end_headers()
            return

        if self.path == "/leave":
            me = signed_in_person(self)
            if not me:
                self._send(render_signin("/", "Sign in to mark your leave."), 403)
                return
            try:
                start, end = mark_leave(me["id"], form.get("leave_from", [""])[0],
                                        form.get("leave_to", [""])[0])
            except Exception as e:
                self.send_response(303)
                self.send_header("Location", "/?err=" + quote(str(e), safe=""))
                self.end_headers()
                return
            self.send_response(303)
            self.send_header("Location", "/?leave=" + quote(start, safe="") + "&leave=" + quote(end, safe=""))
            self.end_headers()
            return

        if self.path == "/leave-remove":
            me = signed_in_person(self)
            if not me:
                self._send(render_signin("/", "Sign in to remove your leave."), 403)
                return
            try:
                day = remove_leave(me["id"], form.get("work_date", [""])[0])
            except Exception as e:
                self.send_response(303)
                self.send_header("Location", "/?err=" + quote(str(e), safe=""))
                self.end_headers()
                return
            self.send_response(303)
            self.send_header("Location", "/?leave_removed=" + quote(day, safe=""))
            self.end_headers()
            return

        if self.path == "/admin":
            if not is_admin(self):
                self._send(render_denied() if signed_in_person(self) else render_signin("/admin"), 403)
                return
            try:
                msg = admin_insert(form)
            except Exception as e:
                msg = f"Error: {e}"
            self.send_response(303)
            self.send_header("Location", f"/admin?msg={quote(msg)}")
            self.end_headers()
            return

        if self.path == "/bulk-team-map":
            if not is_admin(self):
                self._send(render_denied() if signed_in_person(self) else render_signin("/admin"), 403)
                return
            temp_path = None
            try:
                project = form.get("project", [""])[0].strip()
                upload = files.get("xlsx")
                if not upload or not upload[1]:
                    raise ValueError("Choose a mapping workbook.")
                with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as temp:
                    temp.write(upload[1])
                    temp_path = temp.name
                from bulk_import import import_team_map
                result = import_team_map(DB_PATH, temp_path, project)
                msg = (f"{project}: mapped {result['zones']} zone(s) to reviewers "
                       f"and {result['supports']} QC Support assignment(s) "
                       f"from {result['rows']} row(s).")
                if result.get("reviewers"):
                    msg += f" {result['reviewers']} employee(s) set as QC Reviewer."
                if result["created_zones"]:
                    msg += f" Created {result['created_zones']} new zone(s)."
                msg += " Grouped in teams is now on for this project."
                if result["errors"]:
                    msg += " Notes: " + " ".join(result["errors"][:8])
                    if len(result["errors"]) > 8:
                        msg += f" (+{len(result['errors']) - 8} more)"
            except Exception as e:
                msg = f"Team mapping import error: {e}"
            finally:
                if temp_path:
                    try:
                        os.unlink(temp_path)
                    except OSError:
                        pass
            self.send_response(303)
            self.send_header("Location", f"/admin?msg={quote(msg)}")
            self.end_headers()
            return

        if self.path == "/bulk-employee-locations":
            if not is_admin(self):
                self._send(render_denied() if signed_in_person(self) else render_signin("/admin"), 403)
                return
            temp_path = None
            try:
                upload = files.get("xlsx")
                if not upload or not upload[1]:
                    raise ValueError("Choose a location workbook.")
                with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as temp:
                    temp.write(upload[1])
                    temp_path = temp.name
                from bulk_import import import_employee_locations
                result = import_employee_locations(DB_PATH, temp_path)
                msg = (f"Updated location for {result['updated']} employee(s).")
                if result["skipped"]:
                    msg += f" Skipped {result['skipped']} row(s) with no Location or IP."
                if result["errors"]:
                    msg += " Notes: " + " ".join(result["errors"][:8])
                    if len(result["errors"]) > 8:
                        msg += f" (+{len(result['errors']) - 8} more)"
            except Exception as e:
                msg = f"Location import error: {e}"
            finally:
                if temp_path:
                    try:
                        os.unlink(temp_path)
                    except OSError:
                        pass
            self.send_response(303)
            self.send_header("Location", f"/admin?msg={quote(msg)}")
            self.end_headers()
            return

        if self.path in ("/bulk-tasks", "/bulk-models", "/bulk-subtasks", "/bulk-zones", "/bulk-levels", "/bulk-zones-levels", "/bulk-category-tasks"):
            if not is_manager(self):
                self._send(render_denied() if signed_in_person(self) else render_signin("/admin"), 403)
                return
            temp_path = None
            try:
                project = form.get("project", [""])[0].strip()
                from bulk_import import (import_catalog, import_models, import_subtasks, import_subtask_hierarchy,
                                         import_task_subtasks, import_zones_levels, import_zones_only, import_levels_only, import_project_levels,
                                         import_category_tasks)
                if self.path == "/bulk-models":
                    model_names = json.loads(form.get("model_names", ["[]"])[0] or "[]")
                    added = import_models(DB_PATH, project, model_names)
                    msg = f"Added {added} new RVT model(s) to project {project}."
                elif self.path == "/bulk-subtasks":
                    upload = files.get("xlsx")
                    if not upload or not upload[1]:
                        raise ValueError("Choose a Sub Task workbook.")
                    with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as temp:
                        temp.write(upload[1])
                        temp_path = temp.name
                    task_id = int(form.get("task_name_id", ["0"])[0])
                    category_id = int(form.get("category_id", ["0"])[0]) or None
                    added = import_task_subtasks(DB_PATH, temp_path, task_id, category_id)
                    msg = f"Added {added} Sub Task(s) under the selected Category and Task."
                elif self.path == "/bulk-zones-levels":
                    upload = files.get("xlsx")
                    if not upload or not upload[1]:
                        raise ValueError("Choose a Zones and Levels workbook.")
                    with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as temp:
                        temp.write(upload[1]); temp_path = temp.name
                    zones, levels = import_zones_levels(DB_PATH, temp_path, project)
                    msg = f"Added {zones} Zone(s) and {levels} Level(s)."
                elif self.path == "/bulk-category-tasks":
                    upload = files.get("xlsx")
                    if not upload or not upload[1]: raise ValueError("Choose the Review comment workbook.")
                    with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as temp:
                        temp.write(upload[1]); temp_path = temp.name
                    categories, tasks = import_category_tasks(DB_PATH, temp_path, int(form.get("master_task_id", ["0"])[0]))
                    msg = f"Added {categories} Categories and {tasks} Sub Task(s) under the Master Task."
                elif self.path == "/bulk-zones":
                    upload = files.get("xlsx")
                    if not upload or not upload[1]: raise ValueError("Choose a Zone workbook.")
                    with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as temp:
                        temp.write(upload[1]); temp_path = temp.name
                    msg = f"Added {import_zones_only(DB_PATH, temp_path, project)} Zone(s)."
                elif self.path == "/bulk-levels":
                    upload = files.get("xlsx")
                    if not upload or not upload[1]: raise ValueError("Choose a Level workbook.")
                    with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as temp:
                        temp.write(upload[1]); temp_path = temp.name
                    msg = f"Added {import_project_levels(DB_PATH, temp_path, project)} project Level(s)."
                else:
                    upload = files.get("xlsx")
                    if not upload or not upload[1]:
                        raise ValueError("Choose an Excel workbook.")
                    with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as temp:
                        temp.write(upload[1])
                        temp_path = temp.name
                    counts = import_catalog(DB_PATH, temp_path, project, [])
                    msg = "Task catalog import complete: " + ", ".join(
                        f"{name} +{count}" for name, count in counts.items() if count)
            except Exception as e:
                msg = f"Bulk import error: {e}"
            finally:
                if temp_path:
                    try:
                        os.unlink(temp_path)
                    except OSError:
                        pass
            self.send_response(303)
            self.send_header("Location", f"/admin?msg={quote(msg)}")
            self.end_headers()
            return

        if self.path == "/forecast/loading-plan":
            me = signed_in_person(self)
            if not me:
                self._send(render_signin("/forecast", "Sign in to assign work."), 403)
                return
            if not is_admin(self):
                self._send(render_denied(), 403)
                return
            project = (form.get("project", [""])[0] or "").strip()
            try:
                plan = forecast_app.save_loading_plan(DB_PATH, form, me["id"])
            except Exception as e:
                loc = "/forecast?" + forecast_app.window_qs(
                    project, (form.get("plan_start", [""])[0] or "").strip() or dt.date.today().isoformat(),
                    5, extra=forecast_app.floor_days_extra(form))
                loc += f"&msg={quote('Could not save loading plan: ' + str(e))}"
                self.send_response(303)
                self.send_header("Location", loc)
                self.end_headers()
                return
            sess = plan.get("session_start") or plan["start"]
            try:
                s = dt.date.fromisoformat(sess)
                e = dt.date.fromisoformat(plan["end"])
                span = max(1, (e - s).days + 1)
            except ValueError:
                sess, span = plan["start"], plan["days"]
            loc_q = forecast_app.window_qs(
                project, sess, span,
                extra="" if plan.get("replaced") or plan.get("appended") else forecast_app.floor_days_extra(form),
                end=plan["end"])
            if plan.get("replaced"):
                lead = "Updated this plan"
            elif plan.get("appended"):
                lead = "Added to this day"
            else:
                lead = "Loading plan saved"
            bits = [f"{lead}: {plan['n']} person-days"]
            bits.append(f"{plan['people']} people")
            bits.append(f"{plan['floors']} floors · {plan['work_days']} working days")
            bits.append(f"{plan['start']} to {plan['end']}")
            if plan["skipped"]:
                bits.append(f"{plan['skipped']} already booked (left as-is)")
            loc = f"/forecast?{loc_q}&msg={quote(' · '.join(bits))}"
            self.send_response(303)
            self.send_header("Location", loc)
            self.end_headers()
            return

        if self.path == "/forecast/delete-plan":
            if not signed_in_person(self):
                self._send(render_signin("/forecast", "Sign in to change assignments."), 403)
                return
            if not is_admin(self):
                self._send(render_denied(), 403)
                return
            project = (form.get("project", [""])[0] or "").strip()
            session_start = (form.get("session_start", [""])[0] or "").strip()
            start = (form.get("start", [""])[0] or "").strip()
            end = (form.get("end", [""])[0] or "").strip()
            qs = (form.get("qs", [""])[0] or "").strip()
            try:
                n = forecast_app.delete_loading_plan(
                    DB_PATH, project, session_start=session_start or None,
                    start=start or None, end=end or None)
            except Exception as e:
                loc = "/forecast?" + (qs + "&" if qs else f"project={quote(project)}&")
                loc += f"msg={quote('Could not remove plan: ' + str(e))}"
                self.send_response(303)
                self.send_header("Location", loc)
                self.end_headers()
                return
            loc = "/forecast?" + (f"project={quote(project)}" if project else "")
            loc += f"&msg={quote('Removed loading plan (' + str(n) + ' assignment(s)).')}"
            self.send_response(303)
            self.send_header("Location", loc)
            self.end_headers()
            return

        if self.path == "/forecast/add":
            me = signed_in_person(self)
            if not me:
                self._send(render_signin("/forecast", "Sign in to assign work."), 403)
                return
            if not is_admin(self):
                self._send(render_denied(), 403)
                return
            start = (form.get("start", [""])[0] or dt.date.today().isoformat()).strip()
            days = (form.get("days", ["5"])[0] or "5").strip()
            project = (form.get("project", [""])[0] or "").strip()
            loc_q = forecast_app.window_qs(
                project, start, int(days) if days.isdigit() else 5,
                person=(form.get("person", [""])[0] or "").strip(),
                end=(form.get("end", [""])[0] or "").strip())
            try:
                n, updated = forecast_app.save_assignment(DB_PATH, form, me["id"])
            except Exception as e:
                loc = f"/forecast?{loc_q}&msg={quote('Could not save: ' + str(e))}"
                self.send_response(303)
                self.send_header("Location", loc)
                self.end_headers()
                return
            loc = f"/forecast?{loc_q}&ok=1" + ("&upd=1" if updated else f"&n={n}")
            self.send_response(303)
            self.send_header("Location", loc)
            self.end_headers()
            return

        if self.path == "/forecast/delete":
            if not signed_in_person(self):
                self._send(render_signin("/forecast", "Sign in to change assignments."), 403)
                return
            if not is_admin(self):
                self._send(render_denied(), 403)
                return
            qs = (form.get("qs", [""])[0] or "").strip()
            try:
                forecast_app.delete_assignment(DB_PATH, form.get("id", [""])[0])
            except Exception as e:
                self._send(f"<pre>Could not delete: {html.escape(str(e))}</pre>", 400)
                return
            loc = "/forecast?" + (qs + "&" if qs else "") + "del=1"
            self.send_response(303)
            self.send_header("Location", loc)
            self.end_headers()
            return

        if self.path == "/forecast/subtask-hours":
            me = signed_in_person(self)
            if not me:
                self._send(render_signin("/forecast", "Sign in to upload hours."), 403)
                return
            if not is_admin(self):
                self._send(render_denied(), 403)
                return
            project = (form.get("project", [""])[0] or "").strip()
            qs = (form.get("qs", [""])[0] or "").strip()
            loc_q = qs or f"project={quote(project)}"
            temp_path = None
            try:
                upload = files.get("xlsx")
                if not upload or not upload[1]:
                    raise ValueError("Choose the hours workbook.")
                if not project:
                    raise ValueError("Choose a project first.")
                with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as temp:
                    temp.write(upload[1])
                    temp_path = temp.name
                result = forecast_app.import_subtask_hours(DB_PATH, temp_path, project)
                msg = f"Saved hours for {result['saved']} sub-task(s)."
                if result["errors"]:
                    msg += " " + " ".join(result["errors"][:6])
                    if len(result["errors"]) > 6:
                        msg += f" (+{len(result['errors']) - 6} more)"
            except Exception as e:
                msg = f"Could not import hours: {e}"
            finally:
                if temp_path:
                    try:
                        os.unlink(temp_path)
                    except OSError:
                        pass
            self.send_response(303)
            self.send_header("Location", f"/forecast?{loc_q}&msg={quote(msg)}")
            self.end_headers()
            return

        if self.path == "/run":
            tool = form.get("tool", [""])[0]
            if tool in ("import", "sync") and not is_admin(self):
                self._send(render_denied() if signed_in_person(self) else render_signin("/admin"), 403)
                return
            extra = []
            if tool == "export":
                g = lambda k: form.get(k, [""])[0].strip()
                report = g("report") or "daily"
                if report == "compare":
                    project = g("project")
                    if not project:
                        rid = store_result("Export Forecast vs Actual",
                                           "Pick a project code first.", None)
                        self.send_response(303)
                        self.send_header("Location", f"/result?id={rid}")
                        self.end_headers()
                        return
                    start = g("dfrom") or dt.date.today().isoformat()
                    end = g("dto") or start
                    qwin = {"project": [project], "start": [start], "end": [end],
                            "person": [g("person")]}
                    project, start, days = forecast_app.window_from(
                        qwin, reference_data(), DB_PATH)
                    person_id = forecast_app.resolve_person_id(DB_PATH, g("person")) if g("person") else None
                    try:
                        fname, data = forecast_app.export_report(
                            DB_PATH, project, start, days, "compare", person_id)
                    except Exception as e:
                        rid = store_result("Export Forecast vs Actual",
                                           f"Could not export: {e}", None)
                        self.send_response(303)
                        self.send_header("Location", f"/result?id={rid}")
                        self.end_headers()
                        return
                    out_path = os.path.join(HERE, fname)
                    with open(out_path, "wb") as fh:
                        fh.write(data)
                    rid = store_result("Export Forecast vs Actual",
                                       f"Exported -> {out_path}", fname)
                    self.send_response(303)
                    self.send_header("Location", f"/result?id={rid}")
                    self.end_headers()
                    return
                if report == "notfilled":
                    report = "daily"
                extra += ["--report", report]
                for key, flag in (("dfrom", "--from"), ("dto", "--to"), ("project", "--project"),
                                  ("building", "--building"), ("level", "--level"),
                                  ("person", "--person"), ("location", "--location"), ("sheet_type", "--sheet-type"),
                                  ("master_task", "--master-task"), ("task_name", "--task-name")):
                    v = g(key)
                    if v:
                        extra += [flag, v]
                if report == "daily":
                    rd = g("dfrom") or dt.date.today().isoformat()
                    out = f"reports_{rd}.xlsx"
                else:
                    out = f"{report}_{dt.datetime.now():%Y%m%d_%H%M%S}.xlsx"
                extra += ["--out", os.path.join(HERE, out)]
            title, output = run_tool(tool, extra)
            download = None
            for line in output.splitlines():
                if "Exported ->" in line:
                    download = os.path.basename(line.split("Exported ->", 1)[1].strip())
            rid = store_result(title, output, download)
            self.send_response(303)
            self.send_header("Location", f"/result?id={rid}")
            self.end_headers()
            return
            return

        if self.path == "/delete":
            tid = form.get("task_id", [""])[0]
            me = signed_in_person(self)
            if not me:
                self._send(render_signin("/", "Sign in to remove your own task entries."), 403)
                return
            try:
                removed = delete_task(tid, me["id"])
            except Exception as e:
                self._send(f"<pre>Could not delete: {html.escape(str(e))}</pre>", 400)
                return
            if not removed:
                self._send("You can only remove task entries created under your own sign-in.", 403,
                           "text/plain; charset=utf-8")
                return
            self.send_response(303)
            self.send_header("Location", f"/?del={html.escape(str(tid))}")
            self.end_headers()
            return

        if self.path != "/add":
            self._send("Not found", 404, "text/plain")
            return
        edit_id = form.get("edit_id", [""])[0].strip()
        me = signed_in_person(self)
        if not me:
            self._send(render_signin("/", "Your sign-in expired - sign in again to save this task."), 403)
            return
        if edit_id and not can_edit_task(edit_id, me["id"], is_manager(self)):
            self._send("You can edit only your own task entries. Managers can edit any entry.", 403,
                       "text/plain; charset=utf-8")
            return
        if me and not is_admin(self):
            form["assignees"] = [str(me["id"])]
        elif me and not [v for v in form.get("assignees", []) if v.strip()]:
            form["assignees"] = [str(me["id"])]
        block = blocked_full_day_entry(form, edit_id or None)
        if block:
            self.send_response(303)
            self.send_header("Location", "/?err=" + quote(block, safe=""))
            self.end_headers()
            return
        off = blocked_off_team_entry(form)
        if off:
            self.send_response(303)
            self.send_header("Location", "/?err=" + quote(off, safe=""))
            self.end_headers()
            return
        try:
            if edit_id:
                tid = update_task(edit_id, form)
                loc = f"/?ok={tid}&upd=1"
            else:
                tid = insert_task(form, me["id"])
                loc = f"/?ok={tid}"
        except Exception as e:
            self._send(f"<pre>Could not save: {html.escape(str(e))}</pre>"
                       f'<p><a href="/">back</a></p>', 400)
            return
        self.send_response(303)
        self.send_header("Location", loc)
        self.end_headers()

    def log_message(self, *a):
        pass  # quiet


LOG_PATH = os.path.join(HERE, "server.log")


def log(msg):
    """Write to the console if there is one, and always to server.log.

    pythonw has no console, so the log file is how we see why the page is down.
    """
    line = "%s  %s" % (dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 1_000_000:
            os.replace(LOG_PATH, LOG_PATH + ".old")
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    try:
        print(msg)
    except Exception:
        pass


def prevent_sleep():
    """Keep this PC awake while the task log is serving the team.

    A sleeping host is why teammates 'cannot load the page' overnight and
    then it works again after someone sits down and wakes the machine.
    """
    try:
        import ctypes
        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    except Exception:
        pass


def start_address_watch(publish_dir, port, host):
    """If DHCP gives this PC a new IP, refresh the shared shortcut."""
    def loop():
        last = None
        while True:
            try:
                prevent_sleep()
                ip = lan_ip()
                if ip != last:
                    log("address now %s - publishing shortcut" % ip)
                    publish_address(publish_dir, ip, host, port)
                    last = ip
            except Exception as e:
                log("address watch: %s" % e)
            time.sleep(300)
    threading.Thread(target=loop, daemon=True, name="address-watch").start()


def lan_ip():
    """The address teammates must use to reach this PC.

    gethostbyname() picks whichever adapter Windows lists first, which on a
    multi-NIC machine can be an idle card holding a useless 169.254 address.
    Ask the routing table which interface actually reaches the network instead.
    """
    ip = ""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))   # sends nothing; just resolves the route
        ip = s.getsockname()[0]
    except OSError:
        pass
    finally:
        s.close()
    if ip and not ip.startswith(("127.", "169.254.")):
        return ip
    try:
        for cand in socket.gethostbyname_ex(socket.gethostname())[2]:
            if not cand.startswith(("127.", "169.254.")):
                return cand
    except OSError:
        pass
    return ip or "127.0.0.1"


def publish_address(folder, ip, host, port):
    """Write a double-click shortcut to the shared drive.

    Everyone already reaches the share, so it is the one place every teammate
    can look up the current address. Rewritten on every startup, so a new DHCP
    lease repairs itself. check_connection.bat reads the .txt for its target.
    The share being unreachable must never stop the server from running.
    """
    if not folder:
        return None
    link = os.path.join(folder, "JSE Task Log.url")
    try:
        with open(link, "w", encoding="utf-8") as f:
            f.write("[InternetShortcut]\nURL=http://%s:%d\nIconIndex=0\n" % (ip, port))
        with open(os.path.join(folder, "tasklog_address.txt"), "w", encoding="utf-8") as f:
            f.write("%s\n%d\n%s\n" % (ip, port, host))
        return link
    except OSError as e:
        log("NOTE: could not publish the shortcut to %s (%s)" % (folder, e))
        return None


def main():
    global DB_PATH
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--publish-dir", default=PUBLISH_DIR,
                    help="folder for the teammate shortcut; pass '' to skip")
    args = ap.parse_args()
    DB_PATH = os.path.abspath(args.db)
    log("starting (db=%s)" % DB_PATH)
    if not os.path.exists(DB_PATH):
        log("Database not found: %s (run import_xlsx.py first)" % DB_PATH)
        sys.exit(f"Database not found: {DB_PATH} (run import_xlsx.py first)")
    ensure_hierarchy_schema()
    forecast_app.ensure_forecast_schema(DB_PATH)

    prevent_sleep()
    ip = lan_ip()
    host = socket.gethostname()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    log("Task entry form running.")
    log("  This PC:      http://localhost:%d" % args.port)
    log("  Teammates:    http://%s:%d" % (host, args.port))
    log("                http://%s:%d   (if the name does not work)" % (ip, args.port))
    log("  Database:     %s" % DB_PATH)
    link = publish_address(args.publish_dir, ip, host, args.port)
    if link:
        log("  Shortcut:     %s" % link)
    log("NOTE: teammates must NOT use localhost - that means their own PC.")
    start_address_watch(args.publish_dir, args.port, host)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("Stopped.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        log(traceback.format_exc())
        raise
