"""Restore the original formatting of a posting that was stored before formatting was kept.

Postings stored earlier hold plain text with the headings, bullets and bold already thrown away. For boards whose API returns
the whole board in one request (Greenhouse, Lever, Ashby), the viewer fetches the board again the first time you open such a
posting, finds this job in it, and stores the formatted version. Other sources keep their plain text."""
from __future__ import annotations

import sources
from errors import log

FETCHERS = {"gh": "fetch_greenhouse", "lv": "fetch_lever", "ab": "fetch_ashby"}   # job id prefix -> fetcher in sources.py


def can_reformat(job_id: str, formatted) -> bool:
    """True when this job is stored as plain text and its board can be asked for the formatted version."""
    return not formatted and job_id.split(":", 1)[0] in FETCHERS


def reformat_job(conn, job_id: str) -> dict:
    """Fetch the formatted description of one stored job. Returns {"updated": bool, "description": str or None}.
    A board that cannot be reached or a posting that has been taken down just means no change: this is only cosmetic."""
    row = conn.execute("SELECT id, formatted FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise LookupError("no such job")
    if not can_reformat(job_id, row["formatted"]):
        return {"updated": False, "description": None}
    prefix, slug, *_ = job_id.split(":", 2)
    try:
        for job in getattr(sources, FETCHERS[prefix])(slug):   # one request for the whole board
            if job.id == job_id and job.description:
                conn.execute("UPDATE jobs SET description=?, formatted=1 WHERE id=?", (job.description, job_id))
                conn.commit()
                return {"updated": True, "description": job.description}
    except Exception as ex:
        log.warning("Could not restore the formatting of %s: %s", job_id, ex)
    return {"updated": False, "description": None}
