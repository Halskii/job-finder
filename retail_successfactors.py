"""Retailers whose career site is an SAP SuccessFactors "career site builder" page: Tractor Supply, Sephora, Adidas and Under Armour.
Their robots.txt files (the same on every one) block only the apply, talent-community, e-mail, error and `/services/` paths
(`/services/` holds the RSS feed, which is left alone), so the search page `<site>/search/?q=<words>&startrow=<n>` and the job
pages `<site>/job/<slug>/<id>/` are read as HTML. None of them sets a Crawl-delay.

The search page is the site's own, so a word is looked up the way a visitor would. The page lists 10 to 50 postings (a table row or a
tile each, with the title, the place and sometimes the date); the text of a posting is on its job page, read later through
DETAIL_FILLERS. QuikTrip runs the same software but its search page is drawn by script and holds no postings in the page, so it is not read.
"""
from __future__ import annotations

import html
import re
import time
from datetime import datetime
from urllib.parse import quote_plus

import sources
from sources import Job, extract_pay, strip_html

# None of the four sites states a Crawl-delay in robots.txt; `delay` (0.4 s by default) is the pause between requests to one site.
CRAWL_DELAY = 0.0
# At most this many search pages per retailer in one run: about 50 search words with a few pages each. Pages are read one per word
# in turn (page 1 of every word, then page 2 ...), so when the cap is reached it cuts the deep pages of a word, not the last words.
MAX_REQUESTS = 150
QUERY_LENGTH = 50     # the search box takes at most 50 characters

# key -> address, company shown, postings per search page, the `locationsearch=` the site understands for the United States.
# Adidas and Under Armour list the whole world, Sephora the United States and Canada; Tractor Supply is US only.
SITES = {
    "tractorsupply": {"base": "https://www.tractorsupply.careers", "company": "Tractor Supply Company", "page": 25, "where": "", "us_only": False},
    "sephora": {"base": "https://jobs.sephora.com", "company": "Sephora", "page": 25, "where": "US", "us_only": True},
    "adidas": {"base": "https://jobs.adidas-group.com", "company": "adidas", "page": 50, "where": "United States", "us_only": True},
    "underarmour": {"base": "https://careers.underarmour.com", "company": "Under Armour", "page": 10, "where": "United States", "us_only": True},
}
LABELS = {"tractorsupply": "Tractor Supply", "sephora": "Sephora", "adidas": "Adidas", "underarmour": "Under Armour"}


def _text(markup: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", markup))).strip()


def _date(text: str) -> str | None:
    """'Oct 27, 2026' (the search page and the Adidas job page) or 'Sun Oct 18 07:00:00 UTC 2026' (the job page's datePosted)."""
    m = re.search(r"\b([A-Z][a-z]{2}) (\d{1,2}),? (?:[\d:]+ [A-Z]+ )?(\d{4})\b", text or "")
    try:
        return datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%b %d %Y").date().isoformat() if m else None
    except ValueError:
        return None


def sf_place(text: str) -> tuple[str, bool]:
    """('Raceland, LA, United States', True) from 'Raceland, LA, US, 70394'. The site writes city, region, country code and a postal
    code (sometimes a requisition number); the postal code is dropped. A place outside the US keeps its country code, and a posting
    whose city is 'Remote' (Sephora also writes it as the region, or cut to 'Remo') gets ' (Remote)' in place of them.
    The flag is true when the country is the US."""
    parts = [p.strip() for p in re.sub(r"\s+", " ", html.unescape(text or "")).split(",") if p.strip()]
    if len(parts) > 1 and not re.fullmatch(r"[A-Z]{2}", parts[-1]):
        parts.pop()
    us = len(parts) > 1 and parts[-1] == "US" or parts == ["US"]
    remote = bool(parts) and parts[0].lower() == "remote"
    if remote:
        parts = [p for p in parts[1:] if not (len(p) >= 4 and "remote".startswith(p.lower()))]
    if us:
        parts[-1] = "United States"
    return ", ".join(parts) + (" (Remote)" if remote else ""), us


def _total(page: str) -> int | None:
    m = re.search(r"Results\s*<b>[^<]*</b>\s*of\s*<b>(\d+)</b>|Showing\s+\d+\s+to\s+\d+\s+of\s+(\d+)", page)
    return int(m.group(1) or m.group(2)) if m else None


def parse_search_page(page: str, key: str) -> tuple[list[Job], int, int | None]:
    """One page of a site's search: (the postings, how many postings the page listed, the total the page states). The page lists
    postings as table rows (Tractor Supply, Sephora, Adidas) or as tiles (Under Armour). A posting with no id or title is skipped,
    and with the US-only sites so is one outside the US. A page with no postings must say there are none, or its layout is not
    the one expected (a search page drawn by script, a block page): that raises."""
    site = SITES[key]
    cards = re.findall(r'<tr class="data-row[^"]*"[^>]*>(.*?)</tr>', page, re.S) or re.findall(r'<li class="job-tile[^"]*"[^>]*>(.*?)</li>', page, re.S)
    if not cards:
        if re.search(r'id="noresults"|There are currently no open positions matching', page):
            return [], 0, 0
        raise ValueError("search page layout not recognized")
    jobs: dict[str, Job] = {}
    for card in cards:
        link = next((m for m in re.finditer(r"<a\b([^>]*)>(.*?)</a>", card, re.S) if "jobTitle-link" in m.group(1)), None)
        href = re.search(r'href="(/(?:[A-Za-z]+/)?job/[^"]+/(\d+)/)"', link.group(1)) if link else None
        title = _text(link.group(2)) if link else ""
        if not href or not title:
            continue
        where = re.search(r'colLocation[^>]*>\s*<span class="jobLocation">(.*?)</span>', card, re.S) \
            or re.search(r'-desktop-section-location-value">(.*?)</div>', card, re.S) \
            or re.search(r'<span class="jobLocation">\s*(.*?)\s*</span>', card, re.S)
        place, us = sf_place(_text(where.group(1)) if where else "")
        if site["us_only"] and not us:
            continue
        posted = re.search(r'<span class="jobDate">\s*([^<]*?)\s*</span>|-desktop-section-date-value">(.*?)</div>', card, re.S)
        url = site["base"] + href.group(1)
        jobs.setdefault(f"{key}:{href.group(2)}", Job(
            id=f"{key}:{href.group(2)}", source=key, company=site["company"], title=title, location=place, url=url,
            posted=_date(_text(posted.group(1) or posted.group(2))) if posted else None, detail={"api": url},
        ))
    return list(jobs.values()), len(cards), _total(page)


def fetch_successfactors_site(key: str, search_terms: list[str], pages: int = 20, delay: float = 0.4) -> list[Job]:
    """Search the retailer's site for each word, `pages` result pages at most per word and MAX_REQUESTS pages in all. The next page
    of a word starts after the postings already listed; a word is finished when a page comes up short or the stated total is reached."""
    site = SITES[key]
    where = f"&locationsearch={quote_plus(site['where'])}" if site["where"] else ""
    terms = list(dict.fromkeys(t[:QUERY_LENGTH].strip() for t in (str(t).lower() for t in search_terms) if t.strip()))
    live = dict.fromkeys(terms, 0)          # word -> the row its next page starts at
    seen: dict[str, Job] = {}
    asked = 0
    for _ in range(pages):
        for term in list(live):
            if asked >= MAX_REQUESTS:
                return list(seen.values())
            r = sources._session().get(f"{site['base']}/search/?q={quote_plus(term)}{where}&startrow={live[term]}", timeout=sources.TIMEOUT, allow_redirects=False)
            asked += 1
            _checked(r)
            got, listed, total = parse_search_page(r.text, key)
            for j in got:
                seen.setdefault(j.id, j)
            time.sleep(max(delay, CRAWL_DELAY))
            live[term] += listed
            if listed < site["page"] or (total is not None and live[term] >= total):
                del live[term]
        if not live:
            break
    return list(seen.values())


def _checked(r) -> None:
    """A redirect is not followed: the site sends a failed search to its /errorpage/, a path robots.txt blocks."""
    if 300 <= r.status_code < 400:
        raise ValueError(f"redirected to {(r.headers or {}).get('location', '')}")
    r.raise_for_status()


def _span(page: str, marker: str) -> str | None:
    """The inside of the <span> that starts with `marker`, up to its own closing tag (a posting's text holds spans of its own)."""
    start = page.find(marker)
    if start < 0:
        return None
    depth, begin = 1, start + len(marker)
    for m in re.finditer(r"<span\b|</span>", page[begin:]):
        depth += 1 if m.group(0) == "<span" else -1
        if depth == 0:
            return page[begin:begin + m.start()]
    return None


def fill_successfactors_retail_detail(job: Job) -> None:
    """The whole posting from its job page: the text, the real title, the place, the posting date and any yearly pay the text states.
    The date and place are in schema.org meta tags (Tractor Supply, Sephora, Under Armour) or in labelled fields (Adidas)."""
    r = sources._session().get(job.url, timeout=sources.TIMEOUT, allow_redirects=False)
    _checked(r)
    page = r.text
    body = _span(page, '<span class="jobdescription">')
    text = strip_html(body or "")
    if not text:
        raise ValueError("job page layout not recognized")
    job.description = text
    field = lambda meta, prop: html.unescape((re.search(rf'<meta itemprop="{meta}" content="([^"]*)"', page) or re.search(
        rf'data-careersite-propertyid="{prop}"[^>]*>([^<]*)<', page) or [None, ""])[1]).strip()
    title = re.search(r'itemprop="title"[^>]*>([^<]*)<', page)
    job.title = _text(title.group(1)) if title and _text(title.group(1)) else job.title
    job.posted = _date(field("datePosted", "date")) or job.posted
    city, region, country = field("addressLocality", "city"), field("addressRegion", "state"), field("addressCountry", "country")
    if city:
        job.location = sf_place(", ".join(x for x in (city, region, country) if x))[0]
    job.pay_min, job.pay_max = extract_pay(text)


def _reader(key: str):
    def fetch(search_terms: list[str], pages: int = 20, delay: float = 0.4) -> list[Job]:
        return fetch_successfactors_site(key, search_terms, pages, delay)
    fetch.__name__ = f"fetch_{key}"
    return fetch


READERS = {key: (LABELS[key], _reader(key)) for key in SITES}
for _key in SITES:
    sources.DETAIL_FILLERS[_key] = fill_successfactors_retail_detail
