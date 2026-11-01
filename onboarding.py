"""Setting up a new account: reading a resume to suggest job titles and fill in a profile, and checking everything the person
edits before it is saved.

Claude only reads what the resume (or the person's own description) says, and the person reviews and edits all of it on the
profile page. Nothing here is saved until they press Save."""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor

import profile_gen
import scorer
import tailor
import userdata
from errors import BadModelAnswer

MODEL = "claude-haiku-5-5"   # overridden by `onboard_model:` in config.yaml

SUGGEST_SYSTEM = """You help a job seeker decide which job titles to search for. You are given their resume, or their own
description of their experience. Rules:
- Use ONLY what the text says about them. Do not assume skills, years, or seniority that are not stated.
- "titles": 10 to 16 job titles this person could realistically be hired into now, written the way employers write them in
  postings ("Backend Software Engineer", "Site Reliability Engineer"). Match their experience level: do not suggest senior, staff,
  lead, or manager titles unless the text shows that level. Cover their strongest areas first, then close neighbors. No
  duplicates, no level numbers (write "Software Engineer", not "Software Engineer II").
- "level": "entry" (about 0 to 2 years), "mid" (about 2 to 6), or "senior" (more).
- "exclude_words": single words that, when they appear in a posting's title, mean it is the wrong level for them. For entry or
  mid level include words like senior, sr, staff, principal, lead, manager, director, head, vp. For entry level also include
  architect. For senior level use words like intern, internship, junior, entry. Always include intern and internship unless they
  are seeking an internship.
- "name": their name as written at the top of the text, or "" if it is not there.
Return ONLY a JSON object: {"name": str, "level": "entry" | "mid" | "senior", "titles": [str], "exclude_words": [str]}"""


def clean_line(value, limit: int) -> str:
    """One line of plain text: control and invisible characters removed, spaces collapsed, cut to `limit` characters."""
    return " ".join(tailor.clean_request(value).split())[:limit]


def clean_list(values, max_items: int, max_len: int) -> list[str]:
    """A list of short strings with blanks and repeats (ignoring case) removed. Anything that is not a list gives an empty one."""
    if isinstance(values, str):
        values = re.split(r"[\n,]", values)
    out, seen = [], set()
    for v in values if isinstance(values, list) else []:
        s = clean_line(v, max_len)
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
        if len(out) >= max_items:
            break
    return out


def clean_int(value, low: int, high: int, default: int) -> int:
    try:
        return max(low, min(high, int(float(value))))
    except (TypeError, ValueError, OverflowError):
        return default


def clean_settings(data) -> dict:
    """The job search settings from the profile page, checked: only the known keys, with sizes and ranges limited."""
    data = data if isinstance(data, dict) else {}
    d = userdata.SETTINGS_DEFAULTS
    return {
        "titles": clean_list(data.get("titles"), 40, 80),
        "exclude_title_words": clean_list(data.get("exclude_title_words"), 30, 30),
        "min_pay": clean_int(data.get("min_pay"), 0, 1_000_000, d["min_pay"]),
        "max_age_days": clean_int(data.get("max_age_days"), 1, 365, d["max_age_days"]),
        "us_only": data.get("us_only") is not False,
        "exclude_locations": clean_list(data.get("exclude_locations"), 30, 40),
    }


def clean_profile(data) -> tuple[dict, list[str]]:
    """The profile from the profile page, checked and put in the shape the scorer and tailor read. Returns (profile, notes), where
    notes say what was changed on the person's behalf (a skill is never kept if they also said they do not have it)."""
    data = data if isinstance(data, dict) else {}
    blocked = {profile_gen._key(x) for x in clean_list(data.get("do_not_assume"), 80, 80)}
    notes, p = [], {}
    p["name"] = clean_line(data.get("name"), 80)
    p["headline"] = clean_line(data.get("headline"), 300)
    p["years_experience"] = clean_int(data.get("years_experience"), 0, 60, 0)
    p["education"] = clean_line(data.get("education"), 200)
    p["location"] = clean_line(data.get("location"), 80)
    p["work_authorization"] = clean_line(data.get("work_authorization"), 120)
    p["security_clearance"] = clean_line(data.get("security_clearance"), 120)
    p["relocation"] = clean_line(data.get("relocation"), 120)
    p["certifications"] = clean_list(data.get("certifications"), 30, 120)
    for field in ("strong_skills", "familiar_skills"):
        kept = []
        for skill in clean_list(data.get(field), 80, 60):
            if profile_gen._key(skill) in blocked:
                notes.append(f"{skill} was left out of your skills because you also listed it as something you do not have.")
            else:
                kept.append(skill)
        p[field] = kept
    strong = {profile_gen._key(s) for s in p["strong_skills"]}
    p["familiar_skills"] = [s for s in p["familiar_skills"] if profile_gen._key(s) not in strong]
    p["do_not_assume"] = clean_list(data.get("do_not_assume"), 80, 80)
    p["achievements"] = clean_list(data.get("achievements"), 20, 300)
    p["best_fit_notes"] = clean_list(data.get("best_fit_notes"), 10, 200)
    return p, notes


def _suggest(client, model: str, resume_text: str) -> dict:
    extra = {"thinking": {"type": "disabled"}} if model.startswith("claude-haiku-5") else {}
    msg = client.messages.create(model=model, max_tokens=1500, system=SUGGEST_SYSTEM,
                                 messages=[{"role": "user", "content": f"RESUME OR DESCRIPTION:\n{resume_text[:20000]}"}], **extra)
    try:
        data = scorer._extract_json("".join(b.text for b in msg.content if b.type == "text"))
    except ValueError:
        raise BadModelAnswer() from None
    titles = clean_list(data.get("titles"), 40, 80)
    if not titles:
        raise BadModelAnswer("We could not work out job titles from that. Add more detail about your work, then try again.", "no_titles")
    words = [w.lower() for w in clean_list(data.get("exclude_words"), 30, 30) if re.fullmatch(r"[A-Za-z0-9&/+\- ]+", w)]
    return {"name": clean_line(data.get("name"), 80), "level": data.get("level") if data.get("level") in ("entry", "mid", "senior") else "mid",
            "titles": titles, "exclude_words": words}


def analyze(client, resume_text: str, suggest_model: str | None = None, profile_model: str | None = None) -> dict:
    """Read the resume two ways at once: the facts for the profile (profile_gen) and the job titles to search for.
    Returns {"profile": {...the fields a resume states...}, "suggestions": {"name", "level", "titles", "exclude_words"}}."""
    suggest_model = suggest_model or tailor.config_value("onboard_model", MODEL)
    profile_model = profile_model or tailor.config_value("profile_model", "claude-sonnet-5-5")
    with ThreadPoolExecutor(max_workers=2) as pool:
        facts = pool.submit(profile_gen.generate, client, profile_model, resume_text)
        titles = pool.submit(_suggest, client, suggest_model, resume_text)
        try:
            generated = facts.result()
        except ValueError:   # the answer was not the shape we need
            raise BadModelAnswer() from None
        suggestions = titles.result()
    return {"profile": {k: generated[k] for k in profile_gen.FROM_RESUME}, "suggestions": suggestions}
