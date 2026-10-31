"""Bulk board discovery. Common Crawl publishes a free, public index of the web's URLs. Every company that hosts its jobs
on Greenhouse, Lever, Workday and the like has page addresses such as boards.greenhouse.io/<company>/jobs/123, so listing
those addresses gives a long list of company boards without guessing names.

Each candidate is then checked against the platform's own public job API, and only boards that currently list a posting
matching your titles are kept (or every live board, with keep_all). Nothing here touches linkedin.com, indeed.com or any
site that forbids bots."""
from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import requests

import discovery
import filters
import sources

INDEX = "https://index.commoncrawl.org"
CACHE_FILE = "crawl_cache.json"
UA = {"User-Agent": "job-finder/0.1 (personal job search tool; github.com/Halskii/job-finder)"}

# platform -> URL patterns in Common Crawl's index syntax ("host/*" = every page on the host, "*.host" = every subdomain)
PATTERNS = {
    "greenhouse": ["boards.greenhouse.io/*", "job-boards.greenhouse.io/*"],
    "lever": ["jobs.lever.co/*"],
    "ashby": ["jobs.ashbyhq.com/*"],
    "smartrecruiters": ["jobs.smartrecruiters.com/*", "careers.smartrecruiters.com/*"],
    "workable": ["apply.workable.com/*"],
    "recruitee": ["*.recruitee.com"],
    "bamboohr": ["*.bamboohr.com"],
    "rippling": ["ats.rippling.com/*"],
    "gem": ["jobs.gem.com/*"],
    "personio": ["*.jobs.personio.de", "*.jobs.personio.com"],
    "workday": ["*.myworkdayjobs.com"],
}
# first path segments / subdomains that are part of the platform itself, not a company
NOT_A_COMPANY = {"", "www", "api", "app", "apply", "careers", "jobs", "embed", "v1", "static", "assets", "images", "img", "cdn",
                 "help", "support", "blog", "login", "signin", "robots.txt", "favicon.ico", "sitemap.xml", "about", "pricing",
                 "documentation", "docs", "status", "sandbox", "demo", "test", "staging", "j", "o", "oauth", "auth", "public"}
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")


# ----------------------------------------------------------------------------- turning addresses into board names
def _first_segment(url: str) -> str | None:
    parts = [p for p in urlparse(url).path.split("/") if p]
    return unquote(parts[0]) if parts else None


def _subdomain(url: str, suffixes: tuple[str, ...]) -> str | None:
    host = (urlparse(url).hostname or "").lower()
    for suf in suffixes:
        if host.endswith(suf) and host != suf.lstrip("."):
            return host[: -len(suf)].split(".")[-1]
    return None


def candidate_from_url(system: str, url: str):
    """The company board an address belongs to: a slug string, a Workday entry dict, or None for pages that are not a board."""
    u = urlparse(url)
    host = (u.hostname or "").lower()
    cand = None
    if system == "greenhouse":
        if host.endswith("greenhouse.io"):
            seg = _first_segment(url)
            if seg == "embed":
                cand = (parse_qs(u.query).get("for") or [None])[0]
            elif host.startswith(("boards.", "job-boards.")):
                cand = seg
    elif system in ("lever", "ashby", "workable", "rippling", "gem"):
        cand = _first_segment(url)
    elif system == "smartrecruiters":
        cand = _first_segment(url)    # case matters on SmartRecruiters
    elif system == "recruitee":
        cand = _subdomain(url, (".recruitee.com",))
    elif system == "bamboohr":
        if u.path.startswith("/careers") or u.path.startswith("/jobs"):
            cand = _subdomain(url, (".bamboohr.com",))
    elif system == "personio":
        cand = _subdomain(url, (".jobs.personio.de", ".jobs.personio.com"))
    elif system == "workday":
        try:
            return discovery.parse_workday_url(url)
        except ValueError:
            return None
    if not cand:
        return None
    if system != "smartrecruiters":
        cand = cand.lower() if system in ("recruitee", "bamboohr", "personio") else cand
    if cand.lower() in NOT_A_COMPANY or not SLUG_RE.match(cand):
        return None
    return cand


# ----------------------------------------------------------------------------- reading Common Crawl's index
class IndexUnavailable(Exception):
    """Common Crawl's index did not answer (it is a shared server and sometimes times out)."""


def crawl_ids(n: int, tries: int = 4) -> list[str]:
    """Names of the newest n crawls. Raises IndexUnavailable when the server does not answer."""
    for attempt in range(tries):
        try:
            r = requests.get(f"{INDEX}/collinfo.json", headers=UA, timeout=30)
            if r.status_code == 200:
                return [c["id"] for c in r.json()[:n]]
        except (requests.RequestException, ValueError):
            pass
        time.sleep(3 * (attempt + 1))
    raise IndexUnavailable("the list of crawls did not load")


def _cdx(crawl: str, params: dict, tries: int = 4):
    """One request to the index, retried with a growing pause. None means the crawl has nothing for the pattern (404);
    IndexUnavailable means the server did not answer, which must never be mistaken for 'nothing there'."""
    for attempt in range(tries):
        try:
            r = requests.get(f"{INDEX}/{crawl}-index", params=params, headers=UA, timeout=120)
            if r.status_code == 200:
                return r
            if r.status_code == 404:
                return None
        except requests.RequestException:
            pass
        time.sleep(3 * (attempt + 1))
    raise IndexUnavailable(f"{crawl} did not answer after {tries} tries")


def iter_urls(crawl: str, pattern: str, pause: float = 2.0):
    """Every 200-status address matching the pattern in one crawl, page by page."""
    base = {"url": pattern, "fl": "url", "filter": "status:200", "output": "json"}
    head = _cdx(crawl, {**base, "showNumPages": "true"})
    if head is None:   # no captures of this pattern in this crawl
        return
    try:
        pages = int(json.loads(head.text.strip().splitlines()[0]).get("pages", 0))
    except (ValueError, IndexError):
        return
    for page in range(pages):
        r = _cdx(crawl, {**base, "page": page})
        time.sleep(pause)
        if r is None:
            continue
        for line in r.text.splitlines():
            try:
                yield json.loads(line)["url"]
            except (ValueError, KeyError, TypeError):
                continue


def resolve_ids(root: Path, crawls: int, log=print) -> list[str] | None:
    """The newest crawls' names, remembered in the cache. When the list cannot be loaded, the remembered one is used;
    None when there is neither."""
    cache = load_cache(root)
    try:
        ids = crawl_ids(crawls)
        cache["ids"] = ids
        save_cache(root, cache)
        return ids
    except IndexUnavailable:
        ids = cache.get("ids", [])[:crawls]
        return ids or None


def load_cache(root: Path) -> dict:
    p = Path(root) / CACHE_FILE
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            pass
    return {"done": {}, "found": {}}


def save_cache(root: Path, cache: dict) -> None:
    (Path(root) / CACHE_FILE).write_text(json.dumps(cache), encoding="utf-8")


def enumerate_candidates(root: Path, systems: list[str], crawls: int, log=print, pause: float = 2.0,
                         ids: list[str] | None = None, problems: list | None = None) -> dict[str, list]:
    """Candidate boards for each platform, from the last `crawls` crawls. Crawls already read are remembered in
    crawl_cache.json, so running again only reads newer ones."""
    cache = load_cache(root)
    if ids is None:
        ids = resolve_ids(root, crawls, log) or []
    cache = load_cache(root)
    for system in systems:
        found = cache["found"].setdefault(system, [])
        seen = {json.dumps(x, sort_keys=True) if isinstance(x, dict) else x for x in found}
        for crawl in ids:
            key = f"{system}|{crawl}"
            if cache["done"].get(key):
                continue
            before = len(seen)
            try:
                for pattern in PATTERNS[system]:
                    for url in iter_urls(crawl, pattern, pause):
                        cand = candidate_from_url(system, url)
                        if cand is None:
                            continue
                        k = json.dumps(cand, sort_keys=True) if isinstance(cand, dict) else cand
                        if k not in seen:
                            seen.add(k)
                            found.append(cand)
            except IndexUnavailable as e:
                save_cache(root, cache)   # keep what was read; this crawl is NOT marked done, so it is read again next time
                log(f"  {system:16s} {crawl}: the index is not answering ({e}); stopping this platform. "
                    f"Boards read so far are kept. Try again later.")
                if problems is not None:
                    problems.append(system)
                break
            cache["done"][key] = True
            save_cache(root, cache)
            log(f"  {system:16s} {crawl}: +{len(seen) - before} new ({len(seen)} total)")
    return {s: cache["found"].get(s, []) for s in systems}


# ----------------------------------------------------------------------------- checking candidates against the real API
def _light_greenhouse(slug: str) -> list:
    """Titles and locations only (no descriptions), so checking thousands of boards stays light."""
    data = sources._get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    return [sources.Job(id="", source="greenhouse", company=slug, title=j.get("title", ""),
                        location=(j.get("location") or {}).get("name", ""), url="") for j in data.get("jobs", [])]


def check(system: str, cand, cfg: dict):
    """(live postings, postings matching your titles) for one candidate board; raises when the board does not exist."""
    if system == "workday":
        terms = filters.workday_terms(cfg)
        jobs = sources.fetch_workday(cand, terms, 1, cfg.get("request_delay", 0.4))
    elif system == "greenhouse":
        jobs = _light_greenhouse(cand)
    else:
        jobs = dict(discovery.SYSTEMS)[system](cand)
    return len(jobs), len(discovery.matching(jobs, cfg))


def _key(cand) -> str:
    return json.dumps(cand, sort_keys=True) if isinstance(cand, dict) else str(cand).lower()


def known_keys(cfg_sources: dict, system: str) -> set[str]:
    """Boards already in your sources, in the same form as _key()."""
    have = (cfg_sources or {}).get(system, []) or []
    if system == "workday":
        return {_key({"host": e["host"], "tenant": e["tenant"], "site": e["site"]}) for e in have}
    return {_key(x) for x in have}


def _workday_id(cand: dict) -> str:
    return _key({"host": cand["host"], "tenant": cand["tenant"], "site": cand["site"]})


def validate(system: str, cands: list, cfg: dict, keep_all: bool = False, workers: int = 16, log=print,
             root: Path | None = None, recheck_days: float = 14.0, now: float | None = None) -> list:
    """Keep the candidates whose board exists and (unless keep_all) lists a posting matching your titles.
    With `root`, candidates checked within `recheck_days` are not asked again (the answer is remembered in the cache file)."""
    delay = cfg.get("request_delay", 0.4)
    now = time.time() if now is None else now
    cache = load_cache(root) if root else {"done": {}, "found": {}}
    checked = cache.setdefault("checked", {}).setdefault(system + ("|all" if keep_all else ""), {})
    ident = _workday_id if system == "workday" else _key
    todo = [c for c in cands if now - checked.get(ident(c), 0) > recheck_days * 86400]
    skipped = len(cands) - len(todo)

    def one(cand):
        try:
            live, matched = check(system, cand, cfg)
            return cand, live, matched
        except Exception:
            return cand, 0, 0
        finally:
            time.sleep(delay)

    kept, live_boards, done = [], 0, 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for cand, live, matched in pool.map(one, todo):
            done += 1
            live_boards += bool(live)
            good = bool(live and (keep_all or matched))
            if good:
                kept.append(cand)
            else:
                checked[ident(cand)] = now   # boards that failed are not asked again for a while
            if done % 250 == 0:
                log(f"    checked {done}/{len(todo)} {system} boards, {live_boards} live, {len(kept)} kept")
                if root:
                    save_cache(root, cache)
    if root:
        save_cache(root, cache)
    log(f"  {system:16s} checked {len(todo)} ({skipped} skipped, checked recently): {live_boards} live, {len(kept)} kept")
    return kept
