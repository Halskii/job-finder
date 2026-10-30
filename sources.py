"""Fetch job postings from public job-board APIs (Greenhouse, Lever, Ashby, Workday).

Only public endpoints that the boards themselves expose are used. No logins, no scraping of
sites that forbid it. Every fetcher returns a list of Job objects and never raises on a bad slug.
"""
from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests

UA = {"User-Agent": "job-finder/0.1 (personal job search tool)"}
TIMEOUT = 30


@dataclass
class Job:
    id: str
    source: str
    company: str
    title: str
    location: str
    url: str
    posted: str | None = None          # ISO date string when known
    description: str = ""
    pay_min: int | None = None
    pay_max: int | None = None
    detail: dict = field(default_factory=dict)   # fetcher-private data (e.g. Workday detail URL)


# ----------------------------------------------------------------------------- helpers
def strip_html(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"<(br|/p|/li|/div|/h\d)[^>]*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


_AMOUNT = r"(\d{2,3}(?:,\d{3})+|\d{2,3}(?:\.\d+)?\s?[kK])"
PAY_RE = re.compile(r"\$\s?" + _AMOUNT + r"\s*(?:-|–|—|to)\s*\$?\s?" + _AMOUNT)


def _amount(s: str) -> int:
    s = s.replace(",", "").replace(" ", "").lower()
    return int(float(s[:-1]) * 1000) if s.endswith("k") else int(float(s))


def extract_pay(text: str) -> tuple[int | None, int | None]:
    """Find the first plausible annual salary range like $120,000 - $160,000 or $120k-$160k."""
    for m in PAY_RE.finditer(text or ""):
        lo, hi = _amount(m.group(1)), _amount(m.group(2))
        if 30_000 <= lo <= hi <= 1_000_000:
            return lo, hi
    return None, None


def _get(url: str, **kw):
    r = requests.get(url, headers=UA, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    return r.json()


def _iso_from_ms(ms) -> str | None:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------------------- Greenhouse
def fetch_greenhouse(slug: str) -> list[Job]:
    data = _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", params={"content": "true"})
    jobs = []
    for j in data.get("jobs", []):
        desc = strip_html(j.get("content", ""))
        lo, hi = extract_pay(desc)
        jobs.append(Job(
            id=f"gh:{slug}:{j['id']}", source="greenhouse", company=slug,
            title=j.get("title", ""), location=(j.get("location") or {}).get("name", ""),
            url=j.get("absolute_url", ""), posted=(j.get("first_published") or j.get("updated_at") or "")[:10] or None,
            description=desc, pay_min=lo, pay_max=hi,
        ))
    return jobs


# ----------------------------------------------------------------------------- Lever
def fetch_lever(slug: str) -> list[Job]:
    data = _get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"})
    jobs = []
    for j in data:
        desc = j.get("descriptionPlain") or strip_html(j.get("description", ""))
        for lst in j.get("lists", []) or []:
            desc += "\n" + (lst.get("text") or "") + "\n" + strip_html(lst.get("content", ""))
        lo = hi = None
        sr = j.get("salaryRange") or {}
        if sr.get("min") and sr.get("max") and (sr.get("interval") or "").lower().startswith("per-year"):
            lo, hi = int(sr["min"]), int(sr["max"])
        if lo is None:
            lo, hi = extract_pay(desc)
        cats = j.get("categories") or {}
        jobs.append(Job(
            id=f"lv:{slug}:{j['id']}", source="lever", company=slug,
            title=j.get("text", ""), location=cats.get("location") or "",
            url=j.get("hostedUrl", ""), posted=_iso_from_ms(j.get("createdAt")),
            description=desc.strip(), pay_min=lo, pay_max=hi,
        ))
    return jobs


# ----------------------------------------------------------------------------- Ashby
def fetch_ashby(slug: str) -> list[Job]:
    data = _get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", params={"includeCompensation": "true"})
    jobs = []
    for j in data.get("jobs", []):
        desc = j.get("descriptionPlain") or strip_html(j.get("descriptionHtml", ""))
        lo = hi = None
        for comp in (j.get("compensation") or {}).get("summaryComponents", []) or []:
            if (comp.get("compensationType") or "").lower() == "salary" and (comp.get("interval") or "").lower().startswith("1 year"):
                if comp.get("minValue") and comp.get("maxValue"):
                    lo, hi = int(comp["minValue"]), int(comp["maxValue"])
        if lo is None:
            lo, hi = extract_pay(desc)
        loc = j.get("location") or ""
        if j.get("isRemote") and "remote" not in loc.lower():
            loc = f"{loc} (Remote)".strip()
        jobs.append(Job(
            id=f"ab:{slug}:{j['id']}", source="ashby", company=slug,
            title=j.get("title", ""), location=loc, url=j.get("jobUrl", ""),
            posted=(j.get("publishedAt") or "")[:10] or None,
            description=desc, pay_min=lo, pay_max=hi,
        ))
    return jobs


# ----------------------------------------------------------------------------- Workday
def fetch_workday(entry: dict, search_terms: list[str], max_pages: int, delay: float) -> list[Job]:
    """List jobs via the same JSON endpoint the Workday career site uses. Details are filled in later,
    and only for postings that pass the title filter (see fill_workday_detail)."""
    host, tenant, site, name = entry["host"], entry["tenant"], entry["site"], entry.get("name", entry["tenant"])
    base = f"https://{host}/wday/cxs/{tenant}/{site}"
    seen: dict[str, Job] = {}
    for term in search_terms:
        for page in range(max_pages):
            body = {"appliedFacets": {}, "limit": 20, "offset": page * 20, "searchText": term}
            r = requests.post(f"{base}/jobs", json=body, headers={**UA, "Content-Type": "application/json"}, timeout=TIMEOUT)
            r.raise_for_status()
            postings = r.json().get("jobPostings", [])
            if not postings:
                break
            for p in postings:
                path = p.get("externalPath", "")
                if not path or path in seen:
                    continue
                seen[path] = Job(
                    id=f"wd:{tenant}:{path.rsplit('_', 1)[-1] or path}", source="workday", company=name,
                    title=p.get("title", ""), location=p.get("locationsText", ""),
                    url=f"https://{host}/en-US/{site}{path}", posted=None,
                    detail={"api": f"{base}{path}"},
                )
            time.sleep(delay)
    return list(seen.values())


def fill_workday_detail(job: Job) -> None:
    info = _get(job.detail["api"]).get("jobPostingInfo", {})
    job.description = strip_html(info.get("jobDescription", ""))
    job.posted = (info.get("startDate") or "")[:10] or None
    if info.get("location") and not job.location:
        job.location = info["location"]
    job.pay_min, job.pay_max = extract_pay(job.description)
