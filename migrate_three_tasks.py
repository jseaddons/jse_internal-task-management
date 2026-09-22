"""Restructure the entry hierarchy so the Task list is a fixed 3-item catalog.

    Master Task -> Category -> Task (3 core items) -> Sub Task

The review-comment items that used to sit at Task level move down to Sub Task,
scoped to the Category they came from.  Run once; it is idempotent.
"""
import shutil
import sqlite3
import sys
from datetime import datetime

DB = sys.argv[1] if len(sys.argv) > 1 else "tasklog.db"
CORE_TASKS = ("REVIEW COMMENTS", "CHECK MODEL HEALTH", "CHECK UPDATES")
RENAMES = {"MODEL CHECK": "CHECK MODEL HEALTH"}
REVIEW_TASK = "REVIEW COMMENTS"

backup = DB + ".three_tasks_before_" + datetime.now().strftime("%Y%m%d")
shutil.copy2(DB, backup)

con = sqlite3.connect(DB)
cur = con.cursor()

# 1. Sub tasks become category-aware.
cols = [r[1] for r in cur.execute("PRAGMA table_info(task_sub_tasks)")]
if "category_id" not in cols:
    cur.execute("ALTER TABLE task_sub_tasks ADD COLUMN category_id INTEGER REFERENCES categories(id)")

# 2. The three core Task names, reusing the near-miss rows already present.
for old, new in RENAMES.items():
    if cur.execute("SELECT 1 FROM task_names WHERE name=?", (new,)).fetchone():
        continue
    cur.execute("UPDATE task_names SET name=? WHERE name=?", (new, old))
for name in CORE_TASKS:
    cur.execute("INSERT OR IGNORE INTO task_names(name) VALUES (?)", (name,))
core_ids = {n: cur.execute("SELECT id FROM task_names WHERE name=?", (n,)).fetchone()[0]
            for n in CORE_TASKS}
review_id = core_ids[REVIEW_TASK]

# 3. Every master task that already has a category mapping.
masters = [r[0] for r in cur.execute("SELECT DISTINCT master_task_id FROM category_tasks")]
moved = 0
for master in masters:
    pairs = cur.execute(
        """SELECT ct.category_id, tn.name FROM category_tasks ct
             JOIN task_names tn ON tn.id = ct.task_name_id
            WHERE ct.master_task_id = ? AND tn.name NOT IN (?,?,?)""",
        (master, *CORE_TASKS)).fetchall()
    for category_id, task_name in pairs:
        cur.execute(
            "INSERT OR IGNORE INTO task_sub_tasks(task_name_id,category_id,name) VALUES (?,?,?)",
            (review_id, category_id, task_name))
        moved += cur.rowcount

    categories = [r[0] for r in cur.execute(
        "SELECT DISTINCT category_id FROM category_tasks WHERE master_task_id=?", (master,))]
    cur.execute("DELETE FROM category_tasks WHERE master_task_id=?", (master,))
    cur.execute("DELETE FROM master_task_tasks WHERE master_task_id=?", (master,))
    for category_id in categories:
        for task_id in core_ids.values():
            cur.execute(
                "INSERT OR IGNORE INTO category_tasks(master_task_id,category_id,task_name_id) VALUES (?,?,?)",
                (master, category_id, task_id))
            cur.execute(
                "INSERT OR IGNORE INTO master_task_tasks(master_task_id,task_name_id) VALUES (?,?)",
                (master, task_id))

# 4. Drop the stale flat dump of the Review comment sheet under 'Adding Rooms'.
stale = cur.execute(
    """SELECT ts.id FROM task_sub_tasks ts JOIN task_names tn ON tn.id = ts.task_name_id
        WHERE tn.name = 'Adding Rooms' AND ts.category_id IS NULL
          AND ts.id NOT IN (SELECT task_sub_task_id FROM tasks WHERE task_sub_task_id IS NOT NULL)""").fetchall()
cur.executemany("DELETE FROM task_sub_tasks WHERE id=?", stale)

con.commit()

print(f"Backup:        {backup}")
print(f"Core tasks:    {core_ids}")
print(f"Masters fixed: {masters}")
print(f"Sub tasks +:   {moved}   stale removed: {len(stale)}")
for r in cur.execute("""SELECT c.name, COUNT(*) FROM task_sub_tasks ts
                          JOIN categories c ON c.id = ts.category_id
                         WHERE ts.task_name_id = ? GROUP BY 1 ORDER BY 1""", (review_id,)):
    print(f"  {r[0]:<20} {r[1]}")
con.close()
