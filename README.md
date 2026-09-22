# Master Task Log → SQLite

Normalized SQLite version of the `2630_M45_Master Task Log.xlsx` daily task log.
Excel becomes a **viewing/printing front-end**; the database is the source of truth,
and adding a new sub-project is a **data change, not a schema change**.

## Files
| File | Purpose |
|---|---|
| `schema.sql` | Normalized tables + foreign keys |
| `import_xlsx.py` | Load the workbook → `tasklog.db`; compute & reconcile hours/man-days |
| `views.sql` | The four report views + permission + overlap-validation views |
| `add_project.py` | Add a sub-project from a JSON file (bulk alternative to the admin page) |
| `sync_lists.py` | Pull new list items from the Excel *Lists* sheet into the DB (additive, tasks untouched) |
| `server.py` | **Team task-entry form + Manage Lists admin page** (browser) — writes to the database |
| `run_server.bat` | Double-click launcher for `server.py` |
| `export_reports.py` | Dump the report views to a clean `reports.xlsx` |
| `bulk_import.py` | Manager-only bulk import of catalog lists and model filenames |
| `tasklog.db` | The database (generated) |

Requires Python 3 with `pandas` and `openpyxl`.

---

## Daily task entry — the team's browser form (this replaces typing into Excel)

Your teammates add their daily tasks through a web form; dropdowns come **live from
the database**, so nobody maintains lists in Excel.

**Set up once, on ONE host PC** (e.g. yours — the machine that holds `tasklog.db`):
1. Double-click **`run_server.bat`** (or run `python server.py`). Leave the window open.
2. It prints the live links, e.g.
   ```
   This PC:      http://localhost:8000
   Teammates:    http://ALI-158:8000
                 http://192.168.1.223:8000   (if the name does not work)
   Shortcut:     \\192.168.0.7\Timesheet\JSE Task Log.url
   ```
   The IP is detected at startup, so this stays correct if the host PC's
   address changes. On every start the server also refreshes
   **`JSE Task Log.url`** on the shared drive — teammates can just
   double-click that instead of typing anything.

   > **Teammates must never use `localhost:8000`.** `localhost` always means
   > *the PC you are sitting at*, so it points at their own machine and will
   > never load. Only the host PC can use `localhost`.
3. Teammates open that **Teammates** link and **sign in with their Emp ID**
   (no password). This is now the first page - the form does not open until
   they do, so every entry is attributed. Their name is filled in automatically
   and cannot be swapped for a colleague's; only Managers and Team Leaders can
   log work on someone else's behalf. Each task also stores `entered_by`, the
   person who actually typed it.
4. Then fill the form:
   Date → Project → Sheet Type → Master Task → Building/Level/Model → Task Name →
   In/Out time → Status → Save. Each Save writes one task row, logged against
   the signed-in person.

**Rules that keep it safe:**
- Run the form on **one machine only**, and keep `tasklog.db` on **that machine's
  local disk** (where it is now) — *not* on the network share. SQLite with several
  writers over a share can corrupt. The single server serializes everyone's saves.
- The host PC must stay on while people enter tasks, and allow port 8000 through its
  firewall (Windows will prompt once — click *Allow*).
- **Break** auto-calculates from In/Out; a person can type a **Break override** if the
  auto value is wrong. Man-days are derived later in the reports — not entered.

The old Excel Master Log is no longer the input; use it only if you ever want to bulk-
load historical rows via `import_xlsx.py`.

### Manage Lists (the admin page) — add projects, master tasks, people, etc.
Click **"Manage Lists"** (top of the entry form, or open `/admin`). From there you add,
by typing into the page — no Excel, no files:
- **Projects** (with their own day-end / hours-per-day and standard breaks),
- per-project **Buildings, Models, Master Tasks** (Master Task = pick project + sheet type),
- shared **Sheet Types, People, Task Names, Levels** (no Level 0), **Statuses**.
Everything you add appears in the entry-form dropdowns on the next refresh. Adding is
safe — nothing is ever deleted, so it can't break existing tasks.

### Manager bulk catalog import

Managers can use **Bulk project catalog import** on the Manage Lists page to choose a
project, an Excel workbook, and a model folder through the browser file picker. The
import reads model names from the selected filenames and imports catalog values from
either a simple row-based workbook (`Project`, `Sheet Type`, `Master Task`, `Building`,
`Level`, `Task Name`, `Status`) or the existing `Lists` sheet layout. Model files are
not uploaded. Each import creates a dated database snapshot beside `tasklog.db`.

This control is restricted to the `Manager` role. Team Leaders and other users continue
to add individual task entries through the normal form.

The page also has **Tools** buttons that run the programs for you:
- **Export reports to Excel** → runs `export_reports.py` (writes `reports.xlsx`),
- **Sync lists from Excel workbook** → runs `sync_lists.py` (pulls new list items),
- **Rebuild DB from Excel (danger)** → runs `import_xlsx.py` — asks to confirm first,
  because it wipes tasks entered through the form. Use only for a fresh re-migration.

`add_project.py` (JSON) and `sync_lists.py` remain as command-line / bulk options, but
day to day you'll just use the Manage Lists page.

---

## 1. Import (build the database)
```bash
python import_xlsx.py
# or:  python import_xlsx.py "path/to/workbook.xlsx" tasklog.db
```
This **rebuilds `tasklog.db`** from `schema.sql`, loads `Lists` into the reference
tables and `Master Log` into `tasks` + `task_assignees`, converts Excel dates/times,
**computes** break/hours/man-days per the rules below, and prints:
- reference rows auto-added from the log (and duplicate catalog rows skipped),
- **reconciliation** vs the sheet's own values (every mismatch listed),
- **overlap validation** (same person, overlapping task times in a day).

Then apply the views once:
```bash
python -c "import sqlite3;c=sqlite3.connect('tasklog.db');c.executescript(open('views.sql',encoding='utf-8').read())"
```

## 2. Query
Any SQLite client, or Python. Views do the work; filter with `WHERE`.
```sql
-- Daily report for one day
SELECT * FROM v_daily_report WHERE work_date = '2026-08-04';

-- Man-day summary, filtered, re-aggregated by person
SELECT person, SUM(man_days) AS man_days
FROM v_manday_summary
WHERE project_code = '5.2' AND sheet_type = 'Functional Plan'
GROUP BY person ORDER BY man_days DESC;

-- Search: who did what / where / when
SELECT * FROM v_search
WHERE person = 'Aasin' AND work_date BETWEEN '2026-08-01' AND '2026-08-31';

-- Internal report: effort in a date range
SELECT person, SUM(hours) hrs, SUM(man_days) md
FROM v_internal_report
WHERE work_date BETWEEN '2026-08-01' AND '2026-08-07'
GROUP BY person;

-- Client permission report (who left early)
SELECT * FROM v_permission_report;

-- Data check: overlapping task times per person per day
SELECT * FROM v_assignee_overlaps;
```

## 3. Add a new sub-project (the main goal — data only)
```bash
python add_project.py --template new_project.json   # write a starter file, then edit it
python add_project.py new_project.json              # apply (idempotent)
```
The JSON carries the project's own config, break windows, buildings, models, and
master tasks grouped by sheet type. Shared lists (levels, people, statuses, task
names, sheet types) are reused; any new sheet type named is created.

**Equivalent by hand** (nothing structural changes):
```sql
INSERT INTO projects(code,name,client_code,internal_code,day_start,day_end,hours_per_day)
VALUES ('6.1','Abu Dhabi - Tower 6.1 M45','M45','2630','09:00','18:30',8.25);

INSERT INTO break_windows(project_code,name,start_time,end_time) VALUES
 ('6.1','Lunch','12:45','13:30'),('6.1','AM','11:15','11:30'),('6.1','PM','16:15','16:30');

INSERT INTO buildings(project_code,name,is_all) VALUES
 ('6.1','Block A',0),('6.1','All Blocks',1);

INSERT INTO models(project_code,name) VALUES ('6.1','ADC_M45_30_H61_15-AP_01');

INSERT INTO master_tasks(project_code,sheet_type_id,name)
SELECT '6.1', id, 'DD_ADC_02_AR_H6.1_FS_L01_101 - Functional Scheme Plans'
FROM sheet_types WHERE name='Functional Plan';
```
A different project can have a **different day length or breaks** — that's why config
lives on `projects` / `break_windows`, not in code.

## 4. Export to Excel (for non-technical viewers)
```bash
python export_reports.py                              # all data -> reports.xlsx
python export_reports.py --from 2026-08-01 --to 2026-08-31 --out August.xlsx
```
Produces one styled sheet per report (frozen header, auto-filter, sized columns).

---

## Business rules baked in
- **1 man-day = 8.25 h** (09:00–18:30 minus 75 min breaks); configurable per project.
- **Hours = (Out − In) − break.** Break defaults to the overlap with the standard
  windows (lunch 12:45–13:30, AM 11:15–11:30, PM 16:15–16:30). A **manually typed
  Break is trusted** (users enter it by hand when the sheet's auto-formula didn't
  fire); the overlap is computed only where the formula was used or the cell is blank.
  Time past 18:30 counts as extra hours.
- **Man-days = Hours ÷ hours_per_day per assignee.** Reported man-days are
  **normalized per person-day**: capped at 1.0/person/day and split proportionally
  across that day's tasks (raw hours remain queryable). This removes the over-count
  when one person logs several tasks that each carry the whole day's in/out window.
- **Permission** is a Yes/No **client-report flag only** — it marks an early departure
  (out before 18:30) and never affects hours. Any duration lives in Notes.
- **Group** (`grouped` 0/1) is a Yes/No flag marking rows where one person did several
  varied tasks in a day. It's informational — man-days are normalized per person-day
  regardless, so the flag never changes effort totals.
- **Levels are numeric only, no Level 0** (enforced by CHECK). `All levels` is a
  sentinel row; `All Blocks` is a per-project sentinel building.
- **One task per row**; a person's task times shouldn't overlap in a day
  (`v_assignee_overlaps` lists violations).

## Notes from the migration (verify at source when convenient)
- **All 31 rows reconcile exactly** (break / hours / man-days) — typed breaks trusted,
  formula rows computed. (Row 16's typed Break of 60 is kept; note it's higher than the
  09:00–18:30 overlap would give, so glance at it if it looks off.)
- Several 08-04 rows repeat a full-day in/out window across a person's tasks
  (Harika ×4, Aasin/Ramakrishnan ×3). Normalization keeps their day at 1.0 man-day;
  `v_assignee_overlaps` flags the rows so they can be corrected to partitioned times
  (as 08-03 already is) if you want the raw data clean too.
- The `Quick Add` and `Archive 2026-07-28` tabs are not imported (helper / reference).
```
