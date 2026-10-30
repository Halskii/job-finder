"""Cheap, deterministic filters that run BEFORE any LLM call, plus a keyword pre-score."""
from __future__ import annotations

import re
from datetime import date, datetime

LEVEL_WORDS = {"i", "ii", "iii", "iv", "1", "2", "3", "associate", "junior", "jr"}
NON_US_HINTS = [
    "united kingdom", "uk", "london", "ireland", "dublin", "canada", "toronto", "vancouver", "india", "bangalore", "bengaluru",
    "hyderabad", "pune", "germany", "berlin", "munich", "france", "paris", "spain", "madrid", "netherlands", "amsterdam",
    "poland", "warsaw", "israel", "tel aviv", "singapore", "australia", "sydney", "japan", "tokyo", "brazil", "mexico",
    "china", "beijing", "shanghai", "emea", "apac", "latam", "philippines", "romania", "portugal", "sweden", "switzerland",
    "costa rica", "colombia", "bogota", "argentina", "buenos aires", "chile", "santiago", "peru", "lima", "turkey", "istanbul",
    "uae", "dubai", "abu dhabi", "saudi", "south korea", "seoul", "taiwan", "taipei", "vietnam", "thailand", "bangkok",
    "indonesia", "jakarta", "malaysia", "new zealand", "italy", "milan", "belgium", "brussels", "austria", "vienna", "denmark",
    "copenhagen", "norway", "oslo", "finland", "helsinki", "czech", "prague", "ukraine", "kyiv", "hungary", "budapest",
    "greece", "athens", "egypt", "cairo", "nigeria", "lagos", "kenya", "nairobi", "south africa", "cape town", "pakistan",
    "bangladesh", "sri lanka", "hong kong", "ontario", "quebec", "british columbia", "montreal", "ottawa", "calgary",
    "europe", "asia", "africa", "latin america", "middle east",
]
US_HINTS = ["united states", "usa", "u.s.", ", us", "remote - us", "us-remote", "us remote"]
US_STATE_RE = re.compile(r",\s*(A[LKZR]|C[AOT]|D[EC]|F[L]|G[A]|H[I]|I[ADLN]|K[SY]|L[A]|M[ADEINOST]|N[CDEHJMVY]|O[HKR]|P[AR]|R[I]|S[CD]|T[NX]|U[T]|V[AT]|W[AIVY])\b")


def _norm(text: str) -> str:
    text = text.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9+/# ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def base_titles(titles: list[str]) -> list[re.Pattern]:
    """'Software Engineer I' and 'Associate Software Engineer' both become the pattern \\bsoftware engineer\\b."""
    seen, patterns = set(), []
    for t in titles:
        words = [w for w in _norm(t).split() if w not in LEVEL_WORDS]
        base = " ".join(words)
        if base and base not in seen:
            seen.add(base)
            patterns.append(re.compile(r"(?<![a-z0-9])" + re.escape(base) + r"(?![a-z0-9])"))
    return patterns


def title_ok(title: str, patterns: list[re.Pattern], exclude_words: list[str]) -> bool:
    norm = _norm(title)
    words = {w.rstrip("+") for w in norm.split()}   # "Staff+" -> "staff"
    if any(w in words for w in exclude_words):
        return False
    return any(p.search(norm) for p in patterns)


def location_ok(location: str, us_only: bool) -> bool:
    if not us_only:
        return True
    loc = (location or "").lower()
    if not loc:
        return True
    if any(h in loc for h in US_HINTS) or US_STATE_RE.search(location or ""):
        return True
    return not any(re.search(r"(?<![a-z])" + re.escape(h) + r"(?![a-z])", loc) for h in NON_US_HINTS)


def compile_exclusions(terms: list[str]) -> list[re.Pattern]:
    """Whole-word, case-insensitive patterns. Two-letter terms like 'CA' are case-sensitive so 'ca' in a word never matches."""
    out = []
    for t in terms or []:
        t = t.strip()
        if not t:
            continue
        flags = 0 if (len(t) == 2 and t.isupper()) else re.I
        out.append(re.compile(r"(?<![A-Za-z])" + re.escape(t) + r"(?![A-Za-z])", flags))
    return out


def exclusion_ok(location: str, patterns: list[re.Pattern]) -> bool:
    """False only when EVERY location listed for the posting is excluded.
    'San Francisco, CA | Remote - US' stays (the remote option is still open to you); 'San Francisco, CA' is dropped."""
    if not patterns or not (location or "").strip():
        return True
    # Split on ; | newline and the word 'or'. Also split on a comma that starts a "US-XXX" entry (Stripe-style
    # "US-SF, US-NYC, US-Remote"). Other commas stay: "San Francisco, CA" is ONE location.
    parts = [p.strip() for p in re.split(r"[;|\n]|\bor\b|,\s*(?=US-)", location) if p.strip()]
    if not parts:
        return True
    return any(not any(p.search(part) for p in patterns) for part in parts)


def pay_ok(pay_max: int | None, pay_min: int | None, minimum: int) -> bool:
    if pay_max is not None:
        return pay_max >= minimum
    if pay_min is not None:
        return pay_min >= minimum * 0.6   # a lone low number is ambiguous (hourly?), so don't over-filter
    return True


def age_ok(posted: str | None, max_age_days: int) -> bool:
    if not posted:
        return True
    try:
        d = datetime.fromisoformat(posted[:10]).date()
    except ValueError:
        return True
    return (date.today() - d).days <= max_age_days


def quick_score(description: str, profile: dict) -> float:
    """Keyword overlap against the verified profile. Only used to decide which jobs the LLM scores first."""
    text = _norm(description)
    if not text:
        return 0.0
    def hits(terms):
        return sum(1 for t in terms if re.search(r"(?<![a-z0-9])" + re.escape(_norm(t)) + r"(?![a-z0-9])", text))
    good = hits(profile.get("strong_skills", [])) * 1.0 + hits(profile.get("familiar_skills", [])) * 0.5
    bad = hits(profile.get("do_not_assume", [])) * 0.7
    return round(good - bad, 2)
