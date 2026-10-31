"""Tailor your resume to one stored job and write a cover letter for it, both saved as PDFs in 'Optimized Resumes/'.

One Claude call returns the rewritten resume and the letter as JSON; this module checks it, lays it out as HTML, and
prints the HTML to PDF with headless Edge or Chrome. Your phone number and email are never sent to the API: the
contact line on the PDF is copied from the top of your own resume file."""
from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from datetime import date
from pathlib import Path

import yaml

import scorer
from errors import BadModelAnswer, UserError

ROOT = Path(__file__).parent
OUT_DIR = ROOT / "Optimized Resumes"
MODEL = "claude-fable-5-1"   # overridden by `tailor_model:` in config.yaml

SYSTEM = """You tailor one candidate's resume to one job posting and write a cover letter for it. Rules:
- Use ONLY facts in the candidate's profile and resume. Never claim anything else, and never imply it by echoing the
  posting's keywords. An item in "do_not_assume" is something the candidate does NOT have, and an item the profile
  does not mention is UNKNOWN. Claim neither.
- Never write about what the candidate lacks. Do not say they have not done, have not used, or are still learning something,
  and do not apologize, hedge, or list gaps, in the resume or the letter. The employer can read the resume. When the posting
  asks for something the candidate does not have, say nothing about it and lead with the closest real experience instead.
- You may reorder, reword, and choose what to emphasize so the posting's requirements come first where the candidate
  truly meets them. Keep every job and school in the resume; do not change employers, titles, or dates. Do not invent numbers.
- Put certifications in the skills list as a labeled line.
- Write plain, direct sentences with no buzzwords. Keep tense consistent: the summary in present tense, bullets about a
  past job in past tense.
- Keep a steady, professional tone about the whole role. Do not sound especially passionate, excited, or devoted about one
  component of the job description (for example "authentication is what I know best" or "I love building X"), and do not
  single one requirement out for emphasis. Show fit through specifics. Only do this if the candidate's own request asks for it.
- Keep the cover letter focused on the candidate's strengths and how they align with the job requirements, don't just list things off their resume.
  Remember, in this role you are a human candidate with a real voice and personality, convince them you are a good fit. 
- The resume must fit on ONE page: a summary of 2 to 4 sentences, at most 8 bullets per job, and short labeled skill lines.
- The cover letter is 3 to 5 short paragraphs, under 330 words. 
  Say why this role at this company, give the two or three most
  relevant examples from the resume with their specifics, written confidently. Do not repeat the whole resume. Do not write a date, address, greeting, or
  sign-off; those are added for you. Make it flow.
Return ONLY a JSON object, no prose, with exactly these keys ("company_name" is the company's name spelled the way the posting spells it):
{"company_name": str, "resume": {"summary": str, "skills": [{"label": str, "text": str}],
            "jobs": [{"heading": str, "dates": str, "bullets": [str]}],
            "education": {"heading": str, "dates": str, "detail": str}, "interests": str},
 "cover_letter": {"paragraphs": [str]}}"""


def make_client():
    """An Anthropic client using the key in .env or the environment."""
    import main
    main.load_env()
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise UserError("No Anthropic API key found. Add ANTHROPIC_API_KEY to the .env file in the job-finder folder, then restart the viewer.", "no_api_key")
    try:
        import anthropic
    except ImportError:
        raise UserError("A package this feature needs (anthropic) is not installed for the Python running the viewer. "
                        "Start the viewer with the project's virtual environment (.venv), or install requirements.txt.", "missing_package") from None
    return anthropic.Anthropic(api_key=key, max_retries=6)


def load_profile() -> dict:
    """profile.json, or a UserError saying how to make it."""
    path = ROOT / "profile.json"
    if not path.exists():
        raise UserError("profile.json was not found. Create it first (see the README), then try again.", "no_profile")
    return json.loads(path.read_text(encoding="utf-8"))


def config_value(key: str, default: str) -> str:
    """One top-level setting from config.yaml, or the default when the file or the setting is missing."""
    cfg_file = ROOT / "config.yaml"
    return ((yaml.safe_load(cfg_file.read_text(encoding="utf-8")) if cfg_file.exists() else None) or {}).get(key, default)


def short(text: str, limit: int = 40) -> str:
    """Part of a name for use in a file name: cut at the first comma, bracket or dash, keep letters and digits only."""
    head = re.split(r"[,(\[\-–—|:/]", text or "")[0]
    return re.sub(r"[^A-Za-z0-9]+", "_", head).strip("_")[:limit].rstrip("_")


def file_stem(name: str, role: str, company: str) -> str:
    """James_Hall_Software_Engineer_Stripe"""
    return "_".join(p for p in (short(name, 60), short(role), short(company)) if p)


def check(data: dict) -> dict:
    """The model's answer, or ValueError if a part the layout needs is missing."""
    try:
        r, c = data["resume"], data["cover_letter"]
        ok = (isinstance(r["summary"], str) and r["summary"].strip()
              and all(isinstance(s["label"], str) and isinstance(s["text"], str) for s in r["skills"])
              and r["jobs"] and all(j["heading"] and isinstance(j["bullets"], list) for j in r["jobs"])
              and isinstance(r["education"]["heading"], str)
              and c["paragraphs"] and all(isinstance(p, str) and p.strip() for p in c["paragraphs"]))
    except (KeyError, TypeError):
        ok = False
    if not ok:
        raise BadModelAnswer("Claude's resume and letter came back incomplete. Please try again.")
    return data


REQUEST_LIMIT = 2000   # characters kept from each custom request


def clean_request(text) -> str:
    """A custom request as safe plain text: no control, invisible, bidirectional-override, private-use or unpaired-surrogate
    characters (any of which can hide text from you, or make later encoding or printing fail). Line breaks and tabs stay,
    other kinds of space become a plain space, and the result is cut to REQUEST_LIMIT characters."""
    out = []
    for ch in unicodedata.normalize("NFC", str(text or "")):
        cat = unicodedata.category(ch)
        if ch in "\n\t":
            out.append(ch)
        elif cat == "Zs":
            out.append(" ")
        elif cat[0] in "LMNPS" and ch.isprintable():
            out.append(ch)
    return re.sub(r"\n{3,}", "\n\n", "".join(out)).strip()[:REQUEST_LIMIT]


def ask(client, model: str, profile: dict, resume_text: str, row, resume_request: str = "", letter_request: str = "") -> tuple[dict, int, int]:
    """One call: the tailored resume and cover letter for this job, plus the input and output token counts.
    The two requests are the candidate's own optional instructions; the rules in SYSTEM still come first."""
    user = (f"CANDIDATE PROFILE (verified):\n{json.dumps(profile, indent=1)}\n\n"
            f"CANDIDATE RESUME:\n{resume_text}\n\n"
            f"JOB POSTING\nCompany: {row['company']}\nTitle: {row['title']}\nLocation: {row['location']}\n\n"
            f"{(row['description'] or '')[:12000]}")
    for label, text in (("RESUME", resume_request), ("COVER LETTER", letter_request)):
        text = clean_request(text)
        if text:
            user += f"\n\nTHE CANDIDATE'S OWN REQUEST FOR THE {label} (follow it; it overrides the style rules above, but never the rules about facts):\n{text}"
    msg = client.messages.create(model=model, max_tokens=16000, system=SYSTEM, output_config={"effort": "medium"},
                                 messages=[{"role": "user", "content": user}])
    if msg.stop_reason == "refusal":
        raise UserError("Claude declined to write this one. Try again, or change your request.", "refused")
    if msg.stop_reason == "max_tokens":
        raise UserError("Claude's answer was cut off before it finished. Please try again.", "cut_off")
    text = "".join(b.text for b in msg.content if b.type == "text")
    usage = getattr(msg, "usage", None)
    return check(scorer._extract_json(text)), int(getattr(usage, "input_tokens", 0) or 0), int(getattr(usage, "output_tokens", 0) or 0)


# ------------------------------------------------------------ layout

e = html.escape
# (font size in pt, line height) tried in turn until the page count is one
RESUME_SIZES = [(10.5, 1.19), (10, 1.15), (9.5, 1.12)]
LETTER_SIZES = [(11, 1.32), (10.5, 1.28), (10, 1.25)]


def resume_html(name: str, contact: str, r: dict, size: tuple[float, float]) -> str:
    pt, lh = size
    css = (f"@page{{size:Letter;margin:0.45in 0.65in}}"
           f"body{{font-family:Calibri,Carlito,'Segoe UI',Arial,sans-serif;font-size:{pt}pt;line-height:{lh};color:#000;margin:0}}"
           "h1{text-align:center;font-size:21pt;margin:0 0 2pt;letter-spacing:.3pt}"
           ".contact{text-align:center;font-size:10pt;margin-bottom:7pt}"
           "h2{font-size:10.5pt;letter-spacing:.4pt;margin:7pt 0 3pt;padding-bottom:1pt;border-bottom:1px solid #000}"
           "p{margin:0 0 2pt}.row{display:flex;justify-content:space-between;font-weight:700;margin-bottom:2pt}"
           ".row span+span{font-weight:400}ul{margin:0;padding-left:20pt}li{margin-bottom:2.5pt;padding-left:2pt}")
    parts = [f"<!doctype html><html><head><meta charset='utf-8'><title>{e(name)} - Resume</title><style>{css}</style></head><body>",
             f"<h1>{e(name)}</h1><div class='contact'>{e(contact)}</div>",
             f"<h2>SUMMARY</h2><p>{e(r['summary'])}</p><h2>TECHNICAL SKILLS</h2>"]
    parts += [f"<p><b>{e(s['label'])}:</b> {e(s['text'])}</p>" for s in r["skills"]]
    parts.append("<h2>EXPERIENCE</h2>")
    for j in r["jobs"]:
        parts.append(f"<div class='row'><span>{e(j['heading'])}</span><span>{e(j.get('dates', ''))}</span></div>"
                     "<ul>" + "".join(f"<li>{e(b)}</li>" for b in j["bullets"]) + "</ul>")
    ed = r["education"]
    parts.append(f"<h2>EDUCATION</h2><div class='row'><span>{e(ed['heading'])}</span><span>{e(ed.get('dates', ''))}</span></div>")
    if ed.get("detail"):
        parts.append(f"<p>{e(ed['detail'])}</p>")
    if (r.get("interests") or "").strip():
        parts.append(f"<h2>INTERESTS</h2><p>{e(r['interests'])}</p>")
    return "".join(parts) + "</body></html>"


def letter_html(name: str, contact: str, company: str, role: str, paragraphs: list[str], size: tuple[float, float]) -> str:
    pt, lh = size
    today = date.today()
    css = (f"@page{{size:Letter;margin:0.9in 1in}}"
           f"body{{font-family:Calibri,Carlito,'Segoe UI',Arial,sans-serif;font-size:{pt}pt;line-height:{lh};color:#000;margin:0}}"
           "h1{text-align:center;font-size:21pt;margin:0 0 2pt;letter-spacing:.3pt}"
           ".contact{text-align:center;font-size:10pt;padding-bottom:6pt;border-bottom:1px solid #000;margin-bottom:16pt}"
           "p{margin:0 0 10pt}.tight p{margin:0}")
    body = [f"<p>{today:%B} {today.day}, {today.year}</p>",
            f"<div class='tight'><p>{e(company)} Hiring Team</p><p>Re: {e(role)}</p></div><br>",
            f"<p>Dear {e(company)} Hiring Team,</p>"]
    body += [f"<p>{e(p)}</p>" for p in paragraphs]
    body += ["<p style='margin-bottom:22pt'>Sincerely,</p>", f"<p>{e(name)}</p>"]
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>{e(name)} - Cover Letter</title><style>{css}</style></head><body>"
            f"<h1>{e(name)}</h1><div class='contact'>{e(contact)}</div>" + "".join(body) + "</body></html>")


# ------------------------------------------------------------ PDF

def find_browser() -> str:
    """Edge ships with Windows; Chrome works the same way."""
    for p in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Google\Chrome\Application\chrome.exe", r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"):
        if Path(p).exists():
            return p
    for name in ("msedge", "google-chrome", "chromium", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    raise UserError("Making the PDF needs Microsoft Edge or Google Chrome, and neither was found on this computer.", "no_browser")


def html_to_pdf(page: str, out: Path) -> None:
    """Print an HTML page to a PDF. A private browser profile keeps this from being handed to a browser you already have open."""
    try:
        out.unlink(missing_ok=True)
    except PermissionError:
        raise UserError(f"Could not replace {out.name} because it is open in another program. Close it and try again.", "file_in_use") from None
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        src = Path(tmp) / "page.html"
        src.write_text(page, encoding="utf-8")
        try:
            subprocess.run([find_browser(), "--headless", "--disable-gpu", "--no-pdf-header-footer",
                            f"--user-data-dir={Path(tmp) / 'profile'}", f"--print-to-pdf={out.resolve()}", src.as_uri()],
                           check=True, capture_output=True, timeout=90)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            raise UserError("The browser could not make the PDF (it stopped or took too long). Please try again.", "pdf_failed") from None
    if not out.exists():
        raise UserError("The PDF was not created. Please try again.", "pdf_failed")


def page_count(pdf: Path) -> int:
    from pypdf import PdfReader
    return len(PdfReader(str(pdf)).pages)


def write_one_page(make_page, sizes, out: Path) -> int:
    """Print at the first size, then at smaller ones until it is one page. Returns the page count of the last try."""
    pages = 0
    for size in sizes:
        html_to_pdf(make_page(size), out)
        pages = page_count(out)
        if pages == 1:
            break
    return pages


# ------------------------------------------------------------ the whole job

def tailor_job(conn, job_id: str, out_dir: Path = OUT_DIR, client=None, resume_request: str = "", letter_request: str = "") -> dict:
    """Write <Name>_<Role>_<Company>_resume.pdf and ..._cover_letter.pdf for a stored job.
    Returns {"files": [...], "folder": str, "pages": {...}, "tokens": {"in": n, "out": n}}."""
    import main   # imported here so the viewer only loads it when this button is used
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise LookupError("no such job")
    profile = load_profile()
    resume_text, raw = main.load_resume(), main.read_resume_file()
    if not resume_text:
        raise UserError("No resume found. Put your resume in the job-finder folder as resume.pdf (or resume.txt), then try again.", "no_resume")
    client = client or make_client()
    model = config_value("tailor_model", MODEL)
    data, tok_in, tok_out = ask(client, model, profile, resume_text, row, resume_request, letter_request)

    name = profile.get("name") or "Resume"
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    contact = lines[1] if len(lines) > 1 and "@" in lines[1] else profile.get("location", "")
    out_dir.mkdir(exist_ok=True)
    company = str(data.get("company_name") or "").strip() or row["company"]   # the stored name can be a lowercase board slug
    stem = file_stem(name, row["title"], company)
    resume_pdf, letter_pdf = out_dir / f"{stem}_resume.pdf", out_dir / f"{stem}_cover_letter.pdf"
    r_pages = write_one_page(lambda s: resume_html(name, contact, data["resume"], s), RESUME_SIZES, resume_pdf)
    l_pages = write_one_page(lambda s: letter_html(name, contact, company, row["title"], data["cover_letter"]["paragraphs"], s),
                             LETTER_SIZES, letter_pdf)
    return {"files": [resume_pdf.name, letter_pdf.name], "folder": str(out_dir),
            "pages": {"resume": r_pages, "cover_letter": l_pages}, "tokens": {"in": tok_in, "out": tok_out}}
