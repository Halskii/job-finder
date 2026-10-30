"""Local web viewer for the job reports. Usage: python viewer.py [--port 8765] [--no-open]

Reads reports/*.md, joins each entry to its stored posting in jobs.db (by the ID line), and serves a
sidebar + detail page on 127.0.0.1 only. Standard library only."""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).parent
REPORTS = ROOT / "reports"
DB_PATH = ROOT / "jobs.db"
STATUSES = ("new", "saved", "applied", "skipped", "interview", "rejected", "offer")

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
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        for j in data["jobs"]:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (j.get("id"),)).fetchone() if j.get("id") else None
            title, company = split_heading(j["heading"])
            if row:
                title, company = row["title"], row["company"]
                j.setdefault("url", row["url"])
                j["description"] = row["description"] or ""
                j["status"], j["notes"] = row["status"] or "new", row["notes"] or ""
            else:
                j["description"], j["status"], j["notes"] = "", "new", ""
            j["title"], j["company"] = title, company
            j["url"] = safe_url(j.get("url", ""))
    finally:
        conn.close()
    return data


def mark_status(job_id: str, status: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.execute("UPDATE jobs SET status=? WHERE id=?", (status, job_id))
        conn.commit()
        return cur.rowcount
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

    def do_GET(self) -> None:
        if not self._host_ok():
            return self._json(403, {"error": "bad host"})
        path = unquote(urlparse(self.path).path)
        if path == "/":
            return self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        if path == "/api/reports":
            return self._json(200, report_names())
        if path.startswith("/api/report/"):
            data = load_report(path[len("/api/report/"):])
            return self._json(200, data) if data else self._json(404, {"error": "no such report"})
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if not self._host_ok():
            return self._json(403, {"error": "bad host"})
        # Requiring JSON means a web page on another origin can't send this without a CORS preflight, which we never grant.
        if urlparse(self.path).path != "/api/mark" or "application/json" not in (self.headers.get("Content-Type") or ""):
            return self._json(400, {"error": "bad request"})
        try:
            body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 10_000)))
            job_id, status = str(body["id"]), str(body["status"])
        except (ValueError, KeyError, TypeError):
            return self._json(400, {"error": "bad request"})
        if status not in STATUSES:
            return self._json(400, {"error": "bad status"})
        self._json(200, {"updated": mark_status(job_id, status)})


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
@media (max-width:760px){body{flex-direction:column;height:auto}nav{width:auto;max-height:45vh}main{padding:16px}}
</style></head><body>
<nav><header><select id="report" aria-label="Report"></select><input id="q" placeholder="Filter by title or company" aria-label="Filter"></header><div id="list"></div></nav>
<main id="main"><div class="empty">Select a job from the list.</div></main>
<script>
const $=s=>document.querySelector(s);
const el=(tag,props={},...kids)=>{const e=document.createElement(tag);Object.assign(e,props);kids.forEach(k=>e.append(k));return e};
let jobs=[],reportName="";

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
    const a=el("a",{className:"item"+(["applied","skipped","rejected"].includes(j.status)?" done":""),href:"#"+encodeURIComponent(j.id)},
      el("div",{className:"score "+tier,textContent:j.score}),
      el("div",{},el("div",{className:"t",textContent:j.title}),el("div",{className:"c",textContent:j.company+" · "+(j.location||"")})));
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
  const j=jobs.find(x=>x.id===id);markSelected();
  if(!j){$("#main").replaceChildren(el("div",{className:"empty",textContent:jobs.length?"Select a job from the list.":"No jobs in this report."}));return}
  const meta=[j.location,j.pay&&j.pay!=="n/a - n/a"?j.pay:"",j.posted&&"Posted "+j.posted].filter(Boolean).join("  ·  ");
  const buttons=el("div",{className:"row"});
  if(j.url)buttons.append(el("a",{className:"btn primary",href:j.url,target:"_blank",rel:"noopener noreferrer",textContent:"Open original posting"}));
  ["saved","applied","skipped","interview"].forEach(s=>buttons.append(el("button",{className:"btn"+(j.status===s?" on":""),textContent:s,onclick:()=>setStatus(j,s)})));
  const card=el("div",{className:"card"},
    el("h1",{textContent:j.title}),el("p",{className:"sub",textContent:j.company+(meta?"  ·  "+meta:"")}),
    el("div",{},el("span",{className:"verdict "+(j.verdict||""),textContent:(j.verdict||"")+" "+j.score}),el("span",{textContent:j.reason||""})),
    buttons,
    ...list("Matched",j.matched,"good"),...list("Missing required",j.missing_required,"bad"),...list("Dealbreakers",j.dealbreakers,"bad"),
    ...(j.resume_angle?[el("h2",{textContent:"Resume angle"}),el("div",{textContent:j.resume_angle})]:[]),
    el("h2",{textContent:"Posting"}),
    el("div",{id:"desc",textContent:j.description||"No stored description. Use the button above to read the original posting."}));
  $("#main").replaceChildren(card);$("#main").scrollTop=0;
}
async function setStatus(j,status){
  try{
    await api("/api/mark",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id:j.id,status})});
    j.status=status;renderList();route();
  }catch(e){alert("Could not update status: "+e.message)}
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
loadReports().catch(e=>{$("#main").replaceChildren(el("div",{className:"empty",textContent:"Could not load reports: "+e.message}))});
</script></body></html>
"""


def main() -> None:
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
