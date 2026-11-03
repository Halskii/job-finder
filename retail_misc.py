"""Retailers that each have a career site of their own, so each is a small reader of its own. All of them are listed in READERS and
picked up by retail_sources.py.

- ahold: Ahold Delhaize USA's brands (Food Lion, Giant Food, Stop & Shop, Hannaford, The GIANT Company) share one career platform,
  <banner>.careerswithus.com. Its robots.txt allows everything and the site's llms.txt describes a public JSON job API
  (/api/mcp/jobs), which is what is read. The list gives only the start of each text, so the full posting is read later.
- dillards: careers.dillards.com has no keyword search to call; the site's own corporate views (Information Technology,
  Merchandising, Call Center and the general list) are four JSON lists, a few jobs each, with the whole text in them.
  Dillard's has no robots.txt. Its edge refuses the short user agent of sources.UA (HTTP 403) and accepts the longer one that
  names the repository, so the reader fails with a clear message until UA is the long form.
- victoriassecret: the career site is WordPress; the jobs come from a Google Cloud Talent JSON API (jobsapi-google.m-cloud.io)
  that the site's own search page calls. The list holds the full text.
- hobbylobby, harborfreight: both hire through IBM BrassRing (sjobs.brassring.com, whose robots.txt does not exist). The
  site's own search call (ProcessSortAndShowMoreJobs) answers an anonymous POST with 50 jobs a page. Hobby Lobby's answer holds
  the whole text; Harbor Freight's holds a short blurb, so its job page (which carries schema.org JobPosting data) is read later.
- burlington: burlingtonstores.jobs is a DirectEmployers microsite whose page is an empty shell; its public config names the
  search service behind it (prod-search-api.jobsyn.org, asked with the site's own `x-origin` header). That service has no
  robots.txt and holds the whole text of each job, ten to a page.

Dropped after checking, so a reader is not written for them: Amazon and Whole Foods corporate (the terms linked from amazon.jobs
are Amazon's Conditions of Use, which bar robots and data mining), Best Buy (no search call that can be made without the portal's
widget state, and about 300 KB a posting), Discount Tire (a Crawl-delay of 10 and "Load More" runs on a script).
"""
from __future__ import annotations

import html
import json
import re
import time
from datetime import datetime
from urllib.parse import quote, urlencode, urlparse

import sources
from sources import Job, extract_pay, strip_html

# Every reader stops after this many requests in one run, however many search words or pages there are (a loop over 50 words and
# several pages would otherwise be hundreds). The words are tried in the order given, so the first ones matter most.
AHOLD_MAX_REQUESTS = 400        # five banners x about 50 words, plus a second page now and then
VICTORIASSECRET_MAX_REQUESTS = 150
BRASSRING_MAX_REQUESTS = 150    # per retailer; Harbor Freight reads two sites, so a word costs two requests
BURLINGTON_MAX_REQUESTS = 200   # ten jobs a page, so this is at most 2,000 postings


class _Pace:
    """Counts a reader's requests against its cap and waits `delay` seconds between them."""

    def __init__(self, limit: int, delay: float):
        self.limit, self.delay, self.used = limit, delay, 0

    def more(self) -> bool:
        """True when another request may be made (after waiting); False once the cap is reached."""
        if self.used >= self.limit:
            return False
        if self.used:
            time.sleep(self.delay)
        self.used += 1
        return True


def _answer(method: str, url: str, **kw):
    r = getattr(sources._session(), method)(url, timeout=sources.TIMEOUT, **kw)
    r.raise_for_status()
    return r


def _json(method: str, url: str, **kw) -> dict:
    """The JSON object an address answers. A page that is not JSON (a block page, a changed site) is an error that says so."""
    r = _answer(method, url, **kw)
    try:
        data = r.json()
    except ValueError:
        raise ValueError(f"{urlparse(url).netloc}: the answer is not JSON (the site changed, or turned the request away)") from None
    if not isinstance(data, dict):
        raise ValueError(f"{urlparse(url).netloc}: unexpected answer")
    return data


def _iso(text) -> str | None:
    """'2026-10-27T05:00:00Z', '29-Oct-2026' or '10/28/2026' as 2026-10-27, else None."""
    text = str(text or "").strip()
    if re.match(r"\d{4}-\d{2}-\d{2}", text):
        return text[:10]
    for fmt in ("%d-%b-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def _money(n: float) -> str:
    return f"${n:,.2f}" if n != int(n) else f"${int(n):,}"


def _pay_line(low, high, hourly: bool) -> str:
    """'Pay: $16.60 - $24.90 per hour' for a rate a posting keeps outside its text, written so sources.fill_pay finds it."""
    nums = [float(n) for n in (low, high) if n not in (None, "") and float(n) > 0]
    return f"Pay: {' - '.join(dict.fromkeys(_money(n) for n in nums))} per {'hour' if hourly else 'year'}" if nums else ""


def _set_pay(job: Job, low, high, hourly: bool) -> None:
    """A yearly range goes straight onto the job; an hourly one is added to the text for fill_pay (which runs later)."""
    try:
        line = _pay_line(low, high, hourly)
    except (TypeError, ValueError):
        return
    if not line:
        return
    if hourly:
        if line not in job.description:
            job.description += ("\n\n" if job.description else "") + line
    else:
        job.pay_min, job.pay_max = extract_pay(line)


def _place(city: str, state: str) -> str:
    return ", ".join(x for x in (city.strip(), state.strip()) if x)


def _add(seen: dict[str, Job], jobs: list[Job]) -> None:
    """Keep the first copy of a posting that several search words return."""
    for j in jobs:
        seen.setdefault(j.id, j)


# ----------------------------------------------------------------------------- Ahold Delhaize USA
# banner site -> company shown to people. Each is its own address with its own jobs.
AHOLD_BANNERS = {"foodlion": "Food Lion", "giantfood": "Giant Food", "stopandshop": "Stop & Shop", "hannaford": "Hannaford",
                 "thegiantcompany": "The GIANT Company"}
AHOLD_PAGE = 100


def _ahold_api(banner: str) -> str:
    return f"https://{banner}.careerswithus.com/api/mcp/jobs"


def parse_ahold_results(data: dict, banner: str) -> list[Job]:
    """One page of the search. The list gives only the first 300 characters of a text, so a job is stored without text and with
    `detail` set to the call that returns the whole posting."""
    if not isinstance(data.get("results"), list):
        raise ValueError("careerswithus: the search answer has no results list")
    jobs = []
    for it in data["results"]:
        rid, title = str(it.get("requisitionId") or "").strip(), str(it.get("title") or "").strip()
        if not rid or not title:
            continue
        jobs.append(Job(
            id=f"ahold:{rid}", source="ahold", company=AHOLD_BANNERS[banner], title=title, location=re.sub(r",\s*USA$", "", str(it.get("location") or "")),
            url=f"https://{banner}.careerswithus.com/job/{quote(rid)}",       # the site sends this on to the page with the title in its address
            posted=_iso(it.get("datePosted")), detail={"api": f"{_ahold_api(banner)}?tool=get_job&jobId={quote(rid)}"},
        ))
    return jobs


def fetch_ahold(search_terms: list[str], pages: int = 8, delay: float = 0.4) -> list[Job]:
    """Search each banner for each word (the site's search covers title and description), a hundred jobs a page. Words come first
    and banners second, so a run cut short by the cap has still asked every banner for the first words."""
    seen: dict[str, Job] = {}
    pace = _Pace(AHOLD_MAX_REQUESTS, delay)
    for term in search_terms:
        for banner in AHOLD_BANNERS:
            for page in range(1, pages + 1):
                if not pace.more():
                    return list(seen.values())
                data = _json("get", f"{_ahold_api(banner)}?tool=search_jobs&search={quote(term)}&page={page}&pageSize={AHOLD_PAGE}")
                _add(seen, parse_ahold_results(data, banner))
                held = len(data["results"])
                if held < AHOLD_PAGE or page * AHOLD_PAGE >= int(data.get("totalCount") or 0):
                    break
    return list(seen.values())


def fill_ahold_detail(job: Job) -> None:
    """The whole posting from the site's get_job call: the text (HTML), the date, the place and the pay (hourly or yearly)."""
    api = str(job.detail.get("api") or "")
    if not (urlparse(api).hostname or "").endswith(".careerswithus.com"):
        raise ValueError("not a careerswithus posting address")
    posting = _json("get", api).get("result")
    text = strip_html(str((posting or {}).get("description") or ""))
    if not text:
        raise ValueError("careerswithus: job answer has no text")
    job.description = text
    job.title = str(posting.get("title") or "").strip() or job.title
    job.posted = _iso(posting.get("datePosted")) or job.posted
    job.location = _place(str(posting.get("city") or ""), str(posting.get("state") or "")) or job.location
    low, high, rate = posting.get("salaryMin"), posting.get("salaryMax"), str(posting.get("salRateType") or "").lower()
    _set_pay(job, low, high, "hour" in rate)
    if job.pay_min is None and job.pay_max is None and "hour" not in rate:
        job.pay_min, job.pay_max = extract_pay(text)


sources.DETAIL_FILLERS["ahold"] = fill_ahold_detail


# ----------------------------------------------------------------------------- Dillard's
DILLARDS_LIST = "https://careers.dillards.com/Careers/api/jobs/list"
DILLARDS_VIEWS = ("All", "Information Technology", "Merchandising", "Call Center")      # the filters the site's own views ask for
DILLARDS_SECTIONS = (("description", ""), ("responsibilities", "Responsibilities"), ("requiredSkills", "Required skills"),
                     ("desiredSkills", "Desired skills"))


def parse_dillards(data: dict) -> list[Job]:
    """One view's jobs. The whole text is in the list (description, responsibilities, required and desired skills)."""
    if not isinstance(data.get("data"), list):
        raise ValueError("Dillard's: the list answer has no jobs list")
    jobs = []
    for it in data["data"]:
        jid, title = str(it.get("id") or "").strip(), str(it.get("title") or "").strip()
        if not jid or not title:
            continue
        parts = []
        for key, heading in DILLARDS_SECTIONS:
            body = strip_html(str(it.get(key) or "").replace("\r\n", "\n").replace("\r", "\n"))
            if body:
                parts.append(f"## {heading}\n{body}" if heading else body)
        where = it.get("locationInfo") or {}
        link = str(it.get("greenhouseUrl") or "")     # the posting the site itself sends a person to, when there is one
        text = "\n\n".join(parts)
        job = Job(id=f"dillards:{jid}", source="dillards", company="Dillard's", title=title,
                  location=_place(str(where.get("city") or ""), str(where.get("state") or "")),
                  url=link if link.startswith("https://") else "https://careers.dillards.com/Careers/",
                  posted=_iso(it.get("postOn")), description=text)
        job.pay_min, job.pay_max = extract_pay(text)
        jobs.append(job)
    return jobs


def fetch_dillards(search_terms: list[str], pages: int = 1, delay: float = 0.4) -> list[Job]:
    """The corporate lists are a dozen jobs in all and the site has no word search, so every job is returned and the caller's title
    filter picks. (Store jobs come from a different call, a POST per store type, that is not read.)"""
    seen: dict[str, Job] = {}
    pace = _Pace(len(DILLARDS_VIEWS), delay)
    for view in DILLARDS_VIEWS:
        pace.more()
        r = sources._session().get(f"{DILLARDS_LIST}?{urlencode({'filter': view})}", timeout=sources.TIMEOUT)
        if r.status_code == 403:
            raise RuntimeError("Dillard's turned the request away (HTTP 403): its edge refuses the short user agent in sources.UA")
        r.raise_for_status()
        try:
            data = r.json()
        except ValueError:
            raise ValueError("careers.dillards.com: the answer is not JSON (the site changed, or turned the request away)") from None
        _add(seen, parse_dillards(data))
    return list(seen.values())


# ----------------------------------------------------------------------------- Victoria's Secret
VS_API = "https://jobsapi-google.m-cloud.io/api/job/search"
VS_COMPANY = "companies/382ab4db-03e8-40cc-b413-51539aca9954"     # Victoria's Secret & Co, from the career site's own settings
VS_PAGE = 100
VS_PAY = re.compile(r"Minimum Salary:</strong>\s*\$([\d,.]+).*?Maximum Salary:</strong>\s*\$([\d,.]+)", re.S)


def parse_vs_results(data: dict) -> list[Job]:
    """One page of the search. The text is in the list; the pay is in the text as 'Minimum Salary: $x / Maximum Salary: $y' (a yearly
    figure for salaried jobs, an hourly rate for store associates)."""
    if not isinstance(data.get("searchResults"), list):
        raise ValueError("Victoria's Secret: the search answer has no results list")
    jobs = []
    for hit in data["searchResults"]:
        it = (hit or {}).get("job") or {}
        jid, title, url = str(it.get("id") or "").strip(), str(it.get("title") or "").strip(), str(it.get("url") or "")
        if not jid or not title or not url.startswith("https://careers.victoriassecret.com/"):
            continue
        raw = str(it.get("description") or "")
        place = _place(str(it.get("primary_city") or ""), str(it.get("primary_state") or ""))
        if place and str(it.get("location_type") or "").lower() == "remote":
            place += " (Remote)"
        job = Job(id=f"victoriassecret:{jid}", source="victoriassecret", company=str(it.get("brand") or "Victoria's Secret"), title=title,
                  location=place, url=url, posted=_iso(it.get("open_date")), description=strip_html(raw))
        pay = VS_PAY.search(raw)
        if pay:
            low, high = (float(g.replace(",", "")) for g in pay.groups())
            _set_pay(job, low, high, hourly=high < 500)
        if job.pay_min is None:
            job.pay_min, job.pay_max = extract_pay(job.description)
        jobs.append(job)
    return jobs


def fetch_victoriassecret(search_terms: list[str], pages: int = 8, delay: float = 0.4) -> list[Job]:
    """Search for each word, a hundred jobs a page (the next page starts after the ones already read)."""
    seen: dict[str, Job] = {}
    pace = _Pace(VICTORIASSECRET_MAX_REQUESTS, delay)
    for term in search_terms:
        offset = 0
        for _ in range(pages):
            if not pace.more():
                return list(seen.values())
            query = urlencode({"pageSize": VS_PAGE, "offset": offset, "companyName": VS_COMPANY, "query": term})
            data = _json("get", f"{VS_API}?{query}")
            _add(seen, parse_vs_results(data))
            held = len(data["searchResults"])
            offset += held
            if not held or offset >= int(data.get("totalHits") or 0):
                break
    return list(seen.values())


# ----------------------------------------------------------------------------- IBM BrassRing (Hobby Lobby, Harbor Freight)
BRASSRING = "https://sjobs.brassring.com"
# key -> (company, partner id, site ids, whether the list holds the whole text). Harbor Freight keeps store jobs and
# distribution-center and corporate jobs on two sites of one partner.
BRASSRING_SITES = {"hobbylobby": ("Hobby Lobby", "25879", ("5295",), True),
                   "harborfreight": ("Harbor Freight Tools", "26281", ("6657", "6660"), False)}


def brassring_body(partner: str, site: str, term: str, page: int) -> dict:
    """The search the career page itself sends. `Limit` is left out: the site answers fifty a page whatever it says."""
    return {"partnerId": partner, "siteId": site, "keyword": term, "location": "", "keywordCustomSolrFields": "FORMTEXT3,FORMTEXT21",
            "locationCustomSolrFields": "FORMTEXT2,FORMTEXT3", "linkId": "", "Latitude": 0, "Longitude": 0, "facetfilterfields": {"Facet": []},
            "powersearchoptions": {"PowerSearchOption": []}, "SortType": "LastUpdated", "PageNumber": page, "ExcludeFollowingFacets": ""}


def _brassring_places(city: str, state: str) -> str:
    """'Oklahoma City, Oklahoma, United States' (Hobby Lobby spells the state out), 'Auburndale, FL' (Harbor Freight uses the code, and
    sometimes writes the whole 'Piedmont, SC, United States' in the city field)."""
    city, state = html.unescape(city).strip(), state.strip()
    if "," in city or not city:
        return city
    city = city.title() if city.isupper() else city
    return _place(city, state) if len(state) <= 2 else _place(city, state) + ", United States"


def parse_brassring(data: dict, key: str, site: str) -> list[Job]:
    """One page of BrassRing's answer: each job is a list of named values (reqid, jobtitle, lastupdated, jobdescription, ...)."""
    company, partner, _, full = BRASSRING_SITES[key]
    if not isinstance((data.get("Jobs") or {}).get("Job"), list):
        raise ValueError("BrassRing: the search answer has no jobs list")
    jobs = []
    for it in data["Jobs"]["Job"]:
        q = {str(x.get("QuestionName") or "").lower(): x.get("Value") for x in it.get("Questions") or [] if isinstance(x, dict)}
        rid, title = str(q.get("reqid") or "").strip(), html.unescape(str(q.get("jobtitle") or "")).strip()
        if not rid or not title:
            continue
        link = str(it.get("Link") or "")
        url = link if link.startswith(BRASSRING + "/") else f"{BRASSRING}/TGnewUI/Search/home/HomeWithPreLoad?partnerid={partner}&siteid={site}&PageType=JobDetails&jobid={rid}"
        place = _brassring_places(str(q.get("formtext11") or q.get("formtext5") or ""), str(q.get("formtext1") or ""))
        if place and re.search(r"\bremote\b", title, re.I):
            place += " (Remote)"
        job = Job(id=f"{key}:{rid}", source=key, company=company, title=title, location=place, url=url, posted=_iso(q.get("lastupdated")))
        if full:
            job.description = strip_html(str(q.get("jobdescription") or ""))
            job.pay_min, job.pay_max = extract_pay(job.description)
        if not job.description:
            job.detail = {"api": url}
        jobs.append(job)
    return jobs


def fetch_brassring(key: str, search_terms: list[str], pages: int = 8, delay: float = 0.4) -> list[Job]:
    """POST the site's own search for each word on each of the retailer's sites and read its pages (fifty jobs each; the count
    comes with every answer)."""
    _, partner, sites, _ = BRASSRING_SITES[key]
    seen: dict[str, Job] = {}
    pace = _Pace(BRASSRING_MAX_REQUESTS, delay)
    for term in search_terms:
        for site in sites:
            offset = 0
            for page in range(1, pages + 1):
                if not pace.more():
                    return list(seen.values())
                data = _json("post", f"{BRASSRING}/TgNewUI/Search/Ajax/ProcessSortAndShowMoreJobs", json=brassring_body(partner, site, term, page))
                _add(seen, parse_brassring(data, key, site))
                held = len(data["Jobs"]["Job"])
                offset += held
                if not held or offset >= int(data.get("JobsCount") or 0):
                    break
    return list(seen.values())


def fetch_hobbylobby(search_terms: list[str], pages: int = 8, delay: float = 0.4) -> list[Job]:
    return fetch_brassring("hobbylobby", search_terms, pages, delay)


def fetch_harborfreight(search_terms: list[str], pages: int = 8, delay: float = 0.4) -> list[Job]:
    return fetch_brassring("harborfreight", search_terms, pages, delay)


def _ld_posting(page: str) -> dict:
    """The schema.org JobPosting object among a page's <script type="application/ld+json"> blocks, or {}."""
    for block in re.findall(r"<script[^>]*type=.application/ld\+json.[^>]*>(.*?)</script>", page, re.S):
        try:
            data = json.loads(block, strict=False)
        except ValueError:
            continue
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return item
    return {}


def fill_harborfreight_detail(job: Job) -> None:
    """The job page's JobPosting data: the real title, the text, the posting date and the place."""
    if not job.url.startswith(BRASSRING + "/"):
        raise ValueError("not a BrassRing job address")
    posting = _ld_posting(_answer("get", job.url).text)
    text = strip_html(str(posting.get("description") or ""))
    if not text:
        raise ValueError("BrassRing job page layout not recognized")
    job.description = text
    job.title = html.unescape(str(posting.get("title") or "")).strip() or job.title
    job.posted = _iso(posting.get("datePosted")) or job.posted
    where = posting.get("jobLocation")
    address = ((where[0] if isinstance(where, list) and where else where) or {}).get("address") or {}
    place = _place(str(address.get("addressLocality") or ""), str(address.get("addressRegion") or ""))
    if place and re.search(r"\bremote\b", job.title, re.I):
        place += " (Remote)"
    job.location = place or job.location
    job.pay_min, job.pay_max = extract_pay(text)


sources.DETAIL_FILLERS["harborfreight"] = fill_harborfreight_detail


# ----------------------------------------------------------------------------- Burlington
BURLINGTON_API = "https://prod-search-api.jobsyn.org/api/v1/solr/search"
BURLINGTON_ORIGIN = {"x-origin": "burlingtonstores.jobs"}     # the site's own setting; it names which site's jobs are searched


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def parse_burlington(data: dict) -> list[Job]:
    """One page (ten jobs) of the search. The whole text is in the list. A person's page is
    burlingtonstores.jobs/<city-st>/<title>/<id>/job/, the form the site's own sitemap lists."""
    if not isinstance(data.get("jobs"), list):
        raise ValueError("Burlington: the search answer has no jobs list")
    jobs = []
    for it in data["jobs"]:
        guid, title = str(it.get("guid") or "").strip(), str(it.get("title_exact") or "").strip()
        if not guid or not title:
            continue
        city, state = str(it.get("city_exact") or ""), str(it.get("state_short") or "")
        place = str(it.get("location_exact") or "") or _place(city, state)
        if place and re.search(r'"Remote Type"\s*:\s*"Remote"', str(it.get("other") or "")):
            place += " (Remote)"
        slug = _slug(it.get("title_slug") or title)
        where = f"{_slug(city)}-{state.lower()}" if city and state else _slug(city or "jobs")
        text = strip_html(str(it.get("description") or ""))
        job = Job(id=f"burlington:{guid}", source="burlington", company="Burlington", title=title, location=place,
                  url=f"https://burlingtonstores.jobs/{where}/{slug}/{guid}/job/", posted=_iso(it.get("date_new") or it.get("date_added")),
                  description=text)
        job.pay_min, job.pay_max = extract_pay(text)
        jobs.append(job)
    return jobs


def fetch_burlington(search_terms: list[str], pages: int = 8, delay: float = 0.4) -> list[Job]:
    seen: dict[str, Job] = {}
    pace = _Pace(BURLINGTON_MAX_REQUESTS, delay)
    for term in search_terms:
        for page in range(1, pages + 1):
            if not pace.more():
                return list(seen.values())
            data = _json("get", f"{BURLINGTON_API}?{urlencode({'q': term, 'page': page})}", headers=BURLINGTON_ORIGIN)
            _add(seen, parse_burlington(data))
            if not (data.get("pagination") or {}).get("has_more_pages"):
                break
    return list(seen.values())


READERS = {
    "ahold": ("Ahold Delhaize USA", fetch_ahold),
    "dillards": ("Dillard's", fetch_dillards),
    "victoriassecret": ("Victoria's Secret", fetch_victoriassecret),
    "hobbylobby": ("Hobby Lobby", fetch_hobbylobby),
    "harborfreight": ("Harbor Freight", fetch_harborfreight),
}
# Built and tested but NOT switched on: nothing on burlingtonstores.jobs (its page or scripts) names prod-search-api.jobsyn.org, so
# it is not confirmed that the site itself calls that service, and the service's own terms were not read. Add
# `"burlington": ("Burlington", fetch_burlington)` to READERS once both are checked.
