"""Caps on the calls that cost money (scoring, tailoring, interview prep, reading a resume): per person and for everyone, per day, and a
switch that pauses them all. The numbers come from `limits:` in config.yaml, which is read on every check, so changing it takes effect
at once with no restart.

Counts are kept in the `usage` table (per user) and `usage_totals` (everyone)."""
from __future__ import annotations

from contextlib import contextmanager

import db
import tailor
from errors import BadModelAnswer, UserError

DEFAULTS = {
    "per_user_per_day": {"tailor": 6, "letter": 6, "tips": 12, "analyze": 6, "skills": 40},           # scoring has its own: scoring.max_per_user_per_day
    "all_users_per_day": {"score": 4000, "tailor": 100, "letter": 100, "tips": 300, "analyze": 100, "skills": 1000},
    "paused": False,
}
WHAT = {"score": "job scorings", "tailor": "tailored resumes", "letter": "cover letters", "tips": "interview prep write-ups",
        "analyze": "resume readings", "skills": "skill lists"}
PAUSED = "The paid features are paused for a short while. Everything else still works. Try again later."


def settings() -> dict:
    """The limits from config.yaml laid over the defaults."""
    given = tailor.config_value("limits", {}) or {}
    return {"per_user_per_day": {**DEFAULTS["per_user_per_day"], **(given.get("per_user_per_day") or {})},
            "all_users_per_day": {**DEFAULTS["all_users_per_day"], **(given.get("all_users_per_day") or {})},
            "paused": bool(given.get("paused", DEFAULTS["paused"]))}


def paused() -> bool:
    return settings()["paused"]


def everyone_left(conn, kind: str) -> int:
    """How many more calls of this kind the whole site may make today."""
    return max(0, int(settings()["all_users_per_day"].get(kind, 10 ** 9)) - db.usage_total_today(conn, kind))


def user_left(conn, kind: str) -> int:
    """How many more this user may make today (scoring's own limit is applied by the pipeline)."""
    cap = settings()["per_user_per_day"].get(kind)
    return 10 ** 9 if cap is None else max(0, int(cap) - db.usage_today(conn, kind))


def check(conn, kind: str) -> None:
    """Raise a UserError, worded for the person, when this call may not be made now."""
    if paused():
        raise UserError(PAUSED, "paused")
    if user_left(conn, kind) <= 0:
        n = settings()["per_user_per_day"][kind]
        raise UserError(f"You have used today's {n} {WHAT[kind]}. The count starts again tomorrow.", "limit_reached")
    if everyone_left(conn, kind) <= 0:
        raise UserError("This feature has reached its limit for the whole site today, to keep the alpha running. Try again tomorrow.", "limit_reached")


class Spent:
    """What a paid call used, for the caller to fill in (tokens) so the totals are right."""

    def __init__(self):
        self.tokens_in = self.tokens_out = 0
        self.free = False   # set when the call turned out to cost nothing (an answer that was already saved)

    def tokens(self, tokens_in: int = 0, tokens_out: int = 0) -> None:
        self.tokens_in, self.tokens_out = int(tokens_in or 0), int(tokens_out or 0)


@contextmanager
def paid_call(conn, kind: str):
    """Around one paid call: refuse it if a limit is reached, count it when it works, and also when the model answered badly (that still
    cost). A problem with the request itself (no resume yet, no key) raises before anything is spent and is not counted."""
    check(conn, kind)
    spent = Spent()
    try:
        yield spent
    except BadModelAnswer:
        db.add_usage(conn, kind, 1, spent.tokens_in, spent.tokens_out)
        raise
    except Exception:
        raise
    else:
        if not spent.free:
            db.add_usage(conn, kind, 1, spent.tokens_in, spent.tokens_out)
