"""Government job sources.

- USAJobs: the federal government's official search API. Needs a free key (https://developer.usajobs.gov/apirequest),
  read from USAJOBS_API_KEY and USAJOBS_EMAIL (the email the key was issued to) in .env.
- Texas HHS careers (HHSC, DFPS, DSHS at careers.hhs.texas.gov): no API exists, so this reads the public search
  pages and job pages the site serves to every visitor. Its robots.txt allows these paths. It is the most fragile
  source here: if the site's layout changes, it returns nothing and the run says so.

Not included on purpose: WorkInTexas.com and governmentjobs.com (NEOGOV), whose robots.txt tells automated programs
to stay out.
"""
from __future__ import annotations

import html
import re
import time
from datetime import datetime

import requests

import sources
from sources import Job, extract_pay, strip_html

GOV_SOURCES = ("usajobs", "texas_hhs")

HHS_BASE = "https://careers.hhs.texas.gov"
HHS_PAGE = 25
ANNUALIZE = {"annual": 1, "annually": 1, "yearly": 1, "monthly": 12, "biweekly": 26, "bi-weekly": 26,
             "weekly": 52, "hourly": 2080}


# ----------------------------------------------------------------------------- USAJobs
def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_usajobs(data: dict) -> list[Job]:
    """Turn one page of a USAJobs search response into Jobs. Missing fields are tolerated."""
    jobs = []
    for item in (data.get("SearchResult") or {}).get("SearchResultItems", []) or []:
        d = item.get("MatchedObjectDescriptor") or {}
        jid = item.get("MatchedObjectId") or d.get("PositionID")
        if not jid or not d.get("PositionTitle"):
            continue
        where = "; ".join(l.get("LocationName", "") for l in d.get("PositionLocation", []) or [] if l.get("LocationName"))
        details = (d.get("UserArea") or {}).get("Details") or {}
        desc = "\n\n".join(x for x in (strip_html(details.get("JobSummary", "")), strip_html(d.get("QualificationSummary", ""))) if x)
        lo = hi = None
        for p in d.get("PositionRemuneration", []) or []:
            if (p.get("RateIntervalCode") or "").upper() == "PA" or (p.get("Description") or "").lower() == "per year":
                a, b = _num(p.get("MinimumRange")), _num(p.get("MaximumRange"))
                if a and b:
                    lo, hi = int(a), int(b)
                    break
        if lo is None:
            lo, hi = extract_pay(desc)
        jobs.append(Job(
            id=f"usajobs:{jid}", source="usajobs", company=d.get("OrganizationName") or d.get("DepartmentName") or "US Government",
            title=d["PositionTitle"], location=where, url=d.get("PositionURI", ""),
            posted=(d.get("PublicationStartDate") or "")[:10] or None, description=desc, pay_min=lo, pay_max=hi,
        ))
    return jobs


def fetch_usajobs(api_key: str, email: str, keywords: list[str], days: int = 30, pages: int = 2, delay: float = 0.4) -> list[Job]:
    """One search per keyword, newest within `days`. USAJobs requires the registered email as the User-Agent."""
    headers = {"Host": "data.usajobs.gov", "User-Agent": email, "Authorization-Key": api_key}
    seen: dict[str, Job] = {}
    for kw in keywords:
        for page in range(1, pages + 1):
            r = requests.get("https://data.usajobs.gov/api/search", headers=headers, timeout=sources.TIMEOUT,
                             params={"Keyword": kw, "ResultsPerPage": 50, "Page": page, "DatePosted": days})
            r.raise_for_status()
            got = parse_usajobs(r.json())
            for j in got:
                seen.setdefault(j.id, j)
            time.sleep(delay)
            if len(got) < 50:
                break
    return list(seen.values())


# ----------------------------------------------------------------------------- Texas HHS
TILE_SPLIT = '<div class="job-tile-cell">'
TILE_RE = re.compile(r"Title (?P<title>.+?) Agency (?P<agency>.+?) Occupational Category (?P<cat>.+?) "
                     r"Location (?P<loc>.+?) Posting Date (?P<date>[A-Z][a-z]{2} \d{1,2}, \d{4})")
HREF_RE = re.compile(r'class="jobTitle-link[^"]*"[^>]*href="(?P<href>/[a-z]+jobs/job/[^"]+/(?P<id>\d+)/)"')


def _flat(markup: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", markup))).strip()


def parse_hhs_results(page: str) -> list[Job]:
    """One page of careers.hhs.texas.gov/search/ results. Each job is a tile that appears once per layout
    (desktop, mobile), so the first match in a tile is used."""
    jobs = []
    for tile in page.split(TILE_SPLIT)[1:]:
        link = HREF_RE.search(tile)
        if not link:
            continue
        info = TILE_RE.search(_flat(tile))
        title = _flat(re.search(r"jobTitle-link[^>]*>(.*?)</a>", tile, re.S).group(1)) if "jobTitle-link" in tile else ""
        if not info or not title:
            continue
        try:
            posted = datetime.strptime(info["date"], "%b %d, %Y").date().isoformat()
        except ValueError:
            posted = None
        jobs.append(Job(
            id=f"tx:{link['id']}", source="texas_hhs", company=f"Texas {info['agency'].strip()}", title=title,
            location=info["loc"].strip(), url=HHS_BASE + link["href"], posted=posted,
            detail={"category": info["cat"].strip()},
        ))
    return jobs


def fetch_texas_hhs(search_terms: list[str], pages: int = 2, delay: float = 0.4) -> list[Job]:
    """The site ranks every posting by relevance to the search words, 25 per page, so a couple of pages per
    word covers the best matches."""
    seen: dict[str, Job] = {}
    for term in search_terms:
        for page in range(pages):
            r = requests.get(f"{HHS_BASE}/search/", headers=sources.UA, timeout=sources.TIMEOUT,
                             params={"q": term, "startrow": page * HHS_PAGE})
            r.raise_for_status()
            got = parse_hhs_results(r.text)
            for j in got:
                seen.setdefault(j.id, j)
            time.sleep(delay)
            if len(got) < HHS_PAGE:
                break
    return list(seen.values())


def parse_salary(text: str) -> tuple[int | None, int | None]:
    """'Salary Range: $5,425.33 - $8,886.16 Pay Frequency: Monthly' -> annual (65104, 106633)."""
    m = re.search(r"Salary Range:\s*\$\s*([\d,]+(?:\.\d+)?)\s*(?:-|to)\s*\$?\s*([\d,]+(?:\.\d+)?)"
                  r"(?:\s*Pay Frequency:\s*([A-Za-z-]+))?", text or "")
    if not m:
        return extract_pay(text)
    factor = ANNUALIZE.get((m.group(3) or "annual").lower())
    if factor is None:
        return None, None
    lo, hi = (float(x.replace(",", "")) * factor for x in (m.group(1), m.group(2)))
    return int(lo), int(hi)


def fill_texas_hhs_detail(job: Job) -> None:
    """Read one posting's page for what the search list lacks: the description, the salary range and the exact date."""
    r = requests.get(job.url, headers=sources.UA, timeout=sources.TIMEOUT)
    r.raise_for_status()
    body = re.search(r'<span class="jobdescription">(.*?)</span>\s*(?:</div>|<div)', r.text, re.S)
    if not body:
        raise ValueError("job page layout not recognized")
    job.description = strip_html(body.group(1))
    job.pay_min, job.pay_max = parse_salary(_flat(body.group(1)))
    posted = re.search(r'<meta itemprop="datePosted" content="[A-Za-z]{3} ([A-Za-z]{3} \d{1,2}) [\d:]+ [A-Z]+ (\d{4})"', r.text)
    if posted:
        try:
            job.posted = datetime.strptime(f"{posted.group(1)} {posted.group(2)}", "%b %d %Y").date().isoformat()
        except ValueError:
            pass


sources.DETAIL_FILLERS["texas_hhs"] = fill_texas_hhs_detail
