"""SQLite storage: dedupe, cached scores, and an application tracker."""
from __future__ import annotations

import json
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, source TEXT, company TEXT, title TEXT, location TEXT, url TEXT,
  posted TEXT, pay_min INTEGER, pay_max INTEGER, description TEXT,
  quick_score REAL, score INTEGER, verdict TEXT, analysis TEXT,
  status TEXT DEFAULT 'new', first_seen REAL, notes TEXT
);
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def add_job(conn, job, quick: float) -> bool:
    cur = conn.execute(
        "INSERT OR IGNORE INTO jobs (id, source, company, title, location, url, posted, pay_min, pay_max,"
        " description, quick_score, first_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (job.id, job.source, job.company, job.title, job.location, job.url, job.posted,
         job.pay_min, job.pay_max, job.description, quick, time.time()),
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
