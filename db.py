"""SQLite storage: dedupe, cached scores, and an application tracker."""
from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import date, timedelta

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, source TEXT, company TEXT, title TEXT, location TEXT, url TEXT,
  posted TEXT, pay_min INTEGER, pay_max INTEGER, description TEXT,
  quick_score REAL, score INTEGER, verdict TEXT, analysis TEXT,
  status TEXT DEFAULT 'new', first_seen REAL, notes TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS interview_rounds (
  id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, kind TEXT NOT NULL, round_date TEXT,
  outcome TEXT NOT NULL DEFAULT 'scheduled', notes TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS interview_tips (job_id TEXT PRIMARY KEY, content TEXT NOT NULL, model TEXT, created TEXT);
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
    if "formatted" not in cols:   # 1 when the description keeps the posting's headings and bullets; old rows are plain text
        conn.execute("ALTER TABLE jobs ADD COLUMN formatted INTEGER DEFAULT 0")
    if "applied_at" not in cols:   # the day you applied, as YYYY-MM-DD; NULL means not applied
        conn.execute("ALTER TABLE jobs ADD COLUMN applied_at TEXT")
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
    """Store a posting. False when its id is already there, so a job seen on an earlier run is never added twice."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO jobs (id, source, company, title, location, url, posted, pay_min, pay_max,"
        " description, quick_score, first_seen, dedupe_key, formatted) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
        (job.id, job.source, job.company, job.title, job.location, job.url, job.posted,
         job.pay_min, job.pay_max, job.description, quick, time.time(), dedupe_key(job.company, job.title)),
    )
    conn.commit()
    return cur.rowcount == 1


def unscored(conn, limit: int):
    """Jobs not yet scored, best keyword match first, so a limited number of API calls goes to the likeliest fits."""
    return conn.execute(
        "SELECT * FROM jobs WHERE score IS NULL AND status='new' ORDER BY quick_score DESC LIMIT ?", (limit,)
    ).fetchall()


def save_score(conn, job_id: str, score: int, verdict: str, analysis: dict) -> None:
    """Record the model's answer; the full analysis is kept as JSON for the report and viewer."""
    conn.execute("UPDATE jobs SET score=?, verdict=?, analysis=? WHERE id=?",
                 (score, verdict, json.dumps(analysis), job_id))
    conn.commit()


def top(conn, n: int, min_score: int):
    """Best scored jobs still open to you (new or saved, not applied or skipped), highest score first."""
    return conn.execute(
        "SELECT * FROM jobs WHERE score IS NOT NULL AND score>=? AND status IN ('new','saved')"
        " ORDER BY score DESC, quick_score DESC LIMIT ?", (min_score, n)
    ).fetchall()


def saved(conn):
    """Every job you saved to look at later, best score first, whichever report it came from."""
    return conn.execute("SELECT * FROM jobs WHERE status='saved' ORDER BY score DESC, first_seen DESC").fetchall()


# Statuses that mean "I applied": a job that moves on to an interview, offer, or rejection was still applied to.
APPLIED_STATUSES = ("applied", "interview", "offer", "accepted", "rejected", "withdrawn")


def _update_status(conn, where: str, where_args: tuple, status: str, note: str, today: date | None) -> int:
    """Set the status and keep applied_at consistent with it. Moving to an applied-type status records today
    unless a date is already stored; moving to any other status (new, saved, skipped) clears the date, which
    removes the job from the application tracker."""
    if status in APPLIED_STATUSES:
        applied, extra = "COALESCE(applied_at, ?)", ((today or date.today()).isoformat(),)
    else:
        applied, extra = "NULL", ()
    cur = conn.execute(
        f"UPDATE jobs SET status=?, notes=COALESCE(NULLIF(?, ''), notes), applied_at={applied} WHERE {where}",
        (status, note, *extra, *where_args))
    conn.commit()
    return cur.rowcount


def set_status(conn, id_part: str, status: str, note: str = "", today: date | None = None) -> int:
    """Update every job whose id contains `id_part` (used by `main.py mark`)."""
    return _update_status(conn, "id LIKE ?", (f"%{id_part}%",), status, note, today)


def set_status_exact(conn, job_id: str, status: str, note: str = "", today: date | None = None) -> int:
    """Update the one job with exactly this id (used by the web viewer)."""
    return _update_status(conn, "id = ?", (job_id,), status, note, today)


def set_applied_date(conn, job_id: str, day: date) -> int:
    """Change the date of an application that is already in the tracker. Does nothing for a job that is not."""
    cur = conn.execute("UPDATE jobs SET applied_at=? WHERE id=? AND applied_at IS NOT NULL", (day.isoformat(), job_id))
    conn.commit()
    return cur.rowcount


def applications(conn) -> list:
    """Everything in the tracker, newest application first."""
    return conn.execute(
        "SELECT id, company, title, location, url, status, notes, applied_at FROM jobs WHERE applied_at IS NOT NULL"
        " ORDER BY applied_at DESC, first_seen DESC").fetchall()


ROUND_OUTCOMES = ("scheduled", "waiting", "passed", "rejected")


def add_round(conn, job_id: str, kind: str = "", day: str | None = None, outcome: str = "scheduled", notes: str = "") -> int:
    """Add an interview round to a job and return its id. A blank name becomes 'Round N', N counting the job's rounds."""
    if not kind:
        n = conn.execute("SELECT COUNT(*) FROM interview_rounds WHERE job_id=?", (job_id,)).fetchone()[0]
        kind = f"Round {n + 1}"
    cur = conn.execute("INSERT INTO interview_rounds (job_id, kind, round_date, outcome, notes) VALUES (?,?,?,?,?)",
                       (job_id, kind, day, outcome, notes))
    conn.commit()
    return cur.lastrowid


def update_round(conn, round_id: int, kind: str, day: str | None, outcome: str, notes: str) -> int:
    cur = conn.execute("UPDATE interview_rounds SET kind=?, round_date=?, outcome=?, notes=? WHERE id=?",
                       (kind, day, outcome, notes, round_id))
    conn.commit()
    return cur.rowcount


def delete_round(conn, round_id: int) -> int:
    cur = conn.execute("DELETE FROM interview_rounds WHERE id=?", (round_id,))
    conn.commit()
    return cur.rowcount


def rounds_for(conn, job_id: str) -> list:
    """One job's interview rounds in the order they were added."""
    return [dict(r) for r in conn.execute(
        "SELECT id, kind, round_date AS date, outcome, notes FROM interview_rounds WHERE job_id=? ORDER BY id", (job_id,))]


def rounds_by_job(conn) -> dict:
    """Every job's rounds, as {job id: [round, ...]}, in the order they were added."""
    out: dict = {}
    for r in conn.execute("SELECT id, job_id, kind, round_date AS date, outcome, notes FROM interview_rounds ORDER BY id"):
        out.setdefault(r["job_id"], []).append({k: r[k] for k in ("id", "kind", "date", "outcome", "notes")})
    return out


def get_tips(conn, job_id: str) -> dict | None:
    """The interview tips saved for a job, or None."""
    r = conn.execute("SELECT content, model, created FROM interview_tips WHERE job_id=?", (job_id,)).fetchone()
    return {"content": json.loads(r["content"]), "model": r["model"], "created": r["created"]} if r else None


def trashed(conn):
    """Every job you discarded (status 'skipped'), best score first. Discarded jobs stay out of reports until restored."""
    return conn.execute("SELECT * FROM jobs WHERE status='skipped' ORDER BY score DESC, first_seen DESC").fetchall()


def jobs_with_tips(conn) -> list:
    """Every job that has saved interview prep, newest prep first."""
    return conn.execute("SELECT jobs.*, t.created AS prep_created FROM jobs JOIN interview_tips t ON t.job_id = jobs.id"
                        " ORDER BY t.created DESC, jobs.score DESC").fetchall()


def save_tips(conn, job_id: str, content: dict, model: str, created: str) -> None:
    conn.execute("INSERT INTO interview_tips (job_id, content, model, created) VALUES (?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET"
                 " content=excluded.content, model=excluded.model, created=excluded.created",
                 (job_id, json.dumps(content), model, created))
    conn.commit()


def application_stats(conn, today: date | None = None) -> dict:
    """Counts of applications for the current week (Monday through Sunday), the current calendar month, and all time.
    'applications' counts every tracked job; 'companies' counts distinct companies among them."""
    today = today or date.today()
    week_start = today - timedelta(days=today.weekday())          # weekday(): Monday is 0
    week_end = week_start + timedelta(days=6)                      # Sunday
    month_start = today.replace(day=1)
    next_month = (month_start + timedelta(days=32)).replace(day=1)

    def count(lo: date | None = None, hi: date | None = None) -> dict:
        """Applications dated lo <= day < hi."""
        sql = "SELECT COUNT(*) AS n, COUNT(DISTINCT lower(company)) AS c FROM jobs WHERE applied_at IS NOT NULL"
        args: list = []
        if lo:
            sql, args = sql + " AND applied_at >= ?", args + [lo.isoformat()]
        if hi:
            sql, args = sql + " AND applied_at < ?", args + [hi.isoformat()]
        r = conn.execute(sql, args).fetchone()
        return {"applications": r["n"], "companies": r["c"]}

    return {
        "today": today.isoformat(),
        "week": {"start": week_start.isoformat(), "end": week_end.isoformat(), **count(week_start, week_end + timedelta(days=1))},
        "month": {"label": month_start.strftime("%B %Y"), **count(month_start, next_month)},
        "all": count(),
    }
