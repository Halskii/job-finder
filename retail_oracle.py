"""Retailers whose jobs are on Oracle Recruiting Cloud ("candidate experience" career sites): Albertsons (and its banners Safeway,
Vons, Jewel-Osco, ...), Kroger, Dollar General, Macy's, Bloomingdale's, Staples, Sherwin-Williams, AutoZone, Sally Beauty,
American Eagle (Aerie), Williams-Sonoma (Pottery Barn, West Elm), At Home and Warby Parker.

Every one of these career pages is a script over the same public REST resource, which the page itself calls without signing in:
`/hcmRestApi/resources/latest/recruitingCEJobRequisitions` lists the postings that match a keyword and
`.../recruitingCEJobRequisitionDetails` holds one posting in full. Only the Oracle host and the site number differ, so one table
(ORACLE_SITES) and one reader cover them all. None of these Oracle hosts has a robots.txt (it answers 404). The retailers' own
marketing sites and the Radancy search pages in front of some of them are never used (Staples' `/search-jobs` is disallowed).

The list carries only a short teaser, so a posting is stored without text and with `detail` set; the web app reads the full text the
first time someone's search selects it. The keyword search also matches the description, so it returns unrelated titles for a word;
results are ordered by relevance, which puts the matching titles first, and the caller filters by title.
"""
from __future__ import annotations

import re
import threading
import time
from urllib.parse import quote

import sources
from sources import Job, extract_pay, strip_html

# key -> (Oracle host, site number, company shown when the posting names no banner or brand). The site number is the one in the
# address of the career page, .../CandidateExperience/en/sites/<site number>. Restoration Hardware is not listed: unverified.
ORACLE_SITES = {
    "albertsons": ("eofd.fa.us6.oraclecloud.com", "CX_1001", "Albertsons"),             # also Safeway, Vons, Jewel-Osco, ... (the banner)
    "kroger": ("eluq.fa.us2.oraclecloud.com", "CX_2001", "Kroger"),                     # krogerfamilycareers.com; 11.5k jobs, mostly stores
    "dollargeneral": ("ibxwjb.fa.ocs.oraclecloud.com", "CX_1", "Dollar General"),       # 91k jobs, mostly stores
    "macys": ("ebwh.fa.us2.oraclecloud.com", "CX_1001", "Macy's"),
    "bloomingdales": ("ebwh.fa.us2.oraclecloud.com", "CX_1002", "Bloomingdale's"),      # same host and tenant as Macy's
    "staples": ("fa-exhh-saasfaprod1.fa.ocs.oraclecloud.com", "StaplesInc", "Staples"),  # US and Canada
    "sherwinwilliams": ("ejhp.fa.us6.oraclecloud.com", "CX_2", "Sherwin-Williams"),
    "autozone": ("egud.fa.us2.oraclecloud.com", "CX_1", "AutoZone"),
    "sallybeauty": ("eigx.fa.us6.oraclecloud.com", "CX", "Sally Beauty"),
    "americaneagle": ("hcml.fa.us2.oraclecloud.com", "CX_2", "American Eagle Outfitters"),
    "williamssonoma": ("ehac.fa.us6.oraclecloud.com", "CX_1", "Williams-Sonoma"),
    "athome": ("hdiy.fa.us2.oraclecloud.com", "CX", "At Home"),
    "warbyparker": ("fa-evdi-saasfaprod1.fa.ocs.oraclecloud.com", "CX_1", "Warby Parker"),
}
# Where the posting has no brand field, the brand is in the title ("Aerie - Brand Ambassador", "WE Dadeland 878 PT Stock Assoc").
# key -> [(company, pattern on the title)], first match wins.
TITLE_BRANDS = {
    "americaneagle": [("Aerie", r"\bAerie\b"), ("Todd Snyder", r"\bTodd Snyder\b"), ("Unsubscribed", r"\bUnsubscribed\b")],
    "williamssonoma": [("Pottery Barn", r"\bPottery Barn\b"), ("West Elm", r"\bWest Elm\b|^WE\b"), ("Rejuvenation", r"\bRejuvenation\b"),
                       ("Mark and Graham", r"\bMark (?:and|&) Graham\b")],
}
BANNER_NAMES = {"Jewel Osco": "Jewel-Osco"}     # how the banner field spells it -> how the store spells it

PAGE_SIZE = 200               # rows per list request: the resource takes it, and a big page means far fewer requests
# Albertsons' robots.txt (www.albertsonscompanies.com) says Crawl-delay: 10 and is binding for its Oracle site too. The seconds are
# kept per host because the delay applies to every request, whichever reader (list or full posting) makes it.
ALBERTSONS_CRAWL_DELAY = 10.0
CRAWL_DELAY = {ORACLE_SITES["albertsons"][0]: ALBERTSONS_CRAWL_DELAY}
# The most list requests one run makes for one retailer. The words are searched breadth first (every word's first page before any
# word's second), so a cut loses the last pages of the big searches and never a word. At Albertsons' 10 seconds, 60 requests are
# ten minutes; 50 title words fit with room for a few second pages.
MAX_REQUESTS = {"albertsons": 60}
MAX_REQUESTS_DEFAULT = 300

_API = "/hcmRestApi/resources/latest"
_EXPAND = "requisitionList.secondaryLocations,requisitionList.requisitionFlexFields"    # flex fields: the banner, sometimes the pay


def _site(key: str) -> tuple[str, str, str]:
    return ORACLE_SITES[key]


def job_url(key: str, job_id: str) -> str:
    """The page a person opens for a posting."""
    host, site, _ = _site(key)
    return f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{quote(job_id, safe='')}"


def list_url(key: str, term: str, offset: int) -> str:
    """One page of the search for a word. The finder is the one the career page sends; its parts are separated by commas, so the
    word may not hold any of the characters that would end the part. Ordered by relevance: the fuzzy search also matches the text."""
    host, site, _ = _site(key)
    word = re.sub(r'[",;=()\\]+', " ", term)
    word = re.sub(r"\s+", " ", word).strip()
    return (f"https://{host}{_API}/recruitingCEJobRequisitions?onlyData=true&expand={_EXPAND}"
            f"&finder=findReqs;siteNumber={site},limit={PAGE_SIZE},offset={offset},keyword=%22{quote(word, safe='')}%22,sortBy=RELEVANCY")


def detail_url(key: str, job_id: str) -> str:
    host, site, _ = _site(key)
    return (f"https://{host}{_API}/recruitingCEJobRequisitionDetails?expand=all&onlyData=true"
            f"&finder=ById;Id=%22{quote(job_id, safe='')}%22,siteNumber={site}")


# ----------------------------------------------------------------------------- pacing
_pace_guard = threading.Lock()
_pace_locks: dict[str, threading.Lock] = {}
_last_request: dict[str, float] = {}


def _pace(host: str, gap: float) -> None:
    """Wait until `gap` seconds have passed since the last request to this host, from any reader (Macy's and Bloomingdale's share
    a host and run at the same time). Called just before each request."""
    gap = max(gap, CRAWL_DELAY.get(host, 0.0))
    with _pace_guard:
        lock = _pace_locks.setdefault(host, threading.Lock())
    with lock:
        wait = _last_request.get(host, float("-inf")) + gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request[host] = time.monotonic()


def _get_json(host: str, url: str, delay: float) -> dict:
    _pace(host, delay)
    r = sources._session().get(url, timeout=sources.TIMEOUT)
    r.raise_for_status()
    try:
        data = r.json()
    except ValueError:
        raise ValueError("answer is not JSON (the site changed, or the request was turned away)") from None
    if not isinstance(data, dict):
        raise ValueError("answer is not what an Oracle recruiting site sends")
    return data


# ----------------------------------------------------------------------------- reading one posting's fields
def _flex(job: dict) -> list[tuple[str, str]]:
    """The posting's extra fields as (prompt, value), the empty ones left out."""
    out = []
    for f in job.get("requisitionFlexFields") or []:
        if isinstance(f, dict) and str(f.get("Value") or "").strip():
            out.append((str(f.get("Prompt") or "").strip(), str(f["Value"]).strip()))
    return out


def _banner(job: dict) -> str:
    """The banner or brand field of the posting (Albertsons: Safeway, Vons, ...; Kroger: Fred Meyer, Ralphs, ...), or ''."""
    for prompt, value in _flex(job):
        if re.fullmatch(r"(?i)(banner|brand)( name)?", prompt):
            return BANNER_NAMES.get(value, value)
    return ""


def _company(key: str, job: dict) -> str:
    """The banner the posting names, else a brand in the title where the retailer puts it there, else the retailer."""
    label = _site(key)[2]
    if _banner(job):
        return _banner(job)
    title = str(job.get("Title") or "")
    for company, pattern in TITLE_BRANDS.get(key, []):
        if re.search(pattern, title):
            return company
    return label


def _places(job: dict) -> str:
    """'Charlotte, NC, United States', or every place ('; ') for a posting open in several, plus ' (Remote)' when the work location is remote."""
    places = []
    for place in [job.get("PrimaryLocation"), *[s.get("Name") for s in job.get("secondaryLocations") or [] if isinstance(s, dict)]]:
        place = str(place or "").strip()
        if place and place not in places:
            places.append(place)
    if len(places) > 1:      # a bare "United States" next to the real places only adds noise
        places = [p for p in places if p != "United States"]
    text = "; ".join(places)
    if job.get("WorkplaceTypeCode") == "ORA_REMOTE" or str(job.get("WorkplaceType") or "").lower() == "remote":
        text = f"{text} (Remote)" if text else "Remote"
    return text


def _flex_pay(job: dict) -> tuple[float | None, float | None, str]:
    """Pay from a pay field of the posting ('Expected Pay: $150,000-$160,000', or 'Minimum Pay Rate' and 'Maximum Pay Rate'): the
    first two figures, as a yearly or an hourly range by size."""
    nums = []
    for prompt, value in _flex(job):
        if re.search(r"(?i)\b(pay|salary|wage|compensation)\b", prompt):
            # figures after a $ sign, or the value itself when it is only a number ('17.5'); other digits (401k) are not pay
            found = re.findall(r"\$\s*(\d[\d,]*(?:\.\d+)?)", value) or re.findall(r"^\s*(\d[\d,]*(?:\.\d+)?)\s*$", value)
            nums += [float(n.replace(",", "")) for n in found]
    nums = nums[:2]
    if not nums:
        return None, None, "year"
    lo, hi = min(nums), max(nums)
    if 30_000 <= lo and hi <= 1_000_000:
        return int(lo), int(hi), "year"
    if 7 <= lo and hi <= 500:
        return (int(lo) if lo == int(lo) else lo), (int(hi) if hi == int(hi) else hi), "hour"
    return None, None, "year"


def _date(value) -> str | None:
    text = str(value or "")[:10]
    return text if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text) else None


def _list_block(data: dict) -> dict:
    items = data.get("items")
    block = items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else None
    if block is None or not isinstance(block.get("requisitionList"), list):
        raise ValueError("search answer not recognized (no requisitionList)")
    return block


def parse_oracle_list(data: dict, key: str) -> tuple[list[Job], int, int]:
    """One page of a search: its postings, how many the whole search found, and how many rows the page held (a posting with no id or
    title is skipped but still counts as a row, so the next page starts in the right place)."""
    block = _list_block(data)
    rows = block["requisitionList"]
    jobs = []
    for it in rows:
        if not isinstance(it, dict):
            continue
        jid, title = str(it.get("Id") or "").strip(), str(it.get("Title") or "").strip()
        if not jid or not title:
            continue
        lo, hi, unit = _flex_pay(it)
        jobs.append(Job(
            id=f"{key}:{jid}", source=key, company=_company(key, it), title=title, location=_places(it), url=job_url(key, jid),
            posted=_date(it.get("PostedDate")), pay_min=lo, pay_max=hi, pay_unit=unit, detail={"api": detail_url(key, jid)},
        ))
    total = block.get("TotalJobsCount")
    return jobs, int(total) if isinstance(total, (int, float)) else 0, len(rows)


# ----------------------------------------------------------------------------- the search
def _words(term: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", term.lower()) if len(w) >= 3] or [term.lower().strip()]


def _title_matches(term: str, jobs: list[Job]) -> bool:
    words = _words(term)
    return any(w in j.title.lower() for j in jobs for w in words)


def fetch_oracle(key: str, search_terms: list[str], pages: int = 20, delay: float = 0.4) -> list[Job]:
    """Search for each word on the retailer's site, `pages` result pages at most per word, and at most MAX_REQUESTS requests in all.
    The words go breadth first: every word's first page, then every word's second, and so on. A word stops when its results are used
    up, or when a page has no title that holds any of its words (the results are ordered by relevance, so what follows is the text
    matching only)."""
    host = _site(key)[0]
    budget = MAX_REQUESTS.get(key, MAX_REQUESTS_DEFAULT)
    terms = list(dict.fromkeys(t.strip().lower() for t in search_terms if t and t.strip()))
    offsets = {t: 0 for t in terms}
    seen: dict[str, Job] = {}
    made = 0
    for _ in range(max(0, pages)):
        for term in [t for t in terms if t in offsets]:
            if made >= budget:
                return list(seen.values())
            data = _get_json(host, list_url(key, term, offsets[term]), delay)
            made += 1
            jobs, total, held = parse_oracle_list(data, key)
            for j in jobs:
                seen.setdefault(j.id, j)
            offsets[term] += held
            if not held or offsets[term] >= total or not _title_matches(term, jobs):
                del offsets[term]
        if not offsets:
            break
    return list(seen.values())


# ----------------------------------------------------------------------------- the full posting
def fill_oracle_detail(job: Job) -> None:
    """The whole posting: description, responsibilities and qualifications as the page shows them, the pay the posting states, the
    real title, places and posting date."""
    key = job.source
    host = _site(key)[0]
    data = _get_json(host, job.detail.get("api") or detail_url(key, job.id.split(":", 1)[-1]), 0.0)
    items = data.get("items")
    it = items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else {}
    parts = [strip_html(str(it.get(f) or "")) for f in ("ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr", "OrganizationDescriptionStr")]
    text = "\n\n".join(p for p in parts if p)
    if not text:
        raise ValueError("job page layout not recognized")
    lines = [f"{prompt}: {value}" for prompt, value in _flex(it) if re.search(r"(?i)\b(pay|salary|wage|compensation)\b", prompt) and value not in text]
    if lines:
        text += "\n\n" + "\n".join(lines)
    job.description = text
    job.title = str(it.get("Title") or "").strip() or job.title
    job.location = _places(it) or job.location
    job.posted = _date(it.get("ExternalPostedStartDate")) or job.posted
    job.company = _banner(it) or job.company
    lo, hi, unit = _flex_pay(it)
    if lo is None:
        lo, hi = extract_pay(text)
    if lo is not None:
        job.pay_min, job.pay_max, job.pay_unit = lo, hi, unit


# ----------------------------------------------------------------------------- registration
def _reader(key: str):
    def fetch(search_terms: list[str], pages: int = 20, delay: float = 0.4) -> list[Job]:
        return fetch_oracle(key, search_terms, pages, delay)
    fetch.__name__ = f"fetch_{key}"
    return fetch


LABELS = {"albertsons": "Albertsons / Safeway", "americaneagle": "American Eagle / Aerie", "williamssonoma": "Williams-Sonoma / Pottery Barn"}
READERS = {key: (LABELS.get(key, site[2]), _reader(key)) for key, site in ORACLE_SITES.items()}
for _key in ORACLE_SITES:
    sources.DETAIL_FILLERS[_key] = fill_oracle_detail
