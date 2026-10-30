"""SQLite storage: dedupe, cached scores, and an application tracker."""
from __future__ import annotations

import json
import re
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, source TEXT, company TEXT, title TEXT, location TEXT, url TEXT,
  posted TEXT, pay_min INTEGER, pay_max INTEGER, description TEXT,
  quick_score REAL, score INTEGER, verdict TEXT, analysis TEXT,
  status TEXT DEFAULT 'new', first_seen REAL, notes TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def dedupe_key(company: str, title: str) -> str:
    """Company + title with everything but letters and digits removed, so 'Stripe' / 'stripe' and
    'Software Engineer, I' / 'Software Engineer I' compare equal."""
    norm = lambda t: re.sub(r"[^a-z0-9]+", "", (t or "").lower())
    return f"{norm(company)}|{norm(title)}"


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn) -> None:
    """Add the dedupe_key column to databases created before it existed, and fill it for old rows."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
    if "dedupe_key" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN dedupe_key TEXT")
    for r in conn.execute("SELECT id, company, title FROM jobs WHERE dedupe_key IS NULL").fetchall():
        conn.execute("UPDATE jobs SET dedupe_key=? WHERE id=?", (dedupe_key(r["company"], r["title"]), r["id"]))
    conn.commit()


def is_cross_source_duplicate(conn, job) -> bool:
    """True when the same company and title is already stored from a DIFFERENT source (for example a job
    that appears on both a company's own board and an aggregator). Within one source, equal titles are kept:
    'Software Engineer' in Austin and in Chicago are two real postings."""
    return conn.execute("SELECT 1 FROM jobs WHERE dedupe_key=? AND source<>? LIMIT 1",
                        (dedupe_key(job.company, job.title), job.source)).fetchone() is not None


def get_meta(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    conn.commit()


def add_job(conn, job, quick: float) -> bool:
    cur = conn.execute(
        "INSERT OR IGNORE INTO jobs (id, source, company, title, location, url, posted, pay_min, pay_max,"
        " description, quick_score, first_seen, dedupe_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (job.id, job.source, job.company, job.title, job.location, job.url, job.posted,
         job.pay_min, job.pay_max, job.description, quick, time.time(), dedupe_key(job.company, job.title)),
    )
    conn.commit()
    return cur.rowcount == 1


def unscored(conn, limit: int):
    return conn.execute(
        "SELECT * FROM jobs WHERE score IS NULL AND status='new' ORDER BY quick_score DESC LIMIT ?", (limit,)
    ).fetchall()


def save_score(conn, job_id: str, score: int, verdict: str, analysis: dict) -> None:
    conn.execute("UPDATE jobs SET score=?, verdict=?, analysis=? WHERE id=?",
                 (score, verdict, json.dumps(analysis), job_id))
    conn.commit()


def top(conn, n: int, min_score: int):
    return conn.execute(
        "SELECT * FROM jobs WHERE score IS NOT NULL AND score>=? AND status IN ('new','saved')"
        " ORDER BY score DESC, quick_score DESC LIMIT ?", (min_score, n)
    ).fetchall()


def set_status(conn, id_part: str, status: str, note: str = "") -> int:
    cur = conn.execute("UPDATE jobs SET status=?, notes=COALESCE(NULLIF(?, ''), notes) WHERE id LIKE ?",
                       (status, note, f"%{id_part}%"))
    conn.commit()
    return cur.rowcount
