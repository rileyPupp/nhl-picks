"""Database layer: PostgreSQL when DATABASE_URL is set (hosted), else a local SQLite file.

Queries use ? placeholders and SQL that both databases accept
(ON CONFLICT upserts, RETURNING, lower(name) indexes).
"""
import atexit
import os
import sqlite3
from contextlib import contextmanager

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE_URL = os.environ.get("DATABASE_URL")
SQLITE_FILE = os.environ.get("NHL_PICKS_DB", os.path.join(BASE_DIR, "nhl-picks.db"))
IS_POSTGRES = bool(DATABASE_URL)


class Duplicate(Exception):
    """A unique constraint was violated."""


if IS_POSTGRES:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    _pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=10, open=True,
                           kwargs={"row_factory": dict_row})
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
def connect():
    """One transaction: commits on success, rolls back on error."""
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
)
"""


def init():
    with connect() as conn:
        for stmt in SCHEMA.split(";"):
            if stmt.strip():
                conn.run(stmt)


def describe():
    if IS_POSTGRES:
        return "PostgreSQL (DATABASE_URL)"
    return f"SQLite ({SQLITE_FILE})"
