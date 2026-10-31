"""Aggregator feeds: sites that collect remote and tech jobs and publish them through a public API or RSS feed.

Each one asks for credit, and that is kept: the job's link goes to the aggregator's own page, and the job's `source` names
it (the viewer and reports show both). Each feed is fetched at most once per `min_hours` (see main.collect_feeds), so a
frequent `run` does not call them again and again. Every fetcher returns the same Job objects as sources.py."""
from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import sources
from sources import Job, _get, extract_pay, strip_html


def _day(value) -> str | None:
    """An ISO date from a Unix time in seconds, an ISO timestamp, or an RFC 822 date. None when unreadable."""
    if value in (None, "", 0, "0"):
        return None
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            return datetime.fromtimestamp(int(value), tz=timezone.utc).date().isoformat()
        text = str(value)
        if re.match(r"\d{4}-\d{2}-\d{2}", text):
            return text[:10]
        return parsedate_to_datetime(text).date().isoformat()
    except (ValueError, TypeError, OverflowError):
        return None


def _salary(lo, hi, period="annual") -> tuple[int | None, int | None]:
    """Yearly pay from a feed's min/max fields; 0, None and non-yearly periods give nothing."""
    try:
        lo, hi = int(float(lo or 0)), int(float(hi or 0))
    except (TypeError, ValueError):
        return None, None
    if not (lo or hi) or (period or "annual").lower() not in ("annual", "year", "yearly"):
        return None, None
    return (lo or None), (hi or None)


# ----------------------------------------------------------------------------- Remote OK
def fetch_remoteok() -> list[Job]:
    """remoteok.com/api: one request returns every current listing. Their terms ask for a link back to Remote OK and a
    mention of it as the source, so Job.url is the remoteok.com page."""
    data = sources._session().get("https://remoteok.com/api", timeout=sources.TIMEOUT)
    data.raise_for_status()
    jobs = []
    for j in data.json():
        if not isinstance(j, dict) or not j.get("id") or not j.get("position"):
            continue   # the first element is the legal notice
        desc = strip_html(j.get("description", ""))
        lo, hi = _salary(j.get("salary_min"), j.get("salary_max"))
        if lo is None and hi is None:
            lo, hi = extract_pay(desc)
        jobs.append(Job(
            id=f"ro:{j['id']}", source="remoteok", company=j.get("company", ""), title=j.get("position", ""),
            location=j.get("location") or "Remote", url=j.get("url") or j.get("apply_url") or "",
            posted=_day(j.get("epoch")) or _day(j.get("date")), description=desc, pay_min=lo, pay_max=hi,
        ))
    return jobs


# ----------------------------------------------------------------------------- We Work Remotely
def parse_wwr(xml_text: str) -> list[Job]:
    root = ET.fromstring(xml_text.encode("utf-8", errors="replace") if isinstance(xml_text, str) else xml_text)
    jobs = []
    for item in root.iter("item"):
        raw = (item.findtext("title") or "").strip()
        company, _, title = raw.partition(": ")
        if not title:
            company, title = "", raw
        link = (item.findtext("link") or item.findtext("guid") or "").strip()
        if not link:
            continue
        desc = strip_html(html.unescape(item.findtext("description") or ""))
        lo, hi = extract_pay(desc)
        jobs.append(Job(
            id=f"wwr:{link.rstrip('/').rsplit('/', 1)[-1]}", source="weworkremotely", company=company.strip(), title=title.strip(),
            location=(item.findtext("region") or "Remote").strip(), url=link, posted=_day(item.findtext("pubDate")),
            description=desc, pay_min=lo, pay_max=hi,
        ))
    return jobs


def fetch_weworkremotely(categories: list[str]) -> list[Job]:
    """RSS feeds from weworkremotely.com, one per category. Job links go to weworkremotely.com."""
    seen: dict[str, Job] = {}
    for cat in categories:
        r = sources._session().get(f"https://weworkremotely.com/categories/{cat}.rss", timeout=sources.TIMEOUT)
        r.raise_for_status()
        for j in parse_wwr(r.content.decode("utf-8", errors="replace")):
            seen.setdefault(j.id, j)
    return list(seen.values())


# ----------------------------------------------------------------------------- Jobicy
def fetch_jobicy(terms: list[str], count: int = 50) -> list[Job]:
    """Jobicy's remote-jobs API, US-eligible listings only, one search per term. They ask to be credited with a link to
    the source (kept: Job.url is the jobicy.com page) and to not be called more than about once an hour."""
    seen: dict[str, Job] = {}
    for term in terms or [""]:
        params = {"count": count, "geo": "usa"}
        if term:
            params["tag"] = term
        data = _get("https://jobicy.com/api/v2/remote-jobs", params=params)
        for j in data.get("jobs", []):
            desc = strip_html(j.get("jobDescription", ""))
            lo, hi = _salary(j.get("salaryMin") or j.get("annualSalaryMin"), j.get("salaryMax") or j.get("annualSalaryMax"),
                             "annual" if (j.get("salaryPeriod") or "yearly").lower() in ("yearly", "annual") else "other")
            if lo is None and hi is None:
                lo, hi = extract_pay(desc)
            seen.setdefault(str(j["id"]), Job(
                id=f"jb:{j['id']}", source="jobicy", company=j.get("companyName", ""), title=html.unescape(j.get("jobTitle", "")),
                location=(j.get("jobGeo") or "Remote").replace("  ", " "), url=j.get("url", ""), posted=_day(j.get("pubDate")),
                description=desc, pay_min=lo, pay_max=hi))
    return list(seen.values())


# ----------------------------------------------------------------------------- Working Nomads
def fetch_workingnomads() -> list[Job]:
    """workingnomads.com's public JSON of remote jobs (all categories in one request). Links go to workingnomads.com."""
    jobs = []
    for j in _get("https://www.workingnomads.com/api/exposed_jobs/"):
        url = j.get("url", "")
        if not url or not j.get("title"):
            continue
        desc = strip_html(j.get("description", ""))
        lo, hi = extract_pay(desc)
        jobs.append(Job(
            id=f"wn:{url.rstrip('/').rsplit('/', 1)[-1]}", source="workingnomads", company=j.get("company_name", ""), title=j["title"],
            location=j.get("location") or "Remote", url=url, posted=_day(j.get("pub_date")), description=desc, pay_min=lo, pay_max=hi))
    return jobs


# ----------------------------------------------------------------------------- Himalayas
def fetch_himalayas(terms: list[str], pages: int = 2) -> list[Job]:
    """Himalayas' search API (20 results a page), restricted to jobs open to US applicants. The full feed has over
    a hundred thousand postings, so it is searched by your title words instead. Links go to himalayas.app."""
    seen: dict[str, Job] = {}
    for term in terms:
        for page in range(1, pages + 1):
            data = _get("https://himalayas.app/jobs/api/search", params={"q": term, "country": "US", "page": page})
            batch = data.get("jobs", [])
            for j in batch:
                url = j.get("guid") or j.get("applicationLink") or ""
                if not url or url in seen:
                    continue
                desc = strip_html(j.get("description", ""))
                lo, hi = _salary(j.get("minSalary"), j.get("maxSalary"), j.get("salaryPeriod"))
                if lo is None and hi is None:
                    lo, hi = extract_pay(desc)
                places = j.get("locationRestrictions") or []
                seen[url] = Job(
                    id=f"hm:{url.rstrip('/').rsplit('/', 1)[-1]}:{j.get('companySlug', '')}", source="himalayas",
                    company=j.get("companyName", ""), title=j.get("title", ""),
                    location=", ".join(places) if places else "Remote", url=url, posted=_day(j.get("pubDate")),
                    description=desc, pay_min=lo, pay_max=hi)
            if len(batch) < 20:
                break
    return list(seen.values())


# ----------------------------------------------------------------------------- Hacker News "Who is hiring?"
HN = "https://hn.algolia.com/api/v1"


def parse_hn_comment(c: dict) -> Job | None:
    """One top-level comment of a 'Who is hiring?' thread. By long-standing convention the first line reads
    'Company | Role | Location | Remote ...', so that line is the title (and location check), the rest the description."""
    text = c.get("comment_text") or ""
    if not text:
        return None
    parts = [strip_html(p) for p in re.split(r"<p>|<br\s*/?>", text) if strip_html(p)]
    if not parts:
        return None
    header = parts[0][:240]
    company = re.split(r"\s*[|–—]\s*|\s+-\s+", header, maxsplit=1)[0]
    company = re.sub(r"\s*\(.*?\)|\s*https?://\S+", "", company).strip()[:80]   # "Acme (https://acme.com)" -> "Acme"
    if len(parts) < 2 and len(header) < 40:
        return None   # a reply like "thanks!" that slipped through
    desc = "\n".join(parts)
    lo, hi = extract_pay(desc)
    return Job(id=f"hn:{c['objectID']}", source="hackernews", company=company, title=header,
               location=header, url=f"https://news.ycombinator.com/item?id={c['objectID']}", posted=_day(c.get("created_at")),
               description=desc, pay_min=lo, pay_max=hi)


def fetch_hn_hiring(threads: int = 2, max_pages: int = 3) -> list[Job]:
    """The newest 'Ask HN: Who is hiring?' threads (posted monthly by the user whoishiring): every top-level comment is
    a job. Links go to the comment on news.ycombinator.com."""
    found = _get(f"{HN}/search_by_date", params={"tags": "story,author_whoishiring", "hitsPerPage": 10})
    stories = [h for h in found.get("hits", []) if "who is hiring" in (h.get("title") or "").lower()][:threads]
    jobs: list[Job] = []
    for story in stories:
        sid = str(story["objectID"])
        for page in range(max_pages):
            data = _get(f"{HN}/search_by_date", params={"tags": f"comment,story_{sid}", "hitsPerPage": 1000, "page": page})
            for c in data.get("hits", []):
                if str(c.get("parent_id")) != sid:
                    continue   # replies to a posting, not postings
                job = parse_hn_comment(c)
                if job:
                    jobs.append(job)
            if page + 1 >= int(data.get("nbPages", 1)):
                break
    return jobs


# ----------------------------------------------------------------------------- Adzuna (free key)
def fetch_adzuna(app_id: str, app_key: str, terms: list[str], max_age_days: int = 30, pages: int = 2, country: str = "us") -> list[Job]:
    """Adzuna's search API: a large US aggregator. It returns a short snippet of each description, not the whole text, and
    its terms ask that results link to Adzuna (kept: Job.url is Adzuna's redirect page). Predicted salaries are ignored."""
    seen: dict[str, Job] = {}
    for term in terms:
        for page in range(1, pages + 1):
            data = _get(f"https://api.adzuna.com/v1/api/jobs/{country}/search/{page}",
                        params={"app_id": app_id, "app_key": app_key, "results_per_page": 50, "what": term,
                                "max_days_old": max_age_days, "content-type": "application/json"})
            rows = data.get("results", [])
            for j in rows:
                jid = str(j.get("id", ""))
                if not jid or jid in seen:
                    continue
                predicted = str(j.get("salary_is_predicted", "0")) == "1"
                lo, hi = (None, None) if predicted else _salary(j.get("salary_min"), j.get("salary_max"))
                desc = strip_html(j.get("description", ""))
                if lo is None and hi is None:
                    lo, hi = extract_pay(desc)
                seen[jid] = Job(
                    id=f"az:{jid}", source="adzuna", company=(j.get("company") or {}).get("display_name", ""),
                    title=html.unescape(j.get("title", "")), location=(j.get("location") or {}).get("display_name", ""),
                    url=j.get("redirect_url", ""), posted=_day(j.get("created")), description=desc, pay_min=lo, pay_max=hi)
            if len(rows) < 50:
                break
    return list(seen.values())


# ----------------------------------------------------------------------------- Jooble (free key)
def fetch_jooble(api_key: str, terms: list[str], pages: int = 2) -> list[Job]:
    """Jooble's search API (a free key is available on request). It returns a short snippet of each description. Links go
    to Jooble, which forwards to the original posting."""
    seen: dict[str, Job] = {}
    for term in terms:
        for page in range(1, pages + 1):
            r = sources._session().post(f"https://jooble.org/api/{api_key}", json={"keywords": term, "page": str(page)}, timeout=sources.TIMEOUT)
            r.raise_for_status()
            rows = r.json().get("jobs", [])
            for j in rows:
                link = j.get("link", "")
                jid = str(j.get("id") or link)
                if not link or jid in seen:
                    continue
                desc = strip_html(j.get("snippet", ""))
                lo, hi = extract_pay(j.get("salary") or "") if j.get("salary") else (None, None)
                if lo is None and hi is None:
                    lo, hi = extract_pay(desc)
                seen[jid] = Job(
                    id=f"jo:{jid}"[:200], source="jooble", company=j.get("company", ""), title=html.unescape(j.get("title", "")),
                    location=j.get("location", ""), url=link, posted=_day(j.get("updated")), description=desc, pay_min=lo, pay_max=hi)
            if len(rows) < 20:
                break
    return list(seen.values())
