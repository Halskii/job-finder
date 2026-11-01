"""The company job boards the shared fetch reads, kept in the database so every server sees the same list.

Boards come from `main.py crawl` (found in bulk), `discover` and `workday-add`. The boards typed into config.yaml are not here and are
never removed. Each fetch reports how every board answered; one that is gone (not found) several fetches in a row is dropped, and
the crawl can find it again if it ever comes back."""
from __future__ import annotations

import json
import time
from pathlib import Path

import yaml

MAX_FAILS = 3   # fetches in a row that found a board gone, before it is dropped


def ident(system: str, entry) -> str:
    """The board's identity: its lowercase name on its site, or host|tenant|site for a Workday site."""
    if system == "workday":
        return f"{entry['host']}|{entry['tenant']}|{entry['site']}".lower()
    return str(entry).lower()


def load(conn) -> dict:
    """Every stored board as a `sources` mapping: {system: [entry, ...]}, oldest first."""
    out: dict = {}
    for r in conn.execute("SELECT system, entry FROM boards ORDER BY added, ident").fetchall():
        out.setdefault(r["system"], []).append(json.loads(r["entry"]))
    return out


def add(conn, new: dict, origin: str = "crawl") -> int:
    """Store boards ({system: [entry, ...]}). Ones already stored are left as they are. Returns how many were new."""
    now, rows = time.time(), []
    for system, entries in (new or {}).items():
        for e in entries or []:
            rows.append((system, ident(system, e), json.dumps(e), now, origin))
    before = conn.execute("SELECT COUNT(*) AS n FROM boards").fetchone()["n"]
    conn.executemany("INSERT INTO boards (system, ident, entry, added, origin) VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING", rows)
    conn.commit()
    return conn.execute("SELECT COUNT(*) AS n FROM boards").fetchone()["n"] - before


def import_yaml(conn, root: Path, filename: str = "discovered_sources.yaml") -> int:
    """Once: copy the boards from the older discovered_sources.yaml into the database. The file is left alone. Returns how many were copied."""
    from db import get_meta, set_meta
    path = Path(root) / filename
    if get_meta(conn, "boards_yaml_imported") or not path.exists():
        return 0
    n = add(conn, yaml.safe_load(path.read_text(encoding="utf-8")) or {}, origin="yaml")
    set_meta(conn, "boards_yaml_imported", str(time.time()))
    return n


def record(conn, outcomes: list) -> None:
    """Note how each board answered a fetch: (system, entry, 'ok' | 'gone' | 'error'). Working resets its failures, gone adds one, and
    any other trouble (a timeout, a busy server) changes nothing. Boards that are not stored (the ones in config.yaml) are ignored."""
    now = time.time()
    ok = [(now, system, ident(system, e)) for system, e, status in outcomes if status == "ok"]
    gone = [(system, ident(system, e)) for system, e, status in outcomes if status == "gone"]
    conn.executemany("UPDATE boards SET fails = 0, last_ok = ? WHERE system = ? AND ident = ?", ok)
    conn.executemany("UPDATE boards SET fails = fails + 1 WHERE system = ? AND ident = ?", gone)
    conn.commit()


def prune(conn, limit: int = MAX_FAILS) -> list:
    """Drop the boards that were found gone `limit` fetches in a row. Returns [(system, entry), ...]."""
    rows = conn.execute("DELETE FROM boards WHERE fails >= ? RETURNING system, entry", (limit,)).fetchall()
    conn.commit()
    return [(r["system"], json.loads(r["entry"])) for r in rows]


def status_of(error) -> str:
    """How a board's fetch went: 'ok', 'gone' when the site said it does not exist (404 or 410), or 'error' for anything else."""
    if error is None:
        return "ok"
    code = getattr(getattr(error, "response", None), "status_code", None)
    return "gone" if code in (404, 410) else "error"
