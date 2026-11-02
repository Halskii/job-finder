"""Where the work is done: in person, hybrid, or remote, read from what a posting says (its title, its location line, and its text).

A posting can allow more than one ("remote or hybrid"), so the answer is a list. An empty list means the posting does not say.
(The rules are written in lower case and run on a lower-cased copy of the text, which is much faster than ignoring case.)
The rules look for how a posting states its arrangement ("this is a fully remote role", "3 days a week in the office", "Telework: Hybrid")
and skip words that only mention the idea ("hybrid cloud", "on-site child care", "remote-friendly benefits")."""
from __future__ import annotations

import re

REMOTE, HYBRID, ONSITE = "remote", "hybrid", "onsite"

I = re.IGNORECASE
LABEL = r"(?:telework|work\s+(?:arrangement|location|model|type|style)|workplace|location\s+type|remote\s+status)"   # "Telework: Hybrid"
DAYS = r"(?:\d|one|two|three|four|1-2|2-3|3-4)"
NOT_REMOTE = re.compile(r"\bnot\s+(?:a\s+|an\s+|eligible\s+for\s+)?remote\b|\bno\s+remote\b|\bnon[- ]remote\b|\bnot\s+(?:available|offered)\s+(?:as|for)\s+remote"
                        r"|\bdoes\s+not\s+(?:offer|allow|support)\s+remote|\bremote\s+work\s+is\s+not\b|\bcannot\s+be\s+(?:done|performed)\s+remotely")

TEXT = {
    REMOTE: [
        re.compile(r"\b(?:fully|100%|completely|entirely)\s+remote\b"),
        re.compile(r"\bremote\s+(?:position|role|job|opportunity)\b"),
        re.compile(rf"\b{LABEL}\W{{1,8}}(?:fully\s+)?remote\b"),
        re.compile(r"\bthis\s+(?:position|role|job)\s+(?:is|can\s+be\s+(?:done|performed))\s+(?:fully\s+)?remote"),
        # weaker: a hybrid posting says these too ("work from home on Fridays"), so they only count when it does not say hybrid
        re.compile(r"\bremote[- ]first\b|\bremote\s+(?:employee|worker)s?\b"),
        re.compile(r"\bremote\s+work\s+(?:is\s+)?(?:available|allowed|offered|permitted|possible|eligible)\b"),
        re.compile(r"\b(?:eligible|approved|available)\s+for\s+(?:fully\s+)?remote\b|\bremote[- ]eligible\b"),
        re.compile(r"\bwork(?:ing)?\s+(?:fully\s+)?(?:remotely|from\s+home|from\s+anywhere)\b|\bwork[- ]from[- ]home\b"),
    ],
    HYBRID: [
        re.compile(r"\bhybrid\s+(?:work|working|schedule|model|role|position|arrangement|setup|environment\b(?!s?\s+(?:cloud|infrastructure))|workplace|office|remote|on[- ]?site|in[- ]office)"),
        re.compile(rf"\b{LABEL}\W{{1,8}}hybrid\b|\bhybrid\s*[(\-:]\s*(?:remote|in[- ]office|onsite|on-site)|\(hybrid\)"),
        re.compile(rf"\b{DAYS}\s*(?:days?)?\s*(?:per|a|each|/)\s*(?:week|wk)\s+(?:in|on)[- ](?:the\s+|our\s+)?(?:office|site)\b"),
        re.compile(rf"\b(?:in|on)[- ](?:the\s+|our\s+)?(?:office|site)\s+{DAYS}\s+days?\b|\bin[- ]office\s+{DAYS}\s*(?:days?)?\s*(?:per|a|each)\s+week\b"),
        re.compile(rf"\b(?:in[- ]person|in[- ]office|on[- ]?site|in\s+the\s+office)\b[^.]{{0,60}}?\b{DAYS}\s+days?\s+(?:per|a|each)\s+week\b"),
        re.compile(rf"\b{DAYS}\s+days?\s+(?:per|a|each)\s+week\b[^.]{{0,40}}?\b(?:in[- ]person|in[- ]office|on[- ]?site|in\s+(?:the|our)\s+office)\b"),
        re.compile(r"\b(?:partially|partly|part[- ]time)\s+remote\b|\bremote\s+(?:and|or|/)\s+(?:in[- ]office|on-?site)\s+(?:mix|combination)\b"),
    ],
    ONSITE: [
        re.compile(r"\b(?:this|the)\s+(?:is\s+an?\s+)?(?:position|role|job)\s+(?:is|will\s+be)\s+(?:an?\s+)?(?:fully\s+)?(?:on[- ]?site|in[- ]person|in[- ]office)"),
        re.compile(r"\b(?:fully\s+)?on[- ]?site\s+(?:position|role|job|work|presence|only|required|full[- ]time|at)\b"),
        re.compile(r"\b(?:work|working|report|reporting)\s+(?:on[- ]?site|in[- ]person|in\s+(?:the|our)\s+office|to\s+(?:the|our)\s+(?:office|location|site))\b"),
        re.compile(r"\bin[- ]person\s+(?:position|role|work|required|only|attendance)\b|\bin[- ]office\s+(?:position|role|only|required)\b"),
        re.compile(rf"\b{LABEL}\W{{1,8}}(?:on[- ]?site|in[- ]person|in[- ]office|not\s+eligible)\b"),
        re.compile(r"\bmust\s+(?:be\s+(?:able\s+to\s+)?)?(?:work|report|be\s+present)\s+(?:on[- ]?site|in[- ]person|in\s+(?:the|our)\s+office)\b"),
    ],
}
# Words a text must contain before a pattern is worth running, one tuple of alternatives per requirement (every tuple needs one of its words).
# Searching for a plain word is far quicker than a pattern, and most postings lack most of these, so this saves most of the work.
ONSITE_WORDS = ("on-site", "onsite", "on site", "in-person", "in person", "in-office", "in office", "the office", "our office")
GATES = {
    REMOTE: [(("remote",),), (("remote",),), (("remote",),), (("remote",),), (("remote",),), (("remote",),), (("remote",),),
             (("remotely", "from home", "from anywhere", "work-from-home"),)],
    HYBRID: [(("hybrid",),), (("hybrid",),), (("week", "wk"), ("office", "site")), (("day",), ("office", "site")),
             (("week",), ("person", "office", "site")), (("week",), ("person", "office", "site")), (("remote",),)],
    ONSITE: [(ONSITE_WORDS,), (("on-site", "onsite", "on site"),), (ONSITE_WORDS + ("the location", "our location", "the site", "our site"),),
             (("in-person", "in person", "in-office", "in office"),),
             (("telework", "work arrangement", "work location", "work model", "work type", "work style", "workplace", "location type", "remote status"),),
             (("must",), ONSITE_WORDS)],
}
assert all(len(GATES[m]) == len(TEXT[m]) for m in TEXT)


def says(mode: str, text: str, first: int | None = None) -> bool:
    """Whether the (lower-cased) text states this arrangement: any of the mode's patterns matches, running only those whose key words are present."""
    for pattern, gate in list(zip(TEXT[mode], GATES[mode]))[:first]:
        if all(any(word in text for word in options) for options in gate) and pattern.search(text):
            return True
    return False


IN_TITLE_OR_LOCATION = {
    REMOTE: re.compile(r"\bremote\b(?!\s+(?:sensing|sensors?|monitoring|access|desktop|control|diagnostics|operations?|support\s+engineer))|\bwork\s+from\s+home\b|\bwfh\b|\btelecommut", I),
    HYBRID: re.compile(r"\bhybrid\b", I),
    ONSITE: re.compile(r"\bon[- ]?site\b|\bin[- ]person\b|\bin[- ]office\b", I),
}
STRONG = 4       # the first four REMOTE patterns state the arrangement outright; the rest are things a hybrid posting says too
LOOKAT = 20000   # characters of text read: arrangements are stated near the top or in a "Location" line, and the rest is benefits and legal text


def detect(title: str = "", location: str = "", description: str = "") -> list[str]:
    """The ways of working a posting allows, from ["remote", "hybrid", "onsite"]; [] when it does not say."""
    heading = f"{title or ''} | {location or ''}"
    text = (description or "")[:LOOKAT].lower()
    in_heading = {mode for mode, pattern in IN_TITLE_OR_LOCATION.items() if pattern.search(heading)}
    if in_heading:     # a title or location that names the arrangement is the posting's own label; the text is full of company-wide wording
        return [m for m in (REMOTE, HYBRID, ONSITE) if m in in_heading]
    found = {mode for mode in TEXT if says(mode, text)}
    strong_remote = says(REMOTE, text, STRONG)
    negated = "remote" in text and bool(NOT_REMOTE.search(text))
    if negated:                                # "This is not a remote position" beats a stray remote phrase
        found.discard(REMOTE)
        found.add(ONSITE)
    if HYBRID in found:
        found.discard(ONSITE)                  # a hybrid job has days in the office; that is not the same as always being there
        if not strong_remote:
            found.discard(REMOTE)              # "work from home the other days" is part of being hybrid
    if REMOTE in found and ONSITE in found and not negated:
        found.discard(ONSITE)                  # remote work is offered, and an office is mentioned somewhere in the text
    return [m for m in (REMOTE, HYBRID, ONSITE) if m in found]
