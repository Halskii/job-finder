"""Finding and scoring one user's matches in the shared postings.

    1. match   keep the stored postings that fit the user's saved search (titles, places, pay, age) as their jobs. Free. Postings that
               come with their text are matched first; the sites that list only titles are read (once, for everyone) afterwards.
    2. score   Claude scores the best 50 at once so there is something to read straight away, then keeps going in the background
               in batches of 50, best keyword match first, until everything is scored or the day's limit is reached.

A run is queued and carried out by one background worker (the paid calls are spread over a few threads inside it), so a user who
saves their titles gets an answer while they are still on the page. Progress is kept in the user_runs table for the page to show."""
from __future__ import annotations

import hashlib
import json
import logging
import queue
import threading
import time
from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed
from types import SimpleNamespace

import db
import filters
import ingest
import scorer
import tailor
import userdata
from errors import UserError, explain

log = logging.getLogger("job-finder")

DEFAULTS = {"model": "claude-haiku-4-5-20251001", "initial_batch": 50, "batch_size": 50, "max_per_user_per_day": 400, "workers": 8}
FATAL = {"no_api_key", "bad_api_key", "no_credit", "forbidden", "missing_package"}   # a failure every further call would repeat
ACTIVE = ("queued", "matching", "scoring")
connect_as = None   # connect_as(user id, or False for no user) opens a connection; the web app sets this, and by default it is db.connect


def _connect(uid):
    return connect_as(uid) if connect_as else db.connect(user=uid)


def scoring_config() -> dict:
    return {**DEFAULTS, **(tailor.config_value("scoring", {}) or {})}


# ---------------------------------------------------------------- the user's search

class Rules:
    """A user's saved search: which stored postings fit it."""

    def __init__(self, settings: dict):
        self.patterns = filters.base_titles(settings["titles"])
        self.exclude = [w.lower() for w in settings["exclude_title_words"]]
        self.loc_excl = filters.compile_exclusions(settings["exclude_locations"])
        self.us_only, self.max_age, self.min_pay = settings["us_only"], settings["max_age_days"], settings["min_pay"]

    def fits(self, row) -> bool:
        location = row["location"] or ""
        return (filters.title_ok(row["title"] or "", self.patterns, self.exclude)
                and filters.location_ok(location, self.us_only)
                and filters.exclusion_ok(location, self.loc_excl)
                and filters.age_ok(row["posted"], self.max_age)
                and filters.pay_ok(row["pay_max"], row["pay_min"], self.min_pay, row["pay_unit"] or "year"))


def settings_key(settings: dict) -> str:
    """A short fingerprint of the search. When it changes, every stored posting is checked again, not just the new ones."""
    keys = ("titles", "exclude_title_words", "min_pay", "max_age_days", "us_only", "exclude_locations")
    return hashlib.sha1(json.dumps({k: settings[k] for k in keys}, sort_keys=True).encode()).hexdigest()[:16]


# ---------------------------------------------------------------- progress

def _save(conn, **fields) -> None:
    """Record this user's progress."""
    allowed = {"state", "message", "matched", "waiting", "scored", "settings_key", "watermark"}
    assert set(fields) <= allowed, set(fields) - allowed
    cols = list(fields)
    conn.execute(f"INSERT INTO user_runs (user_id, {', '.join(cols)}, updated) VALUES (?, {', '.join('?' * len(cols))}, ?)"
                 f" ON CONFLICT (user_id) DO UPDATE SET {', '.join(f'{c} = excluded.{c}' for c in cols)}, updated = excluded.updated",
                 (conn.user_id, *fields.values(), time.time()))
    conn.commit()


def status(conn) -> dict:
    """What the page shows: the state of this user's run and how much scoring is left today."""
    row = conn.execute("SELECT state, message, matched, waiting, scored, updated FROM user_runs WHERE user_id = ?", (conn.user_id,)).fetchone()
    out = dict(row) if row else {"state": "idle", "message": "", "matched": 0, "waiting": 0, "scored": 0, "updated": 0}
    out["left_today"] = max(0, scoring_config()["max_per_user_per_day"] - db.usage_today(conn, "score"))
    return out


# ---------------------------------------------------------------- matching

def match(conn, rules: Rules, profile: dict, since: float | None = None, read_missing: bool = False) -> tuple[int, int]:
    """Add the stored postings that fit the search to this user's jobs. With `since`, only postings fetched after that moment are
    looked at; without it, all of them. A posting already among the user's jobs is never added twice.

    A posting stored with only a title has no text yet. With read_missing=False it is left for later; with True, those that fit by title
    are read from their sites first (see ingest.ensure_details) and then judged on the full text, because that is what shows the real
    location and pay. Returns (how many were added, how many could not be read this time and must be looked at again)."""
    sql = ("SELECT id, source, company, title, location, posted, pay_min, pay_max, pay_unit, COALESCE(description, '') <> '' AS has_text,"
           " detail IS NOT NULL AND detail_tries < ? AS can_read FROM postings p"
           " WHERE NOT EXISTS (SELECT 1 FROM user_jobs u WHERE u.job_id = p.id AND u.user_id = ?)")
    args: list = [ingest.MAX_DETAIL_TRIES, conn.user_id]
    if since is not None:
        sql, args = sql + " AND p.fetched_at > ?", args + [since]
    rows = conn.execute(sql, args).fetchall()
    fits = [r for r in rows if rules.fits(r)]
    unread = 0
    if read_missing:
        need = [r["id"] for r in fits if not r["has_text"] and r["can_read"]]
        if need:
            ingest.ensure_details(conn, need)
            fresh = {r["id"]: r for r in conn.execute(
                "SELECT id, source, company, title, location, posted, pay_min, pay_max, pay_unit, COALESCE(description, '') <> '' AS has_text,"
                " detail IS NOT NULL AND detail_tries < ? AS can_read FROM postings WHERE id = ANY(?)", (ingest.MAX_DETAIL_TRIES, need)).fetchall()}
            fits = [fresh.get(r["id"], r) if r["id"] in fresh else r for r in fits]
            fits = [r for r in fits if r["id"] not in fresh or rules.fits(r)]   # the full text may show it is somewhere else, or pays too little
        unread = sum(1 for r in fits if not r["has_text"] and r["can_read"])
    added = 0
    for row in fits:
        if not row["has_text"]:
            continue
        if db.is_cross_source_duplicate(conn, SimpleNamespace(company=row["company"], title=row["title"], source=row["source"])):
            continue
        description = conn.execute("SELECT description FROM postings WHERE id = ?", (row["id"],)).fetchone()["description"]
        added += conn.execute("INSERT INTO user_jobs (user_id, job_id, quick_score, first_seen) VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
                              (conn.user_id, row["id"], filters.quick_score(description or "", profile), time.time())).rowcount
    conn.commit()
    return added, unread


def pending(conn, rules: Rules, skip: set) -> list:
    """The ids of this user's unscored jobs that still fit their search, best keyword match first."""
    rows = conn.execute("SELECT id, location, title, posted, pay_min, pay_max, pay_unit, description IS NOT NULL AND description <> '' AS has_text"
                        " FROM jobs WHERE score IS NULL AND status = 'new' ORDER BY quick_score DESC NULLS LAST").fetchall()
    return [r["id"] for r in rows if r["id"] not in skip and r["has_text"] and rules.fits(r)]


# ---------------------------------------------------------------- scoring

def score_batches(conn, rules: Rules, profile: dict, settings: dict, scfg: dict, client, resume_text: str, first: bool = True,
                  only_first: bool = False, skip: set | None = None) -> str:
    """Score this user's unscored jobs, the first batch of `initial_batch` and then batches of `batch_size`, until none are left, the
    day's limit is reached, or a failure that would repeat for every call (no key, no credit). With only_first, stops after the first
    batch. Returns 'complete', 'limit', 'error', or 'more' (stopped after the first batch with jobs still waiting)."""
    skip = skip if skip is not None else set()   # jobs that failed: not tried again, even in a later call of this run
    row = conn.execute("SELECT scored FROM user_runs WHERE user_id = ?", (conn.user_id,)).fetchone()
    scored = row["scored"] if row else 0
    while True:
        left = scfg["max_per_user_per_day"] - db.usage_today(conn, "score")
        todo_ids = pending(conn, rules, skip)
        _save(conn, waiting=len(todo_ids), scored=scored)
        if not todo_ids:
            return "complete"
        if left <= 0:
            return "limit"
        batch = todo_ids[:min(scfg["initial_batch"] if first else scfg["batch_size"], left)]
        first = False
        rows = {r["id"]: r for r in conn.execute("SELECT * FROM jobs WHERE id = ANY(?)", (batch,)).fetchall()}
        ok = tok_in = tok_out = 0
        fatal = None
        with ThreadPoolExecutor(max_workers=max(1, int(scfg["workers"]))) as pool:
            futures = {pool.submit(scorer.score_job, client, scfg["model"], profile, rows[i], settings["min_pay"], resume_text): i
                       for i in batch}
            for fut in as_completed(futures):
                job_id = futures[fut]
                try:
                    res = fut.result()
                except CancelledError:   # not started: a failure that every call would repeat stopped the batch
                    continue
                except Exception as ex:
                    message, code = explain(ex)
                    skip.add(job_id)   # not tried again in this run
                    if code in FATAL and fatal is None:
                        fatal = message
                        for f in futures:
                            f.cancel()
                    continue
                db.save_score(conn, job_id, res["fit_score"], res["verdict"], res)
                ok, tok_in, tok_out, scored = ok + 1, tok_in + res.get("_in", 0), tok_out + res.get("_out", 0), scored + 1
                _save(conn, scored=scored, waiting=max(0, len(todo_ids) - ok))
        if ok:
            db.add_usage(conn, "score", ok, tok_in, tok_out)
        if fatal:
            _save(conn, state="error", message=fatal)
            return "error"
        if only_first:
            return "more"


def run_user(uid: int, client=None) -> dict:
    """Match, then score, for one user. Returns their status. Problems the person can fix are shown on the page, not raised."""
    conn = _connect(uid)
    try:
        settings, profile = userdata.load_settings(uid), userdata.load_profile(uid)
        if not settings["titles"] or profile is None:
            _save(conn, state="idle", message="Add your job titles and profile first.")
            return status(conn)
        rules, key = Rules(settings), settings_key(settings)
        row = conn.execute("SELECT settings_key, watermark FROM user_runs WHERE user_id = ?", (conn.user_id,)).fetchone()
        since = row["watermark"] if row and row["settings_key"] == key else None   # a changed search is checked against everything
        _save(conn, state="matching", message="", scored=0)
        started = time.time()
        match(conn, rules, profile, since)                       # the postings that came with their text: quick
        _save(conn, matched=conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"], state="scoring")
        import main
        resume_text, scfg = main.load_resume(userdata.home(uid)), scoring_config()
        outcome, skip = "complete", set()
        if pending(conn, rules, skip):
            try:
                client = client or tailor.make_client()
            except UserError as ex:
                _save(conn, state="error", message=ex.message)
                return status(conn)
            outcome = score_batches(conn, rules, profile, settings, scfg, client, resume_text, only_first=True, skip=skip)   # something to read at once
            if outcome == "error":
                return status(conn)
        _save(conn, state="matching", message="")
        _, unread = match(conn, rules, profile, since, read_missing=True)   # then the sites that list only titles
        _save(conn, matched=conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"], state="scoring",
              settings_key=key, watermark=since if unread else started)   # anything that could not be read is looked at again next time
        if pending(conn, rules, skip):
            try:
                client = client or tailor.make_client()
            except UserError as ex:
                _save(conn, state="error", message=ex.message)
                return status(conn)
            outcome = score_batches(conn, rules, profile, settings, scfg, client, resume_text, first=False, skip=skip)
        if outcome == "limit":
            waiting = len(pending(conn, rules, skip))
            _save(conn, state="done", message=f"Today's scoring limit is reached. The other {waiting} matches are scored tomorrow.", waiting=waiting)
        elif outcome != "error":
            _save(conn, state="done", message="", waiting=0)
        return status(conn)
    finally:
        conn.close()


# ---------------------------------------------------------------- the background worker

class Worker:
    """Runs queued users one at a time on a background thread. A user asked for again while their run is going is run once more after."""

    def __init__(self):
        self.q: queue.Queue = queue.Queue()
        self.waiting: set[int] = set()
        self.again: set[int] = set()
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None

    def submit(self, uid: int) -> None:
        with self.lock:
            if uid in self.waiting:
                self.again.add(uid)
                return
            self.waiting.add(uid)
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._loop, name="pipeline", daemon=True)
                self.thread.start()
        self.q.put(uid)

    def _loop(self) -> None:
        while True:
            uid = self.q.get()
            try:
                run_user(uid)
            except Exception as ex:   # never let one person's problem stop everyone else's
                message, code = explain(ex)
                log.warning("Run for user %s stopped: %s (%s)", uid, code, ex)
                try:
                    conn = _connect(uid)
                    try:
                        _save(conn, state="error", message=message)
                    finally:
                        conn.close()
                except Exception:
                    pass
            finally:
                with self.lock:
                    again = uid in self.again
                    self.again.discard(uid)
                    if not again:
                        self.waiting.discard(uid)
                if again:
                    self.q.put(uid)


WORKER = Worker()


def request(uid: int) -> None:
    """Queue a run for this user (their saved search changed, or they asked for a check). Their page shows 'queued' at once."""
    conn = _connect(uid)
    try:
        _save(conn, state="queued", message="")
    finally:
        conn.close()
    WORKER.submit(uid)


def mark_queued(uid: int) -> None:
    """Say in the database that this user is waiting for a run, without running it here. Whichever web server is running picks it up."""
    conn = _connect(uid)
    try:
        _save(conn, state="queued", message="")
    finally:
        conn.close()


def poll_queued() -> int:
    """Hand the worker every user who is marked as waiting (for example by the daily fetch, which may run in another process)."""
    conn = _connect(False)
    try:
        ids = [r["user_id"] for r in conn.execute("SELECT user_id FROM user_runs WHERE state = 'queued'").fetchall()]
    finally:
        conn.close()
    for uid in ids:
        WORKER.submit(uid)
    return len(ids)


def resume_unfinished() -> int:
    """Queue the runs that were in progress when the server last stopped. Returns how many."""
    conn = _connect(False)
    try:
        ids = [r["user_id"] for r in conn.execute("SELECT user_id FROM user_runs WHERE state IN ('queued', 'matching', 'scoring')").fetchall()]
    finally:
        conn.close()
    for uid in ids:
        WORKER.submit(uid)
    return len(ids)
