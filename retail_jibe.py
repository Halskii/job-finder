"""Retailer career sites on iCIMS Jibe: Sprouts, Ulta Beauty, JCPenney (Catalyst Brands), PetSmart, Costco, H-E-B, Foot Locker, Sheetz.

A Jibe career site's own search page calls `GET <site>/api/jobs?keywords=<words>&page=<n>&limit=<n>` and gets JSON back, so that is
the only request made here. The answer holds each posting's full text, so a job is stored with its description and no `detail`
(nothing is read later). The iCIMS hosts these sites send people to when they apply (`*.icims.com`) say `Disallow: /` in their
robots.txt and are never called. Every site's robots.txt allows everything and asks for `Crawl-delay: 5`, which is honoured.

The search is loose (a word is looked for in the whole posting, not only the title) and a retailer lists thousands of store jobs,
so a word like "cashier" has far more hits than are read. The caller filters by title afterwards.
"""
from __future__ import annotations

import re
import time
from urllib.parse import quote

import sources
from sources import Job, extract_pay, strip_html

CRAWL_DELAY = 5.0     # seconds between requests to one site: every site's robots.txt says `crawl-delay: 5`
PAGE_SIZE = 100       # the most the API takes in one answer (limit=500 is refused with HTTP 422)
MAX_REQUESTS = 60     # for one site in one run, whatever the number of search words: at the crawl delay that is five minutes

# key -> (company shown, site, job page path). The job page is the one a person opens: Ulta and JCPenney send /jobs/<id> on to the
# path given here, the others serve /jobs/<id> itself.
JIBE_SITES = {
    "sprouts": ("Sprouts Farmers Market", "https://jobs.sprouts.com", "/jobs/{}"),          # its iCIMS hosts are off limits; only this one is used
    "ulta": ("Ulta Beauty", "https://careers.ulta.com", "/careers/jobs/{}"),
    "jcpenney": ("JCPenney", "https://jobs.jcp.com", "/careers-home/jobs/{}"),              # also Aeropostale, Brooks Brothers, Eddie Bauer, ... (`brand`)
    "petsmart": ("PetSmart", "https://careers.petsmart.com", "/jobs/{}"),
    "costco": ("Costco", "https://careers.costco.com", "/jobs/{}"),                         # costcocareers.com is a parked domain, not the job site
    "heb": ("H-E-B", "https://careers.heb.com", "/jobs/{}"),                                # also Central Market and Joe V's Smart Shop (`brand`)
    "footlocker": ("Foot Locker", "https://careers.footlocker.com", "/jobs/{}"),
    "sheetz": ("Sheetz", "https://jobs.sheetz.com", "/jobs/{}"),
}


def _letters(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _places(d: dict) -> str:
    """'Charlotte, North Carolina, United States', every place of a posting open in several ('; '), and ' (Remote)' when the
    posting's place or title says so. A site shows 'City, State' for its own country only, so the United States is added."""
    full = str(d.get("full_location") or "").strip() or ", ".join(
        str(d.get(k)) for k in ("city", "state", "country") if d.get(k))
    us = str(d.get("country_code") or "").upper() == "US"
    places: list[str] = []
    for place in (p.strip() for p in full.split(";")):
        if place and us and not re.search(r"united states|\busa\b|\bu\.s\.", place, re.I):
            place += ", United States"
        if place and place not in places:
            places.append(place)
    text = "; ".join(places)
    where = " ".join(str(d.get(k) or "") for k in ("location_name", "full_location"))
    if text and (re.search(r"\b(remote|virtual)\b", where, re.I) or re.search(r"\bremote\b", str(d.get("title") or ""), re.I)):
        text += " (Remote)"
    return text


def _text(d: dict) -> str:
    """The posting's text. Some sites keep the qualifications and duties apart from the description and some repeat them inside it."""
    parts: list[str] = []
    for key in ("description", "qualifications", "responsibilities"):
        part = strip_html(str(d.get(key) or ""))
        flat = re.sub(r"\s+", " ", part)
        if part and not any(flat in re.sub(r"\s+", " ", p) for p in parts):
            parts.append(part)
    return "\n\n".join(parts)


def _number(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pay(d: dict, text: str) -> tuple[int | float | None, int | float | None, str]:
    """(low, high, unit). PetSmart gives its pay as numbers with a frequency (a few store managers are marked HOURLY with a
    yearly figure, which is ignored); the others state it in the text."""
    lo, hi, freq = _number(d.get("salary_min_value")), _number(d.get("salary_max_value")), str(d.get("salary_frequency") or "").upper()
    if lo and hi and lo <= hi:
        if freq == "YEARLY" and 20_000 <= lo and hi <= 1_000_000:
            return int(lo), int(hi), "year"
        if freq == "HOURLY" and 7 <= lo and hi <= 500:
            return (int(lo) if lo == int(lo) else lo), (int(hi) if hi == int(hi) else hi), "hour"
    lo, hi = extract_pay(text)
    return lo, hi, "year"


def parse_jibe_results(data: dict, source: str) -> list[Job]:
    """One answer of a Jibe site's `/api/jobs`. Raises ValueError when it is not one (the site changed or turned the request away)."""
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        raise ValueError("not a Jibe job list")
    company, base, page = JIBE_SITES[source]
    jobs = []
    for item in data["jobs"]:
        d = item.get("data") if isinstance(item, dict) else None
        d = d if isinstance(d, dict) else {}
        jid, title = str(d.get("req_id") or d.get("slug") or "").strip(), str(d.get("title") or "").strip()
        if not jid or not title:
            continue
        brand = str(d.get("brand") or "").strip()
        text = _text(d)
        lo, hi, unit = _pay(d, text)
        posted = re.match(r"\d{4}-\d\d-\d\d", str(d.get("posted_date") or d.get("create_date") or ""))
        jobs.append(Job(
            id=f"{source}:{jid}", source=source, company=brand if brand and _letters(brand) != _letters(company) else company,
            title=title, location=_places(d), url=base + page.format(quote(str(d.get("slug") or jid), safe="")),
            posted=posted.group(0) if posted else None, description=text, pay_min=lo, pay_max=hi, pay_unit=unit,
        ))
    return jobs


def _plan(search_terms: list[str]) -> list[tuple[str, list[str]]]:
    """The distinct search words with their words, the shortest first: a longer phrase is a narrower search, so when it comes up
    after a shorter one that was read to the end, it adds nothing (see fetch_jibe). The order is otherwise kept."""
    seen, out = set(), []
    for term in search_terms:
        words = re.findall(r"[a-z0-9+#]+", str(term).lower())
        if words and tuple(words) not in seen:
            seen.add(tuple(words))
            out.append((str(term).strip(), words))
    return sorted(out, key=lambda t: len(t[1]))


def _inside(short: list[str], long: list[str]) -> bool:
    return any(long[i:i + len(short)] == short for i in range(len(long) - len(short) + 1))


def fetch_jibe(source: str, search_terms: list[str], pages: int = 20, delay: float = 0.4) -> list[Job]:
    """Search each word and read its result pages, `PAGE_SIZE` postings to a page, at most `MAX_REQUESTS` requests for the site.

    The first page of every word is read first, so the budget goes to many words rather than to the depth of one. A word whose
    first page did not hold every hit then gets more pages, one word after another, while the budget lasts. A word that contains
    a shorter, fully read word ("senior software engineer" after "software engineer") is skipped: the site only matches postings
    that hold all its words, so it would find nothing new. The first request failing raises (the site changed, or turned the
    request away); a later failure ends the run with what was read.
    """
    base = JIBE_SITES[source][1]
    wait = max(delay, CRAWL_DELAY)
    seen: dict[str, Job] = {}
    made = 0

    def read(term: str, page: int) -> tuple[int, int]:
        """(postings on the page, hits in all). Waits out the crawl delay before every request but the first."""
        nonlocal made
        if made:
            time.sleep(wait)
        made += 1
        r = sources._session().get(f"{base}/api/jobs?keywords={quote(term)}&page={page}&limit={PAGE_SIZE}", timeout=sources.TIMEOUT,
                                   allow_redirects=False)
        if 300 <= r.status_code < 400:
            raise ValueError(f"{base} redirected the job search to {r.headers.get('location') or 'somewhere else'}")
        r.raise_for_status()
        try:
            data = r.json()
        except ValueError:
            raise ValueError(f"{base} did not answer with JSON (a bot check, or the site changed)") from None
        found = parse_jibe_results(data, source)
        for j in found:
            seen.setdefault(j.id, j)
        return len(data["jobs"]), int(data.get("totalCount") or 0)

    finished: list[list[str]] = []       # the words of every search read to the end
    more: dict[str, list[int]] = {}      # search -> [next page, postings read so far, hits in all]
    try:
        for term, words in _plan(search_terms):
            if made >= MAX_REQUESTS:
                break
            if any(_inside(short, words) for short in finished):
                continue
            got, total = read(term, 1)
            if got < PAGE_SIZE or got >= total:
                finished.append(words)
            elif pages > 1:
                more[term] = [2, got, total]
        while more and made < MAX_REQUESTS:
            for term in list(more):
                if made >= MAX_REQUESTS:
                    break
                page, read_so_far, total = more[term]
                got, total = read(term, page)
                more[term] = [page + 1, read_so_far + got, total]
                if got < PAGE_SIZE or read_so_far + got >= total or page >= pages:
                    del more[term]
    except Exception:
        if not seen:
            raise
    return list(seen.values())


def _fetcher(source: str):
    def fetch(search_terms: list[str], pages: int = 20, delay: float = 0.4) -> list[Job]:
        return fetch_jibe(source, search_terms, pages, delay)
    fetch.__name__ = f"fetch_{source}"
    return fetch


# key -> (company label, fetch function): picked up by retail_sources. The full text comes with the list, so there is no
# sources.DETAIL_FILLERS entry.
READERS = {key: (site[0], _fetcher(key)) for key, site in JIBE_SITES.items()}
