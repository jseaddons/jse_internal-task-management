"""Remove master tasks that have no name.

A blank-named master task renders as an empty row in the entry form's Master
Task dropdown, which makes the list look broken and lets someone log work
against a sheet with no identity.  Project 24139 picked one up from an early
import run; it duplicated QC REVIEW's Category mapping and had no work logged
against it.

Only unused rows are removed — a blank master task that someone has already
logged time against is left alone and reported, because deleting it would take
the task history with it.  Run once; it is idempotent.
"""
import shutil
import sqlite3
import sys
from datetime import datetime

DB = sys.argv[1] if len(sys.argv) > 1 else "tasklog.db"
backup = DB + ".blank_master_task_before_" + datetime.now().strftime("%Y%m%d")
shutil.copy2(DB, backup)

con = sqlite3.connect(DB, timeout=15)
cur = con.cursor()
cur.execute("PRAGMA foreign_keys = ON")

blanks = cur.execute(
    "SELECT id, project_code FROM master_tasks WHERE TRIM(COALESCE(name,'')) = ''").fetchall()

removed, kept = [], []
for mid, project in blanks:
    used = cur.execute("SELECT COUNT(*) FROM tasks WHERE master_task_id=?", (mid,)).fetchone()[0]
    if used:
        kept.append((mid, project, used))
        continue
    cur.execute("DELETE FROM category_tasks WHERE master_task_id=?", (mid,))
    cur.execute("DELETE FROM master_task_tasks WHERE master_task_id=?", (mid,))
    cur.execute("DELETE FROM master_tasks WHERE id=?", (mid,))
    removed.append((mid, project))

con.commit()

print(f"Backup: {backup}")
for mid, project in removed:
    print(f"  removed blank master task {mid} (project {project})")
for mid, project, used in kept:
    print(f"  KEPT blank master task {mid} (project {project}) — {used} task(s) logged against it")
if not blanks:
    print("  nothing to do — no blank-named master tasks")

print()
print("master tasks for 24139:", cur.execute(
    "SELECT id, name FROM master_tasks WHERE project_code='24139'").fetchall())
print("blank-named remaining:", cur.execute(
    "SELECT COUNT(*) FROM master_tasks WHERE TRIM(COALESCE(name,'')) = ''").fetchone()[0])
print("total master tasks:", cur.execute("SELECT COUNT(*) FROM master_tasks").fetchone()[0])
print("fk check:", cur.execute("PRAGMA foreign_key_check").fetchall() or "clean")
con.close()
