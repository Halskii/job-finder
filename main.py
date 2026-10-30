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
import sys
import time
from datetime import date
from pathlib import Path

import yaml

import db
import discovery
import filters
import gov_sources
import scorer
import sources

ROOT = Path(__file__).parent
STATUSES = {"new", "saved", "applied", "skipped", "interview", "rejected", "offer"}


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


def load_resume() -> str:
    """Load the resume. Contact lines (email, phone, links) are removed before anything is sent to the API."""
    raw = read_resume_file()
    if not raw:
        return ""
    keep = []
    for line in raw.splitlines():
        low = line.lower()
        if "@" in low or "linkedin.com" in low or "github.com" in low:
            continue
        keep.append(line)
    return "\n".join(keep).strip()


def log(msg: str) -> None:
    print(msg, flush=True)


def collect(cfg: dict, conn=None) -> list[sources.Job]:
    delay = cfg.get("request_delay", 0.4)
    src = cfg.get("sources", {})
    jobs: list[sources.Job] = []
    plan = [("greenhouse", sources.fetch_greenhouse), ("lever", sources.fetch_lever), ("ashby", sources.fetch_ashby),
            ("smartrecruiters", sources.fetch_smartrecruiters)]
    for kind, fn in plan:
        for slug in src.get(kind, []) or []:
            try:
                got = fn(slug)
                log(f"  {kind:10s} {slug:18s} {len(got):4d} postings")
                jobs += got
            except Exception as e:  # bad slug, rate limit, network: skip and keep going
                log(f"  {kind:10s} {slug:18s} skipped ({type(e).__name__}: {str(e)[:70]})")
            time.sleep(delay)
    for entry in src.get("workday", []) or []:
        try:
            got = sources.fetch_workday(entry, cfg.get("workday_search_terms", ["software engineer"]),
                                        cfg.get("workday_max_pages", 3), delay)
            log(f"  workday    {entry.get('name', entry['tenant']):18s} {len(got):4d} postings")
            jobs += got
        except Exception as e:
            log(f"  workday    {entry.get('name', entry['tenant']):18s} skipped ({type(e).__name__}: {str(e)[:70]})")
    jobs += collect_feeds(cfg, conn)
    jobs += collect_government(cfg)
    return jobs


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
            log("  usajobs    skipped (add USAJOBS_API_KEY and USAJOBS_EMAIL to .env; free key at developer.usajobs.gov/apirequest)")
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


def collect_feeds(cfg: dict, conn) -> list[sources.Job]:
    """Aggregator feeds. Remotive asks for at most about four requests a day, so it is fetched at most once per
    `min_hours` (default 6), tracked in the database. Job pages link back to Remotive, as their terms require."""
    rem = (cfg.get("feeds") or {}).get("remotive") or {}
    if not rem.get("enabled") or conn is None:
        return []
    gap = float(rem.get("min_hours", 6)) * 3600
    if time.time() - float(db.get_meta(conn, "remotive_last_fetch") or 0) < gap:
        log("  remotive   skipped (fetched recently; Remotive asks for at most about 4 requests a day)")
        return []
    db.set_meta(conn, "remotive_last_fetch", str(time.time()))   # recorded before the call so failures don't cause retries
    try:
        got = sources.fetch_remotive(rem.get("categories", ["software-development"]))
        log(f"  remotive   {'(feed)':18s} {len(got):4d} postings")
        return got
    except Exception as e:
        log(f"  remotive   {'(feed)':18s} skipped ({type(e).__name__}: {str(e)[:70]})")
        return []


def cmd_run(args, cfg, profile, conn) -> None:
    log("Fetching postings...")
    jobs = collect(cfg, conn)
    patterns = filters.base_titles(cfg["titles"])
    gov_patterns = patterns + filters.base_titles(cfg.get("gov_titles", []))   # government titles read differently
    exclude = [w.lower() for w in cfg.get("exclude_title_words", [])]
    loc_excl = filters.compile_exclusions(cfg.get("exclude_locations", []))
    stats = {"fetched": len(jobs), "title": 0, "location": 0, "excluded": 0, "pay": 0, "age": 0, "new": 0, "seen": 0, "dupe": 0}

    for job in jobs:
        if not filters.title_ok(job.title, gov_patterns if job.source in gov_sources.GOV_SOURCES else patterns, exclude):
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
        if db.is_cross_source_duplicate(conn, job):   # same company + title already stored from another source
            stats["dupe"] += 1
            continue
        if job.source in sources.DETAIL_FILLERS:      # one extra request, only for title matches
            try:
                sources.fill_detail(job)
                time.sleep(cfg.get("request_delay", 0.4))
            except Exception:
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
        f"excluded locations {stats['excluded']} | already seen {stats['seen']} | duplicates {stats['dupe']} | too old {stats['age']} | "
        f"below pay {stats['pay']} | NEW {stats['new']}")

    if args.no_llm:
        log("Skipping LLM scoring (--no-llm). Jobs are stored; run again without the flag to score them.")
        return

    load_env()
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        log("\nNo ANTHROPIC_API_KEY found (set it in .env or your environment). Skipping scoring.")
        return
    import anthropic
    client = anthropic.Anthropic(api_key=key)
    resume_text = load_resume()
    log(f"Resume context: {'loaded' if resume_text else 'none (add resume.txt)'}")
    scfg = cfg.get("scoring", {})
    limit = args.limit or scfg.get("max_per_run", 40)
    loc_excl = filters.compile_exclusions(cfg.get("exclude_locations", []))
    # jobs stored before an exclusion was added must not be scored (that would waste API credits)
    todo = visible(db.unscored(conn, limit * 4), loc_excl)[:limit]
    log(f"\nScoring {len(todo)} jobs with {scfg.get('model')}...")
    tok_in = tok_out = 0
    for i, row in enumerate(todo, 1):
        try:
            res = scorer.score_job(client, scfg["model"], profile, row, cfg.get("min_pay", 0), resume_text)
            db.save_score(conn, row["id"], res["fit_score"], res["verdict"], res)
            tok_in += res.get("_in", 0)
            tok_out += res.get("_out", 0)
            log(f"  [{i}/{len(todo)}] {res['fit_score']:3d} {res['verdict']:5s} {row['company']} - {row['title']}")
        except Exception as e:
            log(f"  [{i}/{len(todo)}] error on {row['title']}: {type(e).__name__}: {str(e)[:80]}")
    price = scfg.get("price_per_million_tokens", {})
    cost = tok_in / 1e6 * price.get("input", 0) + tok_out / 1e6 * price.get("output", 0)
    log(f"\nTokens: {tok_in:,} in / {tok_out:,} out | estimated cost ${cost:.3f} "
        f"(uses the prices in config.yaml; check Anthropic's pricing page)")
    write_report(conn, cfg)


def visible(rows, loc_excl) -> list:
    """Drop stored rows whose location is excluded by the current config."""
    return [r for r in rows if filters.exclusion_ok(r["location"], loc_excl)]


def money(v) -> str:
    return f"${v:,}" if v else "?"


def write_report(conn, cfg) -> Path | None:
    scfg = cfg.get("scoring", {})
    size = scfg.get("report_size", 25)
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
    w = sub.add_parser("workday-add", help="add a Workday careers site from its web address")
    w.add_argument("url")
    w.add_argument("--name", help="label to show for the company")
    args = ap.parse_args()

    cfg = discovery.load_config(ROOT)
    profile = json.loads((ROOT / "profile.json").read_text(encoding="utf-8"))
    conn = db.connect(str(ROOT / "jobs.db"))
    {"run": cmd_run, "list": cmd_list, "mark": cmd_mark, "profile": cmd_profile,
     "discover": cmd_discover, "workday-add": cmd_workday_add}[args.cmd](args, cfg, profile, conn)


if __name__ == "__main__":
    main()
