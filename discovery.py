"""Find and add job boards: guess board addresses from company names, parse Workday careers URLs, and keep the
boards you add in discovered_sources.yaml (merged into config.yaml's `sources` at load time, so config.yaml and its
comments are never rewritten)."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import requests
import yaml

import ats_sources
import filters
import sources

DISCOVERED_FILE = "discovered_sources.yaml"
# system name -> fetcher. Order is the order they are tried and shown.
BOARD_FETCHERS = [   # (system, module, function name): the function is looked up when used, so tests can replace it
    ("greenhouse", sources, "fetch_greenhouse"),
    ("lever", sources, "fetch_lever"),
    ("ashby", sources, "fetch_ashby"),
    ("smartrecruiters", sources, "fetch_smartrecruiters"),
    ("workable", ats_sources, "fetch_workable"),
    ("recruitee", ats_sources, "fetch_recruitee"),
    ("bamboohr", ats_sources, "fetch_bamboohr"),
    ("rippling", ats_sources, "fetch_rippling"),
    ("gem", ats_sources, "fetch_gem"),
    ("personio", ats_sources, "fetch_personio"),
]
SYSTEMS = [(name, getattr(mod, fn)) for name, mod, fn in BOARD_FETCHERS]
LIST_KEYS = tuple(k for k, _ in SYSTEMS)
COMPANY_SUFFIXES = {"inc", "llc", "ltd", "corp", "corporation", "co", "company", "plc", "the"}
LOCALE_RE = re.compile(r"^[a-z]{2}(?:-[A-Za-z]{2})?$")


# ----------------------------------------------------------------------------- config merge
def load_discovered(root: Path) -> dict:
    path = Path(root) / DISCOVERED_FILE
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def merge_sources(base: dict, extra: dict) -> dict:
    """Union of two `sources` mappings. Slugs compare case-insensitively; Workday entries by host+tenant+site."""
    out = {k: list(v) for k, v in (base or {}).items()}
    for key in LIST_KEYS:
        have = {str(s).lower() for s in out.get(key, []) or []}
        for slug in (extra or {}).get(key, []) or []:
            if str(slug).lower() not in have:
                out.setdefault(key, []).append(slug)
                have.add(str(slug).lower())
    have_wd = {(e["host"], e["tenant"], e["site"]) for e in out.get("workday", []) or []}
    for e in (extra or {}).get("workday", []) or []:
        if (e["host"], e["tenant"], e["site"]) not in have_wd:
            out.setdefault("workday", []).append(e)
            have_wd.add((e["host"], e["tenant"], e["site"]))
    return out


def load_config(root: Path) -> dict:
    cfg = yaml.safe_load((Path(root) / "config.yaml").read_text(encoding="utf-8"))
    cfg["sources"] = merge_sources(cfg.get("sources") or {}, load_discovered(root))
    return cfg


def save_discovered(root: Path, new: dict) -> None:
    merged = merge_sources(load_discovered(root), new)
    header = ("# Boards added by `main.py discover --add` and `main.py workday-add`. Merged into config.yaml's sources\n"
              "# when the program starts. Safe to edit by hand.\n")
    body = yaml.safe_dump(merged, sort_keys=False, default_flow_style=None, allow_unicode=True)
    (Path(root) / DISCOVERED_FILE).write_text(header + body, encoding="utf-8")


# ----------------------------------------------------------------------------- slug discovery
def slug_candidates(name: str) -> list[str]:
    """'Palo Alto Networks, Inc.' -> ['paloaltonetworks', 'palo-alto-networks']. Never guesses from a single word
    of a longer name: 'palo' would be somebody else's board."""
    words = [w for w in re.findall(r"[a-z0-9]+", name.lower()) if w not in COMPANY_SUFFIXES]
    if not words:
        return []
    cands = ["".join(words)]
    if len(words) > 1:
        cands.append("-".join(words))
    return cands


@dataclass
class Hit:
    system: str
    slug: str
    jobs: list


def probe(name: str, delay: float = 0.4, systems=None) -> list[Hit]:
    """Try each job-board system with each guessed slug. A board counts as found when it lists at least one posting."""
    hits = []
    for system, fn in systems or SYSTEMS:
        for slug in slug_candidates(name):
            try:
                jobs = fn(slug)
            except (requests.RequestException, ValueError, KeyError):
                jobs = []
            time.sleep(delay)
            if jobs:
                hits.append(Hit(system, slug, jobs))
                break   # the hyphenated guess is only needed when the joined one failed
    return hits


def matching(jobs: list, cfg: dict) -> list:
    """Postings that pass the same title, location, and known-pay filters the main run applies."""
    patterns = filters.base_titles(cfg["titles"])
    exclude = [w.lower() for w in cfg.get("exclude_title_words", [])]
    loc_excl = filters.compile_exclusions(cfg.get("exclude_locations", []))
    return [j for j in jobs
            if filters.title_ok(j.title, patterns, exclude)
            and filters.location_ok(j.location, cfg.get("us_only", True))
            and filters.exclusion_ok(j.location, loc_excl)
            and filters.pay_ok(j.pay_max, j.pay_min, cfg.get("min_pay", 0))]


# ----------------------------------------------------------------------------- Workday
def parse_workday_url(url: str, name: str | None = None) -> dict:
    """https://vanguard.wd5.myworkdayjobs.com/en-US/vanguard_external/... -> {host, tenant, site, name}."""
    u = urlparse(url.strip())
    host = (u.hostname or "").lower()
    if not host.endswith(".myworkdayjobs.com"):
        raise ValueError("That is not a myworkdayjobs.com address. Open the company's careers page, click a job, "
                         "and copy the address from the browser bar.")
    parts = [p for p in u.path.split("/") if p]
    if parts and LOCALE_RE.match(parts[0]):
        parts = parts[1:]
    if not parts:
        raise ValueError("Could not find the site name in that address (the part after the language code, e.g. /en-US/<site>).")
    tenant = host.split(".")[0]
    return {"name": name or tenant.capitalize(), "host": host, "tenant": tenant, "site": parts[0]}


def check_workday(entry: dict) -> int:
    """Total postings the site reports, via the same request the career site makes. Raises on failure."""
    url = f"https://{entry['host']}/wday/cxs/{entry['tenant']}/{entry['site']}/jobs"
    r = requests.post(url, json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""},
                      headers={**sources.UA, "Content-Type": "application/json"}, timeout=sources.TIMEOUT)
    r.raise_for_status()
    return int(r.json().get("total", 0))
