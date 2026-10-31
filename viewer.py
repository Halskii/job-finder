"""Local web viewer for the job reports. Usage: python viewer.py [--port 8765] [--no-open]

Reads reports/*.md, joins each entry to its stored posting in jobs.db (by the ID line), and serves a
sidebar + detail page on 127.0.0.1 only. Standard library only."""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import threading
import webbrowser
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import db
import reformat
import tailor
import tips
from errors import STATUS, explain

ROOT = Path(__file__).parent
REPORTS = ROOT / "reports"
DB_PATH = ROOT / "jobs.db"
STATUSES = ("new", "saved", "applied", "skipped", "interview", "offer", "accepted", "rejected", "withdrawn")

HEADER_RE = re.compile(r"^## (\d+) \| (.+)$")
META_RE = re.compile(r"^- Location: (.*) \| Pay: (.*) \| Posted: (.*)$")   # location itself may contain '|'
FIELD_RE = re.compile(r"^- ([A-Za-z ]+): ?(.*)$")


def parse_report(text: str) -> dict:
    """Turn a report written by main.write_report back into {'title': str, 'jobs': [...]}."""
    title, jobs, cur = "", [], None
    for line in text.splitlines():
        if line.startswith("# ") and not title:
            title = line[2:].strip()
            continue
        h = HEADER_RE.match(line)
        if h:
            cur = {"score": int(h.group(1)), "heading": h.group(2).strip()}
            jobs.append(cur)
            continue
        if cur is None:
            continue
        m = META_RE.match(line)
        if m:
            cur.update(location=m.group(1), pay=m.group(2), posted=m.group(3))
            continue
        f = FIELD_RE.match(line)
        if not f:
            continue
        key, val = f.group(1).strip().lower(), f.group(2).strip()
        if key == "verdict":
            v = re.match(r"\*\*(.+?)\*\*\s*\|?\s*(.*)$", val)
            cur["verdict"], cur["reason"] = (v.group(1), v.group(2)) if v else (val, "")
        elif key in ("matched", "missing required", "dealbreakers"):
            cur[key.replace(" ", "_")] = [] if val in ("n/a", "none", "none noted") else [s.strip() for s in val.split(",") if s.strip()]
        elif key == "resume angle":
            cur["resume_angle"] = val
        elif key == "link":
            cur["url"] = val
        elif key == "id":
            cur["id"] = val.strip("`")
    return {"title": title, "jobs": jobs}


def split_heading(heading: str) -> tuple[str, str]:
    """'Software Engineer at Stripe' -> ('Software Engineer', 'Stripe'); splits on the LAST ' at '."""
    title, sep, company = heading.rpartition(" at ")
    return (title, company) if sep else (heading, "")


def safe_url(url: str) -> str:
    return url if urlparse(url or "").scheme in ("http", "https") else ""


def report_names() -> list[str]:
    return sorted((p.name for p in REPORTS.glob("*.md")), reverse=True) if REPORTS.is_dir() else []


def load_report(name: str) -> dict | None:
    if name not in report_names():   # whitelist: also blocks path traversal
        return None
    data = parse_report((REPORTS / name).read_text(encoding="utf-8"))
    conn = db.connect(str(DB_PATH))   # also upgrades an older database (adds the applied_at column)
    try:
        for j in data["jobs"]:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (j.get("id"),)).fetchone() if j.get("id") else None
            title, company = split_heading(j["heading"])
            if row:
                title, company = row["title"], row["company"]
                j.setdefault("url", row["url"])
                j["description"] = row["description"] or ""
                j["status"], j["notes"], j["applied_at"] = row["status"] or "new", row["notes"] or "", row["applied_at"]
                j["can_reformat"] = reformat.can_reformat(row["id"], row["formatted"])
            else:
                j["description"], j["status"], j["notes"], j["applied_at"], j["can_reformat"] = "", "new", "", None, False
            j["title"], j["company"] = title, company
            j["url"] = safe_url(j.get("url", ""))
    finally:
        conn.close()
    return data


def job_dict(r) -> dict:
    """A stored job in the same shape load_report gives, so the page can show it like any report entry."""
    a = json.loads(r["analysis"] or "{}")
    pay = f"{r['pay_min'] or '?'} - {r['pay_max'] or '?'}" if r["pay_min"] or r["pay_max"] else "n/a - n/a"
    return {
        "id": r["id"], "score": r["score"] or 0, "title": r["title"], "company": r["company"],
        "location": r["location"] or "", "pay": pay, "posted": r["posted"] or "unknown",
        "verdict": r["verdict"] or "", "reason": a.get("reason", ""), "matched": a.get("matched", []),
        "missing_required": a.get("missing_required", []), "dealbreakers": a.get("dealbreakers", []),
        "resume_angle": a.get("resume_angle", ""), "url": safe_url(r["url"] or ""), "description": r["description"] or "",
        "status": r["status"], "notes": r["notes"] or "", "applied_at": r["applied_at"],
        "can_reformat": reformat.can_reformat(r["id"], r["formatted"]),
    }


def reformat_for(job_id: str) -> dict:
    conn = db.connect(str(DB_PATH))
    try:
        return reformat.reformat_job(conn, job_id)
    finally:
        conn.close()


def load_saved() -> dict:
    """The saved jobs from jobs.db, shaped like a report so the page can show them like one."""
    conn = db.connect(str(DB_PATH))
    try:
        return {"title": "Saved jobs", "jobs": [job_dict(r) for r in db.saved(conn)]}
    finally:
        conn.close()


def load_trash() -> dict:
    """The jobs you discarded, shaped like a report."""
    conn = db.connect(str(DB_PATH))
    try:
        return {"title": "Trash", "jobs": [job_dict(r) for r in db.trashed(conn)]}
    finally:
        conn.close()


def load_job(job_id: str) -> dict | None:
    conn = db.connect(str(DB_PATH))
    try:
        r = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return job_dict(r) if r else None
    finally:
        conn.close()


def load_prep() -> dict:
    """The jobs that have saved interview prep, newest first, shaped like a report."""
    conn = db.connect(str(DB_PATH))
    try:
        return {"title": "Interview prep", "jobs": [{**job_dict(r), "prep_created": r["prep_created"]} for r in db.jobs_with_tips(conn)]}
    finally:
        conn.close()


def mark_status(job_id: str, status: str) -> dict:
    """Set a job's status. An applied-type status records today as the date applied (unless one is already stored);
    any other status removes the job from the tracker. Returns how many rows changed and the resulting date."""
    conn = db.connect(str(DB_PATH))
    try:
        n = db.set_status_exact(conn, job_id, status)
        row = conn.execute("SELECT applied_at FROM jobs WHERE id=?", (job_id,)).fetchone()
        return {"updated": n, "applied_at": row["applied_at"] if row else None}
    finally:
        conn.close()


def parse_day(value) -> date | None:
    """A YYYY-MM-DD string that is a real date and not in the future, else None."""
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        day = date.fromisoformat(value)
    except ValueError:
        return None
    return day if day <= date.today() else None


def set_applied_date(job_id: str, day: date) -> int:
    conn = db.connect(str(DB_PATH))
    try:
        return db.set_applied_date(conn, job_id, day)
    finally:
        conn.close()


def parse_any_day(value) -> date | None:
    """A YYYY-MM-DD string that is a real date (a future one is fine: interviews get scheduled), else None."""
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def round_fields(body: dict) -> tuple:
    """The name, date, result and notes of an interview round from a request, cleaned. ValueError says what is wrong."""
    kind = " ".join(tailor.clean_request(body.get("kind")).split())[:80]
    notes = " ".join(tailor.clean_request(body.get("notes")).split())[:1000]
    day = None
    if body.get("date"):
        parsed = parse_any_day(body["date"])
        if parsed is None:
            raise ValueError("That date is not valid. Use the date picker.")
        day = parsed.isoformat()
    outcome = str(body.get("outcome") or "scheduled")
    if outcome not in db.ROUND_OUTCOMES:
        raise ValueError("That result is not one of the choices.")
    return kind, day, outcome, notes


def round_action(body: dict) -> dict:
    """Add, update, or delete one interview round. LookupError for a job or round that is not there, ValueError for bad input."""
    action = body.get("action")
    conn = db.connect(str(DB_PATH))
    try:
        if action == "add":
            job_id = str(body["id"])
            row = conn.execute("SELECT status, applied_at FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or not row["applied_at"]:
                raise LookupError("Only jobs in your tracker can have interview rounds. Mark it as applied first.")
            kind, day, outcome, notes = round_fields(body)
            round_id = db.add_round(conn, job_id, kind, day, outcome, notes)
            if row["status"] == "applied":   # a first round means you are interviewing
                db.set_status_exact(conn, job_id, "interview")
            return {"id": round_id}
        if action in ("update", "delete"):
            round_id = int(body["round_id"])
            if action == "delete":
                n = db.delete_round(conn, round_id)
            else:
                kind, day, outcome, notes = round_fields(body)
                if not kind:
                    raise ValueError("Give the round a name.")
                n = db.update_round(conn, round_id, kind, day, outcome, notes)
            if not n:
                raise LookupError("That round no longer exists. Reload the page.")
            return {"updated": n}
        raise ValueError("That action is not supported.")
    finally:
        conn.close()


def load_tracker() -> dict:
    """Counts for this week / this month / all time, and every tracked application, newest first."""
    conn = db.connect(str(DB_PATH))
    try:
        rounds = db.rounds_by_job(conn)
        return {
            "stats": db.application_stats(conn),
            "applications": [{**dict(r), "url": safe_url(r["url"] or ""), "rounds": rounds.get(r["id"], [])} for r in db.applications(conn)],
        }
    finally:
        conn.close()


_tailoring = threading.Lock()   # one tailoring at a time: each one is a paid API call
_tipping = threading.Lock()     # likewise for interview tips


def job_exists(job_id: str) -> bool:
    conn = db.connect(str(DB_PATH))
    try:
        return conn.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone() is not None
    finally:
        conn.close()


def tips_events(job_id: str, refresh: bool = False):
    """The events of one interview prep run (see tips.stream_tips). A failure becomes an error event, so the page can show it."""
    conn = db.connect(str(DB_PATH))
    try:
        yield from tips.stream_tips(conn, job_id, refresh)
    except Exception as ex:   # missing key, a failed API call, a bad answer: show the reason on the page
        message, code = explain(ex)
        yield {"type": "error", "error": message, "code": code}
    finally:
        conn.close()


def tailor_for(job_id: str, resume_request: str = "", letter_request: str = "") -> dict:
    conn = db.connect(str(DB_PATH))
    try:
        return tailor.tailor_job(conn, job_id, resume_request=resume_request, letter_request=letter_request)
    finally:
        conn.close()


class Handler(BaseHTTPRequestHandler):
    server_version = "JobViewer"

    def log_message(self, fmt, *args) -> None:   # keep the terminal quiet
        pass

    def _host_ok(self) -> bool:
        # Blocks DNS-rebinding: only answer requests addressed to the loopback name we serve on.
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        return host in ("127.0.0.1", "localhost", "::1")

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8")

    def _error(self, code: int, message: str, kind: str) -> None:
        """An error response: {"error": a sentence for the person using the page, "code": a short name for programs}."""
        self._json(code, {"error": message, "code": kind})

    def _fail(self, ex: BaseException) -> None:
        """Any exception the handlers did not expect, as a friendly message. The details go to the terminal, never to the page."""
        message, kind = explain(ex)
        try:
            self._error(STATUS.get(kind, 422), message, kind)
        except OSError:   # the page went away
            pass

    def _bad_request(self) -> None:
        self._error(400, "That request was not understood. Reload the page and try again.", "bad_request")

    def _tailor(self, job_id: str, resume_request: str, letter_request: str) -> None:
        """Make the tailored resume and cover letter for one job. Takes a minute or two, since it waits on the model."""
        if not _tailoring.acquire(blocking=False):
            return self._error(409, "Already working on another job. Wait for it to finish.", "busy")
        try:
            self._json(200, tailor_for(job_id, resume_request, letter_request))
        except LookupError:
            self._error(404, "We could not find that job. Reload the page and try again.", "not_found")
        except Exception as ex:   # missing key or resume, no browser for the PDF, a failed API call: say what to do, in plain words
            self._fail(ex)
        finally:
            _tailoring.release()

    def _tips(self, job_id: str, refresh: bool) -> None:
        """Interview prep for a job, streamed to the page as it is written, one JSON event per line. A saved answer comes at once."""
        if not job_exists(job_id):
            return self._error(404, "We could not find that job. Reload the page and try again.", "not_found")
        if not _tipping.acquire(blocking=False):
            return self._error(409, "Interview prep is already being written for another job. Wait for it to finish.", "busy")
        try:
            self.send_response(200)
            for k, v in (("Content-Type", "application/x-ndjson; charset=utf-8"), ("Cache-Control", "no-store"),
                         ("X-Content-Type-Options", "nosniff"), ("Content-Security-Policy", "default-src 'none'")):
                self.send_header(k, v)
            self.end_headers()   # no Content-Length: the response ends when the connection closes
            alive = True
            for event in tips_events(job_id, refresh):
                if alive:
                    try:
                        self.wfile.write((json.dumps(event) + "\n").encode("utf-8"))
                        self.wfile.flush()
                    except OSError:   # the page went away; keep going so the answer is still written and saved
                        alive = False
        finally:
            _tipping.release()

    def _round(self, body: dict) -> None:
        try:
            self._json(200, round_action(body))
        except (ValueError, KeyError, TypeError) as ex:   # KeyError is a LookupError, so it must be caught before the 404 below
            if isinstance(ex, ValueError):
                self._error(400, str(ex), "invalid")
            else:
                self._bad_request()
        except LookupError as ex:
            self._error(404, str(ex), "not_found")
        except Exception as ex:
            self._fail(ex)

    def do_GET(self) -> None:
        try:
            self._get()
        except Exception as ex:   # never let a stack trace or a bare dropped connection reach the page
            self._fail(ex)

    def do_POST(self) -> None:
        try:
            self._post()
        except Exception as ex:
            self._fail(ex)

    def _get(self) -> None:
        """Serve the page and the read-only JSON endpoints it loads its data from."""
        if not self._host_ok():
            return self._error(403, "This page can only be opened from this computer.", "bad_host")
        path = unquote(urlparse(self.path).path)
        if path == "/":
            return self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        if path == "/api/reports":
            return self._json(200, report_names())
        if path == "/api/tracker":
            return self._json(200, load_tracker())
        if path == "/api/saved":
            return self._json(200, load_saved())
        if path == "/api/prep":
            return self._json(200, load_prep())
        if path == "/api/trash":
            return self._json(200, load_trash())
        if path.startswith("/api/job/"):
            job = load_job(path[len("/api/job/"):])
            return self._json(200, job) if job else self._error(404, "We could not find that job. Reload the page and try again.", "not_found")
        if path.startswith("/api/report/"):
            data = load_report(path[len("/api/report/"):])
            return self._json(200, data) if data else self._error(404, "That report does not exist.", "not_found")
        self._error(404, "That page does not exist.", "not_found")

    def _post(self) -> None:
        """The writes the page can make: change a job's status or application date, track interview rounds, get interview tips,
        or write a tailored resume and cover letter."""
        if not self._host_ok():
            return self._error(403, "This page can only be opened from this computer.", "bad_host")
        # Requiring JSON means a web page on another origin can't send this without a CORS preflight, which we never grant.
        path = urlparse(self.path).path
        if path not in ("/api/mark", "/api/applied-date", "/api/tailor", "/api/round", "/api/tips", "/api/reformat") or "application/json" not in (self.headers.get("Content-Type") or ""):
            return self._bad_request()
        try:
            body = json.loads(self.rfile.read(max(0, min(int(self.headers.get("Content-Length") or 0), 40_000))))   # room for two 2,000-character requests, even all non-ASCII
            if not isinstance(body, dict):
                raise ValueError
            if path == "/api/round":
                return self._round(body)
            job_id = str(body["id"])
            if path == "/api/mark":
                status = str(body["status"])
            elif path == "/api/applied-date":
                day = parse_day(body["date"])
        except (ValueError, KeyError, TypeError):
            return self._bad_request()
        if path == "/api/tips":
            return self._tips(job_id, bool(body.get("refresh")))
        if path == "/api/reformat":
            try:
                return self._json(200, reformat_for(job_id))
            except LookupError:
                return self._error(404, "We could not find that job. Reload the page and try again.", "not_found")
        if path == "/api/tailor":
            return self._tailor(job_id, tailor.clean_request(body.get("resume_request")), tailor.clean_request(body.get("letter_request")))
        if path == "/api/applied-date":
            if day is None:
                return self._error(400, "Pick a real date that is not in the future.", "invalid")
            return self._json(200, {"updated": set_applied_date(job_id, day)})
        if status not in STATUSES:
            return self._error(400, "That is not a status this program knows.", "invalid")
        self._json(200, mark_status(job_id, status))


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Job matches</title>
<script>try{if(localStorage.getItem("theme")==="light")document.documentElement.dataset.theme="light"}catch(e){}</script>
<style>
:root{color-scheme:dark;--bg:#202124;--panel:#292a2d;--ink:#e8eaed;--mute:#9aa0a6;--line:#3c4043;--accent:#8ab4f8;--good:#81c995;--warn:#fdd663;--bad:#f28b82;--on-accent:#202124}   /* Chrome's dark theme */
:root[data-theme="light"]{color-scheme:light;--bg:#f8f9fa;--panel:#fff;--ink:#202124;--mute:#5f6368;--line:#dadce0;--accent:#1a73e8;--good:#188038;--warn:#b06000;--bad:#d93025;--on-accent:#fff}
#theme-toggle{position:absolute;top:10px;right:22px;z-index:3;display:flex;border:1px solid var(--line);border-radius:99px;background:var(--panel);padding:2px}
#theme-toggle button{display:flex;align-items:center;gap:5px;border:0;background:none;color:var(--mute);font:inherit;font-size:13px;padding:4px 11px;border-radius:99px;cursor:pointer}
#theme-toggle button:hover{color:var(--ink)}#theme-toggle button[aria-pressed="true"]{background:var(--accent);color:var(--on-accent);font-weight:600}
#theme-toggle svg{display:block}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;height:100vh;display:flex}
nav{width:340px;flex:none;border-right:1px solid var(--line);background:var(--panel);display:flex;flex-direction:column}
nav header{padding:12px;border-bottom:1px solid var(--line);display:grid;gap:8px}
nav select,nav input{width:100%;padding:7px 9px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--ink);font:inherit}
#list{overflow:auto;flex:1}
.item{display:flex;gap:10px;padding:10px 12px;border-bottom:1px solid var(--line);cursor:pointer;text-decoration:none;color:inherit}
.item:hover{background:var(--bg)}.item.sel{background:var(--bg);box-shadow:inset 3px 0 var(--accent)}
.score{flex:none;width:38px;height:38px;border-radius:8px;display:grid;place-items:center;font-weight:700;border:1.5px solid var(--mute)}
.score.hi{color:var(--good);border-color:var(--good)}.score.mid{color:var(--warn);border-color:var(--warn)}
.item .t{font-weight:600;line-height:1.3}.item .c{color:var(--mute);font-size:13px}
.item.done{opacity:.55}
#stage{flex:1;min-width:0;position:relative;display:flex;flex-direction:column}
main{flex:1;min-height:0;overflow:auto;padding:52px 32px 24px}
.card{position:relative}.card h1{padding-right:56px}
.save-btn{position:absolute;top:0;right:0;display:flex;align-items:center;padding:6px 9px}.save-btn svg{display:block}.save-btn.on svg{fill:currentColor}
#trash-btn{position:absolute;top:10px;left:12px;z-index:2;display:flex;align-items:center;gap:6px;padding:6px 10px}
#trash-btn svg{display:block}#trash-count{font-size:13px;font-weight:600}
.card{max-width:860px;margin:0 auto}
h1{font-size:22px;margin:0 0 4px}.sub{color:var(--mute);margin:0 0 14px}
.row{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0}
.btn{padding:7px 12px;border:1px solid var(--line);border-radius:6px;background:var(--panel);color:var(--ink);font:inherit;cursor:pointer;text-decoration:none}
.btn:hover{border-color:var(--accent)}.btn.primary{background:var(--accent);border-color:var(--accent);color:var(--on-accent)}
.btn.on{border-color:var(--accent);color:var(--accent);font-weight:600}
.chip{display:inline-block;padding:2px 9px;border-radius:99px;border:1px solid var(--line);font-size:13px;margin:0 6px 6px 0}
.chip.good{color:var(--good);border-color:var(--good)}.chip.bad{color:var(--bad);border-color:var(--bad)}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--mute);margin:22px 0 6px}
.verdict{display:inline-block;font-weight:700;text-transform:uppercase;font-size:12px;letter-spacing:.05em;margin-right:8px}
.verdict.apply{color:var(--good)}.verdict.maybe{color:var(--warn)}.verdict.skip{color:var(--bad)}
#desc{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:6px 20px 14px;overflow-wrap:anywhere}
#desc h2{font-size:17px;text-transform:none;letter-spacing:0;color:var(--ink);margin:20px 0 4px}
#desc h3{font-size:15px;margin:16px 0 2px}#desc p{margin:8px 0}#desc ul{margin:6px 0;padding-left:24px}#desc li{margin:4px 0}
.empty{color:var(--mute);text-align:center;margin-top:20vh}
.tabs{display:grid;grid-template-columns:1fr 1fr;gap:6px}
.tab{padding:7px 10px;border:1px solid var(--line);border-radius:6px;text-align:center;text-decoration:none;color:var(--ink);font-weight:600}
.tab.on{background:var(--accent);border-color:var(--accent);color:var(--on-accent)}
.btn.apply{background:var(--good);border-color:var(--good);color:var(--on-accent);font-weight:600}
.btn.apply:hover{filter:brightness(1.1)}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin:14px 0 8px}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.stat .k{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--mute)}
.stat .n{font-size:38px;font-weight:700;line-height:1.1;margin:2px 0}
.stat .d{color:var(--mute);font-size:13px}
table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line);border-radius:8px;overflow:hidden}
th{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--mute);text-align:left;padding:8px 10px;border-bottom:1px solid var(--line)}
td{padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:middle}
tr:last-child td{border-bottom:0}
td select.status{padding:4px 6px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--ink);font:inherit}
td select.s-interview{border-color:var(--accent)}td select.s-offer,td select.s-accepted{border-color:var(--good);color:var(--good);font-weight:600}
td select.s-rejected{border-color:var(--bad);color:var(--bad)}td select.s-withdrawn{color:var(--mute)}
td input[type=date]{padding:4px 6px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--ink);font:inherit}
td a{color:var(--accent)}
.muted{color:var(--mute)}
dialog{border:1px solid var(--line);border-radius:10px;background:var(--panel);color:var(--ink);padding:20px 22px;width:min(560px,92vw)}
dialog::backdrop{background:rgba(0,0,0,.4)}
dialog h3{margin:0 0 4px;font-size:18px}dialog label{display:block;font-weight:600;margin:14px 0 4px}
dialog textarea{width:100%;min-height:84px;padding:8px 10px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--ink);font:inherit;resize:vertical}
dialog .count{text-align:right;font-size:12px;margin-top:2px}
dialog .actions{display:flex;justify-content:flex-end;gap:8px;margin-top:16px}
.btn:disabled{cursor:progress}
.btn.danger{background:var(--bad);border-color:var(--bad);color:var(--on-accent)}.btn.danger:hover{filter:brightness(1.1)}
#toasts{position:fixed;left:50%;transform:translateX(-50%);bottom:18px;display:grid;gap:8px;z-index:50;width:min(520px,92vw);pointer-events:none}
.toast{pointer-events:auto;display:flex;gap:10px;align-items:flex-start;background:var(--panel);color:var(--ink);border:1px solid var(--line);border-left:4px solid var(--accent);border-radius:8px;padding:10px 12px;box-shadow:0 8px 28px rgba(0,0,0,.35)}
.toast.error{border-left-color:var(--bad)}.toast.ok{border-left-color:var(--good)}
.toast .tt{font-weight:600}.toast .td{color:var(--mute);font-size:14px;margin-top:1px}
.toast button{margin-left:auto;background:none;border:0;color:var(--mute);font-size:20px;line-height:1;cursor:pointer;padding:0 2px}
#confirm-dlg{width:min(460px,92vw)}#confirm-dlg h3{margin:0}#confirm-dlg p{margin:8px 0 0;color:var(--mute)}
#prep-panel{display:none;width:440px;flex:none;border-left:1px solid var(--line);background:var(--panel);overflow:auto;padding:16px 18px}
#prep-panel.open{display:block}
@media (max-width:1150px){#prep-panel{position:fixed;right:0;top:0;bottom:0;width:min(480px,100vw);z-index:5;box-shadow:-8px 0 24px rgba(0,0,0,.35)}}
.prep-head{display:flex;justify-content:space-between;gap:10px;align-items:flex-start}.prep-head h3{margin:0;font-size:17px}
#prep-text h2{margin-top:18px}#prep-text h3{font-size:15px;margin:14px 0 2px}#prep-text ul{margin:4px 0;padding-left:20px}#prep-text li{margin:5px 0}#prep-text p{margin:6px 0}
.prep-status{display:flex;gap:8px;align-items:center;color:var(--mute);margin:8px 0;min-height:20px}
.disclaimer{border:1px solid var(--warn);color:var(--warn);border-radius:8px;padding:9px 12px;margin:10px 0;font-size:14px}
.stage{margin:8px 0}
.tag{font-size:11px;border:1px solid var(--line);color:var(--mute);border-radius:99px;padding:1px 8px;margin-left:8px;font-weight:400}
.tag.rep{color:var(--good);border-color:var(--good)}
tr.rounds td{background:var(--bg)}
.round{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0}
.round input,.round select{flex:1 1 130px}.round input.round-notes{flex:3 1 220px}
.round input,.round select{padding:5px 7px;border:1px solid var(--line);border-radius:6px;background:var(--panel);color:var(--ink);font:inherit;min-width:0}
table.trk{table-layout:fixed;width:100%}table.trk td{overflow-wrap:break-word}table.trk td .btn{padding:4px 6px;font-size:13px;white-space:nowrap}table.trk td select.status{width:100%;min-width:0}
td.actions-cell .btn{display:block;width:100%;padding:4px 8px;margin:0 0 4px}td.actions-cell .btn:last-child{margin:0}
.spinner{display:inline-block;width:14px;height:14px;border:2px solid var(--line);border-top-color:var(--accent);border-radius:50%;animation:spin .8s linear infinite;vertical-align:-2px}
@keyframes spin{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion:reduce){.spinner{animation-duration:2.4s}}
@media print{
  :root{color-scheme:light;--bg:#fff;--panel:#fff;--ink:#000;--mute:#555;--line:#ccc;--accent:#1a56db;--good:#137333;--warn:#8a5a00;--bad:#b3261e}
  body{display:block;height:auto}#stage{display:block}main{overflow:visible;padding:0}
  nav,#trash-btn,#theme-toggle,.save-btn,#prep-panel,#toasts,.row{display:none!important}
  #desc{border:0;padding:0}.chip{border-color:#999}
}
@media (max-width:760px){body{flex-direction:column;height:auto}nav{width:auto;max-height:45vh}main{padding:52px 16px 16px}}
</style></head><body>
<nav><header><div class="tabs"><a class="tab" id="tab-matches" href="#">Matches</a><a class="tab" id="tab-saved" href="#saved">Saved <span id="scount"></span></a><a class="tab" id="tab-prep" href="#prep">Interview Prep <span id="pcount"></span></a><a class="tab" id="tab-tracker" href="#tracker">Tracker <span id="tcount"></span></a></div><select id="report" aria-label="Report"></select><input id="q" placeholder="Filter by title or company" aria-label="Filter"></header><div id="list"></div></nav>
<div id="toasts" role="status" aria-live="polite"></div>
<dialog id="confirm-dlg" aria-labelledby="confirm-title" aria-describedby="confirm-msg"><h3 id="confirm-title"></h3><p id="confirm-msg"></p>
<div class="actions"><button class="btn" id="confirm-cancel"></button><button class="btn" id="confirm-ok"></button></div></dialog>
<dialog id="tailor-dlg"><form method="dialog">
<h3>Tailor resume + cover letter</h3><div class="muted" id="tailor-for"></div>
<div class="muted" style="margin-top:8px">Both boxes are optional. Leave them empty to use the defaults. Your requests are followed as long as they stay true to your resume and profile.</div>
<label for="tailor-resume">Resume request (optional)</label>
<textarea id="tailor-resume" maxlength="2000" placeholder="e.g. Lead with my Kubernetes work and keep the skills section short"></textarea>
<div class="muted count" id="tailor-resume-n"></div>
<label for="tailor-letter">Cover letter request (optional)</label>
<textarea id="tailor-letter" maxlength="2000" placeholder="e.g. Mention that I am open to relocating, and keep it under 250 words"></textarea>
<div class="muted count" id="tailor-letter-n"></div>
<div class="muted" style="margin-top:10px;font-size:12px">Up to 2,000 characters in each box, plain text only. Control and invisible characters are removed.</div>
<div class="actions"><button class="btn" value="cancel">Cancel</button><button class="btn primary" value="ok">Tailor</button></div>
</form></dialog>
<datalist id="round-kinds"><option value="Recruiter screen"><option value="Phone screen"><option value="Technical screen"><option value="Coding interview"><option value="System design"><option value="Take-home"><option value="Hiring manager"><option value="Behavioral"><option value="Onsite / final loop"></datalist>
<div id="stage"><div id="theme-toggle" role="group" aria-label="Color theme">
<button type="button" data-theme="dark" aria-pressed="true"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>Dark</button>
<button type="button" data-theme="light" aria-pressed="false"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>Light</button></div>
<button class="btn" id="trash-btn" aria-label="Trash" title="Trash: jobs you discarded"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg><span id="trash-count"></span></button>
<main id="main"><div class="empty">Select a job from the list.</div></main></div>
<aside id="prep-panel" aria-label="Interview prep">
<div class="prep-head"><h3 id="prep-title"></h3><button class="btn" id="prep-close" title="Close this panel">&times;</button></div>
<div class="disclaimer" id="prep-disclaimer"></div>
<div class="prep-status" id="prep-status"></div>
<div id="prep-text"></div><div id="prep-sources"></div><div class="muted" id="prep-meta" style="margin-top:12px"></div>
<div class="row"><button class="btn" id="prep-regen" disabled>Regenerate</button></div>
</aside>
<script>
const $=s=>document.querySelector(s);
const el=(tag,props={},...kids)=>{const e=document.createElement(tag);Object.assign(e,props);kids.forEach(k=>e.append(k));return e};
let jobs=[],reportName="",tracker=null,lastJob="";
const APPLIED=["applied","interview","offer","accepted","rejected","withdrawn"];
const STATUS_LABEL={applied:"Applied",interview:"Interview",offer:"Offer received",accepted:"Offer accepted",rejected:"Rejected",withdrawn:"Withdrawn"};
const fmt=(iso,opts)=>new Date(iso+"T00:00:00").toLocaleDateString(undefined,opts);

class ApiError extends Error{constructor(message,status,code){super(message);this.status=status;this.code=code}}   // its message is always fit to show a person
const NETWORK="Can't reach the viewer. Check that it's still running, then try again.";
const BY_STATUS={400:"That didn't go through. Reload the page and try again.",403:"This page can only be used from this computer.",
  404:"We couldn't find that. It may have been removed. Reload the page.",409:"Something else is already running. Wait for it to finish, then try again.",
  429:"Too many requests right now. Wait a moment and try again."};
const SERVER_TROUBLE="Something went wrong on our side. Try again in a moment.";
function friendly(e){   // the sentence to show a person for any error; never a technical message
  if(e instanceof ApiError)return e.message;
  if(e instanceof TypeError)return NETWORK;   // fetch could not reach the server
  return "Something unexpected happened. Reload the page and try again.";
}
async function api(path,opts){
  let r;try{r=await fetch(path,opts)}catch(e){throw new ApiError(NETWORK,0,"network")}
  const d=await r.json().catch(()=>null);
  if(!r.ok)throw new ApiError((d&&d.error)||BY_STATUS[r.status]||SERVER_TROUBLE,r.status,d&&d.code);
  return d;
}
function toast(title,detail="",kind="error"){   // a message in the corner, in the same style as the rest of the page
  const box=$("#toasts"),t=el("div",{className:"toast "+kind},
    el("div",{},el("div",{className:"tt",textContent:title}),...(detail?[el("div",{className:"td",textContent:detail})]:[])),
    el("button",{textContent:"\u00d7",title:"Dismiss",onclick:()=>t.remove()}));
  t.querySelector("button").setAttribute("aria-label","Dismiss");
  box.append(t);while(box.children.length>4)box.firstChild.remove();
  setTimeout(()=>t.remove(),kind==="error"?12000:5000);
}
function fail(title,e){console.error(title,e);toast(title,friendly(e),"error")}
function ask({title,message,confirmText="OK",cancelText="Cancel",danger=false}){   // a styled confirmation; resolves true or false
  return new Promise(resolve=>{
    const dlg=$("#confirm-dlg"),ok=$("#confirm-ok"),no=$("#confirm-cancel");
    $("#confirm-title").textContent=title;$("#confirm-msg").textContent=message;
    ok.textContent=confirmText;no.textContent=cancelText;ok.className="btn "+(danger?"danger":"primary");
    let done=false;
    const finish=v=>{if(done)return;done=true;if(dlg.open)dlg.close();resolve(v)};
    ok.onclick=()=>finish(true);no.onclick=()=>finish(false);
    dlg.oncancel=e=>{e.preventDefault();finish(false)};   // the Escape key
    dlg.onclick=e=>{const r=dlg.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)finish(false)};   // a click outside the box
    dlg.showModal();(danger?no:ok).focus();   // for something destructive, Enter should not confirm
  });
}
addEventListener("unhandledrejection",e=>{e.preventDefault();console.error(e.reason);toast("Something went wrong",friendly(e.reason))});
addEventListener("error",e=>{console.error(e.error||e.message);toast("Something went wrong","The page ran into a problem. If it keeps happening, reload it.")});

const SAVED="__saved__",PREP="__prep__",TRASH="__trash__";   // the report menu's entries for jobs you saved and jobs with interview prep, from any report
const EMPTY={[SAVED]:"Nothing saved yet. Open a job and click Save for later.",[PREP]:"No interview prep yet. Open a job and click Interview prep.",[TRASH]:"The trash is empty. Jobs you discard show up here."};
let reportNames=[],savedCount=0,prepCount=0,trashCount=0;
function markTabs(inTracker){   // which of the four tabs is lit
  $("#tab-tracker").classList.toggle("on",inTracker);
  $("#tab-saved").classList.toggle("on",!inTracker&&reportName===SAVED);
  $("#tab-prep").classList.toggle("on",!inTracker&&reportName===PREP);
  $("#tab-matches").classList.toggle("on",!inTracker&&reportName!==SAVED&&reportName!==PREP&&reportName!==TRASH);
  $("#trash-btn").classList.toggle("on",!inTracker&&reportName===TRASH);
}
function fillReportMenu(){
  $("#report").replaceChildren(el("option",{value:SAVED,textContent:"★ Saved jobs"+(savedCount?" ("+savedCount+")":"")}),
    el("option",{value:PREP,textContent:"Interview Prep"+(prepCount?" ("+prepCount+")":"")}),
    el("option",{value:TRASH,textContent:"Trash"+(trashCount?" ("+trashCount+")":"")}),
    ...reportNames.map(n=>el("option",{value:n,textContent:n.replace(/\.md$/,"")})));
  $("#report").value=reportName||reportNames[0]||SAVED;
}
async function refreshTrash(){
  try{trashCount=(await api("/api/trash")).jobs.length}catch(e){}
  $("#trash-count").textContent=trashCount||"";
  fillReportMenu();
}
async function refreshPrep(){
  try{prepCount=(await api("/api/prep")).jobs.length}catch(e){}
  $("#pcount").textContent=prepCount?"("+prepCount+")":"";
  fillReportMenu();
}
async function refreshSaved(){
  try{savedCount=(await api("/api/saved")).jobs.length}catch(e){}
  $("#scount").textContent=savedCount?"("+savedCount+")":"";
  fillReportMenu();
}
async function loadReports(){
  reportNames=await api("/api/reports");
  $("#report").onchange=()=>openReport($("#report").value);
  await refreshSaved();refreshPrep();refreshTrash();
  if(!reportNames.length&&!savedCount){$("#list").replaceChildren(el("div",{className:"empty",textContent:"No reports yet. Run: python main.py run"}));return}
  await openReport(reportNames[0]||SAVED);
}
async function openReport(name){
  reportName=name;$("#report").value=name;
  const data=await api(name===SAVED?"/api/saved":name===PREP?"/api/prep":name===TRASH?"/api/trash":"/api/report/"+encodeURIComponent(name));
  jobs=data.jobs;renderList();route();
}
function hiddenFromList(j){   // discarded jobs live in the trash, saved ones in the Saved tab, applied ones in the tracker
  if(reportName===TRASH)return false;
  if(j.status==="skipped")return true;
  if(j.status==="saved"&&reportName!==SAVED&&reportName!==PREP)return true;   // only the report lists hide saved jobs
  return reportName!==PREP&&APPLIED.includes(j.status)&&!!j.applied_at;       // the Interview Prep list keeps applied jobs
}
function renderList(){
  const q=$("#q").value.toLowerCase();
  const items=jobs.filter(j=>!hiddenFromList(j)&&(j.title+" "+j.company).toLowerCase().includes(q)).map(j=>{
    const tier=j.score>=80?"hi":j.score>=60?"mid":"";
    const a=el("a",{className:"item"+(["applied","rejected","withdrawn"].includes(j.status)?" done":""),href:"#"+encodeURIComponent(j.id)},
      el("div",{className:"score "+tier,textContent:j.score}),
      el("div",{},el("div",{className:"t",textContent:j.title}),el("div",{className:"c",textContent:j.company+" · "+(j.location||"")+(j.applied_at?" · ✓ applied":"")+(j.status==="saved"?" · ★ saved":"")})));
    a.dataset.id=j.id;return a});
  $("#list").replaceChildren(...(items.length||q||!EMPTY[reportName]?items:[el("div",{className:"empty",textContent:EMPTY[reportName]})]));markSelected();
}
function markSelected(){
  const id=decodeURIComponent(location.hash.slice(1));
  document.querySelectorAll(".item").forEach(a=>a.classList.toggle("sel",a.dataset.id===id));
  const s=document.querySelector(".item.sel");if(s)s.scrollIntoView({block:"nearest"});
}
function list(title,items,cls){
  if(!items||!items.length)return[];
  return[el("h2",{textContent:title}),el("div",{},...items.map(s=>el("span",{className:"chip "+cls,textContent:s})))]
}
const BOOKMARK='<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/></svg>';
function saveButton(j){   // the bookmark in the posting's top-right corner: save this job for later, or unsave it
  const saved=j.status==="saved",b=el("button",{className:"btn save-btn"+(saved?" on":""),
    title:saved?"Saved. Click to unsave.":"Save for later: keep this job in your Saved list",onclick:()=>setStatus(j,saved?"new":"saved")});
  b.innerHTML=BOOKMARK;   // fixed markup of our own, no job text in it
  b.setAttribute("aria-label",b.title);
  return b;
}
const askedFor=new Set(),reformatTried=new Set();
function restoreFormatting(j){   // a posting stored as plain text gets its headings and bullets back, fetched once from its board
  if(!j.can_reformat||reformatTried.has(j.id))return;
  reformatTried.add(j.id);
  post("/api/reformat",{id:j.id}).then(d=>{
    if(!d.updated)return;
    j.description=d.description;j.can_reformat=false;
    if(decodeURIComponent(location.hash.slice(1))===j.id&&$("#desc"))$("#desc").replaceChildren(...markdown(j.description));
  }).catch(e=>console.warn("Could not restore the posting's formatting",e));   // purely cosmetic, so no message
}
function route(){
  const id=decodeURIComponent(location.hash.slice(1));
  const inTracker=id==="tracker";
  markTabs(inTracker);
  if(inTracker){markSelected();renderTracker();return}
  const j=jobs.find(x=>x.id===id);markSelected();
  if(!j&&id&&!askedFor.has(id)){   // not in this list (for example opened from the tracker): fetch it once
    askedFor.add(id);
    api("/api/job/"+encodeURIComponent(id)).then(d=>{jobs.push(d);route()}).catch(()=>{});
  }
  if(j)lastJob=id;
  if(!j){$("#main").replaceChildren(el("div",{className:"empty",textContent:jobs.length?"Select a job from the list.":"No jobs in this report."}));return}
  const meta=[j.location,j.pay&&j.pay!=="n/a - n/a"?j.pay:"",j.posted&&"Posted "+j.posted].filter(Boolean).join("  ·  ");
  const buttons=el("div",{className:"row"});
  if(j.url)buttons.append(el("a",{className:"btn primary",href:j.url,target:"_blank",rel:"noopener noreferrer",textContent:"Open original posting"}));
  const tracked=APPLIED.includes(j.status)&&j.applied_at;
  if(tracked){
    buttons.append(el("span",{className:"btn on",textContent:"✓ Applied "+fmt(j.applied_at,{month:"short",day:"numeric",year:"numeric"})}),
      el("button",{className:"btn",textContent:"Untrack",title:"Remove from the tracker",onclick:()=>setStatus(j,"new",true)}));
  }else{
    buttons.append(el("button",{className:"btn apply",textContent:"Mark as applied",title:"Adds this job to the tracker with today's date",onclick:()=>setStatus(j,"applied",true)}));
  }
  const note=el("div",{className:"muted",id:"tailor-note",style:"margin:-4px 0 8px"});
  const tailorBtn=el("button",{className:"btn",id:"tailor-btn",textContent:TAILOR_LABEL,title:"Uses Claude (Fable 5.1) to rewrite your resume for this posting and write a cover letter, saved as PDFs in the Optimized Resumes folder. Costs a few cents to under a dollar.",onclick:()=>askTailor(j)});
  buttons.append(tailorBtn);
  buttons.append(el("button",{className:"btn",textContent:"Interview prep",
    title:"How the interview for this role and company usually runs, what to study, and what to ask. Uses Claude once (about 3 cents); the answer is saved in your Interview Prep list, so opening it again is free.",
    onclick:()=>openPrep(j,false)}));
  buttons.append(j.status==="skipped"
    ?el("button",{className:"btn on",textContent:"Restore from trash",title:"Put this job back in your lists and future reports",onclick:()=>setStatus(j,"new")})
    :el("button",{className:"btn",textContent:"Discard",title:"Move this job to the trash (the trash icon, top left). It stays out of future reports until you restore it.",onclick:()=>setStatus(j,"skipped")}));
  const card=el("div",{className:"card"},saveButton(j),
    el("h1",{textContent:j.title}),el("p",{className:"sub",textContent:j.company+(meta?"  ·  "+meta:"")}),
    el("div",{},el("span",{className:"verdict "+(j.verdict||""),textContent:(j.verdict||"")+" "+j.score}),el("span",{textContent:j.reason||""})),
    buttons,note,
    ...list("Matched",j.matched,"good"),...list("Missing required",j.missing_required,"bad"),...list("Dealbreakers",j.dealbreakers,"bad"),
    ...(j.resume_angle?[el("h2",{textContent:"Resume angle"}),el("div",{textContent:j.resume_angle})]:[]),
    el("h2",{textContent:"Posting"}),
    el("div",{id:"desc"},...(j.description?markdown(j.description):[el("p",{textContent:"No stored description. Use the button above to read the original posting."})])));
  $("#main").replaceChildren(card);$("#main").scrollTop=0;paintTailor(j.id);
  restoreFormatting(j);
  if(reportName===PREP&&prepJobId!==j.id&&!prepStreaming)openPrep(j,false);   // from the Interview Prep list, show its saved prep
}
const post=(path,body)=>api(path,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
async function setStatus(j,status,explicit){
  const tracked=APPLIED.includes(j.status)&&j.applied_at;
  const what=status==="skipped"?"Discarding it":"Marking it \""+status+"\"";
  if(tracked&&!APPLIED.includes(status)&&!explicit&&!(await ask({title:status==="skipped"?"Discard this application?":"Remove from your tracker?",
    message:"This job is in your tracker. "+what+" will take it out of the tracker and its counts.",confirmText:status==="skipped"?"Discard":"Continue",danger:true})))return;
  const ids=[...document.querySelectorAll(".item")].map(a=>a.dataset.id),at=ids.indexOf(j.id);
  try{
    const res=await post("/api/mark",{id:j.id,status});
    j.status=status;j.applied_at=res.applied_at;
    if(reportName===SAVED)jobs=jobs.filter(x=>x.status==="saved");   // a job you unsave or act on leaves the Saved list
    if(reportName===TRASH)jobs=jobs.filter(x=>x.status==="skipped");   // a job you restore leaves the trash
    const next=at>=0&&(!jobs.includes(j)||hiddenFromList(j))?(ids[at+1]||ids[at-1]):undefined;   // it left the list: move on to the neighbouring job
    renderList();
    if(next)location.hash="#"+encodeURIComponent(next);else route();
    refreshTracker();refreshSaved();refreshTrash();
  }catch(e){fail("Couldn't update that job",e)}
}
const TAILOR_LABEL="Tailor resume + cover letter";
const REQUEST_MAX=2000;
const BAD_CHARS=/[\u0000-\u0008\u000B-\u001F\u007F-\u009F\u00AD\u200B-\u200F\u2028-\u202E\u2060-\u206F\uE000-\uF8FF\uFEFF\uFFF0-\uFFFF]/g;   // control, invisible, direction-override, private-use
function wireRequestBox(id){
  const ta=$("#"+id),n=$("#"+id+"-n");
  const update=()=>{
    const clean=ta.value.replace(BAD_CHARS,"").slice(0,REQUEST_MAX);
    if(clean!==ta.value)ta.value=clean;
    n.textContent=ta.value.length.toLocaleString()+" / "+REQUEST_MAX.toLocaleString();
  };
  ta.addEventListener("input",update);update();return update;
}
const requestBoxes=[wireRequestBox("tailor-resume"),wireRequestBox("tailor-letter")];
const tailorState={};   // job id -> {running, width, message}; kept here so it survives leaving the job's page and coming back
function paintTailor(id){
  const btn=$("#tailor-btn"),note=$("#tailor-note");
  if(!btn||decodeURIComponent(location.hash.slice(1))!==id)return;   // some other job is on screen
  const st=tailorState[id]||{};
  btn.disabled=!!st.running;
  if(st.running){
    btn.style.minWidth=st.width+"px";   // keep the button the same size while it shows the spinner
    btn.replaceChildren(el("span",{className:"spinner"}));btn.setAttribute("aria-label","Working, this takes a minute or two");
  }else{btn.textContent=TAILOR_LABEL;btn.style.minWidth="";btn.removeAttribute("aria-label")}
  note.textContent=st.message||"";
}
function askTailor(j){
  const dlg=$("#tailor-dlg");
  $("#tailor-for").textContent=j.title+" at "+j.company;$("#tailor-resume").value="";$("#tailor-letter").value="";requestBoxes.forEach(f=>f());
  dlg.querySelector("form").onsubmit=e=>{if(e.submitter&&e.submitter.value==="ok")tailorJob(j,$("#tailor-resume").value.trim(),$("#tailor-letter").value.trim())};
  dlg.showModal();$("#tailor-resume").focus();
}
async function tailorJob(j,resume_request,letter_request){
  const btn=$("#tailor-btn");
  tailorState[j.id]={running:true,width:btn?btn.offsetWidth:206,message:""};paintTailor(j.id);
  let message;
  try{
    const d=await post("/api/tailor",{id:j.id,resume_request,letter_request});
    const long=Object.entries(d.pages).filter(([k,n])=>n>1).map(([k])=>k.replace("_"," "));
    message="Saved to Optimized Resumes: "+d.files.join(", ")+(long.length?" (the "+long.join(" and ")+" runs past one page; trim it before sending)":"");
  }catch(e){fail("Couldn't write your resume and cover letter",e);message="That didn't finish. "+friendly(e)}
  tailorState[j.id]={running:false,message};paintTailor(j.id);
}
async function reloadJobs(){if(reportName)await openReport(reportName)}
async function refreshTracker(){
  try{
    tracker=await api("/api/tracker");
    const n=tracker.stats.all.applications;$("#tcount").textContent=n?"("+n+")":"";
    if(location.hash==="#tracker")renderTracker(true);
  }catch(e){}
}
function stat(label,s,detail){
  return el("div",{className:"stat"},el("div",{className:"k",textContent:label}),el("div",{className:"n",textContent:s.applications}),
    el("div",{className:"d",textContent:detail+" · "+s.companies+(s.companies===1?" company":" companies")}));
}
function statusSelect(a){
  const sel=el("select",{className:"status s-"+a.status,title:"Update where this application stands"},
    ...APPLIED.map(s=>el("option",{value:s,textContent:STATUS_LABEL[s],selected:s===a.status})));
  sel.onchange=async()=>{
    try{await post("/api/mark",{id:a.id,status:sel.value});await refreshTracker();await reloadJobs()}
    catch(e){fail("Couldn't change the status",e);sel.value=a.status}
  };
  return sel;
}
const call=post;
const openRounds=new Set();   // tracker rows whose interview rounds are showing
const ROUND_RESULT={scheduled:"Scheduled",waiting:"Waiting to hear back",passed:"Passed",rejected:"Rejected"};
function roundsRow(a,cols){
  const rows=a.rounds.map(r=>{
    const save=async change=>{
      Object.assign(r,change);
      try{await call("/api/round",{action:"update",round_id:r.id,kind:r.kind,date:r.date||"",outcome:r.outcome,notes:r.notes||""})}
      catch(e){fail("Couldn't save the round",e);await refreshTracker()}
    };
    const kind=el("input",{value:r.kind,maxLength:80,placeholder:"Round name"});kind.setAttribute("list","round-kinds");kind.setAttribute("aria-label","Round name");
    kind.onchange=()=>save({kind:kind.value.trim()||r.kind});
    const day=el("input",{type:"date",value:r.date||""});day.setAttribute("aria-label","Round date");day.onchange=()=>save({date:day.value});
    const result=el("select",{},...Object.entries(ROUND_RESULT).map(([v,t])=>el("option",{value:v,textContent:t,selected:v===r.outcome})));
    result.setAttribute("aria-label","Round result");result.onchange=()=>save({outcome:result.value});
    const notes=el("input",{className:"round-notes",value:r.notes||"",maxLength:1000,placeholder:"Notes: interviewer, topics, how it went"});notes.setAttribute("aria-label","Round notes");
    notes.onchange=()=>save({notes:notes.value.trim()});
    const del=el("button",{className:"btn",textContent:"\u00d7",title:"Delete this round",onclick:async()=>{
      if(!(await ask({title:"Delete this round?",message:"\u201c"+r.kind+"\u201d will be deleted. This can't be undone.",confirmText:"Delete",danger:true})))return;
      try{await call("/api/round",{action:"delete",round_id:r.id});await refreshTracker()}catch(e){fail("Couldn't delete the round",e)}}});
    return el("div",{className:"round"},kind,day,result,notes,del);
  });
  const add=el("button",{className:"btn",textContent:"+ Add round",onclick:async()=>{
    try{await call("/api/round",{action:"add",id:a.id});await refreshTracker();await reloadJobs()}catch(e){fail("Couldn't add a round",e)}}});
  return el("tr",{className:"rounds"},el("td",{colSpan:cols},
    el("div",{className:"muted",textContent:rows.length?"Interview rounds":"No rounds yet. Add one for each interview stage you are told about."}),...rows,add));
}
// ---- Interview prep side panel: the answer streams in as Markdown and is saved in the Interview Prep list
let prepJobId="",prepStreaming="",prepText="",prepFrame=0;
function inline(text){   // **bold** is the only inline markup; everything else stays plain text
  return text.split(/(\*\*[^*]+\*\*)/).filter(Boolean).map(p=>/^\*\*[^*]+\*\*$/.test(p)?el("b",{textContent:p.slice(2,-2)}):document.createTextNode(p));
}
function markdown(text){   // headings (##, ###), bullets, and paragraphs, built as DOM nodes so nothing from the model is ever parsed as HTML
  const out=[];let list=null;
  for(const raw of text.split("\n")){
    const line=raw.trimEnd();let m;
    if(!line.trim()){list=null;continue}
    if((m=line.match(/^(#{2,3})\s+(.*)$/))){
      list=null;
      const tag=m[2].match(/^(.*?)\s*\((Reported|Typical pattern)\)\s*$/);   // "### Stage (Reported)" becomes a heading plus a tag
      const h=el(m[1].length===2?"h2":"h3",{});
      h.append(tag?tag[1]:m[2]);
      if(tag)h.append(el("span",{className:"tag"+(tag[2]==="Reported"?" rep":""),textContent:tag[2],
        title:tag[2]==="Reported"?"Reported by candidates or the company":"The usual pattern at similar companies; no report found for this one"}));
      out.push(h);continue;
    }
    if((m=line.match(/^\s*(?:[-*]|\d+\.)\s+(.*)$/))){
      if(!list){list=el("ul",{});out.push(list)}
      list.append(el("li",{},...inline(m[1])));continue;
    }
    list=null;out.push(el("p",{},...inline(line.trim())));
  }
  return out;
}
function paintPrep(){   // redraw what has arrived so far, at most once per frame
  if(prepFrame)return;
  prepFrame=requestAnimationFrame(()=>{prepFrame=0;$("#prep-text").replaceChildren(...markdown(prepText))});
}
function prepStatus(text){$("#prep-status").replaceChildren(...(text?[el("span",{className:"spinner"}),text]:[]))}
function showPrepResult(d){
  prepText=d.content.markdown;cancelAnimationFrame(prepFrame);prepFrame=0;
  $("#prep-text").replaceChildren(...markdown(prepText));
  const src=(d.content.sources||[]).filter(s=>/^https?:\/\//.test(s.url));
  $("#prep-sources").replaceChildren(...(src.length?[el("h2",{textContent:"Sources the search found"}),
    el("ul",{},...src.map(s=>el("li",{},el("a",{href:s.url,target:"_blank",rel:"noopener noreferrer",textContent:s.title||s.url}))))]:[]));
  const n=d.content.searches;
  $("#prep-meta").textContent=(n?"Searched the web "+n+(n===1?" time. ":" times. "):"")+"Generated "+d.created+" with "+d.model+(d.cached?" (saved earlier, no new charge).":".");
}
async function openPrep(j,refresh){
  $("#prep-panel").classList.add("open");
  if(prepStreaming===j.id)return;             // already being written; the panel just keeps filling in
  if(prepStreaming){toast("Interview prep is still being written","It is for another job. Wait for it to finish, then try this one.","info");return}
  prepJobId=j.id;prepText="";prepStreaming=j.id;
  $("#prep-title").textContent="Interview prep: "+j.title+" at "+j.company;
  for(const id of ["prep-text","prep-sources"])$("#"+id).replaceChildren();
  $("#prep-meta").textContent="";$("#prep-disclaimer").textContent="";$("#prep-regen").disabled=true;prepStatus("Starting...");
  const mine=()=>prepJobId===j.id;   // false once another job's prep has been opened in the panel
  try{
    const r=await fetch("/api/tips",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id:j.id,refresh})});
    if(!r.ok){const d=await r.json().catch(()=>({}));throw new ApiError(d.error||BY_STATUS[r.status]||SERVER_TROUBLE,r.status,d.code)}
    const reader=r.body.getReader(),dec=new TextDecoder();let buf="";
    for(;;){
      const {value,done}=await reader.read();if(done)break;
      buf+=dec.decode(value,{stream:true});
      for(let i;(i=buf.indexOf("\n"))>=0;){
        const line=buf.slice(0,i);buf=buf.slice(i+1);if(!line.trim())continue;
        const ev=JSON.parse(line);
        if(ev.type==="error")throw new ApiError(ev.error,200,ev.code);
        if(!mine())continue;
        if(ev.type==="start")$("#prep-disclaimer").textContent=ev.disclaimer;
        else if(ev.type==="status")prepStatus(ev.text);
        else if(ev.type==="text"){prepText+=ev.text;paintPrep();prepStatus("Writing...")}
        else if(ev.type==="reset"){prepText="";paintPrep()}
        else if(ev.type==="done"){showPrepResult(ev.result);prepStatus("")}
      }
    }
  }catch(e){
    console.error(e);
    if(mine()){prepStatus("");$("#prep-text").replaceChildren(el("div",{className:"disclaimer"},el("b",{textContent:"We couldn't write the interview prep. "}),friendly(e)),
      el("div",{className:"row"},el("button",{className:"btn",textContent:"Try again",onclick:()=>openPrep(j,refresh)})))}
  }
  prepStreaming="";
  if(mine())$("#prep-regen").disabled=false;
  await refreshPrep();if(reportName===PREP)await reloadJobs();
}
$("#prep-close").onclick=()=>$("#prep-panel").classList.remove("open");
$("#prep-regen").onclick=()=>{
  const j=jobs.find(x=>x.id===prepJobId)||{id:prepJobId,title:$("#prep-title").textContent.replace("Interview prep: ",""),company:""};
  ask({title:"Write new interview prep?",message:"Claude will write it again and replace the saved answer. This uses a few cents of API credit.",confirmText:"Write it again"}).then(yes=>{if(yes)openPrep(j,true)});
};
function renderTracker(keepScroll){
  const scrolled=$("#main").scrollTop;
  markTabs(true);
  if(!tracker){$("#main").replaceChildren(el("div",{className:"empty",textContent:"Loading..."}));refreshTracker();return}
  const s=tracker.stats,short={weekday:"short",month:"short",day:"numeric"};
  const rows=tracker.applications.map(a=>{
    const date=el("input",{type:"date",value:a.applied_at,max:s.today,"aria-label":"Date applied"});
    date.onchange=async()=>{
      try{await post("/api/applied-date",{id:a.id,date:date.value});await refreshTracker();await reloadJobs()}
      catch(e){fail("Couldn't change the date",e);await refreshTracker()}
    };
    const title=a.url?el("a",{href:a.url,target:"_blank",rel:"noopener noreferrer",textContent:a.title}):el("span",{textContent:a.title});
    const remove=el("button",{className:"btn",textContent:"Remove",title:"Remove from the tracker",onclick:async()=>{
      if(!(await ask({title:"Remove from the tracker?",message:a.title+" at "+a.company+" will be taken out of the tracker and its counts. You can add it again by marking it as applied.",confirmText:"Remove",danger:true})))return;
      try{await post("/api/mark",{id:a.id,status:"new"});await refreshTracker();await reloadJobs()}
      catch(e){fail("Couldn't remove it",e)}}});
    const roundsBtn=el("button",{className:"btn"+(openRounds.has(a.id)?" on":""),textContent:"Rounds"+(a.rounds.length?" ("+a.rounds.length+")":""),
      title:"Track each interview round for this job",onclick:()=>{openRounds.has(a.id)?openRounds.delete(a.id):openRounds.add(a.id);renderTracker(true)}});
    const details=el("button",{className:"btn",textContent:"Details",title:"Open this job's page: the posting analysis, tailor resume, interview prep",onclick:()=>{location.hash="#"+encodeURIComponent(a.id)}});
    const main=el("tr",{},el("td",{},date),el("td",{textContent:a.company}),el("td",{},title),el("td",{},statusSelect(a)),el("td",{className:"actions-cell"},roundsBtn,details),el("td",{},remove));
    return openRounds.has(a.id)?[main,roundsRow(a,6)]:[main];
  });
  const table=rows.length
    ?el("table",{className:"trk"},el("thead",{},el("tr",{},...[["Date applied",130],["Company",0],["Role",0],["Status",150],["Interview",100],["",84]].map(([h,w])=>el("th",{textContent:h,style:w?"width:"+w+"px":""})))),el("tbody",{},...rows.flat()))
    :el("div",{className:"empty",style:"margin-top:8vh",textContent:"No applications yet. Open a job on the Matches tab and click “Mark as applied”."});
  $("#main").replaceChildren(el("div",{className:"card"},
    el("h1",{textContent:"Application tracker"}),
    el("p",{className:"sub",textContent:"Counts include every job marked as applied, whatever happened next (interview, offer, rejection)."}),
    el("div",{className:"stats"},
      stat("This week",s.week,fmt(s.week.start,short)+" – "+fmt(s.week.end,short)),
      stat("This month",s.month,s.month.label),
      stat("All time",s.all,"since you started tracking")),
    el("h2",{textContent:"Applications"}),table));
  $("#main").scrollTop=keepScroll?scrolled:0;   // staying where you were when only adding or editing a round
}
function step(d){
  const ids=[...document.querySelectorAll(".item")].map(a=>a.dataset.id);if(!ids.length)return;
  const i=ids.indexOf(decodeURIComponent(location.hash.slice(1)));
  location.hash="#"+encodeURIComponent(ids[Math.max(0,Math.min(ids.length-1,i+d))]);
}
addEventListener("hashchange",route);
$("#q").addEventListener("input",renderList);
addEventListener("keydown",e=>{if(e.target.matches("input,select")||e.ctrlKey||e.metaKey||e.altKey)return;
  if(e.key==="j")step(1);else if(e.key==="k")step(-1)});
$("#tab-matches").addEventListener("click",async e=>{
  e.preventDefault();
  if((reportName===SAVED||reportName===PREP||reportName===TRASH)&&reportNames.length)await openReport(reportNames[0]);   // back from a list to the newest report
  location.hash=lastJob?"#"+encodeURIComponent(lastJob):"#";
});
$("#tab-saved").addEventListener("click",async e=>{
  e.preventDefault();
  await openReport(SAVED);
  location.hash=jobs.length?"#"+encodeURIComponent(jobs[0].id):"#";   // open the best saved job; leaves the Tracker if it was showing
});
$("#trash-btn").addEventListener("click",async()=>{
  if(reportName===TRASH&&location.hash!=="#tracker"&&reportNames.length){   // already in the trash: the icon goes back to the newest report
    await openReport(reportNames[0]);location.hash=lastJob?"#"+encodeURIComponent(lastJob):"#";return;
  }
  await openReport(TRASH);
  location.hash=jobs.length?"#"+encodeURIComponent(jobs[0].id):"#";
});
$("#tab-prep").addEventListener("click",async e=>{
  e.preventDefault();
  await openReport(PREP);
  location.hash=jobs.length?"#"+encodeURIComponent(jobs[0].id):"#";
});
function setTheme(t,save){   // Dark (the default) or Light; the choice is remembered in this browser
  document.documentElement.dataset.theme=t;
  document.querySelectorAll("#theme-toggle button").forEach(b=>b.setAttribute("aria-pressed",String(b.dataset.theme===t)));
  if(save)try{localStorage.setItem("theme",t)}catch(e){}
}
document.querySelectorAll("#theme-toggle button").forEach(b=>b.addEventListener("click",()=>setTheme(b.dataset.theme,true)));
setTheme(document.documentElement.dataset.theme==="light"?"light":"dark",false);
refreshTracker();
loadReports().catch(e=>{console.error(e);$("#main").replaceChildren(el("div",{className:"empty"},"We couldn't load your jobs. "+friendly(e),
  el("div",{className:"row",style:"justify-content:center"},el("button",{className:"btn",textContent:"Try again",onclick:()=>location.reload()}))))});
</script></body></html>
"""


def main() -> None:
    """Start the viewer on this computer only (127.0.0.1), so nothing else on the network can reach it."""
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")   # unexpected errors, with details, go to this window
    ap = argparse.ArgumentParser(description="Browse job reports in your browser.")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-open", action="store_true", help="don't open a browser tab")
    args = ap.parse_args()
    if not DB_PATH.exists():
        sys.exit("jobs.db not found. Run: python main.py run")
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Serving {url}  (Ctrl+C to stop)")
    if not args.no_open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
