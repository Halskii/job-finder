"""Retailer career sites read through their job sitemap. These sites have no search to call (or a search their robots.txt blocks),
but every one lists all of its postings in a sitemap with the title in the address, and every job page carries the posting as
schema.org JobPosting data. Same approach as Home Depot in retail_sources.py, made into one small engine and a table:

- LIST: read the sitemap (and the sitemaps an index lists), keep the addresses whose title words hold a search word, and store
  each as a job with a rough title read back from the address, no text, and `detail` set. No job page is read here.
- FILL: the web app reads the one page of a posting the first time someone's search selects it (sources.DETAIL_FILLERS) and takes
  the real title, text, date, places and pay from its JobPosting data.

Per retailer the table (SITES) holds the sitemap addresses, how to read an address (id and title), and a few quirks:
- Gap (gapinc.com): the brand (Old Navy, Banana Republic, Athleta, Gap) shows only on the page (the posting's organization and the
  page title), so the filler sets `company`. A closed job answers 302 to the job-search page, which robots.txt blocks, so redirects are never followed: a 302 means "gone".
- Nike (careers.nike.com): its Workday host is blocked in robots.txt, so only the Nike-hosted sitemap is used.
- NAPA (jobs.genpt.com), The Fresh Market, IKEA, 7-Eleven and Walgreens run on Radancy, whose robots.txt blocks `/search-jobs/`
  (Walgreens allows it, but the sitemap is enough); only the sitemap and job pages are read.
- URBN (Urban Outfitters, Anthropologie, Free People, Nuuly): an iCIMS portal whose pages are 20 times lighter with `?in_iframe=1`;
  the brand is the first word of the address.
- Ross: dates are MM/DD/YYYY; ross.avature.net (the apply side) is never called; the page marks its JSON-LD script with `ld&#x2B;json`.
- IKEA lists every country: a posting's country is only in its page, so a foreign posting is dropped after it is read (its place
  then names the country) and the sitemap's city is kept as the place until then.
- Wayfair: some old pages have no JobPosting data, which the filler reports as an error.
- Publix: a Next.js page with no JSON-LD; the posting is the WordPress record in the page's `__NEXT_DATA__` (pay per month there).
- Container Store dates read "Sep 28, 2026", and its pay is only in the structured data (an hourly rate is added to the text
  so sources.fill_pay finds it).
No site here has a Crawl-delay.
"""
from __future__ import annotations

import html
import json
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from urllib.parse import unquote, urljoin

import filters
import sources
from sources import Job, extract_pay, strip_html

# Requests for the list of one retailer: the sitemap, plus those an index lists. Each page read afterwards is one request more.
MAX_REQUESTS = 6
# A word like "cashier" can match thousands of addresses (Love's); the newest are kept.
MAX_LISTED = 3000
# An address whose sitemap date is older than this is skipped (a site that dates every address with the day the sitemap was made is unaffected).
MAX_AGE_DAYS = 365
SITEMAP_TIMEOUT = 60      # the biggest sitemaps are 3 MB


@dataclass(frozen=True)
class Site:
    label: str                      # the company shown to people
    sitemaps: tuple[str, ...]       # sitemap or sitemap index addresses
    job: str                        # the path of a job address, after the host: named groups `id` and `slug` (and `city`)
    children: str = ""              # which sitemaps an index lists to read, as a regex (all of them when empty)
    suffix: str = ""                # added to the address when the page is read
    cut: str = ""                   # regex of the tail of an address that is not part of the title (a place)
    follow: bool = False            # follow redirects when reading a job page (never where they lead to a blocked page)
    country: str = ""               # the country of a place that names none
    city_location: bool = False     # keep the address's city as the place until the page is read
    brands: tuple = ()              # (first word(s) of the address, company): a retailer with several banners
    reader: str = "jsonld"          # how to read a job page: "jsonld" or "publix"
    company_from: str = ""          # where the filler finds the banner: "org" (Gap) or "industry" (NAPA); else the label stays


# Radancy: /job/<city>/<title>/<site id>/<job id>
def _radancy(prefix: str = "") -> str:
    return prefix + r"/job/(?P<city>[^/]+)/(?P<slug>[^/]+)/\d+/(?P<id>\d+)"


SITES: dict[str, Site] = {
    "gap": Site("Gap Inc.", ("https://www.gapinc.com/googlesitemap.xml",), r"/en-us/jobs/(?P<id>w\d+/[\d-]+)/(?P<slug>[^/?#]+)",
                company_from="org"),
    "nike": Site("Nike", ("https://careers.nike.com/sitemap.xml",), r"/(?P<slug>[^/]+)/job/(?P<id>R-\d+)", children=r"-en\.xml$"),
    "napa": Site("NAPA Auto Parts", ("https://jobs.genpt.com/sitemap.xml",), _radancy(), company_from="industry"),
    "bathandbodyworks": Site("Bath & Body Works", ("https://careers.bathandbodyworks.com/sitemap.xml",),
                             r"/(?P<slug>[^/]+)/job/(?P<id>[A-Za-z0-9]+)", children=r"-en\.xml$"),
    "urbn": Site("URBN", ("https://homeoffice-na-urbn.icims.com/sitemap.xml",), r"/jobs/(?P<id>\d+)/(?P<slug>[^/]+)/job",
                 suffix="?in_iframe=1", brands=(("urban-outfitters", "Urban Outfitters"), ("uo", "Urban Outfitters"),
                                                ("anthropologie", "Anthropologie"), ("free-people", "Free People"), ("fp", "Free People"),
                                                ("nuuly", "Nuuly"), ("terrain", "Terrain"))),
    "ross": Site("Ross Stores", ("https://jobs.rossstores.com/sitemap.xml",), r"/search/jobdetails/(?P<slug>[^/]+)/(?P<id>[0-9a-fA-F-]{36})",
                 children=r"jobs-site\.xml$"),
    "lidl": Site("Lidl US", ("https://careers.lidl.com/sitemap.xml",), r"/jobs/(?P<slug>.+)-(?P<id>\d+)", children=r"sitemap-jobs\.xml$"),
    "freshmarket": Site("The Fresh Market", ("https://jobs.thefreshmarket.com/sitemap.xml",), _radancy()),
    "wayfair": Site("Wayfair", ("https://www.wayfair.com/careers/sitemap.xml",), r"/careers/job/(?P<slug>[^/]+)/(?P<id>2-\d+)"),
    "ikea": Site("IKEA", ("https://jobs.ikea.com/sitemap.xml",), _radancy("/en"), city_location=True),
    "containerstore": Site("The Container Store", ("https://www.jobs.containerstore.com/sitemap.xml",),
                           r"/jobs/(?P<slug>.+)-container-store-jobs-(?P<id>\d+)", cut=r"-jobs-location-.*$"),
    "seveneleven": Site("7-Eleven", ("https://careers.7-eleven.com/sitemap.xml",), _radancy()),
    "loves": Site("Love's Travel Stops", ("https://jobs.loves.com/sitemap.xml",), r"/(?P<slug>[^/]+)/job/(?P<id>\d+)", children=r"-en\.xml$"),
    "pilot": Site("Pilot Flying J", ("https://careers.pilotcompany.com/sitemap.xml",), r"/(?P<slug>[^/]+)/job/(?P<id>[0-9A-Fa-f]{32})",
                  children=r"-en\.xml$"),
    "publix": Site("Publix", ("https://jobs.publix.com/sitemap.xml",), r"/job-opening/(?P<id>(?P<slug>[^/]+))", children=r"job_opening-sitemap\.xml$",
                   cut=r"-\d+$", reader="publix", country="United States"),
    "walgreens": Site("Walgreens", ("https://jobs.walgreens.com/en/sitemap.xml",), _radancy("/en"), country="United States"),
}


# ----------------------------------------------------------------------------- the list: sitemaps
def _today() -> date:
    return date.today()


def _words(text: str) -> str:
    """'Principal%20Engineer,-Agentic' as 'principal-engineer-agentic': lowercase words joined with single dashes."""
    return re.sub(r"[^a-z0-9]+", "-", unquote(text).lower()).strip("-")


def _patterns(search_terms: list[str]) -> list[re.Pattern]:
    return [re.compile(r"(?:^|-)" + re.escape(w) + r"(?:-|$)") for w in (_words(t) for t in search_terms) if w]


def _sitemap(xml: str, base: str) -> tuple[list[str], list[tuple[str, str]]]:
    """(sitemaps listed by an index, (address, date) of each page listed). Raises ValueError for a page that is not a sitemap."""
    if "<sitemapindex" in xml:
        return [urljoin(base, html.unescape(m).strip()) for m in re.findall(r"<sitemap>.*?<loc>\s*(.*?)\s*</loc>", xml, re.S)], []
    if "<urlset" not in xml:
        raise ValueError("not a sitemap")
    entries = []
    for block in re.findall(r"<url>(.*?)</url>", xml, re.S):
        loc, mod = re.search(r"<loc>\s*(.*?)\s*</loc>", block, re.S), re.search(r"<lastmod>\s*(.*?)\s*</lastmod>", block, re.S)
        if loc:
            entries.append((urljoin(base, html.unescape(loc.group(1))), mod.group(1) if mod else ""))
    return [], entries


def read_sitemaps(site: Site, delay: float) -> list[tuple[str, str]]:
    """Every (address, date) the site's sitemaps list, at most MAX_REQUESTS requests. Raises when an answer is not a sitemap."""
    todo, entries, asked = list(site.sitemaps), [], 0
    while todo:
        if asked >= MAX_REQUESTS:
            raise ValueError(f"more than {MAX_REQUESTS} sitemaps")
        address = todo.pop(0)
        if asked:
            time.sleep(delay)
        r = sources._session().get(address, timeout=SITEMAP_TIMEOUT)
        r.raise_for_status()
        asked += 1
        more, found = _sitemap(r.text, address)
        todo += [m for m in more if not site.children or re.search(site.children, m)]
        entries += found
    return entries


def _job_re(site: Site) -> re.Pattern:
    return re.compile(r"https?://[^/]+" + site.job + r"/?$")


def _company(site: Site, slug: str) -> str:
    return next((name for prefix, name in site.brands if slug == prefix or slug.startswith(prefix + "-")), site.label)


def parse_sitemap(site: Site, entries: list[tuple[str, str]], search_terms: list[str], key: str = "", today: date | None = None) -> list[Job]:
    """The job addresses among the sitemap's, the newest first, that hold one of the search words as whole words in their title.
    The title here is the address read back; the job page gives the real one."""
    key = key or next(k for k, s in SITES.items() if s is site)
    patterns = _patterns(search_terms)
    job_re = _job_re(site)
    oldest = ((today or _today()) - timedelta(days=MAX_AGE_DAYS)).isoformat()
    found, seen = [], set()
    for address, modified in entries:
        m = job_re.match(address)
        if not m or m.group("id") in seen or (modified[:4].isdigit() and modified[:10] < oldest):
            continue
        slug = unquote(m.group("slug")).lower()
        if site.cut:
            slug = re.sub(site.cut, "", slug)
        words = _words(slug)
        if not any(p.search(words) for p in patterns):
            continue
        seen.add(m.group("id"))
        city = m.groupdict().get("city") or ""
        found.append((modified, Job(
            id=f"{key}:{m.group('id').replace('/', '-')}", source=key, company=_company(site, words), title=words.replace("-", " ").title(),
            location=_words(city).replace("-", " ").title() if site.city_location else "", url=address,
            detail={"api": address + site.suffix})))
    found.sort(key=lambda pair: pair[0], reverse=True)       # stable: addresses with the same date keep the sitemap's order
    return [job for _, job in found[:MAX_LISTED]]


def fetch_site(key: str, search_terms: list[str], pages: int = 20, delay: float = 0.4) -> list[Job]:
    """The postings whose title holds a search word. `pages` is not used: a sitemap has no pages to step through."""
    site = SITES[key]
    if not _patterns(search_terms):
        return []
    entries = read_sitemaps(site, delay)
    job_re = _job_re(site)
    if not any(job_re.match(address) for address, _ in entries):
        raise ValueError("no job addresses in the sitemap (the site changed)")
    return parse_sitemap(site, entries, search_terms, key)


# ----------------------------------------------------------------------------- the job page
# Country codes written out as the names filters.NON_US_HINTS knows: a place ending in ', IN' or ', CA' would pass for Indiana or
# California.
COUNTRIES = dict(pair.split(" ", 1) for pair in (
    "AE United Arab Emirates;AR Argentina;AT Austria;AU Australia;BD Bangladesh;BE Belgium;BG Bulgaria;BR Brazil;CA Canada;CH Switzerland;"
    "CL Chile;CN China;CO Colombia;CR Costa Rica;CZ Czech Republic;DE Germany;DK Denmark;EG Egypt;ES Spain;FI Finland;FR France;"
    "GB United Kingdom;UK United Kingdom;GR Greece;HK Hong Kong;HR Croatia;HU Hungary;ID Indonesia;IE Ireland;IL Israel;IN India;IT Italy;"
    "JO Jordan;JP Japan;KH Cambodia;KR South Korea;KW Kuwait;LT Lithuania;LV Latvia;MA Morocco;MX Mexico;MY Malaysia;NL Netherlands;"
    "NO Norway;NZ New Zealand;PH Philippines;PL Poland;PT Portugal;RO Romania;RS Serbia;SA Saudi Arabia;SE Sweden;SG Singapore;"
    "SI Slovenia;SK Slovakia;TH Thailand;TR Turkey;TW Taiwan;UA Ukraine;VN Vietnam;ZA South Africa").split(";"))
US_NAMES = {"us", "usa", "u.s.", "united states", "united states of america"}


def _case(text: str) -> str:
    """'ATLANTA' as 'Atlanta'; a two-letter state code and mixed-case text stay as they are."""
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text.title() if len(text) > 2 and text.isupper() else text


def _country(value, default: str = "") -> str:
    name = value.get("name") if isinstance(value, dict) else value
    name = re.sub(r"\s+", " ", str(name or "")).strip()
    if not name:
        return default
    if name.lower() in US_NAMES:
        return "United States"
    return COUNTRIES.get(name.upper(), _case(name)) if len(name) == 2 else _case(name)


def _place(place, site: Site) -> str:
    """'Charlotte, North Carolina, United States' or 'Austin, TX' (a US place whose state is a code leaves the country out)."""
    address = place.get("address") if isinstance(place, dict) else None
    if isinstance(address, str):
        return _case(address)
    if not isinstance(address, dict):
        return ""
    city, region = _case(address.get("addressLocality")), _case(address.get("addressRegion"))
    country = _country(address.get("addressCountry"), site.country)
    parts = [x for x in (city, region) if x]
    if country and not (country == "United States" and len(region) == 2):
        parts.append(country)
    return ", ".join(parts)


def _places(posting: dict, site: Site) -> list[str]:
    """Every place a posting is open in; ' (Remote)' is added when the posting says the work is remote (schema.org's jobLocationType
    TELECOMMUTE, or the street address 'VIRTUAL' that Home Depot uses)."""
    where = posting.get("jobLocation")
    out, remote = [], str(posting.get("jobLocationType") or "").upper() == "TELECOMMUTE"
    for place in where if isinstance(where, list) else [where]:
        address = place.get("address") if isinstance(place, dict) else None
        text = _place(place, site)
        if text and (remote or (isinstance(address, dict) and str(address.get("streetAddress") or "").strip().upper() == "VIRTUAL")):
            text += " (Remote)"
        if text and text not in out:
            out.append(text)
    return out or (["Remote"] if remote else [])


def iso_date(text) -> str | None:
    """YYYY-MM-DD from '2026-10-26T00:00:00+00:00', '10/23/2026' (Ross) or 'Sep 28, 2026' (Container Store, Publix); None otherwise."""
    text = str(text or "").strip()
    if re.match(r"\d{4}-\d{2}-\d{2}", text):
        return text[:10]
    for pattern in ("%m/%d/%Y", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(text, pattern).date().isoformat()
        except ValueError:
            pass
    return None


def _money(n: float) -> str:
    return f"${n:,.2f}" if n != int(n) else f"${int(n):,}"


def _pay_line(low, high, per: str) -> str:
    """'Pay: $22.00 - $24.00 per hour' for the pay a page keeps outside its text, written so extract_pay and fill_pay find it. A
    monthly or weekly figure is turned into a yearly one."""
    per = per.lower()
    times = 12 if "month" in per else 52 if "week" in per else 1
    unit = "hour" if "hour" in per else "year" if times > 1 or "year" in per or "annual" in per else ""
    nums = [float(n) * times for n in (low, high) if n not in (None, "") and float(n) > 0]
    return f"Pay: {' - '.join(dict.fromkeys(_money(n) for n in nums))} per {unit}" if nums and unit else ""


def _salary(posting: dict) -> str:
    """baseSalary as a pay line. Most sites leave it an empty shell; the Container Store puts 'USD 22.00 - 24.00 per hour' in it."""
    salary = posting.get("baseSalary")
    value = salary.get("value") if isinstance(salary, dict) else None
    if isinstance(value, dict):
        text, per = value.get("value"), str(value.get("unitText") or "")
        if isinstance(text, str) and re.search(r"\d", text):
            return "Pay: " + re.sub(r"\bUSD\s*", "$", text).replace("$ ", "$")
        try:
            return _pay_line(value.get("minValue") or text, value.get("maxValue") or text, per)
        except (TypeError, ValueError):
            return ""
    return ""


def _json_ld(page: str) -> dict:
    """The schema.org JobPosting object among a page's ld+json scripts, or {}. Ross writes the type as `application/ld&#x2B;json`."""
    for block in re.findall(r"<script[^>]*type=.application/ld(?:\+|&#x2B;|&#43;)json.[^>]*>(.*?)</script>", page, re.S | re.I):
        try:
            data = json.loads(block, strict=False)
        except ValueError:
            continue
        for item in (data if isinstance(data, list) else data.get("@graph") or [data]) if isinstance(data, (list, dict)) else []:
            kind = item.get("@type") if isinstance(item, dict) else None
            if kind == "JobPosting" or (isinstance(kind, list) and "JobPosting" in kind):
                return item
    return {}


def _page_company(site: Site, page: str, posting: dict) -> str:
    org = posting.get("hiringOrganization")
    org = str(org.get("name") or "").strip() if isinstance(org, dict) else ""
    if site.company_from == "org":                # Gap: 'Old Navy', 'Banana Republic', 'Athleta' or 'Gap Inc.'; the page title ends with it too
        title = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
        parts = html.unescape(title.group(1)).rsplit("|", 1) if title else []
        return org or (parts[1].strip() if len(parts) == 2 else "")
    if site.company_from == "industry":           # Genuine Parts' board: industry NAPA, or another brand of the group
        return "NAPA Auto Parts" if str(posting.get("industry") or "").strip().upper() == "NAPA" else org
    return ""


def read_jsonld(site: Site, page: str) -> dict:
    posting = _json_ld(page)
    if not posting:
        raise ValueError("no JobPosting data on the page")
    return {"title": html.unescape(str(posting.get("title") or "")).strip(), "text": strip_html(str(posting.get("description") or "")),
            "posted": iso_date(posting.get("datePosted")), "places": _places(posting, site), "pay": _salary(posting),
            "company": _page_company(site, page, posting)}


def read_publix(site: Site, page: str) -> dict:
    """Publix: a Next.js page with no JSON-LD. The WordPress record of the posting is in the page's __NEXT_DATA__ (`fallback`), with its
    text as blocks, one of which ('publix/job-header') holds the place, the date and the pay."""
    found = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
    try:
        fallback = json.loads(found.group(1))["props"]["pageProps"]["fallback"]
    except (AttributeError, KeyError, TypeError, ValueError):
        raise ValueError("no Publix posting data on the page") from None
    records = [v.get("result", v) for v in (fallback.values() if isinstance(fallback, dict) else []) if isinstance(v, dict)]
    post = next((v for v in records if isinstance(v, dict) and isinstance(v.get("content"), dict)
                 and "publix/job-header" in str(v["content"].get("rendered"))), None)
    if post is None:
        raise ValueError("no Publix posting data on the page")
    body = str(post["content"]["rendered"])
    block = re.search(r"data-wp-block-name=.publix/job-header. data-wp-block='(\{.*?\})'", body, re.S)
    try:
        head = json.loads(block.group(1))
    except (AttributeError, ValueError):
        raise ValueError("no Publix posting header on the page") from None
    place = html.unescape(str(head.get("location") or "")).strip()
    if place and not filters.US_STATE_RE.search(place):
        place += ", United States"                # 'All retail locations'
    pay = re.findall(r"\d[\d,]*(?:\.\d+)?", str(head.get("pay_range") or ""))
    return {"title": html.unescape(str(head.get("title") or (post.get("title") or {}).get("rendered") or "")).strip(), "text": strip_html(body),
            "posted": iso_date(head.get("date_posted")), "places": [place] if place else [],
            "pay": _pay_line(*(pay[0].replace(",", ""), pay[-1].replace(",", "")), str(head.get("pay_frequency") or "")) if pay else "",
            "company": ""}


READ_PAGE = {"jsonld": read_jsonld, "publix": read_publix}


def fill_sitemap_detail(job: Job) -> None:
    """The whole posting from its page: the real title, the text, the posting date, the places and any pay it states. A redirect is
    never followed (Gap sends a closed job to a page robots.txt blocks), so it means the posting is gone; the web app tries a few times."""
    site = SITES[job.source]
    r = sources._session().get(str(job.detail.get("api") or job.url), timeout=sources.TIMEOUT, allow_redirects=site.follow)
    if 300 <= r.status_code < 400:
        raise ValueError("job page moved or closed")
    r.raise_for_status()
    info = READ_PAGE[site.reader](site, r.text)
    text = info["text"]
    if not text:
        raise ValueError("job page layout not recognized")
    pay = info["pay"]
    if pay and pay not in text and not extract_pay(text)[0]:
        text += f"\n\n{pay}"
    text = re.sub(r"\bUSD\s*(?=\d)", "$", text)    # 'USD 165,000 - USD 181,500' as the pay patterns expect
    job.description = text
    job.title = info["title"] or job.title
    job.posted = info["posted"] or job.posted
    job.company = info["company"] or job.company
    job.location = "; ".join(info["places"]) or job.location
    job.pay_min, job.pay_max = extract_pay(text)


for _key in SITES:
    sources.DETAIL_FILLERS[_key] = fill_sitemap_detail


# ----------------------------------------------------------------------------- reader table
def _fetcher(key: str):
    def fetch(search_terms: list[str], pages: int = 20, delay: float = 0.4) -> list[Job]:
        return fetch_site(key, search_terms, pages, delay)
    fetch.__name__ = f"fetch_{key}"
    return fetch


READERS = {key: (site.label, _fetcher(key)) for key, site in SITES.items()}
