"""Write the flat task table as CSV for Power BI.

Why a file and not a live ODBC connection: Power BI has no native SQLite
connector, so a live link needs a third-party ODBC driver and a DSN configured
on the machine. It also cannot DirectQuery SQLite, so even then every refresh
is a manual import. Given the refresh is manual either way, a CSV removes the
driver, the DSN, and their failure modes for exactly nothing lost.

Power BI reads CSV natively: Get data -> Text/CSV -> pick this file. Refresh
is then: run this script, click Refresh in Power BI.

Only Active projects are exported. Closed work is dead weight in a report about
current progress, and the two finished Dubai projects alone outnumber the live
one three to one.

    python export_powerbi.py                     # every Active project
    python export_powerbi.py --project 24139     # just that one
    python export_powerbi.py --all-projects      # include Completed too
"""
import argparse
import csv
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(HERE, "tasklog.db"))
    ap.add_argument("--out", default=os.path.join(HERE, "powerbi", "tasklog_flat.csv"))
    ap.add_argument("--project", help="one project code; default is every Active project")
    ap.add_argument("--all-projects", action="store_true", help="include Completed projects")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        sys.exit(f"Database not found: {args.db}")

    con = sqlite3.connect(args.db)
    # Rebuild the views so the export always matches views.sql, not whatever
    # shape the database happened to be left in.
    try:
        with open(os.path.join(HERE, "views.sql"), encoding="utf-8") as f:
            con.executescript(f.read())
    except sqlite3.Error:
        pass    # views already present is fine

    # One filter, reused by every query below. The column is qualified per query
    # because some of them join more than one table carrying a project_code.
    if args.project:
        where, pargs = "{p}project_code = ?", (args.project,)
        scope = args.project
    elif args.all_projects:
        where, pargs = "1=1", ()
        scope = "all projects (Active + Completed)"
    else:
        where = "{p}project_code IN (SELECT code FROM projects WHERE project_status = 'Active')"
        pargs = ()
        scope = "Active projects"

    def scoped(prefix=""):
        return where.replace("{p}", prefix)

    outdir = os.path.dirname(args.out)
    os.makedirs(outdir, exist_ok=True)

    def dump(sql, params, path):
        """utf-8-sig: Power BI and Excel both misread plain UTF-8 without the
        BOM, which mangles the names carrying accents (FACADE)."""
        cur = con.execute(sql, params)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(cols)
            w.writerows(rows)
        return len(rows)

    # 1) Facts - what was actually logged.
    # zone_level: Power BI relationships are single-column, so the grid and the
    # facts need one key rather than matching on zone AND level.
    n_fact = dump(f"""
        SELECT *, COALESCE(zone,'') || ' | ' || COALESCE(level,'') AS zone_level
          FROM v_report_flat
         WHERE {scoped()}
         ORDER BY work_date, person, task_id""", pargs, args.out)

    # 2) The task catalog. Without this Power BI only knows about tasks somebody
    #    logged, so a check nobody has touched would silently vanish instead of
    #    showing as outstanding. This is the table that makes "pending against
    #    total" mean total, and it drives the red default in the matrix.
    n_cat = dump("""
        SELECT cat.name AS category, ts.name AS sub_task
          FROM task_sub_tasks ts
          JOIN categories cat ON cat.id = ts.category_id
         ORDER BY cat.name, ts.name""", (),
        os.path.join(outdir, "tasklog_catalog.csv"))

    # 3) Zones and levels per project, so the grid has rows before anyone logs.
    n_grid = dump(f"""
        SELECT DISTINCT pl.project_code, b.name AS zone, l.label AS level, l.number AS level_number,
               b.name || ' | ' || l.label AS zone_level
          FROM project_levels pl
          JOIN levels l ON l.id = pl.level_id
          JOIN tasks t ON t.project_code = pl.project_code
          JOIN buildings b ON b.id = t.building_id
         WHERE b.name <> '' AND {scoped('pl.')}
         ORDER BY pl.project_code, b.name, l.number""", pargs,
        os.path.join(outdir, "tasklog_grid.csv"))

    # 4) The matrix, pre-built. Every zone/level crossed with every catalog task,
    #    status resolved, and the mark and colour already worked out. Power BI
    #    needs no measures, no relationships and no "show items with no data"
    #    for this one - drop the fields on a Matrix and colour by [colour].
    n_mx = dump(f"""
        WITH grid AS (
            SELECT DISTINCT pl.project_code, b.name AS zone,
                   l.label AS level, l.number AS level_number
              FROM project_levels pl
              JOIN levels l ON l.id = pl.level_id
              JOIN tasks t ON t.project_code = pl.project_code
              JOIN buildings b ON b.id = t.building_id
             WHERE b.name <> '' AND {scoped('pl.')}
        ),
        catalog AS (
            SELECT cat.name AS category, ts.name AS sub_task
              FROM task_sub_tasks ts
              JOIN categories cat ON cat.id = ts.category_id
        ),
        latest AS (
            SELECT project_code, zone, level, sub_task, status, person, role, work_date,
                   ROW_NUMBER() OVER (PARTITION BY project_code, zone, level, sub_task
                                      ORDER BY work_date DESC, task_id DESC) AS rn
              FROM v_report_flat
             WHERE sub_task IS NOT NULL
        )
        SELECT g.project_code, g.zone, g.level, g.level_number,
               c.category, c.sub_task,
               COALESCE(x.status, 'Not Started') AS status,
               CASE COALESCE(x.status, 'Not Started')
                    WHEN 'Completed' THEN 'Y'
                    WHEN 'Ongoing'   THEN 'WIP'
                    WHEN 'On Hold'   THEN 'HOLD'
                    ELSE 'X' END AS mark,
               CASE COALESCE(x.status, 'Not Started')
                    WHEN 'Completed' THEN '#C6E0B4'
                    WHEN 'Ongoing'   THEN '#FFFFFF'
                    WHEN 'On Hold'   THEN '#FFD966'
                    ELSE '#FF8585' END AS colour,
               CASE WHEN COALESCE(x.status,'Not Started') = 'Completed' THEN 1 ELSE 0 END AS is_done,
               x.person, x.role, x.work_date AS last_date
          FROM grid g
          CROSS JOIN catalog c
          LEFT JOIN latest x
                 ON x.project_code = g.project_code AND x.zone = g.zone
                AND x.level = g.level AND x.sub_task = c.sub_task AND x.rn = 1
         ORDER BY g.project_code, g.zone, g.level_number, c.category, c.sub_task""", pargs,
        os.path.join(outdir, "tasklog_matrix.csv"))

    con.close()
    print(f"  matrix   {n_mx:>5} rows -> tasklog_matrix.csv   <- use this one")
    print(f"  facts    {n_fact:>5} rows -> {os.path.basename(args.out)}")
    print(f"  catalog  {n_cat:>5} rows -> tasklog_catalog.csv")
    print(f"  grid     {n_grid:>5} rows -> tasklog_grid.csv")
    print("")
    print(f"Folder: {outdir}")
    print(f"Scope:  {scope}")
    print("In Power BI: Get data -> Text/CSV -> tasklog_matrix.csv. Then just Refresh.")


if __name__ == "__main__":
    main()
