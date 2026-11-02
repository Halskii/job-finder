"""Government job sources.

- USAJobs: the federal government's official search API. Needs a free key (https://developer.usajobs.gov/apirequest),
  read from USAJOBS_API_KEY and USAJOBS_EMAIL (the email the key was issued to) in .env.
- Texas HHS careers (HHSC, DFPS, DSHS at careers.hhs.texas.gov): no API exists, so this reads the public search
  pages and job pages the site serves to every visitor. Its robots.txt allows these paths. It is the most fragile
  source here: if the site's layout changes, it returns nothing and the run says so.
- State job sites. Each state posts on its own system, so each one is its own small reader, listed in STATE_SOURCES and
  switched on and off under `government:` in config.yaml. Maryland, Connecticut and Delaware run on JobAps, whose home page lists every current recruitment.
  Florida, Vermont, Indiana, Arkansas and Illinois run on the same SAP
  SuccessFactors career site software as Texas HHS, so they share the search loop and the job page reader.

Not included on purpose: WorkInTexas.com and governmentjobs.com (NEOGOV), whose robots.txt tells automated programs
to stay out. Many states post there, so those states can only be reached where they also keep a job site of their own.
"""
from __future__ import annotations

import html
import json
import re
import time
from datetime import datetime, timezone

import requests
from urllib.parse import unquote

import sources
from sources import Job, extract_pay, strip_html

GOV_SOURCES = ("usajobs", "texas_hhs", "florida", "vermont", "indiana", "arkansas", "illinois", "new_york", "maryland", "connecticut", "delaware", "arizona", "missouri", "wisconsin", "massachusetts", "montana", "utah", "kentucky", "south_dakota", "california", "kansas", "minnesota", "north_dakota", "tennessee")

HHS_BASE = "https://careers.hhs.texas.gov"
HHS_PAGE = 25
FL_BASE = "https://jobs.myflorida.com"
VT_BASE = "https://careers.vermont.gov"
IN_BASE = "https://workforindiana.in.gov"
AR_BASE = "https://arcareers.arkansas.gov"
IL_BASE = "https://illinois.jobs2web.com"
# (config key, label for the run log, name of the fetch function here, default search words): one line per state job site.
# The SuccessFactors sites (Florida to Illinois) are searched with these words; every other site lists all its jobs at once, so it takes none.
STATE_SOURCES = [("florida", "Florida", "fetch_florida", ["developer", "programmer", "software", "engineer", "devops"]),
                 ("vermont", "Vermont", "fetch_vermont", ["developer", "programmer", "software", "engineer", "devops"]),
                 ("indiana", "Indiana", "fetch_indiana", ["developer", "programmer", "software", "engineer", "devops"]),
                 ("arkansas", "Arkansas", "fetch_arkansas", ["developer", "programmer", "software", "engineer", "devops"]),
                 ("illinois", "Illinois", "fetch_illinois", ["developer", "programmer", "software", "engineer", "devops"]),
                 ("new_york", "New York", "fetch_new_york", []),
                 ("maryland", "Maryland", "fetch_maryland", []),
                 ("connecticut", "Connecticut", "fetch_connecticut", []),
                 ("delaware", "Delaware", "fetch_delaware", []),
                 ("arizona", "Arizona", "fetch_arizona", []),
                 ("missouri", "Missouri", "fetch_missouri", []),
                 ("wisconsin", "Wisconsin", "fetch_wisconsin", []),
                 ("massachusetts", "Massachusetts", "fetch_massachusetts", []),
                 ("montana", "Montana", "fetch_montana", []),
                 ("utah", "Utah", "fetch_utah", []),
                 ("kentucky", "Kentucky", "fetch_kentucky", []),
                 ("south_dakota", "South Dakota", "fetch_south_dakota", []),
                 ("california", "California", "fetch_california", []),
                 ("kansas", "Kansas", "fetch_kansas", []),
                 ("minnesota", "Minnesota", "fetch_minnesota", []),
                 ("north_dakota", "North Dakota", "fetch_north_dakota", []),
                 ("tennessee", "Tennessee", "fetch_tennessee", [])]
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


def fetch_successfactors(base: str, parse, search_terms: list[str], pages: int = 2, delay: float = 0.4) -> list[Job]:
    """A SuccessFactors career site ranks every posting by relevance to the search words, 25 per page, so a couple of
    pages per word covers the best matches. `parse` turns one page of results into Jobs."""
    seen: dict[str, Job] = {}
    for term in search_terms:
        for page in range(pages):
            r = requests.get(f"{base}/search/", headers=sources.UA, timeout=sources.TIMEOUT,
                             params={"q": term, "startrow": page * HHS_PAGE})
            r.raise_for_status()
            got = parse(r.text)
            for j in got:
                seen.setdefault(j.id, j)
            time.sleep(delay)
            if len(got) < HHS_PAGE:
                break
    return list(seen.values())


def fetch_texas_hhs(search_terms: list[str], pages: int = 2, delay: float = 0.4) -> list[Job]:
    return fetch_successfactors(HHS_BASE, parse_hhs_results, search_terms, pages, delay)


# ----------------------------------------------------------------------------- Florida
def _cell(row: str, css: str) -> str:
    m = re.search(rf'<td class="{css}[^"]*"[^>]*>(.*?)</td>', row, re.S)
    return _flat(m.group(1)) if m else ""


def _sf_title(title: str, state: str) -> str:
    """Career sites add extras to many titles: " - Tallahassee, Florida" at the end, and the agency's position numbers
    ("886199 - GOVERNMENT OPERATIONS CONSULTANT III - 40000018 1"). Neither helps a reader or the title filters."""
    title = re.sub(rf"\s+-\s+[^-]+,\s*{re.escape(state)}$", "", title)
    title = re.sub(r"\s+-\s+\d{5,}(\s+\d+)?$", "", title)
    return re.sub(r"^\d{5,}\s+-\s+", "", title).strip()


def parse_sf_rows(page: str, base: str, source: str, prefix: str, state: str, letters: str = "") -> list[Job]:
    """One page of results from a SuccessFactors career site that lists jobs as table rows (Florida, Vermont): one row per job,
    each repeated in a phone layout, so the first link in the row is used. A row that does not look right is skipped."""
    jobs = []
    for row in page.split('<tr class="data-row">')[1:]:
        row = row.split("</tr>")[0]
        link = next((m for m in re.finditer(r"<a\b([^>]*)>(.*?)</a>", row, re.S) if "jobTitle-link" in m.group(1)), None)
        href = re.search(r'href="(/job/[^"]+/(\d+)/)"', link.group(1)) if link else None
        title = _flat(link.group(2)) if link else ""
        if not href or not title:
            continue
        agency = re.search(r'<span class="jobFacility">([^<]*)', row)
        agency = html.unescape(agency.group(1)).strip() if agency else ""
        category = _cell(row, "colDepartment")
        if agency.startswith("$"):   # Indiana's facility column holds the pay, not an agency
            agency = ""
        if not agency:        # some sites (Vermont) have no facility column: their department is the agency
            agency, category = category, ""
        if letters and agency.startswith(letters + " "):   # Arkansas writes "AR DEPT OF HUMAN SERVICES"
            agency = agency[len(letters) + 1:]
        where, _, _ = _cell(row, "colLocation").partition(", US")      # "TALLAHASSEE, FL, US, 32399" -> "TALLAHASSEE, FL"
        city, _, rest = where.partition(",")
        date = re.search(r'<span class="jobDate">\s*([A-Z][a-z]{2} \d{1,2}, \d{4})', row)
        try:
            posted = datetime.strptime(date.group(1), "%b %d, %Y").date().isoformat() if date else None
        except ValueError:
            posted = None
        jobs.append(Job(
            id=f"{prefix}:{href.group(2)}", source=source,
            company=(agency if state.lower() in agency.lower() else f"{state} {agency}") if agency else f"State of {state}",
            title=_sf_title(title, state),
            location=f"{city.strip().title()},{rest}" if rest else city.strip().title(),
            url=base + href.group(1), posted=posted,
            detail={"category": category},
        ))
    return jobs


def parse_sf_tiles(page: str, base: str, source: str, prefix: str, state: str, letters: str) -> list[Job]:
    """One page of results from a SuccessFactors career site that lists jobs as tiles (Illinois): each tile holds the title
    link, location, date and agency. A tile that does not look right is skipped."""
    jobs = {}
    for tile in re.findall(r'<li class="job-tile[^"]*"[^>]*>.*?</li>', page, re.S):
        link = re.search(r'<a class="jobTitle-link[^"]*"[^>]*href="(/job/[^"]+/(\d+)/)"[^>]*>(.*?)</a>', tile, re.S)
        title = _flat(link.group(3)) if link else ""
        if not link or not title:
            continue
        field = lambda name: (lambda m: _flat(m.group(1)) if m else "")(re.search(rf'-desktop-section-{name}-value">(.*?)</div>', tile, re.S))
        where, _, _ = field("location").partition(", US")
        city, _, rest = where.partition(",")
        try:
            posted = datetime.strptime(field("date"), "%b %d, %Y").date().isoformat()
        except ValueError:
            posted = None
        agency = field("dept")
        jobs.setdefault(f"{prefix}:{link.group(2)}", Job(
            id=f"{prefix}:{link.group(2)}", source=source,
            company=(agency if state.lower() in agency.lower() else f"{state} {agency}") if agency else f"State of {state}",
            title=_sf_title(title, state), location=f"{city.strip().title()},{rest}" if rest else city.strip().title(),
            url=base + link.group(1), posted=posted, detail={},
        ))
    return list(jobs.values())


def parse_florida_results(page: str) -> list[Job]:
    return parse_sf_rows(page, FL_BASE, "florida", "fl", "Florida")


def parse_vermont_results(page: str) -> list[Job]:
    return parse_sf_rows(page, VT_BASE, "vermont", "vt", "Vermont")


def fetch_florida(search_terms: list[str], pages: int = 2, delay: float = 0.4) -> list[Job]:
    return fetch_successfactors(FL_BASE, parse_florida_results, search_terms, pages, delay)


def parse_indiana_results(page: str) -> list[Job]:
    return parse_sf_rows(page, IN_BASE, "indiana", "in", "Indiana")


def parse_arkansas_results(page: str) -> list[Job]:
    return parse_sf_rows(page, AR_BASE, "arkansas", "ar", "Arkansas", "AR")


def parse_illinois_results(page: str) -> list[Job]:
    return parse_sf_tiles(page, IL_BASE, "illinois", "il", "Illinois", "IL")


def fetch_indiana(search_terms: list[str], pages: int = 2, delay: float = 0.4) -> list[Job]:
    return fetch_successfactors(IN_BASE, parse_indiana_results, search_terms, pages, delay)


def fetch_arkansas(search_terms: list[str], pages: int = 2, delay: float = 0.4) -> list[Job]:
    return fetch_successfactors(AR_BASE, parse_arkansas_results, search_terms, pages, delay)


def fetch_illinois(search_terms: list[str], pages: int = 2, delay: float = 0.4) -> list[Job]:
    return fetch_successfactors(IL_BASE, parse_illinois_results, search_terms, pages, delay)


def fetch_vermont(search_terms: list[str], pages: int = 2, delay: float = 0.4) -> list[Job]:
    return fetch_successfactors(VT_BASE, parse_vermont_results, search_terms, pages, delay)


# ----------------------------------------------------------------------------- JobAps (Maryland, Connecticut)
JOBAPS_BASE = "https://www.jobapscloud.com"
JOBAPS_STATES = {"maryland": ("MD", "md", "Maryland", "MD"), "connecticut": ("CT", "ct", "Connecticut", "CT"),
                 "delaware": ("DE", "de", "Delaware", "DE")}   # source -> (site code, id prefix, name, state letters)
NOISE = re.compile(r"ï¿½|Â|\ufffd")
UNITS = {"year": 1, "yr": 1, "yearly": 1, "annual": 1, "annually": 1, "month": 12, "monthly": 12, "week": 52, "weekly": 52,
         "hour": 2080, "hourly": 2080, "biweekly": 26}


def _jobaps_pay(text: str) -> tuple[int | None, int | None]:
    """'$55,710.00 - $57,961.00/ year ...' -> (55710, 57961); hourly and monthly figures become yearly."""
    m = re.search(r"\$\s*([\d,]+(?:\.\d+)?)(?:\s*(?:-|to)\s*\$?\s*([\d,]+(?:\.\d+)?))?\s*(?:/|per\s+)?\s*([A-Za-z]+)?", text or "")
    factor = UNITS.get((m.group(3) or "year").lower(), 1) if m else None    # a word that is not a pay period ("Grade") means yearly
    if not m:
        return None, None
    lo, hi = (float(x.replace(",", "")) * factor for x in (m.group(1), m.group(2) or m.group(1)))
    return int(lo), int(hi)


def _jobaps_place(text: str, letters: str) -> str:
    """The work location cell: usually a county or city, but Delaware writes a site name and street address
    ("Stockley Center: (26351 Patriots Way, Georgetown, DE, 19947)"), which is cut down to "Georgetown, DE"."""
    text = re.sub(r"<br\s*/?>?", " ", text).strip()
    addr = re.search(rf"([A-Za-z][A-Za-z .'-]+),\s*{letters},\s*\d{{5}}", text)
    if addr:
        return f"{addr.group(1).strip()}, {letters}"
    return text if ("," in text or not text) else f"{text}, {letters}"


def parse_jobaps_results(page: str, source: str) -> list[Job]:
    """The 'Current Recruitments' tables on a JobAps state home page: a row per job with its title and number, work location,
    agency and (on some sites) salary. The sample-application and on-file rows are not jobs and are skipped."""
    code, prefix, state, letters = JOBAPS_STATES[source]
    page = re.sub(r"<!--.*?-->", "", page, flags=re.S)
    jobs: dict[str, Job] = {}
    for row in re.findall(r"<tr[^>]*>.*?</tr>", page, re.S):
        link = re.search(r'<a href="([^"]*bulpreview\.asp\?[^"]*R1=(\w+)&amp;R2=(\w+)&amp;R3=(\w+))"[^>]*class="JobTitle[^"]*"[^>]*>(.*?)</a>', row, re.S)
        if not link or link.group(2) == "AF" or link.group(2).startswith("PR"):    # the on-file and practice applications are not jobs
            continue
        title = re.sub(r"^\W*REPOST\W*\s*", "", _flat(link.group(5))).strip()
        if not title:
            continue
        loc = _cell(row, "Locs")
        dept = _cell(row, "Dept")
        lo, hi = _jobaps_pay(_cell(row, "Salary"))
        jid = f"{prefix}:{link.group(2)}-{link.group(3)}-{link.group(4)}"
        jobs.setdefault(jid, Job(
            id=jid, source=source, company=dept if state.lower() in dept.lower() else f"{state} {dept}".strip(), title=title,
            location=_jobaps_place(loc, letters), url=html.unescape(link.group(1)), pay_min=lo, pay_max=hi,
        ))
    return list(jobs.values())


def fetch_jobaps(source: str) -> list[Job]:
    """One request: the state's home page lists every current recruitment, so there is nothing to search or page through."""
    r = requests.get(f"{JOBAPS_BASE}/{JOBAPS_STATES[source][0]}/", headers=sources.UA, timeout=sources.TIMEOUT)
    r.raise_for_status()
    return parse_jobaps_results(r.content.decode("utf-8", errors="replace"), source)


def fetch_maryland(search_terms=None, pages: int = 1, delay: float = 0.4) -> list[Job]:
    return fetch_jobaps("maryland")


def fetch_connecticut(search_terms=None, pages: int = 1, delay: float = 0.4) -> list[Job]:
    return fetch_jobaps("connecticut")


def fetch_delaware(search_terms=None, pages: int = 1, delay: float = 0.4) -> list[Job]:
    return fetch_jobaps("delaware")


def _job_posting_data(content: bytes) -> dict:
    """The schema.org JobPosting data a job page carries in a <script type="application/ld+json"> block, or {}."""
    found = re.search(r"<script type=.application/ld\+json.>(.*?)</script>", content.decode("utf-8", errors="replace"), re.S)
    try:
        data = json.loads(found.group(1), strict=False) if found else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _fill_from_job_posting(job: Job, data: dict) -> None:
    """Description, date posted and pay from JobPosting data. Raises ValueError when there is no text, so the job is skipped."""
    text = NOISE.sub(" ", (data.get("description") or "").replace("\\r\\n", "\n").replace("\\n", "\n"))
    if not strip_html(text):
        raise ValueError("job page layout not recognized")
    job.description = re.sub(r"\n{3,}", "\n\n", strip_html(text)).strip()
    if re.match(r"\d{4}-\d{2}-\d{2}", data.get("datePosted") or ""):
        job.posted = data["datePosted"][:10]
    value = ((data.get("baseSalary") or {}).get("value") or {})
    try:
        factor = UNITS.get(str(value.get("unitText", "year")).lower())
        if factor and value.get("minValue") and value.get("maxValue"):
            job.pay_min, job.pay_max = int(float(value["minValue"]) * factor), int(float(value["maxValue"]) * factor)
    except (TypeError, ValueError):
        pass


def fill_jobaps_detail(job: Job) -> None:
    """A recruitment's page carries the posting as JobPosting data: the full text, the date posted and the pay."""
    r = requests.get(job.url, headers=sources.UA, timeout=sources.TIMEOUT)
    r.raise_for_status()
    _fill_from_job_posting(job, _job_posting_data(r.content))


# ----------------------------------------------------------------------------- Arizona
AZ_BASE = "https://www.azstatejobs.gov"
AZ_PAGE = 30
AZ_DELAY = 5   # the site's robots.txt asks for a 5 second crawl delay


def parse_arizona_results(page: str) -> list[Job]:
    """One page of azstatejobs.gov/jobs/search: a table with one row per job."""
    jobs = []
    for row in re.findall(r"<tr\b[^>]*data-job-url=[^>]*>.*?</tr>", page, re.S):
        url = re.search(r'data-job-url="([^"]+)"', row)
        title = re.search(r'class="job-search-results-title"[^>]*>\s*<a[^>]*>(.*?)</a>', row, re.S)
        slug = re.search(r"/jobs/([^/?#]+)$", html.unescape(url.group(1))) if url else None   # the end of the address names the posting
        if not (url and title and slug and _flat(title.group(1))):
            continue
        dept = re.search(r'class="job-search-results-department"[^>]*>.*?<li[^>]*>(.*?)</li>', row, re.S)
        places = re.search(r'class="job-search-results-location"[^>]*>(.*?)</td>', row, re.S)
        cities = [c.title() for c in (_flat(x) for x in re.findall(r"<li[^>]*>(.*?)</li>", places.group(1), re.S)) if c] if places else []
        closing = re.search(r'class="job-search-results-closing-at"[^>]*>\s*([A-Z][a-z]{2} \d{1,2} \d{4})', row)
        try:
            deadline = datetime.strptime(closing.group(1), "%b %d %Y").date().isoformat() if closing else None
        except ValueError:
            deadline = None
        agency = _flat(dept.group(1)) if dept else ""
        jobs.append(Job(
            id=f"az:{slug.group(1)}"[:150], source="arizona", company=f"Arizona {agency.title()}" if agency else "State of Arizona",
            title=_flat(title.group(1)), location=", ".join(f"{c}, AZ" for c in cities) or "Arizona",
            url=html.unescape(url.group(1)), detail={"deadline": deadline},
        ))
    return jobs


def fetch_arizona(search_terms=None, pages: int = 20, delay: float = 0.4) -> list[Job]:
    """Walk the site's search pages (30 jobs each, about 20 pages), one every 5 seconds as its robots.txt asks."""
    seen: dict[str, Job] = {}
    for page in range(1, pages + 1):
        r = requests.get(f"{AZ_BASE}/jobs/search", headers=sources.UA, timeout=sources.TIMEOUT, params={"page": page})
        r.raise_for_status()
        got = parse_arizona_results(r.text)
        for j in got:
            seen.setdefault(j.id, j)
        if len(got) < AZ_PAGE:
            break
        time.sleep(max(delay, AZ_DELAY))
    return list(seen.values())


def fill_arizona_detail(job: Job) -> None:
    """The job page: JobPosting data (the text and the date), with the salary read from the text."""
    r = requests.get(job.url, headers=sources.UA, timeout=sources.TIMEOUT)
    r.raise_for_status()
    _fill_from_job_posting(job, _job_posting_data(r.content))
    pay = re.search(r"Salary:\s*(\$[^\n]{0,60})", job.description)
    job.pay_min, job.pay_max = _jobaps_pay(pay.group(1)) if pay else (None, None)


# ----------------------------------------------------------------------------- Missouri
MO_BASE = "https://mocareers.mo.gov"
MO_BOARD = "5effe9b2-4b89-494b-ac76-c45e25190768"   # the public board; the other one on the site is for current employees
MO_STALE_DAYS = 60   # a posting with no closing date that was last posted longer ago than this is treated as old


def _ms_date(ms) -> str | None:
    try:
        return datetime.fromtimestamp(int(ms) / 1000).date().isoformat() if int(ms) > 0 else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def parse_missouri_results(items: list, today: str | None = None) -> list[Job]:
    """The job board's JSON list of requisitions. Closed ones, and old ones that never got a closing date, are left out."""
    today = today or datetime.now().date().isoformat()
    cutoff = datetime.fromisoformat(today).toordinal() - MO_STALE_DAYS
    jobs = []
    for it in items if isinstance(items, list) else []:
        key, title = str(it.get("primaryKey") or ""), (it.get("title") or "").strip()
        closes, posted = _ms_date(it.get("closeTimestamp")), _ms_date(it.get("lastPostedTimestamp") or it.get("openTimestamp"))
        if not key or not title or (closes and closes < today) or (not closes and posted and datetime.fromisoformat(posted).toordinal() < cutoff):
            continue
        agency = (it.get("division") or "").strip()
        jobs.append(Job(
            id=f"mo:{key}", source="missouri", company=agency if "missouri" in agency.lower() else f"Missouri {agency}".strip(), title=title,
            location=(it.get("location") or "Missouri").strip(), url=f"{MO_BASE}/hiretrue/ce3/job-board/{MO_BOARD}/{it.get('externalId')}",
            posted=posted, detail={"category": (it.get("department") or "").strip(), "deadline": closes},
        ))
    return jobs


def fetch_missouri(search_terms=None, pages: int = 1, delay: float = 0.4) -> list[Job]:
    """Two requests: the board's number, then every requisition on it in one response (about 1,100)."""
    headers = {**sources.UA, "Accept": "application/json"}
    board = requests.get(f"{MO_BASE}/hiretrue/api/ce3/job-board", params={"externalId": MO_BOARD}, headers=headers, timeout=sources.TIMEOUT)
    board.raise_for_status()
    r = requests.get(f"{MO_BASE}/hiretrue/api/ce3/job-board/requisitions", params={"jobBoardPrimaryKey": board.json()["primaryKey"]},
                     headers=headers, timeout=sources.TIMEOUT)
    r.raise_for_status()
    return parse_missouri_results(r.json())


def fill_missouri_detail(job: Job) -> None:
    """The requisition's own JSON: the description sections and the salary."""
    r = requests.get(f"{MO_BASE}/hiretrue/api/ce3/job-board/requisitions/{job.id.split(':', 1)[1]}",
                     headers={**sources.UA, "Accept": "application/json"}, timeout=sources.TIMEOUT)
    r.raise_for_status()
    d = r.json()
    parts = [strip_html(d.get(f"descriptionSection{i}") or "") for i in range(1, 6)]
    if not any(parts):
        raise ValueError("job page layout not recognized")
    job.description = "\n\n".join(x for x in parts if x)
    unit = {"peryear": "year", "perhour": "hour", "permonth": "month", "perweek": "week"}.get(str(d.get("salaryType", "")).lower(), "year")
    job.pay_min, job.pay_max = _jobaps_pay(f"{str(d.get('salary') or '').replace('-', ' - ', 1)} per {unit}")


# ----------------------------------------------------------------------------- PeopleSoft Candidate Gateway (Kansas, Minnesota)
# source -> (gateway address, the page that opens the search, the posting page's SiteId, id prefix, state name, state letters,
#            what to send to run an empty search (or None when the opening page already lists jobs))
PS_SITES = {
    "kansas": ("https://jobs.sok.ks.gov/psc/sokhrprdcg/APPLICANT/HRMS/c/HRS_HRAM_FL.HRS_CG_SEARCH_FL.GBL", "?Page=HRS_APP_SCHJOB_FL&Action=U", "1",
               "ks", "Kansas", "KS", None),
    "minnesota": ("https://careers.mn.gov/psc/mnjob/MNCAREERS/HRCR/c/HRS_HRAM_FL.HRS_CG_SEARCH_FL.GBL",
                  "?Page=HRS_CAREERS_FL&Action=U&FOCUS=Applicant&SiteId=1001", "1001", "mn", "Minnesota", "MN",
                  {"ICAction": "HRS_SCH_WRK_FLU_HRS_SEARCH_BTN", "HRS_SCH_WRK_HRS_SCH_TEXT100": ""}),
}
PS_VISIT_HOURS = {"minnesota": (0, 12)}   # Minnesota's robots.txt asks for visits between midnight and noon GMT


def _ps_value(page: str, name: str, n: str) -> str:
    m = re.search(rf"id=['\"]{name}\${n}['\"][^>]*>(.*?)</span>", page, re.S)
    return _flat(m.group(1)) if m else ""


def parse_peoplesoft_results(page: str, source: str) -> list[Job]:
    """The job rows of a PeopleSoft Candidate Gateway results page: each row's fields are spans named FIELD$row. Internal-only
    postings (Kansas marks them) are left out."""
    gateway, _, site, prefix, state, letters, _ = PS_SITES[source]
    jobs = {}
    for n in dict.fromkeys(re.findall(r"id=['\"]SCH_JOB_TITLE\$(\d+)['\"]", page)):
        title, jid = _ps_value(page, "SCH_JOB_TITLE", n), _ps_value(page, "HRS_APP_JBSCH_I_HRS_JOB_OPENING_ID", n)
        if not title or not jid or _ps_value(page, "KS_POSTING_TYPE", n).lower() == "internal":
            continue
        agency = _ps_value(page, "KS_AGENCY_DESC", n) or _ps_value(page, "HRS_APP_JBSCH_I_HRS_DEPT_DESCR", n)
        where = _ps_value(page, "LOCATION", n)
        dates = {}
        for key, field in (("posted", "SCH_OPENED"), ("deadline", "HRS_CLS_DT_DESCR")):
            try:
                dates[key] = datetime.strptime(_ps_value(page, field, n), "%m/%d/%Y").date().isoformat()
            except ValueError:
                dates[key] = None
        jobs.setdefault(f"{prefix}:{jid}", Job(
            id=f"{prefix}:{jid}", source=source, company=agency if state.lower() in agency.lower() else f"{state} {agency}".strip() or f"State of {state}",
            title=title, location=where if "," in where or not where else f"{where}, {letters}",
            url=f"{gateway}?Page=HRS_APP_JBPST_FL&Action=U&FOCUS=Applicant&SiteId={site}&JobOpeningId={jid}&PostingSeq=1",
            posted=dates["posted"], detail={"deadline": dates["deadline"], "category": _ps_value(page, "JOB_FAMILY_LABEL", n)},
        ))
    return list(jobs.values())


def fetch_peoplesoft(source: str, pages: int = 20, delay: float = 1.0) -> list[Job]:
    """Open the search page (with cookies), run an empty search where the site needs one, then press its 'more' button over and over:
    each answer holds every row so far, and each form carries state that must be sent back exactly as the last answer gave it."""
    gateway, start, _, _, _, _, search = PS_SITES[source]
    window = PS_VISIT_HOURS.get(source)
    if window and not window[0] <= datetime.now(timezone.utc).hour < window[1]:
        raise RuntimeError(f"outside the hours the site's robots.txt asks for ({window[0]:02d}:00 to {window[1]:02d}:00 GMT)")
    s = requests.Session()
    s.headers.update(sources.UA)
    page = s.get(gateway + start, timeout=sources.TIMEOUT).text
    if search:
        time.sleep(delay)
        page = s.post(gateway, data={**_hidden_fields(page), **search}, timeout=60).text
    jobs: dict[str, Job] = {}
    for _ in range(pages):
        got = parse_peoplesoft_results(page, source)
        before = len(jobs)
        for j in got:
            jobs.setdefault(j.id, j)
        if "HRS_AGNT_RSLT_I$hdown$0" not in page or (_ > 0 and len(jobs) == before):
            break                                  # no 'more' button, or pressing it added nothing
        time.sleep(delay)
        page = s.post(gateway, data={**_hidden_fields(page), "ICAction": "HRS_AGNT_RSLT_I$hdown$0"}, timeout=60).text
    return list(jobs.values())


def fetch_kansas(search_terms=None, pages: int = 20, delay: float = 1.0) -> list[Job]:
    return fetch_peoplesoft("kansas", pages, max(delay, 1.0))


def fetch_minnesota(search_terms=None, pages: int = 20, delay: float = 1.0) -> list[Job]:
    return fetch_peoplesoft("minnesota", pages, max(delay, 1.0))


# ----------------------------------------------------------------------------- North Dakota (the state's job feed)
ND_FEED = "https://www.nd.gov/sites/www/files/careers/Careers.xml"
ND_PAY = {"M": 12, "H": 2080, "B": 26, "W": 52, "A": 1, "Y": 1}


def parse_north_dakota_results(xml_text: str, today: str | None = None) -> list[Job]:
    """The state's published feed of open jobs. It gives the facts of each posting (agency, department, kind of work, pay, place,
    dates) but no description, and the job pages it links to are disallowed to programs, so the text is those facts. Contact
    names, phones and emails in the feed are left out. Postings past their closing date are dropped."""
    import xml.etree.ElementTree as ET
    today = today or datetime.now().date().isoformat()
    try:
        nodes = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text).findall("Node")
    except ET.ParseError:
        return []
    jobs = []
    for n in nodes:
        f = lambda tag: (n.findtext(tag) or "").strip()
        jid, title, closes = f("JobId"), f("JobTitle"), f("CloseDate")
        if not jid or not title or f("Action") not in ("", "ADD") or (re.fullmatch(r"\d{4}-\d{2}-\d{2}", closes) and closes < today):
            continue
        agency = re.sub(r"^ND ", "North Dakota ", f("AgencyDescr")) or "State of North Dakota"
        factor = ND_PAY.get(f("PayFreq").upper())
        try:
            lo, hi = (int(float(f(k)) * factor) for k in ("MinRate", "MaxRate")) if factor else (None, None)
        except ValueError:
            lo = hi = None
        facts = [f"{title} at {agency}." if agency else title + ".", f"Department: {f('DepartmentDescr')}." if f("DepartmentDescr") else "",
                 f"Kind of work: {f('JobFuncDescr')}." if f("JobFuncDescr") else "",
                 "Full time." if f("FullPart") == "F" else "Part time." if f("FullPart") == "P" else "",
                 "Regular position." if f("RegTemp") == "R" else "Temporary position." if f("RegTemp") == "T" else "",
                 f"Openings: {f('NumOpenings')}." if f("NumOpenings") else "", f"Closes {closes}." if closes else ""]
        jobs.append(Job(
            id=f"nd:{jid}", source="north_dakota", company=agency, title=title,
            location=f"{f('City').title()}, {f('State') or 'ND'}" if f("City") else "North Dakota", url=html.unescape(f("JobURL")) or "https://www.nd.gov/omb/public/careers",
            posted=f("PostDate") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", f("PostDate")) else None, description=" ".join(x for x in facts if x),
            pay_min=lo, pay_max=hi, detail={"deadline": closes or None, "category": f("JobFuncDescr")},
        ))
    return jobs


def fetch_north_dakota(search_terms=None, pages: int = 1, delay: float = 0.4) -> list[Job]:
    """One request: the state's feed."""
    r = requests.get(ND_FEED, headers=sources.UA, timeout=sources.TIMEOUT)
    r.raise_for_status()
    return parse_north_dakota_results(r.content.decode("utf-8", errors="replace"))


# ----------------------------------------------------------------------------- Tennessee (Oracle APEX list, PeopleSoft job pages)
TN_HUB = "https://hub.edison.tn.gov"
TN_SEARCH = f"{TN_HUB}/ords/r/tn_er/tn-careers-external/job-search?clear=2"
TN_PAGE = 50
TN_ITEMS = ("P2_SEARCH", "P2_JOB_LOCATION_NAME", "P2_JOB_FAMILY", "P2_JOB_AGENCY_NAME", "P2_JOB_EMPLOYMENT_TYPE", "P2_JOB_CITY")


def parse_tennessee_results(page: str) -> list[Job]:
    """The rows of the job report (on the search page, or in one page of the report the page loads for itself): title with the
    posting's address, county, last day to apply, agency and job family."""
    jobs = {}
    for row in re.findall(r"<tr>\s*<td class=\"t-Report-cell\".*?</tr>", page, re.S):
        link = re.search(r'<a href="([^"]*JobOpeningId[^"]*)"[^>]*>(.*?)</a>', row, re.S)
        if not link:
            continue
        url = html.unescape(link.group(1))
        jid = re.search(r"JobOpeningId=(\d+)", url)
        title = re.sub(r"\s*-\s*\d{8}\s*-?\s*\d+\s*$", "", _flat(link.group(2)))     # the site adds " - 10302026- 82395" (date and number)
        field = lambda label: (lambda m: _flat(m.group(1)) if m else "")(re.search(rf'data-label="{label}">(.*?)</span>', row, re.S))
        if not jid or not title:
            continue
        agency, county = field("Agency"), field("Location")
        try:
            deadline = datetime.strptime(field("Last Day To Apply"), "%B %d, %Y").date().isoformat()
        except ValueError:
            deadline = None
        jobs.setdefault(f"tn:{jid.group(1)}", Job(
            id=f"tn:{jid.group(1)}", source="tennessee", company=agency if "tennessee" in agency.lower() else f"Tennessee {agency}".strip(),
            title=title, location=county if "," in county or not county else f"{county}, TN", url=url,
            detail={"deadline": deadline, "category": field("Job Family")},
        ))
    return list(jobs.values())


def fetch_tennessee(search_terms=None, pages: int = 20, delay: float = 1.0) -> list[Job]:
    """The search page lists the first 50 jobs. The rest come from the report's own paging request, which needs the page's session
    values (flow, instance, salt, protected items and the report's id) sent back, and ends when the page has no 'Next' link."""
    s = requests.Session()
    s.headers.update(sources.UA)
    page = s.get(TN_SEARCH, timeout=sources.TIMEOUT).text
    def value(ident: str) -> str:
        tag = re.search(rf'<input[^>]*id="{ident}"[^>]*>', page)
        found = re.search(r'value="([^"]*)"', tag.group(0)) if tag else None
        return html.unescape(found.group(1)) if found else ""
    report = re.search(r'apex\.widget\.report\.init\("JOB_SEARCH_RESULTS","([^"]+)"', page)
    if not report:
        raise ValueError("search page layout not recognized")
    seen: dict[str, Job] = {}
    for n in range(pages):
        got = parse_tennessee_results(page)
        before = len(seen)
        for j in got:
            seen.setdefault(j.id, j)
        if "t-Report-paginationLink--next" not in page or len(seen) == before:
            break
        time.sleep(delay)
        body = {"p_flow_id": value("pFlowId"), "p_flow_step_id": value("pFlowStepId") or "2", "p_instance": value("pInstance"), "p_debug": "",
                "p_request": "PLUGIN=" + json.loads('"' + report.group(1) + '"'), "p_widget_name": "classic_report", "p_widget_action": "paginate",
                "p_pg_min_row": str(TN_PAGE * (n + 1) + 1), "p_pg_max_rows": str(TN_PAGE), "p_pg_rows_fetched": str(TN_PAGE), "x01": "",
                "p_json": json.dumps({"pageItems": {"itemsToSubmit": [{"n": i, "v": ""} for i in TN_ITEMS], "protected": value("pPageItemsProtected"),
                                                     "rowVersion": ""}, "salt": value("pSalt")})}
        page = s.post(f"{TN_HUB}/ords/wwv_flow.ajax", data=body, timeout=60).text
    return list(seen.values())


def fill_tennessee_detail(job: Job) -> None:
    """The posting page is a PeopleSoft page like Wisconsin's; its summary table also holds the opening date and annual salary."""
    fill_peoplesoft_detail(job)
    opened = re.search(r"Opening Date/Time\W*(\d{2})/(\d{2})/(\d{4})", job.description)
    if opened:
        job.posted = f"{opened.group(3)}-{opened.group(1)}-{opened.group(2)}"
    annual = re.search(r"Salary \(Annually\)\W*\$([\d,]+(?:\.\d+)?)\s*-\s*\$([\d,]+(?:\.\d+)?)", job.description)
    if annual:
        job.pay_min, job.pay_max = (int(float(x.replace(",", ""))) for x in annual.groups())


# ----------------------------------------------------------------------------- Wisconsin
WI_LIST = "https://wisc.jobs/Pages/SearchResults.aspx?keywords=%25&city=&zip=&distance=25"
WI_JOB = ("https://wj.wi.gov/psc/wisjobs/CAREERS/HRMS/c/HRS_HRAM_FL.HRS_CG_SEARCH_FL.GBL"
          "?Page=HRS_APP_JBPST_FL&Action=U&FOCUS=Applicant&SiteId=1&PostingSeq=1&JobOpeningId={}")


def parse_wisconsin_results(page: str) -> list[Job]:
    """wisc.jobs puts every open posting in the page as the data of its results grid (a JSON array in a script)."""
    start = page.find("data: [")
    try:
        rows, _ = json.JSONDecoder().raw_decode(page[start + len("data: "):]) if start >= 0 else ([], 0)
    except ValueError:
        return []
    jobs = []
    for r in rows if isinstance(rows, list) else []:
        jid, title = str(r.get("JobId") or "").strip(), (r.get("PostingTitle") or "").strip()
        if not jid or not title:
            continue
        agency = (r.get("AgencyName") or "").strip()
        jobs.append(Job(
            id=f"wi:{jid}", source="wisconsin", company=agency if "wisconsin" in agency.lower() else f"Wisconsin {agency}".strip(), title=title,
            location=(lambda w: f"{w}, WI" if w and "," not in w else w or "Wisconsin")((r.get("Location") or "").strip()), url=WI_JOB.format(jid),
            posted=r.get("OpenDate") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", r.get("OpenDate") or "") else None,
            detail={"category": (r.get("JobCategory") or "").strip(), "deadline": r.get("CloseDate") or None},
        ))
    return jobs


def fetch_wisconsin(search_terms=None, pages: int = 1, delay: float = 0.4) -> list[Job]:
    """One request: the results page carries every posting."""
    r = requests.get(WI_LIST, headers=sources.UA, timeout=sources.TIMEOUT)
    r.raise_for_status()
    return parse_wisconsin_results(r.content.decode("utf-8", errors="replace"))


def fill_peoplesoft_detail(job: Job) -> None:
    """The posting page (a PeopleSoft Candidate Gateway page that needs no sign-in: Wisconsin, Kansas and Minnesota all use it): its headed sections of text, and the salary among them."""
    r = requests.get(job.url, headers=sources.UA, timeout=sources.TIMEOUT)
    r.raise_for_status()
    page = r.content.decode("utf-8", errors="replace")
    sections = []
    for n, body in re.findall(r"id=HRS_SCH_PSTDSC_DESCRLONG\$(\d+)\s*>(.*?)</span>\s*</DIV>", page, re.S):
        head = re.search(rf"id='HRS_SCH_WRK_DESCR100\${n}lbl'>(.*?)</span>", page, re.S)
        text = strip_html(body)
        if text:
            sections.append((_flat(head.group(1)) if head else "", text))
    if not sections:
        raise ValueError("job page layout not recognized")
    job.description = "\n\n".join(f"**{h}**\n\n{x}" if h else x for h, x in sections)
    salary = next((x for h, x in sections if "salary" in h.lower()), job.description)
    job.pay_min, job.pay_max = _jobaps_pay(salary)
    if job.pay_min is None:
        job.pay_min, job.pay_max = extract_pay(salary)


# ----------------------------------------------------------------------------- Taleo (Massachusetts, Montana)
# source -> (site, career section, portal number, time zone offset, time zone name, id prefix, company, state letters)
TALEO_SITES = {
    "massachusetts": ("https://massanf.taleo.net", "ex", "101430233", "GMT-05:00", "America/New_York", "ma", "State of Massachusetts", "MA"),
    "montana": ("https://mtstatejobs.taleo.net", "200", "101430233", "GMT-07:00", "America/Denver", "mt", "State of Montana", "MT"),
}
TALEO_FILTERS = ("POSTING_DATE", "LOCATION", "JOB_FIELD", "JOB_TYPE", "JOB_SCHEDULE", "JOB_LEVEL")
TALEO_ADVANCED = ("ORGANIZATION", "LOCATION", "JOB_FIELD", "JOB_NUMBER", "URGENT_JOB", "EMPLOYEE_STATUS", "STUDY_LEVEL", "WILL_TRAVEL", "JOB_SHIFT")


def _taleo_body(page: int) -> dict:
    """The search request a Taleo career section's own page sends, with nothing typed in: every job, newest first."""
    return {"multilineEnabled": False, "sortingSelection": {"sortBySelectionParam": "3", "ascendingSortingOrder": "false"},
            "fieldData": {"fields": {"KEYWORD": "", "LOCATION": ""}, "valid": True},
            "filterSelectionParam": {"searchFilterSelections": [{"id": i, "selectedValues": []} for i in TALEO_FILTERS]},
            "advancedSearchFiltersSelectionParam": {"searchFilterSelections": [{"id": i, "selectedValues": []} for i in TALEO_ADVANCED]},
            "pageNo": page}


def parse_taleo_results(data: dict, source: str) -> list[Job]:
    """One page of a Taleo career section's search: per job an id, a contest number and columns (title, location, then the
    posting date on some sites and the department on others)."""
    base, section, _, _, _, prefix, company, letters = TALEO_SITES[source]
    jobs = []
    for it in (data or {}).get("requisitionList") or []:
        cols = it.get("column") or []
        job_no, jid = str(it.get("contestNo") or ""), str(it.get("jobId") or "")
        if not job_no or not jid or not cols or not str(cols[0]).strip():
            continue
        try:
            place = json.loads(cols[1]) if len(cols) > 1 else []
        except ValueError:
            place = []
        where = re.sub(r"^[A-Za-z ]+-", "", (place[0] if place else "")) if place else ""   # "Massachusetts-Salem" -> "Salem"
        posted, category = None, ""
        if len(cols) > 2:
            try:
                posted = datetime.strptime(cols[2], "%b %d, %Y").date().isoformat()
            except ValueError:
                category = str(cols[2]).strip()
        jobs.append(Job(
            id=f"{prefix}:{jid}", source=source, title=str(cols[0]).strip(),
            company=(category if company.removeprefix("State of ") in category else f"{company.removeprefix('State of ')} {category}") if category else company,
            location=f"{where}, {letters}" if where and "multiple" not in where.lower() else (where or letters),
            url=f"{base}/careersection/{section}/jobdetail.ftl?job={job_no}&lang=en", posted=posted,
            detail={"job_no": job_no, "category": category},
        ))
    return jobs


def fetch_taleo(source: str, pages: int = 40, delay: float = 0.4) -> list[Job]:
    """Page through every posting (25 a page) with the career section's own search request."""
    base, section, portal, tz, tzname, *_ = TALEO_SITES[source]
    session = requests.Session()
    session.headers.update({**sources.UA, "Content-Type": "application/json", "Accept": "application/json", "X-Requested-With": "XMLHttpRequest",
                            "tz": tz, "tzname": tzname, "Referer": f"{base}/careersection/{section}/jobsearch.ftl?lang=en"})
    session.get(f"{base}/careersection/{section}/jobsearch.ftl?lang=en", timeout=sources.TIMEOUT)     # the first visit hands out its cookies
    seen: dict[str, Job] = {}
    for page in range(1, pages + 1):
        r = session.post(f"{base}/careersection/rest/jobboard/searchjobs", params={"lang": "en", "portal": portal},
                         data=json.dumps(_taleo_body(page)), timeout=sources.TIMEOUT)
        r.raise_for_status()
        data = r.json()
        got = parse_taleo_results(data, source)
        for j in got:
            seen.setdefault(j.id, j)
        paging = data.get("pagingData") or {}
        if not got or page * int(paging.get("pageSize") or 25) >= int(paging.get("totalCount") or 0):
            break
        time.sleep(delay)
    return list(seen.values())


def parse_massachusetts_results(data: dict) -> list[Job]:
    return parse_taleo_results(data, "massachusetts")


def fetch_massachusetts(search_terms=None, pages: int = 40, delay: float = 0.4) -> list[Job]:
    return fetch_taleo("massachusetts", pages, delay)


def fetch_montana(search_terms=None, pages: int = 40, delay: float = 0.4) -> list[Job]:
    return fetch_taleo("montana", pages, delay)


def fill_taleo_detail(job: Job) -> None:
    """A Taleo job page keeps its fields as a list of quoted strings in one script call. The text is the one that starts with !*!,
    the salary is the two money amounts just before the pay period, and the posting date is a date with a time. Some career
    sections have no such call; their hidden `initialHistory` field holds the same values, and the text is the longest one."""
    r = requests.get(job.url, headers=sources.UA, timeout=sources.TIMEOUT)
    r.raise_for_status()
    found = re.search(r"api\.fillList\('requisitionDescriptionInterface',\s*'descRequisition',\s*\[(.*?)\]\);", r.text, re.S)
    values = [v.encode().decode("unicode_escape", errors="ignore") if "\\" in v else v for v in re.findall(r"'((?:[^'\\]|\\.)*)'", found.group(1))] if found else []
    start = next((i for i, v in enumerate(values) if v.startswith("!*!")), None)
    if start is not None:
        job.description = strip_html(unquote(values[start][3:]))
    else:
        history = re.search(r'id="initialHistory"[^>]*value="([^"]*)"', r.text)
        parts = [unquote(html.unescape(x)) for x in html.unescape(history.group(1)).split("!|!")] if history else []
        text = max(parts, key=len, default="")
        if len(text) < 200:
            raise ValueError("job page layout not recognized")
        job.description = strip_html(text.removeprefix("!*!"))
    tail = values[(start or 0) + 1:]
    period = next((i for i, v in enumerate(tail) if i >= 2 and v.lower() in ANNUALIZE), None)     # "Yearly", "Hourly", "Monthly"...
    if period and all(re.fullmatch(r"[\d,]+\.\d{2}", tail[i]) for i in (period - 2, period - 1)):   # the low and high pay sit just before it
        job.pay_min, job.pay_max = (int(float(tail[i].replace(",", "")) * ANNUALIZE[tail[period].lower()]) for i in (period - 2, period - 1))
    when = next((v for v in tail if re.match(r"[A-Z][a-z]{2} \d{1,2}, \d{4}", v)), "")
    try:
        job.posted = datetime.strptime(when[:12].rstrip(","), "%b %d, %Y").date().isoformat() if when else job.posted
    except ValueError:
        pass


# ----------------------------------------------------------------------------- Cornerstone (Utah, Kentucky)
CSOD_API = "https://us.api.csod.com/rec-job-search/external/jobs"
CSOD_DELAY = 10   # the careersite's robots.txt asks for a 10 second crawl delay
# source -> (site, career site number, corporation, id prefix, company, state letters)
CSOD_SITES = {
    "utah": ("https://utdgohcm.csod.com", 4, "utdgohcm", "ut", "State of Utah", "UT"),
    "kentucky": ("https://kypersonnelcabinet.csod.com", 49, "kypersonnelcabinet", "ky", "State of Kentucky", "KY"),
}


def parse_csod_results(data: dict, source: str) -> list[Job]:
    """One page of a Cornerstone career site's job search: each requisition carries its dates, places and (short or full) text."""
    base, site, corp, prefix, company, letters = CSOD_SITES[source]
    jobs = []
    for r in ((data or {}).get("data") or {}).get("requisitions") or []:
        rid, title = str(r.get("requisitionId") or ""), (r.get("displayJobTitle") or "").strip()
        if not rid or not title:
            continue
        place = next((loc for loc in r.get("locations") or [] if loc.get("city")), {})
        try:
            posted = datetime.strptime(r.get("postingEffectiveDate") or "", "%m/%d/%Y").date().isoformat()
        except ValueError:
            posted = None
        text = strip_html(r.get("externalDescription") or "")
        lo, hi = extract_pay(text)
        jobs.append(Job(
            id=f"{prefix}:{rid}", source=source, company=company, title=title,
            location=f"{place['city'].strip().title()}, {place.get('state') or letters}" if place else letters,
            url=f"{base}/ux/ats/careersite/{site}/home/requisition/{rid}?c={corp}", posted=posted, description=text, pay_min=lo, pay_max=hi,
            detail={"deadline": r.get("postingExpirationDate")},
        ))
    return jobs


def fetch_csod(source: str, pages: int = 10, delay: float = CSOD_DELAY) -> list[Job]:
    """The career site's own page hands every visitor an anonymous token, which its own job search then uses. Read the token
    from the page, then page through the search (100 at a time), waiting as the site's robots.txt asks."""
    base, site, corp, *_ = CSOD_SITES[source]
    home = requests.get(f"{base}/ux/ats/careersite/{site}/home?c={corp}", headers=sources.UA, timeout=sources.TIMEOUT)
    home.raise_for_status()
    token = re.search(r'"token":"([^"]+)"', home.text)
    if not token:
        raise ValueError("no token on the career site page")
    seen: dict[str, Job] = {}
    for page in range(1, pages + 1):
        time.sleep(max(delay, CSOD_DELAY) if delay else 0)
        r = requests.post(CSOD_API, headers={**sources.UA, "Authorization": f"Bearer {token.group(1)}", "Content-Type": "application/json"}, timeout=sources.TIMEOUT,
                          data=json.dumps({"careerSiteId": site, "careerSitePageId": site, "pageNumber": page, "pageSize": 100, "cultureId": 1,
                                           "searchText": "", "cultureName": "en-US", "states": [], "countryCodes": [], "cities": [], "placeID": "",
                                           "radius": None, "postingsWithinDays": None, "customFieldCheckboxKeys": [], "customFieldDropdowns": [],
                                           "customFieldRadios": []}))
        r.raise_for_status()
        data = r.json()
        got, before = parse_csod_results(data, source), len(seen)
        for j in got:
            seen.setdefault(j.id, j)
        if not got or len(seen) == before or len(seen) >= int(((data.get("data") or {}).get("totalCount")) or 0):
            break                                  # the last page, or a page that added nothing new
    return list(seen.values())


def fetch_utah(search_terms=None, pages: int = 10, delay: float = 0.4) -> list[Job]:
    return fetch_csod("utah", pages, delay)


def fetch_kentucky(search_terms=None, pages: int = 10, delay: float = 0.4) -> list[Job]:
    return fetch_csod("kentucky", pages, delay)


# ----------------------------------------------------------------------------- South Dakota (HireClick)
SD_BOARD = "https://statesdgovt.hireclick.com"
SD_ORG = "16343DEB-35A1-4A0B-9922-A7FC866656E9"   # the state's number on the board, from its page
SD_DELAY = 10   # robots.txt: Crawl-delay 10


def parse_south_dakota_results(data) -> list[Job]:
    """The board's own JSON (double-encoded: a JSON string holding JSON). Each job's short text starts with its job id and runs
    through agency, location, salary and closing date; that is all the board's list gives, and its job pages are 2 MB each."""
    for _ in range(2):      # the board's answer may be a JSON string holding JSON, and `requests` unwraps only the outer one
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except ValueError:
                return []
    if not isinstance(data, dict):
        return []
    jobs = []
    for r in data.get("JobList") or []:
        url, title = r.get("JobURL") or "", (r.get("JobTitle") or "").strip()
        num = re.search(r"/view/(\d+)", url)
        if not num or not title:
            continue
        blurb = re.sub(r"\s+", " ", strip_html(r.get("JobDescriptionShort") or ""))
        agency = re.search(r"Agency:\s*(.+?)\s*Location:", blurb)
        salary = re.search(r"Salary:\s*(\$[^A-Za-z]{0,40})", blurb)
        lo, hi = _jobaps_pay(salary.group(1)) if salary else (None, None)
        city = (r.get("JobCity") or "").strip()
        jobs.append(Job(
            id=f"sd:{num.group(1)}", source="south_dakota", company=f"South Dakota {agency.group(1).strip()}" if agency else "State of South Dakota",
            title=title, location=f"{city}, SD" if city and city.lower() != "statewide" else "South Dakota", url=url, description=blurb,
            pay_min=lo, pay_max=hi,
        ))
    return jobs


def fetch_south_dakota(search_terms=None, pages: int = 1, delay: float = 0.4) -> list[Job]:
    """One request: the board page's own list of every job."""
    r = requests.get(f"{SD_BOARD}/api/Controllers/JobBoard/GetSettingsAndJobs", params={"orgGUID": SD_ORG}, headers=sources.UA, timeout=sources.TIMEOUT)
    r.raise_for_status()
    return parse_south_dakota_results(r.json())


# ----------------------------------------------------------------------------- California
CA_SEARCH = "https://calcareers.ca.gov/CalHRPublic/Search/JobSearchResults.aspx"


def _hidden_fields(page: str) -> dict:
    """Every hidden input of an ASP.NET page: what the next postback must send back (view state and the like)."""
    out = {}
    for tag in re.findall(r"""<input[^>]*type=['"]hidden['"][^>]*>""", page):
        name, value = re.search(r"""name=['"]([^'"]*)['"]""", tag), re.search(r"""value=['"]([^'"]*)['"]""", tag)
        if name:
            out[name.group(1)] = html.unescape(value.group(1)) if value else ""
    return out


def _ca_pay(text: str) -> tuple[int | None, int | None]:
    """'$4590.00 - $5803.00' is a monthly figure; amounts under $200 are hourly."""
    m = re.search(r"\$\s*([\d,]+(?:\.\d+)?)\s*-\s*\$\s*([\d,]+(?:\.\d+)?)", text or "")
    if not m:
        return None, None
    lo, hi = (float(x.replace(",", "")) for x in m.groups())
    factor = 2080 if lo < 200 else 12
    return int(lo * factor), int(hi * factor)


def parse_california_results(page: str) -> list[Job]:
    """One page of CalCareers search results: a card per job with labelled fields (title, salary, department, dates)."""
    jobs = []
    for card in page.split('id="cphMainContent_rptResults_pnlCardContainer_')[1:]:
        link = re.search(r'hlViewJobPosting_\d+"[^>]*href="([^"]*JobControlId=(\d+))"[^>]*>(.*?)</a>', card, re.S)
        if not link or not _flat(link.group(3)):
            continue
        fields = {_flat(k).rstrip(":"): _flat(v) for k, v in re.findall(r'job-label">(.*?)</div>\s*<div[^>]*job-details[^>]*>(.*?)</div>', card, re.S)}
        dept = fields.get("Department", "")
        lo, hi = _ca_pay(fields.get("Salary Range", ""))
        dates = {}
        for key in ("Publish Date", "Filing Deadline"):
            try:
                dates[key] = datetime.strptime(fields.get(key, ""), "%m/%d/%Y").date().isoformat()
            except ValueError:
                dates[key] = None
        jobs.append(Job(
            id=f"ca:{link.group(2)}", source="california", company=dept if "california" in dept.lower() else f"California {dept}".strip(),
            title=_flat(link.group(3)), location="California", url=html.unescape(link.group(1)), posted=dates["Publish Date"], pay_min=lo, pay_max=hi,
            detail={"deadline": dates["Filing Deadline"], "work_type": fields.get("Work Type/Schedule", ""), "telework": fields.get("Telework", "")},
        ))
    return jobs


def fetch_california(search_terms=None, pages: int = 40, delay: float = 1.0) -> list[Job]:
    """CalCareers is an ASP.NET form: the first visit gives the form, 'Update Results' with nothing typed lists every job, then the row count
    is set to 100 and the pager's own buttons are posted back one by one (each answer is about 650 KB; about 28 pages)."""
    s = requests.Session()
    s.headers.update(sources.UA)
    page = s.get(CA_SEARCH, timeout=sources.TIMEOUT).text
    time.sleep(delay)
    form = {**_hidden_fields(page), "ctl00$cphMainContent$btnUpdateResults": "Update Results", "ctl00$cphMainContent$txtKeyword": ""}
    page = s.post(CA_SEARCH, data=form, timeout=60).text
    time.sleep(delay)
    form = {**_hidden_fields(page), "__EVENTTARGET": "ctl00$cphMainContent$ddlRowCount", "ctl00$cphMainContent$ddlRowCount": "100",
            "ctl00$cphMainContent$ddlSortBy": "PublishDate DESC"}
    page = s.post(CA_SEARCH, data=form, timeout=60).text
    seen: dict[str, Job] = {}
    for n in range(pages):
        got = parse_california_results(page)
        before = len(seen)
        for j in got:
            seen.setdefault(j.id, j)
        if len(got) < 100 or len(seen) == before:
            break                                  # a short page is the last; nothing new means the pager is not moving
        time.sleep(delay)
        form = {**_hidden_fields(page), "__EVENTTARGET": f"ctl00$cphMainContent$ucRepeaterPager$rptPager$ctl{n + 1:02d}$btnPagerItem",
                "ctl00$cphMainContent$ddlRowCount": "100", "ctl00$cphMainContent$ddlSortBy": "PublishDate DESC"}
        page = s.post(CA_SEARCH, data=form, timeout=60).text
    return list(seen.values())


def fill_california_detail(job: Job) -> None:
    """The job page: position details (work location, salary, schedule), department information and the duties, as plain text."""
    r = requests.get(job.url, headers=sources.UA, timeout=sources.TIMEOUT)
    r.raise_for_status()
    body = re.sub(r"<script.*?</script>|<style.*?</style>|<!--.*?-->", "", r.text, flags=re.S)
    start = body.find("Position Details")
    if start < 0:
        raise ValueError("job page layout not recognized")
    text = strip_html(body[start:])
    end = min((i for i in (text.find(m) for m in ("Application Instructions", "Contact Information", "Back to Top")) if i > 200), default=len(text))
    job.description = text[:end].strip()[:12000]
    where = re.search(r"Work Location:\**\s*([^\n*]+)", job.description)
    if where and where.group(1).strip():
        job.location = f"{where.group(1).strip()}, CA" if "," not in where.group(1) else where.group(1).strip()


# ----------------------------------------------------------------------------- New York
NY_BASE = "https://statejobsny.com/public"
NY_NAMES = re.compile(r"^(.+), (Office of the|Department of|Division of|Commission on|State)$")


def _ny_agency(name: str) -> str:
    """'Environmental Conservation, Department of' -> 'New York Department of Environmental Conservation'."""
    m = NY_NAMES.match(name.strip())
    name = f"{m.group(2)} {m.group(1)}" if m else name.strip()
    return name if "New York" in name else f"New York {name}"


def _ny_date(text: str) -> str | None:
    try:
        return datetime.strptime(html.unescape(text).strip(), "%m/%d/%y").date().isoformat()
    except ValueError:
        return None


def parse_ny_results(page: str) -> list[Job]:
    """statejobsny.com's table of every open posting: item number, title, grade, posted, deadline, agency, county."""
    jobs = []
    for row in re.findall(r'<tr class="(?:odd|even)">(.*?)</tr>', page, re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        link = re.search(r'href="vacancyDetailsView\.cfm\?id=(\d+)"', cells[1]) if len(cells) >= 7 else None
        title = _flat(cells[1]) if link else ""
        if not link or not title:
            continue
        county = _flat(cells[6])
        jobs.append(Job(
            id=f"ny:{link.group(1)}", source="new_york", company=_ny_agency(_flat(cells[5])), title=title,
            location=f"{county}, NY" if county else "New York", url=f"{NY_BASE}/vacancyDetailsView.cfm?id={link.group(1)}",
            posted=_ny_date(cells[3]), detail={"grade": _flat(cells[2]), "deadline": _ny_date(cells[4])},
        ))
    return jobs


def fetch_new_york(search_terms=None, pages: int = 1, delay: float = 0.4) -> list[Job]:
    """One page lists every open posting (about 1,700), so there is nothing to search or page through here. The title
    filters later keep only the ones that fit someone."""
    r = requests.get(f"{NY_BASE}/vacancyTable.cfm", headers=sources.UA, timeout=sources.TIMEOUT)
    r.raise_for_status()
    return parse_ny_results(r.text)


def fill_new_york_detail(job: Job) -> None:
    """The posting's page: duties, minimum qualifications and the salary range."""
    r = requests.get(job.url, headers=sources.UA, timeout=sources.TIMEOUT)
    r.raise_for_status()
    fields = {_flat(label): value for label, value in
              re.findall(r'<span class="leftCol">(.*?)</span><span class="rightCol">(.*?)</span>\s*</p>', r.text, re.S)}
    duties = strip_html(fields.get("Duties Description", ""))
    if not duties:
        raise ValueError("job page layout not recognized")
    quals = strip_html(fields.get("Minimum Qualifications", ""))
    job.description = duties + (f"\n\n**Minimum qualifications**\n\n{quals}" if quals else "")
    pay = re.search(r"\$\s*([\d,]+(?:\.\d+)?)\s*to\s*\$?\s*([\d,]+(?:\.\d+)?)\s*([A-Za-z-]+)?", _flat(fields.get("Salary Range", "")))
    factor = ANNUALIZE.get((pay.group(3) or "annual").lower()) if pay else None
    if pay and factor:
        job.pay_min, job.pay_max = (int(float(x.replace(",", "")) * factor) for x in (pay.group(1), pay.group(2)))


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


def fill_successfactors_detail(job: Job) -> None:
    """Read one posting's page for what the search list lacks: the description, the salary range and the exact date.
    The job page looks the same on every SuccessFactors career site."""
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


sources.DETAIL_FILLERS["texas_hhs"] = fill_successfactors_detail
sources.DETAIL_FILLERS["florida"] = fill_successfactors_detail
sources.DETAIL_FILLERS["vermont"] = fill_successfactors_detail
for _name in ("indiana", "arkansas", "illinois"):
    sources.DETAIL_FILLERS[_name] = fill_successfactors_detail
sources.DETAIL_FILLERS["new_york"] = fill_new_york_detail
sources.DETAIL_FILLERS["maryland"] = fill_jobaps_detail
sources.DETAIL_FILLERS["connecticut"] = fill_jobaps_detail
sources.DETAIL_FILLERS["delaware"] = fill_jobaps_detail
sources.DETAIL_FILLERS["arizona"] = fill_arizona_detail
sources.DETAIL_FILLERS["missouri"] = fill_missouri_detail
for _name in ("wisconsin", "kansas", "minnesota"):
    sources.DETAIL_FILLERS[_name] = fill_peoplesoft_detail
sources.DETAIL_FILLERS["tennessee"] = fill_tennessee_detail
sources.DETAIL_FILLERS["massachusetts"] = fill_taleo_detail
sources.DETAIL_FILLERS["montana"] = fill_taleo_detail
sources.DETAIL_FILLERS["california"] = fill_california_detail
