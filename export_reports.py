#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Export a report to .xlsx.

    --report daily    (default) -> DAILY PROGRESS REPORT for one date
                                  (+ Not filled sheet + Permission),
    --report notfilled -> people and dates with no task logged in the range.
                                  matching the workbook layout.
  --report manday   -> Man-day Summary for the given filters.
  --report search   -> Search results for the given filters.
  --report internal -> effort per person for the date range.

Query reports (manday/search/internal) take filters: --project --building --level
--person --sheet-type --master-task --from --to. Blank filters are ignored.

Usage:
    python export_reports.py --report daily --from 2026-08-04 --to 2026-08-04
    python export_reports.py --report manday --person Aasin --from 2026-08-01 --to 2026-08-31
"""
import sys, os, argparse, sqlite3, datetime as dt

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
INK = "1F4E5F"

DAILY_HEADERS = ["Project", "Building", "Level", "Model Name", "Category", "Task Name",
                 "Sub Task", "Task Description", "Assignees", "Status", "Notes"]
DAILY_WIDTHS = [9, 22, 12, 34, 18, 18, 44, 44, 16, 14, 30]
DAILY_SQL = """
SELECT t.project_code,
       COALESCE(b.name, ''), COALESCE(l.label, ''), COALESCE(m.name, ''),
       COALESCE((SELECT GROUP_CONCAT(c2.name, ', ') FROM task_categories tc
                   JOIN categories c2 ON c2.id = tc.category_id WHERE tc.task_id = t.id),
                cat.name, ''),
       COALESCE(tn.name, ''),
       COALESCE((SELECT GROUP_CONCAT(s2.name, ', ') FROM task_sub_task_links tl
                   JOIN task_sub_tasks s2 ON s2.id = tl.task_sub_task_id WHERE tl.task_id = t.id),
                ts.name, ''),
       COALESCE(t.description, ''),
       (SELECT GROUP_CONCAT(pe.name, ', ') FROM task_assignees ta
          JOIN people pe ON pe.id = ta.person_id
         WHERE ta.task_id = t.id
           AND NOT EXISTS (
                 SELECT 1 FROM roles r WHERE r.id=pe.role_id
                   AND lower(trim(COALESCE(r.name,'')))='manager')
           AND NOT EXISTS (
                 SELECT 1 FROM project_people pp LEFT JOIN roles pr ON pr.id=pp.role_id
                  WHERE pp.person_id=pe.id AND pp.project_code=t.project_code
                    AND lower(trim(COALESCE(pr.name,'')))='manager')),
       COALESCE(s.name, ''), COALESCE(t.notes, '')
FROM tasks t
LEFT JOIN buildings b ON b.id=t.building_id
LEFT JOIN levels    l ON l.id=t.level_id
LEFT JOIN models    m ON m.id=t.model_id
LEFT JOIN task_names tn ON tn.id=t.task_name_id
LEFT JOIN categories cat ON cat.id=t.category_id
LEFT JOIN task_sub_tasks ts ON ts.id=t.task_sub_task_id
LEFT JOIN statuses  s ON s.id=t.status_id
WHERE t.work_date = ?
  AND EXISTS (
        SELECT 1 FROM task_assignees ta
         WHERE ta.task_id = t.id
           AND NOT EXISTS (
                 SELECT 1 FROM people pe LEFT JOIN roles r ON r.id=pe.role_id
                  WHERE pe.id=ta.person_id
                    AND lower(trim(COALESCE(r.name,'')))='manager')
           AND NOT EXISTS (
                 SELECT 1 FROM project_people pp LEFT JOIN roles pr ON pr.id=pp.role_id
                  WHERE pp.person_id=ta.person_id AND pp.project_code=t.project_code
                    AND lower(trim(COALESCE(pr.name,'')))='manager')
      )
ORDER BY t.project_code, t.start_time
"""

# Query reports: (title, headers, SQL with {where}, indexes to total)
QUERY_REPORTS = {
    "manday": ("MAN-DAY SUMMARY",
               ["Person", "Category", "Task", "Tasks", "Hours", "Man-days"],
               "SELECT person, COALESCE(category,''), task_name, COUNT(*), ROUND(SUM(spent_hours),2), "
               "ROUND(SUM(norm_manday),3) FROM v_task_effort {where} "
               "GROUP BY person, category, task_name ORDER BY person, category, task_name",
               [3, 4, 5]),
    "search": ("SEARCH RESULTS",
               ["Date", "Person", "Project", "Zone", "Level", "Master Task",
                "Category", "Task", "Sub Task", "Description", "Status", "Hours"],
               "SELECT work_date, person, project_code, building, level, master_task, category, "
               "task_name, sub_task, description, status, ROUND(hours,2) "
               "FROM v_task_effort {where} ORDER BY work_date, person",
               []),
    "internal": ("INTERNAL EFFORT REPORT",
                 ["Person", "Tasks", "Hours", "Man-days"],
                 "SELECT person, COUNT(*), ROUND(SUM(spent_hours),2), ROUND(SUM(norm_manday),3) "
                 "FROM v_task_effort {where} GROUP BY person ORDER BY person",
                 [1, 2, 3]),
}


def project_line(con, project_code=None):
    if project_code:
        prj = con.execute(
            "SELECT code, name FROM projects WHERE code=?",
            (project_code,)).fetchone()
        if not prj:
            return f"Project {project_code}"
        code, name = prj
        bits = [f"Project code {code}"]
        if name:
            bits.append(name)
        return "  ·  ".join(bits)
    return "All projects"


def _styles():
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    thin = Side(style="thin", color="BBBBBB")
    return dict(
        title=Font(bold=True, size=16, color=INK), sub=Font(size=10, color="555555"),
        label=Font(bold=True), hdr=Font(bold=True, color="FFFFFF"),
        fill=PatternFill("solid", fgColor=INK), border=Border(left=thin, right=thin, top=thin, bottom=thin),
        Alignment=Alignment)


def _pp_has_dates(con):
    cols = {r[1] for r in con.execute("PRAGMA table_info(project_people)")}
    return "joined_on" in cols and "left_on" in cols


def _as_date(value):
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def next_working_day(on_date):
    """First weekday after on_date. Saturday and Sunday are skipped."""
    d = _as_date(on_date)
    if not d:
        return None
    d = d + dt.timedelta(days=1)
    while d.weekday() >= 5:
        d += dt.timedelta(days=1)
    return d.isoformat()


def on_team_that_day(joined_on, left_on, on_date):
    """True if this membership covers on_date.

    Joined on is inclusive. Left on is their last day; they drop off from
    the next working day after that (weekends skipped). Left Saturday 5th
    means still on the team on the 5th and off from Monday the 7th.
    """
    day = _as_date(on_date)
    if day is None:
        return not left_on
    joined = _as_date(joined_on)
    if joined and joined > day:
        return False
    if not left_on:
        return True
    nxt = next_working_day(left_on)
    return bool(nxt) and day.isoformat() < nxt


def project_team_people(con, project, on_date=None):
    """People expected to log on this project on on_date.

    Newly added members are ignored before their join date. Removed members
    stay on through Left on and are ignored from the next working day.
    """
    if not project:
        return [], ""
    on_date = on_date or dt.date.today().isoformat()
    dated = _pp_has_dates(con)
    has_team = con.execute(
        "SELECT 1 FROM project_people WHERE project_code=? LIMIT 1",
        (project,)).fetchone()
    if has_team:
        if dated:
            rows = con.execute(
                """
                SELECT pe.id, pe.name, pe.emp_code, pe.location,
                       COALESCE(pr.name, r.name, '') AS role,
                       pp.joined_on, pp.left_on
                FROM people pe
                JOIN project_people pp
                  ON pp.person_id = pe.id AND pp.project_code = ?
                LEFT JOIN roles pr ON pr.id = pp.role_id
                LEFT JOIN roles r ON r.id = pe.role_id
                WHERE pp.joined_on <= ?
                ORDER BY pe.name, pp.left_on IS NOT NULL, pp.joined_on
                """,
                (project, on_date)).fetchall()
            seen = set()
            out = []
            for r in rows:
                pid = r[0]
                if pid in seen:
                    continue
                if on_team_that_day(r[5], r[6], on_date):
                    seen.add(pid)
                    out.append(r)
            return out, f"project team as of {on_date}"
        rows = con.execute(
            """
            SELECT DISTINCT pe.id, pe.name, pe.emp_code, pe.location,
                   COALESCE(pr.name, r.name, '') AS role
            FROM people pe
            LEFT JOIN project_people pp ON pp.person_id = pe.id AND pp.project_code = ?
            LEFT JOIN roles pr ON pr.id = pp.role_id
            LEFT JOIN roles r ON r.id = pe.role_id
            WHERE pe.id IN (
                SELECT person_id FROM project_people WHERE project_code = ?
                UNION SELECT lead_id FROM lead_support WHERE project_code = ?
                UNION SELECT support_id FROM lead_support WHERE project_code = ?
            )
            ORDER BY pe.name
            """,
            (project, project, project, project)).fetchall()
        return rows, "project team"
    rows = con.execute(
        """
        SELECT DISTINCT pe.id, pe.name, pe.emp_code, pe.location, COALESCE(r.name, '') AS role
        FROM people pe
        JOIN task_assignees ta ON ta.person_id = pe.id
        JOIN tasks t ON t.id = ta.task_id
        LEFT JOIN roles r ON r.id = pe.role_id
        WHERE t.project_code = ?
        ORDER BY pe.name
        """,
        (project,)).fetchall()
    return rows, "people who have logged this project"


SKIP_DAILY_FILL_ROLES = {"manager"}


def _is_skip_fill_role(name):
    return (name or "").strip().lower() in SKIP_DAILY_FILL_ROLES


def not_manager_sql(person_id_col="person_id", project_col="project_code"):
    """SQL: this person is not a Manager (employee role or project role)."""
    return (
        f"NOT EXISTS ("
        f" SELECT 1 FROM people pe LEFT JOIN roles r ON r.id=pe.role_id"
        f" WHERE pe.id={person_id_col}"
        f" AND lower(trim(COALESCE(r.name,'')))='manager')"
        f" AND NOT EXISTS ("
        f" SELECT 1 FROM project_people pp LEFT JOIN roles pr ON pr.id=pp.role_id"
        f" WHERE pp.person_id={person_id_col} AND pp.project_code={project_col}"
        f" AND lower(trim(COALESCE(pr.name,'')))='manager')"
    )


def with_managers_excluded(where, person_id_col="person_id", project_col="project_code"):
    clause = not_manager_sql(person_id_col, project_col)
    return (where + " AND " + clause) if where else ("WHERE " + clause)


def _employee_role_names(con, person_ids):
    if not person_ids:
        return {}
    ph = ",".join("?" * len(person_ids))
    return {
        int(r[0]): (r[1] or "")
        for r in con.execute(
            f"SELECT pe.id, r.name FROM people pe "
            f"LEFT JOIN roles r ON r.id = pe.role_id "
            f"WHERE pe.id IN ({ph})",
            list(person_ids))
    }


def daily_fill_status(con, report_date, project, location=None):
    """Team vs who logged a task that day. missing = team members with no task.

    Managers are excluded (employee role or project role): they cover several
    projects and their work is supervisory, so they are not expected to fill
    a daily task.
    """
    team, source = project_team_people(con, project, report_date)
    roles = _employee_role_names(con, [p[0] for p in team])
    team = [
        p for p in team
        if not _is_skip_fill_role(roles.get(int(p[0])))
        and not _is_skip_fill_role(p[4])
    ]
    people = [{"id": p[0], "name": p[1], "emp_code": p[2],
               "location": p[3], "role": p[4]} for p in team]
    if location:
        people = [p for p in people if p["location"] == location]
    con.execute("""
        CREATE TABLE IF NOT EXISTS leave_days (
            person_id INTEGER NOT NULL,
            work_date TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (person_id, work_date)
        )
    """)
    leave_ids = {r[0] for r in con.execute(
        "SELECT person_id FROM leave_days WHERE work_date=?", (report_date,))}
    people = [p for p in people if p["id"] not in leave_ids]
    filled_ids = set()
    if project and report_date:
        sql = (
            "SELECT DISTINCT ta.person_id FROM task_assignees ta "
            "JOIN tasks t ON t.id = ta.task_id "
            "JOIN people pe ON pe.id = ta.person_id "
            "WHERE t.work_date = ? AND t.project_code = ?")
        params = [report_date, project]
        if location:
            sql += " AND pe.location = ?"
            params.append(location)
        filled_ids = {r[0] for r in con.execute(sql, params)}
    return {
        "source": source,
        "expected": people,
        "filled": [p for p in people if p["id"] in filled_ids],
        "missing": [p for p in people if p["id"] not in filled_ids],
    }


def working_days(dfrom, dto):
    """Return weekdays in the inclusive date range."""
    start = _as_date(dfrom)
    end = _as_date(dto or dfrom)
    if not start or not end or end < start:
        return []
    days = []
    while start <= end:
        if start.weekday() < 5:
            days.append(start.isoformat())
        start += dt.timedelta(days=1)
    return days


def range_fill_status(con, dfrom, dto, project, location=None):
    """Return one row per project team member and weekday with no task logged."""
    missing = []
    expected_by_person = {}
    filled_by_person = {}
    for report_date in working_days(dfrom, dto):
        status = daily_fill_status(con, report_date, project, location)
        for person in status["expected"]:
            expected_by_person[person["id"]] = expected_by_person.get(person["id"], 0) + 1
        for person in status["filled"]:
            filled_by_person[person["id"]] = filled_by_person.get(person["id"], 0) + 1
        for person in status["missing"]:
            missing.append(dict(person, missing_date=report_date))
    return {
        "missing": missing,
        "expected": sum(expected_by_person.values()),
        "filled": sum(filled_by_person.values()),
        "days": working_days(dfrom, dto),
        "source": "project team on each weekday",
    }


def write_not_filled_sheet(con, ws, dfrom, dto=None, project=None, location=None):
    from openpyxl.utils import get_column_letter
    s = _styles()
    headers = ["Date", "Person", "Emp ID", "Role", "Location"]
    ncol = len(headers)
    ws["A1"] = "DID NOT FILL — no task logged"; ws["A1"].font = s["title"]
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ncol)
    dto = dto or dfrom
    ws["A3"] = "Date range"; ws["A3"].font = s["label"]; ws["B3"] = f"{dfrom} to {dto}"
    ws["A4"] = "Project"; ws["A4"].font = s["label"]
    ws["B4"] = project_line(con, project)
    status = range_fill_status(con, dfrom, dto, project, location)
    ws["A5"] = "Compared against"; ws["A5"].font = s["label"]
    ws["B5"] = "Project team on each weekday (Managers excluded)"
    ws["A6"] = "Expected person/dates"; ws["A6"].font = s["label"]; ws["B6"] = status["expected"]
    ws["A7"] = "Filled person/dates"; ws["A7"].font = s["label"]; ws["B7"] = status["filled"]
    ws["A8"] = "Missing person/dates"; ws["A8"].font = s["label"]; ws["B8"] = len(status["missing"])
    if location:
        ws["A9"] = "Location"; ws["A9"].font = s["label"]; ws["B9"] = location
    hr = 11
    for i, h in enumerate(headers, 1):
        c = ws.cell(hr, i, h)
        c.font = s["hdr"]; c.fill = s["fill"]; c.border = s["border"]
    r = hr + 1
    for p in status["missing"]:
        vals = [p["missing_date"], p["name"], p["emp_code"] or "", p["role"] or "", p["location"] or ""]
        for i, v in enumerate(vals, 1):
            c = ws.cell(r, i, v)
            c.border = s["border"]
        r += 1
    if not project:
        ws["A12"] = "Pick a project to see who did not fill."
    elif not status["expected"]:
        ws["A12"] = "No project team yet — add people in Manage Lists."
    elif not status["missing"]:
        ws["A12"] = "Everyone on the project team logged a task on every weekday."
    for i, w in enumerate([14, 28, 12, 22, 14], 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A12"
    return len(status["missing"])


def write_daily_report(con, ws, report_date, project=None, location=None):
    from openpyxl.utils import get_column_letter
    s = _styles()
    ncol = len(DAILY_HEADERS)
    ws["A1"] = "DAILY PROGRESS REPORT"; ws["A1"].font = s["title"]
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ncol)
    ws["A3"] = "Report date"; ws["A3"].font = s["label"]; ws["B3"] = report_date
    code, name = "", ""
    if project:
        prj = con.execute(
            "SELECT code, name FROM projects WHERE code=?", (project,)).fetchone()
        if prj:
            code, name = prj[0] or project, prj[1] or ""
        else:
            code = project
    ws["A4"] = "Project code"; ws["A4"].font = s["label"]; ws["B4"] = code
    ws["A5"] = "Project name"; ws["A5"].font = s["label"]; ws["B5"] = name
    sql, params = DAILY_SQL, [report_date]
    extra = []
    if project:
        extra.append("t.project_code = ?")
        params.append(project)
    if location:
        extra.append(
            "EXISTS (SELECT 1 FROM task_assignees ta "
            "JOIN people pe ON pe.id=ta.person_id "
            "WHERE ta.task_id=t.id AND pe.location=?)")
        params.append(location)
    if extra:
        sql = DAILY_SQL.replace(
            "WHERE t.work_date = ?",
            "WHERE t.work_date = ? AND " + " AND ".join(extra))
    ws["A6"] = "Tasks reported"; ws["A6"].font = s["label"]
    rows = con.execute(sql, params).fetchall()
    ws["B6"] = len(rows)
    status = daily_fill_status(con, report_date, project, location)
    ws["A7"] = "Team members"; ws["A7"].font = s["label"]; ws["B7"] = len(status["expected"])
    ws["A8"] = "Filled a task"; ws["A8"].font = s["label"]; ws["B8"] = len(status["filled"])
    ws["A9"] = "Did not fill"; ws["A9"].font = s["label"]; ws["B9"] = len(status["missing"])
    if location:
        ws["A10"] = "Location"; ws["A10"].font = s["label"]; ws["B10"] = location
    hr = 12
    for i, h in enumerate(DAILY_HEADERS, 1):
        c = ws.cell(hr, i, h)
        c.font = s["hdr"]; c.fill = s["fill"]; c.border = s["border"]
        c.alignment = s["Alignment"](vertical="center", wrap_text=True)
    r = hr + 1
    for row in rows:
        for i, v in enumerate(row, 1):
            c = ws.cell(r, i, "" if v is None else v)
            c.border = s["border"]; c.alignment = s["Alignment"](vertical="top", wrap_text=(i in (6, 9)))
        r += 1
    for i, w in enumerate(DAILY_WIDTHS, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A13"
    return len(rows)


def write_query_report(con, ws, report, where, params, filters_text, project=None):
    from openpyxl.utils import get_column_letter
    s = _styles()
    title, headers, sql, totals_idx = QUERY_REPORTS[report]
    ncol = len(headers)
    ws["A1"] = title; ws["A1"].font = s["title"]
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ncol)
    ws["A2"] = project_line(con, project); ws["A2"].font = s["sub"]
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=ncol)
    ws["A3"] = "Filters: " + (filters_text or "none (all data)"); ws["A3"].font = s["sub"]
    ws.merge_cells(start_row=3, start_column=1, end_row=3, end_column=ncol)

    if report in ("manday", "internal"):
        where = with_managers_excluded(where)
    rows = con.execute(sql.format(where=where), params).fetchall()
    hr = 5
    for i, h in enumerate(headers, 1):
        c = ws.cell(hr, i, h)
        c.font = s["hdr"]; c.fill = s["fill"]; c.border = s["border"]
        c.alignment = s["Alignment"](vertical="center", wrap_text=True)
    r = hr + 1
    totals = {}
    for row in rows:
        for i, v in enumerate(row, 1):
            c = ws.cell(r, i, "" if v is None else v)
            c.border = s["border"]; c.alignment = s["Alignment"](vertical="top", wrap_text=(i in (8,)))
        for ci in totals_idx:
            totals[ci] = totals.get(ci, 0) + (row[ci] or 0)
        r += 1
    if totals_idx and rows:
        ws.cell(r, 1, "Total").font = s["label"]
        for ci in totals_idx:
            ws.cell(r, ci + 1, round(totals[ci], 2)).font = s["label"]
    for i in range(1, ncol + 1):
        ws.column_dimensions[get_column_letter(i)].width = 34 if headers[i - 1] in ("Description",) else 16
    ws.freeze_panes = "A6"
    return len(rows)


def build_where(con, a):
    conds, params, human = [], [], []

    def add(col, val, label, like=False):
        if val:
            conds.append(f"{col} LIKE ?" if like else f"{col}=?")
            params.append(f"%{val}%" if like else val)
            human.append(f"{label}={val}")

    add("project_code", a.project, "Project")
    if a.person:
        pid = None
        row = con.execute(
            "SELECT id FROM people WHERE emp_code=? COLLATE NOCASE", (a.person,)).fetchone()
        if row:
            pid = row[0]
        if pid is None:
            row = con.execute(
                "SELECT id FROM people WHERE name=? COLLATE NOCASE", (a.person,)).fetchone()
            if row:
                pid = row[0]
        if pid is None and str(a.person).isdigit():
            row = con.execute("SELECT id FROM people WHERE id=?", (int(a.person),)).fetchone()
            if row:
                pid = row[0]
        if pid is not None:
            conds.append("person_id=?"); params.append(pid); human.append(f"Person={a.person}")
        else:
            add("person", a.person, "Person")
    add("location", a.location, "Location")
    flags = {"no_sheet_type": 0, "no_zone": 0}
    if a.project:
        prow = con.execute(
            "SELECT no_sheet_type, no_zone FROM projects WHERE code=?", (a.project,)).fetchone()
        if prow:
            flags = {"no_sheet_type": int(prow[0] or 0), "no_zone": int(prow[1] or 0)}
    if not flags["no_sheet_type"]:
        add("sheet_type", a.sheet_type, "Sheet Type")
    add("task_name", a.task_name, "Task Name")
    if a.master_task:          # a specific master task already pins zone & level
        add("master_task", a.master_task, "Master Task")
    else:
        if not flags["no_zone"]:
            add("building", a.building, "Zone")
        add("level", a.level, "Level")
    if a.dfrom:
        conds.append("work_date>=?"); params.append(a.dfrom); human.append(f"From={a.dfrom}")
    if a.dto:
        conds.append("work_date<=?"); params.append(a.dto); human.append(f"To={a.dto}")
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    return where, params, ", ".join(human)


MATRIX_TEXT = {"Completed": "Y", "Ongoing": "WIP", "On Hold": "HOLD", "Not Started": "N"}
MATRIX_FILL = {"Y": "C6EFCE", "WIP": "FFEB9C", "HOLD": "E4D3F5", "N": "FFC7CE"}


def write_matrix_report(con, ws, project, zone, level, dfrom, dto):
    """The zone/level x sub-task grid, mirroring the on-screen report."""
    from openpyxl.styles import Alignment, Font, PatternFill

    where, params = ["t.project_code=?"], [project]
    for col, val in (("b.name", zone), ("l.label", level)):
        if val:
            where.append(f"{col}=?"); params.append(val)
    if dfrom:
        where.append("t.work_date>=?"); params.append(dfrom)
    if dto:
        where.append("t.work_date<=?"); params.append(dto)

    rows = con.execute(f"""
        SELECT b.name AS zone, l.label AS level, COALESCE(c.name,'') AS category,
               ts.name AS sub_task, COALESCE(st.name,'') AS status
          FROM tasks t
          JOIN task_sub_task_links tl ON tl.task_id=t.id
          JOIN task_sub_tasks ts ON ts.id=tl.task_sub_task_id
          LEFT JOIN categories c ON c.id=ts.category_id
          LEFT JOIN buildings b ON b.id=t.building_id
          LEFT JOIN levels l ON l.id=t.level_id
          LEFT JOIN statuses st ON st.id=t.status_id
         WHERE {' AND '.join(where)}
         ORDER BY t.work_date, t.id""", params).fetchall()

    cols = con.execute("""
        SELECT DISTINCT COALESCE(c.name,''), ts.name FROM task_sub_tasks ts
          LEFT JOIN categories c ON c.id=ts.category_id
         WHERE ts.category_id IN (SELECT DISTINCT category_id FROM category_tasks ct
                                    JOIN master_tasks mt ON mt.id=ct.master_task_id
                                   WHERE mt.project_code=?)
         ORDER BY 1, 2""", (project,)).fetchall()
    zones = [r[0] for r in con.execute(
        "SELECT name FROM buildings WHERE project_code=? AND TRIM(name)<>'' ORDER BY name",
        (project,))]
    levels = [r[0] for r in con.execute(
        """SELECT l.label FROM project_levels pl JOIN levels l ON l.id=pl.level_id
            WHERE pl.project_code=? ORDER BY (l.number IS NULL), l.number""", (project,))]

    cell = {}
    for r in rows:
        if r[0] and r[1]:
            cell[(r[0], r[1], r[2], r[3])] = r[4]

    ws.append([f"ZONE / LEVEL PROGRESS GRID — project {project}"])
    ws["A1"].font = Font(bold=True, size=13)
    ws.append([])
    ws.append(["Zone", "Level"] + [c[0] for c in cols])
    ws.append(["", ""] + [c[1] for c in cols])
    for c in ws[3] + ws[4]:
        c.font = Font(bold=True, size=9)
        c.alignment = Alignment(textRotation=90, vertical="bottom", horizontal="center")
    ws["A3"].alignment = ws["B3"].alignment = Alignment(vertical="bottom")

    written = 0
    for z in zones:
        for lv in levels:
            marks = [cell.get((z, lv, cat, sub)) for cat, sub in cols]
            if not any(marks):
                continue
            written += 1
            ws.append([z, lv] + [MATRIX_TEXT.get(m, "") if m else "" for m in marks])
            for i, m in enumerate(marks, start=3):
                text = MATRIX_TEXT.get(m, "") if m else ""
                if text:
                    cl = ws.cell(row=ws.max_row, column=i)
                    cl.alignment = Alignment(horizontal="center")
                    cl.fill = PatternFill("solid", fgColor=MATRIX_FILL[text])

    ws.freeze_panes = "C5"
    ws.column_dimensions["A"].width = 10
    ws.column_dimensions["B"].width = 8
    ws.append([])
    ws.append(["Legend: Y = Completed, WIP = Work in progress, HOLD = On hold, "
               "N = Not started, blank = nothing logged"])
    return written


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(HERE, "tasklog.db"))
    ap.add_argument("--out", default=os.path.join(HERE, "reports.xlsx"))
    ap.add_argument("--report", default="daily",
                    choices=["daily", "notfilled", "manday", "search", "internal", "matrix"])
    ap.add_argument("--from", dest="dfrom")
    ap.add_argument("--to", dest="dto")
    ap.add_argument("--project"); ap.add_argument("--building"); ap.add_argument("--level")
    ap.add_argument("--person"); ap.add_argument("--location")
    ap.add_argument("--sheet-type", dest="sheet_type")
    ap.add_argument("--master-task", dest="master_task")
    ap.add_argument("--task-name", dest="task_name")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        sys.exit(f"Database not found: {args.db} (run import_xlsx.py first)")


    con = sqlite3.connect(args.db)
    try:
        with open(os.path.join(HERE, "views.sql"), encoding="utf-8") as vf:
            con.executescript(vf.read())
    except Exception:
        pass

    report_date = args.dfrom or args.dto or dt.date.today().isoformat()
    where, params, filters_text = build_where(con, args)

    def build(out_path):
        import openpyxl
        wb = openpyxl.Workbook()
        wb.remove(wb.active)  # drop default sheet
        if args.report == "daily":
            loc = getattr(args, "location", None)
            n = write_daily_report(con, wb.create_sheet("Daily Report"), report_date,
                                   args.project, loc)
            print(f"  Daily Report   {n:>5} rows (date {report_date})")
            miss = write_not_filled_sheet(con, wb.create_sheet("Not filled"),
                                          args.dfrom or report_date, args.dto,
                                          args.project, loc)
            print(f"  Not filled     {miss:>5} people")
            # Permission (who left early) — a daily client list, no filters needed
            perm = wb.create_sheet("Permission")
            perm.append(["Date", "Person", "First in", "Last out", "Permission", "Notes"])
            pr = con.execute("SELECT work_date, person, first_in, last_out, permission, notes "
                             "FROM v_permission_report WHERE work_date=? ORDER BY person",
                             (report_date,)).fetchall()
            for row in pr:
                perm.append(list(row))
            print(f"  Permission     {len(pr):>5} rows")
        elif args.report == "notfilled":
            miss = write_not_filled_sheet(con, wb.create_sheet("Not filled"),
                                          args.dfrom or report_date, args.dto,
                                          args.project, args.location)
            print(f"  Not filled     {miss:>5} people/dates")
        elif args.report == "matrix":
            n = write_matrix_report(con, wb.create_sheet("Zone-Level Grid"),
                                    args.project, args.building, args.level,
                                    args.dfrom, args.dto)
            print(f"  matrix       {n:>5} zone/level rows")
        else:
            n = write_query_report(con, wb.create_sheet(args.report[:31]), args.report,
                                   where, params, filters_text, args.project)
            print(f"  {args.report:12} {n:>5} rows")
        wb.save(out_path)

    out = args.out
    try:
        build(out)
    except PermissionError:
        base, ext = os.path.splitext(args.out)
        out = f"{base}_{dt.datetime.now():%H%M%S}{ext}"
        build(out)
        print(f"\nNOTE: '{os.path.basename(args.out)}' was open (locked); saved a new file instead:")

    con.close()
    print(f"\nExported -> {out}")


if __name__ == "__main__":
    main()
