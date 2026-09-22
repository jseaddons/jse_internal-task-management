"""Category and Sub Task become many-per-task.

A person routinely works across several Categories in a day and ticks off
several Sub Tasks, so both move from a single FK on `tasks` to junction tables
alongside `task_assignees`.  The original `tasks.category_id` and
`tasks.task_sub_task_id` columns stay as the first-picked value so older rows
and anything still reading them keep working.  Run once; it is idempotent.
"""
import shutil
import sqlite3
import sys
from datetime import datetime

DB = sys.argv[1] if len(sys.argv) > 1 else "tasklog.db"
backup = DB + ".multi_select_before_" + datetime.now().strftime("%Y%m%d")
shutil.copy2(DB, backup)

con = sqlite3.connect(DB, timeout=15)
cur = con.cursor()
cur.executescript("""
CREATE TABLE IF NOT EXISTS task_categories (
    task_id     INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    PRIMARY KEY(task_id, category_id));
CREATE TABLE IF NOT EXISTS task_sub_task_links (
    task_id          INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    task_sub_task_id INTEGER NOT NULL REFERENCES task_sub_tasks(id) ON DELETE CASCADE,
    PRIMARY KEY(task_id, task_sub_task_id));
CREATE INDEX IF NOT EXISTS ix_task_categories_cat ON task_categories(category_id);
CREATE INDEX IF NOT EXISTS ix_task_sub_task_links_sub ON task_sub_task_links(task_sub_task_id);
""")

# Backfill the junctions from whatever the single columns already hold.
cur.execute("""INSERT OR IGNORE INTO task_categories(task_id,category_id)
               SELECT id, category_id FROM tasks WHERE category_id IS NOT NULL""")
cats = cur.rowcount
cur.execute("""INSERT OR IGNORE INTO task_sub_task_links(task_id,task_sub_task_id)
               SELECT id, task_sub_task_id FROM tasks WHERE task_sub_task_id IS NOT NULL""")
subs = cur.rowcount
con.commit()

print(f"Backup:            {backup}")
print(f"task_categories:   +{cats}  (total {cur.execute('SELECT COUNT(*) FROM task_categories').fetchone()[0]})")
print(f"task_sub_task_links: +{subs}  (total {cur.execute('SELECT COUNT(*) FROM task_sub_task_links').fetchone()[0]})")
print("fk check:", cur.execute("PRAGMA foreign_key_check").fetchall() or "clean")
con.close()
