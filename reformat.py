"""Restore the original formatting of postings that were stored before formatting was kept.

Postings stored earlier hold plain text with the headings, bullets and bold already thrown away. This module asks each posting's
own source for it again and stores the formatted version:
  Greenhouse, Lever, Ashby,  the company's whole board comes back in one request; every stored job of that board is found in it
  Recruitee, Personio
  SmartRecruiters            the posting's own detail request
  Workday                    the same detail request the first fetch used, rebuilt from the posting's address
  Himalayas                  the search API, by the posting's title
Other sources keep their plain text.

jobs.formatted is 0 for a posting still waiting, 1 once it has its formatting, and 2 when its source no longer has it (a closed
posting), which is not asked for again. The viewer restores everything in the background when it starts, best matches first, and a
posting opened before its turn is restored on the spot."""
from __future__ import annotations

import re
import time

import ats_sources
import feed_sources
import sources
from errors import log

BOARDS = {"gh": (sources, "fetch_greenhouse"), "lv": (sources, "fetch_lever"), "ab": (sources, "fetch_ashby"),
          "rc": (ats_sources, "fetch_recruitee"), "pn": (ats_sources, "fetch_personio")}   # job id prefix -> the fetcher for a whole board
SUPPORTED = set(BOARDS) | {"wd", "hm", "sr"}
WORKDAY_URL = re.compile(r"https://([^/]+)/[A-Za-z-]+/([^/]+)(/job/.+)$")   # host / locale / site / the posting's own path


def can_reformat(job_id: str, formatted) -> bool:
    """True when this job is stored as plain text and its source can be asked for the formatted version."""
    return not formatted and job_id.split(":", 1)[0] in SUPPORTED


def _board_of(job_id: str) -> tuple[str, str]:
    prefix, slug, *_ = job_id.split(":", 2)
    return prefix, slug


def _from_workday(row) -> str | None:
    m = WORKDAY_URL.match(row["url"] or "")
    tenant = row["id"].split(":", 2)[1]
    if not m:
        return None
    host, site, path = m.groups()
    info = sources._get(f"https://{host}/wday/cxs/{tenant}/{site}{path}").get("jobPostingInfo", {})
    return sources.strip_html(info.get("jobDescription", ""))


def _from_smartrecruiters(row) -> str | None:
    _, slug, posting = row["id"].split(":", 2)
    job = sources.Job(row["id"], "smartrecruiters", row["company"], row["title"], "", row["url"] or "",
                      detail={"api": f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{posting}"})
    sources.fill_smartrecruiters_detail(job)
    return job.description


def _from_himalayas(row) -> str | None:
    for term in (row["title"], f"{row['company']} {row['title']}"):   # the search is by words, so the title usually finds it
        for job in feed_sources.fetch_himalayas([term], pages=3):
            if job.id == row["id"]:
                return job.description
    return None


SINGLES = {"wd": _from_workday, "hm": _from_himalayas, "sr": _from_smartrecruiters}   # sources asked one posting at a time


def _board_descriptions(prefix: str, slug: str) -> dict[str, str]:
    """{job id: formatted description} for everything on one company's board (one request)."""
    module, name = BOARDS[prefix]
    return {j.id: j.description for j in getattr(module, name)(slug) if j.description}


def _store(conn, job_id: str, description: str | None) -> None:
    """Keep the formatted description, or remember that the source no longer has this posting (formatted = 2)."""
    if description:
        conn.execute("UPDATE jobs SET description=?, formatted=1 WHERE id=?", (description, job_id))
    else:
        conn.execute("UPDATE jobs SET formatted=2 WHERE id=?", (job_id,))
    conn.commit()


def reformat_job(conn, job_id: str) -> dict:
    """Restore one stored job now. Returns {"updated": bool, "description": str or None}.
    A source that cannot be reached just means no change (it is tried again later): this is only cosmetic."""
    row = conn.execute("SELECT id, title, company, url, formatted FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise LookupError("no such job")
    if not can_reformat(job_id, row["formatted"]):
        return {"updated": False, "description": None}
    prefix = job_id.split(":", 1)[0]
    try:
        if prefix in BOARDS:
            description = _board_descriptions(*_board_of(job_id)).get(job_id)
        else:
            description = SINGLES[prefix](row)
    except Exception as ex:
        log.warning("Could not restore the formatting of %s: %s", job_id, ex)
        return {"updated": False, "description": None}
    _store(conn, job_id, description)
    return {"updated": bool(description), "description": description or None}


def restore_all(conn, delay: float = 0.4, progress=None, stop=None) -> dict:
    """Restore every waiting posting, best matches first, one source request at a time with a pause between requests.
    A board is asked for once however many of its jobs are waiting. Returns {"restored", "gone", "failed"} counts;
    `progress(done, total)` is called after each request and `stop` (a threading.Event) ends the run early."""
    waiting = [r for r in conn.execute("SELECT id, title, company, url FROM jobs WHERE (formatted IS NULL OR formatted=0)"
                                       " ORDER BY score IS NULL, score DESC").fetchall() if r["id"].split(":", 1)[0] in SUPPORTED]
    tasks: dict[tuple, list] = {}   # in order of the best job in each: a board's jobs together, any other job on its own
    for r in waiting:
        prefix = r["id"].split(":", 1)[0]
        tasks.setdefault(("board", *_board_of(r["id"])) if prefix in BOARDS else ("one", r["id"]), []).append(r)
    counts = {"restored": 0, "gone": 0, "failed": 0}
    for done, (key, rows) in enumerate(tasks.items(), 1):
        if stop is not None and stop.is_set():
            break
        try:
            if key[0] == "board":
                found = _board_descriptions(key[1], key[2])
                results = [(r["id"], found.get(r["id"])) for r in rows]
            else:
                results = [(rows[0]["id"], SINGLES[key[1].split(":", 1)[0]](rows[0]))]
        except Exception as ex:
            log.warning("Could not restore formatting for %s: %s", key[-1], ex)
            counts["failed"] += len(rows)
        else:
            for job_id, description in results:
                _store(conn, job_id, description)
                counts["restored" if description else "gone"] += 1
        if progress:
            progress(done, len(tasks))
        time.sleep(delay)
    return counts
