#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Import the Master Task Log workbook into a normalized SQLite database.

- Rebuilds the DB from schema.sql (destructive: drops/recreates the file).
- Loads the Lists sheet into reference tables (projects, buildings, models,
  master_tasks, sheet_types, levels, task_names, people, statuses).
- Loads Master Log into tasks + task_assignees.
- Converts Excel dates/times to real values, and COMPUTES break_mins / hours /
  man-days in code per the business rules, then RECONCILES against the sheet's
  own computed values and reports every mismatch (does not silently trust either).
- Reports: reference rows auto-added from the log, per-day assignee overlaps.

Usage:
    python import_xlsx.py                     # uses default paths below
    python import_xlsx.py "path/to.xlsx" tasklog.db
"""
import sys, os, re, sqlite3, datetime as dt

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_XLSX = r"C:\Users\jse2084\Documents\Copy of 2630_M45_Master Task Log.xlsx"
DEFAULT_DB   = os.path.join(HERE, "tasklog.db")
SCHEMA_SQL   = os.path.join(HERE, "schema.sql")

# Standard working-time config (confirmed from the Read Me tab). Seeded per
# project so a future project can differ. HH:MM.
DEFAULT_CONFIG = dict(day_start="09:00", day_end="18:30", hours_per_day=8.25)
DEFAULT_BREAKS = [("Lunch", "12:45", "13:30"),
                  ("AM",    "11:15", "11:30"),
                  ("PM",    "16:15", "16:30")]

TOL = 0.02  # reconciliation tolerance (minutes / hours / man-days)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def to_min(t):
    """datetime.time/datetime -> minutes since midnight (float), or None."""
    if t is None:
        return None
    if isinstance(t, dt.datetime):
        t = t.time()
    if isinstance(t, dt.time):
        return t.hour * 60 + t.minute + t.second / 60.0
    return None


def hhmm(t):
    if t is None:
        return None
    if isinstance(t, dt.datetime):
        t = t.time()
    if isinstance(t, dt.time):
        return f"{t.hour:02d}:{t.minute:02d}"
    return None


def hhmm_from_min(m):
    m = int(round(m))
    return f"{m // 60:02d}:{m % 60:02d}"


def date_iso(d):
    if isinstance(d, dt.datetime):
        d = d.date()
    if isinstance(d, dt.date):
        return d.isoformat()
    return None


def proj_code(v):
    """Project cell is a float like 5.2 -> '5.2'."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return ("%g" % v)
    return str(v).strip()


def overlap_min(s, e, ws, we):
    return max(0.0, min(e, we) - max(s, ws))


def compute_break(start_m, end_m, windows):
    if start_m is None or end_m is None:
        return 0.0
    return sum(overlap_min(start_m, end_m, a, b) for (a, b) in windows)


def compute_hours(start_m, end_m, break_mins):
    if start_m is None or end_m is None:
        return 0.0
    worked = ((end_m - start_m) % 1440) / 60.0
    return max(0.0, worked - break_mins / 60.0)


# --------------------------------------------------------------------------- #
# main import
# --------------------------------------------------------------------------- #
def main(xlsx_path, db_path):
    import openpyxl

    if not os.path.exists(xlsx_path):
        sys.exit(f"Workbook not found: {xlsx_path}")

    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    lists = wb["Lists"]
    log = wb["Master Log"]
    readme = wb["Read Me"]
    # Second load WITH formulas, so we can tell a typed Break from the formula.
    log_f = openpyxl.load_workbook(xlsx_path, data_only=False)["Master Log"]

    # ---- (re)create database from schema ---------------------------------- #
    if os.path.exists(db_path):
        os.remove(db_path)
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys = ON")
    with open(SCHEMA_SQL, encoding="utf-8") as f:
        con.executescript(f.read())
    cur = con.cursor()

    report = {"ref_added": [], "recon": [], "overlaps": []}

    # ---- Lists helpers ---------------------------------------------------- #
    def col_values(letter):
        c = openpyxl.utils.column_index_from_string(letter)
        out = []
        for r in range(2, lists.max_row + 1):
            v = lists.cell(r, c).value
            if v not in (None, ""):
                out.append(v)
        return out

    def header_map():
        """{header_text: column_index} for the Lists header row."""
        m = {}
        for c in range(1, lists.max_column + 1):
            h = lists.cell(1, c).value
            if h not in (None, ""):
                m[str(h).strip()] = c
        return m

    hmap = header_map()

    # ---- projects (names from Read Me) ------------------------------------ #
    proj_names = {}
    for r in range(1, readme.max_row + 1):
        cells = [readme.cell(r, c).value for c in range(1, readme.max_column + 1)]
        cells = [str(c).strip() for c in cells if c not in (None, "")]
        if len(cells) >= 2 and re.fullmatch(r"\d+\.\d+", cells[0]):
            proj_names[cells[0]] = cells[1]

    for code in col_values("A"):
        code = proj_code(code)
        name = proj_names.get(code, f"Project {code}")
        cur.execute(
            "INSERT OR IGNORE INTO projects(code,name,client_code,internal_code,"
            "day_start,day_end,hours_per_day) VALUES (?,?,?,?,?,?,?)",
            (code, name, "M45", "2630",
             DEFAULT_CONFIG["day_start"], DEFAULT_CONFIG["day_end"],
             DEFAULT_CONFIG["hours_per_day"]))
        for (bn, bs, be) in DEFAULT_BREAKS:
            cur.execute("INSERT OR IGNORE INTO break_windows(project_code,name,"
                        "start_time,end_time) VALUES (?,?,?,?)", (code, bn, bs, be))

    # per-project break windows as minutes (for computation)
    breaks_by_proj = {}
    for code, bs, be in cur.execute(
            "SELECT project_code,start_time,end_time FROM break_windows").fetchall():
        h1, m1 = map(int, bs.split(":"))
        h2, m2 = map(int, be.split(":"))
        breaks_by_proj.setdefault(code, []).append((h1 * 60 + m1, h2 * 60 + m2))
    hrs_per_day = {code: hpd for code, hpd in
                   cur.execute("SELECT code,hours_per_day FROM projects")}

    # ---- shared reference tables ------------------------------------------ #
    # levels (numeric + 'All levels' sentinel)
    # levels belong to a project; they are created from the log, not a shared list
    def add_level(label, project_code=None):
        if label in (None, "") or not project_code:
            return None
        label = str(label).strip()
        row = cur.execute(
            "SELECT id FROM levels WHERE project_code=? AND label=?",
            (project_code, label)).fetchone()
        if row:
            cur.execute(
                "INSERT OR IGNORE INTO project_levels(project_code,level_id) VALUES (?,?)",
                (project_code, row[0]))
            return row[0]
        num, is_all = None, 0
        m = re.search(r"-?\d+", label)
        if m and "all" not in label.lower():
            num = int(m.group())
        else:
            is_all = 1
        cur.execute(
            "INSERT INTO levels(project_code,number,label,is_all) VALUES (?,?,?,?)",
            (project_code, num, label, is_all))
        lid = cur.lastrowid
        cur.execute(
            "INSERT OR IGNORE INTO project_levels(project_code,level_id) VALUES (?,?)",
            (project_code, lid))
        return lid

    def get_or_create(table, name, extra_report=True):
        name = str(name).strip()
        row = cur.execute(f"SELECT id FROM {table} WHERE name=?", (name,)).fetchone()
        if row:
            return row[0]
        cur.execute(f"INSERT INTO {table}(name) VALUES (?)", (name,))
        if extra_report:
            report["ref_added"].append(f"{table}: {name!r}")
        return cur.lastrowid

    # sheet types (shared, 11)
    st_col = hmap.get("Sheet Types")
    if st_col:
        for r in range(2, lists.max_row + 1):
            v = lists.cell(r, st_col).value
            if v not in (None, ""):
                cur.execute("INSERT OR IGNORE INTO sheet_types(name) VALUES (?)",
                            (str(v).strip(),))

    # statuses, people, task_names (Task Name + Sub-Tasks)
    for v in col_values("I"):
        cur.execute("INSERT OR IGNORE INTO statuses(name) VALUES (?)", (str(v).strip(),))
    for rname in ("Manager", "Team Leader", "Senior Team Leader", "Coordinator", "Modeller"):
        cur.execute("INSERT OR IGNORE INTO roles(name) VALUES (?)", (rname,))
    for v in col_values("H"):
        cur.execute("INSERT OR IGNORE INTO people(name) VALUES (?)", (str(v).strip(),))
    for letter in ("G", "W"):
        for v in col_values(letter):
            cur.execute("INSERT OR IGNORE INTO task_names(name) VALUES (?)", (str(v).strip(),))

    # ---- per-project reference tables ------------------------------------- #
    def get_or_create_scoped(table, project_code, name, is_all=None):
        name = str(name).strip()
        row = cur.execute(
            f"SELECT id FROM {table} WHERE project_code=? AND name=?",
            (project_code, name)).fetchone()
        if row:
            return row[0]
        if is_all is not None:
            cur.execute(f"INSERT INTO {table}(project_code,name,is_all) VALUES (?,?,?)",
                        (project_code, name, is_all))
        else:
            cur.execute(f"INSERT INTO {table}(project_code,name) VALUES (?,?)",
                        (project_code, name))
        report["ref_added"].append(f"{table}[{project_code}]: {name!r}")
        return cur.lastrowid

    # buildings: 'Building (5.2)' / 'Building (5.4)' columns
    for hdr, c in hmap.items():
        m = re.fullmatch(r"Building \((\d+\.\d+)\)", hdr)
        if m:
            code = m.group(1)
            for r in range(2, lists.max_row + 1):
                v = lists.cell(r, c).value
                if v not in (None, ""):
                    nm = str(v).strip()
                    get_or_create_scoped("buildings", code, nm,
                                         is_all=1 if nm.lower().startswith("all") else 0)
            get_or_create_scoped("buildings", code, "All Blocks", is_all=1)

    # models: 'Model Name (5.2)' / '(5.4)'
    for hdr, c in hmap.items():
        m = re.fullmatch(r"Model Name \((\d+\.\d+)\)", hdr)
        if m:
            code = m.group(1)
            for r in range(2, lists.max_row + 1):
                v = lists.cell(r, c).value
                if v not in (None, ""):
                    get_or_create_scoped("models", code, str(v).strip())

    # master_tasks: columns headed '<proj>_<SheetType>'  (e.g. '5.2_General')
    st_id = {name: sid for sid, name in
             [(r[0], r[1]) for r in cur.execute("SELECT id,name FROM sheet_types")]}
    def sheet_type_id(name):
        if name is None:
            return None
        name = str(name).strip()
        row = cur.execute("SELECT id FROM sheet_types WHERE name=?", (name,)).fetchone()
        if row:
            return row[0]
        cur.execute("INSERT INTO sheet_types(name) VALUES (?)", (name,))
        report["ref_added"].append(f"sheet_types: {name!r}")
        return cur.lastrowid

    mt_dupe = 0
    for hdr, c in hmap.items():
        m = re.fullmatch(r"(\d+\.\d+)_(.+)", hdr)
        if not m:
            continue
        code, stype = m.group(1), m.group(2).strip()
        stid = sheet_type_id(stype)
        for r in range(2, lists.max_row + 1):
            v = lists.cell(r, c).value
            if v in (None, ""):
                continue
            nm = re.sub(r"^\[\d+\]\s*", "", str(v).strip())  # drop any [NN] prefix
            exists = cur.execute(
                "SELECT id FROM master_tasks WHERE project_code=? AND name=?",
                (code, nm)).fetchone()
            if exists:
                mt_dupe += 1
                continue
            cur.execute("INSERT INTO master_tasks(project_code,sheet_type_id,name) "
                        "VALUES (?,?,?)", (code, stid, nm))
    if mt_dupe:
        report["ref_added"].append(f"(master_tasks: skipped {mt_dupe} duplicate catalog rows)")

    con.commit()

    # ---- resolve helpers for the fact load -------------------------------- #
    def id_of(table, name):
        if name in (None, ""):
            return None
        row = cur.execute(f"SELECT id FROM {table} WHERE name=?",
                          (str(name).strip(),)).fetchone()
        return row[0] if row else None

    def scoped_id(table, code, name, is_all_default=0):
        if name in (None, ""):
            return None
        return get_or_create_scoped(table, code, name,
                                    is_all=None if table == "models" else
                                    (1 if str(name).lower().startswith("all") else 0))

    def master_task_id(code, name, stype):
        if name in (None, ""):
            return None
        nm = re.sub(r"^\[\d+\]\s*", "", str(name).strip())
        row = cur.execute("SELECT id FROM master_tasks WHERE project_code=? AND name=?",
                          (code, nm)).fetchone()
        if row:
            return row[0]
        cur.execute("INSERT INTO master_tasks(project_code,sheet_type_id,name) "
                    "VALUES (?,?,?)", (code, sheet_type_id(stype), nm))
        report["ref_added"].append(f"master_tasks[{code}]: {nm!r} (from log)")
        return cur.lastrowid

    # ---- load tasks ------------------------------------------------------- #
    # Master Log columns (1-indexed): A Date .. see schema comment
    C = dict(date=1, start=2, end=3, brk=4, perm=5, proj=6, sheet=7, master=8,
             bld=9, lvl=10, group=11, model=12, task=13, desc=14,
             asg1=15, asg6=20, status=21, pct=22, hours=23, mandays=24, notes=25)

    day_people = {}   # (date, person) -> list of (start_m, end_m, task_id)
    n_tasks = 0

    for r in range(2, log.max_row + 1):
        d = log.cell(r, C["date"]).value
        st = log.cell(r, C["start"]).value
        en = log.cell(r, C["end"]).value
        if d is None and st is None and en is None:
            continue

        code = proj_code(log.cell(r, C["proj"]).value)
        start_m, end_m = to_min(st), to_min(en)
        wins = breaks_by_proj.get(code, [(765, 810), (675, 690), (975, 990)])

        # Break: trust a TYPED value (user enters it by hand when the sheet's
        # auto-formula didn't fire); COMPUTE only where the formula was used or
        # the cell is blank.
        brk_formula = log_f.cell(r, C["brk"]).value
        brk_typed = log.cell(r, C["brk"]).value
        is_formula = isinstance(brk_formula, str) and brk_formula.startswith("=")
        if not is_formula and isinstance(brk_typed, (int, float)):
            brk = float(brk_typed)                       # trust typed value
        else:
            brk = compute_break(start_m, end_m, wins)    # formula / blank -> compute
        hrs = compute_hours(start_m, end_m, brk)

        # reconcile against the sheet's own values
        sheet_brk = log.cell(r, C["brk"]).value
        sheet_hrs = log.cell(r, C["hours"]).value
        sheet_md  = log.cell(r, C["mandays"]).value

        stype = log.cell(r, C["sheet"]).value
        perm = 1 if str(log.cell(r, C["perm"]).value).strip().lower() == "yes" else 0
        grouped = 1 if str(log.cell(r, C["group"]).value).strip().lower() == "yes" else 0

        cur.execute(
            """INSERT INTO tasks(work_date,start_time,end_time,break_mins,hours,
                 permission,grouped,project_code,sheet_type_id,master_task_id,building_id,
                 level_id,model_id,task_name_id,description,status_id,
                 pct_complete,notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (date_iso(d), hhmm(st), hhmm(en), round(brk, 2), round(hrs, 4),
             perm, grouped, code, sheet_type_id(stype),
             master_task_id(code, log.cell(r, C["master"]).value, stype),
             scoped_id("buildings", code, log.cell(r, C["bld"]).value),
             add_level(log.cell(r, C["lvl"]).value, code) if log.cell(r, C["lvl"]).value else None,
             scoped_id("models", code, log.cell(r, C["model"]).value),
             id_of("task_names", log.cell(r, C["task"]).value),
             (str(log.cell(r, C["desc"]).value) if log.cell(r, C["desc"]).value else None),
             id_of("statuses", log.cell(r, C["status"]).value),
             log.cell(r, C["pct"]).value,
             (str(log.cell(r, C["notes"]).value) if log.cell(r, C["notes"]).value else None)))
        task_id = cur.lastrowid
        n_tasks += 1

        # assignees (skip blank and the 'None' sentinel)
        n_real = 0
        for c in range(C["asg1"], C["asg6"] + 1):
            p = log.cell(r, c).value
            if p in (None, "") or str(p).strip().lower() == "none":
                continue
            pid = id_of("people", p)
            if pid is None:
                cur.execute("INSERT OR IGNORE INTO people(name) VALUES (?)", (str(p).strip(),))
                report["ref_added"].append(f"people: {str(p).strip()!r} (from log)")
                pid = id_of("people", p)
            cur.execute("INSERT OR IGNORE INTO task_assignees(task_id,person_id) "
                        "VALUES (?,?)", (task_id, pid))
            n_real += 1
            if start_m is not None:
                day_people.setdefault((date_iso(d), pid), []).append(
                    (start_m, end_m, task_id, str(p).strip()))

        # reconciliation
        md_calc = round(hrs / hrs_per_day.get(code, 8.25) * n_real, 4)
        if sheet_brk is not None and abs(float(sheet_brk) - brk) > TOL:
            report["recon"].append(f"row{r} break: sheet={float(sheet_brk):.2f} calc={brk:.2f}")
        if sheet_hrs is not None and abs(float(sheet_hrs) - hrs) > TOL:
            report["recon"].append(f"row{r} hours: sheet={float(sheet_hrs):.3f} calc={hrs:.3f}")
        if sheet_md is not None and abs(float(sheet_md) - md_calc) > TOL:
            report["recon"].append(f"row{r} man-days: sheet={float(sheet_md):.3f} calc={md_calc:.3f}")

    con.commit()

    # ---- overlap validation (same person, same day) ---------------------- #
    for (date, pid), items in day_people.items():
        items = [x for x in items if x[0] is not None and x[1] is not None]
        items.sort()
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                s1, e1, t1, nm = items[i]
                s2, e2, t2, _ = items[j]
                if s2 < e1 and s1 < e2:  # intervals overlap
                    report["overlaps"].append(
                        f"{nm} {date}: task#{t1} [{hhmm_from_min(s1)}-{hhmm_from_min(e1)}] "
                        f"overlaps task#{t2} [{hhmm_from_min(s2)}-{hhmm_from_min(e2)}]")

    con.commit()
    con.close()

    # ---- print report ----------------------------------------------------- #
    def counts():
        c2 = sqlite3.connect(db_path)
        for t in ("projects", "buildings", "models", "master_tasks", "sheet_types",
                  "levels", "task_names", "people", "statuses", "tasks", "task_assignees"):
            n = c2.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            print(f"    {t:16} {n}")
        c2.close()

    print(f"\n=== IMPORT COMPLETE -> {db_path} ===")
    print(f"  tasks loaded: {n_tasks}")
    print("  row counts:")
    counts()

    print(f"\n  reference rows auto-added from the log / dedupe ({len(report['ref_added'])}):")
    for line in report["ref_added"][:40]:
        print("    +", line)
    if len(report["ref_added"]) > 40:
        print(f"    ... and {len(report['ref_added'])-40} more")

    print(f"\n  RECONCILIATION vs sheet ({len(report['recon'])} mismatches, tol={TOL}):")
    if not report["recon"]:
        print("    OK — every row's break / hours / man-days match the sheet.")
    for line in report["recon"][:50]:
        print("    !", line)

    print(f"\n  OVERLAP CHECK — same person same day ({len(report['overlaps'])}):")
    if not report["overlaps"]:
        print("    OK — no overlapping task times per person per day.")
    for line in report["overlaps"][:50]:
        print("    !", line)


if __name__ == "__main__":
    xlsx = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_XLSX
    db   = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_DB
    main(xlsx, db)
