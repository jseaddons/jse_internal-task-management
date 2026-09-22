#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Add a new sub-project to the task-log database as PURE DATA (no schema change).

A new project brings its own:
  * working-time config (day_start / day_end / hours_per_day)
  * break windows
  * buildings, models
  * master tasks, grouped by sheet type
Shared reference tables (levels, task_names, people, statuses, sheet_types) are
NOT per-project; any sheet type named here that doesn't exist yet is created.

Usage:
    python add_project.py --template new_project.json     # write a starter file
    python add_project.py new_project.json                # apply it (idempotent)
    python add_project.py new_project.json --db tasklog.db

The JSON shape (see --template):
{
  "code": "6.1",
  "name": "Abu Dhabi - Tower 6.1 M45",
  "client_code": "M45",
  "internal_code": "2630",
  "config":   {"day_start": "09:00", "day_end": "18:30", "hours_per_day": 8.25},
  "breaks":   [["Lunch","12:45","13:30"], ["AM","11:15","11:30"], ["PM","16:15","16:30"]],
  "buildings": ["Block A", "Block B", "All Blocks"],
  "models":    ["ADC_M45_30_..._01", "ADC_M45_30_..._02"],
  "master_tasks": {
      "General":        ["DD_ADC_00_GI_H6.1_EX_XX_01 - Explanatory Note"],
      "Functional Plan":["DD_ADC_02_AR_H6.1_FS_L01_101 - Functional Scheme Plans"]
  }
}

Equivalent manual SQL is documented in README.md.
"""
import sys, os, json, sqlite3, argparse

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))

TEMPLATE = {
    "code": "6.1",
    "name": "Abu Dhabi - Tower 6.1 M45",
    "client_code": "M45",
    "internal_code": "2630",
    "config": {"day_start": "09:00", "day_end": "18:30", "hours_per_day": 8.25},
    "breaks": [["Lunch", "12:45", "13:30"], ["AM", "11:15", "11:30"], ["PM", "16:15", "16:30"]],
    "buildings": ["Block A", "Block B", "All Blocks"],
    "models": ["ADC_M45_30_H61_15-AP_01", "ADC_M45_30_H61_15-AP_02"],
    "master_tasks": {
        "General": ["DD_ADC_00_GI_H6.1_EX_XX_01 - Explanatory Note"],
        "Functional Plan": ["DD_ADC_02_AR_H6.1_FS_L01_101 - Functional Scheme Plans"],
    },
}


def add_project(db_path, spec):
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys = ON")
    cur = con.cursor()
    added = []

    code = str(spec["code"]).strip()
    cfg = spec.get("config", {})
    cur.execute(
        """INSERT INTO projects(code,name,client_code,internal_code,day_start,day_end,hours_per_day)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(code) DO UPDATE SET
             name=excluded.name, client_code=excluded.client_code,
             internal_code=excluded.internal_code, day_start=excluded.day_start,
             day_end=excluded.day_end, hours_per_day=excluded.hours_per_day""",
        (code, spec["name"], spec.get("client_code"), spec.get("internal_code"),
         cfg.get("day_start", "09:00"), cfg.get("day_end", "18:30"),
         cfg.get("hours_per_day", 8.25)))
    added.append(f"project {code}")

    for (bn, bs, be) in spec.get("breaks", []):
        cur.execute("""INSERT INTO break_windows(project_code,name,start_time,end_time)
                       VALUES (?,?,?,?)
                       ON CONFLICT(project_code,name) DO UPDATE SET
                         start_time=excluded.start_time, end_time=excluded.end_time""",
                    (code, bn, bs, be))

    for b in spec.get("buildings", []):
        is_all = 1 if str(b).lower().startswith("all") else 0
        cur.execute("INSERT OR IGNORE INTO buildings(project_code,name,is_all) VALUES (?,?,?)",
                    (code, b, is_all))
    added.append(f"{len(spec.get('buildings', []))} buildings")

    for m in spec.get("models", []):
        cur.execute("INSERT OR IGNORE INTO models(project_code,name) VALUES (?,?)", (code, m))
    added.append(f"{len(spec.get('models', []))} models")

    mt_total = 0
    for sheet_type, names in spec.get("master_tasks", {}).items():
        row = cur.execute("SELECT id FROM sheet_types WHERE name=?", (sheet_type,)).fetchone()
        if row:
            stid = row[0]
        else:
            cur.execute("INSERT INTO sheet_types(name) VALUES (?)", (sheet_type,))
            stid = cur.lastrowid
            added.append(f"sheet_type {sheet_type!r} (new shared)")
        for nm in names:
            cur.execute("INSERT OR IGNORE INTO master_tasks(project_code,sheet_type_id,name) "
                        "VALUES (?,?,?)", (code, stid, nm))
            mt_total += 1
    added.append(f"{mt_total} master tasks")

    con.commit()
    con.close()
    return added


def main():
    ap = argparse.ArgumentParser(description="Add a project to the task-log DB (data only).")
    ap.add_argument("spec", nargs="?", help="JSON file describing the project")
    ap.add_argument("--db", default=os.path.join(HERE, "tasklog.db"))
    ap.add_argument("--template", metavar="FILE", help="write a starter JSON and exit")
    args = ap.parse_args()

    if args.template:
        with open(args.template, "w", encoding="utf-8") as f:
            json.dump(TEMPLATE, f, indent=2, ensure_ascii=False)
        print(f"Template written -> {args.template}")
        return

    if not args.spec:
        ap.error("give a JSON spec file, or use --template FILE to create one")
    if not os.path.exists(args.db):
        sys.exit(f"Database not found: {args.db} (run import_xlsx.py first)")
    with open(args.spec, encoding="utf-8") as f:
        spec = json.load(f)

    added = add_project(args.db, spec)
    print(f"Added/updated project {spec['code']!r} in {args.db}:")
    for a in added:
        print("  +", a)
    print("Done. New project is now available to all dropdowns/reports as data.")


if __name__ == "__main__":
    main()
