"""Local web viewer for the job reports. Usage: python viewer.py [--port 8765] [--no-open]

Reads reports/*.md, joins each entry to its stored posting in jobs.db (by the ID line), and serves a
sidebar + detail page on 127.0.0.1 only. Standard library only."""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import webbrowser
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import db
import tailor

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
            else:
                j["description"], j["status"], j["notes"], j["applied_at"] = "", "new", "", None
            j["title"], j["company"] = title, company
            j["url"] = safe_url(j.get("url", ""))
    finally:
        conn.close()
    return data


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


def load_tracker() -> dict:
    """Counts for this week / this month / all time, and every tracked application, newest first."""
    conn = db.connect(str(DB_PATH))
    try:
        return {
            "stats": db.application_stats(conn),
            "applications": [{**dict(r), "url": safe_url(r["url"] or "")} for r in db.applications(conn)],
        }
    finally:
        conn.close()


_tailoring = threading.Lock()   # one tailoring at a time: each one is a paid API call


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

    def _tailor(self, job_id: str, resume_request: str, letter_request: str) -> None:
        """Make the tailored resume and cover letter for one job. Takes a minute or two, since it waits on the model."""
        if not _tailoring.acquire(blocking=False):
            return self._json(409, {"error": "Already working on another job. Wait for it to finish."})
        try:
            self._json(200, tailor_for(job_id, resume_request, letter_request))
        except LookupError:
            self._json(404, {"error": "no such job"})
        except Exception as ex:   # missing key or resume, no browser for the PDF, a failed API call: show the reason on the page
            self._json(500, {"error": f"{type(ex).__name__}: {str(ex)[:300]}"})
        finally:
            _tailoring.release()

    def do_GET(self) -> None:
        """Serve the page and the read-only JSON endpoints it loads its data from."""
        if not self._host_ok():
            return self._json(403, {"error": "bad host"})
        path = unquote(urlparse(self.path).path)
        if path == "/":
            return self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        if path == "/api/reports":
            return self._json(200, report_names())
        if path == "/api/tracker":
            return self._json(200, load_tracker())
        if path.startswith("/api/report/"):
            data = load_report(path[len("/api/report/"):])
            return self._json(200, data) if data else self._json(404, {"error": "no such report"})
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        """The writes the page can make: change a job's status, change the date of an application, or write a tailored resume and cover letter."""
        if not self._host_ok():
            return self._json(403, {"error": "bad host"})
        # Requiring JSON means a web page on another origin can't send this without a CORS preflight, which we never grant.
        path = urlparse(self.path).path
        if path not in ("/api/mark", "/api/applied-date", "/api/tailor") or "application/json" not in (self.headers.get("Content-Type") or ""):
            return self._json(400, {"error": "bad request"})
        try:
            body = json.loads(self.rfile.read(max(0, min(int(self.headers.get("Content-Length") or 0), 40_000))))   # room for two 2,000-character requests, even all non-ASCII
            job_id = str(body["id"])
            if path == "/api/mark":
                status = str(body["status"])
            elif path == "/api/applied-date":
                day = parse_day(body["date"])
        except (ValueError, KeyError, TypeError):
            return self._json(400, {"error": "bad request"})
        if path == "/api/tailor":
            return self._tailor(job_id, tailor.clean_request(body.get("resume_request")), tailor.clean_request(body.get("letter_request")))
        if path == "/api/applied-date":
            if day is None:
                return self._json(400, {"error": "date must be a real day, YYYY-MM-DD, not in the future"})
            return self._json(200, {"updated": set_applied_date(job_id, day)})
        if status not in STATUSES:
            return self._json(400, {"error": "bad status"})
        self._json(200, mark_status(job_id, status))


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Job matches</title>
<style>
:root{--bg:#f7f7f5;--panel:#fff;--ink:#1d1d1b;--mute:#6b6b66;--line:#e3e3de;--accent:#2f6fed;--good:#1f7a4d;--warn:#a15c00;--bad:#b3261e}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--panel:#1f1f1d;--ink:#ecece8;--mute:#9a9a93;--line:#34342f;--accent:#7aa2ff;--good:#5fd39a;--warn:#e0a24a;--bad:#ff8a80}}
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
main{flex:1;overflow:auto;padding:24px 32px}
.card{max-width:860px;margin:0 auto}
h1{font-size:22px;margin:0 0 4px}.sub{color:var(--mute);margin:0 0 14px}
.row{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0}
.btn{padding:7px 12px;border:1px solid var(--line);border-radius:6px;background:var(--panel);color:var(--ink);font:inherit;cursor:pointer;text-decoration:none}
.btn:hover{border-color:var(--accent)}.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.btn.on{border-color:var(--accent);color:var(--accent);font-weight:600}
.chip{display:inline-block;padding:2px 9px;border-radius:99px;border:1px solid var(--line);font-size:13px;margin:0 6px 6px 0}
.chip.good{color:var(--good);border-color:var(--good)}.chip.bad{color:var(--bad);border-color:var(--bad)}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--mute);margin:22px 0 6px}
.verdict{display:inline-block;font-weight:700;text-transform:uppercase;font-size:12px;letter-spacing:.05em;margin-right:8px}
.verdict.apply{color:var(--good)}.verdict.maybe{color:var(--warn)}.verdict.skip{color:var(--bad)}
#desc{white-space:pre-wrap;background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:16px;overflow-wrap:anywhere}
.empty{color:var(--mute);text-align:center;margin-top:20vh}
.tabs{display:grid;grid-template-columns:1fr 1fr;gap:6px}
.tab{padding:7px 10px;border:1px solid var(--line);border-radius:6px;text-align:center;text-decoration:none;color:var(--ink);font-weight:600}
.tab.on{background:var(--accent);border-color:var(--accent);color:#fff}
.btn.apply{background:var(--good);border-color:var(--good);color:#fff;font-weight:600}
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
.spinner{display:inline-block;width:14px;height:14px;border:2px solid var(--line);border-top-color:var(--accent);border-radius:50%;animation:spin .8s linear infinite;vertical-align:-2px}
@keyframes spin{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion:reduce){.spinner{animation-duration:2.4s}}
@media (max-width:760px){body{flex-direction:column;height:auto}nav{width:auto;max-height:45vh}main{padding:16px}}
</style></head><body>
<nav><header><div class="tabs"><a class="tab" id="tab-matches" href="#">Matches</a><a class="tab" id="tab-tracker" href="#tracker">Tracker <span id="tcount"></span></a></div><select id="report" aria-label="Report"></select><input id="q" placeholder="Filter by title or company" aria-label="Filter"></header><div id="list"></div></nav>
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
<main id="main"><div class="empty">Select a job from the list.</div></main>
<script>
const $=s=>document.querySelector(s);
const el=(tag,props={},...kids)=>{const e=document.createElement(tag);Object.assign(e,props);kids.forEach(k=>e.append(k));return e};
let jobs=[],reportName="",tracker=null,lastJob="";
const APPLIED=["applied","interview","offer","accepted","rejected","withdrawn"];
const STATUS_LABEL={applied:"Applied",interview:"Interview",offer:"Offer received",accepted:"Offer accepted",rejected:"Rejected",withdrawn:"Withdrawn"};
const fmt=(iso,opts)=>new Date(iso+"T00:00:00").toLocaleDateString(undefined,opts);

async function api(path,opts){const r=await fetch(path,opts);if(!r.ok)throw new Error(path+" "+r.status);return r.json()}

async function loadReports(){
  const names=await api("/api/reports"),sel=$("#report");
  sel.replaceChildren(...names.map(n=>el("option",{value:n,textContent:n.replace(/\.md$/,"")})));
  if(!names.length){$("#list").replaceChildren(el("div",{className:"empty",textContent:"No reports yet. Run: python main.py run"}));return}
  sel.onchange=()=>openReport(sel.value);
  await openReport(names[0]);
}
async function openReport(name){
  reportName=name;
  const data=await api("/api/report/"+encodeURIComponent(name));
  jobs=data.jobs;renderList();route();
}
function renderList(){
  const q=$("#q").value.toLowerCase();
  const items=jobs.filter(j=>(j.title+" "+j.company).toLowerCase().includes(q)).map(j=>{
    const tier=j.score>=80?"hi":j.score>=60?"mid":"";
    const a=el("a",{className:"item"+(["applied","skipped","rejected","withdrawn"].includes(j.status)?" done":""),href:"#"+encodeURIComponent(j.id)},
      el("div",{className:"score "+tier,textContent:j.score}),
      el("div",{},el("div",{className:"t",textContent:j.title}),el("div",{className:"c",textContent:j.company+" · "+(j.location||"")+(j.applied_at?" · ✓ applied":"")})));
    a.dataset.id=j.id;return a});
  $("#list").replaceChildren(...items);markSelected();
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
function route(){
  const id=decodeURIComponent(location.hash.slice(1));
  const inTracker=id==="tracker";
  $("#tab-tracker").classList.toggle("on",inTracker);$("#tab-matches").classList.toggle("on",!inTracker);
  if(inTracker){markSelected();renderTracker();return}
  const j=jobs.find(x=>x.id===id);markSelected();
  if(j)lastJob=id;
  if(!j){$("#main").replaceChildren(el("div",{className:"empty",textContent:jobs.length?"Select a job from the list.":"No jobs in this report."}));return}
  const meta=[j.location,j.pay&&j.pay!=="n/a - n/a"?j.pay:"",j.posted&&"Posted "+j.posted].filter(Boolean).join("  ·  ");
  const buttons=el("div",{className:"row"});
  if(j.url)buttons.append(el("a",{className:"btn primary",href:j.url,target:"_blank",rel:"noopener noreferrer",textContent:"Open original posting"}));
  const tracked=APPLIED.includes(j.status)&&j.applied_at;
  if(tracked){
    buttons.append(el("span",{className:"btn on",textContent:"✓ Applied "+fmt(j.applied_at,{month:"short",day:"numeric",year:"numeric"})}),
      el("button",{className:"btn",textContent:"Undo",title:"Remove from the tracker",onclick:()=>setStatus(j,"new",true)}));
  }else{
    buttons.append(el("button",{className:"btn apply",textContent:"Mark as applied",title:"Adds this job to the tracker with today's date",onclick:()=>setStatus(j,"applied",true)}));
  }
  const note=el("div",{className:"muted",id:"tailor-note",style:"margin:-4px 0 8px"});
  const tailorBtn=el("button",{className:"btn",id:"tailor-btn",textContent:TAILOR_LABEL,title:"Uses Claude (Fable 5.1) to rewrite your resume for this posting and write a cover letter, saved as PDFs in the Optimized Resumes folder. Costs a few cents to under a dollar.",onclick:()=>askTailor(j)});
  buttons.append(tailorBtn);
  ["saved","skipped","interview"].forEach(s=>buttons.append(el("button",{className:"btn"+(j.status===s?" on":""),textContent:s,onclick:()=>setStatus(j,s)})));
  const card=el("div",{className:"card"},
    el("h1",{textContent:j.title}),el("p",{className:"sub",textContent:j.company+(meta?"  ·  "+meta:"")}),
    el("div",{},el("span",{className:"verdict "+(j.verdict||""),textContent:(j.verdict||"")+" "+j.score}),el("span",{textContent:j.reason||""})),
    buttons,note,
    ...list("Matched",j.matched,"good"),...list("Missing required",j.missing_required,"bad"),...list("Dealbreakers",j.dealbreakers,"bad"),
    ...(j.resume_angle?[el("h2",{textContent:"Resume angle"}),el("div",{textContent:j.resume_angle})]:[]),
    el("h2",{textContent:"Posting"}),
    el("div",{id:"desc",textContent:j.description||"No stored description. Use the button above to read the original posting."}));
  $("#main").replaceChildren(card);$("#main").scrollTop=0;paintTailor(j.id);
}
const post=(path,body)=>api(path,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
async function setStatus(j,status,explicit){
  const tracked=APPLIED.includes(j.status)&&j.applied_at;
  if(tracked&&!APPLIED.includes(status)&&!explicit&&!confirm("This job is in your tracker. Marking it \""+status+"\" removes it from the tracker. Continue?"))return;
  try{
    const res=await post("/api/mark",{id:j.id,status});
    j.status=status;j.applied_at=res.applied_at;renderList();route();refreshTracker();
  }catch(e){alert("Could not update status: "+e.message)}
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
    const r=await fetch("/api/tailor",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id:j.id,resume_request,letter_request})});
    const d=await r.json();
    if(!r.ok)throw new Error(d.error||r.status);
    const long=Object.entries(d.pages).filter(([k,n])=>n>1).map(([k])=>k.replace("_"," "));
    message="Saved to Optimized Resumes: "+d.files.join(", ")+(long.length?" (the "+long.join(" and ")+" runs past one page; trim it before sending)":"");
  }catch(e){message="Could not tailor: "+e.message}
  tailorState[j.id]={running:false,message};paintTailor(j.id);
}
async function reloadJobs(){if(reportName)await openReport(reportName)}
async function refreshTracker(){
  try{
    tracker=await api("/api/tracker");
    const n=tracker.stats.all.applications;$("#tcount").textContent=n?"("+n+")":"";
    if(location.hash==="#tracker")renderTracker();
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
    catch(e){alert("Could not update the status: "+e.message);sel.value=a.status}
  };
  return sel;
}
function renderTracker(){
  $("#tab-tracker").classList.add("on");$("#tab-matches").classList.remove("on");
  if(!tracker){$("#main").replaceChildren(el("div",{className:"empty",textContent:"Loading..."}));refreshTracker();return}
  const s=tracker.stats,short={weekday:"short",month:"short",day:"numeric"};
  const rows=tracker.applications.map(a=>{
    const date=el("input",{type:"date",value:a.applied_at,max:s.today,"aria-label":"Date applied"});
    date.onchange=async()=>{
      try{await post("/api/applied-date",{id:a.id,date:date.value});await refreshTracker();await reloadJobs()}
      catch(e){alert("Could not change the date: "+e.message);await refreshTracker()}
    };
    const title=a.url?el("a",{href:a.url,target:"_blank",rel:"noopener noreferrer",textContent:a.title}):el("span",{textContent:a.title});
    const remove=el("button",{className:"btn",textContent:"Remove",title:"Remove from the tracker",onclick:async()=>{
      if(!confirm("Remove "+a.title+" at "+a.company+" from the tracker?"))return;
      try{await post("/api/mark",{id:a.id,status:"new"});await refreshTracker();await reloadJobs()}
      catch(e){alert("Could not remove it: "+e.message)}}});
    return el("tr",{},el("td",{},date),el("td",{textContent:a.company}),el("td",{},title),el("td",{},statusSelect(a)),el("td",{},remove));
  });
  const table=rows.length
    ?el("table",{},el("thead",{},el("tr",{},...["Date applied","Company","Role","Status",""].map(h=>el("th",{textContent:h})))),el("tbody",{},...rows))
    :el("div",{className:"empty",style:"margin-top:8vh",textContent:"No applications yet. Open a job on the Matches tab and click “Mark as applied”."});
  $("#main").replaceChildren(el("div",{className:"card"},
    el("h1",{textContent:"Application tracker"}),
    el("p",{className:"sub",textContent:"Counts include every job marked as applied, whatever happened next (interview, offer, rejection)."}),
    el("div",{className:"stats"},
      stat("This week",s.week,fmt(s.week.start,short)+" – "+fmt(s.week.end,short)),
      stat("This month",s.month,s.month.label),
      stat("All time",s.all,"since you started tracking")),
    el("h2",{textContent:"Applications"}),table));
  $("#main").scrollTop=0;
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
$("#tab-matches").addEventListener("click",e=>{e.preventDefault();location.hash=lastJob?"#"+encodeURIComponent(lastJob):"#"});
refreshTracker();
loadReports().catch(e=>{$("#main").replaceChildren(el("div",{className:"empty",textContent:"Could not load reports: "+e.message}))});
</script></body></html>
"""


def main() -> None:
    """Start the viewer on this computer only (127.0.0.1), so nothing else on the network can reach it."""
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
