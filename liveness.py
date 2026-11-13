"""Is a posting still on its employer's site? Postings a user has stay in the database after the site takes them down (a posting a user has is
never deleted), so a list built from old fetches fills up with jobs that are gone. This asks each posting's own source, the same public
requests the fetch makes, and remembers the answer on the shared posting (`closed_at`, `checked_at`):

  Greenhouse, Lever, Ashby,   the company's whole board comes back in one request; a stored job that is not on it has been taken down
  Recruitee, Personio, Gem
  Workday                     the posting's own detail request: 403 (or 404 / 410) is how it answers for a posting no longer on the board
  Rippling, Workable,         the posting's own detail request: 404 / 410
  BambooHR

Anything else (Jooble, the job boards that collect from many sites, each retailer's own site, SmartRecruiters, whose robots.txt turns programs
away) cannot be asked this way, so those postings are never marked: not knowing is not the same as closed. A request that fails for any other reason (a timeout, a server error) is not an answer
either, so the posting is left as it was and asked about again next time.

A posting a fetch lists again is open again (see db.py and ingest.py), so a mistake here does not last.

Taken-down jobs are left out of the matches (db.top) and are not scored (pipeline.pending): they stay in the Saved list and the tracker, marked."""
from __future__ import annotations

import time

import reformat
from errors import log

OPEN, CLOSED, UNKNOWN = "open", "closed", "unknown"
CHECKABLE = set(reformat.BOARDS) | {"wd", "rp", "wk", "bb"}   # the id prefixes (see reformat.py) of the sources that can be asked
RECHECK_AFTER = 20 * 3600     # a posting asked about more recently than this is not asked about again
BLOCKED_AFTER = 6             # a Workday site with this many jobs all answering 403 is refusing us, not closed: nothing is marked there
DELAY = 0.4                   # seconds between requests (the same politeness as a fetch)


def prefix_of(job_id: str) -> str:
    return job_id.split(":", 1)[0]


def can_check(job_id: str) -> bool:
    """True when this job's source can be asked whether it still has the posting."""
    return prefix_of(job_id) in CHECKABLE


def check_board(prefix: str, slug: str, ids: list[str]) -> dict[str, str]:
    """{job id: state} for stored jobs of one company's board, from one request for the whole board."""
    module, name = reformat.BOARDS[prefix]
    try:
        have = {j.id for j in getattr(module, name)(slug)}
    except Exception as ex:
        if reformat._is_gone(ex, prefix):          # the whole board is gone
            return {i: CLOSED for i in ids}
        log.warning("Could not check the %s board %s: %s", prefix, slug, ex)
        return {i: UNKNOWN for i in ids}
    return {i: OPEN if i in have else CLOSED for i in ids}


def check_one(row) -> str:
    """The state of one job from its own detail request."""
    prefix = prefix_of(row["id"])
    if prefix == "wd" and not reformat.WORKDAY_URL.match(row["url"] or ""):
        return UNKNOWN                              # its address does not say where to ask
    try:
        reformat.SINGLES[prefix](row)
    except Exception as ex:
        if reformat._is_gone(ex, prefix):
            return CLOSED
        log.warning("Could not check %s: %s", row["id"], ex)
        return UNKNOWN
    return OPEN


def _tasks(rows) -> dict:
    """Group the jobs by what has to be asked: a board once however many of its jobs there are, any other job on its own. In the order of
    the first (best) job of each."""
    tasks: dict[tuple, list] = {}
    for r in rows:
        prefix = prefix_of(r["id"])
        if prefix in reformat.BOARDS:
            tasks.setdefault(("board", prefix, r["id"].split(":", 2)[1]), []).append(r)
        elif prefix in CHECKABLE:
            tasks[("one", r["id"])] = [r]
    return tasks


def _unblock(results: dict[str, str]) -> None:
    """A Workday site that answered 403 for every one of several jobs is refusing requests rather than having closed them all: forget those."""
    sites: dict[str, list[str]] = {}
    for job_id in results:
        if prefix_of(job_id) == "wd":
            sites.setdefault(job_id.split(":", 2)[1], []).append(job_id)
    for ids in sites.values():
        if len(ids) >= BLOCKED_AFTER and all(results[i] == CLOSED for i in ids):
            for i in ids:
                results[i] = UNKNOWN


def record(conn, results: dict[str, str], now: float | None = None) -> None:
    """Remember the answers on the shared postings. An unknown answer changes nothing."""
    now = time.time() if now is None else now
    for job_id, state in results.items():
        if state == CLOSED:
            conn.execute("UPDATE postings SET closed_at = COALESCE(closed_at, ?), checked_at = ? WHERE id = ?", (now, now, job_id))
        elif state == OPEN:
            conn.execute("UPDATE postings SET closed_at = NULL, checked_at = ? WHERE id = ?", (now, job_id))
    conn.commit()


def sweep(conn, rows, delay: float = DELAY, progress=None, stop=None) -> dict:
    """Ask about these jobs (rows with id and url) and remember the answers. One request at a time with a pause between them; `progress(done,
    total)` is called after each request and `stop` (a threading.Event) ends the run early. Returns {"open", "closed", "unknown"} counts
    (a job whose source cannot be asked is not counted)."""
    tasks = _tasks(rows)
    results: dict[str, str] = {}
    for done, (key, group) in enumerate(tasks.items(), 1):
        if stop is not None and stop.is_set():
            break
        if key[0] == "board":
            results.update(check_board(key[1], key[2], [r["id"] for r in group]))
        else:
            results[group[0]["id"]] = check_one(group[0])
        if progress:
            progress(done, len(tasks))
        time.sleep(delay)
    _unblock(results)
    record(conn, results)
    counts = {OPEN: 0, CLOSED: 0, UNKNOWN: 0}
    for state in results.values():
        counts[state] += 1
    return counts


def due_rows(conn, min_score: int, max_age: float = RECHECK_AFTER, now: float | None = None) -> list:
    """The jobs this person is looking at (the matches, and everything they saved) that can be asked about and have not been
    asked about lately, best first."""
    cutoff = (time.time() if now is None else now) - max_age
    rows = conn.execute("SELECT id, url, title, company FROM jobs WHERE status IN ('new', 'saved') AND (status = 'saved' OR score >= ?)"
                        " AND (checked_at IS NULL OR checked_at < ?) ORDER BY score DESC NULLS LAST", (min_score, cutoff)).fetchall()
    return [r for r in rows if can_check(r["id"])]


def check_job(conn, job_id: str, now: float | None = None) -> dict:
    """Ask about one job now (unless it was asked about lately). {"state", "checked", "supported"}; LookupError for a job the person does
    not have. The state is "closed" or "open" as last found, or "unknown" for a source that cannot be asked or a failed request."""
    row = conn.execute("SELECT id, url, title, company, closed_at, checked_at FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise LookupError(job_id)
    if not can_check(job_id):
        return {"state": UNKNOWN, "checked": None, "supported": False}
    now = time.time() if now is None else now
    if not (row["checked_at"] and now - row["checked_at"] < RECHECK_AFTER):
        sweep(conn, [row], delay=0)
        row = conn.execute("SELECT closed_at, checked_at FROM jobs WHERE id = ?", (job_id,)).fetchone()
    state = CLOSED if row["closed_at"] else OPEN if row["checked_at"] else UNKNOWN
    return {"state": state, "checked": row["checked_at"], "supported": True}


def summary(conn, min_score: int, now: float | None = None) -> dict:
    """What the page says about it: how many of this person's jobs are known to be taken down, and how many could be asked about and
    have not been lately."""
    closed = conn.execute("SELECT COUNT(*) AS n FROM jobs WHERE closed_at IS NOT NULL AND status IN ('new', 'saved')").fetchone()["n"]
    last = conn.execute("SELECT MAX(checked_at) AS t FROM jobs WHERE status IN ('new', 'saved')").fetchone()["t"]
    return {"closed": closed, "due": len(due_rows(conn, min_score, now=now)), "last": last}


def sweep_everyone(min_score: int, delay: float = DELAY) -> dict:
    """After a fetch: ask about the jobs of every user (a posting shared by several is asked about once). Returns the total counts. A
    problem with one user's jobs is logged and does not stop the rest."""
    import pipeline
    anon = pipeline._connect(False)
    try:
        ids = [r["id"] for r in anon.execute("SELECT id FROM users").fetchall()]
    finally:
        anon.close()
    total = {OPEN: 0, CLOSED: 0, UNKNOWN: 0}
    for uid in ids:
        conn = pipeline._connect(uid)
        try:
            for state, n in sweep(conn, due_rows(conn, min_score), delay=delay).items():
                total[state] += n
        except Exception:
            log.exception("Could not check which of user %s's postings are still open", uid)
        finally:
            conn.close()
    return total
