"""More applicant-tracking systems with public job-board APIs: Workable, Recruitee, BambooHR, Rippling, Gem, Personio.
Each company that uses one has a board named by a short "slug" (the part of its careers address that names the company).
Fetchers return the same Job objects as sources.py. Where a list has no description (Workable, BambooHR, Rippling), the
description is filled in later, only for postings that pass your filters (see sources.DETAIL_FILLERS)."""
from __future__ import annotations

import xml.etree.ElementTree as ET

import sources
from sources import Job, _get, _session, extract_pay, strip_html
from feed_sources import _day, _salary


def _place(*parts) -> str:
    return ", ".join(str(p) for p in parts if p)


# ----------------------------------------------------------------------------- Workable
def fetch_workable(slug: str, max_pages: int = 10) -> list[Job]:
    """apply.workable.com/<slug>. The list gives no description; fill_workable_detail adds it."""
    jobs, token = [], None
    for _ in range(max_pages):
        body = {"query": "", "location": [], "department": [], "worktype": [], "remote": []}
        if token:
            body["token"] = token
        r = _session().post(f"https://apply.workable.com/api/v3/accounts/{slug}/jobs", json=body, timeout=sources.TIMEOUT)
        r.raise_for_status()
        data = r.json()
        for j in data.get("results", []):
            loc = j.get("location") or {}
            place = _place(loc.get("city"), loc.get("region"), loc.get("country"))
            if j.get("remote") or j.get("workplace") == "remote":
                place = _place(place, "Remote")
            jobs.append(Job(
                id=f"wk:{slug}:{j['shortcode']}", source="workable", company=slug, title=j.get("title", ""), location=place,
                url=f"https://apply.workable.com/{slug}/j/{j['shortcode']}/", posted=_day(j.get("published")),
                detail={"slug": slug, "shortcode": j["shortcode"]}))
        token = data.get("nextPage")
        if not token:
            break
    return jobs


def fill_workable_detail(job: Job) -> None:
    d = _get(f"https://apply.workable.com/api/v2/accounts/{job.detail['slug']}/jobs/{job.detail['shortcode']}")
    job.description = strip_html(" ".join(d.get(k) or "" for k in ("description", "requirements", "benefits")))
    job.pay_min, job.pay_max = extract_pay(job.description)


# ----------------------------------------------------------------------------- Recruitee
def fetch_recruitee(slug: str) -> list[Job]:
    """<slug>.recruitee.com. One request returns every offer with its description."""
    jobs = []
    for o in _get(f"https://{slug}.recruitee.com/api/offers/").get("offers", []):
        if o.get("status") not in (None, "published"):
            continue
        desc = strip_html((o.get("description") or "") + " " + (o.get("requirements") or ""))
        sal = o.get("salary") or {}
        lo, hi = _salary(sal.get("min"), sal.get("max"), "annual" if (sal.get("period") or "year") == "year" else "other")
        if lo is None and hi is None:
            lo, hi = extract_pay(desc)
        place = o.get("location") or _place(o.get("city"), o.get("state_name"), o.get("country"))
        if o.get("remote"):
            place = _place(place, "Remote")
        jobs.append(Job(
            id=f"rc:{slug}:{o['id']}", source="recruitee", company=o.get("company_name") or slug, title=o.get("title", ""),
            location=place, url=o.get("careers_url", ""), posted=_day(o.get("published_at") or o.get("created_at")),
            description=desc, pay_min=lo, pay_max=hi))
    return jobs


# ----------------------------------------------------------------------------- BambooHR
def fetch_bamboohr(slug: str) -> list[Job]:
    """<slug>.bamboohr.com/careers. A slug that is not a BambooHR customer answers with a web page, not JSON."""
    r = _session().get(f"https://{slug}.bamboohr.com/careers/list", headers={"Accept": "application/json"}, timeout=sources.TIMEOUT)
    r.raise_for_status()
    if "json" not in (r.headers.get("content-type") or ""):
        raise ValueError("not a BambooHR board")
    jobs = []
    for j in r.json().get("result", []):
        loc = j.get("atsLocation") or {}
        city = j.get("location") or {}
        place = _place(loc.get("city") or city.get("city"), loc.get("state") or city.get("state"), loc.get("country"))
        if j.get("isRemote"):
            place = _place(place, "Remote")
        jobs.append(Job(
            id=f"bb:{slug}:{j['id']}", source="bamboohr", company=slug, title=j.get("jobOpeningName", ""), location=place,
            url=f"https://{slug}.bamboohr.com/careers/{j['id']}", detail={"slug": slug, "id": j["id"]}))
    return jobs


def fill_bamboohr_detail(job: Job) -> None:
    d = _get(f"https://{job.detail['slug']}.bamboohr.com/careers/{job.detail['id']}/detail")
    opening = (d.get("result") or {}).get("jobOpening") or {}
    job.description = strip_html(opening.get("description") or "")
    job.posted = _day(opening.get("datePosted")) or job.posted
    job.pay_min, job.pay_max = extract_pay(job.description)


# ----------------------------------------------------------------------------- Rippling
def fetch_rippling(slug: str, page_size: int = 100, max_pages: int = 10) -> list[Job]:
    """ats.rippling.com/<slug>. The list gives no description; fill_rippling_detail adds it."""
    jobs: list[Job] = []
    for page in range(max_pages):
        data = _get(f"https://ats.rippling.com/api/v2/board/{slug}/jobs", params={"page": page, "pageSize": page_size})
        for j in data.get("items", []):
            places = [l.get("name") for l in j.get("locations", []) if l.get("name")]
            jobs.append(Job(
                id=f"rp:{slug}:{j['id']}", source="rippling", company=slug, title=j.get("name", ""),
                location="; ".join(places), url=j.get("url", ""), detail={"slug": slug, "id": j["id"]}))
        if page + 1 >= int(data.get("totalPages", 1) or 1):
            break
    return jobs


def fill_rippling_detail(job: Job) -> None:
    d = _get(f"https://ats.rippling.com/api/v2/board/{job.detail['slug']}/jobs/{job.detail['id']}")
    desc = d.get("description")
    if isinstance(desc, dict):   # some boards return several named sections
        desc = " ".join(str(v) for v in desc.values())
    job.description = strip_html(desc or "")
    job.posted = _day(d.get("createdOn")) or job.posted
    places = [w if isinstance(w, str) else (w or {}).get("name", "") for w in d.get("workLocations") or []]
    if places and not job.location:
        job.location = "; ".join(p for p in places if p)
    job.pay_min, job.pay_max = extract_pay(job.description)


# ----------------------------------------------------------------------------- Gem
def fetch_gem(slug: str) -> list[Job]:
    """jobs.gem.com/<slug>. One request returns every posting with its description."""
    data = _get(f"https://api.gem.com/job_board/v0/{slug}/job_posts/")
    jobs = []
    for j in data if isinstance(data, list) else []:
        desc = j.get("content_plain") or strip_html(j.get("content", ""))
        lo, hi = extract_pay(desc)
        place = (j.get("location") or {}).get("name", "")
        if j.get("location_type") == "remote" and "remote" not in place.lower():
            place = _place(place, "Remote")
        jobs.append(Job(
            id=f"gm:{slug}:{j['id']}", source="gem", company=slug, title=j.get("title", ""), location=place,
            url=j.get("absolute_url", ""), posted=_day(j.get("first_published_at") or j.get("created_at")),
            description=desc, pay_min=lo, pay_max=hi))
    return jobs


# ----------------------------------------------------------------------------- Personio
def fetch_personio(slug: str) -> list[Job]:
    """<slug>.jobs.personio.de, an XML feed of every open position with its description."""
    r = _session().get(f"https://{slug}.jobs.personio.de/xml", params={"language": "en"}, timeout=sources.TIMEOUT)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    jobs = []
    for p in root.iter("position"):
        pid = (p.findtext("id") or "").strip()
        if not pid:
            continue
        parts = [strip_html((d.findtext("name") or "") + ". " + (d.findtext("value") or "")) for d in p.iter("jobDescription")]
        desc = "\n".join(x for x in parts if x)
        lo, hi = extract_pay(desc)
        jobs.append(Job(
            id=f"pn:{slug}:{pid}", source="personio", company=(p.findtext("subcompany") or slug).strip(),
            title=(p.findtext("name") or "").strip(), location=(p.findtext("office") or "").strip(),
            url=f"https://{slug}.jobs.personio.de/job/{pid}", posted=_day(p.findtext("createdAt")),
            description=desc, pay_min=lo, pay_max=hi))
    return jobs


# Registered here so main.py needs no changes when a platform is added.
sources.DETAIL_FILLERS.update({"workable": fill_workable_detail, "bamboohr": fill_bamboohr_detail, "rippling": fill_rippling_detail})
