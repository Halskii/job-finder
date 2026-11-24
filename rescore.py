"""Admin only: score one person's jobs again with the model now configured, for when the model or the scoring prompt has changed.

    python main.py rescore --user 1 --dry-run        (count the jobs and estimate the cost; change nothing)
    python main.py rescore --user me@example.com     (do it)
    python main.py rescore --user 1 --batch          (do it through the Batch API: half the price, answers in minutes to hours)

This is a command on the server and has no web page, so the only people who can run it are the people who can run commands there.
Each job is scored again and its score is replaced only when the new answer comes back, so the person's list stays full the whole
time and a job whose call fails keeps the score it had. Every score records the model that gave it (`_model` in its analysis), so a
run that is stopped, or a second run, skips the jobs already done; scores from before that was recorded count as not done.

Only jobs the person is still looking at (new or saved) are scored; applied and skipped ones are left as they are. The calls are
counted under their own kind ("rescore") in `python main.py usage`, so they neither use up the person's daily scoring nor are held
back by its limit. The site-wide pause switch (`limits: paused`) still stops them.

Two things make a rescore cheaper than scoring the same jobs one by one:
- Prompt caching. Every call sends the same profile and resume ahead of the posting, so that part is marked for caching and read back
  at a fraction of the price after the first call (about a twentieth on Claude Opus 5.5 and Sonnet 5.5, a tenth elsewhere).
- `--batch` sends the jobs to Anthropic's Message Batches API, which charges half the price and takes minutes to hours instead of
  answering at once. The batches' ids are written to the database as soon as they are made, so stopping the command (Ctrl-C, a closed
  window, a restart) loses nothing: the batches keep running on Anthropic's side, and running the same command again collects them
  and applies the scores instead of paying for a second set. Results come back in any order, so each request carries a short id
  that maps back to the job.
"""
from __future__ import annotations

import json
import re
import statistics
import time
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

BATCH_DISCOUNT = 0.5     # the Batch API charges half the price for everything
BATCH_CHUNK = 1000       # requests in one batch (the limit is 100,000 or 256 MB; each request repeats the profile and resume)
POLL_SECONDS = 30        # how often a waiting `--batch` run asks whether its batches have finished
MIN_CACHE_TOKENS = 512   # a prefix shorter than this is not cached by the Claude 5 models, whatever is marked
WRITE_MULTIPLIER = {"5m": 1.25, "1h": 2.0}      # what writing to the cache costs, as a multiple of the input price, by how long it is kept
HIT_RATE = {"sync": 0.95, "batch": 0.7}        # the share of calls expected to read the cache. A batch runs its requests in parallel and in any order, so a hit is best
#                                                effort (Anthropic says 30% to 98%); 0.7 is a guess, and the cost line printed at the end is what it really was


def cache_read_multiplier(model: str) -> float:
    """What reading the prompt cache costs, as a multiple of the model's input price."""
    if re.match(r"claude-(?:fable|mythos)-5-[12]", model):
        return 0.025
    if re.match(r"claude-(?:opus|sonnet)-5-5", model):
        return 0.05
    return 0.1


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
                        " AND COALESCE(description, '') <> '' AND closed_at IS NULL ORDER BY score DESC, id", (list(STATUSES),)).fetchall()
    todo = [r for r in rows if redo_all or scored_with(r["analysis"]) != model]
    return (todo[:limit] if limit else todo), len(rows), len(rows) - len(todo)


def prefix_share(conn, todo: list, profile: dict, resume_text: str, min_pay: int, sample: int = 100) -> float:
    """About what fraction of a scoring call is the part that is the same for every job (the instructions, the profile and the resume),
    worked out from the real prompts of the first `sample` jobs. That is the part prompt caching can read back cheaply."""
    ids = [r["id"] for r in todo[:sample]]
    if not ids:
        return 0.0
    fractions = []
    for job in conn.execute("SELECT * FROM jobs WHERE id = ANY(?)", (ids,)).fetchall():
        shared, posting = scorer.prompt_parts(profile, job, min_pay, resume_text)
        head = len(scorer.SYSTEM) + len(shared)
        fractions.append(head / (head + len(posting)))
    return statistics.fmean(fractions) if fractions else 0.0


def estimate(rows: list, model: str, price: dict, *, share: float = 0.0, batch: bool = False) -> dict:
    """What scoring these would take: the input size from what the stored analyses say the last calls took, the output size from what the
    model writes (a Claude 5 model's thinking included), and the cost at the configured prices. `share` is the fraction of the input that
    prompt caching can serve (0 = no caching) and `batch` the Batch API's half price; `standard_cost` is what it would be with neither."""
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
    pin, pout = price.get("input", 0), price.get("output", 0)
    factor = 1.0      # what an input token costs on average, as a multiple of the plain price
    if share > 0:
        hit = HIT_RATE["batch" if batch else "sync"]
        write = WRITE_MULTIPLIER["1h" if batch else "5m"]
        factor = (1 - share) + share * (hit * cache_read_multiplier(model) + (1 - hit) * write)
    calls = len(rows)
    return {"calls": calls, "tokens_in": round(tin), "tokens_out": round(tout),
            "standard_cost": calls * (tin * pin + tout * pout) / 1e6,
            "cost": calls * (tin * factor * pin + tout * pout) / 1e6 * (BATCH_DISCOUNT if batch else 1)}


def money(x: float) -> str:
    return f"${x:.2f}" if x >= 0.1 else f"${x:.4f}"


class Tally:
    """What a run has done so far: scores saved, failures, how far scores moved, and the tokens used, written to `usage` in steps."""

    def __init__(self, conn, price: dict, model: str, batch: bool, cache_ttl: str):
        self.conn, self.price, self.model, self.batch, self.ttl = conn, price, model, batch, cache_ttl
        self.ok = self.failed = self.changed = 0
        self.shift = 0.0
        self.tin = self.tout = self.read = self.write = 0
        self._calls = self._in = self._out = 0

    def saved(self, job_id: str, res: dict, old) -> None:
        """Replace a job's score with the new one (the old one stays until this moment)."""
        db.save_score(self.conn, job_id, res["fit_score"], res["verdict"], res)
        self.ok += 1
        self.changed += old["verdict"] != res["verdict"]
        self.shift += res["fit_score"] - old["score"]
        self.tin += res.get("_in", 0)
        self.tout += res.get("_out", 0)
        self.read += res.get("_cache_read", 0)
        self.write += res.get("_cache_write", 0)
        self._calls, self._in, self._out = self._calls + 1, self._in + res.get("_in", 0), self._out + res.get("_out", 0)
        if self._calls >= FLUSH_EVERY:
            self.flush()

    def flush(self) -> None:
        if self._calls:
            db.add_usage(self.conn, KIND, self._calls, self._in, self._out)
            self._calls = self._in = self._out = 0

    def cost(self) -> float:
        """What the calls cost at the configured prices, counting what was read from and written to the cache and the batch discount."""
        pin, pout = self.price.get("input", 0), self.price.get("output", 0)
        plain = self.tin - self.read - self.write
        tokens = plain + self.read * cache_read_multiplier(self.model) + self.write * WRITE_MULTIPLIER[self.ttl]
        return (tokens * pin + self.tout * pout) / 1e6 * (BATCH_DISCOUNT if self.batch else 1)


# ----------------------------------------------------------------------------- batches
def _state_key(uid) -> str:
    return f"rescore_batches:{uid}"


def load_state(conn, uid) -> dict | None:
    """The batches this person's last `--batch` run submitted and has not collected yet, or None."""
    raw = db.get_meta(conn, _state_key(uid))
    return json.loads(raw) if raw else None


def save_state(conn, uid, state: dict | None) -> None:
    db.set_meta(conn, _state_key(uid), json.dumps(state) if state and state.get("batches") else "")


def submit(client, conn, uid, ids: list, rows: dict, model: str, profile: dict, min_pay: int, resume_text: str, log) -> dict:
    """Send the jobs to the Batch API in chunks, writing down each batch's id the moment it exists. Raises if a batch cannot be made;
    the ones already made stay in the saved state."""
    state = {"model": model, "created": time.time(), "batches": []}
    for start in range(0, len(ids), BATCH_CHUNK):
        jobs = {f"j{start + i}": job_id for i, job_id in enumerate(ids[start:start + BATCH_CHUNK])}      # custom ids: letters, digits, _ and - only
        requests = [{"custom_id": cid, "params": scorer.build_request(model, profile, rows[job_id], min_pay, resume_text, "1h")}
                    for cid, job_id in jobs.items()]
        made = client.messages.batches.create(requests=requests)
        state["batches"].append({"id": made.id, "jobs": jobs})
        save_state(conn, uid, state)
        log(f"Submitted batch {made.id}: {len(jobs):,} jobs.")
    return state


def wait(client, state: dict, log, sleep=time.sleep) -> None:
    """Ask every unfinished batch how it is doing until all have ended."""
    while True:
        counts = []
        for b in state["batches"]:
            info = client.messages.batches.retrieve(b["id"])
            b["status"] = info.processing_status
            rc = getattr(info, "request_counts", None)
            counts.append((getattr(rc, "processing", 0) or 0, len(b["jobs"])))
        if all(b["status"] == "ended" for b in state["batches"]):
            return
        left = sum(c[0] for c in counts)
        total = sum(c[1] for c in counts)
        log(f"  waiting for Anthropic: {total - left:,} of {total:,} jobs done ({sum(b['status'] == 'ended' for b in state['batches'])} of {len(state['batches'])} batches ended)")
        sleep(POLL_SECONDS)


def _error_text(result) -> str:
    err = getattr(result, "error", None)
    inner = getattr(err, "error", err)
    return str(getattr(inner, "message", None) or inner or result.type)


def collect(client, conn, uid, state: dict, tally: Tally, log) -> tuple[int, str | None]:
    """Apply the answers of every ended batch in `state` and forget each batch once it is applied. Returns (jobs applied or failed
    counted in `tally`, the first error text seen). A job whose answer is missing, errored, expired or not JSON keeps its old score."""
    first_error = None
    for b in list(state["batches"]):
        if b.get("status") != "ended":
            continue
        old = {r["id"]: r for r in conn.execute("SELECT id, score, verdict FROM jobs WHERE id = ANY(?)", (list(b["jobs"].values()),)).fetchall()}
        answered = set()
        for item in client.messages.batches.results(b["id"]):
            job_id = b["jobs"].get(item.custom_id)
            if job_id is None or job_id not in old:         # not one of ours, or a job that has been deleted since
                continue
            answered.add(job_id)
            result = item.result
            if result.type != "succeeded":
                tally.failed += 1
                first_error = first_error or f"{result.type}: {_error_text(result)}"
                continue
            try:
                res = scorer.parse_reply(result.message, state["model"])
            except (ValueError, TypeError):
                tally.failed += 1
                continue
            tally.saved(job_id, res, old[job_id])
        tally.failed += sum(1 for job_id in b["jobs"].values() if job_id in old and job_id not in answered)
        state["batches"].remove(b)
        save_state(conn, uid, state)
    return tally.ok, first_error


def run_batch(client, conn, uid, label: str, model: str, todo: list, pending: dict | None, profile: dict, min_pay: int,
              resume_text: str, price: dict, log, sleep=time.sleep) -> int:
    """The `--batch` run: collect the batches waiting from an earlier run, or submit new ones, then wait for them and apply the answers."""
    try:
        if pending:
            state = pending
            log(f"{label}: {len(state['batches'])} batch(es) submitted earlier with {state['model']} are still to be collected "
                f"(not submitting new ones; run the command again afterwards for any jobs left).")
        else:
            rows = {r["id"]: r for r in conn.execute("SELECT * FROM jobs WHERE id = ANY(?)", ([r["id"] for r in todo],)).fetchall()}
            state = submit(client, conn, uid, [r["id"] for r in todo if r["id"] in rows], rows, model, profile, min_pay, resume_text, log)
    except KeyboardInterrupt:
        log("Stopped. Run the same command again to collect what was submitted.")
        return 1
    except Exception as ex:
        message, _ = explain(ex)
        log(f"Could not submit the batch: {message}" + (" Batches already made are kept; run the command again to collect them." if load_state(conn, uid) else ""))
        return 1
    tally = Tally(conn, price, state["model"], True, "1h")
    first_error = None
    try:
        log("Waiting for the batch(es). This can take minutes or an hour or more; it is safe to stop (Ctrl-C) and run the same command later.")
        wait(client, state, log, sleep)
        _, first_error = collect(client, conn, uid, state, tally, log)
    except KeyboardInterrupt:
        tally.flush()
        log("Stopped waiting. The batch(es) keep running on Anthropic's side; run the same command again to collect the answers.")
        return 1
    except Exception as ex:
        tally.flush()
        message, _ = explain(ex)
        log(f"Stopped: {message} The batch(es) are kept; run the same command again to collect them.")
        return 1
    finally:
        tally.flush()
    report(tally, log, first_error)
    return 0


def report(tally: Tally, log, first_error: str | None = None, total: int | None = None) -> None:
    if tally.ok:
        log(f"Scored {tally.ok:,} of {total or tally.ok + tally.failed:,}" + (f"; {tally.failed:,} failed and kept their old score" if tally.failed else "")
            + f". The verdict changed on {tally.changed:,}, and scores moved {tally.shift / tally.ok:+.1f} points on average.")
    else:
        log(f"Nothing was scored; {tally.failed:,} failed.")
    log(f"Cost about {money(tally.cost())} at the prices in config.yaml" + (f" ({tally.read:,} of {tally.tin:,} input tokens were read from the cache)." if tally.read else "."))
    if first_error:
        log(f"The first problem reported: {first_error}")
    if tally.failed:
        log("Run the same command again to retry the ones that failed.")


# ----------------------------------------------------------------------------- the run
def run(who, *, redo_all: bool = False, limit: int | None = None, dry_run: bool = False, workers: int | None = None, batch: bool = False,
        client=None, log=print, sleep=time.sleep) -> int:
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
    price = scfg.get("price_per_million_tokens") or {}

    conn = pipeline._connect(uid)
    try:
        model = db.active_model(conn)      # the model this person is looking at, which is not always the one in config.yaml
        pending = load_state(conn, uid) if batch else None
        if pending and not dry_run:
            # Batches from an earlier run are waiting: collect them first, so the same jobs are never paid for twice.
            try:
                client = client or tailor.make_client()
            except UserError as ex:
                log(ex.message)
                return 1
            return run_batch(client, conn, uid, label, model, [], pending, profile, settings["min_pay"], "", price, log, sleep)
        todo, looking_at, done = pick(conn, model, redo_all, limit)
        resume_text = main.load_resume(userdata.home(uid))
        plain = estimate(todo, model, price)
        share = prefix_share(conn, todo, profile, resume_text, settings["min_pay"])
        if share * plain["tokens_in"] < MIN_CACHE_TOKENS:
            share = 0.0
        est = estimate(todo, model, price, share=share, batch=batch)
        log(f"{label}: {looking_at:,} scored jobs they are looking at (new or saved), {done:,} already scored with {model}.")
        extras = [name for name, on in (("batch pricing", batch), ("prompt caching", share)) if on]
        log(f"To score: {est['calls']:,}{' (limited)' if limit else ''}. About {est['tokens_in']:,} tokens in and {est['tokens_out']:,} out each, "
            f"about {money(est['cost'])} at the prices in config.yaml"
            + (f" with {' and '.join(extras)} (about {money(est['standard_cost'])} without)." if extras else "."))
        if pending:
            log("A batch from an earlier run is still waiting to be collected; run without --dry-run to collect it.")
        if dry_run or not todo:
            log("Dry run: nothing was changed." if dry_run else "Nothing to do.")
            return 0
        try:
            client = client or tailor.make_client()
        except UserError as ex:
            log(ex.message)
            return 1
        if batch:
            return run_batch(client, conn, uid, label, model, todo, None, profile, settings["min_pay"], resume_text, price, log, sleep)
        old = {r["id"]: r for r in todo}
        rows = {r["id"]: r for r in conn.execute("SELECT * FROM jobs WHERE id = ANY(?)", (list(old),)).fetchall()}
        n_workers = max(1, int(workers or scfg.get("workers", 8)))
        tally = Tally(conn, price, model, False, "5m")
        fatal = None
        interrupted = False
        pool = ThreadPoolExecutor(max_workers=n_workers)
        futures = {pool.submit(scorer.score_job, client, model, profile, rows[i], settings["min_pay"], resume_text, "5m"): i for i in old}
        try:
            for fut in as_completed(futures):
                job_id = futures[fut]
                try:
                    res = fut.result()
                except CancelledError:
                    continue
                except Exception as ex:
                    message, code = explain(ex)
                    tally.failed += 1
                    if code in pipeline.FATAL and fatal is None:
                        fatal = message       # every further call would fail the same way
                        for f in futures:
                            f.cancel()
                    continue
                tally.saved(job_id, res, old[job_id])       # the old score is replaced only now
                if tally.ok % 25 == 0:
                    log(f"  {tally.ok:,} of {len(old):,} scored")
        except KeyboardInterrupt:
            interrupted = True
            for f in futures:
                f.cancel()
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
            tally.flush()
        if tally.ok:
            report(tally, log, total=len(old))
        elif not fatal:
            log(f"Nothing was scored; {tally.failed:,} failed.")
        if fatal:
            log(f"Stopped: {fatal}")
            return 1
        if interrupted:
            log("Stopped. Run the same command again to carry on with the jobs not done yet.")
            return 1
        if tally.failed and not tally.ok:
            log("Run the same command again to retry the ones that failed.")
        return 0
    finally:
        conn.close()
