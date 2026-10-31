"""Fetch job postings from public job-board APIs (Greenhouse, Lever, Ashby, Workday, SmartRecruiters) and the Remotive feed.

Only public endpoints that the boards themselves expose are used. No logins, no scraping of
sites that forbid it. Every fetcher returns a list of Job objects and never raises on a bad slug.
"""
from __future__ import annotations

import html
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

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


_local = threading.local()


def _session() -> requests.Session:
    """One HTTP session per thread. It keeps the connection to a server open, so the next request to the same server skips
    the connection and TLS handshake (a large share of the time for a small JSON request)."""
    if not hasattr(_local, "session"):
        _local.session = requests.Session()
        _local.session.headers.update(UA)
    return _local.session


def _get(url: str, **kw):
    r = _session().get(url, timeout=TIMEOUT, **kw)
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
def workday_posted(text: str, today: date | None = None) -> str | None:
    """An approximate ISO date from the list's 'Posted 5 Days Ago' / 'Posted Today' / 'Posted 30+ Days Ago'. It lets old postings
    be dropped before their full text is fetched. 'Posted 30+' becomes 31 days ago: older than a 30-day limit, but not
    claiming to know how old. The exact date from the full posting replaces this one."""
    today = today or date.today()
    t = (text or "").lower()
    if "today" in t:
        days = 0
    elif "yesterday" in t:
        days = 1
    else:
        m = re.search(r"(\d+)(\+?)\s*days?", t)
        if not m:
            return None
        days = int(m.group(1)) + (1 if m.group(2) else 0)
    return (today - timedelta(days=days)).isoformat()


def fetch_workday(entry: dict, search_terms: list[str], max_pages: int, delay: float) -> list[Job]:
    """List jobs via the same JSON endpoint the Workday career site uses. Details are filled in later,
    and only for postings that pass the title filter (see fill_workday_detail)."""
    host, tenant, site, name = entry["host"], entry["tenant"], entry["site"], entry.get("name", entry["tenant"])
    base = f"https://{host}/wday/cxs/{tenant}/{site}"
    seen: dict[str, Job] = {}
    for term in search_terms:
        total = None
        for page in range(max_pages):
            body = {"appliedFacets": {}, "limit": 20, "offset": page * 20, "searchText": term}
            r = _session().post(f"{base}/jobs", json=body, timeout=TIMEOUT)
            r.raise_for_status()
            data = r.json()
            postings = data.get("jobPostings", [])
            if not postings:
                break
            if page == 0:
                total = data.get("total")   # Workday reports the total on the first page only
            for p in postings:
                path = p.get("externalPath", "")
                if not path or path in seen:
                    continue
                seen[path] = Job(
                    id=f"wd:{tenant}:{path.rsplit('_', 1)[-1] or path}", source="workday", company=name,
                    title=p.get("title", ""), location=p.get("locationsText", ""),
                    url=f"https://{host}/en-US/{site}{path}", posted=workday_posted(p.get("postedOn", "")),
                    detail={"api": f"{base}{path}", **({"gov": True} if entry.get("gov") else {})},
                )
            time.sleep(delay)
            if len(postings) < 20 or (total is not None and (page + 1) * 20 >= total):
                break   # that was the last page; don't ask for an empty one
    return list(seen.values())


def fill_workday_detail(job: Job) -> None:
    info = _get(job.detail["api"]).get("jobPostingInfo", {})
    job.description = strip_html(info.get("jobDescription", ""))
    job.posted = (info.get("startDate") or "")[:10] or job.posted
    # The list only says "6 Locations" for a multi-location posting. The real places are in the detail, and the location
    # filters need them: a "2 Locations" job can turn out to be in Australia, or in California.
    places = [p for p in [info.get("location"), *(info.get("additionalLocations") or [])] if p]
    if places and (not job.location or re.fullmatch(r"\d+ Locations?", job.location)):
        job.location = "; ".join(places)
    job.pay_min, job.pay_max = extract_pay(job.description)


# ----------------------------------------------------------------------------- SmartRecruiters
SR_PAGE = 100
SR_MAX = 500   # per company; the list is already limited to US postings


def fetch_smartrecruiters(slug: str) -> list[Job]:
    """List a company's US postings. The list has no description, so details are filled in later and only
    for postings that pass the title filter (see fill_detail). An unknown company id returns an empty list."""
    jobs: list[Job] = []
    offset = 0
    while offset < SR_MAX:
        data = _get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings",
                    params={"limit": SR_PAGE, "offset": offset, "country": "us"})
        page = data.get("content", [])
        for p in page:
            loc = p.get("location") or {}
            where = loc.get("fullLocation") or ", ".join(x for x in (loc.get("city"), loc.get("region")) if x)
            if loc.get("remote") and "remote" not in where.lower():
                where = f"{where} (Remote)".strip()
            company = (p.get("company") or {}).get("name") or slug
            jobs.append(Job(
                id=f"sr:{slug.lower()}:{p['id']}", source="smartrecruiters", company=company,
                title=p.get("name", ""), location=where,
                url=f"https://jobs.smartrecruiters.com/{slug}/{p['id']}",
                posted=(p.get("releasedDate") or "")[:10] or None,
                detail={"api": f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{p['id']}"},
            ))
        offset += SR_PAGE
        if not page or offset >= data.get("totalFound", 0):
            break
    return jobs


def fill_smartrecruiters_detail(job: Job) -> None:
    info = _get(job.detail["api"])
    sections = (info.get("jobAd") or {}).get("sections") or {}
    parts = [strip_html((sections.get(k) or {}).get("text", ""))
             for k in ("jobDescription", "qualifications", "additionalInformation")]
    job.description = "\n\n".join(p for p in parts if p)
    job.posted = (info.get("releasedDate") or "")[:10] or job.posted
    job.url = info.get("postingUrl") or job.url
    job.pay_min, job.pay_max = extract_pay(job.description)


# Sources whose list call has no description. Each maps to a function that fetches the full posting and raises on
# failure. gov_sources.py registers its own entry when imported.
DETAIL_FILLERS = {"workday": fill_workday_detail, "smartrecruiters": fill_smartrecruiters_detail}


def fill_detail(job: Job) -> None:
    DETAIL_FILLERS[job.source](job)


# ----------------------------------------------------------------------------- Remotive (feed)
def fetch_remotive(categories: list[str]) -> list[Job]:
    """Remotive's public feed of remote jobs. Their terms ask for a link back to the Remotive page and a mention
    of Remotive as the source (both kept: Job.url is the Remotive page, Job.source is 'remotive') and no more
    than about four requests a day (main.py enforces a minimum gap between fetches)."""
    seen: dict[int, Job] = {}
    for cat in categories:
        data = _get("https://remotive.com/api/remote-jobs", params={"category": cat})
        for j in data.get("jobs", []):
            if j["id"] in seen:
                continue
            desc = strip_html(j.get("description", ""))
            lo, hi = extract_pay(j.get("salary") or "")
            if lo is None:
                lo, hi = extract_pay(desc)
            seen[j["id"]] = Job(
                id=f"rm:{j['id']}", source="remotive", company=j.get("company_name", ""),
                title=j.get("title", ""), location=j.get("candidate_required_location") or "Remote",
                url=j.get("url", ""), posted=(j.get("publication_date") or "")[:10] or None,
                description=desc, pay_min=lo, pay_max=hi,
            )
    return list(seen.values())

