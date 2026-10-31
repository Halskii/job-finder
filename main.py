"""job-finder: collect postings, filter them, score fit with Claude, and keep a tracker.

Usage:
  python main.py run [--no-llm] [--limit N] [--fetch-only-new]
  python main.py list [--top N] [--min-score S]
  python main.py mark <id-fragment> <applied|saved|skipped|interview|rejected|offer> [note]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import yaml

import db
import discovery
import feed_sources
import filters
import gov_sources
import scorer
import sources

ROOT = Path(__file__).parent
STATUSES = {"new", "saved", "applied", "skipped", "interview", "offer", "accepted", "rejected", "withdrawn"}


def load_env() -> None:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def read_resume_file() -> str:
    """Prefer resume.pdf, then resume.txt / resume.md. Returns raw text, or '' if none exists."""
    pdf = ROOT / "resume.pdf"
    if pdf.exists():
        from pypdf import PdfReader
        text = "\n".join((p.extract_text() or "") for p in PdfReader(str(pdf)).pages)
        return text.replace("ﬁ", "fi").replace("ﬂ", "fl")   # PDF ligatures
    for name in ("resume.txt", "resume.md"):
        path = ROOT / name
        if path.exists():
            return path.read_text(encoding="utf-8")
    return ""


# A phone number such as (555) 123-4567, 555-123-4567, or 555.123.4567. Ordinary numbers like 100,000 or 2018 do not match.
PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[-. ]?)?\(?\d{3}\)?[-. ]?\d{3}[-. ]\d{4}(?!\d)")


def load_resume() -> str:
    """Load the resume. Contact lines (email, phone, links) are removed before anything is sent to the API."""
    raw = read_resume_file()
    if not raw:
        return ""
    keep = []
    for line in raw.splitlines():
        low = line.lower()
        if "@" in low or "linkedin.com" in low or "github.com" in low or PHONE_RE.search(line):
            continue
        keep.append(line)
    return "\n".join(keep).strip()


def log(msg: str) -> None:
    print(msg, flush=True)


PLATFORMS = [kind for kind, _, _ in discovery.BOARD_FETCHERS] + ["workday"]   # the names `platforms:` in config.yaml accepts


def platform_enabled(cfg: dict, name: str) -> bool:
    """False when the `platforms:` section of config.yaml turns this board platform off. One that is not listed is on."""
    return bool((cfg.get("platforms") or {}).get(name, True))


def collect(cfg: dict, conn=None) -> list[sources.Job]:
    delay = cfg.get("request_delay", 0.4)
    src = cfg.get("sources", {})
    jobs: list[sources.Job] = []
    for name in cfg.get("platforms") or {}:   # a typo would otherwise switch nothing off and say nothing
        if name not in PLATFORMS:
            log(f"  platforms: '{name}' is not a platform name (ignored). Names: {', '.join(PLATFORMS)}")
    for kind in PLATFORMS:
        if not platform_enabled(cfg, kind) and src.get(kind):
            log(f"  {kind:10s} turned off in config.yaml (platforms), {len(src[kind])} board(s) not read")
    plan = [(kind, getattr(mod, fn)) for kind, mod, fn in discovery.BOARD_FETCHERS if platform_enabled(cfg, kind)]
    tasks = [(kind, fn, slug) for kind, fn in plan for slug in src.get(kind, []) or []]
    workday = (src.get("workday", []) or []) if platform_enabled(cfg, "workday") else []

    def one(task):
        kind, fn, slug = task
        try:
            return kind, slug, fn(slug), None
        except Exception as e:  # bad slug, rate limit, network: skip and keep going
            return kind, slug, [], e
        finally:
            time.sleep(delay)

    # The boards are small, separate lists, so several are read at once. Lines print as boards finish; jobs keep config order.
    with ThreadPoolExecutor(max_workers=max(1, int(cfg.get("fetch_workers", 8)))) as pool:
        futures = [pool.submit(one, t) for t in tasks]
        results = {}
        for fut in as_completed(futures):
            kind, slug, got, err = results[id(fut)] = fut.result()
            if err:
                log(f"  {kind:10s} {slug:18s} skipped ({type(err).__name__}: {str(err)[:70]})")
            else:
                log(f"  {kind:10s} {slug:18s} {len(got):4d} postings")
        for f in futures:
            jobs += results[id(f)][2]
    jobs += collect_workday(cfg, workday, delay)
    jobs += collect_feeds(cfg, conn)
    jobs += collect_government(cfg)
    return jobs


def collect_workday(cfg: dict, entries: list[dict], delay: float) -> list[sources.Job]:
    """Read every Workday site. Sites are different servers, so several are read at once (`workday_workers`, default 16);
    each site is still read one request at a time, with `request_delay` between requests."""
    terms, pages = filters.workday_terms(cfg), cfg.get("workday_max_pages", 3)
    gov_terms = terms + [t for t in filters.base_title_strings(cfg.get("gov_titles", [])) if t not in terms]

    def one(entry):
        try:   # an entry marked `gov: true` (a city, university, or agency) is also searched with the government titles
            return entry, sources.fetch_workday(entry, gov_terms if entry.get("gov") else terms, pages, delay), None
        except Exception as e:   # a site that is down or renamed must not stop the others
            return entry, [], e

    jobs: list[sources.Job] = []
    with ThreadPoolExecutor(max_workers=max(1, int(cfg.get("workday_workers", 16)))) as pool:
        futures = [pool.submit(one, e) for e in entries]
        done = {}
        for fut in as_completed(futures):
            entry, got, err = done[id(fut)] = fut.result()
            name = entry.get("name", entry["tenant"])
            if err:
                log(f"  workday    {name:18s} skipped ({type(err).__name__}: {str(err)[:70]})")
            else:
                log(f"  workday    {name:18s} {len(got):4d} postings")
        for f in futures:   # keep the order of the list, whatever order the sites finished in
            jobs += done[id(f)][1]
    return jobs


def prefetch_details(jobs: list[sources.Job], workers: int, delay: float) -> set[str]:
    """Fetch the full posting for each job. Jobs are grouped by server: different servers are asked at the same time
    (up to `workers`), but one server only ever gets one request at a time. Returns the ids that failed."""
    by_host: dict[str, list[sources.Job]] = {}
    for j in jobs:
        by_host.setdefault(urlparse(j.detail.get("api") or j.url).netloc, []).append(j)

    lock = threading.Lock()
    state = {"done": 0, "next": 10}

    def advance() -> None:
        with lock:
            state["done"] += 1
            pct = 100 * state["done"] // len(jobs)
            if pct >= state["next"] or state["done"] == len(jobs):
                while state["next"] <= pct:
                    state["next"] += 10
                log(f"  [{min(pct, 100):3d}%] full postings read: {state['done']} of {len(jobs)}")

    def fetch_group(group):
        bad = []
        for j in group:
            try:
                sources.fill_detail(j)
            except Exception:
                bad.append(j.id)
            advance()
            time.sleep(delay)
        return bad

    failed: set[str] = set()
    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
        for bad in pool.map(fetch_group, by_host.values()):
            failed.update(bad)
    return failed


def collect_government(cfg: dict) -> list[sources.Job]:
    """Federal (USAJobs, needs a free key in .env) and Texas HHS postings. On by default; set `enabled: false`
    under `government:` in config.yaml to turn one off."""
    gov = cfg.get("government") or {}
    delay = cfg.get("request_delay", 0.4)
    jobs: list[sources.Job] = []
    uj = gov.get("usajobs") or {}
    if uj.get("enabled", True):
        load_env()
        key, email = os.environ.get("USAJOBS_API_KEY"), os.environ.get("USAJOBS_EMAIL")
        if not (key and email):
            log(f"  usajobs    skipped (add USAJOBS_API_KEY and USAJOBS_EMAIL to .env; free key at developer.usajobs.gov/apirequest)")
        else:
            try:
                got = gov_sources.fetch_usajobs(key, email, uj.get("keywords", ["software developer"]),
                                                cfg.get("max_age_days", 30), uj.get("pages", 2), delay)
                log(f"  usajobs    {'(federal)':18s} {len(got):4d} postings")
                jobs += got
            except Exception as e:
                log(f"  usajobs    {'(federal)':18s} skipped ({type(e).__name__}: {str(e)[:70]})")
    tx = gov.get("texas_hhs") or {}
    if tx.get("enabled", True):
        try:
            got = gov_sources.fetch_texas_hhs(tx.get("search_terms", ["developer", "programmer"]), tx.get("pages", 2), delay)
            log(f"  texas hhs  {'(HHSC/DFPS/DSHS)':18s} {len(got):4d} postings")
            if not got:
                log("             no postings found; the site's page layout may have changed")
            jobs += got
        except Exception as e:
            log(f"  texas hhs  {'(HHSC/DFPS/DSHS)':18s} skipped ({type(e).__name__}: {str(e)[:70]})")
    return jobs


def _feed_terms(feed: dict, cfg: dict) -> list[str]:
    return list(feed.get("terms") or filters.workday_terms(cfg))


# name -> (label for the log, function(feed settings, whole config) -> jobs). Looked up when called, so tests can replace them.
FEEDS = {
    "remotive": lambda f, cfg: sources.fetch_remotive(f.get("categories", ["software-development"])),
    "remoteok": lambda f, cfg: feed_sources.fetch_remoteok(),
    "weworkremotely": lambda f, cfg: feed_sources.fetch_weworkremotely(
        f.get("categories", ["remote-programming-jobs", "remote-devops-sysadmin-jobs", "remote-back-end-programming-jobs"])),
    "jobicy": lambda f, cfg: feed_sources.fetch_jobicy(_feed_terms(f, cfg)),
    "workingnomads": lambda f, cfg: feed_sources.fetch_workingnomads(),
    "himalayas": lambda f, cfg: feed_sources.fetch_himalayas(_feed_terms(f, cfg), f.get("pages", 2)),
    "hackernews": lambda f, cfg: feed_sources.fetch_hn_hiring(f.get("threads", 2)),
    "adzuna": lambda f, cfg: feed_sources.fetch_adzuna(os.environ["ADZUNA_APP_ID"], os.environ["ADZUNA_APP_KEY"], _feed_terms(f, cfg),
                                                       cfg.get("max_age_days", 30), f.get("pages", 2)),
    "jooble": lambda f, cfg: feed_sources.fetch_jooble(os.environ["JOOBLE_API_KEY"], _feed_terms(f, cfg), f.get("pages", 2)),
}
# feeds that need a free key: environment variables and where to get one
FEED_KEYS = {"adzuna": (("ADZUNA_APP_ID", "ADZUNA_APP_KEY"), "free keys at developer.adzuna.com"),
             "jooble": (("JOOBLE_API_KEY",), "a free key on request at jooble.org/api/about")}


def collect_feeds(cfg: dict, conn) -> list[sources.Job]:
    """Aggregator feeds, each on only when `enabled: true` under `feeds:`. Remotive asks for at most about four requests a
    day; the others ask not to be polled constantly. So each feed is fetched at most once per its `min_hours` (default 6),
    tracked in the database. Job pages link back to the feed's own site, as their terms require."""
    jobs: list[sources.Job] = []
    if conn is None:
        return jobs
    for name, fetch in FEEDS.items():
        feed = (cfg.get("feeds") or {}).get(name) or {}
        if not feed.get("enabled"):
            continue
        if name in FEED_KEYS:
            load_env()
            names, where = FEED_KEYS[name]
            if not all(os.environ.get(n) for n in names):
                log(f"  {name:14s} skipped (add {' and '.join(names)} to .env; {where})")
                continue
        gap = float(feed.get("min_hours", 6)) * 3600
        if time.time() - float(db.get_meta(conn, f"{name}_last_fetch") or 0) < gap:
            log(f"  {name:14s} skipped (fetched recently; it is fetched at most once every {feed.get('min_hours', 6)} hours)")
            continue
        db.set_meta(conn, f"{name}_last_fetch", str(time.time()))   # recorded before the call so failures don't cause retries
        try:
            got = fetch(feed, cfg)
            log(f"  {name:14s} {'(feed)':14s} {len(got):4d} postings")
            jobs += got
        except Exception as e:
            log(f"  {name:14s} {'(feed)':14s} skipped ({type(e).__name__}: {str(e)[:70]})")
    return jobs


def require_anthropic() -> None:
    """Scoring needs the `anthropic` package. Check before a long fetch, not after it, and say which Python is missing it."""
    try:
        import anthropic  # noqa: F401
    except ImportError:
        venv = ROOT / ".venv" / "Scripts" / "python.exe"
        hint = (f"This project has a virtual environment with it installed. Run:  {venv.relative_to(ROOT)} main.py run"
                if venv.exists() else "Install it with:  pip install -r requirements.txt")
        sys.exit(f"The 'anthropic' package is not installed for the Python running this ({sys.executable}).\n{hint}\n"
                 f"Nothing was fetched. (Fetch only, with no scoring, works without it:  main.py run --no-llm)")


def cmd_run(args, cfg, profile, conn) -> None:
    """The whole pipeline: fetch every source, filter cheaply, read full postings where a site only lists titles,
    store what is new, then score the best unscored jobs with Claude and write the report."""
    if not args.no_llm:
        load_env()
        if os.environ.get("ANTHROPIC_API_KEY"):   # without a key, scoring is skipped anyway
            require_anthropic()
    log("Fetching postings...")
    t0 = time.time()
    jobs = collect(cfg, conn)
    t_list = time.time() - t0
    patterns = filters.base_titles(cfg["titles"])
    gov_patterns = patterns + filters.base_titles(cfg.get("gov_titles", []))   # government titles read differently
    exclude = [w.lower() for w in cfg.get("exclude_title_words", [])]
    loc_excl = filters.compile_exclusions(cfg.get("exclude_locations", []))
    # How many postings each step dropped or kept; printed as the one-line summary after filtering.
    stats = {"fetched": len(jobs), "title": 0, "location": 0, "excluded": 0, "pay": 0, "age": 0, "new": 0, "seen": 0, "dupe": 0, "late": 0}

    log(f"\nFiltering {len(jobs):,} postings...")
    # Pass 1: free filters only, in order of cheapness. A job that fails any of them is never looked up or stored.
    candidates = []
    for job in jobs:
        use_gov = job.source in gov_sources.GOV_SOURCES or job.detail.get("gov")
        if not filters.title_ok(job.title, gov_patterns if use_gov else patterns, exclude):
            continue
        stats["title"] += 1
        if not filters.location_ok(job.location, cfg.get("us_only", True)):
            continue
        stats["location"] += 1
        if not filters.exclusion_ok(job.location, loc_excl):
            stats["excluded"] += 1
            continue
        if conn.execute("SELECT 1 FROM jobs WHERE id=?", (job.id,)).fetchone():
            stats["seen"] += 1
            continue
        if not filters.age_ok(job.posted, cfg.get("max_age_days", 30)):   # a date from the list: no need to fetch the full posting
            stats["age"] += 1
            continue
        candidates.append(job)

    # Sources whose lists have no description need one extra request per job. Only jobs that passed the cheap filters
    # above, and are not already stored from another source, are looked up, several servers at a time.
    need = [j for j in candidates if j.source in sources.DETAIL_FILLERS and not db.is_cross_source_duplicate(conn, j)]
    t1 = time.time()
    if need:
        log(f"Reading the full text of {len(need)} postings that passed the filters (these sites list only titles)...")
    failed = prefetch_details(need, cfg.get("detail_workers", 16), cfg.get("request_delay", 0.4)) if need else set()
    t_detail = time.time() - t1

    # Pass 2: now that descriptions are filled in, apply what could not be checked before (real location, pay,
    # duplicates from other sources) and store the survivors.
    for job in candidates:
        if db.is_cross_source_duplicate(conn, job):   # same company + title already stored from another source
            stats["dupe"] += 1
            continue
        if job.id in failed:   # the lookup failed; it is not stored, so the next run tries it again
            continue
        if job.source in sources.DETAIL_FILLERS and not (filters.location_ok(job.location, cfg.get("us_only", True))
                                                         and filters.exclusion_ok(job.location, loc_excl)):
            stats["late"] += 1   # the full posting revealed the real location(s), and none are allowed
            continue
        if not filters.age_ok(job.posted, cfg.get("max_age_days", 30)):
            stats["age"] += 1
            continue
        if not filters.pay_ok(job.pay_max, job.pay_min, cfg.get("min_pay", 0)):
            stats["pay"] += 1
            continue
        if db.add_job(conn, job, filters.quick_score(job.description, profile)):
            stats["new"] += 1

    log(f"\nFetched {stats['fetched']} | title match {stats['title']} | after location {stats['location']} | "
        f"excluded locations {stats['excluded']} | already seen {stats['seen']} | duplicates {stats['dupe']} | dropped by full location {stats['late']} | too old {stats['age']} | "
        f"below pay {stats['pay']} | NEW {stats['new']}")
    log(f"Time: listing {t_list:.0f}s | full postings {t_detail:.0f}s ({len(need)} looked up)")

    if args.no_llm:
        log("Skipping LLM scoring (--no-llm). Jobs are stored; run again without the flag to score them.")
        return

    load_env()
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        log("\nNo ANTHROPIC_API_KEY found (set it in .env or your environment). Skipping scoring.")
        return
    # Scoring: the stored jobs that look best by keyword overlap are scored first, up to the per-run limit.
    import anthropic
    client = anthropic.Anthropic(api_key=key, max_retries=6)   # retries rate-limit (429) and overloaded responses with backoff
    resume_text = load_resume()
    log(f"Resume context: {'loaded' if resume_text else 'none (add resume.txt)'}")
    scfg = cfg.get("scoring", {})
    limit = args.limit or scfg.get("max_per_run", 40)
    # jobs stored before an exclusion was added must not be scored (that would waste API credits)
    todo = visible(db.unscored(conn, limit * 4), loc_excl)[:limit]
    workers = max(1, int(scfg.get("workers", 8)))
    log(f"\nScoring {len(todo)} jobs with {scfg.get('model')}, {workers} at a time...")
    t2 = time.time()
    tok_in = tok_out = 0

    def score_one(row):
        return scorer.score_job(client, scfg["model"], profile, row, cfg.get("min_pay", 0), resume_text)

    # The API calls run in worker threads. Results are stored here, one at a time, because a SQLite connection
    # belongs to the thread that made it.
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(score_one, row): row for row in todo}
        for i, fut in enumerate(as_completed(futures), 1):
            row = futures[fut]
            try:
                res = fut.result()
                db.save_score(conn, row["id"], res["fit_score"], res["verdict"], res)
                tok_in += res.get("_in", 0)
                tok_out += res.get("_out", 0)
                log(f"  [{i}/{len(todo)}] {res['fit_score']:3d} {res['verdict']:5s} {row['company']} - {row['title']}")
            except Exception as e:
                log(f"  [{i}/{len(todo)}] error on {row['title']}: {type(e).__name__}: {str(e)[:80]}")
    log(f"Scoring took {time.time() - t2:.0f}s")
    price = scfg.get("price_per_million_tokens", {})
    cost = tok_in / 1e6 * price.get("input", 0) + tok_out / 1e6 * price.get("output", 0)
    log(f"\nTokens: {tok_in:,} in / {tok_out:,} out | estimated cost ${cost:.3f} "
        f"(uses the prices in config.yaml; check Anthropic's pricing page)")
    write_report(conn, cfg)


def visible(rows, loc_excl) -> list:
    """Drop stored rows whose location is excluded by the current config."""
    return [r for r in rows if filters.exclusion_ok(r["location"], loc_excl)]


def money(v) -> str:
    """A pay figure for the report, or "?" when the posting did not list one."""
    return f"${v:,}" if v else "?"


def write_report(conn, cfg) -> Path | None:
    """Write reports/<today>.md with the best scored jobs, highest score first. Rerunning on the same day overwrites it."""
    scfg = cfg.get("scoring", {})
    size = scfg.get("report_size", 1000)
    rows = visible(db.top(conn, size * 4, scfg.get("report_min_score", 60)),
                   filters.compile_exclusions(cfg.get("exclude_locations", [])))[:size]
    out_dir = ROOT / "reports"
    out_dir.mkdir(exist_ok=True)
    path = out_dir / f"{date.today().isoformat()}.md"
    lines = [f"# Job matches, {date.today().isoformat()}", ""]
    if not rows:
        lines.append("No scored jobs at or above the report threshold yet.")
    for r in rows:
        a = json.loads(r["analysis"] or "{}")
        lines += [
            f"## {r['score']} | {r['title']} at {r['company']}",
            f"- Location: {r['location'] or 'n/a'} | Pay: {money(r['pay_min'])} - {money(r['pay_max'])} | Posted: {r['posted'] or 'unknown'}",
            f"- Verdict: **{r['verdict']}** | {a.get('reason', '')}",
            f"- Matched: {', '.join(a.get('matched', [])) or 'n/a'}",
            f"- Missing required: {', '.join(a.get('missing_required', [])) or 'none noted'}",
            f"- Dealbreakers: {', '.join(a.get('dealbreakers', [])) or 'none'}",
            f"- Resume angle: {a.get('resume_angle', '')}",
            f"- Link: {r['url']}",
            f"- ID: `{r['id']}`",
            "",
        ]
    path.write_text("\n".join(lines), encoding="utf-8")
    log(f"\nReport written: {path}")
    return path


def cmd_list(args, cfg, profile, conn) -> None:
    scfg = cfg.get("scoring", {})
    rows = visible(db.top(conn, args.top * 4, args.min_score if args.min_score is not None else scfg.get("report_min_score", 60)),
                   filters.compile_exclusions(cfg.get("exclude_locations", [])))[:args.top]
    if not rows:
        log("Nothing scored at that threshold yet. Run: python main.py run")
        return
    for r in rows:
        log(f"{r['score']:3d} {r['verdict']:5s} {r['company']:18s} {r['title'][:55]:55s} {r['id']}")


def cmd_discover(args, cfg, profile, conn) -> None:
    """Look for job boards by company name. Shows what it finds; saves only with --add."""
    names = list(args.names)
    if args.file:
        names += [ln.strip() for ln in Path(args.file).read_text(encoding="utf-8").splitlines()
                  if ln.strip() and not ln.lstrip().startswith("#")]
    if not names:
        log("Give company names, e.g.  python main.py discover Stripe Datadog   (or --file companies.txt, one name per line)")
        return
    have = cfg.get("sources", {})
    found: dict[str, list[str]] = {}
    for name in names:
        hits = discovery.probe(name, cfg.get("request_delay", 0.4))
        if not hits:
            log(f"{name}: no board found on {', '.join(discovery.LIST_KEYS)}. If it uses Workday, run: "
                f"python main.py workday-add <careers page URL>")
            continue
        for h in hits:
            m = discovery.matching(h.jobs, cfg)
            known = h.slug.lower() in {str(x).lower() for x in have.get(h.system, []) or []}
            log(f"{name}: {h.system}/{h.slug}: {len(h.jobs)} postings, {len(m)} match your filters"
                f"{' (already in your sources)' if known else ''}")
            sample = (m or h.jobs)[0]
            log(f"    e.g. {sample.title} | {sample.location} | {sample.url}")
            if not known:
                found.setdefault(h.system, []).append(h.slug)
    total = sum(len(v) for v in found.values())
    if not total:
        log("\nNothing new to add.")
    elif args.add:
        discovery.save_discovered(ROOT, found)
        log(f"\nSaved {total} board(s) to {discovery.DISCOVERED_FILE}. They are searched on the next run.")
    else:
        log(f"\n{total} new board(s) found, nothing saved. Check the examples above are the right company, "
            f"then run again with --add.")


def cmd_crawl(args, cfg, profile, conn) -> None:
    """Find company boards in bulk from Common Crawl's public web index, check each against the platform's job API, and
    keep the ones that list a posting matching your titles. Saves only with --add."""
    import crawl

    systems = [s.strip() for s in args.systems.split(",")] if args.systems else list(crawl.PATTERNS)
    bad = [s for s in systems if s not in crawl.PATTERNS]
    if bad:
        sys.exit(f"Unknown platform(s): {', '.join(bad)}. Choose from: {', '.join(crawl.PATTERNS)}")
    ids = crawl.resolve_ids(ROOT, args.crawls)
    if not ids:
        log("Common Crawl's index is not answering right now, so no boards could be looked up. Nothing is wrong with your setup and "
            "nothing was lost. It is a shared free service that sometimes refuses connections for a while after heavy use. "
            "Try again in an hour or so; the command picks up where it stopped.")
        return
    total, problems = 0, []
    for system in systems:
        log(f"\n{system}: reading Common Crawl's index ({args.crawls} recent crawls)...")
        cands = crawl.enumerate_candidates(ROOT, [system], args.crawls, log, ids=ids, problems=problems)[system]
        have = crawl.known_keys(cfg.get("sources"), system)
        ident = crawl._workday_id if system == "workday" else crawl._key
        fresh = [c for c in cands if ident(c) not in have]
        log(f"  {len(cands)} boards found, {len(cands) - len(fresh)} already in your sources, {len(fresh)} to check")
        kept = crawl.validate(system, fresh, cfg, args.keep_all, int(cfg.get("crawl_workers", 16)), log, ROOT, 0 if args.recheck else 14)
        total += len(kept)
        if kept and args.add:
            discovery.save_discovered(ROOT, {system: kept})
            log(f"  saved {len(kept)} {system} board(s) to {discovery.DISCOVERED_FILE}")
    if problems:
        log(f"\nThe index stopped answering while reading: {', '.join(dict.fromkeys(problems))}. Those were only partly read; "
            f"run the command again later to read the rest.")
    if not args.add:
        log(f"\n{total} new board(s) found, nothing saved. Run again with --add to keep them (checked boards are remembered, so it is quick).")
    else:
        log(f"\nDone: {total} new board(s) saved. They are searched on the next `run`.")


def cmd_workday_add(args, cfg, profile, conn) -> None:
    """Add a Workday careers site from any page address on it."""
    try:
        entry = discovery.parse_workday_url(args.url, args.name)
    except ValueError as e:
        log(str(e))
        return
    try:
        total = discovery.check_workday(entry)
    except Exception as e:
        log(f"Could not read {entry['host']} / {entry['site']} ({type(e).__name__}: {str(e)[:80]}). Nothing saved.")
        return
    log(f"Workday site {entry['host']} / {entry['site']}: {total} postings.")
    discovery.save_discovered(ROOT, {"workday": [entry]})
    log(f"Saved as '{entry['name']}' in {discovery.DISCOVERED_FILE} (use --name to change the label).")


def cmd_profile(args, cfg, profile, conn) -> None:
    """Rebuild profile.json from resume.pdf, show the differences, and write it only with approval."""
    import shutil
    import profile_gen

    resume_text = load_resume()
    if not resume_text:
        sys.exit("No resume found. Put resume.pdf in the project folder.")
    load_env()
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        sys.exit("No ANTHROPIC_API_KEY found (set it in .env or your environment).")
    import anthropic
    client = anthropic.Anthropic(api_key=key)
    model = cfg.get("profile_model") or cfg.get("scoring", {}).get("model")
    log(f"Reading resume and extracting a profile with {model}...")
    generated = profile_gen.generate(client, model, resume_text)
    new, notes = profile_gen.merge(profile, generated)
    changes = profile_gen.diff(profile, new)
    log("\nChanges compared with the current profile.json:" if changes else "\nNo changes from the resume.")
    for line in changes:
        log(line)
    for n in notes:
        log("\n" + n)
    if not changes:
        return
    if not args.apply:
        if sys.stdin.isatty():
            args.apply = input("\nWrite these changes to profile.json? [y/N] ").strip().lower() == "y"
        if not args.apply:
            log("\nNothing written. Re-run with --apply to accept.")
            return
    shutil.copy(ROOT / "profile.json", ROOT / "profile.json.bak")
    (ROOT / "profile.json").write_text(json.dumps(new, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    log("profile.json updated (previous version saved as profile.json.bak).")


def cmd_mark(args, cfg, profile, conn) -> None:
    if args.status not in STATUSES:
        sys.exit(f"status must be one of: {', '.join(sorted(STATUSES))}")
    n = db.set_status(conn, args.id, args.status, args.note or "")
    log(f"Updated {n} job(s).")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Find and rank jobs that fit your verified profile.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="fetch, filter, score, and write a report")
    r.add_argument("--no-llm", action="store_true", help="fetch and filter only (no API calls)")
    r.add_argument("--limit", type=int, help="max LLM scoring calls this run")
    l = sub.add_parser("list", help="show top scored jobs from the database")
    l.add_argument("--top", type=int, default=20)
    l.add_argument("--min-score", type=int)
    p = sub.add_parser("profile", help="rebuild profile.json from resume.pdf (shows a diff, asks before writing)")
    p.add_argument("--apply", action="store_true", help="write the changes without asking")
    m = sub.add_parser("mark", help="update a job's status in the tracker")
    m.add_argument("id", help="job id or any unique fragment of it")
    m.add_argument("status")
    m.add_argument("note", nargs="?")
    d = sub.add_parser("discover", help="find job boards by company name (Greenhouse, Lever, Ashby, SmartRecruiters)")
    d.add_argument("names", nargs="*", help="company names")
    d.add_argument("--file", help="text file with one company name per line")
    d.add_argument("--add", action="store_true", help="save the boards it finds")
    c = sub.add_parser("crawl", help="find company boards in bulk from Common Crawl's public index (Greenhouse, Workday, ...)")
    c.add_argument("--systems", help="comma-separated platforms (default: all). Choices: greenhouse, lever, ashby, smartrecruiters, "
                                     "workable, recruitee, bamboohr, rippling, gem, personio, workday")
    c.add_argument("--crawls", type=int, default=4, help="how many recent Common Crawl crawls to read (default 4)")
    c.add_argument("--keep-all", action="store_true", help="keep every live board, not only those with a posting matching your titles")
    c.add_argument("--recheck", action="store_true", help="ask boards that were checked recently again")
    c.add_argument("--add", action="store_true", help="save the boards it finds")
    w = sub.add_parser("workday-add", help="add a Workday careers site from its web address")
    w.add_argument("url")
    w.add_argument("--name", help="label to show for the company")
    args = ap.parse_args()

    if not (ROOT / "config.yaml").exists():
        sys.exit("config.yaml not found. Copy config.example.yaml to config.yaml, then edit it (see the README).")
    if not (ROOT / "profile.json").exists():
        sys.exit("profile.json not found. Copy profile.example.json to profile.json and fill in the facts a resume can't "
                 "show (citizenship, clearance, do_not_assume), then run: python main.py profile")
    cfg = discovery.load_config(ROOT)
    profile = json.loads((ROOT / "profile.json").read_text(encoding="utf-8"))
    conn = db.connect(str(ROOT / "jobs.db"))
    # Every command takes the same four arguments, even those that ignore some of them.
    {"run": cmd_run, "list": cmd_list, "mark": cmd_mark, "profile": cmd_profile,
     "discover": cmd_discover, "workday-add": cmd_workday_add, "crawl": cmd_crawl}[args.cmd](args, cfg, profile, conn)


if __name__ == "__main__":
    main()
