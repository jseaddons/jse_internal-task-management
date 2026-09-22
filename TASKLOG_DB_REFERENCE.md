# JSE Task Log — SQLite App Reference

Working reference for the daily team task-log application, reconstructed on
2026-08-27 from the build sessions of **6–14 Aug 2026** plus the live code and database.

- **Current code + database:** `C:\Users\jse2084\Documents\tasklog_db\` *(to be migrated — see §2)*
- **Origin:** migration of `2630_M45_Master Task Log.xlsx` (internal project 2630 / client code M45, sub-projects 5.2 Dubai-Hotel and 5.4 Dubai-Boulevard Apart Hotel)
- **Last active use:** `tasklog.db` written 2026-08-21 15:38, alongside `reports_2026-08-21.xlsx`
- **Stack:** Python 3 stdlib HTTP server + SQLite. `pandas` and `openpyxl` are needed for the import/export scripts only — the server itself is stdlib.

The core idea: **the database is the source of truth**, Excel is only a viewing/printing front end, and adding a sub-project is a data change rather than a schema change.

---

## 1. Quick start

```bat
cd <app folder>
run_server.bat
```

Keep the window open — closing it stops the server. It refuses to start twice on port 8000.

| Who | URL |
|---|---|
| Host PC | `http://localhost:8000` |
| Teammates | `http://<host-pc-name>:8000`, e.g. `http://ALI-158:8000`. The IP is detected at startup and published as `JSE Task Log.url` on the shared drive. Never `localhost` - that is the viewer's own PC. |

**Hard rule:** run the server on **one machine only**, with `tasklog.db` on that machine's **local disk**, never the network share. Multiple SQLite writers over SMB corrupt the file. The single server process serialises everyone's saves. Port 8000 must be open on the host firewall.

---

## 2. Migration plan — move to a new home, under git

**Decision:** the new project folder becomes the **single home** for this app. The current
`C:\Users\jse2084\Documents\tasklog_db\` is renamed, kept until the new home is verified
working, then deleted.

### Why the rename rather than a straight copy

`tasklog.db` is live and the team writes to it. If the server can be launched from two
folders against two databases, entries split across both and merging afterwards is painful.
Renaming the old folder makes the old launcher fail loudly instead of silently accepting
writes into an orphaned database.

### Steps

1. **Back up first.** Dated copy of `tasklog.db` to somewhere outside both folders.
2. **Copy into the new home:**
   - `server.py`, `run_server.bat`
   - `schema.sql`, `views.sql`
   - `import_xlsx.py`, `import_employees.py`, `add_project.py`, `sync_lists.py`, `export_reports.py`
   - `README.md`, this reference doc
   - `tasklog.db` (the live database)
   - the two `tasklog_before_emp_import_*.db` backups → into a `backups/` subfolder
3. **Leave behind** the ~15 accumulated `reports_*.xlsx` and `manday_*.xlsx` exports, plus
   `__pycache__`, `fix.py`, `ALLOW`. Disposable output, not data.
4. **Rename the old folder** to `tasklog_db_OLD_2026-08-27` — do **not** delete yet.
5. **Verify** against the checklist below.
6. **Delete the old folder** only once every check passes.

### Verification checklist — all must pass before deleting the old folder

- [ ] `run_server.bat` starts from the new folder, no port conflict
- [ ] Entry form loads and every dropdown is populated (projects, master tasks, buildings, levels, models, task names, statuses, people)
- [ ] Sign in with a real Emp ID; the header shows the correct name and role
- [ ] Save a new task; it appears in the list with working modify/remove buttons
- [ ] Admin page opens for an admin role and is refused for a non-admin role
- [ ] Daily report for a date with known data matches what the old folder produced
- [ ] Man-day summary totals are unchanged (the 1.0-per-person-per-day cap still applies)
- [ ] `export_reports.py` writes a valid `reports.xlsx`
- [ ] A teammate can reach the app from their own PC over the network
- [ ] Row counts match the pre-move backup: tasks 45, task_assignees 52, master_tasks 298, people 32

### Rollback

Rename `tasklog_db_OLD_2026-08-27` back and restart the server from it. Nothing else to undo,
provided no new entries were made in the new home — which is why the checklist should be
worked through in one sitting rather than left half-done over a working day.

### Git

Initialise the repo in the new folder. **Do not commit the database.** It is binary, changes
on every save, and is the one file that cannot be merged — two people committing it would
produce conflicts with no sane resolution.

Suggested `.gitignore`:

```gitignore
# live database and backups — never commit
*.db
*.db-journal
*.db-wal
backups/

# generated report output
reports*.xlsx
manday_*.xlsx

# python
__pycache__/
*.pyc
```

Committed: `server.py`, the five scripts, `schema.sql`, `views.sql`, `run_server.bat`,
`README.md`, this doc, `.gitignore`.

Because the schema lives in `schema.sql` and `views.sql`, the database stays reproducible
from code plus an import — which is the point of keeping it out of the repo.

---

## 3. File map

| File | Purpose |
|---|---|
| `server.py` (61 KB) | The whole web app — entry form, admin page, reports, sign-in |
| `run_server.bat` | Launcher with a duplicate-instance guard |
| `schema.sql` | Normalised tables + foreign keys |
| `views.sql` | Report views incl. man-day normalisation logic |
| `import_xlsx.py` | Bulk-load the Excel workbook into `tasklog.db`, reconcile hours/man-days |
| `import_employees.py` | Load the team CSV into `people` |
| `add_project.py` | Add a sub-project from JSON (bulk alternative to the admin page) |
| `sync_lists.py` | Pull new list items from the Excel *Lists* sheet (additive; tasks untouched) |
| `export_reports.py` | Dump report views to a clean `reports.xlsx` |
| `bulk_import.py` | Manager-only Excel loaders for models, categories, sub tasks, zones, and levels |
| `migrate_three_tasks.py` | One-off: collapsed the Task level to three fixed names and pushed the review-comment lines down to Sub Task |
| `migrate_multi_select.py` | One-off: added the `task_categories` / `task_sub_task_links` junctions |
| `README.md` | Original build-time notes — still accurate on setup |
| `tasklog.db` | The live database |
| `tasklog_before_emp_import_*.db` | Two pre-import backups from 14 Aug |

---

## 4. Data model

Additional catalog tables and mappings were added during the 2026-08-27 migration.

### Transaction tables

**`tasks`** (45 rows) — one row per logged piece of work.

| Column | Notes |
|---|---|
| `work_date` | TEXT |
| `start_time`, `end_time` | The in/out window |
| `break_mins` | REAL — auto-computed, manually overridable |
| `hours` | REAL — derived from in/out minus break |
| `permission` | INTEGER — **client-report figure only**; does not drive manhours |
| `grouped` | INTEGER — several master tasks done in one day by one person |
| `project_code`, `sheet_type_id`, `master_task_id`, `building_id`, `level_id`, `model_id`, `task_name_id` | Foreign keys |
| `description`, `status_id`, `pct_complete`, `notes` | Free/detail fields |

**`task_assignees`** (52 rows) — composite primary key `(task_id, person_id)`. This is what makes a task many-to-many with people.

**`task_categories`** and **`task_sub_task_links`** — the same pattern for the two
multi-select levels of the entry hierarchy: `(task_id, category_id)` and
`(task_id, task_sub_task_id)`. The single-value `tasks.category_id` and
`tasks.task_sub_task_id` columns are kept in step, holding the first tick, so anything
still reading one value keeps working; reports read the junctions and fall back to the
columns for rows logged before the switch.

### Reference tables

| Table | Rows | Scope |
|---|---|---|
| `master_tasks` | 298 | **Per project** (FK `project_code`) + `sheet_type_id` |
| `people` | 32 | `name`, `role_id`, `emp_code`, `category`, `skillset`, `bim_id`, `ip_address`, `email`, `contact` |
| `roles` | 9 | Manager, Team Leader, Coordinator, Modeller, … |
| `projects` | 3 | PK is `code`; carries working-time config plus `project_status`, `no_sheet_type`, and `no_zone` |
| `buildings` | 11 | Per project, has `is_all` flag |
| `models` | 18 | Per project |
| `break_windows` | 6 | Per project — standard breaks |
| `levels` | shared catalog | has `is_all` flag (no Level 0); visibility is restricted by `project_levels` |
| `sheet_types` | 11 | Shared |
| `task_names` | shared catalog | Holds history, but only the three core names are offered for entry; mapped to Master Tasks through `master_task_tasks` |
| `categories` | shared catalog | Categories such as Floor, Wall, Ceiling, Bathroom, Door, and Facade |
| `category_tasks` | mapping | Maps `Master Task -> Category -> Task`; every Category carries the same three Tasks, so this is really what tells you which Categories a Master Task has |
| `task_sub_tasks` | 47 | `Category + Task -> Sub Task`. `category_id` is nullable |
| `project_levels` | mapping | Maps project-specific levels to the shared level catalog |
| `statuses` | 4 | Shared |

### Relationships (settled 7 Aug)

- project → master task: **one-to-many, not shared between projects** — master tasks are project-specific
- project ↔ person: **many-to-many** — a person works across several projects
- task ↔ person: **many-to-many** via `task_assignees`
- Project-scoped: `buildings`, `models`, `master_tasks`, `break_windows`
- Project-level mappings: `project_levels`, `category_tasks`
- Global catalogs: `sheet_types`, `statuses`, `people`, `categories`, `task_names`

### Current catalog hierarchy

Daily Task Entry uses this order:

```text
Master Task -> Category (multi) -> Task (3 fixed) -> Sub Task (multi)
```

**The Task level is a fixed three-item list** — `REVIEW COMMENTS`, `CHECK MODEL HEALTH`,
`CHECK UPDATES` — offered under every Category. Anything more specific than those three
is a Sub Task. This replaced the earlier arrangement where each review-comment line was
its own Task name.

**Category and Sub Task are multi-select checkboxes.** A person routinely works across
several Categories in a day and ticks off several Sub Tasks, so both are many-per-task
via junction tables, the same way assignees already were. Ticking FLOOR, CEILING and WALL
shows all three Categories' Sub Tasks in one panel, grouped under labelled headings;
unticking a Category removes only its own items and leaves other ticks alone.

For project `24139`, the workbook `24139_EXCEL LOADING SHEETS.xlsx` was analyzed and
loaded. Its `Review comment` sheet uses underlined rows as Categories and numbered rows
below each underline as **Sub Tasks**. It contains 10 Categories and 47 Sub Tasks. The
`LEVEL` and `Zone` sheets supply the project-specific Level and Zone dropdowns.

The `QC REVIEW` Master Task carries those Categories. Selecting a Master Task fills the
Category checkboxes; ticking Categories fills the Task list (always the same three); the
chosen Task plus the ticked Categories fill the Sub Task checkboxes.

Sub Tasks are scoped to a Category as well as a Task (`task_sub_tasks.category_id`) —
with only three Task names, the Category is what makes one Sub Task list differ from the
next. `category_id` is nullable; a null Sub Task shows under its Task for any Category.

---

## 5. Reporting

| View | Purpose |
|---|---|
| `v_task_effort` | Base view — one row per (task, assignee), fully described, raw + normalised man-days. Everything else builds on it. `category` and `sub_task` come back as comma-joined lists, since both are multi-select |
| `v_daily_report` | One date to that day's tasks per person. Mirrors the Excel daily report tab |
| `v_manday_summary` | Man-day rollup |
| `v_search` | Ad-hoc filtered query |
| `v_internal_report` | Internal-format report |
| `v_permission_report` | Permission hours for the client |
| `v_employees` | `people` joined to role |
| `v_assignee_overlaps` | **Validation** — flags overlapping time windows for the same person |

### Man-day normalisation — read this before changing reports

Raw effort is stored as entered, but reported man-days are **capped at 1.0 per person per day** and split proportionally across that day's tasks:

```
raw_manday  = task hours / project.hours_per_day        (per assignee)
norm_manday = raw_manday / MAX(1.0, SUM(raw_manday) over that person-day)
```

A properly partitioned day — tasks that abut and don't overlap — sums to 1.0 or less and passes through unchanged. A day where someone logged the same full in/out window against several tasks gets scaled down to sum to 1.0. This exists specifically to stop the over-count.

---

## 6. Web app

### Routes

| Route | Purpose |
|---|---|
| `/` | Daily task entry form — **sign-in required** |
| `/admin` | Manage Lists — role-gated |
| `/reports` | Report UI with separate filter sections — **sign-in required** |
| `/run` | Report execution |
| `/download` | Export |
| `/signin`, `/signout` | Identity |
| `/delete` | Row removal |

### Sign-in — how it landed

People sign in by **Emp ID, no password**. Identity is remembered in a `uid` cookie and works from any PC. An Emp ID absent from `people` is refused with *"check with your manager"*.

This was reached by elimination over 10–14 Aug:

1. **IP address** — rejected, DHCP reassigns them
2. **PC hostname suffixed with emp id** — rejected, people don't always sit at their own machine
3. **Company portal `jseeng.in`** — considered, username only, no password check
4. **Emp ID + cookie** — what shipped

The `people.ip_address` column is a leftover from step 1. It is deliberately **not** mapped to the task list.

**Where sign-in applies *(changed 2026-08-28)*.** It used to guard `/admin` only, so `/` and `/reports` were open to anyone on the LAN — and an unsigned visitor could save a task with **no assignee at all**, producing a row owned by nobody. Sign-in is now the front door for `/`, `/reports` and the `/add` POST. The POST is checked independently of the GET so a tab left open overnight cannot save anonymously.

**Who typed it.** `task_assignees` answers *whose work is this*; `tasks.entered_by` answers *who sat at the keyboard*. They match for ordinary members — a signed-in non-admin is forced to their own name and cannot log against a colleague — and diverge only when a Manager or Team Leader enters work on someone else's behalf, which is the case an audit cares about. Set at insert only, never on edit, so it records the original author rather than the last editor. Rows predating 2026-08-28 are NULL; `migrate_entered_by.py` deliberately does not guess them from the assignee.

**This is attribution, not security.** There is no password, and the cookie holds a bare `people.id`, so anyone who can reach the server can set `uid` to any value by hand. It answers *"who logged this?"* under normal use; it will not withstand someone deliberately impersonating a colleague.

### Admin gating

```python
ADMIN_ROLES = ("Manager", "Team Leader", "Team leader",
               "Assistant team leader", "Coordinator")
```

Role is the gateway — it's already fixed per person, so there's no separate permission system. Non-admins get a denied page; signed-out users are redirected to sign-in.

### Entry-form behaviour worth preserving

- Dropdowns come **live from the database** — nobody maintains lists in Excel
- Break auto-calculates from in/out, with a manual override
- The form **pre-fills from the user's last save** so they only change a few fields
- Every row has **modify** and **remove** buttons
- Adding list items is safe — nothing is ever deleted, so existing tasks can't break
- Categories and Sub Tasks can be loaded from the Manager upload in Manage Lists. Underlined
   rows in `Review comment` become Categories; numbered rows beneath them become Sub Tasks
   under `REVIEW COMMENTS`. The importer never invents a new Task name.
- Categories and Sub Tasks are tick-boxes, not dropdowns — tick every Category worked on
   and every Sub Task done. The Sub Task panel is the union across the ticked Categories,
   grouped under a heading per Category.
- A workbook with `LEVEL`, `Zone`, and `Review comment` sheets can be reused in the
   corresponding upload controls. Levels are mapped to the selected project.
- Managers can set a project to `Completed`, `Sheet Type = None`, or `Zone = None`.
   Completed projects remain in the database and reports but are hidden from Daily Task
   Entry; the two None settings grey out their respective entry controls.
- Employee deletion is Manager-only and refuses to remove employees linked to task history.

---

## 7. Open items / candidates for improvement

From the build sessions and from reading the current code:

- **`ip_address` on `people` is dead weight.** Superseded by Emp ID sign-in. Drop the column or repurpose it.
- **Cookie identity is unverified.** Anyone who knows an Emp ID can sign in as that person. Fine inside the office; revisit if the data ever feeds payroll or client billing.
- ~~**Hard-coded IP in `run_server.bat`**~~ *(fixed 2026-08-28)* `server.py` now detects the LAN address at startup via `lan_ip()` and publishes `JSE Task Log.url` to the shared drive on every start, so a new DHCP lease repairs itself. The helper scripts read the address from `tasklog_address.txt` on the share instead of hard-coding it.
- **`levels` is global while `buildings` is per-project.** Cascading building/level to master-task filtering was fixed during the build — worth a regression check if the schema changes.
- **Only 2 projects and 45 tasks.** `import_xlsx.py` exists for bulk history, so the historical Excel rows may not all be loaded. Verify before trusting long-range reports.
- **`v_assignee_overlaps` exists but nothing appears to act on it.** Could surface as a warning in the entry form rather than a report someone has to remember to run.
- **No automated backup.** The only backups are the two manual `tasklog_before_emp_import_*.db` snapshots. A dated copy on each server start would be cheap insurance.
- **Single point of failure** — one host PC, one local file. Acceptable for a small team; document who the host is so nobody wonders why the page is down.

---

## 8. Housekeeping

- Back up `tasklog.db` before running any import script. `import_employees.py` already does; the others may not.
- `sync_lists.py` is additive and leaves tasks alone — safe to re-run.
- Report exports are timestamped into the same folder and are accumulating. They are disposable output, not data.

---

## 9. Provenance

Rebuilt from the Claude Code transcript at
`C:\Users\jse2084\.claude\projects\c--Jse-Developments-Jse-T1\de5ea062-c1e2-47b1-b612-02192e0e1873.jsonl`
(task-log work spans 2026-08-06 to 2026-08-14, roughly 62 messages within a longer session),
cross-checked against the live `server.py`, `views.sql` and `tasklog.db`.

Note: a Cursor agent also worked on this on 10 Aug. Any changes made there are not in the transcript, so this document reflects the code as it stands rather than a complete change history.
