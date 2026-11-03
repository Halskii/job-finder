"""The shared fetch: read every job source once for everyone, keep the postings, drop the ones nobody lists any more, and queue each
user for a new search of the result.

    python main.py fetch        (run it daily, from a scheduler; the web app can also do it itself, see `fetch_every_hours`)

Postings are stored for all users with the text the source's list gives. Sites that list only titles (Workday, SmartRecruiters, ...)
store how to read the full posting, and it is read the first time someone's search selects the posting (ensure_details), so
nobody pays for thousands of requests nobody needs."""
from __future__ import annotations

import json
import logging
import time

import psycopg

import boards
import db
import filters
import sources
import userdata

log = logging.getLogger("job-finder")
LOCK = 7305422          # held while a fetch runs, so two servers (or a server and a command) never fetch at the same time
CRAWL_LOCK = 7305423     # held while a crawl runs
RETRY_AFTER = 3600       # seconds to wait before trying a crawl or a fetch again after it was started (it may have failed)
MAX_DETAIL_TRIES = 3    # a posting whose full text cannot be read this many times is left alone (it is probably gone)

STORE_SQL = ("INSERT INTO postings (id, source, company, title, location, url, posted, pay_min, pay_max, pay_unit, description, dedupe_key,"
             " formatted, detail, fetched_at, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?)"
             " ON CONFLICT (id) DO UPDATE SET last_seen = excluded.last_seen,"
             " description = CASE WHEN COALESCE(postings.description, '') = '' THEN excluded.description ELSE postings.description END,"
             " pay_min = CASE WHEN COALESCE(postings.description, '') = '' THEN excluded.pay_min ELSE postings.pay_min END,"
             " pay_max = CASE WHEN COALESCE(postings.description, '') = '' THEN excluded.pay_max ELSE postings.pay_max END,"
             " pay_unit = CASE WHEN COALESCE(postings.description, '') = '' THEN excluded.pay_unit ELSE postings.pay_unit END")


def search_titles(cfg: dict, conn) -> list[str]:
    """The titles to search for: the ones in config.yaml plus everyone's own, without repeats. Sites that search by title use these."""
    out, seen = [], set()
    titles = list(cfg.get("titles", []))
    for r in conn.execute("SELECT id FROM users").fetchall():
        titles += userdata.load_settings(r["id"])["titles"]
    for t in titles:
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def store(conn, jobs: list, max_age_days: int = 90) -> dict:
    """Keep these postings for everyone. A posting already stored is only marked as still listed (and given its text if it had none).
    Postings clearly older than `max_age_days` are not kept. Returns {"fetched", "new", "skipped_old"}."""
    now, rows, old = time.time(), [], 0
    for j in jobs:
        if j.posted and not filters.age_ok(j.posted, max_age_days):
            old += 1
            continue
        if j.description:
            sources.fill_pay(j)   # an hourly rate in the text, when no yearly pay is listed
        rows.append((j.id, j.source, j.company, j.title, j.location, j.url, j.posted, j.pay_min, j.pay_max, j.pay_unit or "year",
                     j.description or "", db.dedupe_key(j.company, j.title), json.dumps(j.detail) if j.detail else None, now, now))
    before = conn.execute("SELECT COUNT(*) AS n FROM postings").fetchone()["n"]
    for i in range(0, len(rows), 1000):
        conn.executemany(STORE_SQL, rows[i:i + 1000])
        conn.commit()
    after = conn.execute("SELECT COUNT(*) AS n FROM postings").fetchone()["n"]
    return {"fetched": len(jobs), "new": after - before, "skipped_old": old}


def _delete(conn, ids: list) -> int:
    try:
        n = conn.execute("DELETE FROM postings WHERE id = ANY(?)", (ids,)).rowcount
        conn.commit()
        return n
    except psycopg.errors.ForeignKeyViolation:   # some of these are somebody's job: leave those, delete the rest one by one
        if len(ids) == 1:
            return 0
        return sum(_delete(conn, [i]) for i in ids)


def purge(conn, days: int) -> int:
    """Delete postings no source has listed for `days` days, except any that a user has among their jobs (the database refuses to
    delete those, and they are skipped). Returns how many were deleted."""
    stale = [r["id"] for r in conn.execute("SELECT id FROM postings WHERE last_seen < ?", (time.time() - days * 86400,)).fetchall()]
    return sum(_delete(conn, stale[i:i + 200]) for i in range(0, len(stale), 200))


def ensure_details(conn, ids: list, workers: int = 16, delay: float = 0.4) -> int:
    """Read the full text of these postings from their sources, for those that were stored with only a title, and keep it for everyone.
    One server is never asked for two things at once. Returns how many could not be read this time."""
    import main
    rows = conn.execute("SELECT id, source, company, title, location, url, posted, detail FROM postings WHERE id = ANY(?)"
                        " AND COALESCE(description, '') = '' AND detail IS NOT NULL AND detail_tries < ?", (list(ids), MAX_DETAIL_TRIES)).fetchall()
    jobs = [sources.Job(r["id"], r["source"], r["company"], r["title"], r["location"] or "", r["url"] or "", r["posted"], "", detail=json.loads(r["detail"]))
            for r in rows]
    if not jobs:
        return 0
    failed = main.prefetch_details(jobs, workers, delay)
    for j in jobs:
        if j.id in failed or not j.description:
            conn.execute("UPDATE postings SET detail_tries = detail_tries + 1 WHERE id = ?", (j.id,))
            continue
        sources.fill_pay(j)
        # A site whose list shows only a rough title (Home Depot) gives the real title here, and one with several brands (Gap Inc.) the brand.
        conn.execute("UPDATE postings SET title = ?, company = ?, dedupe_key = ?, description = ?, location = ?, posted = ?, pay_min = ?, pay_max = ?,"
                     " pay_unit = ?, formatted = 1 WHERE id = ?",
                     (j.title, j.company, db.dedupe_key(j.company, j.title), j.description, j.location, j.posted, j.pay_min, j.pay_max,
                      j.pay_unit or "year", j.id))
    conn.commit()
    return sum(1 for j in jobs if j.id in failed or not j.description)


def queue_all_users(conn) -> int:
    """Mark every user who has a search set up as waiting for a new run. The web app's worker picks them up. Returns how many."""
    import pipeline
    n = 0
    for r in conn.execute("SELECT id FROM users").fetchall():
        if userdata.load_settings(r["id"])["titles"] and userdata.load_profile(r["id"]) is not None:
            pipeline.mark_queued(r["id"])
            n += 1
    return n


def due(conn, hours: float, now: float | None = None) -> bool:
    """True when the last shared fetch was more than `hours` ago (or there never was one)."""
    last = float(db.get_meta(conn, "fetch_all_last") or 0)
    return hours > 0 and (time.time() if now is None else now) - last >= hours * 3600


def run_fetch(cfg: dict | None = None, connect=None, queue_users: bool = True) -> dict:
    """Fetch every source, keep the postings, drop stale ones, and queue the users. Returns what happened. Only one fetch runs at a
    time: if another is already going, this returns {"skipped": ...} straight away."""
    import discovery
    import main
    import pipeline
    conn = (connect or pipeline._connect)(False)
    try:
        cfg = cfg if cfg is not None else discovery.load_config(main.ROOT, conn)
        if not conn.execute("SELECT pg_try_advisory_lock(?) AS ok", (LOCK,)).fetchone()["ok"]:
            return {"skipped": "another fetch is already running"}
        try:
            cfg = {**cfg, "titles": search_titles(cfg, conn)}
            started = time.time()
            db.set_meta(conn, "fetch_attempt", str(started))
            outcomes: list = []
            stats = store(conn, main.collect(cfg, conn, outcomes), int(cfg.get("store_max_age_days", 90)))
            boards.record(conn, outcomes)
            stats["boards_removed"] = len(boards.prune(conn))   # found gone fetch after fetch: no longer worth asking
            stats["purged"] = purge(conn, int(cfg.get("keep_postings_days", 14)))
            db.set_meta(conn, "fetch_all_last", str(time.time()))
            stats["queued"] = queue_all_users(conn) if queue_users else 0
            stats["seconds"] = round(time.time() - started)
            return stats
        finally:
            conn.execute("SELECT pg_advisory_unlock(?)", (LOCK,))
            conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------- finding boards, and what the web app does on its own

def _base(titles) -> set:
    return set(filters.base_title_strings(titles))


def crawled_titles(conn, cfg: dict) -> set:
    """The titles (without levels, lowercase) boards have already been searched for. The first time this is asked on a database that
    already has a crawl behind it (the crawl cache file is there), it starts from the titles in config.yaml, which that crawl used."""
    raw = db.get_meta(conn, "crawl_titles")
    if raw is None:
        import crawl
        import main
        cache = main.ROOT / crawl.CACHE_FILE
        seeded = _base(cfg.get("titles", [])) if cache.exists() else set()
        db.set_meta(conn, "crawl_titles", json.dumps(sorted(seeded)))
        db.set_meta(conn, "crawl_last", str(cache.stat().st_mtime if cache.exists() else 0))
        return seeded
    return set(json.loads(raw))


def uncrawled_titles(conn, cfg: dict) -> list[str]:
    """Titles someone searches for that no crawl has looked for boards with yet."""
    done = crawled_titles(conn, cfg)
    return [t for t in search_titles(cfg, conn) if _base([t]) - done]


def crawl_due(conn, days: float, now: float | None = None) -> bool:
    return days > 0 and (time.time() if now is None else now) - float(db.get_meta(conn, "crawl_last") or 0) >= days * 86400


def run_crawl(conn, cfg: dict, root, only_new: bool = False, systems=None, crawls: int = 4, keep_all: bool = False,
              recheck: bool = False, add: bool = True, log=print) -> dict:
    """Find boards for everyone's titles (see crawl.run). With only_new, just for the titles nobody has crawled for, asking again about
    boards rejected before, since the titles are different now. One crawl at a time. Remembers which titles have been crawled."""
    import crawl
    if not conn.execute("SELECT pg_try_advisory_lock(?) AS ok", (CRAWL_LOCK,)).fetchone()["ok"]:
        return {"skipped": "another crawl is already running"}
    try:
        everything = search_titles(cfg, conn)
        titles = uncrawled_titles(conn, cfg) if only_new else everything
        if only_new and not titles:
            return {"new": 0, "problems": [], "unavailable": False, "titles": []}
        db.set_meta(conn, "crawl_attempt", str(time.time()))
        res = crawl.run(conn, cfg, root, titles, systems, crawls, keep_all, only_new or recheck, add, log)
        res["titles"] = titles
        if add and not res["unavailable"] and not res["problems"] and not systems:   # a complete crawl: remember what it covered
            db.set_meta(conn, "crawl_titles", json.dumps(sorted(crawled_titles(conn, cfg) | _base(titles))))
            if not only_new:
                db.set_meta(conn, "crawl_last", str(time.time()))
        return res
    finally:
        conn.execute("SELECT pg_advisory_unlock(?)", (CRAWL_LOCK,))
        conn.commit()


def next_job(conn, cfg: dict, crawl_every_days: float, fetch_every_hours: float, now: float | None = None) -> str | None:
    """What the web app should do next on its own: 'crawl_new' (someone has titles no crawl covered), 'crawl' (the monthly one), 'fetch'
    (the daily one), or None. A crawl or fetch that was started less than an hour ago is not started again, in case it failed."""
    now = time.time() if now is None else now
    recent = lambda key: now - float(db.get_meta(conn, key) or 0) < RETRY_AFTER
    if not recent("crawl_attempt"):
        if uncrawled_titles(conn, cfg):
            return "crawl_new"
        if crawl_due(conn, crawl_every_days, now):
            return "crawl"
    if fetch_every_hours and due(conn, fetch_every_hours, now) and not recent("fetch_attempt"):
        return "fetch"
    return None


def run_job(job: str, connect=None) -> dict:
    """Do one thing next_job asked for. A crawl that finds new boards is followed by a fetch, so their postings are there at once."""
    import discovery
    import main
    import pipeline
    conn = (connect or pipeline._connect)(False)
    try:
        cfg = discovery.load_config(main.ROOT, conn)
        if job == "fetch":
            return {"fetch": run_fetch(cfg, connect)}
        out = {"crawl": run_crawl(conn, cfg, main.ROOT, only_new=(job == "crawl_new"), log=lambda *a: None)}
        if out["crawl"].get("new"):
            out["fetch"] = run_fetch(None, connect)
        return out
    finally:
        conn.close()
