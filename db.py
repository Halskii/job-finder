"""PostgreSQL storage: postings shared by everyone, and each user's own scores, statuses, notes, interview rounds and tips.

  postings                    one row per job posting, fetched once for all users (no user data in it)
  user_jobs                   one row per (user, posting) the user matched: score, verdict, status, notes, applied_at
  interview_rounds / _tips    a user's own interview tracking and prep
  users                       accounts
  jobs                        a VIEW: this connection's user's postings and their own columns side by side. Reads use it as
                              the old single-user `jobs` table, and an UPDATE on it is routed to the right table.

Isolation is enforced by the database, not only by the code: user_jobs, interview_rounds and interview_tips have row-level
security, and every connection says whose data it is (connect(user=...)). A query that forgets to filter by user still
sees only that user's rows."""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import date, timedelta

import psycopg

LOCAL_USER = "local@localhost"   # the one account the single-user tools (CLI, viewer without sign-in) work as

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id BIGSERIAL PRIMARY KEY, email TEXT NOT NULL, password_hash TEXT, google_sub TEXT, name TEXT NOT NULL DEFAULT '',
  created_at DOUBLE PRECISION NOT NULL DEFAULT extract(epoch FROM now())
);
CREATE UNIQUE INDEX IF NOT EXISTS users_email ON users (lower(email));
CREATE UNIQUE INDEX IF NOT EXISTS users_google ON users (google_sub) WHERE google_sub IS NOT NULL;
ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified BOOLEAN NOT NULL DEFAULT FALSE;
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at DOUBLE PRECISION NOT NULL, expires_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions (user_id);
CREATE TABLE IF NOT EXISTS login_failures (id BIGSERIAL PRIMARY KEY, email TEXT NOT NULL, ip TEXT, at DOUBLE PRECISION NOT NULL);
CREATE INDEX IF NOT EXISTS login_failures_email ON login_failures (email, at);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS postings (
  id TEXT PRIMARY KEY, source TEXT, company TEXT, title TEXT, location TEXT, url TEXT, posted TEXT,
  pay_min DOUBLE PRECISION, pay_max DOUBLE PRECISION, pay_unit TEXT DEFAULT 'year',
  description TEXT, formatted INTEGER DEFAULT 1, dedupe_key TEXT,
  fetched_at DOUBLE PRECISION, last_seen DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS postings_dedupe ON postings (dedupe_key);
ALTER TABLE postings ADD COLUMN IF NOT EXISTS detail TEXT;                              -- JSON: what a source needs to read the full posting later
ALTER TABLE postings ADD COLUMN IF NOT EXISTS detail_tries INTEGER NOT NULL DEFAULT 0;  -- failed attempts to read it; after a few it is left alone
CREATE INDEX IF NOT EXISTS postings_last_seen ON postings (last_seen);
CREATE INDEX IF NOT EXISTS postings_fetched ON postings (fetched_at);

CREATE TABLE IF NOT EXISTS user_jobs (
  user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  job_id TEXT NOT NULL REFERENCES postings(id),
  quick_score DOUBLE PRECISION, score INTEGER, verdict TEXT, analysis TEXT,
  status TEXT NOT NULL DEFAULT 'new', first_seen DOUBLE PRECISION, notes TEXT, applied_at TEXT,
  PRIMARY KEY (user_id, job_id)
);
CREATE TABLE IF NOT EXISTS interview_rounds (
  id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL, job_id TEXT NOT NULL, kind TEXT NOT NULL, round_date TEXT,
  outcome TEXT NOT NULL DEFAULT 'scheduled', notes TEXT DEFAULT '',
  FOREIGN KEY (user_id, job_id) REFERENCES user_jobs (user_id, job_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS interview_tips (
  user_id BIGINT NOT NULL, job_id TEXT NOT NULL, content TEXT NOT NULL, model TEXT, created TEXT,
  PRIMARY KEY (user_id, job_id),
  FOREIGN KEY (user_id, job_id) REFERENCES user_jobs (user_id, job_id) ON DELETE CASCADE
);

-- Company job boards found by `main.py crawl`, `discover` and `workday-add`, shared by everyone. `fails` counts fetches in a row that found
-- the board gone, and a board is dropped after a few.
CREATE TABLE IF NOT EXISTS boards (
  system TEXT NOT NULL, ident TEXT NOT NULL, entry TEXT NOT NULL,   -- entry: JSON, the board's name on its site, or a Workday site's details
  added DOUBLE PRECISION NOT NULL, fails INTEGER NOT NULL DEFAULT 0, last_ok DOUBLE PRECISION, origin TEXT NOT NULL DEFAULT 'crawl',
  PRIMARY KEY (system, ident)
);
-- Where each user's matching and scoring has got to. Only counters and a state word, so it has no row-level security: the background
-- worker has to see every user's row to pick up unfinished work, and the web app always filters by the signed-in user.
CREATE TABLE IF NOT EXISTS user_runs (
  user_id BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  state TEXT NOT NULL DEFAULT 'idle',            -- idle, queued, matching, scoring, done, error
  message TEXT NOT NULL DEFAULT '',
  matched INTEGER NOT NULL DEFAULT 0,            -- postings that fit this user's search, all time
  waiting INTEGER NOT NULL DEFAULT 0,            -- of those, not scored yet
  scored INTEGER NOT NULL DEFAULT 0,             -- scored in the current run
  settings_key TEXT,                             -- which version of the search the postings were last checked against
  watermark DOUBLE PRECISION,                    -- postings fetched after this have not been checked yet
  updated DOUBLE PRECISION NOT NULL DEFAULT 0
);
-- What each user's paid calls add up to, per day and kind of call (scoring, ...), for limits and for knowing what a user costs.
CREATE TABLE IF NOT EXISTS usage (
  user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE, day TEXT NOT NULL, kind TEXT NOT NULL,
  calls INTEGER NOT NULL DEFAULT 0, tokens_in BIGINT NOT NULL DEFAULT 0, tokens_out BIGINT NOT NULL DEFAULT 0,
  PRIMARY KEY (user_id, day, kind)
);

CREATE OR REPLACE VIEW jobs WITH (security_invoker = true) AS
  SELECT p.id, p.source, p.company, p.title, p.location, p.url, p.posted, p.pay_min, p.pay_max, p.pay_unit,
         p.description, p.formatted, p.dedupe_key,
         u.quick_score, u.score, u.verdict, u.analysis, u.status, u.first_seen, u.notes, u.applied_at
  FROM postings p JOIN user_jobs u ON u.job_id = p.id;

CREATE OR REPLACE FUNCTION jobs_update() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  UPDATE user_jobs SET quick_score = NEW.quick_score, score = NEW.score, verdict = NEW.verdict, analysis = NEW.analysis,
         status = NEW.status, first_seen = NEW.first_seen, notes = NEW.notes, applied_at = NEW.applied_at
   WHERE job_id = OLD.id AND user_id = current_setting('app.user_id')::bigint;
  IF (NEW.source, NEW.company, NEW.title, NEW.location, NEW.url, NEW.posted, NEW.pay_min, NEW.pay_max, NEW.pay_unit,
      NEW.description, NEW.formatted, NEW.dedupe_key)
     IS DISTINCT FROM (OLD.source, OLD.company, OLD.title, OLD.location, OLD.url, OLD.posted, OLD.pay_min, OLD.pay_max, OLD.pay_unit,
      OLD.description, OLD.formatted, OLD.dedupe_key) THEN
    UPDATE postings SET source = NEW.source, company = NEW.company, title = NEW.title, location = NEW.location, url = NEW.url,
           posted = NEW.posted, pay_min = NEW.pay_min, pay_max = NEW.pay_max, pay_unit = NEW.pay_unit,
           description = NEW.description, formatted = NEW.formatted, dedupe_key = NEW.dedupe_key
     WHERE id = OLD.id;
  END IF;
  RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS jobs_update ON jobs;
CREATE TRIGGER jobs_update INSTEAD OF UPDATE ON jobs FOR EACH ROW EXECUTE FUNCTION jobs_update();
"""

# Tables a user owns. Each gets a policy that only lets a connection see and write the rows of the user it was opened for.
PRIVATE_TABLES = ("user_jobs", "interview_rounds", "interview_tips", "usage")
_OWN_ROWS = "user_id = nullif(current_setting('app.user_id', true), '')::bigint"


def dedupe_key(company: str, title: str) -> str:
    """Company + title with everything but letters and digits removed, so 'Stripe' / 'stripe' and
    'Software Engineer, I' / 'Software Engineer I' compare equal."""
    norm = lambda t: re.sub(r"[^a-z0-9]+", "", (t or "").lower())
    return f"{norm(company)}|{norm(title)}"


class Row(dict):
    """A result row: readable by column name, by position, and as a slice, like sqlite3.Row (iterating gives the values)."""
    __slots__ = ()

    def __getitem__(self, key):
        if isinstance(key, (int, slice)):
            return tuple(self.values())[key]
        return dict.__getitem__(self, key)

    def __iter__(self):
        return iter(self.values())


def _row_factory(cursor):
    names = [c.name for c in cursor.description] if cursor.description else []
    return lambda values: Row(zip(names, values))


class Conn:
    """A database connection for one user. SQL is written with ? placeholders; a failed statement rolls back so the
    connection stays usable."""

    def __init__(self, raw: psycopg.Connection, user_id: int):
        self.raw, self.user_id = raw, user_id

    def execute(self, sql: str, args=()):
        try:
            return self.raw.execute(sql.replace("%", "%%").replace("?", "%s"), tuple(args))
        except Exception:
            self.raw.rollback()
            raise

    def executemany(self, sql: str, rows) -> None:
        """One statement for many rows, sent together (much faster than one by one)."""
        try:
            with self.raw.cursor() as cur:
                cur.executemany(sql.replace("%", "%%").replace("?", "%s"), [tuple(r) for r in rows])
        except Exception:
            self.raw.rollback()
            raise

    def commit(self) -> None:
        self.raw.commit()

    def rollback(self) -> None:
        self.raw.rollback()

    def close(self) -> None:
        self.raw.close()


_ready: set = set()
_ready_lock = threading.Lock()


def database_url() -> str:
    """DATABASE_URL from the environment or the .env file."""
    if not os.environ.get("DATABASE_URL"):
        import main
        main.load_env()
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set. Add it to the .env file (see the README).")
    return url


def _ensure_schema(raw: psycopg.Connection) -> None:
    """Create the tables, the view and the row-level security policies if they are not there yet. Safe to run again."""
    raw.execute("SELECT pg_advisory_xact_lock(7305421)")   # two processes starting together do not both create things
    raw.execute(SCHEMA)
    for table in PRIVATE_TABLES:
        raw.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        raw.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        raw.execute(f"DROP POLICY IF EXISTS own_rows ON {table}")
        raw.execute(f"CREATE POLICY own_rows ON {table} USING ({_OWN_ROWS}) WITH CHECK ({_OWN_ROWS})")
    raw.commit()


def user_id_for(conn_or_raw, who) -> int:
    """The id of an account, given its email or id. An email that has no account yet gets one (used by the single-user tools)."""
    raw = getattr(conn_or_raw, "raw", conn_or_raw)
    if isinstance(who, int):
        return who
    row = raw.execute("SELECT id FROM users WHERE lower(email) = lower(%s)", (who,)).fetchone()
    if row is None:
        row = raw.execute("INSERT INTO users (email) VALUES (%s) ON CONFLICT (lower(email)) DO UPDATE SET email = users.email"
                          " RETURNING id", (who,)).fetchone()
    raw.commit()
    return row["id"] if isinstance(row, dict) else row[0]


def connect(url: str | None = None, *, schema: str | None = None, user: int | str | None | bool = None) -> Conn:
    """Open a connection that works as one user (an id or an email; by default the local account). `schema` puts the tables in
    a schema of their own instead of the default one; the tests use it to get a fresh database each.
    user=False opens a connection with no user, for signing in: it sees no user data at all, only accounts and sessions."""
    url = url or database_url()
    if schema and not re.fullmatch(r"[a-z0-9_]{1,60}", schema):
        raise ValueError("bad schema name")
    raw = psycopg.connect(url, row_factory=_row_factory)
    try:
        if schema:
            raw.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
            raw.execute(f'SET search_path TO "{schema}"')
            raw.commit()
        key = (url, schema)
        with _ready_lock:
            if key not in _ready:
                _ensure_schema(raw)
                _ready.add(key)
        if user is False:
            uid = None
        else:
            uid = user_id_for(raw, user if user is not None else os.environ.get("JOBFINDER_USER", LOCAL_USER))
        raw.execute("SELECT set_config('app.user_id', %s, false)", (str(uid) if uid is not None else "",))
        raw.commit()
    except Exception:
        raw.close()
        raise
    return Conn(raw, uid)


def get_meta(conn, key: str) -> str | None:
    """A setting shared by everyone, such as when a source was last fetched."""
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    conn.commit()


def is_cross_source_duplicate(conn, job) -> bool:
    """True when the same company and title is already among this user's jobs from a DIFFERENT source (for example a job
    that appears on both a company's own board and an aggregator). Within one source, equal titles are kept:
    'Software Engineer' in Austin and in Chicago are two real postings."""
    return conn.execute("SELECT 1 FROM jobs WHERE dedupe_key=? AND source<>? LIMIT 1",
                        (dedupe_key(job.company, job.title), job.source)).fetchone() is not None


def add_job(conn, job, quick: float) -> bool:
    """Store a posting (once, for everyone) and add it to this user's jobs. False when the user already has it, so a job
    seen on an earlier run is never added twice."""
    now = time.time()
    conn.execute(
        "INSERT INTO postings (id, source, company, title, location, url, posted, pay_min, pay_max, description,"
        " dedupe_key, formatted, pay_unit, fetched_at, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?,?,?)"
        " ON CONFLICT (id) DO UPDATE SET last_seen = excluded.last_seen",
        (job.id, job.source, job.company, job.title, job.location, job.url, job.posted, job.pay_min, job.pay_max,
         job.description, dedupe_key(job.company, job.title), job.pay_unit, now, now))
    cur = conn.execute("INSERT INTO user_jobs (user_id, job_id, quick_score, first_seen) VALUES (?,?,?,?) ON CONFLICT DO NOTHING",
                       (conn.user_id, job.id, quick, now))
    conn.commit()
    return cur.rowcount == 1


def unscored(conn, limit: int):
    """Jobs not yet scored, best keyword match first, so a limited number of API calls goes to the likeliest fits."""
    return conn.execute(
        "SELECT * FROM jobs WHERE score IS NULL AND status='new' ORDER BY quick_score DESC NULLS LAST LIMIT ?", (limit,)
    ).fetchall()


def save_score(conn, job_id: str, score: int, verdict: str, analysis: dict) -> None:
    """Record the model's answer; the full analysis is kept as JSON for the report and viewer."""
    conn.execute("UPDATE user_jobs SET score=?, verdict=?, analysis=? WHERE job_id=?",
                 (score, verdict, json.dumps(analysis), job_id))
    conn.commit()


def top(conn, n: int, min_score: int):
    """Best scored jobs still open to you (new or saved, not applied or skipped), highest score first."""
    return conn.execute(
        "SELECT * FROM jobs WHERE score IS NOT NULL AND score>=? AND status IN ('new','saved')"
        " ORDER BY score DESC, quick_score DESC NULLS LAST LIMIT ?", (min_score, n)
    ).fetchall()


def saved(conn):
    """Every job you saved to look at later, best score first, whichever report it came from."""
    return conn.execute("SELECT * FROM jobs WHERE status='saved' ORDER BY score DESC NULLS LAST, first_seen DESC").fetchall()


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
        f"UPDATE user_jobs SET status=?, notes=COALESCE(NULLIF(?, ''), notes), applied_at={applied} WHERE {where}",
        (status, note, *extra, *where_args))
    conn.commit()
    return cur.rowcount


def set_status(conn, id_part: str, status: str, note: str = "", today: date | None = None) -> int:
    """Update every job whose id contains `id_part` (used by `main.py mark`)."""
    return _update_status(conn, "job_id ILIKE ?", (f"%{id_part}%",), status, note, today)


def set_status_exact(conn, job_id: str, status: str, note: str = "", today: date | None = None) -> int:
    """Update the one job with exactly this id (used by the web viewer)."""
    return _update_status(conn, "job_id = ?", (job_id,), status, note, today)


def set_applied_date(conn, job_id: str, day: date) -> int:
    """Change the date of an application that is already in the tracker. Does nothing for a job that is not."""
    cur = conn.execute("UPDATE user_jobs SET applied_at=? WHERE job_id=? AND applied_at IS NOT NULL", (day.isoformat(), job_id))
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
        n = conn.execute("SELECT COUNT(*) AS n FROM interview_rounds WHERE job_id=?", (job_id,)).fetchone()["n"]
        kind = f"Round {n + 1}"
    row = conn.execute("INSERT INTO interview_rounds (user_id, job_id, kind, round_date, outcome, notes) VALUES (?,?,?,?,?,?)"
                       " RETURNING id", (conn.user_id, job_id, kind, day, outcome, notes)).fetchone()
    conn.commit()
    return row["id"]


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
    return conn.execute("SELECT * FROM jobs WHERE status='skipped' ORDER BY score DESC NULLS LAST, first_seen DESC").fetchall()


def jobs_with_tips(conn) -> list:
    """Every job that has saved interview prep, newest prep first."""
    return conn.execute("SELECT jobs.*, t.created AS prep_created FROM jobs JOIN interview_tips t ON t.job_id = jobs.id"
                        " ORDER BY t.created DESC, jobs.score DESC NULLS LAST").fetchall()


def save_tips(conn, job_id: str, content: dict, model: str, created: str) -> None:
    conn.execute("INSERT INTO interview_tips (user_id, job_id, content, model, created) VALUES (?,?,?,?,?)"
                 " ON CONFLICT (user_id, job_id) DO UPDATE SET content=excluded.content, model=excluded.model, created=excluded.created",
                 (conn.user_id, job_id, json.dumps(content), model, created))
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


def add_usage(conn, kind: str, calls: int = 1, tokens_in: int = 0, tokens_out: int = 0) -> None:
    """Add to today's total of paid calls of one kind for this user."""
    conn.execute("INSERT INTO usage (user_id, day, kind, calls, tokens_in, tokens_out) VALUES (?, ?, ?, ?, ?, ?)"
                 " ON CONFLICT (user_id, day, kind) DO UPDATE SET calls = usage.calls + excluded.calls,"
                 " tokens_in = usage.tokens_in + excluded.tokens_in, tokens_out = usage.tokens_out + excluded.tokens_out",
                 (conn.user_id, date.today().isoformat(), kind, calls, tokens_in, tokens_out))
    conn.commit()


def usage_today(conn, kind: str) -> int:
    """How many paid calls of this kind this user has made today."""
    row = conn.execute("SELECT calls FROM usage WHERE day = ? AND kind = ?", (date.today().isoformat(), kind)).fetchone()
    return row["calls"] if row else 0


def unscored_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) AS n FROM jobs WHERE score IS NULL AND status = 'new'").fetchone()["n"]
