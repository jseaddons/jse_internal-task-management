"""Give each project its own team, so the Assignees list can be filtered.

Until now the entry form offered all 31 employees on every project, even
people who have never worked on it.  `project_people` records who is on which
project.

An empty team means "no restriction" — every employee stays selectable.  That
matters because a hard filter with no data would leave nobody assignable and
block entry entirely; a project only narrows once someone has actually picked
its team in Manage Lists.  Run once; it is idempotent.
"""
import shutil
import sqlite3
import sys
from datetime import datetime

DB = sys.argv[1] if len(sys.argv) > 1 else "tasklog.db"
backup = DB + ".project_team_before_" + datetime.now().strftime("%Y%m%d")
shutil.copy2(DB, backup)

con = sqlite3.connect(DB, timeout=15)
cur = con.cursor()
cur.executescript("""
CREATE TABLE IF NOT EXISTS project_people (
    project_code TEXT    NOT NULL REFERENCES projects(code) ON DELETE CASCADE,
    person_id    INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    PRIMARY KEY (project_code, person_id));
CREATE INDEX IF NOT EXISTS ix_project_people_person ON project_people(person_id);
""")
con.commit()

print(f"Backup: {backup}")
print("project_people rows:", cur.execute("SELECT COUNT(*) FROM project_people").fetchone()[0],
      "(empty = every project still offers all employees)")
print()
print("For reference, who has actually logged work on each project:")
for code, n in cur.execute("""SELECT t.project_code, COUNT(DISTINCT ta.person_id)
                                FROM tasks t JOIN task_assignees ta ON ta.task_id = t.id
                               GROUP BY 1 ORDER BY 1"""):
    print(f"  {code}: {n} people")
print("fk check:", cur.execute("PRAGMA foreign_key_check").fetchall() or "clean")
con.close()
