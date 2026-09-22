#!/usr/bin/env python
"""Bulk-import project catalog lists from a workbook and model filenames."""
import argparse
import json
import os
import re
import shutil
import sqlite3
import unicodedata
from datetime import datetime


CORE_TASKS = ("REVIEW COMMENTS", "CHECK MODEL HEALTH", "CHECK UPDATES")
REVIEW_TASK = "REVIEW COMMENTS"


def clean(value):
    return "" if value is None else str(value).strip()


def norm(value):
    return re.sub(r"[^a-z0-9]+", " ", clean(value).lower()).strip()


def parse_level_name(raw):
    """Turn typed text into (sort_number, label, is_all).

    Accepts F06, L01, B02, 6, Level 6. Digits are used only for sorting.
    """
    s = " ".join(str(raw or "").split())
    if not s:
        return None, None, False
    up = s.upper()
    if up in ("ALL LEVELS", "ALL LEVEL", "ALL"):
        return None, "All levels", True
    if re.fullmatch(r"-?\d+", s):
        num = int(s)
        if num == 0:
            return 0, "F00", False
        return num, f"Level {num}", False
    m = re.search(r"(-?\d+)", s)
    num = int(m.group(1)) if m else None
    if re.fullmatch(r"[A-Za-z]+[0-9]+[A-Za-z0-9]*", s):
        s = s.upper()
    if s.startswith("B") and num is not None and num > 0:
        num = -num
    return num, s, False


def add_unique(cur, sql, params):
    cur.execute(sql, params)
    return cur.rowcount > 0


def upsert_project_level(cur, project_code, raw, number=None, is_all=None):
    """Add a level on this project only. Same name on another project is a different row."""
    if not project_code:
        return None
    if number is None and is_all is None:
        number, label, is_all = parse_level_name(raw)
    else:
        label = (raw or "").strip() or None
    if not label:
        return None
    cur.execute(
        "INSERT OR IGNORE INTO levels(project_code,number,label,is_all) VALUES (?,?,?,?)",
        (project_code, number, label, int(bool(is_all))))
    row = cur.execute(
        "SELECT id FROM levels WHERE project_code=? AND label=?",
        (project_code, label)).fetchone()
    if not row:
        return None
    level_id = row[0]
    cur.execute(
        "INSERT OR IGNORE INTO project_levels(project_code,level_id) VALUES (?,?)",
        (project_code, level_id))
    return level_id


def put_on_project_team(cur, project_code, person_id, role_id=None):
    """Add someone to the current team without wiping their join date."""
    today = datetime.now().strftime("%Y-%m-%d")
    cols = {r[1] for r in cur.execute("PRAGMA table_info(project_people)")}
    if "joined_on" in cols:
        cur.execute(
            "INSERT OR IGNORE INTO project_people"
            "(project_code,person_id,role_id,joined_on) VALUES (?,?,?,?)",
            (project_code, person_id, role_id, today))
        if role_id is not None:
            extra = " AND left_on IS NULL" if "left_on" in cols else ""
            cur.execute(
                "UPDATE project_people SET role_id=? "
                f"WHERE project_code=? AND person_id=?{extra}",
                (role_id, project_code, person_id))
        return
    cur.execute(
        "INSERT OR IGNORE INTO project_people(project_code,person_id,role_id) VALUES (?,?,?)",
        (project_code, person_id, role_id))
    if role_id is not None:
        cur.execute(
            "UPDATE project_people SET role_id=? WHERE project_code=? AND person_id=?",
            (role_id, project_code, person_id))


def import_catalog(db_path, xlsx_path, project_code, model_names):
    import openpyxl

    if not os.path.isfile(db_path):
        raise RuntimeError(f"Database not found: {db_path}")
    if not os.path.isfile(xlsx_path):
        raise RuntimeError(f"Workbook not found: {xlsx_path}")

    workbook = openpyxl.load_workbook(xlsx_path, data_only=True, read_only=True)
    sheet = workbook["Lists"] if "Lists" in workbook.sheetnames else workbook.active
    rows = list(sheet.iter_rows(values_only=True))
    if not rows:
        raise RuntimeError("The selected workbook has no rows")

    headers = [clean(value) for value in rows[0]]
    header_map = {norm(value): index for index, value in enumerate(headers) if value}
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys = ON")
    cur = con.cursor()
    counts = {"models": 0, "buildings": 0, "master_tasks": 0, "sheet_types": 0,
              "levels": 0, "task_names": 0, "statuses": 0}

    if not cur.execute("SELECT 1 FROM projects WHERE code=?", (project_code,)).fetchone():
        raise RuntimeError(f"Project {project_code!r} does not exist; create it first")

    for model in model_names:
        if clean(model) and add_unique(cur,
                "INSERT OR IGNORE INTO models(project_code,name) VALUES (?,?)",
                (project_code, clean(model))):
            counts["models"] += 1

    def column(*names):
        for name in names:
            if norm(name) in header_map:
                return header_map[norm(name)]
        return None

    # Supports a simple row-based workbook as well as the existing Lists layout.
    project_col = column("project", "project code")
    sheet_col = column("sheet type", "sheet", "discipline")
    master_col = column("master task", "master task name", "midp", "task")
    building_col = column("building", "building name")
    level_col = column("level", "level name")
    task_name_col = column("task name", "sub task", "sub-task")
    status_col = column("status")

    def value(row, index):
        return clean(row[index]) if index is not None and index < len(row) else ""

    def sheet_type_id(name):
        row = cur.execute("SELECT id FROM sheet_types WHERE name=?", (name,)).fetchone()
        if row:
            return row[0]
        cur.execute("INSERT INTO sheet_types(name) VALUES (?)", (name,))
        counts["sheet_types"] += 1
        return cur.lastrowid

    for row in rows[1:]:
        row_project = value(row, project_col) or project_code
        if row_project != project_code:
            continue
        sheet_name = value(row, sheet_col)
        master_name = value(row, master_col)
        if sheet_name and master_name:
            stid = sheet_type_id(sheet_name)
            master_name = re.sub(r"^\[\d+\]\s*", "", master_name)
            if add_unique(cur, "INSERT OR IGNORE INTO master_tasks(project_code,sheet_type_id,name) VALUES (?,?,?)",
                          (project_code, stid, master_name)):
                counts["master_tasks"] += 1
        building = value(row, building_col)
        if building and add_unique(cur, "INSERT OR IGNORE INTO buildings(project_code,name,is_all) VALUES (?,?,?)",
                                   (project_code, building, int(building.lower().startswith("all")))):
            counts["buildings"] += 1
        level = value(row, level_col)
        if level:
            existed = cur.execute(
                "SELECT 1 FROM levels WHERE project_code=? AND label=?",
                (project_code, parse_level_name(level)[1])).fetchone()
            if upsert_project_level(cur, project_code, level) and not existed:
                counts["levels"] += 1
        task_name = value(row, task_name_col)
        if task_name and add_unique(cur, "INSERT OR IGNORE INTO task_names(name) VALUES (?)", (task_name,)):
            counts["task_names"] += 1
        status = value(row, status_col)
        if status and add_unique(cur, "INSERT OR IGNORE INTO statuses(name) VALUES (?)", (status,)):
            counts["statuses"] += 1

    # Existing Lists workbooks use one column per project/catalog.
    for index, header in enumerate(headers):
        match = re.fullmatch(r"Model Name \(([^)]+)\)", header, re.I)
        if match and match.group(1).strip() == project_code:
            for row in rows[1:]:
                name = value(row, index)
                if name and add_unique(cur, "INSERT OR IGNORE INTO models(project_code,name) VALUES (?,?)",
                                       (project_code, name)):
                    counts["models"] += 1
        match = re.fullmatch(r"Building \(([^)]+)\)", header, re.I)
        if match and match.group(1).strip() == project_code:
            for row in rows[1:]:
                name = value(row, index)
                if name and add_unique(cur, "INSERT OR IGNORE INTO buildings(project_code,name,is_all) VALUES (?,?,?)",
                                       (project_code, name, int(name.lower().startswith("all")))):
                    counts["buildings"] += 1
        match = re.fullmatch(r"([^_]+)_(.+)", header)
        if match and match.group(1).strip() == project_code:
            stid = sheet_type_id(match.group(2).strip())
            for row in rows[1:]:
                name = re.sub(r"^\[\d+\]\s*", "", value(row, index))
                if name and add_unique(cur, "INSERT OR IGNORE INTO master_tasks(project_code,sheet_type_id,name) VALUES (?,?,?)",
                                       (project_code, stid, name)):
                    counts["master_tasks"] += 1

    con.commit()
    con.close()
    workbook.close()
    return counts


def import_models(db_path, project_code, model_names):
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys = ON")
    if not con.execute("SELECT 1 FROM projects WHERE code=?", (project_code,)).fetchone():
        con.close()
        raise RuntimeError(f"Project {project_code!r} does not exist; create it first")
    added = 0
    for model in model_names:
        model = clean(model)
        if model and add_unique(con.cursor(),
                "INSERT OR IGNORE INTO models(project_code,name) VALUES (?,?)",
                (project_code, model)):
            added += 1
    con.commit()
    con.close()
    return added


def import_subtasks(db_path, xlsx_path):
    import openpyxl

    workbook = openpyxl.load_workbook(xlsx_path, data_only=True, read_only=True)
    sheet = workbook.active
    rows = list(sheet.iter_rows(values_only=True))
    if not rows:
        workbook.close()
        raise RuntimeError("The selected workbook has no rows")
    headers = [norm(value) for value in rows[0]]
    column = next((i for i, name in enumerate(headers)
                   if name in ("sub task", "subtask", "task name", "task", "name")), 0)
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS task_sub_tasks (id INTEGER PRIMARY KEY, task_name_id INTEGER NOT NULL REFERENCES task_names(id) ON DELETE CASCADE, name TEXT NOT NULL, UNIQUE(task_name_id, name))")
    added = 0
    for row in rows[1:]:
        name = clean(row[column]) if column < len(row) else ""
        if name and add_unique(cur, "INSERT OR IGNORE INTO task_names(name) VALUES (?)", (name,)):
            added += 1
    con.commit()
    con.close()
    workbook.close()
    return added


def import_subtask_hierarchy(db_path, xlsx_path, project_code, master_task_id):
    import openpyxl
    workbook = openpyxl.load_workbook(xlsx_path, data_only=True, read_only=True)
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    if not cur.execute("SELECT 1 FROM master_tasks WHERE id=? AND project_code=?", (master_task_id, project_code)).fetchone():
        con.close(); workbook.close()
        raise RuntimeError("Selected Master Task does not belong to the selected project")
    added_sub = added_items = 0
    for sheet in workbook.worksheets:
        sub_name = clean(sheet.title)
        if not sub_name:
            continue
        cur.execute("INSERT OR IGNORE INTO sub_tasks(master_task_id,name) VALUES (?,?)", (master_task_id, sub_name))
        if cur.rowcount:
            added_sub += 1
        sub_id = cur.execute("SELECT id FROM sub_tasks WHERE master_task_id=? AND name=?", (master_task_id, sub_name)).fetchone()[0]
        for row in sheet.iter_rows(values_only=True):
            name = clean(row[0] if row else "")
            if not name or norm(name) in ("sub task", "subtask", "sub sub task", "subsub task", "name"):
                continue
            cur.execute("INSERT OR IGNORE INTO sub_sub_tasks(sub_task_id,name) VALUES (?,?)", (sub_id, name))
            if cur.rowcount:
                added_items += 1
    con.commit(); con.close(); workbook.close()
    return added_sub, added_items


def import_task_subtasks(db_path, xlsx_path, task_name_id, category_id=None):
    import openpyxl
    workbook = openpyxl.load_workbook(xlsx_path, data_only=True, read_only=True)
    lookup = sqlite3.connect(db_path)
    task_name = lookup.execute("SELECT name FROM task_names WHERE id=?", (task_name_id,)).fetchone()
    lookup.close()
    wanted = norm(task_name[0]) if task_name else ""
    sheet = next((ws for ws in workbook.worksheets if norm(ws.title) == wanted), workbook.active)
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS task_sub_tasks (id INTEGER PRIMARY KEY, task_name_id INTEGER NOT NULL REFERENCES task_names(id) ON DELETE CASCADE, category_id INTEGER REFERENCES categories(id) ON DELETE CASCADE, name TEXT NOT NULL, UNIQUE(task_name_id, category_id, name))")
    if not cur.execute("SELECT 1 FROM task_names WHERE id=?", (task_name_id,)).fetchone():
        con.close(); workbook.close(); raise RuntimeError("Selected Task does not exist")
    added = 0
    for row in sheet.iter_rows(values_only=True):
        name = clean(row[0] if row else "")
        if not name or norm(name) in ("sub task", "subtask", "name"):
            continue
        cur.execute("INSERT OR IGNORE INTO task_sub_tasks(task_name_id,category_id,name) VALUES (?,?,?)",
                    (task_name_id, category_id, name))
        added += cur.rowcount
    con.commit(); con.close(); workbook.close()
    return added


def import_zones_levels(db_path, xlsx_path, project_code):
    import openpyxl
    workbook = openpyxl.load_workbook(xlsx_path, data_only=True, read_only=True)
    zone_sheet = next((ws for ws in workbook.worksheets if norm(ws.title) in ("zone", "zones")), None)
    level_sheet = next((ws for ws in workbook.worksheets if norm(ws.title) in ("level", "levels")), None)
    rows = list((zone_sheet or workbook.active).iter_rows(values_only=True))
    if not rows:
        workbook.close(); raise RuntimeError("The selected workbook has no rows")
    headers = [norm(value) for value in rows[0]]
    zone_col = next((i for i, name in enumerate(headers) if name in ("zone", "building", "area")), None)
    level_rows = list((level_sheet or workbook.active).iter_rows(values_only=True))
    level_headers = [norm(value) for value in level_rows[0]] if level_rows else []
    level_col = next((i for i, name in enumerate(level_headers) if name in ("level", "levels", "storey", "floor")), None)
    if zone_col is None and level_col is None:
        workbook.close(); raise RuntimeError("Workbook needs a Zone/Building column or a Level column")
    con = sqlite3.connect(db_path); cur = con.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS category_tasks (master_task_id INTEGER NOT NULL REFERENCES master_tasks(id) ON DELETE CASCADE, category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE, task_name_id INTEGER NOT NULL REFERENCES task_names(id) ON DELETE CASCADE, PRIMARY KEY(master_task_id, category_id, task_name_id))")
    if not cur.execute("SELECT 1 FROM projects WHERE code=?", (project_code,)).fetchone():
        con.close(); workbook.close(); raise RuntimeError(f"Project {project_code!r} does not exist")
    zones = levels = 0
    for row in rows[1:]:
        if zone_col is not None and zone_col < len(row) and clean(row[zone_col]):
            name = clean(row[zone_col])
            cur.execute("INSERT OR IGNORE INTO buildings(project_code,name,is_all) VALUES (?,?,?)", (project_code, name, int(name.lower().startswith("all"))))
            zones += cur.rowcount
    for row in level_rows[1:]:
        if level_col is not None and level_col < len(row) and clean(row[level_col]):
            existed = cur.execute(
                "SELECT 1 FROM levels WHERE project_code=? AND label=?",
                (project_code, parse_level_name(clean(row[level_col]))[1])).fetchone()
            if upsert_project_level(cur, project_code, clean(row[level_col])) and not existed:
                levels += 1
    con.commit(); con.close(); workbook.close()
    return zones, levels


def import_levels_only(db_path, xlsx_path, project_code=None):
    if not project_code:
        raise RuntimeError("Choose a project. Levels belong to one project only.")
    return import_project_levels(db_path, xlsx_path, project_code)


def import_project_levels(db_path, xlsx_path, project_code):
    import openpyxl
    workbook = openpyxl.load_workbook(xlsx_path, data_only=True, read_only=True)
    sheet = next((ws for ws in workbook.worksheets if norm(ws.title) in ("level", "levels")), workbook.active)
    rows = list(sheet.iter_rows(values_only=True)); workbook.close()
    if not rows: raise RuntimeError("The selected workbook has no rows")
    con = sqlite3.connect(db_path); cur = con.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS project_levels (project_code TEXT NOT NULL, level_id INTEGER NOT NULL, PRIMARY KEY(project_code,level_id))")
    added = 0
    for row in rows:
        raw = clean(row[0] if row else "")
        number, label, is_all = parse_level_name(raw)
        if not label:
            continue
        existed = cur.execute(
            "SELECT 1 FROM levels WHERE project_code=? AND label=?",
            (project_code, label)).fetchone()
        if upsert_project_level(cur, project_code, raw, number, is_all) and not existed:
            added += 1
    con.commit(); con.close(); return added


def import_zones_only(db_path, xlsx_path, project_code):
    import openpyxl
    workbook = openpyxl.load_workbook(xlsx_path, data_only=True, read_only=True)
    sheet = next((ws for ws in workbook.worksheets if norm(ws.title) in ("zone", "zones")), workbook.active)
    rows = list(sheet.iter_rows(values_only=True)); workbook.close()
    if not rows: raise RuntimeError("The selected workbook has no rows")
    headers = [norm(value) for value in rows[0]]
    col = next((i for i, name in enumerate(headers) if name in ("zone", "building", "area")), 0)
    con = sqlite3.connect(db_path); cur = con.cursor(); added = 0
    for row in rows[1:]:
        name = clean(row[col]) if col < len(row) else ""
        if name:
            cur.execute("INSERT OR IGNORE INTO buildings(project_code,name,is_all) VALUES (?,?,?)", (project_code, name, int(name.lower().startswith("all"))))
            added += cur.rowcount
    con.commit(); con.close(); return added


def import_category_tasks(db_path, xlsx_path, master_task_id):
    """Load a Review comment sheet as Categories and Sub Tasks.

    Underlined rows in column A are Categories. The numbered rows beneath each
    one are Sub Tasks, filed under that Category and the REVIEW COMMENTS Task.
    The Task level itself stays fixed at CORE_TASKS — this importer never
    invents new Task names.
    """
    import openpyxl
    workbook = openpyxl.load_workbook(xlsx_path, data_only=False, read_only=False)
    sheet = next((ws for ws in workbook.worksheets if norm(ws.title) in ("review comment", "review comments")), workbook.active)
    con = sqlite3.connect(db_path); cur = con.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS categories (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE)")
    cur.execute("CREATE TABLE IF NOT EXISTS category_tasks (master_task_id INTEGER NOT NULL, category_id INTEGER NOT NULL, task_name_id INTEGER NOT NULL, PRIMARY KEY(master_task_id, category_id, task_name_id))")
    cur.execute("CREATE TABLE IF NOT EXISTS master_task_tasks (master_task_id INTEGER NOT NULL, task_name_id INTEGER NOT NULL, PRIMARY KEY(master_task_id, task_name_id))")
    cur.execute("CREATE TABLE IF NOT EXISTS task_sub_tasks (id INTEGER PRIMARY KEY, task_name_id INTEGER NOT NULL REFERENCES task_names(id) ON DELETE CASCADE, category_id INTEGER REFERENCES categories(id) ON DELETE CASCADE, name TEXT NOT NULL, UNIQUE(task_name_id, category_id, name))")
    if not cur.execute("SELECT 1 FROM master_tasks WHERE id=?", (master_task_id,)).fetchone():
        con.close(); workbook.close(); raise RuntimeError("Selected Master Task does not exist")

    for name in CORE_TASKS:
        cur.execute("INSERT OR IGNORE INTO task_names(name) VALUES (?)", (name,))
    core_ids = [cur.execute("SELECT id FROM task_names WHERE name=?", (n,)).fetchone()[0] for n in CORE_TASKS]
    review_id = cur.execute("SELECT id FROM task_names WHERE name=?", (REVIEW_TASK,)).fetchone()[0]

    category_id = None; categories = sub_tasks = 0
    for cell in sheet["A"]:
        value = clean(cell.value)
        if not value:
            continue
        if cell.font.underline:
            cur.execute("INSERT OR IGNORE INTO categories(name) VALUES (?)", (value.upper(),))
            categories += cur.rowcount
            category_id = cur.execute("SELECT id FROM categories WHERE name=?", (value.upper(),)).fetchone()[0]
            # Every Category offers the same three Tasks.
            for task_id in core_ids:
                cur.execute("INSERT OR IGNORE INTO category_tasks(master_task_id,category_id,task_name_id) VALUES (?,?,?)", (master_task_id, category_id, task_id))
                cur.execute("INSERT OR IGNORE INTO master_task_tasks(master_task_id,task_name_id) VALUES (?,?)", (master_task_id, task_id))
            continue
        if category_id is None:
            continue
        sub_task = re.sub(r"^\s*\d+\.\s*", "", value).strip()
        if not sub_task:
            continue
        cur.execute("INSERT OR IGNORE INTO task_sub_tasks(task_name_id,category_id,name) VALUES (?,?,?)",
                    (review_id, category_id, sub_task))
        sub_tasks += cur.rowcount
    con.commit(); con.close(); workbook.close()
    return categories, sub_tasks


ZONE_MAP_HEADERS = {"zone", "zones", "fow", "building", "building name"}
LEAD_MAP_HEADERS = {"qc reviewer", "reviewer", "team lead", "lead", "qc review"}
SUPP_MAP_HEADERS = {"qc support", "support", "team support", "supports", "qc supports"}


def split_list(value):
    """Split a cell on comma, semicolon, or slash: 'HF, facade' → HF and facade."""
    text = clean(value)
    if not text:
        return []
    return [part for part in (clean(p) for p in re.split(r"[,;/]", text)) if part]


def split_people_names(value):
    return split_list(value)


def fold_key(value):
    text = unicodedata.normalize("NFKD", clean(value))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "", text.lower())


# Typed name (folded) → employee name fold. Typos and swapped word order.
PERSON_ALIASES = {
    "surendra": "surendera",
    "venkatkrishnan": "venkatakrishna",
    "venkatakrishnan": "venkatakrishna",
    "venkatakrishna": "venkatakrishna",
    "kumarbhogaparupu": "bhogapurapukumar",
    "bhogaparupu": "bhogapurapukumar",
    "bhogapurapu": "bhogapurapukumar",
    "ramakrishnar": "ramakrishna",
    "rramakrishna": "ramakrishna",
    "gokul": "gokulakrishnan",
    "sravanbhoja": "bojjasravankumar",
    "durgaprasad": "chelamaladurgaprasad",
    "kartheekpolamarasetti": "kartheek",
    "anilkumar": "anilkumarravada",
}


def _edit_distance(a, b):
    if a == b:
        return 0
    if abs(len(a) - len(b)) > 2:
        return 3
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(cur[j - 1] + 1, prev[j] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
        if min(prev) > 2:
            return 3
    return prev[-1]


def _uniq_people(rows):
    seen, out = set(), []
    for row in rows:
        if row["id"] in seen:
            continue
        seen.add(row["id"])
        out.append(row)
    return out


def _wanted_roles(role_wanted):
    if isinstance(role_wanted, (list, tuple)):
        return [(r or "").strip().upper() for r in role_wanted if (r or "").strip()]
    return [(role_wanted or "").strip().upper()]


def _find_person(cur, name, role_wanted):
    n = clean(name)
    if not n:
        raise RuntimeError("Missing employee name.")
    wanted = _wanted_roles(role_wanted)
    wanted_set = set(wanted)
    role_label = " / ".join(wanted) if wanted else "employee"
    people = cur.execute(
        "SELECT pe.id, pe.name, pe.emp_code, r.name AS role FROM people pe "
        "LEFT JOIN roles r ON r.id=pe.role_id"
    ).fetchall()

    exact = [
        p for p in people
        if (p["name"] or "").strip().lower() == n.lower()
        or (p["emp_code"] or "").strip().lower() == n.lower()
    ]
    if len(exact) == 1:
        return exact[0]["id"], exact[0]["name"]
    if len(exact) > 1:
        role_match = [p for p in exact if (p["role"] or "").strip().upper() in wanted_set] or exact
        if len(role_match) == 1:
            return role_match[0]["id"], role_match[0]["name"]
        raise RuntimeError(f"Several employees match {n!r}; use the exact name or Emp ID.")

    role_people = [
        p for p in people
        if not wanted_set or (p["role"] or "").strip().upper() in wanted_set
    ]
    q = fold_key(n)
    alias = PERSON_ALIASES.get(q, q)
    hits = []
    for p in role_people:
        f = fold_key(p["name"])
        if not f:
            continue
        if f == q or f == alias:
            hits.append(p)
            continue
        if len(q) >= 3 and (f.startswith(q) or alias != q and f.startswith(alias)):
            hits.append(p)
            continue
        if len(q) >= 4 and q.startswith(f) and len(f) >= 4:
            hits.append(p)
            continue
        if len(q) >= 4 and (q in f or (alias != q and len(alias) >= 4 and alias in f)):
            hits.append(p)
            continue
        if len(q) >= 6 and _edit_distance(q, f) <= 2:
            hits.append(p)
    hits = _uniq_people(hits)
    if len(hits) > 1:
        for role in wanted:
            preferred = [p for p in hits if (p["role"] or "").strip().upper() == role]
            if len(preferred) == 1:
                return preferred[0]["id"], preferred[0]["name"]
        listed = ", ".join(p["name"] for p in hits)
        raise RuntimeError(
            f"{n!r} matches more than one {role_label}: {listed}. Use the full name.")
    if len(hits) == 1:
        return hits[0]["id"], hits[0]["name"]
    raise RuntimeError(f"No employee named {n!r}. Add them under Employee first.")


def _find_or_add_zone(cur, project_code, name):
    n = clean(name)
    row = cur.execute(
        "SELECT id FROM buildings WHERE project_code=? AND name=? COLLATE NOCASE",
        (project_code, n)).fetchone()
    if row:
        return row["id"], False
    want = fold_key(n)
    matches = [
        r for r in cur.execute(
            "SELECT id, name FROM buildings WHERE project_code=?", (project_code,)
        ).fetchall()
        if fold_key(r["name"]) == want
    ]
    if len(matches) == 1:
        return matches[0]["id"], False
    if len(matches) > 1:
        exact = [m for m in matches if (m["name"] or "").lower() == n.lower()]
        return (exact[0] if exact else matches[0])["id"], False
    cur.execute(
        "INSERT INTO buildings(project_code,name,is_all) VALUES (?,?,?)",
        (project_code, n, int(n.lower().startswith("all"))))
    return cur.lastrowid, True


def _norm_emp_code(value):
    return re.sub(r"[\s\xa0\-]+", "", clean(value)).upper()


def _find_person_by_id_or_name(cur, name, emp_id):
    code = _norm_emp_code(emp_id)
    if code:
        people = cur.execute("SELECT id, name, emp_code FROM people").fetchall()
        hits = [p for p in people if _norm_emp_code(p["emp_code"]) == code]
        if len(hits) == 1:
            return hits[0]["id"], hits[0]["name"]
        if len(hits) > 1:
            raise RuntimeError(f"Several employees have Emp ID {emp_id!r}.")
    if clean(name):
        return _find_person(cur, name, "")
    raise RuntimeError("Need an Emp ID or employee name.")


def _sheet_looks_like_qc_plan(rows):
    text = " ".join(norm(v) for row in (rows or [])[:8] for v in (row or []) if v)
    return "qc team" in text and "qc support" in text and "zone" in text


def _parse_qc_team_plan_rows(rows):
    """QC Team (reviewer + zone) on the left, QC Support on the right, with fill-down."""
    header_i = None
    for i, row in enumerate(rows):
        cells = [norm(v) for v in (row or [])]
        if "zone" in cells and any(c in ("emp name", "emp. name", "name") for c in cells):
            header_i = i
            break
    if header_i is None:
        raise RuntimeError("Could not find the Emp. Name / Zone header row.")

    header = [norm(v) for v in rows[header_i]]
    # First Emp. Name / Emp. ID = reviewer; Zone; then Emp. Name / Emp. ID = support.
    name_cols = [i for i, h in enumerate(header) if h in ("emp name", "emp. name", "name")]
    id_cols = [i for i, h in enumerate(header) if h in ("emp id", "emp. id", "id", "staff id")]
    zone_col = next((i for i, h in enumerate(header) if h == "zone"), None)
    rev_name_col = name_cols[0] if name_cols else 3
    rev_id_col = id_cols[0] if id_cols else 4
    if zone_col is None:
        zone_col = 5
    sup_name_col = name_cols[1] if len(name_cols) > 1 else 6
    sup_id_col = id_cols[1] if len(id_cols) > 1 else 7

    def cell(row, index):
        if index is None or row is None or index >= len(row):
            return ""
        return clean(row[index]).replace("\xa0", " ").strip()

    skip = {
        "architecture", "sino", "slno", "sno", "qcteam", "qcsupport",
        "empname", "empid", "zone", "task",
    }
    location = None
    last_rev_name = last_rev_id = last_zone = ""
    out = []
    for offset, row in enumerate(rows[header_i + 1 :], start=header_i + 2):
        if row is None or not any(clean(v) for v in row):
            continue
        labels = [fold_key(v) for v in row if clean(v)]
        if labels and all(k in skip or k in ("chennai", "vizag", "facade", "faade") for k in labels):
            joined = labels[0] if len(labels) == 1 else ""
            if joined == "chennai":
                location = "Chennai"
            elif joined == "vizag":
                location = "Vizag"
            elif joined in ("facade", "faade"):
                location = None
            continue
        first = fold_key(cell(row, 2) or cell(row, 0) or cell(row, 3))
        if first in ("chennai", "vizag", "architecture", "facade", "faade"):
            if first == "chennai":
                location = "Chennai"
            elif first == "vizag":
                location = "Vizag"
            elif first in ("facade", "faade"):
                location = None
            continue

        rev_name = cell(row, rev_name_col)
        rev_id = cell(row, rev_id_col)
        zone = cell(row, zone_col)
        sup_name = cell(row, sup_name_col)
        sup_id = cell(row, sup_id_col)
        if rev_name or rev_id:
            last_rev_name, last_rev_id = rev_name or last_rev_name, rev_id or last_rev_id
        if zone:
            last_zone = zone
        if not (last_rev_name or last_rev_id):
            continue
        if not last_zone and not (sup_name or sup_id):
            continue
        out.append({
            "row": offset,
            "reviewer": last_rev_name,
            "reviewer_id": last_rev_id,
            "zone": last_zone,
            "support": sup_name,
            "support_id": sup_id,
            "location": location,
        })
    return out


def import_qc_team_plan(db_path, xlsx_path, project_code):
    """Apply an AL AIN-style QC Team / QC Support plan: set roles, replace zone mapping."""
    import openpyxl

    project_code = clean(project_code)
    workbook = openpyxl.load_workbook(xlsx_path, data_only=True, read_only=True)
    sheet = workbook.active
    rows = list(sheet.iter_rows(values_only=True))
    workbook.close()
    parsed = _parse_qc_team_plan_rows(rows)
    if not parsed:
        raise RuntimeError("No QC Team rows found in the plan workbook.")

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    cur = con.cursor()
    if not cur.execute("SELECT 1 FROM projects WHERE code=?", (project_code,)).fetchone():
        con.close()
        raise RuntimeError(f"Project {project_code!r} does not exist; create it first.")
    cur.execute("INSERT OR IGNORE INTO roles(name) VALUES (?)", ("QC REVIEWER",))
    cur.execute("INSERT OR IGNORE INTO roles(name) VALUES (?)", ("QC SUPPORT",))
    reviewer_role = cur.execute("SELECT id FROM roles WHERE name='QC REVIEWER'").fetchone()[0]
    support_role = cur.execute("SELECT id FROM roles WHERE name='QC SUPPORT'").fetchone()[0]

    errors = []
    reviewer_ids = {}
    support_ids = {}
    zone_pairs = []
    support_to_lead = {}
    created_zones = 0
    locations = {}

    for item in parsed:
        try:
            lid, lname = _find_person_by_id_or_name(
                cur, item["reviewer"], item["reviewer_id"])
        except RuntimeError as e:
            errors.append(f"row {item['row']}: reviewer {e}")
            continue
        reviewer_ids[lid] = lname
        if item["location"]:
            locations[lid] = item["location"]
        if item["zone"]:
            for zname in split_list(item["zone"]):
                zid, is_new = _find_or_add_zone(cur, project_code, zname)
                created_zones += int(is_new)
                pair = (zid, lid)
                if pair not in zone_pairs:
                    zone_pairs.append(pair)
        if item["support"] or item["support_id"]:
            try:
                sid, sname = _find_person_by_id_or_name(
                    cur, item["support"], item["support_id"])
            except RuntimeError as e:
                errors.append(f"row {item['row']}: support {e}")
                continue
            if sid == lid:
                errors.append(f"row {item['row']}: {sname} cannot support themselves")
                continue
            support_ids[sid] = sname
            support_to_lead[sid] = lid
            if item["location"]:
                locations[sid] = item["location"]

    if not zone_pairs and not support_to_lead:
        con.close()
        detail = " ".join(errors[:8])
        raise RuntimeError("No valid plan rows." + (f" {detail}" if detail else ""))

    for lid in reviewer_ids:
        put_on_project_team(cur, project_code, lid, reviewer_role)
    for sid in support_ids:
        if sid not in reviewer_ids:
            put_on_project_team(cur, project_code, sid, support_role)
    for pid, loc in locations.items():
        cur.execute("UPDATE people SET location=? WHERE id=?", (loc, pid))

    cur.execute(
        "DELETE FROM lead_scope WHERE project_code=? AND kind='zone'", (project_code,))
    cur.execute("DELETE FROM lead_support WHERE project_code=?", (project_code,))

    people_ids = set(reviewer_ids) | set(support_ids)
    for pid in people_ids:
        put_on_project_team(cur, project_code, pid)
    for zone_id, lead_id in zone_pairs:
        cur.execute(
            "INSERT OR IGNORE INTO lead_scope(project_code,person_id,kind,item_id) "
            "VALUES (?,?,?,?)",
            (project_code, lead_id, "zone", zone_id))
    for support_id, lead_id in support_to_lead.items():
        cur.execute(
            "INSERT OR IGNORE INTO lead_support(project_code,lead_id,support_id) VALUES (?,?,?)",
            (project_code, lead_id, support_id))
    cur.execute("UPDATE projects SET use_teams=1 WHERE code=?", (project_code,))
    con.commit()
    con.close()
    return {
        "zones": len(zone_pairs),
        "supports": len(support_to_lead),
        "created_zones": created_zones,
        "rows": len(parsed),
        "reviewers": len(reviewer_ids),
        "errors": errors,
        "reviewer_names": sorted(reviewer_ids.values()),
    }


def import_team_map(db_path, xlsx_path, project_code):
    """Load Zone → QC Reviewer → QC Support rows for one project.

    Rows merge into the existing mapping: each zone/support named in the file is
    moved onto the reviewer on that row. Other mappings on the project stay.
    Blank Zone / QC Reviewer cells copy down from the row above.
    """
    import openpyxl

    if not os.path.isfile(db_path):
        raise RuntimeError(f"Database not found: {db_path}")
    if not os.path.isfile(xlsx_path):
        raise RuntimeError(f"Workbook not found: {xlsx_path}")
    project_code = clean(project_code)
    if not project_code:
        raise RuntimeError("Choose a project.")

    workbook = openpyxl.load_workbook(xlsx_path, data_only=True, read_only=True)
    sheet = next(
        (ws for ws in workbook.worksheets
         if norm(ws.title) in ("mapping", "map", "team", "zones", "zone")),
        workbook.active)
    rows = list(sheet.iter_rows(values_only=True))
    workbook.close()
    if not rows:
        raise RuntimeError("The selected workbook has no rows.")
    if _sheet_looks_like_qc_plan(rows):
        return import_qc_team_plan(db_path, xlsx_path, project_code)

    headers = [norm(value) for value in rows[0]]
    zone_col = next((i for i, name in enumerate(headers) if name in ZONE_MAP_HEADERS), None)
    lead_col = next((i for i, name in enumerate(headers) if name in LEAD_MAP_HEADERS), None)
    supp_col = next((i for i, name in enumerate(headers) if name in SUPP_MAP_HEADERS), None)
    data_rows = rows[1:]
    if zone_col is None or lead_col is None:
        zone_col, lead_col, supp_col = 0, 1, 2
        first = [clean(v) for v in rows[0]]
        if first and norm(first[0]) in ZONE_MAP_HEADERS | {"zone"}:
            data_rows = rows[1:]
        else:
            data_rows = rows

    def cell(row, index):
        if index is None or index >= len(row):
            return ""
        return clean(row[index])

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    cur = con.cursor()
    if not cur.execute("SELECT 1 FROM projects WHERE code=?", (project_code,)).fetchone():
        con.close()
        raise RuntimeError(f"Project {project_code!r} does not exist; create it first.")

    last_zone, last_lead = "", ""
    errors = []
    zone_pairs = []
    seen_zp = set()
    support_to_lead = {}
    created_zones = 0
    applied = 0

    for offset, row in enumerate(data_rows, start=2):
        if row is None or not any(clean(v) for v in row):
            continue
        zone_cell = cell(row, zone_col) or last_zone
        lead_name = cell(row, lead_col) or last_lead
        if cell(row, zone_col):
            last_zone = cell(row, zone_col)
        if lead_name:
            last_lead = lead_name
        zone_names = split_list(zone_cell)
        if not zone_names or not lead_name:
            errors.append(f"row {offset}: need a Zone and a QC Reviewer")
            continue
        supports = split_people_names(cell(row, supp_col))
        try:
            lead_id, _ = _find_person(cur, lead_name, "QC REVIEWER")
            zone_ids = []
            for zname in zone_names:
                zone_id, is_new = _find_or_add_zone(cur, project_code, zname)
                if is_new:
                    created_zones += 1
                zone_ids.append((zone_id, zname))
            support_ids = []
            for sname in supports:
                sid, _ = _find_person(cur, sname, ("QC SUPPORT", "QC REVIEWER"))
                support_ids.append(sid)
        except RuntimeError as exc:
            errors.append(f"row {offset}: {exc}")
            continue
        for zone_id, zname in zone_ids:
            key = (zone_id, lead_id)
            if key not in seen_zp:
                seen_zp.add(key)
                zone_pairs.append(key)
        for sid in support_ids:
            if sid == lead_id:
                continue
            prev_s = support_to_lead.get(sid)
            if prev_s and prev_s != lead_id:
                errors.append(
                    f"row {offset}: that QC Support is listed under two reviewers; using the later row")
            support_to_lead[sid] = lead_id
        applied += 1

    if not zone_pairs and not support_to_lead:
        con.close()
        detail = " ".join(errors[:8])
        raise RuntimeError("No valid mapping rows." + (f" {detail}" if detail else ""))

    people_ids = {lid for _, lid in zone_pairs} | set(support_to_lead.values()) | set(support_to_lead)
    reviewer_row = cur.execute("SELECT id FROM roles WHERE name='QC REVIEWER'").fetchone()
    support_row = cur.execute("SELECT id FROM roles WHERE name='QC SUPPORT'").fetchone()
    reviewer_role = reviewer_row[0] if reviewer_row else None
    support_role = support_row[0] if support_row else None
    lead_ids = {lid for _, lid in zone_pairs} | set(support_to_lead.values())
    for pid in lead_ids:
        put_on_project_team(cur, project_code, pid, reviewer_role)
    for sid in support_to_lead:
        if sid in lead_ids:
            continue
        put_on_project_team(cur, project_code, sid, support_role)
    for pid in people_ids:
        put_on_project_team(cur, project_code, pid)

    for zone_id, lead_id in zone_pairs:
        cur.execute(
            "INSERT OR IGNORE INTO lead_scope(project_code,person_id,kind,item_id) "
            "VALUES (?,?,?,?)",
            (project_code, lead_id, "zone", zone_id))

    for support_id, lead_id in support_to_lead.items():
        cur.execute(
            "DELETE FROM lead_support WHERE project_code=? AND support_id=?",
            (project_code, support_id))
        cur.execute(
            "INSERT OR IGNORE INTO lead_support(project_code,lead_id,support_id) VALUES (?,?,?)",
            (project_code, lead_id, support_id))

    cur.execute("UPDATE projects SET use_teams=1 WHERE code=?", (project_code,))
    con.commit()
    con.close()
    return {
        "zones": len(zone_pairs),
        "supports": len(support_to_lead),
        "created_zones": created_zones,
        "rows": applied,
        "errors": errors,
    }


def team_map_workbook_bytes(db_path, project_code=None):
    """Blank template, or a sheet pre-filled with this project's zones and mapping."""
    import openpyxl
    from io import BytesIO
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    wb = openpyxl.Workbook()
    notes = wb.active
    notes.title = "Instructions"
    notes["A1"] = "Zone → QC Reviewer → QC Support"
    notes["A1"].font = Font(bold=True, size=14)
    instructions = [
        "",
        "1. Open the Mapping sheet. One row is one QC Reviewer, with their zones and QC Support.",
        "2. Several zones on one reviewer: put them in the Zone cell, comma-separated (HF, Facade, All zones).",
        "   The same zone can be on several reviewers (task-based work across zones).",
        "3. A reviewer with two or more supports: repeat Zone and QC Reviewer, change QC Support.",
        "   You can also list several supports in one cell, separated by commas.",
        "4. Names can be short if they uniquely identify the person (Aasin, Vignesh, sravan).",
        "   If two people share a short name, use the full Employee name or Emp ID.",
        "5. Blank Zone or QC Reviewer cells copy down from the row above.",
        "6. Upload the file on Manage Lists → Team mapping (Excel). Choose the same project.",
        "7. Upload updates the zones and supports named in the file; other mappings are left as they are.",
    ]
    for i, line in enumerate(instructions, start=2):
        notes[f"A{i}"] = line
    notes.column_dimensions["A"].width = 110

    ws = wb.create_sheet("Mapping", 0)
    headers = ["Zone", "QC Reviewer", "QC Support"]
    header_fill = PatternFill("solid", fgColor="1F5A86")
    header_font = Font(bold=True, color="FFFFFF")
    for i, title in enumerate(headers, start=1):
        cell = ws.cell(1, i, title)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center")
        ws.column_dimensions[get_column_letter(i)].width = 28
    ws.freeze_panes = "A2"

    data_rows = []
    if project_code:
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        zones = cur.execute(
            "SELECT id, name FROM buildings WHERE project_code=? ORDER BY name",
            (project_code,)).fetchall()
        zone_leads = {}
        for r in cur.execute(
                "SELECT person_id, item_id FROM lead_scope "
                "WHERE project_code=? AND kind='zone'",
                (project_code,)):
            zone_leads.setdefault(r["item_id"], []).append(r["person_id"])
        lead_supports = {}
        for r in cur.execute(
                "SELECT lead_id, support_id FROM lead_support WHERE project_code=?",
                (project_code,)):
            lead_supports.setdefault(r["lead_id"], []).append(r["support_id"])
        names = {r["id"]: r["name"] for r in cur.execute("SELECT id, name FROM people")}
        con.close()
        for zone in zones:
            leads = zone_leads.get(zone["id"]) or [None]
            for lead_id in leads:
                lead_name = names.get(lead_id, "") if lead_id else ""
                supports = lead_supports.get(lead_id, []) if lead_id else []
                if supports:
                    for sid in supports:
                        data_rows.append((zone["name"], lead_name, names.get(sid, "")))
                else:
                    data_rows.append((zone["name"], lead_name, ""))
    if not data_rows:
        data_rows = [
            ("HF", "", ""),
            ("HG", "", ""),
        ]

    for r_i, values in enumerate(data_rows, start=2):
        for c_i, value in enumerate(values, start=1):
            ws.cell(r_i, c_i, value)

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def normalize_office_location(value):
    """Map typed office names onto Chennai / Vizag."""
    key = fold_key(value)
    if not key:
        return None
    if key in ("vizag", "vskp") or key.startswith("vizag") or key.startswith("visak"):
        return "Vizag"
    if key in ("chennai", "madras") or key.startswith("chennai"):
        return "Chennai"
    return None


def infer_location_from_ip(ip):
    """Office LAN: 192.168.1.x = Chennai, 192.168.50.x = Vizag."""
    text = clean(ip)
    match = re.search(r"(\d{1,3}(?:\.\d{1,3}){1,3})", text)
    if not match:
        return None
    parts = match.group(1).split(".")
    if len(parts) >= 3 and parts[0] == "192" and parts[1] == "168":
        if parts[2] == "50":
            return "Vizag"
        if parts[2] == "1":
            return "Chennai"
    return None


def location_workbook_bytes(db_path):
    """Employee list with Location filled from the record, or inferred from IP."""
    import openpyxl
    from io import BytesIO
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    wb = openpyxl.Workbook()
    notes = wb.active
    notes.title = "Instructions"
    notes["A1"] = "Employee locations"
    notes["A1"].font = Font(bold=True, size=14)
    instructions = [
        "",
        "1. Open the Locations sheet. Every employee is listed.",
        "2. Location is filled when already stored, otherwise inferred from IP:",
        "      192.168.1.x  = Chennai",
        "      192.168.50.x = Vizag",
        "3. Correct any row, then upload this file on Manage Lists → Employee.",
        "4. Columns: Emp ID | Emp name | IP address | Location",
        "5. Location must be Chennai or Vizag. Blank Location uses the IP on that row.",
        "6. Rows with no Location and no usable IP are skipped.",
    ]
    for i, line in enumerate(instructions, start=2):
        notes[f"A{i}"] = line
    notes.column_dimensions["A"].width = 100

    ws = wb.create_sheet("Locations", 0)
    headers = ["Emp ID", "Emp name", "IP address", "Location"]
    header_fill = PatternFill("solid", fgColor="1F5A86")
    header_font = Font(bold=True, color="FFFFFF")
    for i, title in enumerate(headers, start=1):
        cell = ws.cell(1, i, title)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center")
    ws.column_dimensions["A"].width = 14
    ws.column_dimensions["B"].width = 28
    ws.column_dimensions["C"].width = 18
    ws.column_dimensions["D"].width = 14
    ws.freeze_panes = "A2"

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    people = con.execute(
        "SELECT emp_code, name, ip_address, location FROM people ORDER BY name"
    ).fetchall()
    con.close()
    for r_i, person in enumerate(people, start=2):
        loc = clean(person["location"]) or infer_location_from_ip(person["ip_address"]) or ""
        ws.cell(r_i, 1, person["emp_code"] or "")
        ws.cell(r_i, 2, person["name"] or "")
        ws.cell(r_i, 3, person["ip_address"] or "")
        ws.cell(r_i, 4, loc)

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def import_employee_locations(db_path, xlsx_path):
    """Set people.location from an Excel Locations sheet (or any sheet with those columns)."""
    import openpyxl

    if not os.path.isfile(db_path):
        raise RuntimeError(f"Database not found: {db_path}")
    if not os.path.isfile(xlsx_path):
        raise RuntimeError(f"Workbook not found: {xlsx_path}")

    workbook = openpyxl.load_workbook(xlsx_path, data_only=True, read_only=True)
    sheet = next(
        (ws for ws in workbook.worksheets
         if norm(ws.title) in ("locations", "location", "employees", "employee", "people")),
        workbook.active)
    rows = list(sheet.iter_rows(values_only=True))
    workbook.close()
    if not rows:
        raise RuntimeError("The selected workbook has no rows.")

    headers = [norm(value) for value in rows[0]]
    id_col = next((i for i, name in enumerate(headers)
                   if name in ("emp id", "empid", "staff id", "staffid", "id")), None)
    name_col = next((i for i, name in enumerate(headers)
                     if name in ("emp name", "empname", "staff name", "staffname", "name",
                                 "employee", "employee name")), None)
    ip_col = next((i for i, name in enumerate(headers)
                   if name in ("ip address", "ipaddress", "ip")), None)
    loc_col = next((i for i, name in enumerate(headers)
                    if name in ("location", "office", "city", "site")), None)
    data_rows = rows[1:]
    if id_col is None and name_col is None:
        id_col, name_col, ip_col, loc_col = 0, 1, 2, 3
        data_rows = rows

    def cell(row, index):
        if index is None or index >= len(row):
            return ""
        return clean(row[index])

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    cur = con.cursor()
    updated, skipped, errors = 0, 0, []

    for offset, row in enumerate(data_rows, start=2):
        if row is None or not any(clean(v) for v in row):
            continue
        emp_id = cell(row, id_col)
        name = cell(row, name_col)
        ip = cell(row, ip_col)
        loc = normalize_office_location(cell(row, loc_col)) or infer_location_from_ip(ip)
        if not loc:
            skipped += 1
            continue
        key = emp_id or name
        if not key:
            errors.append(f"row {offset}: need Emp ID or Emp name")
            continue
        try:
            pid, _ = _find_person(cur, key, "")
        except RuntimeError as e:
            errors.append(f"row {offset}: {e}")
            continue
        cur.execute("UPDATE people SET location=? WHERE id=?", (loc, pid))
        if ip:
            existing = cur.execute("SELECT ip_address FROM people WHERE id=?", (pid,)).fetchone()
            if existing and not clean(existing["ip_address"]):
                cur.execute("UPDATE people SET ip_address=? WHERE id=?", (ip, pid))
        updated += 1

    con.commit()
    con.close()
    return {"updated": updated, "skipped": skipped, "errors": errors}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--xlsx", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--models", default="[]")
    args = parser.parse_args()
    backup = args.db + ".bulk_before_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    shutil.copy2(args.db, backup)
    counts = import_catalog(args.db, args.xlsx, args.project, json.loads(args.models))
    print(f"Bulk import complete for project {args.project}.")
    for name, count in counts.items():
        print(f"  {name}: +{count}")
    print(f"Backup: {backup}")


if __name__ == "__main__":
    main()
