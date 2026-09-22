"""Record who actually typed each task, not just whose work it is.

`task_assignees` answers "whose work was this?".  It does not answer "who sat
at the keyboard?".  Those differ whenever a Manager or Team Leader logs an
entry on someone else's behalf, which is exactly the case an audit needs to
distinguish.  `tasks.entered_by` stores the signed-in person at insert time.

Historical rows stay NULL on purpose.  Guessing the typist from the assignee
would invent an audit trail that was never recorded, and a NULL that plainly
means "entered before attribution existed" is worth more than a confident
wrong answer.  Run once; it is idempotent.
"""
import shutil
import sqlite3
import sys
from datetime import datetime

DB = sys.argv[1] if len(sys.argv) > 1 else "tasklog.db"

con = sqlite3.connect(DB, timeout=15)
cur = con.cursor()
have = {r[1] for r in cur.execute("PRAGMA table_info(tasks)")}
if "entered_by" in have:
    print("tasks.entered_by already exists - nothing to do.")
    con.close()
    sys.exit(0)

backup = DB + ".entered_by_before_" + datetime.now().strftime("%Y%m%d")
shutil.copy2(DB, backup)
print("backup:", backup)

cur.execute("ALTER TABLE tasks ADD COLUMN entered_by INTEGER REFERENCES people(id)")
cur.execute("CREATE INDEX IF NOT EXISTS ix_tasks_entered_by ON tasks(entered_by)")
con.commit()

total = cur.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
print(f"added tasks.entered_by; {total} existing row(s) left NULL (entered before attribution).")
con.close()
