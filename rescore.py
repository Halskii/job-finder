"""Admin only: score one person's jobs again with the model now configured, for when the model or the scoring prompt has changed.

    python main.py rescore --user 1 --dry-run        (count the jobs and estimate the cost; change nothing)
    python main.py rescore --user me@example.com     (do it)

This is a command on the server and has no web page, so the only people who can run it are the people who can run commands there.
Each job is scored again and its score is replaced only when the new answer comes back, so the person's list stays full the whole
time and a job whose call fails keeps the score it had. Every score records the model that gave it (`_model` in its analysis), so a
run that is stopped, or a second run, skips the jobs already done; scores from before that was recorded count as not done.

Only jobs the person is still looking at (new or saved) are scored; applied and skipped ones are left as they are. The calls are
counted under their own kind ("rescore") in `python main.py usage`, so they neither use up the person's daily scoring nor are held
back by its limit. The site-wide pause switch (`limits: paused`) still stops them.
"""
from __future__ import annotations

import json
import statistics
from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed

import db
import limits
import pipeline
import scorer
import tailor
import userdata
from errors import UserError, explain

KIND = "rescore"
STATUSES = ("new", "saved")
FLUSH_EVERY = 50         # usage is written this often, so a run that is killed has still been counted
TYPICAL_IN, TYPICAL_OUT = 4700, 600      # tokens a scoring call took when nothing stored says
THINKING_OUT = 1000      # what a Claude 5 model writes at low effort, its thinking included (measured on Haiku 5.5)
NEWER_TOKENIZER = 1.35   # the Claude 5 models count the same text as about this many times the tokens Haiku 4.5 did (measured: 5,000 against 3,700)


def scored_with(analysis) -> str | None:
    """The model recorded in a stored analysis, or None for a score from before that was recorded."""
    try:
        data = json.loads(analysis or "{}")
    except (TypeError, ValueError):
        return None
    return data.get("_model") if isinstance(data, dict) else None


def find_user(anon, who) -> dict | None:
    """A user by id or by email."""
    who = str(who).strip()
    if who.isdigit():
        row = anon.execute("SELECT id, email FROM users WHERE id = ?", (int(who),)).fetchone()
    else:
        row = anon.execute("SELECT id, email FROM users WHERE lower(email) = lower(?)", (who,)).fetchone()
    return dict(row) if row else None


def pick(conn, model: str, redo_all: bool = False, limit: int | None = None) -> tuple[list, int, int]:
    """(the jobs to score again, best score first; how many scored jobs the person is looking at; how many of those are done already).
    A job without its text cannot be scored, so it is left out."""
    rows = conn.execute("SELECT id, score, verdict, analysis FROM jobs WHERE score IS NOT NULL AND status = ANY(?)"
                        " AND COALESCE(description, '') <> '' ORDER BY score DESC, id", (list(STATUSES),)).fetchall()
    todo = [r for r in rows if redo_all or scored_with(r["analysis"]) != model]
    return (todo[:limit] if limit else todo), len(rows), len(rows) - len(todo)


def estimate(rows: list, model: str, price: dict) -> dict:
    """What scoring these would take: the input size from what the stored analyses say the last calls took, the output size from what the
    model writes (a Claude 5 model's thinking included), and the cost at the configured prices."""
    ins, outs = [], []
    for r in rows:
        try:
            a = json.loads(r["analysis"] or "{}")
        except (TypeError, ValueError):
            continue
        if isinstance(a, dict):
            was = a.get("_model") or ""
            counted_newer = bool(scorer.THINKS_FIRST.match(was)) or not scorer.THINKS_FIRST.match(model)      # tokens counted the way the new model counts
            ins += [a["_in"] * (1 if counted_newer else NEWER_TOKENIZER)] if a.get("_in") else []
            outs += [a["_out"]] if a.get("_out") else []
    tin = statistics.fmean(ins) if ins else TYPICAL_IN
    tout = THINKING_OUT if scorer.THINKS_FIRST.match(model) else (statistics.fmean(outs) if outs else TYPICAL_OUT)
    return {"calls": len(rows), "tokens_in": round(tin), "tokens_out": round(tout),
            "cost": len(rows) * (tin * price.get("input", 0) + tout * price.get("output", 0)) / 1e6}


def run(who, *, redo_all: bool = False, limit: int | None = None, dry_run: bool = False, workers: int | None = None, client=None, log=print) -> int:
    """Score one person's jobs again. Returns 0 when it ran (some calls may have failed; they are counted), 1 when it could not start
    or was stopped."""
    import main
    anon = pipeline._connect(False)
    try:
        user = find_user(anon, who)
    finally:
        anon.close()
    if user is None:
        log(f"No user '{who}'. Use their id or the email they signed up with.")
        return 1
    uid, label = user["id"], f"{user['email']} (id {user['id']})"
    settings, profile = userdata.load_settings(uid), userdata.load_profile(uid)
    if profile is None:
        log(f"{label} has no profile yet, which scoring needs.")
        return 1
    if limits.paused() and not dry_run:
        log(limits.PAUSED + " (limits: paused in config.yaml)")
        return 1
    scfg = pipeline.scoring_config()
    model, price = scfg["model"], scfg.get("price_per_million_tokens") or {}

    conn = pipeline._connect(uid)
    try:
        todo, looking_at, done = pick(conn, model, redo_all, limit)
        est = estimate(todo, model, price)
        log(f"{label}: {looking_at:,} scored jobs they are looking at (new or saved), {done:,} already scored with {model}.")
        cost = f"${est['cost']:.2f}" if est["cost"] >= 0.1 else f"${est['cost']:.4f}"
        log(f"To score: {est['calls']:,}{' (limited)' if limit else ''}. About {est['tokens_in']:,} tokens in and {est['tokens_out']:,} out each, "
            f"about {cost} at the prices in config.yaml.")
        if dry_run or not todo:
            log("Dry run: nothing was changed." if dry_run else "Nothing to do.")
            return 0
        try:
            client = client or tailor.make_client()
        except UserError as ex:
            log(ex.message)
            return 1
        resume_text = main.load_resume(userdata.home(uid))
        old = {r["id"]: r for r in todo}
        rows = {r["id"]: r for r in conn.execute("SELECT * FROM jobs WHERE id = ANY(?)", (list(old),)).fetchall()}
        n_workers = max(1, int(workers or scfg.get("workers", 8)))
        ok = failed = changed = 0
        shift = 0.0
        pending_calls = pending_in = pending_out = 0
        fatal = None
        interrupted = False
        pool = ThreadPoolExecutor(max_workers=n_workers)
        futures = {pool.submit(scorer.score_job, client, model, profile, rows[i], settings["min_pay"], resume_text): i for i in old}
        try:
            for fut in as_completed(futures):
                job_id = futures[fut]
                try:
                    res = fut.result()
                except CancelledError:
                    continue
                except Exception as ex:
                    message, code = explain(ex)
                    failed += 1
                    if code in pipeline.FATAL and fatal is None:
                        fatal = message       # every further call would fail the same way
                        for f in futures:
                            f.cancel()
                    continue
                db.save_score(conn, job_id, res["fit_score"], res["verdict"], res)       # the old score is replaced only now
                ok += 1
                changed += old[job_id]["verdict"] != res["verdict"]
                shift += res["fit_score"] - old[job_id]["score"]
                pending_calls, pending_in, pending_out = pending_calls + 1, pending_in + res.get("_in", 0), pending_out + res.get("_out", 0)
                if pending_calls >= FLUSH_EVERY:
                    db.add_usage(conn, KIND, pending_calls, pending_in, pending_out)
                    pending_calls = pending_in = pending_out = 0
                if ok % 25 == 0:
                    log(f"  {ok:,} of {len(old):,} scored")
        except KeyboardInterrupt:
            interrupted = True
            for f in futures:
                f.cancel()
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
            if pending_calls:
                db.add_usage(conn, KIND, pending_calls, pending_in, pending_out)
        if ok:
            log(f"Scored {ok:,} of {len(old):,}" + (f"; {failed:,} failed and kept their old score" if failed else "")
                + f". The verdict changed on {changed:,}, and scores moved {shift / ok:+.1f} points on average.")
        elif not fatal:
            log(f"Nothing was scored; {failed:,} failed.")
        if fatal:
            log(f"Stopped: {fatal}")
            return 1
        if interrupted:
            log("Stopped. Run the same command again to carry on with the jobs not done yet.")
            return 1
        if failed:
            log("Run the same command again to retry the ones that failed.")
        return 0
    finally:
        conn.close()
