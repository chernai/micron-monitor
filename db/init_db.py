"""Create/upgrade the database (Postgres, via Supabase) from schema.sql.

The rest of the codebase (collectors, scoring, dashboard) was written
against sqlite3's connection.execute(...) convenience API with '?'
placeholders and dict-like row access. Rather than rewrite every call site
for psycopg2's cursor-based API, PgConnection below wraps a psycopg2
connection to present that same surface, so this is the only file (plus
store.py, which needs real Postgres idioms for upserts) that had to change
for the SQLite -> Postgres/Supabase migration.
"""
import os
import re
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
_QMARK_RE = re.compile(r"\?")


class PgConnection:
    def __init__(self, dsn):
        self._conn = psycopg2.connect(dsn)

    def execute(self, sql, params=()):
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(_QMARK_RE.sub("%s", sql), params)
        return cur

    def executescript(self, sql):
        cur = self._conn.cursor()
        cur.execute(sql)
        cur.close()

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()


def get_conn():
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError(
            "DATABASE_URL is not set. Create a Supabase project, copy its Postgres "
            "connection string (Project Settings -> Database -> Connection string), "
            "and set DATABASE_URL in a local .env file (see .env.example) and in "
            "your deploy platform's secrets/environment variables."
        )
    return PgConnection(dsn)


def init_db():
    conn = get_conn()
    with open(SCHEMA_PATH) as f:
        conn.executescript(f.read())
    conn.commit()
    conn.close()
    print("Database ready (Supabase/Postgres).")


if __name__ == "__main__":
    init_db()
