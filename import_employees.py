#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""
Import the Architecture Team CSV into the employee table (people), deterministically.

The 6 people who already have tasks are matched by an explicit map (so their id — and
therefore their task links — is preserved) and UPDATED. Every other CSV row is INSERTED.
Original placeholder people that have no tasks and weren't matched are removed, so there
are no duplicates. Backs up the DB first.

CSV -> fields: Staff ID->emp_code | Staff Name->name | Designation->category |
Roll->role | Skils->skillset | Bim ID->bim_id | Mail ID->email | IP Address->ip_address
(Password ignored — password-less.)

Usage:  python import_employees.py ["path\to.csv"]
"""
import sys, os, csv, shutil, sqlite3, datetime as dt

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "tasklog.db")
DEFAULT_CSV = r"C:\Users\jse2084\Downloads\Architecture Team list(Sheet1).csv"

# Existing task-linked people (lowercase DB name) -> the CSV Staff ID they are.
TASK_LINKED = {
    "aasin": "2588", "harika": "1795", "rajini": "2052",
    "ramakrishnan": "2084", "vignesh": "1597", "akul": "2013",
}


def norm(s):
    return (s or "").strip()


def main(csv_path):
    if not os.path.exists(csv_path):
        sys.exit(f"CSV not found: {csv_path}")
    backup = os.path.join(HERE, f"tasklog_before_emp_import_{dt.datetime.now():%Y%m%d_%H%M%S}.db")
    shutil.copyfile(DB, backup)
    print(f"Backup saved: {os.path.basename(backup)}\n")

    con = sqlite3.connect(DB)
    con.execute("PRAGMA foreign_keys = ON")
    cur = con.cursor()

    # staff id -> existing person id, for the task-linked people
    sid_to_pid = {}
    for pid, name in cur.execute("SELECT id, name FROM people").fetchall():
        key = norm(name).lower()
        if key in TASK_LINKED:
            sid_to_pid[TASK_LINKED[key]] = pid

    def role_id(roll):
        roll = norm(roll)
        if not roll:
            return None
        cur.execute("INSERT OR IGNORE INTO roles(name) VALUES (?)", (roll,))
        return cur.execute("SELECT id FROM roles WHERE name=?", (roll,)).fetchone()[0]

    rows = list(csv.DictReader(open(csv_path, encoding="utf-8-sig", newline="")))
    updated, inserted = [], []
    for r in rows:
        name = norm(r.get("Staff Name")); sid = norm(r.get("Staff ID"))
        if not name and not sid:
            continue
        fields = {
            "name": name, "emp_code": sid or None, "role_id": role_id(r.get("Roll")),
            "category": norm(r.get("Designation")) or None, "skillset": norm(r.get("Skils")) or None,
            "bim_id": norm(r.get("Bim ID")) or None, "email": norm(r.get("Mail ID")) or None,
            "ip_address": norm(r.get("IP Address")) or None,
        }
        pid = sid_to_pid.get(sid)
        if pid:
            cur.execute(f"UPDATE people SET {','.join(k+'=?' for k in fields)} WHERE id=?",
                        list(fields.values()) + [pid])
            updated.append((sid, name))
        else:
            cur.execute(f"INSERT INTO people({','.join(fields)}) VALUES ({','.join('?'*len(fields))})",
                        list(fields.values()))
            inserted.append((sid, name))

    # remove leftover placeholder people (no emp id assigned and no tasks) to avoid duplicates
    removed = cur.execute(
        "SELECT name FROM people WHERE emp_code IS NULL "
        "AND id NOT IN (SELECT person_id FROM task_assignees)").fetchall()
    cur.execute("DELETE FROM people WHERE emp_code IS NULL "
                "AND id NOT IN (SELECT person_id FROM task_assignees)")
    con.commit()
    total = con.execute("SELECT COUNT(*) FROM people").fetchone()[0]
    orphan = con.execute("SELECT COUNT(*) FROM task_assignees ta "
                         "LEFT JOIN people pe ON pe.id=ta.person_id WHERE pe.id IS NULL").fetchone()[0]
    con.close()

    print(f"UPDATED task-linked (ids/tasks preserved): {len(updated)}  {[u[0] for u in updated]}")
    print(f"INSERTED: {len(inserted)}")
    print(f"REMOVED stray placeholders: {[r[0] for r in removed] or 'none'}")
    print(f"\nEmployee table now: {total} people | orphaned task links: {orphan} (must be 0)")
    print(f"(Undo: copy {os.path.basename(backup)} back over tasklog.db)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CSV)
