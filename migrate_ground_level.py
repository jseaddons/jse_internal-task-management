"""Add F00 (ground level) for the AL AIN project, and sort basements below it.

AL AIN (24139) numbers its floors from the ground up: F00 is the ground floor.
The older 2630/M45 projects follow a client convention with no Level 0, which
is why the levels catalog had no zero — see the guard in server.py's "level"
admin action.  That guard governs the legacy `Level N` labels and is left
alone: levels are shared, but a project only sees the ones mapped to it in
`project_levels`, so a ground level in the catalog never appears on M45.

Basements also shared ordinals with floors (B01 and F01 both number 1), which
made the Level dropdown interleave them unpredictably.  Basements become
negative so they sort below ground: B02, B01, F00, F01, F02 ...

Run once; it is idempotent.
"""
import io
import os
import shutil
import sqlite3
import sys
from datetime import datetime

DB = sys.argv[1] if len(sys.argv) > 1 else "tasklog.db"
PROJECT = "24139"
BASEMENTS = {"B01": -1, "B02": -2}

backup = DB + ".ground_level_before_" + datetime.now().strftime("%Y%m%d")
shutil.copy2(DB, backup)

con = sqlite3.connect(DB, timeout=15)
cur = con.cursor()
cur.execute("PRAGMA foreign_keys = ON")

# 1. The no-Level-0 rule is a table CHECK, not just the admin guard, so the
#    table has to be rebuilt before a ground level can exist at all. Note that
#    INSERT OR IGNORE swallows a CHECK violation silently — the constraint has
#    to go, it cannot be worked around.
sql = cur.execute("SELECT sql FROM sqlite_master WHERE name='levels'").fetchone()[0]
if "number <> 0" in sql:
    # The report views read `levels`, so SQLite refuses to drop it while they
    # exist. Drop them first and rebuild from views.sql afterwards.
    views = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='view'")]
    views_sql = os.path.join(os.path.dirname(os.path.abspath(DB)), "views.sql")
    if not os.path.exists(views_sql):
        views_sql = os.path.join(os.path.dirname(os.path.abspath(__file__)), "views.sql")
    if not os.path.exists(views_sql):
        con.close()
        raise SystemExit("views.sql not found — needed to rebuild the report views.")
    for v in views:
        cur.execute(f"DROP VIEW IF EXISTS {v}")
    cur.execute("PRAGMA foreign_keys = OFF")
    cur.executescript("""
    BEGIN;
    CREATE TABLE levels_new (
        id      INTEGER PRIMARY KEY,
        number  INTEGER,                 -- NULL = 'All levels' sentinel; 0 = ground
        label   TEXT NOT NULL UNIQUE,
        is_all  INTEGER NOT NULL DEFAULT 0
    );
    INSERT INTO levels_new(id,number,label,is_all)
        SELECT id,number,label,is_all FROM levels;
    DROP TABLE levels;
    ALTER TABLE levels_new RENAME TO levels;
    COMMIT;
    """)
    cur.execute("PRAGMA foreign_keys = ON")
    cur.executescript(io.open(views_sql, encoding="utf-8").read())
    restored = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='view'")]
    missing = set(views) - set(restored)
    if missing:
        con.close()
        raise SystemExit(f"views not restored: {sorted(missing)}")
    rebuilt = True
else:
    rebuilt = False

# 2. The ground level itself.
cur.execute("INSERT OR IGNORE INTO levels(number,label,is_all) VALUES (0,'F00',0)")
created = cur.rowcount
row = cur.execute("SELECT id FROM levels WHERE label='F00'").fetchone()
if row is None:
    con.close()
    raise SystemExit("F00 was not created — check the levels table constraints.")
ground = row[0]

# 3. Visible on AL AIN only.
cur.execute("INSERT OR IGNORE INTO project_levels(project_code,level_id) VALUES (?,?)",
            (PROJECT, ground))
mapped = cur.rowcount

# 4. Basements sort below ground rather than colliding with F01/F02.
renumbered = []
for label, number in BASEMENTS.items():
    row = cur.execute("SELECT id, number FROM levels WHERE label=?", (label,)).fetchone()
    if row and row[1] != number:
        cur.execute("UPDATE levels SET number=? WHERE id=?", (number, row[0]))
        renumbered.append(f"{label}: {row[1]} -> {number}")

con.commit()

print(f"Backup: {backup}")
print("levels table rebuilt to allow a ground level:", "yes" if rebuilt else "already allowed")
print(f"F00 created: {'yes' if created else 'already present'} (id {ground}, number 0)")
print(f"Mapped to {PROJECT}: {'yes' if mapped else 'already mapped'}")
print("Basements renumbered:", ", ".join(renumbered) if renumbered else "already correct")
print()
print(f"Level dropdown order for {PROJECT} is now:")
for label, number in cur.execute(
        """SELECT l.label, l.number FROM project_levels pl JOIN levels l ON l.id = pl.level_id
            WHERE pl.project_code = ? ORDER BY (l.number IS NULL), l.number""", (PROJECT,)):
    print(f"   {label:<6} number={number}")
print("\nfk check:", cur.execute("PRAGMA foreign_key_check").fetchall() or "clean")
con.close()
