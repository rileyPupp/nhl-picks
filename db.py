"""Database layer: PostgreSQL when DATABASE_URL is set (hosted), else a local SQLite file.

Locally the app uses the SQLite file even though `neon deploy` writes the Neon connection
strings into .env.local. Set USE_NEON=1 to use them (e.g. `copy_to_postgres.py` does).

Queries use ? placeholders and SQL that both databases accept
(ON CONFLICT upserts, RETURNING, lower(name) indexes).
"""
import atexit
import os
import sqlite3
from contextlib import contextmanager

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


DB_KEYS = ("DATABASE_URL", "DATABASE_URL_UNPOOLED")


def _load_env_files():
    """Read KEY=VALUE lines from .env.local, then .env (written by `neon deploy` / `neon env pull`).

    Real environment variables win, so a host's settings (e.g. on Render) are never overridden.
    The database strings in these files are skipped unless USE_NEON=1, so a local run
    doesn't touch the live database.
    """
    use_neon = os.environ.get("USE_NEON") == "1"
    for name in (".env.local", ".env"):
        path = os.path.join(BASE_DIR, name)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.removeprefix("export ").split("=", 1)
                key, value = key.strip(), value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                    value = value[1:-1]
                if key in DB_KEYS and not use_neon:
                    continue
                os.environ.setdefault(key, value)


_load_env_files()

# Neon gives two strings: pooled (DATABASE_URL, for app traffic) and direct
# (DATABASE_URL_UNPOOLED, for schema changes and bulk copies).
DATABASE_URL = os.environ.get("DATABASE_URL")
DIRECT_URL = os.environ.get("DATABASE_URL_UNPOOLED") or DATABASE_URL
SQLITE_FILE = os.environ.get("NHL_PICKS_DB", os.path.join(BASE_DIR, "nhl-picks.db"))
IS_POSTGRES = bool(DATABASE_URL)


class Duplicate(Exception):
    """A unique constraint was violated."""


if IS_POSTGRES:
    try:
        import psycopg
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool
    except ImportError:
        raise SystemExit("DATABASE_URL is set but the Postgres driver isn't installed. "
                         "Run: pip install -r requirements.txt")

    # prepare_threshold=None: the pooled endpoint (PgBouncer, transaction mode) can hand each
    # transaction a different server connection, so don't rely on server-side prepared statements.
    _PG_KWARGS = {"row_factory": dict_row, "prepare_threshold": None}
    # check_connection: Neon suspends idle computes, which drops pooled connections; test before use.
    _pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=10, open=True, kwargs=_PG_KWARGS,
                           check=ConnectionPool.check_connection)
    atexit.register(_pool.close)
    _UNIQUE_ERRORS = (psycopg.errors.UniqueViolation,)
else:
    _UNIQUE_ERRORS = (sqlite3.IntegrityError,)


class Conn:
    def __init__(self, raw):
        self.raw = raw

    def run(self, sql, params=()):
        if IS_POSTGRES:
            sql = sql.replace("?", "%s")
        try:
            return self.raw.execute(sql, params)
        except _UNIQUE_ERRORS as e:
            if IS_POSTGRES or "UNIQUE" in str(e):
                raise Duplicate(str(e)) from e
            raise

    def many(self, sql, rows):
        if IS_POSTGRES:
            sql = sql.replace("?", "%s")
            with self.raw.cursor() as cur:
                cur.executemany(sql, rows)
        else:
            self.raw.executemany(sql, rows)

    def one(self, sql, params=()):
        row = self.run(sql, params).fetchone()
        return dict(row) if row is not None else None

    def all(self, sql, params=()):
        return [dict(r) for r in self.run(sql, params).fetchall()]

    def value(self, sql, params=()):
        row = self.run(sql, params).fetchone()
        return None if row is None else next(iter(dict(row).values()))


@contextmanager
def connect(direct=False):
    """One transaction: commits on success, rolls back on error.

    direct=True uses Neon's unpooled connection, for schema setup and bulk copies.
    """
    if IS_POSTGRES and direct:
        with psycopg.connect(DIRECT_URL, **_PG_KWARGS) as raw:
            yield Conn(raw)
        return
    if IS_POSTGRES:
        with _pool.connection() as raw:
            yield Conn(raw)
        return
    raw = sqlite3.connect(SQLITE_FILE, timeout=10)
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA foreign_keys = ON")
    try:
        yield Conn(raw)
        raw.commit()
    except BaseException:
        raw.rollback()
        raise
    finally:
        raw.close()


def placeholders(n):
    return ", ".join("?" * n)


_ID = "BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY" if IS_POSTGRES else "INTEGER PRIMARY KEY"
_REAL = "DOUBLE PRECISION" if IS_POSTGRES else "REAL"

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS users (
    id {_ID},
    name TEXT NOT NULL,
    pin_salt TEXT NOT NULL,
    pin_hash TEXT NOT NULL,
    created TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS users_name_key ON users (lower(name));
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires {_REAL} NOT NULL
);
CREATE TABLE IF NOT EXISTS picks (
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    game_id TEXT NOT NULL,
    date TEXT NOT NULL,
    winner TEXT NOT NULL,
    away_score INTEGER,
    home_score INTEGER,
    PRIMARY KEY (user_id, game_id)
);
CREATE INDEX IF NOT EXISTS picks_date ON picks (date);
CREATE TABLE IF NOT EXISTS slates (
    date TEXT NOT NULL,
    slot INTEGER NOT NULL,
    player_id BIGINT NOT NULL,
    name TEXT NOT NULL,
    position TEXT,
    team TEXT NOT NULL,
    opponent TEXT NOT NULL,
    game_id TEXT NOT NULL,
    headshot TEXT,
    tier TEXT NOT NULL,
    season_gp INTEGER NOT NULL,
    season_points INTEGER NOT NULL,
    PRIMARY KEY (date, slot)
);
CREATE TABLE IF NOT EXISTS player_picks (
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    date TEXT NOT NULL,
    player_id BIGINT NOT NULL,
    guess INTEGER NOT NULL,
    PRIMARY KEY (user_id, date, player_id)
);
CREATE TABLE IF NOT EXISTS leagues (
    id {_ID},
    name TEXT NOT NULL,
    code TEXT NOT NULL UNIQUE,
    owner_id BIGINT NOT NULL REFERENCES users(id),
    created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS league_members (
    league_id BIGINT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    joined TEXT NOT NULL,
    PRIMARY KEY (league_id, user_id)
);
CREATE INDEX IF NOT EXISTS league_members_user ON league_members (user_id);
CREATE TABLE IF NOT EXISTS identities (
    provider TEXT NOT NULL,
    subject TEXT NOT NULL,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    email TEXT,
    created TEXT NOT NULL,
    PRIMARY KEY (provider, subject)
);
CREATE INDEX IF NOT EXISTS identities_user ON identities (user_id);
CREATE TABLE IF NOT EXISTS oauth_states (
    state TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    intent TEXT NOT NULL,
    user_id BIGINT REFERENCES users(id) ON DELETE CASCADE,
    expires {_REAL} NOT NULL
);
CREATE TABLE IF NOT EXISTS standings_orders (
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    period_type TEXT NOT NULL,
    period_key TEXT NOT NULL,
    division TEXT NOT NULL,
    team TEXT NOT NULL,
    predicted_rank INTEGER NOT NULL,
    PRIMARY KEY (user_id, period_type, period_key, team)
);
CREATE INDEX IF NOT EXISTS standings_orders_period ON standings_orders (period_type, period_key);
CREATE TABLE IF NOT EXISTS standings_results (
    period_type TEXT NOT NULL,
    period_key TEXT NOT NULL,
    team TEXT NOT NULL,
    division TEXT NOT NULL,
    division_rank INTEGER NOT NULL,
    points INTEGER NOT NULL,
    PRIMARY KEY (period_type, period_key, team)
);
CREATE TABLE IF NOT EXISTS news_items (
    link TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    published TEXT NOT NULL,
    teams TEXT NOT NULL,
    fetched TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS news_items_published ON news_items (published);
"""


def init():
    with connect(direct=True) as conn:
        for stmt in SCHEMA.split(";"):
            if stmt.strip():
                conn.run(stmt)


def describe():
    if IS_POSTGRES:
        return "PostgreSQL (DATABASE_URL)"
    return f"SQLite ({SQLITE_FILE})"
