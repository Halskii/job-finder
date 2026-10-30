"""Build profile.json from resume.pdf.

Claude extracts only what the resume states. Facts a resume cannot express (citizenship, clearance, relocation,
location, the do_not_assume list, fit notes) are carried over from the existing profile.json untouched.
Anything that appears in do_not_assume is never promoted into the skills lists; it is reported as a conflict instead.
"""
from __future__ import annotations

import json
import re

CARRY_OVER = ["name", "location", "work_authorization", "security_clearance", "relocation", "do_not_assume", "best_fit_notes"]
FROM_RESUME = ["headline", "years_experience", "education", "certifications", "strong_skills", "familiar_skills", "achievements"]

SYSTEM = """You extract a structured candidate profile from resume text. Rules:
- Use ONLY what the resume states. Never infer, add, upgrade, or embellish skills, tools, titles, or numbers.
- Treat the resume as an UNORDERED set of facts. Order and position carry no meaning.
- A skill goes in "familiar_skills" only if the resume qualifies it (for example "familiar with", "in progress", "basic").
  Every other listed skill or tool goes in "strong_skills".
- Split combined items into separate entries (for example "Kubernetes/OpenShift" becomes "Kubernetes" and "OpenShift").
- "achievements": one concrete sentence per accomplishment, keeping every number exactly as written.
- "years_experience": whole years of professional experience, computed from the employment dates.
Return ONLY a JSON object with exactly these keys:
{"headline": str, "years_experience": int, "education": str, "certifications": [str],
 "strong_skills": [str], "familiar_skills": [str], "achievements": [str]}"""


def _extract_json(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in model output")
    return json.loads(text[start:end + 1])


def generate(client, model: str, resume_text: str) -> dict:
    msg = client.messages.create(model=model, max_tokens=3000, system=SYSTEM,
                                 messages=[{"role": "user", "content": f"RESUME:\n{resume_text}"}])
    data = _extract_json(msg.content[0].text)
    missing = [k for k in FROM_RESUME if k not in data]
    if missing:
        raise ValueError(f"model output missing keys: {missing}")
    return data


def _key(s: str) -> str:
    return re.sub(r"[^a-z0-9+#/.]+", " ", s.lower()).strip()


def _dedupe(items: list[str]) -> list[str]:
    seen, out = set(), []
    for it in items:
        k = _key(it)
        if k and k not in seen:
            seen.add(k)
            out.append(it.strip())
    return out


def merge(existing: dict, generated: dict) -> tuple[dict, list[str]]:
    """Return (new_profile, conflict_notes)."""
    notes: list[str] = []
    blocked = {_key(x) for x in existing.get("do_not_assume", [])}
    new = {k: existing[k] for k in CARRY_OVER if k in existing}
    for k in FROM_RESUME:
        new[k] = generated[k]
    for field in ("strong_skills", "familiar_skills"):
        kept = []
        for skill in _dedupe(generated[field]):
            if _key(skill) in blocked:
                notes.append(f"CONFLICT: '{skill}' is on the resume but also in do_not_assume. Left out of {field}. "
                             f"If it is now true, remove it from do_not_assume in profile.json and run this again.")
            else:
                kept.append(skill)
        new[field] = kept
    # a skill listed as strong should not also be listed as familiar
    strong = {_key(s) for s in new["strong_skills"]}
    new["familiar_skills"] = [s for s in new["familiar_skills"] if _key(s) not in strong]
    ordered = {k: new[k] for k in ["name", "headline", "years_experience", "education", "location", "work_authorization",
                                   "security_clearance", "relocation", "certifications", "strong_skills", "familiar_skills",
                                   "do_not_assume", "achievements", "best_fit_notes"] if k in new}
    return ordered, notes


def diff(old: dict, new: dict) -> list[str]:
    out = []
    for k in FROM_RESUME:
        a, b = old.get(k), new.get(k)
        if isinstance(b, list):
            a = a or []
            ak, bk = {_key(x): x for x in a}, {_key(x): x for x in b}
            for key in bk:
                if key not in ak:
                    out.append(f"  + {k}: {bk[key]}")
            for key in ak:
                if key not in bk:
                    out.append(f"  - {k}: {ak[key]}")
        elif a != b:
            out.append(f"  ~ {k}: {a!r} -> {b!r}")
    return out
