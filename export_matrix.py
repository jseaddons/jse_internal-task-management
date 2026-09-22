"""Colour-coded status matrix for Excel - the QA progress tracker.

Rows are Floor+Zone grouped by person; columns are every task name in the
catalog, banded by category; the cell background carries the status. There are
no hours or man-days here on purpose - this report answers "how many tasks are
pending against the total", nothing else.

Three deliberate choices worth knowing:

1. Columns come from the CATALOG, not from what has been logged. A task nobody
   has touched still gets a column, and every cell in it defaults to Not done
   (red). Nothing disappears by never having been entered.

2. "Not applicable" (cyan) is never generated. It is a human judgement that a
   check does not apply to a floor, and the database cannot know that. Set them
   by hand: type "none" in the cell and fill it cyan.

3. The Total / Done / Pending columns are Excel FORMULAS, not baked numbers.
   Marking a cell "none" by hand drops it out of Total straight away, so the
   percentages stay honest after you annotate the sheet. A regenerate
   overwrites hand edits, so keep the annotated copy under another filename.

    python export_matrix.py --project 24139
    python export_matrix.py --project 24139 --all-team
"""
import argparse
import datetime as dt
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

FILL_DONE, FILL_NOTDONE, FILL_WIP, FILL_NA = "C6E0B4", "FF8585", "FFFFFF", "D9E1F2"
# The database also has On Hold, which the spec did not assign a colour. It is
# the most actionable status on the sheet, so it gets its own rather than being
# folded into WIP. Change this to FILL_WIP to collapse them.
FILL_HOLD = "FFD966"

LBL_DONE, LBL_NOT, LBL_WIP, LBL_HOLD, LBL_NA = "Y", "X", "WIP", "HOLD", "none"

STATUS_STYLE = {
    "Completed":   (FILL_DONE,    LBL_DONE),
    "Not Started": (FILL_NOTDONE, LBL_NOT),
    "Ongoing":     (FILL_WIP,     LBL_WIP),
    "On Hold":     (FILL_HOLD,    LBL_HOLD),
}
MISSING_STYLE = (FILL_NOTDONE, LBL_NOT)   # never entered -> Not done, never cyan

HDR_CAT, HDR_TASK, HDR_INFO, HDR_TITLE, HDR_SUM = "FFF2CC", "D9E1F2", "E2EFDA", "1F4E79", "FCE4D6"
INFO_COLS = ["Name", "Emp ID", "Role", "Model", "Floor", "Zone"]
SUM_COLS = ["Total", "Done", "Pending", "% Done"]


def fetch(con, project, all_team):
    """Columns from the catalog, rows from what has been logged."""
    cols = con.execute("""
        SELECT cat.name AS category, ts.name AS task
          FROM task_sub_tasks ts
          JOIN categories cat ON cat.id = ts.category_id
         ORDER BY cat.name, ts.name""").fetchall()

    rows = con.execute("""
        SELECT DISTINCT person, emp_code, role, model, level, zone
          FROM v_report_flat
         WHERE project_code = ?
         ORDER BY person, level, zone""", (project,)).fetchall()
    rows = [dict(r) for r in rows]

    if all_team:
        # Everyone on the project against every project level, so people who
        # have not logged yet still appear instead of silently missing.
        have = {(r["person"], r["level"], r["zone"]) for r in rows}
        for s in con.execute("""
            SELECT pe.name AS person, pe.emp_code AS emp_code,
                   r.name AS role, l.label AS level
              FROM project_people pp
              JOIN people pe ON pe.id = pp.person_id
              LEFT JOIN roles r ON r.id = pe.role_id
              JOIN project_levels pl ON pl.project_code = pp.project_code
              JOIN levels l ON l.id = pl.level_id
             WHERE pp.project_code = ? AND pp.left_on IS NULL
             ORDER BY pe.name, l.number""", (project,)):
            if (s["person"], s["level"], None) not in have:
                rows.append({"person": s["person"], "emp_code": s["emp_code"],
                             "role": s["role"], "model": None,
                             "level": s["level"], "zone": None})
        rows.sort(key=lambda r: (r["person"] or "", r["level"] or "", r["zone"] or ""))

    # Latest entry wins for each person / floor / zone / task.
    marks = {}
    for r in con.execute("""
        SELECT person, level, zone, sub_task, status
          FROM v_report_flat
         WHERE project_code = ? AND sub_task IS NOT NULL
         ORDER BY work_date""", (project,)):
        marks[(r["person"], r["level"], r["zone"], r["sub_task"])] = r["status"]
    return cols, rows, marks


def build(con, project, out, all_team):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    cols, rows, marks = fetch(con, project, all_team)
    if not cols:
        sys.exit("No task names in the catalog - nothing to build columns from.")

    pname = con.execute("SELECT name FROM projects WHERE code=?", (project,)).fetchone()
    title = f"{pname['name'] if pname else project} Task Tracker"

    wb = Workbook()
    ws = wb.active
    ws.title = "Status Matrix"

    thin = Side(style="thin", color="B0B0B0")
    box = Border(left=thin, right=thin, top=thin, bottom=thin)
    n_info, n_sum, n_task = len(INFO_COLS), len(SUM_COLS), len(cols)
    first_task = n_info + n_sum + 1
    last = n_info + n_sum + n_task

    ws.cell(row=1, column=1, value=title)
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=last)
    t = ws.cell(row=1, column=1)
    t.font = Font(bold=True, size=15, color="FFFFFF")
    t.fill = PatternFill("solid", fgColor=HDR_TITLE)
    t.alignment = Alignment(horizontal="left", vertical="center", indent=1)
    ws.row_dimensions[1].height = 26

    sub = ws.cell(row=2, column=1,
                  value=f"Project {project}   |   generated {dt.datetime.now():%Y-%m-%d %H:%M}"
                        "   |   tasks never entered default to Not done (red)")
    sub.font = Font(size=9, italic=True, color="666666")
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=last)

    HR_CAT, HR_TASK, R0 = 3, 4, 5

    for i, label in enumerate(INFO_COLS, start=1):
        ws.merge_cells(start_row=HR_CAT, start_column=i, end_row=HR_TASK, end_column=i)
        c = ws.cell(row=HR_CAT, column=i, value=label)
        c.font = Font(bold=True, size=10)
        c.fill = PatternFill("solid", fgColor=HDR_INFO)
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    for i, label in enumerate(SUM_COLS, start=n_info + 1):
        ws.merge_cells(start_row=HR_CAT, start_column=i, end_row=HR_TASK, end_column=i)
        c = ws.cell(row=HR_CAT, column=i, value=label)
        c.font = Font(bold=True, size=10)
        c.fill = PatternFill("solid", fgColor=HDR_SUM)
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    col = first_task
    start = col
    for idx, rec in enumerate(cols):
        is_last = idx == len(cols) - 1
        nxt = None if is_last else cols[idx + 1]["category"]
        if is_last or nxt != rec["category"]:
            if col > start:
                ws.merge_cells(start_row=HR_CAT, start_column=start,
                               end_row=HR_CAT, end_column=col)
            c = ws.cell(row=HR_CAT, column=start, value=rec["category"])
            c.font = Font(bold=True, size=10)
            c.fill = PatternFill("solid", fgColor=HDR_CAT)
            c.alignment = Alignment(horizontal="center", vertical="center")
            start = col + 1
        col += 1

    for i, rec in enumerate(cols):
        c = ws.cell(row=HR_TASK, column=first_task + i, value=rec["task"])
        c.font = Font(bold=True, size=8)
        c.fill = PatternFill("solid", fgColor=HDR_TASK)
        c.alignment = Alignment(textRotation=90, horizontal="center",
                                vertical="bottom", wrap_text=True)
    ws.row_dimensions[HR_TASK].height = 155

    for r in (HR_CAT, HR_TASK):
        for i in range(1, last + 1):
            ws.cell(row=r, column=i).border = box

    prev = None
    for ri, row in enumerate(rows):
        er = R0 + ri
        same = row["person"] == prev
        vals = ["" if same else row["person"],
                "" if same else (row["emp_code"] or ""),
                "" if same else (row["role"] or ""),
                row["model"] or "", row["level"] or "", row["zone"] or ""]
        prev = row["person"]
        for i, v in enumerate(vals, start=1):
            c = ws.cell(row=er, column=i, value=v)
            c.font = Font(size=9, bold=(i == 1 and not same))
            c.border = box
            c.alignment = Alignment(horizontal="left", vertical="center")

        for i, rec in enumerate(cols):
            key = (row["person"], row["level"], row["zone"], rec["task"])
            fill, label = STATUS_STYLE.get(marks[key], MISSING_STYLE) \
                if key in marks else MISSING_STYLE
            c = ws.cell(row=er, column=first_task + i, value=label)
            c.fill = PatternFill("solid", fgColor=fill)
            c.font = Font(size=8, color="444444")
            c.border = box
            c.alignment = Alignment(horizontal="center", vertical="center")

        # Live formulas: hand-marking a cell "none" drops it out of Total, so
        # the percentage stays honest after the sheet is annotated.
        rng = (f"{get_column_letter(first_task)}{er}:"
               f"{get_column_letter(last)}{er}")
        done = f'COUNTIF({rng},"{LBL_DONE}")'
        pend = (f'COUNTIF({rng},"{LBL_NOT}")+COUNTIF({rng},"{LBL_WIP}")'
                f'+COUNTIF({rng},"{LBL_HOLD}")')
        for off, formula in enumerate([f"={done}+{pend}", f"={done}", f"={pend}"]):
            c = ws.cell(row=er, column=n_info + 1 + off, value=formula)
            c.font = Font(size=9)
            c.border = box
            c.alignment = Alignment(horizontal="center", vertical="center")
        tot_ref = f"{get_column_letter(n_info + 1)}{er}"
        done_ref = f"{get_column_letter(n_info + 2)}{er}"
        p = ws.cell(row=er, column=n_info + 4,
                    value=f'=IF({tot_ref}=0,"",{done_ref}/{tot_ref})')
        p.number_format = "0%"
        p.font = Font(size=9, bold=True)
        p.border = box
        p.alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[er].height = 16

    # Project totals
    if rows:
        tr = R0 + len(rows)
        ws.cell(row=tr, column=1, value="PROJECT TOTAL").font = Font(bold=True, size=10)
        for i in range(1, n_info + 1):
            ws.cell(row=tr, column=i).fill = PatternFill("solid", fgColor=HDR_SUM)
            ws.cell(row=tr, column=i).border = box
        for off in range(3):
            cl = get_column_letter(n_info + 1 + off)
            c = ws.cell(row=tr, column=n_info + 1 + off,
                        value=f"=SUM({cl}{R0}:{cl}{tr - 1})")
            c.font = Font(bold=True, size=10)
            c.fill = PatternFill("solid", fgColor=HDR_SUM)
            c.border = box
            c.alignment = Alignment(horizontal="center", vertical="center")
        tot_ref = f"{get_column_letter(n_info + 1)}{tr}"
        done_ref = f"{get_column_letter(n_info + 2)}{tr}"
        c = ws.cell(row=tr, column=n_info + 4,
                    value=f'=IF({tot_ref}=0,"",{done_ref}/{tot_ref})')
        c.number_format = "0%"
        c.font = Font(bold=True, size=10)
        c.fill = PatternFill("solid", fgColor=HDR_SUM)
        c.border = box
        c.alignment = Alignment(horizontal="center", vertical="center")

    for i, w in enumerate([22, 9, 13, 16, 8, 8, 7, 7, 8, 8], start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for i in range(first_task, last + 1):
        ws.column_dimensions[get_column_letter(i)].width = 4.6

    ws.freeze_panes = ws.cell(row=R0, column=first_task)

    lg = R0 + len(rows) + 3
    ws.cell(row=lg, column=1, value="LEGEND").font = Font(bold=True, size=10)
    for i, (label, fill) in enumerate([
            (f'Done  ("{LBL_DONE}")', FILL_DONE),
            (f'Not done  ("{LBL_NOT}")', FILL_NOTDONE),
            (f'WIP / ongoing  ("{LBL_WIP}")', FILL_WIP),
            (f'On hold  ("{LBL_HOLD}")', FILL_HOLD),
            (f'Not applicable  ("{LBL_NA}") - set by hand', FILL_NA)]):
        r = lg + 1 + i
        sw = ws.cell(row=r, column=1, value="")
        sw.fill = PatternFill("solid", fgColor=fill)
        sw.border = box
        ws.cell(row=r, column=2, value=label).font = Font(size=9)
    note = ws.cell(row=lg + 7, column=1,
                   value=f'Cyan is never generated. Where a check does not apply, type '
                         f'"{LBL_NA}" and fill it cyan - Total and % Done update themselves.')
    note.font = Font(size=8, italic=True, color="666666")

    wb.save(out)
    return len(rows), len(cols)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(HERE, "tasklog.db"))
    ap.add_argument("--project", required=True)
    ap.add_argument("--out")
    ap.add_argument("--all-team", action="store_true",
                    help="also add rows for team members who have not logged yet")
    args = ap.parse_args()

    out = args.out or os.path.join(HERE, f"matrix_{args.project}_{dt.date.today():%Y%m%d}.xlsx")
    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    try:
        with open(os.path.join(HERE, "views.sql"), encoding="utf-8") as f:
            con.executescript(f.read())
    except sqlite3.Error:
        pass
    nr, nc = build(con, args.project, out, args.all_team)
    con.close()
    print(f"{nr} rows x {nc} task columns -> {out}")


if __name__ == "__main__":
    main()
