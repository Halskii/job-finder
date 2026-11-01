"""Copy an old single-user jobs.db (SQLite) into PostgreSQL as one user's data.

    python migrate_sqlite.py                      # jobs.db into the local account (local@localhost)
    python migrate_sqlite.py old.db me@example.com

The postings go into the shared postings table; scores, statuses, notes, applied dates, interview rounds and interview prep go
to that user. Running it again adds nothing twice. The SQLite file is only read."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import db

POSTING = ("id", "source", "company", "title", "location", "url", "posted", "pay_min", "pay_max", "pay_unit",
           "description", "formatted", "dedupe_key")
MINE = ("quick_score", "score", "verdict", "analysis", "status", "first_seen", "notes", "applied_at")


def migrate(sqlite_path: str, conn) -> dict:
    """Copy everything from the SQLite file into this connection's user. Returns the counts copied."""
    src = sqlite3.connect(f"file:{Path(sqlite_path).resolve().as_posix()}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    have = {r["name"] for r in src.execute("PRAGMA table_info(jobs)")}
    count = {"postings": 0, "jobs": 0, "rounds": 0, "tips": 0}
    for r in src.execute("SELECT * FROM jobs"):
        row = {k: (r[k] if k in have else None) for k in POSTING + MINE}
        row["pay_unit"] = row["pay_unit"] or "year"
        row["formatted"] = row["formatted"] if row["formatted"] is not None else 0
        row["dedupe_key"] = row["dedupe_key"] or db.dedupe_key(row["company"], row["title"])
        row["status"] = row["status"] or "new"
        seen = row["first_seen"]
        conn.execute(f"INSERT INTO postings ({', '.join(POSTING)}, fetched_at, last_seen) VALUES ({', '.join('?' * len(POSTING))}, ?, ?)"
                     " ON CONFLICT (id) DO NOTHING", (*[row[k] for k in POSTING], seen, seen))
        count["postings"] += 1
        cur = conn.execute(f"INSERT INTO user_jobs (user_id, job_id, {', '.join(MINE)}) VALUES (?, ?, {', '.join('?' * len(MINE))})"
                           " ON CONFLICT DO NOTHING", (conn.user_id, row["id"], *[row[k] for k in MINE]))
        count["jobs"] += cur.rowcount
    tables = {r["name"] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "interview_rounds" in tables:
        for r in src.execute("SELECT * FROM interview_rounds ORDER BY id"):
            dup = conn.execute("SELECT 1 FROM interview_rounds WHERE job_id=? AND kind=? AND round_date IS NOT DISTINCT FROM ?"
                               " AND outcome=? AND notes IS NOT DISTINCT FROM ?",
                               (r["job_id"], r["kind"], r["round_date"], r["outcome"], r["notes"])).fetchone()
            if not dup:
                conn.execute("INSERT INTO interview_rounds (user_id, job_id, kind, round_date, outcome, notes) VALUES (?,?,?,?,?,?)",
                             (conn.user_id, r["job_id"], r["kind"], r["round_date"], r["outcome"], r["notes"]))
                count["rounds"] += 1
    if "interview_tips" in tables:
        for r in src.execute("SELECT * FROM interview_tips"):
            cur = conn.execute("INSERT INTO interview_tips (user_id, job_id, content, model, created) VALUES (?,?,?,?,?)"
                               " ON CONFLICT DO NOTHING", (conn.user_id, r["job_id"], r["content"], r["model"], r["created"]))
            count["tips"] += cur.rowcount
    if "meta" in tables:
        for r in src.execute("SELECT key, value FROM meta WHERE key LIKE '%\\_last\\_fetch' ESCAPE '\\'"):   # when each source was last read
            conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO NOTHING", (r["key"], r["value"]))
    conn.commit()
    src.close()
    return count


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).parent / "jobs.db")
    who = sys.argv[2] if len(sys.argv) > 2 else None
    if not Path(path).exists():
        sys.exit(f"{path} not found.")
    conn = db.connect(user=who)
    count = migrate(path, conn)
    mine = conn.execute("SELECT COUNT(*) AS n FROM user_jobs").fetchone()["n"]
    print(f"Copied into the account {who or db.LOCAL_USER}: {count['jobs']} new jobs, {count['rounds']} interview rounds, "
          f"{count['tips']} interview prep notes. That account now has {mine} jobs.")


if __name__ == "__main__":
    main()
