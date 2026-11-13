"""Copy the local SQLite data (nhl-picks.db) into an empty hosted Postgres database.

    pip install -r requirements.txt
    python copy_to_postgres.py

It reads DATABASE_URL (and DATABASE_URL_UNPOOLED) from the environment or from
.env.local, which `neon deploy` writes. Run it once, before anyone signs up on the
website. Sessions are not copied, so everyone signs in again on the site.
"""
import sqlite3
import sys

import db  # loads .env.local, picks Postgres when DATABASE_URL is set

if not db.IS_POSTGRES:
    sys.exit("Set DATABASE_URL (or run `neon deploy` to write .env.local) first.")

TABLES = ["users", "picks", "slates", "player_picks", "leagues", "league_members", "identities"]
IDENTITY_TABLES = {"users", "leagues"}  # their ids are copied as-is, then the counters are moved past them

src = sqlite3.connect(db.SQLITE_FILE)
src.row_factory = sqlite3.Row
have = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}

db.init()
with db.connect(direct=True) as conn:
    if conn.value("SELECT COUNT(*) AS n FROM users"):
        sys.exit("The Postgres database already has users. Copy only into an empty database.")
    for table in TABLES:
        if table not in have:
            continue
        rows = [dict(r) for r in src.execute(f"SELECT * FROM {table}")]
        if not rows:
            continue
        cols = list(rows[0])
        override = " OVERRIDING SYSTEM VALUE" if table in IDENTITY_TABLES else ""
        conn.many(f"INSERT INTO {table} ({', '.join(cols)}){override} VALUES ({db.placeholders(len(cols))})",
                  [tuple(r[c] for c in cols) for r in rows])
        print(f"{table}: {len(rows)} rows")
    for table in IDENTITY_TABLES:
        conn.run(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                 f"COALESCE((SELECT MAX(id) FROM {table}), 0) + 1, false)")
print("Done.")
