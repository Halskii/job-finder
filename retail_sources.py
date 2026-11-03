"""Retailer career sites. Each retailer posts on its own career site, so each is a small reader listed in RETAIL_SOURCES and
switched on and off under `retailers:` in config.yaml.

- Phenom career sites: Lowe's (talent.lowes.com), CVS Health, TJX (T.J. Maxx, Marshalls, HomeGoods), Dollar Tree and Kohl's. Their
  robots.txt files allow the search and job pages (they block only the apply, cart, chatbot and tracking paths) and list a
  sitemap. A Phenom page carries the data it shows as one JSON object in the page (`phApp.ddo`), so the search page lists the
  jobs and the job page holds the whole posting. Lowe's also keeps a Workday site, but its robots.txt there blocks the site's
  path, so that one is left alone.
- Home Depot (careers.homedepot.com) has no search to call, but its robots.txt allows everything except two image paths and
  lists a job sitemap. The sitemap has every posting with its title in the address, so the addresses holding a search word are
  picked and only those job pages are read (each carries the posting as schema.org JobPosting data). Home Depot's Workday site
  is blocked in its robots.txt, like Lowe's.

A retailer lists thousands of store jobs (cashiers, stockers) next to its corporate and technology roles, so the shared fetch
searches with the same title words as Workday and reads in full only what matches.
"""
from __future__ import annotations

import json
import re
import time
from urllib.parse import quote

import sources
from sources import Job, extract_pay, strip_html

# source -> (career site address, company, id prefix). The address is the site's search page without `/search-results`.
PHENOM_SITES = {
    "lowes": ("https://talent.lowes.com/us/en", "Lowe's", "lowes"),
    "cvs": ("https://jobs.cvshealth.com/us/en", "CVS Health", "cvs"),
    "tjx": ("https://jobs.tjx.com/global/en", "TJX", "tjx"),
    "dollartree": ("https://careers.dollartree.com/us/en", "Dollar Tree", "dollartree"),
    "kohls": ("https://careers.kohls.com", "Kohl's", "kohls"),
    "familydollar": ("https://careers.familydollar.com/us/en", "Family Dollar", "familydollar"),
    "fivebelow": ("https://careers.fivebelow.com/us/en", "Five Below", "fivebelow"),
    "gianteagle": ("https://jobs.gianteagle.com/us/en", "Giant Eagle", "gianteagle"),
    "oreilly": ("https://careers.oreillyauto.com/us/en", "O'Reilly Auto Parts", "oreilly"),
    "advanceauto": ("https://jobs.advanceautoparts.com/us/en", "Advance Auto Parts", "advanceauto"),
    "chewy": ("https://careers.chewy.com/us/en", "Chewy", "chewy"),
    "flooranddecor": ("https://careers.flooranddecor.com/us/en", "Floor & Decor", "flooranddecor"),
    "academy": ("https://careers.academy.com/us/en", "Academy Sports + Outdoors", "academy"),
    "basspro": ("https://careers.basspro.com/us/en", "Bass Pro Shops", "basspro"),      # also Cabela's
    "michaels": ("https://careers.michaels.com/us/en", "Michaels", "michaels"),
    "wawa": ("https://careers.wawa.com/us/en", "Wawa", "wawa"),
    "circlek": ("https://workwithus.circlek.com/global/en", "Circle K", "circlek"),    # worldwide: the location settings drop the rest
}
# (config key, label for the run log, name of the fetch function here): one line per retailer.
RETAIL_SOURCES = [("homedepot", "Home Depot", "fetch_homedepot"),
                  *((key, site[1], f"fetch_{key}") for key, site in PHENOM_SITES.items())]
# Each Phenom retailer gets a fetch_<key>(search_terms, pages, delay) function of its own, so a test or a caller can name it.
for _key in PHENOM_SITES:
    globals()[f"fetch_{_key}"] = (lambda key: lambda search_terms, pages=8, delay=0.4: fetch_phenom(key, search_terms, pages, delay))(_key)


def phenom_data(page: str) -> dict:
    """The JSON object a Phenom page sets as `phApp.ddo = {...}`. It holds the search results on a search page and the posting
    on a job page. Raises ValueError when the page has none (the site changed, or the request was turned away)."""
    found = re.search(r"phApp\.ddo\s*=\s*(?=\{)", page)
    if not found:
        raise ValueError("no Phenom data on the page")
    return json.JSONDecoder().raw_decode(page, found.end())[0]


def _phenom_search(data: dict) -> dict:
    return (data or {}).get("eagerLoadRefineSearch") or {}


def _phenom_places(job: dict) -> str:
    """'Toledo, Ohio, United States', or all of them ('; ') for a posting open in several places. A site that gives only an
    address line (Kohl's) keeps that line."""
    entries = job.get("multi_location_array") or job.get("multi_location") or []
    places = [e for e in entries if isinstance(e, dict)] if job.get("isMultiLocation") or len(entries) > 1 else []
    out = []
    for e in places or [job]:
        city_state = e.get("cityState") or ", ".join(x for x in (e.get("city"), e.get("state")) if x) or str(e.get("location") or "")
        country = re.sub(r"^United States of America$", "United States", str(e.get("country") or ""))
        text = ", ".join(x for x in (city_state, country) if x)
        if text and text not in out:
            out.append(text)
    return "; ".join(out)


def _phenom_slug(title: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", title).strip("-")


def parse_phenom_results(data: dict, source: str) -> list[Job]:
    """One page of a Phenom career site's search. The list holds only a short teaser, so a job is stored without text and with
    `detail` set: the web app reads the full posting the first time someone's search selects it (see ingest.ensure_details)."""
    base, company, prefix = PHENOM_SITES[source]
    jobs = []
    for it in (_phenom_search(data).get("data") or {}).get("jobs") or []:
        jid, title = str(it.get("jobId") or it.get("reqId") or ""), str(it.get("title") or "").strip()
        if not jid or not title:
            continue
        url = f"{base}/job/{jid}/{_phenom_slug(title)}"
        jobs.append(Job(
            id=f"{prefix}:{jid}", source=source, company=company, title=title, location=_phenom_places(it), url=url,
            posted=str(it.get("postedDate") or "")[:10] or None, detail={"api": url},
        ))
    return jobs


def fetch_phenom(source: str, search_terms: list[str], pages: int = 8, delay: float = 0.4) -> list[Job]:
    """Search for each word and read its result pages (ten jobs on most sites, 25 on some; the next page starts after however many
    the last one held). The search is the site's own: the page is asked for `keywords=` and no `s=` (with `s=1` the page
    ignores the words and lists every job)."""
    base = PHENOM_SITES[source][0]
    seen: dict[str, Job] = {}
    for term in search_terms:
        offset = 0
        for _ in range(pages):
            r = sources._session().get(f"{base}/search-results?keywords={quote(term)}&from={offset}", timeout=sources.TIMEOUT)
            r.raise_for_status()
            data = phenom_data(r.text)
            for j in parse_phenom_results(data, source):
                seen.setdefault(j.id, j)
            time.sleep(delay)
            held = len((_phenom_search(data).get("data") or {}).get("jobs") or [])
            offset += held
            if not held or offset >= int(_phenom_search(data).get("totalHits") or 0):
                break
    return list(seen.values())


def fill_phenom_detail(job: Job) -> None:
    """The whole posting from its job page: the text, the real places, the posting date and any pay the page states."""
    r = sources._session().get(job.url, timeout=sources.TIMEOUT)
    r.raise_for_status()
    info = ((phenom_data(r.text).get("jobDetail") or {}).get("data") or {}).get("job") or {}
    text = strip_html(str(info.get("description") or ""))
    if not text:
        raise ValueError("job page layout not recognized")
    pay = str(info.get("payInfo") or "").strip()      # Lowe's: "Pay Range: $75,300.00 - $143,100.00 annually", kept apart from the text
    if pay and pay not in text:
        text += f"\n\n{pay}"
    job.description = text
    job.posted = str(info.get("postedDate") or "")[:10] or job.posted
    job.location = _phenom_places(info) or job.location
    job.pay_min, job.pay_max = extract_pay(text)


for _name in PHENOM_SITES:
    sources.DETAIL_FILLERS[_name] = fill_phenom_detail


# ----------------------------------------------------------------------------- Home Depot
HD_BASE = "https://careers.homedepot.com"
HD_JOB = re.compile(r"<loc>https://careers\.homedepot\.com/job/(\d+)/([^/<]+)/?</loc>")


def parse_homedepot_sitemap(xml: str, search_terms: list[str]) -> list[Job]:
    """The job sitemap lists every posting (about 25,000, mostly store jobs) with its title in the address:
    /job/24077598/software-engineer-ii-remote-remote/. Only the addresses holding one of the search words are kept, so
    only those job pages are read. The title here is the address read back; the job page gives the real one."""
    patterns = [re.compile(r"(?:^|-)" + re.escape(w) + r"(?:-|$)") for w in (re.sub(r"[^a-z0-9]+", "-", str(t).lower()).strip("-") for t in search_terms) if w]
    return [Job(id=f"homedepot:{jid}", source="homedepot", company="The Home Depot", title=slug.replace("-", " ").title(), location="",
                url=f"{HD_BASE}/job/{jid}/{slug}/", detail={"api": f"{HD_BASE}/job/{jid}/{slug}/"})
            for jid, slug in HD_JOB.findall(xml) if any(p.search(slug) for p in patterns)]


def fetch_homedepot(search_terms: list[str], pages: int = 1, delay: float = 0.4) -> list[Job]:
    r = sources._session().get(f"{HD_BASE}/jobsitemap", timeout=60)
    r.raise_for_status()
    return parse_homedepot_sitemap(r.text, search_terms)


def _job_posting(page: str) -> dict:
    """The schema.org JobPosting object among a page's <script type="application/ld+json"> blocks, or {}."""
    for block in re.findall(r"<script[^>]*type=.application/ld\+json.[^>]*>(.*?)</script>", page, re.S):
        try:
            data = json.loads(block, strict=False)
        except ValueError:
            continue
        for item in (data if isinstance(data, list) else data.get("@graph") or [data]) if isinstance(data, (list, dict)) else []:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return item
    return {}


def fill_homedepot_detail(job: Job) -> None:
    """The job page's JobPosting data: the real title, the text, the posting date and the place. A remote job has the street
    address 'VIRTUAL' (its title also says Remote)."""
    r = sources._session().get(job.url, timeout=sources.TIMEOUT)
    r.raise_for_status()
    posting = _job_posting(r.text)
    text = strip_html(str(posting.get("description") or ""))
    if not text:
        raise ValueError("job page layout not recognized")
    job.title = str(posting.get("title") or "").strip() or job.title
    job.description = text
    job.posted = str(posting.get("datePosted") or "")[:10] or job.posted
    where = posting.get("jobLocation")
    places = []
    for place in where if isinstance(where, list) else [where]:
        address = (place or {}).get("address") or {}
        line = ", ".join(x for x in (address.get("addressLocality"), address.get("addressRegion")) if x)
        if line and str(address.get("streetAddress") or "").strip().upper() == "VIRTUAL":
            line += " (Remote)"
        if line and line not in places:
            places.append(line)
    job.location = "; ".join(places) or job.location
    job.pay_min, job.pay_max = extract_pay(text)


sources.DETAIL_FILLERS["homedepot"] = fill_homedepot_detail


# ----------------------------------------------------------------------------- reader modules
# Retailers on other systems have a module of their own (retail_oracle.py, retail_jibe.py, ...). Each lists its retailers in
# `READERS = {key: (company label, fetch function)}`, registers its job page readers in sources.DETAIL_FILLERS, and is picked up here
# when the file exists. A module that is missing is skipped; one that is broken raises, so a mistake is not hidden.
READER_MODULES = ("retail_oracle", "retail_jibe", "retail_sitemap", "retail_successfactors", "retail_misc")


def _load_reader_modules() -> None:
    import importlib
    for name in READER_MODULES:
        try:
            module = importlib.import_module(name)
        except ModuleNotFoundError as e:
            if e.name != name:
                raise
            continue
        for key, (label, fetch) in module.READERS.items():
            if any(key == k for k, *_ in RETAIL_SOURCES):
                raise ValueError(f"{name}: retailer key '{key}' is already taken")
            RETAIL_SOURCES.append((key, label, f"fetch_{key}"))
            globals()[f"fetch_{key}"] = fetch


_load_reader_modules()
