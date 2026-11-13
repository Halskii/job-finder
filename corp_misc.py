"""Large employers (not retailers) that run a career site of their own, each a small reader of its own. They are listed in READERS and
picked up by retail_sources.py, so they are searched with the same title words as Workday and switched on and off under `retailers:`
in config.yaml, like the retailers.

- apple: jobs.apple.com has no robots.txt (it answers 404). The career page asks its own JSON search, `POST /api/v1/search`, with the
  token that `GET /api/v1/CSRFToken` hands out first; that is exactly what is read here, twenty jobs a page, United States only. The
  list holds a short summary, so a posting is stored with `detail` set and the web app reads the whole text the first time someone's
  search selects it (`GET /api/v1/jobDetails/<id>`: the description, the qualifications and the pay footer).

- bankofamerica: careers.bankofamerica.com (robots.txt allows everything) has a search call, `/services/jobssearchservlet`, that the
  career page itself makes and that answers JSON. Its `rows` is the position the window ends at, so a page is `start=N&rows=N+50`. The
  list has the title, place, date and page address; the job page's schema.org JobPosting data holds the text and is read on demand.

AMD, State Farm and Garmin are rows in retail_jibe.py (the same iCIMS Jibe search the retailers use), Fidelity is a row in
retail_sitemap.py (its job sitemap plus the structured data on each job page), and Honeywell, Texas Instruments and Oracle are rows in
retail_oracle.py. Not built after checking: FedEx and T-Mobile answer the Jibe search with 403; Verizon's job pages carry no structured
data (and its search answers 500); GM's sitemap lists no job pages; Zoom's lists only marketing pages.

Checked and left out on purpose (their robots.txt or terms turn automated programs away): Microsoft (apply.careers.microsoft.com),
Netflix, Goldman Sachs, Qualcomm and Bloomberg block everything; Uber blocks its API paths; Amazon's terms bar robots; Tesla and
JPMorgan's career host refuse the request.
"""
from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urlencode, urlparse

import sources
from retail_misc import _Pace, _add, _json
from sources import Job, extract_pay, strip_html

APPLE = "https://jobs.apple.com"
APPLE_PAGE = 20                   # jobs the search returns a page (it takes no page size)
APPLE_MAX_REQUESTS = 150          # the most requests one run makes for Apple
APPLE_FORMAT = {"longDate": "MMMM D, YYYY", "mediumDate": "MMM D, YYYY"}


def _apple_headers() -> dict:
    """The headers the career page sends: the anti-forgery token it asks for before it searches."""
    r = sources._session().get(f"{APPLE}/api/v1/CSRFToken", timeout=sources.TIMEOUT)
    r.raise_for_status()
    token = r.headers.get("x-apple-csrf-token")
    if not token:
        raise ValueError("jobs.apple.com: no token came back (the site changed, or turned the request away)")
    return {"x-apple-csrf-token": token, "Accept": "application/json"}


def _apple_places(locations) -> str:
    out = []
    for loc in locations if isinstance(locations, list) else []:
        if not isinstance(loc, dict):
            continue
        place = ", ".join(x for x in (str(loc.get("city") or loc.get("name") or "").strip(), str(loc.get("stateProvince") or "").strip()) if x)
        if place and place not in out:
            out.append(place)
    return "; ".join(out)


def _apple_date(text) -> str | None:
    text = str(text or "")[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
    except ValueError:
        return None


_APPLE_PAY = re.compile(r"\$\s?(\d[\d,]{3,})(?:\.\d+)?\s*(?:and|to|-|–)\s*\$\s?(\d[\d,]{3,})")


def _apple_pay(text: str) -> tuple[int | None, int | None]:
    """Apple writes 'The base pay range for this role is between $119,700 and $225,600', which extract_pay does not read."""
    m = _APPLE_PAY.search(text)
    if m:
        low, high = (int(g.replace(",", "")) for g in m.groups())
        if 0 < low <= high:
            return low, high
    return extract_pay(text)


def parse_apple_results(data: dict) -> list[Job]:
    rows = ((data.get("res") or {}).get("searchResults")) if isinstance(data, dict) else None
    jobs = []
    for it in rows if isinstance(rows, list) else []:
        if not isinstance(it, dict) or not it.get("positionId") or not it.get("postingTitle"):
            continue
        pid, slug = str(it["positionId"]), str(it.get("transformedPostingTitle") or "").strip()
        jobs.append(Job(id=f"apple:{pid}", source="apple", company="Apple", title=str(it["postingTitle"]).strip(),
                        location=_apple_places(it.get("locations")), url=f"{APPLE}/en-us/details/{pid}/{slug}".rstrip("/"),
                        posted=_apple_date(it.get("postDateInGMT")), description=strip_html(str(it.get("jobSummary") or "")),
                        detail={"id": pid}))
    return jobs


def fetch_apple(search_terms: list[str], pages: int = 8, delay: float = 0.4) -> list[Job]:
    """Search Apple's US jobs for each word, twenty a page, until a word's results run out or the request cap is reached."""
    seen: dict[str, Job] = {}
    pace = _Pace(APPLE_MAX_REQUESTS, delay)
    headers = None
    for term in dict.fromkeys(t.strip() for t in search_terms if t and t.strip()):
        for page in range(1, pages + 1):
            if not pace.more():
                return list(seen.values())
            if headers is None:
                headers = _apple_headers()
            body = {"query": term, "filters": {"locations": ["postLocation-USA"]}, "page": page, "locale": "en-us", "sort": "",
                    "format": APPLE_FORMAT}
            data = _json("post", f"{APPLE}/api/v1/search", json=body, headers=headers)
            jobs = parse_apple_results(data)
            _add(seen, jobs)
            total = int(((data.get("res") or {}).get("totalRecords")) or 0)
            if not jobs or page * APPLE_PAGE >= total:
                break
    return list(seen.values())


def fill_apple_detail(job: Job) -> None:
    """The whole posting: the description, the minimum and preferred qualifications, and the pay the footer states."""
    pid = str(job.detail.get("id") or job.id.split(":", 1)[-1])
    if not re.fullmatch(r"\d{6,12}", pid):
        raise ValueError("not an Apple position number")
    res = (_json("get", f"{APPLE}/api/v1/jobDetails/{pid}?locale=en-us", headers=_apple_headers()).get("res")) or {}
    parts = [str(res.get("description") or "").strip()]
    for heading, field in (("Minimum Qualifications", "minimumQualifications"), ("Preferred Qualifications", "preferredQualifications")):
        body = str(res.get(field) or "").strip()
        if body:
            parts.append(f"## {heading}\n{body}")
    text = "\n\n".join(p for p in parts if p)
    if not text:
        raise ValueError("jobs.apple.com: job answer has no text")
    job.description = text
    job.title = str(res.get("postingTitle") or "").strip() or job.title
    job.posted = _apple_date(res.get("postDateInGMT")) or job.posted
    job.location = _apple_places(res.get("locations")) or job.location
    footer = ""
    for item in res.get("postingFooters") or []:
        for entry in ((item or {}).get("localizations") or {}).get("en_US") or []:
            if "pay" in str((entry or {}).get("name") or "").lower():
                footer = strip_html(str(entry.get("content") or ""))
                break
        if footer:
            break
    pay_low, pay_high = _apple_pay(footer)
    if pay_low is not None or pay_high is not None:
        job.pay_min, job.pay_max = pay_low, pay_high


sources.DETAIL_FILLERS["apple"] = fill_apple_detail


# ----------------------------------------------------------------------------- Bank of America
BOFA = "https://careers.bankofamerica.com"
BOFA_PAGE = 50                    # jobs a request asks for
BOFA_MAX_REQUESTS = 80
_BOFA_DATE = "%m/%d/%Y"


def _bofa_date(text) -> str | None:
    try:
        return datetime.strptime(str(text or "").strip(), _BOFA_DATE).date().isoformat()
    except ValueError:
        return None


def parse_bofa_results(data: dict) -> list[Job]:
    rows = data.get("jobsList") if isinstance(data, dict) else None
    jobs = []
    for it in rows if isinstance(rows, list) else []:
        if not isinstance(it, dict) or not it.get("jobRequisitionId") or not it.get("postingTitle") or not str(it.get("jcrURL") or "").startswith("/"):
            continue
        place = str(it.get("primaryLocation") or it.get("location") or "").strip()
        if str(it.get("country") or "") == "United States" and "United States" not in place:
            place = f"{place}, United States" if place else "United States"
        url = BOFA + str(it["jcrURL"])
        jobs.append(Job(id=f"bankofamerica:{it['jobRequisitionId']}", source="bankofamerica", company="Bank of America",
                        title=str(it["postingTitle"]).strip(), location=place, url=url, posted=_bofa_date(it.get("postedDate")),
                        detail={"url": url}))
    return jobs


def fetch_bankofamerica(search_terms: list[str], pages: int = 8, delay: float = 0.4) -> list[Job]:
    """The career site's own search call. Its `rows` is the position the window ends at, not a page size, so a page is
    `start=N&rows=N+50`. The search looks at the whole posting, so the caller filters by title afterwards."""
    seen: dict[str, Job] = {}
    pace = _Pace(BOFA_MAX_REQUESTS, delay)
    for term in dict.fromkeys(t.strip() for t in search_terms if t and t.strip()):
        for page in range(pages):
            if not pace.more():
                return list(seen.values())
            start = page * BOFA_PAGE
            query = urlencode({"term": term, "start": start, "rows": start + BOFA_PAGE, "search": "getAllJobs"})
            data = _json("get", f"{BOFA}/services/jobssearchservlet?{query}")
            held = data.get("jobsList") or []
            _add(seen, parse_bofa_results(data))
            if len(held) < BOFA_PAGE or start + BOFA_PAGE >= int(data.get("totalMatches") or 0):
                break
    return list(seen.values())


def fill_bankofamerica_detail(job: Job) -> None:
    """The whole posting from the job page's schema.org JobPosting data: the text, the date and the places."""
    import retail_sitemap
    url = str(job.detail.get("url") or "")
    if urlparse(url).hostname != "careers.bankofamerica.com":
        raise ValueError("not a Bank of America job page address")
    r = sources._session().get(url, timeout=sources.TIMEOUT)
    r.raise_for_status()
    posting = retail_sitemap._json_ld(r.text)
    text = strip_html(str((posting or {}).get("description") or ""))
    if not text:
        raise ValueError("Bank of America: no JobPosting text on the page")
    job.description = text
    job.posted = retail_sitemap.iso_date(posting.get("datePosted")) or job.posted
    job.pay_min, job.pay_max = extract_pay(text)


sources.DETAIL_FILLERS["bankofamerica"] = fill_bankofamerica_detail

READERS = {
    "apple": ("Apple", fetch_apple),
    "bankofamerica": ("Bank of America", fetch_bankofamerica),
}
