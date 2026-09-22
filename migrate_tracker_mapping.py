"""Two things the AL AIN tracker needs before it can be imported.

1. An "All zones" zone for 24139. Surendera's Facade row spans every zone, and
   no building carried the is_all flag, so those rows had nowhere to go.

2. A name mapping for the tracker's sub-task columns. The sheet uses short
   headings ("Height", "Clash", "Missing door") for sub tasks the database
   already holds under fuller names ("Ceiling Height", "Clash with wall &
   column", "Place missing doors"). All 34 columns have an equivalent, so
   adding them as new sub tasks would duplicate the whole catalog. They are
   recorded as aliases instead: the import resolves a tracker heading to the
   sub task that already exists, and the dropdowns stay as they are.

Run once; it is idempotent.
"""
import io
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime

DB = sys.argv[1] if len(sys.argv) > 1 else "tasklog.db"
MAP = sys.argv[2] if len(sys.argv) > 2 else "alias_map.json"
PROJECT = "24139"
ALL_ZONES = "All zones"

backup = DB + ".tracker_map_before_" + datetime.now().strftime("%Y%m%d")
shutil.copy2(DB, backup)

con = sqlite3.connect(DB, timeout=15)
cur = con.cursor()
cur.execute("PRAGMA foreign_keys = ON")

# 1. The all-zones zone.
cur.execute("INSERT OR IGNORE INTO buildings(project_code,name,is_all) VALUES (?,?,1)",
            (PROJECT, ALL_ZONES))
zone_added = cur.rowcount
cur.execute("UPDATE buildings SET is_all=1 WHERE project_code=? AND name=?", (PROJECT, ALL_ZONES))

# 2. Alias table: an external sheet's wording -> a sub task we already have.
# The category is part of the key: the tracker uses "Clash" and "Location"
# under both Signage and Handrail, meaning different sub tasks each time.
cur.execute("""CREATE TABLE IF NOT EXISTS sub_task_aliases (
    source           TEXT NOT NULL,          -- which sheet the wording came from
    category         TEXT NOT NULL,          -- the band the column sits under
    alias            TEXT NOT NULL,          -- the heading as it appears there
    task_sub_task_id INTEGER NOT NULL REFERENCES task_sub_tasks(id) ON DELETE CASCADE,
    PRIMARY KEY (source, category, alias))""")

with io.open(MAP, encoding="utf-8") as fh:
    mapping = json.load(fh)

added = skipped = 0
for m in mapping:
    exists = cur.execute("SELECT 1 FROM task_sub_tasks WHERE id=?", (m["id"],)).fetchone()
    if not exists:
        skipped += 1
        print(f"  SKIP {m['alias']!r}: sub task id {m['id']} no longer exists")
        continue
    cur.execute("INSERT OR IGNORE INTO sub_task_aliases(source,category,alias,task_sub_task_id) "
                "VALUES ('AL AIN Task Tracker',?,?,?)", (m["cat"], m["alias"], m["id"]))
    added += cur.rowcount

con.commit()

print(f"Backup: {backup}")
print(f"'{ALL_ZONES}' zone: {'created' if zone_added else 'already present'} (is_all=1)")
print(f"Aliases added: {added}   already present: {len(mapping) - added - skipped}   skipped: {skipped}")
print()
print("Zones for", PROJECT, "->", ", ".join(
    r[0] for r in cur.execute(
        "SELECT name FROM buildings WHERE project_code=? AND TRIM(name)<>'' ORDER BY name", (PROJECT,))))
print()
print("Sample of the mapping now stored:")
for cat, alias, name in cur.execute("""SELECT a.category, a.alias, ts.name FROM sub_task_aliases a
                                         JOIN task_sub_tasks ts ON ts.id = a.task_sub_task_id
                                        WHERE a.alias IN ('Clash','Location')
                                        ORDER BY a.alias, a.category"""):
    print(f"   {cat:<10} {alias:<12} -> {name}")
print("\ntotal aliases:", cur.execute("SELECT COUNT(*) FROM sub_task_aliases").fetchone()[0])
print("fk check:", cur.execute("PRAGMA foreign_key_check").fetchall() or "clean")
con.close()
