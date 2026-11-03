"""LLM fit scoring with Claude. The model may only treat facts in profile.json as true."""
from __future__ import annotations

import json
import re

import sources

SYSTEM = """You are a strict, honest job-fit analyst for one candidate. You are given the candidate's VERIFIED profile
and a job posting. Rules:
- Treat ONLY facts in the profile as true. Anything in "do_not_assume", or not mentioned at all, is something the candidate
  does NOT have. Never credit the candidate with it.
- Treat the profile and resume as an UNORDERED set of facts. Position, section order, line length, and bullet order carry no
  meaning: a skill or achievement mentioned anywhere counts exactly as much as one mentioned first. Consider the whole text.
- Compare against the posting's REQUIRED qualifications first, preferred second.
- Be calibrated. 85+ means they clearly meet nearly all required items; 60-84 is a reasonable stretch; below 40 is a poor fit.
- Flag dealbreakers: requires a currently held clearance, requires 5+ years, a required primary language the candidate lacks
  (e.g. Go), a stated pay maximum below the candidate's minimum, a non-US location, or a role that is mostly a different
  discipline (pure data science, accounting, sales).
- A clearance that will be sponsored/obtained after hire is NOT a dealbreaker (the candidate is a US citizen willing to obtain one).
Return ONLY a JSON object, no prose, with exactly these keys:
{"fit_score": int 0-100, "verdict": "apply" | "maybe" | "skip", "matched": [short strings], "missing_required": [short strings],
 "dealbreakers": [short strings], "pay_notes": str, "location_notes": str, "resume_angle": "one sentence on what to emphasize",
 "reason": "one sentence"}"""


THINKS_FIRST = re.compile(r"claude-(?:haiku|sonnet|opus|fable|mythos)-5")


def request_options(model: str) -> dict:
    """The size and effort of the request. The Claude 5 models think before they answer, and the thinking counts against `max_tokens`:
    with 900 the answer was never written (20 of 22 scoring calls on Haiku 5.5 returned no JSON). A low effort keeps the thinking short
    and 4096 leaves room for it; the cost of the thinking is in the output tokens `score_job` reports. Older models do not think."""
    if THINKS_FIRST.match(model):
        return {"max_tokens": 4096, "output_config": {"effort": "low"}}
    return {"max_tokens": 900}


def reply_text(msg) -> str:
    """The text of a model reply. A reply can start with blocks that are not text (the model's thinking), so the first block is not the answer."""
    return "".join(b.text for b in msg.content if getattr(b, "type", "text") == "text")


def _extract_json(text: str) -> dict:
    """The JSON object in the model's reply, tolerating any prose or code fence around it."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in model output")
    return json.loads(text[start:end + 1])


def score_job(client, model: str, profile: dict, job, min_pay: int, resume_text: str = "") -> dict:
    """Ask Claude how well one stored job (a database row) fits the profile. Returns the parsed answer plus the token
    counts under `_in` / `_out`, which the caller uses to estimate cost."""
    description = (job["description"] or "")[:7000]   # long postings are cut to keep each call cheap
    unit = job["pay_unit"] if "pay_unit" in job.keys() else "year"   # a stored job is a row; tests pass a dict
    pay = sources.format_pay(job["pay_min"], job["pay_max"], unit) or "unknown"
    if unit == "hour" and pay != "unknown":   # say what an hourly rate comes to in a year, so it can be compared with the minimum
        yearly = [f"${round(v * sources.HOURS_PER_YEAR):,}" for v in dict.fromkeys(v for v in (job["pay_min"], job["pay_max"]) if v)]
        pay += f" (about {' - '.join(yearly)} a year full-time)"
    resume_block = (f"CANDIDATE RESUME (context only; if it conflicts with the profile, the profile wins):\n{resume_text}\n\n"
                    if resume_text else "")
    user = (
        f"CANDIDATE PROFILE (verified):\n{json.dumps(profile, indent=1)}\n\n"
        f"{resume_block}"
        f"CANDIDATE MINIMUM PAY: ${min_pay:,} per year\n\n"
        f"JOB POSTING\nCompany: {job['company']}\nTitle: {job['title']}\nLocation: {job['location']}\n"
        f"Listed pay: {pay}\nPosted: {job['posted'] or 'unknown'}\n\n{description}"
    )
    msg = client.messages.create(model=model, system=SYSTEM, messages=[{"role": "user", "content": user}], **request_options(model))
    data = _extract_json(reply_text(msg))
    usage = getattr(msg, "usage", None)
    data["_in"] = int(getattr(usage, "input_tokens", 0) or 0)
    data["_out"] = int(getattr(usage, "output_tokens", 0) or 0)
    # Keep the answer usable even if the model strays from the format.
    data["fit_score"] = max(0, min(100, int(data.get("fit_score", 0))))
    if data.get("verdict") not in ("apply", "maybe", "skip"):
        data["verdict"] = "maybe" if data["fit_score"] >= 60 else "skip"
    return data
