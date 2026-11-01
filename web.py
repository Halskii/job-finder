"""The web app: sign-in and the JSON API behind the page. Usage: python web.py [--port 8765] [--host 127.0.0.1] [--no-open]

Every route except the sign-in page and /auth/* needs a session cookie, and every database connection it opens works as that
one user, so the database itself keeps users apart (see db.py)."""
from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import re
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

import auth
import db
import limits
import loginpage
import onboarding
import pipeline
import privacypage
import profilepage
import reformat
import tailor
import tips
import userdata
import viewer
from errors import STATUS, UserError, explain

log = logging.getLogger("job-finder")
COOKIE = "session"
MAX_BODY = 40_000          # room for two 2,000-character requests, even all non-ASCII
MAX_UPLOAD = userdata.MAX_RESUME_BYTES
RUNS = True                # find and score a user's matches in the background after they save their search
RESTORE = True             # restore the formatting of older postings in the background (python web.py --no-restore turns it off)
FILE_NAME = re.compile(r"^[A-Za-z0-9_.\-]{1,200}\.(pdf|docx)$")
MEDIA = {".pdf": "application/pdf", ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)   # no public API documentation page
pipeline.connect_as = lambda uid: viewer._db(uid) if uid is not False else auth_db()   # the background worker uses the same database


class Problem(Exception):
    """A request that cannot be done, with the sentence to show the person and a short code for programs."""

    def __init__(self, status: int, message: str, code: str):
        super().__init__(message)
        self.status, self.message, self.code = status, message, code


def problem_response(status: int, message: str, code: str) -> JSONResponse:
    return JSONResponse({"error": message, "code": code}, status_code=status)


@app.exception_handler(Problem)
async def _problem(request: Request, ex: Problem):
    return problem_response(ex.status, ex.message, ex.code)


@app.exception_handler(UserError)
async def _user_error(request: Request, ex: UserError):
    """An error written for the person (a resume that is too short, a missing key): answered, and not logged as a crash."""
    return problem_response(STATUS.get(ex.code, 422), ex.message, ex.code)


@app.exception_handler(StarletteHTTPException)
async def _http(request: Request, ex: StarletteHTTPException):
    if ex.status_code == 404:
        return problem_response(404, "That page does not exist.", "not_found")
    return problem_response(ex.status_code, "That request was not understood. Reload the page and try again.", "bad_request")


@app.exception_handler(Exception)
async def _unexpected(request: Request, ex: Exception):
    """Anything nobody planned for: a kind message for the page, the details in the terminal."""
    message, code = explain(ex)
    return problem_response(STATUS.get(code, 422), message, code)


# ---------------------------------------------------------------- settings

def allowed_hosts() -> set[str]:
    """The host names this server answers to (ALLOWED_HOSTS, comma separated). Anything else could be a DNS-rebinding attempt."""
    raw = os.environ.get("ALLOWED_HOSTS", "")
    return {"127.0.0.1", "localhost", "::1"} | {h.strip().strip("[]").lower() for h in raw.split(",") if h.strip()}   # this computer's own names always work (health checks)


def google_client_id() -> str:
    return os.environ.get("GOOGLE_CLIENT_ID", "").strip()


def cookie_is_secure(request: Request) -> bool:
    if os.environ.get("COOKIE_SECURE") in ("0", "1"):
        return os.environ["COOKIE_SECURE"] == "1"
    proto = request.headers.get("x-forwarded-proto") if os.environ.get("TRUST_PROXY") == "1" else None
    return (proto or request.url.scheme) == "https"


# ---------------------------------------------------------------- guards

SECURITY = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "same-origin",
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'"}


def host_of(request: Request) -> str:
    """The host name the request was addressed to, without the port: 'localhost:8765' -> 'localhost', '[::1]:8765' -> '::1'."""
    h = (request.headers.get("host") or "").lower()
    if h.startswith("["):
        return h[1:h.find("]")] if "]" in h else h[1:]
    return h.rsplit(":", 1)[0] if ":" in h else h


@app.middleware("http")
async def guard(request: Request, call_next):
    host = host_of(request)
    if host not in allowed_hosts():
        return problem_response(403, "This page cannot be opened at this address.", "bad_host")
    if request.method in ("POST", "PUT"):
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc.lower() != (request.headers.get("host") or "").lower():
            return problem_response(403, "That request came from another site, so it was not done.", "bad_origin")
        # Requiring JSON (or, for the resume upload, a PDF) means a page on another origin cannot send this without a CORS
        # preflight, which is never granted.
        wanted = "application/pdf" if request.method == "PUT" else "application/json"
        if wanted not in (request.headers.get("content-type") or ""):
            return problem_response(400, "That request was not understood. Reload the page and try again.", "bad_request")
        if int(request.headers.get("content-length") or 0) > (MAX_UPLOAD if request.method == "PUT" else MAX_BODY):
            return problem_response(413, "That request was too large.", "too_large")
    response = await call_next(request)
    for k, v in SECURITY.items():
        response.headers.setdefault(k, v)
    return response


async def json_body(request: Request) -> dict:
    raw = b""
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > MAX_BODY:
            raise Problem(413, "That request was too large.", "too_large")
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        raise Problem(400, "That request was not understood. Reload the page and try again.", "bad_request") from None
    if not isinstance(body, dict):
        raise Problem(400, "That request was not understood. Reload the page and try again.", "bad_request")
    return body


async def pdf_body(request: Request) -> bytes:
    raw = b""
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > MAX_UPLOAD:
            raise Problem(413, "That file is too large. A resume PDF can be at most 5 MB.", "too_large")
    return raw


def bad_request() -> Problem:
    return Problem(400, "That request was not understood. Reload the page and try again.", "bad_request")


def not_found() -> Problem:
    return Problem(404, "We could not find that job. Reload the page and try again.", "not_found")


def auth_db():
    """A connection with no user, for signing in and checking sessions."""
    return db.connect(**{**viewer.DB_ARGS, "user": False})


def session_user(request: Request) -> dict | None:
    conn = auth_db()
    try:
        return auth.user_for_session(conn, request.cookies.get(COOKIE))
    finally:
        conn.close()


def current_user(request: Request) -> dict:
    user = session_user(request)
    if user is None:
        raise Problem(401, "Please sign in to continue.", "not_signed_in")
    return user


_locks: dict = {}
_locks_guard = threading.Lock()


def user_lock(kind: str, uid: int) -> threading.Lock:
    """One lock per user and kind of work: each tailoring and each interview prep is a paid call, so a person gets one at a time."""
    with _locks_guard:
        return _locks.setdefault((kind, uid), threading.Lock())


# ---------------------------------------------------------------- sign-in

def start_session(request: Request, user: dict) -> JSONResponse:
    conn = auth_db()
    try:
        token = auth.new_session(conn, user["id"])
    finally:
        conn.close()
    response = JSONResponse({"ok": True, "user": user})
    response.set_cookie(COOKIE, token, max_age=auth.SESSION_DAYS * 86400, httponly=True, samesite="lax",
                        secure=cookie_is_secure(request), path="/")
    return response


def run_auth(fn):
    """Run an account action on a user-less connection; a UserError becomes the answer the sign-in page shows."""
    conn = auth_db()
    try:
        return fn(conn)
    except UserError as ex:
        raise Problem(STATUS.get(ex.code, 422), ex.message, ex.code) from None
    finally:
        conn.close()


@app.get("/healthz")
def healthz():
    """For the load balancer and the container's health check: answers when the app is up and the database answers."""
    conn = None
    try:
        conn = auth_db()
        conn.execute("SELECT 1").fetchone()
        return {"ok": True}
    except Exception:
        raise Problem(503, "The database is not answering.", "db_down") from None
    finally:
        if conn is not None:
            conn.close()


@app.get("/privacy")
def privacy():
    return HTMLResponse(privacypage.render(os.environ.get("CONTACT_EMAIL", "")))


@app.get("/login")
def login_page(request: Request):
    if session_user(request):
        return RedirectResponse("/", status_code=303)
    return HTMLResponse(loginpage.render(google_client_id()), headers={"Content-Security-Policy": loginpage.CSP})


@app.post("/auth/signup")
def signup(request: Request, body: dict = Depends(json_body)):
    user = run_auth(lambda c: auth.sign_up(c, body.get("email"), body.get("password"), body.get("name", ""), invite_only=not auth.signups_open()))
    return start_session(request, user)


@app.post("/auth/login")
def login(request: Request, body: dict = Depends(json_body)):
    ip = request.client.host if request.client else ""
    user = run_auth(lambda c: auth.sign_in(c, body.get("email"), body.get("password"), ip))
    return start_session(request, user)


@app.post("/auth/google")
def google_login(request: Request, body: dict = Depends(json_body)):
    credential = body.get("credential")
    if not isinstance(credential, str) or not credential:
        raise bad_request()
    user = run_auth(lambda c: auth.google_sign_in(c, credential, google_client_id(), auth.verify_google_token, invite_only=not auth.signups_open()))
    return start_session(request, user)


@app.post("/api/account/delete")
def delete_account(body: dict = Depends(json_body), user: dict = Depends(current_user)):
    """Delete the signed-in person's account and everything that belongs to it, at once. They must type their own email to confirm."""
    if str(body.get("confirm", "")).strip().lower() != user["email"].lower():
        raise Problem(400, "Type your email address exactly as shown to confirm.", "invalid")
    uid = user["id"]
    conn = auth_db()
    try:
        conn.execute("DELETE FROM users WHERE id = ?", (uid,))   # their sessions, jobs, rounds, prep, usage and run state go with it
        conn.commit()
    finally:
        conn.close()
    userdata.delete_home(uid)
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE, path="/")
    return response


@app.post("/auth/logout")
def logout(request: Request):
    run_auth(lambda c: auth.end_session(c, request.cookies.get(COOKIE)))
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE, path="/")
    return response


# ---------------------------------------------------------------- the page and its data

_restoring: set[int] = set()


def restore_in_background(uid: int, delay: float) -> None:
    """Give this user's postings that were stored as plain text their original formatting back, best matches first."""
    conn = viewer._db(uid)
    try:
        reformat.restore_all(conn, delay)
    except Exception as ex:   # cosmetic: never let it hurt anything
        log.warning("Background formatting restore stopped: %s", ex)
    finally:
        conn.close()


def start_restore(uid: int) -> None:
    if RESTORE and uid not in _restoring:
        _restoring.add(uid)
        threading.Thread(target=restore_in_background, args=(uid, 0.4), daemon=True).start()


@app.get("/")
def index(request: Request):
    user = session_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if not userdata.is_set_up(user["id"]):   # a new account starts by saying what jobs it wants
        return RedirectResponse("/profile", status_code=303)
    return HTMLResponse(viewer.PAGE)


@app.get("/profile")
def profile_page(request: Request):
    if session_user(request) is None:
        return RedirectResponse("/login", status_code=303)
    return HTMLResponse(profilepage.PAGE)


@app.get("/api/profile")
def get_profile(user: dict = Depends(current_user)):
    uid = user["id"]
    return {"account": user, "set_up": userdata.is_set_up(uid), "resume": {"kind": userdata.resume_kind(uid)},
            "settings": userdata.load_settings(uid), "profile": userdata.load_profile(uid)}


@app.put("/api/resume")
def upload_resume(data: bytes = Depends(pdf_body), user: dict = Depends(current_user)):
    return userdata.save_resume_pdf(user["id"], data)


@app.post("/api/resume-text")
def resume_text(body: dict = Depends(json_body), user: dict = Depends(current_user)):
    return {"chars": userdata.save_resume_text(user["id"], body.get("text"))}


@app.post("/api/profile/analyze")
def analyze_resume(user: dict = Depends(current_user)):
    """Read the saved resume with Claude: suggested job titles and the facts it states. Nothing is saved until the person presses Save."""
    import main
    uid = user["id"]
    text = main.load_resume(userdata.home(uid))   # contact details are removed before anything goes to the API
    if not text:
        raise Problem(400, "Add your resume first, then we can read it.", "no_resume")
    lock = user_lock("analyze", uid)
    if not lock.acquire(blocking=False):
        raise Problem(409, "Already reading your resume. Wait for it to finish.", "busy")
    conn = viewer._db(uid)
    try:
        with limits.paid_call(conn, "analyze"):
            return onboarding.analyze(tailor.make_client(), text)
    finally:
        conn.close()
        lock.release()


@app.post("/api/profile/save")
def save_profile(body: dict = Depends(json_body), user: dict = Depends(current_user)):
    uid = user["id"]
    settings = onboarding.clean_settings(body.get("settings"))
    profile, notes = onboarding.clean_profile(body.get("profile"))
    if not settings["titles"]:
        raise Problem(400, "Add at least one job title to search for.", "invalid")
    userdata.save_settings(uid, settings)
    userdata.save_profile(uid, profile)
    start_run(uid)   # their search is saved: look for jobs that fit it
    WAKE.set()       # and, if the titles are new to the crawl, find boards for them now
    return {"settings": settings, "profile": profile, "notes": notes}


@app.post("/api/profile/skip")
def skip_profile(user: dict = Depends(current_user)):
    """Go to the app without a profile for now (the profile page asks again from the Profile button)."""
    if not userdata.is_set_up(user["id"]):
        userdata.save_settings(user["id"], userdata.load_settings(user["id"]))
    return {"ok": True}


@app.get("/api/me")
def me(user: dict = Depends(current_user)):
    start_restore(user["id"])
    return user


@app.get("/api/reports")
def reports(user: dict = Depends(current_user)):
    return viewer.report_names(user["id"])


@app.get("/api/report/{name}")
def report(name: str, user: dict = Depends(current_user)):
    data = viewer.load_report(user["id"], name)
    if data is None:
        raise Problem(404, "That report does not exist.", "not_found")
    return data


@app.get("/api/matches")
def matches(user: dict = Depends(current_user)):
    return viewer.load_matches(user["id"])


def refresh_allowed() -> bool:
    """Whether people can start a new search themselves (the "Check for new jobs" link). Off unless MANUAL_REFRESH is on: each search can
    score jobs with Claude, which costs money, so on the deployed site new jobs are found automatically once a day instead."""
    return os.environ.get("MANUAL_REFRESH", "").strip().lower() in ("1", "on", "true", "yes")


def run_status(uid: int) -> dict:
    """This user's search progress, plus whether they may start a new search by hand."""
    conn = viewer._db(uid)
    try:
        return {**pipeline.status(conn), "can_refresh": refresh_allowed()}
    finally:
        conn.close()


@app.get("/api/matches/status")
def matches_status(user: dict = Depends(current_user)):
    return run_status(user["id"])


@app.post("/api/matches/refresh")
def matches_refresh(user: dict = Depends(current_user)):
    """Look for new jobs that fit this person's search, and score them. Refused unless MANUAL_REFRESH is on."""
    if not refresh_allowed():
        raise Problem(403, "New jobs are found automatically once a day.", "refresh_off")
    return start_run(user["id"])


def start_run(uid: int) -> dict:
    """Queue a run for this user and return their status (which says 'queued' at once)."""
    if RUNS:
        pipeline.request(uid)
    return run_status(uid)


@app.get("/api/tracker")
def tracker(user: dict = Depends(current_user)):
    return viewer.load_tracker(user["id"])


@app.get("/api/saved")
def saved(user: dict = Depends(current_user)):
    return viewer.load_saved(user["id"])


@app.get("/api/prep")
def prep(user: dict = Depends(current_user)):
    return viewer.load_prep(user["id"])


@app.get("/api/trash")
def trash(user: dict = Depends(current_user)):
    return viewer.load_trash(user["id"])


@app.get("/api/job/{job_id:path}")
def job(job_id: str, user: dict = Depends(current_user)):
    found = viewer.load_job(user["id"], job_id)
    if found is None:
        raise not_found()
    return found


@app.get("/api/files/{name}")
def download(name: str, user: dict = Depends(current_user)):
    """A resume or cover letter this user had made. Only the exact files the program writes, only from their own folder."""
    folder = (userdata.home(user["id"]) / "Optimized Resumes").resolve()
    path = (folder / name).resolve()
    if not FILE_NAME.match(name) or path.parent != folder or not path.is_file():
        raise Problem(404, "That file does not exist. Make it again with the tailor button.", "not_found")
    return FileResponse(path, media_type=MEDIA[path.suffix], filename=name)


def job_id_of(body: dict) -> str:
    try:
        return str(body["id"])
    except KeyError:
        raise bad_request() from None


@app.post("/api/mark")
def mark(body: dict = Depends(json_body), user: dict = Depends(current_user)):
    job_id = job_id_of(body)
    status = str(body.get("status", ""))
    if status not in viewer.STATUSES:
        raise Problem(400, "That is not a status this program knows.", "invalid")
    return viewer.mark_status(user["id"], job_id, status)


@app.post("/api/applied-date")
def applied_date(body: dict = Depends(json_body), user: dict = Depends(current_user)):
    job_id = job_id_of(body)
    day = viewer.parse_day(body.get("date"))
    if day is None:
        raise Problem(400, "Pick a real date that is not in the future.", "invalid")
    return {"updated": viewer.set_applied_date(user["id"], job_id, day)}


@app.post("/api/round")
def round_(body: dict = Depends(json_body), user: dict = Depends(current_user)):
    try:
        return viewer.round_action(user["id"], body)
    except ValueError as ex:
        raise Problem(400, str(ex), "invalid") from None
    except (KeyError, TypeError):   # KeyError is a LookupError, so it is caught before the 404 below
        raise bad_request() from None
    except LookupError as ex:
        raise Problem(404, str(ex), "not_found") from None


@app.post("/api/reformat")
def reformat_(body: dict = Depends(json_body), user: dict = Depends(current_user)):
    try:
        return viewer.reformat_for(user["id"], job_id_of(body))
    except LookupError:
        raise not_found() from None


@app.post("/api/tailor")
def tailor_(body: dict = Depends(json_body), user: dict = Depends(current_user)):
    """Make the tailored resume and cover letter for one job. Takes a minute or two, since it waits on the model."""
    job_id = job_id_of(body)
    lock = user_lock("tailor", user["id"])
    if not lock.acquire(blocking=False):
        raise Problem(409, "Already working on another job. Wait for it to finish.", "busy")
    conn = viewer._db(user["id"])
    try:
        with limits.paid_call(conn, "tailor") as spent:
            result = viewer.tailor_for(user["id"], job_id, tailor.clean_request(body.get("resume_request")),
                                       tailor.clean_request(body.get("letter_request")))
            spent.tokens(result.get("tokens", {}).get("in"), result.get("tokens", {}).get("out"))
            return result
    except LookupError:
        raise not_found() from None
    finally:
        conn.close()
        lock.release()


@app.post("/api/tips")
def tips_(body: dict = Depends(json_body), user: dict = Depends(current_user)):
    """Interview prep for a job, streamed to the page as it is written, one JSON event per line. A saved answer comes at once."""
    uid, job_id, refresh = user["id"], job_id_of(body), bool(body.get("refresh"))
    if not viewer.job_exists(uid, job_id):
        raise not_found()
    conn = viewer._db(uid)
    try:
        if refresh or not db.get_tips(conn, job_id):   # an answer that is already saved costs nothing, so it is never refused
            limits.check(conn, "tips")
    finally:
        conn.close()
    lock = user_lock("tips", uid)
    if not lock.acquire(blocking=False):
        raise Problem(409, "Interview prep is already being written for another job. Wait for it to finish.", "busy")
    events: queue.SimpleQueue = queue.SimpleQueue()

    def work() -> None:   # runs to the end even if the page goes away, so the answer is still written and saved
        fresh = False
        try:
            for event in viewer.tips_events(uid, job_id, refresh):
                fresh = fresh or (event.get("type") == "done" and event.get("result", {}).get("cached") is False)
                events.put(event)
        finally:
            if fresh:   # a newly written answer was paid for
                count = viewer._db(uid)
                try:
                    db.add_usage(count, "tips")
                finally:
                    count.close()
            events.put(None)
            lock.release()

    threading.Thread(target=work, daemon=True).start()

    def lines():
        while (event := events.get()) is not None:
            yield (json.dumps(event) + "\n").encode("utf-8")

    return StreamingResponse(lines(), media_type="application/x-ndjson; charset=utf-8")


# ---------------------------------------------------------------- start-up

WAKE = threading.Event()   # set when something happened that the scheduler should look at now, not at its next minute (a saved profile)


def scheduler() -> None:
    """Runs for as long as the server does. Every minute (or at once when woken): hands the worker any user marked as waiting (the
    daily fetch may have run in another process), and does whatever shared upkeep is due, one thing at a time, in the background:
    boards for titles nobody has searched yet (at once), the monthly crawl for new boards (`crawl_every_days`, default 30), and the
    shared fetch of every posting (`fetch_every_hours`, default 0 = only when `main.py fetch` is run)."""
    import discovery
    import ingest
    import main as program
    busy = threading.Event()

    def work(job: str) -> None:
        try:
            result = ingest.run_job(job, connect=pipeline._connect)
            print(f"Upkeep ({job}) done: {result}", flush=True)
        except Exception as ex:
            log.warning("Upkeep (%s) stopped: %s", job, ex)
        finally:
            busy.clear()

    while True:
        try:
            pipeline.poll_queued()
            if not busy.is_set():
                conn = pipeline._connect(False)
                try:
                    cfg = discovery.load_config(program.ROOT, conn)
                    job = ingest.next_job(conn, cfg, float(tailor.config_value("crawl_every_days", 30) or 0),
                                          float(tailor.config_value("fetch_every_hours", 0) or 0))
                finally:
                    conn.close()
                if job:
                    busy.set()
                    threading.Thread(target=work, args=(job,), name=job, daemon=True).start()
        except Exception as ex:   # a database hiccup must not end the scheduler
            log.warning("Scheduler: %s", ex)
        WAKE.wait(60)
        WAKE.clear()


def main() -> None:
    global RESTORE
    import main as program
    import uvicorn
    program.load_env()
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")   # unexpected errors, with details, go to this window
    ap = argparse.ArgumentParser(description="Run the job finder web app.")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1", help="the address to listen on (127.0.0.1 = this computer only)")
    ap.add_argument("--no-open", action="store_true", help="don't open a browser tab")
    ap.add_argument("--no-restore", action="store_true", help="don't restore the formatting of older postings in the background")
    args = ap.parse_args()
    RESTORE = not args.no_restore
    try:
        db.connect(**viewer.DB_ARGS).close()
    except Exception as ex:
        sys.exit(f"Could not open the database: {ex}\nCheck DATABASE_URL in the .env file and that PostgreSQL is running.")
    # "localhost", not 127.0.0.1: Google treats them as different origins and only the one registered for the client ID works
    if RUNS:
        threading.Thread(target=scheduler, name="scheduler", daemon=True).start()
        n = pipeline.resume_unfinished()   # searches that were running when the server last stopped
        if n:
            print(f"Resuming {n} unfinished search{'es' if n != 1 else ''}.")
    url = f"http://{'localhost' if args.host in ('0.0.0.0', '', '127.0.0.1') else args.host}:{args.port}/"
    print(f"Serving {url}  (Ctrl+C to stop)")
    if not args.no_open:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
