"""A fresh PostgreSQL schema for each test, in the jobfinder_test database (TEST_DATABASE_URL in .env).

    conn = pgtest.connect()                   # a new empty database, working as the local user
    other = pgtest.connect(conn.schema, user="b@example.com")   # the same database, as another user

Every schema made here is dropped when the test run ends."""
from __future__ import annotations

import atexit
import os
import uuid
import weakref
from pathlib import Path

import psycopg

import db

_made: list[str] = []
_open: weakref.WeakSet = weakref.WeakSet()   # connections made here that are still alive: closed before the schemas are dropped (or the drop waits for
#                                                              their locks). Ones a test has let go of are closed when they are collected, so a long run never runs out.


def url() -> str:
    if not os.environ.get("TEST_DATABASE_URL"):
        env = Path(__file__).resolve().parent.parent / ".env"
        if env.exists():
            for line in env.read_text(encoding="utf-8").splitlines():
                if line.startswith("TEST_DATABASE_URL="):
                    os.environ["TEST_DATABASE_URL"] = line.split("=", 1)[1].strip()
    if not os.environ.get("TEST_DATABASE_URL"):
        raise RuntimeError("TEST_DATABASE_URL is not set. Add it to the .env file (a PostgreSQL database for tests).")
    return os.environ["TEST_DATABASE_URL"]


def new_schema() -> str:
    name = f"t_{uuid.uuid4().hex[:12]}"
    _made.append(name)
    return name


def connect(schema: str | None = None, user=None) -> db.Conn:
    schema = schema or new_schema()
    conn = db.connect(url(), schema=schema, user=user)
    conn.schema = schema
    _open.add(conn)
    return conn


def args(schema: str, user=None) -> dict:
    """The keyword arguments db.connect needs to open that schema (what the viewer is given in tests)."""
    return {"url": url(), "schema": schema, "user": user}


def _drop(names: list[str]) -> None:
    for conn in list(_open):
        try:
            conn.close()
        except Exception:
            pass
    with psycopg.connect(url(), autocommit=True) as c:
        c.execute("SET lock_timeout = '5s'")
        for name in names:
            try:
                c.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
            except psycopg.errors.LockNotAvailable:
                pass   # still in use by a leftover connection: the next test run drops it


def _drop_leftovers() -> None:
    """Schemas left behind by a test run that was killed."""
    with psycopg.connect(url(), autocommit=True) as c:
        old = [r[0] for r in c.execute("SELECT nspname FROM pg_namespace WHERE nspname LIKE 't\\_%'")]
    _drop(old)


_drop_leftovers()
atexit.register(lambda: _drop(_made))
