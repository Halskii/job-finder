"""Scoring a person's jobs again (rescore.py, `python main.py rescore`): only theirs, only the jobs still open, the old score kept until a new
one arrives, finished jobs skipped on a second run, the cost counted under its own kind, and no way in from the web."""
import json
import re
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import db
import limits
import main
import rescore
import scorer
import sources
from tests import pgtest
from tests.test_pipeline import PROFILE, RECENT, Base

NEW = "claude-haiku-5-5"


def reply(score, verdict="apply", tokens=(1000, 200), cache=(0, 0)):
    return SimpleNamespace(content=[SimpleNamespace(text=json.dumps({"fit_score": score, "verdict": verdict, "reason": "ok", "dealbreakers": []}))],
                           usage=SimpleNamespace(input_tokens=tokens[0], output_tokens=tokens[1],
                                                 cache_read_input_tokens=cache[0], cache_creation_input_tokens=cache[1]))


class AuthenticationError(Exception):      # what the Anthropic package raises for a bad key; errors.explain knows it by name
    status_code = 401


class FakeClaude:
    """Answers every question with one score (`by_title` gives a title its own); a title in `bad` gets an answer that is not JSON, one in
    `errored` an error (in a batch), and `raises` makes every call raise. `cached` notes, for each call, whether the profile was marked for caching."""

    def __init__(self, score=77, verdict="apply", bad=(), raises=None, by_title=None, errored=(), polls=0):
        self.score, self.verdict, self.bad, self.raises, self.titles, self.models = score, verdict, set(bad), raises, [], []
        self.by_title, self.errored, self.cached = by_title or {}, set(errored), []
        self.batches = FakeBatches(self, polls)
        self.messages = SimpleNamespace(create=self.create, batches=self.batches)

    def answer(self, title):
        """What the model says about a job: not JSON, or a score."""
        if title in self.bad:
            return SimpleNamespace(content=[SimpleNamespace(text="Sorry, I cannot do that.")], usage=SimpleNamespace(input_tokens=5, output_tokens=5))
        return reply(self.by_title.get(title, self.score), self.verdict, cache=(800, 0))

    def create(self, **kw):
        if self.raises:
            raise self.raises
        title = re.search(r"Title: (.*)", scorer.request_text(kw)).group(1)
        self.titles.append(title)
        self.models.append(kw["model"])
        content = kw["messages"][0]["content"]
        self.cached.append(not isinstance(content, str) and "cache_control" in content[0])
        return self.answer(title)


class FakeBatches:
    """The Message Batches API: `create` takes requests, `retrieve` says they are in progress for `polls` questions and then ended, and
    `results` gives the answers in the opposite order to the requests, as the real one may give them in any order."""

    def __init__(self, claude, polls=0):
        self.claude, self.polls, self.made, self.asked = claude, polls, {}, {}

    def create(self, requests):
        if self.claude.raises:
            raise self.claude.raises
        bid = f"msgbatch_{len(self.made) + 1}"
        self.made[bid] = requests
        return SimpleNamespace(id=bid, processing_status="in_progress")

    def retrieve(self, bid):
        self.asked[bid] = self.asked.get(bid, 0) + 1
        ended = self.asked[bid] > self.polls
        return SimpleNamespace(id=bid, processing_status="ended" if ended else "in_progress",
                               request_counts=SimpleNamespace(processing=0 if ended else len(self.made[bid])))

    def results(self, bid):
        out = []
        for r in self.made[bid]:
            title = re.search(r"Title: (.*)", scorer.request_text(r["params"])).group(1)
            if title in self.claude.errored:
                result = SimpleNamespace(type="errored", error=SimpleNamespace(error=SimpleNamespace(type="invalid_request_error", message="no access")))
            else:
                result = SimpleNamespace(type="succeeded", message=self.claude.answer(title))
            out.append(SimpleNamespace(custom_id=r["custom_id"], result=result))
        return out[::-1]


class RescoreBase(Base):
    def setUp(self):
        super().setUp()
        self.scfg.update(model=NEW, workers=1, price_per_million_tokens={"input": 0.10, "output": 0.50})
        self.lines = []
        self.uid = self.user("me@example.com")

    def tearDown(self):
        self.admin.close()      # the base class leaves it for the garbage collector, which is too slow for a run of this many tests against one server
        super().tearDown()

    def add(self, uid, jid, score, verdict="maybe", status="new", model=None, description="Build services in Python.", title=None):
        """A posting the user has, with the score a model gave it."""
        conn = pgtest.connect(self.schema, user=uid)
        try:
            db.add_job(conn, sources.Job(jid, "greenhouse", "Acme", title or f"Engineer {jid}", "Austin, TX", "https://x/" + jid, posted=RECENT,
                                         description=description), 0.0)
            analysis = {"fit_score": score, "verdict": verdict, "_in": 4000, "_out": 500, **({"_model": model} if model else {})}
            if model and model != NEW:       # a score made before the model changed: it is what the person still sees, as it was saved then
                conn.execute("UPDATE user_jobs SET score = ?, verdict = ?, analysis = ? WHERE job_id = ?", (score, verdict, json.dumps(analysis), jid))
                conn.commit()
            else:
                db.save_score(conn, jid, score, verdict, analysis)
            if status != "new":
                db.set_status_exact(conn, jid, status)
        finally:
            conn.close()

    def state(self, uid):
        conn = pgtest.connect(self.schema, user=uid)
        try:
            return {r["id"]: r for r in conn.execute("SELECT id, score, verdict, analysis, status FROM jobs ORDER BY id").fetchall()}
        finally:
            conn.close()

    def usage(self, uid):
        conn = pgtest.connect(self.schema, user=uid)
        try:
            return {r["kind"]: (r["calls"], r["tokens_in"], r["tokens_out"]) for r in conn.execute("SELECT kind, calls, tokens_in, tokens_out FROM usage").fetchall()}
        finally:
            conn.close()

    def run_rescore(self, who=None, client=None, **kw):
        kw.setdefault("sleep", lambda seconds: None)
        code = rescore.run(who if who is not None else self.uid, client=client, log=self.lines.append, **kw)
        return code, "\n".join(self.lines)

    def waiting(self):
        """The batches saved as still to be collected for the test person."""
        conn = pgtest.connect(self.schema, user=self.uid)
        try:
            return rescore.load_state(conn, self.uid)
        finally:
            conn.close()


class Choosing(RescoreBase):
    def test_which_model_gave_a_score(self):
        self.assertEqual(rescore.scored_with(json.dumps({"_model": "x"})), "x")
        for nothing in (None, "", "{}", "not json", "[1]"):
            self.assertIsNone(rescore.scored_with(nothing))

    def test_only_scored_open_jobs_with_text_not_done_with_this_model_best_first(self):
        self.add(self.uid, "a", 40)
        self.add(self.uid, "b", 90, status="saved")
        self.add(self.uid, "c", 70, model=NEW)                       # done already
        self.add(self.uid, "d", 80, status="applied")                 # finished with
        self.add(self.uid, "e", 60, status="skipped")
        self.add(self.uid, "f", 50, description="")                   # no text to score
        self.add(self.uid, "g", 65, model="claude-haiku-4-5-20251001")
        conn = pgtest.connect(self.schema, user=self.uid)
        todo, looking_at, done = rescore.pick(conn, NEW)
        self.assertEqual([r["id"] for r in todo], ["b", "g", "a"])
        self.assertEqual((looking_at, done), (4, 1))                  # a, b, c, g are open with text; c is done
        self.assertEqual([r["id"] for r in rescore.pick(conn, NEW, redo_all=True)[0]], ["b", "c", "g", "a"])
        self.assertEqual([r["id"] for r in rescore.pick(conn, NEW, limit=2)[0]], ["b", "g"])
        conn.close()

    def test_the_estimate_uses_what_the_last_calls_took(self):
        rows = [{"analysis": json.dumps({"_in": 4000, "_out": 500})}, {"analysis": json.dumps({"_in": 6000, "_out": 700})}, {"analysis": "{}"}]
        e = rescore.estimate(rows, "claude-haiku-4-5-20251001", {"input": 1.0, "output": 5.0})
        self.assertEqual((e["calls"], e["tokens_in"], e["tokens_out"]), (3, 5000, 600))
        self.assertAlmostEqual(e["cost"], 3 * (5000 * 1.0 + 600 * 5.0) / 1e6)
        e = rescore.estimate(rows, NEW, {"input": 0.10, "output": 0.50})
        self.assertEqual(e["tokens_out"], rescore.THINKING_OUT)       # a Claude 5 model writes its thinking too
        self.assertEqual(e["tokens_in"], round(5000 * rescore.NEWER_TOKENIZER))      # and counts the same text as more tokens than Haiku 4.5 did
        done_by_new = [{"analysis": json.dumps({"_in": 5000, "_out": 900, "_model": NEW})}]
        self.assertEqual(rescore.estimate(done_by_new, NEW, {})["tokens_in"], 5000)      # already counted that way: no correction
        self.assertEqual(rescore.estimate([], NEW, {})["cost"], 0)


class Running(RescoreBase):
    def test_a_dry_run_counts_and_estimates_and_changes_nothing(self):
        self.add(self.uid, "a", 40)
        self.add(self.uid, "b", 90)
        before = self.state(self.uid)
        client = FakeClaude()
        code, out = self.run_rescore(client=client, dry_run=True)
        self.assertEqual(code, 0)
        self.assertEqual(client.titles, [])
        self.assertEqual(self.state(self.uid), before)
        self.assertEqual(self.usage(self.uid), {})
        self.assertIn("To score: 2", out)
        self.assertIn("about $", out)
        self.assertIn("nothing was changed", out)

    def test_scores_are_replaced_in_place_and_remember_the_model(self):
        self.add(self.uid, "a", 40, verdict="skip")
        self.add(self.uid, "b", 90, verdict="apply")
        code, out = self.run_rescore(client=FakeClaude(score=75, verdict="maybe"))
        self.assertEqual(code, 0)
        state = self.state(self.uid)
        self.assertEqual([(state[j]["score"], state[j]["verdict"]) for j in "ab"], [(75, "maybe"), (75, "maybe")])
        self.assertTrue(all(json.loads(state[j]["analysis"])["_model"] == NEW for j in "ab"))
        self.assertIn("Scored 2 of 2", out)
        self.assertIn("verdict changed on 2", out)
        self.assertIn("+10.0 points", out)                           # (+35, -15) on average

    def test_the_model_asked_is_the_configured_one(self):
        self.add(self.uid, "a", 40)
        client = FakeClaude()
        self.run_rescore(client=client)
        self.assertEqual(client.models, [NEW])

    def test_jobs_already_done_are_skipped_on_a_second_run_unless_all_is_asked_for(self):
        self.add(self.uid, "a", 40)
        self.add(self.uid, "b", 90)
        first = FakeClaude()
        self.run_rescore(client=first)
        self.assertEqual(len(first.titles), 2)
        self.lines.clear()
        second = FakeClaude()
        code, out = self.run_rescore(client=second)
        self.assertEqual((code, second.titles), (0, []))
        self.assertIn("Nothing to do", out)
        third = FakeClaude()
        self.run_rescore(client=third, redo_all=True)
        self.assertEqual(len(third.titles), 2)

    def test_limit_does_the_best_first_and_a_later_run_does_the_rest(self):
        for jid, score in (("a", 30), ("b", 80), ("c", 55)):
            self.add(self.uid, jid, score)
        client = FakeClaude(score=61)
        self.run_rescore(client=client, limit=2)
        self.assertEqual(client.titles, ["Engineer b", "Engineer c"])
        state = self.state(self.uid)
        self.assertEqual([state[j]["score"] for j in "abc"], [30, 61, 61])
        self.lines.clear()
        later = FakeClaude(score=61)
        self.run_rescore(client=later)
        self.assertEqual(later.titles, ["Engineer a"])

    def test_applied_and_skipped_jobs_are_left_alone(self):
        self.add(self.uid, "a", 40)
        self.add(self.uid, "b", 88, status="applied")
        self.add(self.uid, "c", 20, status="skipped")
        client = FakeClaude(score=50)
        self.run_rescore(client=client)
        self.assertEqual(client.titles, ["Engineer a"])
        self.assertEqual([self.state(self.uid)[j]["score"] for j in "abc"], [50, 88, 20])

    def test_a_job_whose_call_fails_keeps_its_score_and_is_tried_again_next_time(self):
        self.add(self.uid, "a", 40)
        self.add(self.uid, "b", 90)
        code, out = self.run_rescore(client=FakeClaude(score=70, bad={"Engineer b"}))
        self.assertEqual(code, 0)
        state = self.state(self.uid)
        self.assertEqual((state["a"]["score"], state["b"]["score"]), (70, 90))        # b still has the score it had
        self.assertIn("1 failed and kept their old score", out)
        self.assertIn("retry", out)
        self.lines.clear()
        again = FakeClaude(score=70)
        self.run_rescore(client=again)
        self.assertEqual(again.titles, ["Engineer b"])
        self.assertEqual(self.state(self.uid)["b"]["score"], 70)

    def test_a_failure_every_call_would_repeat_stops_the_run_and_changes_nothing(self):
        self.add(self.uid, "a", 40)
        self.add(self.uid, "b", 90)
        before = {j: s["score"] for j, s in self.state(self.uid).items()}
        code, out = self.run_rescore(client=FakeClaude(raises=AuthenticationError("bad key")))
        self.assertEqual(code, 1)
        self.assertIn("Stopped", out)
        self.assertEqual({j: s["score"] for j, s in self.state(self.uid).items()}, before)

    def test_the_calls_are_counted_as_rescoring_and_not_as_the_persons_daily_scoring(self):
        for jid in "abc":
            self.add(self.uid, jid, 50)
        self.run_rescore(client=FakeClaude())
        self.assertEqual(self.usage(self.uid), {"rescore": (3, 3 * (1000 + 800), 600)})      # the tokens sent count whether or not they came from the cache
        conn = pgtest.connect(self.schema, user=self.uid)
        self.assertEqual(db.usage_today(conn, "score"), 0)               # their own daily allowance is untouched
        conn.close()

    def test_usage_is_written_in_steps_so_a_killed_run_is_still_counted(self):
        for i in range(5):
            self.add(self.uid, f"j{i}", 50)
        with mock.patch.object(rescore, "FLUSH_EVERY", 2):
            self.run_rescore(client=FakeClaude())
        self.assertEqual(self.usage(self.uid)["rescore"][0], 5)

    def test_it_never_touches_another_persons_jobs(self):
        other = self.user("other@example.com")
        self.add(self.uid, "a", 40)
        self.add(other, "z", 33)
        client = FakeClaude(score=99)
        self.run_rescore(client=client)
        self.assertEqual(client.titles, ["Engineer a"])
        self.assertEqual(self.state(other)["z"]["score"], 33)
        self.assertEqual(self.usage(other), {})


class WhoAndWhen(RescoreBase):
    def test_a_person_is_found_by_id_or_by_email_in_any_case(self):
        self.add(self.uid, "a", 40)
        for who in (str(self.uid), "ME@Example.com"):
            self.lines.clear()
            code, _ = self.run_rescore(who=who, client=FakeClaude(), dry_run=True)
            self.assertEqual(code, 0, who)

    def test_an_unknown_person_or_one_without_a_profile_is_refused(self):
        code, out = self.run_rescore(who="nobody@example.com", client=FakeClaude())
        self.assertEqual(code, 1)
        self.assertIn("No user", out)
        bare = self.user("bare@example.com", profile=None)
        self.lines.clear()
        code, out = self.run_rescore(who=bare, client=FakeClaude())
        self.assertEqual(code, 1)
        self.assertIn("no profile", out)

    def test_the_pause_switch_stops_it_but_a_dry_run_is_allowed(self):
        self.add(self.uid, "a", 40)
        with mock.patch.object(limits, "paused", return_value=True):
            client = FakeClaude()
            code, _ = self.run_rescore(client=client)
            self.assertEqual((code, client.titles), (1, []))
            self.lines.clear()
            self.assertEqual(self.run_rescore(client=client, dry_run=True)[0], 0)

    def test_without_an_api_key_it_says_so(self):
        self.add(self.uid, "a", 40)
        with mock.patch.object(rescore.tailor, "make_client", side_effect=rescore.UserError("No Anthropic API key found.", "no_api_key")):
            code, out = self.run_rescore()
        self.assertEqual(code, 1)
        self.assertIn("No Anthropic API key", out)


class OnlyOnTheServer(unittest.TestCase):
    """Rescoring is admin-only because it is a command and not a page: nothing in the web app reaches it."""

    def test_no_web_route_or_page_uses_it(self):
        import web
        import viewer
        for module in (web, viewer):
            self.assertNotIn("rescore", Path(module.__file__).read_text(encoding="utf-8").lower(), module.__name__)

    def test_the_command_passes_its_options_and_exits_with_the_result(self):
        args = SimpleNamespace(user="7", all=True, limit=5, dry_run=True, workers=3, batch=True)
        with mock.patch.object(rescore, "run", return_value=0) as run:
            main.cmd_rescore(args, {}, {}, None)
        run.assert_called_once_with("7", redo_all=True, limit=5, dry_run=True, workers=3, batch=True, log=main.log)
        with mock.patch.object(rescore, "run", return_value=1), self.assertRaises(SystemExit) as ctx:
            main.cmd_rescore(args, {}, {}, None)
        self.assertEqual(ctx.exception.code, 1)

    def test_the_command_is_registered_with_its_options(self):
        with mock.patch.object(sys, "argv", ["main.py", "rescore", "--user", "1", "--dry-run", "--limit", "3"]), \
                mock.patch.object(main, "cmd_rescore") as cmd, mock.patch.object(main, "load_env"), \
                mock.patch.object(main.db, "connect"), mock.patch.object(main.discovery, "load_config", return_value={}):
            main.main()
        seen = cmd.call_args[0][0]
        self.assertEqual((seen.user, seen.dry_run, seen.limit, seen.all, seen.batch), ("1", True, 3, False, False))

    def test_a_score_records_the_model_that_gave_it(self):
        job = {"description": "d", "pay_min": None, "pay_max": None, "company": "c", "title": "t", "location": "l", "posted": None}
        client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: reply(70)))
        self.assertEqual(scorer.score_job(client, "some-model", {}, job, 0)["_model"], "some-model")


class Caching(RescoreBase):
    JOB = {"description": "Build services.", "pay_min": None, "pay_max": None, "company": "Acme", "title": "Engineer", "location": "Austin, TX", "posted": None}

    def test_splitting_the_message_for_caching_changes_nothing_the_model_reads(self):
        plain = scorer.build_request(NEW, PROFILE, self.JOB, 90000, "my resume")
        five = scorer.build_request(NEW, PROFILE, self.JOB, 90000, "my resume", cache="5m")
        hour = scorer.build_request(NEW, PROFILE, self.JOB, 90000, "my resume", cache="1h")
        self.assertIsInstance(plain["messages"][0]["content"], str)
        self.assertEqual(scorer.request_text(five), plain["messages"][0]["content"])
        self.assertEqual(scorer.request_text(hour), plain["messages"][0]["content"])
        shared, posting = five["messages"][0]["content"]
        self.assertEqual(shared["cache_control"], {"type": "ephemeral"})
        self.assertEqual(hour["messages"][0]["content"][0]["cache_control"], {"type": "ephemeral", "ttl": "1h"})
        self.assertNotIn("cache_control", posting)
        self.assertTrue(shared["text"].startswith("CANDIDATE PROFILE") and "my resume" in shared["text"])
        self.assertTrue(posting["text"].startswith("JOB POSTING"))          # only the posting changes from job to job

    def test_the_cached_part_is_the_same_for_every_job(self):
        other = dict(self.JOB, title="Platform Engineer", description="Something else.")
        a = scorer.build_request(NEW, PROFILE, self.JOB, 90000, "r", cache="5m")["messages"][0]["content"][0]
        b = scorer.build_request(NEW, PROFILE, other, 90000, "r", cache="5m")["messages"][0]["content"][0]
        self.assertEqual(a, b)

    def test_cached_tokens_count_in_the_total_so_estimates_are_not_fooled(self):
        usage = SimpleNamespace(input_tokens=100, output_tokens=50, cache_read_input_tokens=4000, cache_creation_input_tokens=500)
        data = scorer.parse_reply(SimpleNamespace(content=[SimpleNamespace(text='{"fit_score": 70}')], usage=usage), NEW)
        self.assertEqual((data["_in"], data["_out"], data["_cache_read"], data["_cache_write"]), (4600, 50, 4000, 500))
        plain = scorer.parse_reply(reply(70), NEW)
        self.assertNotIn("_cache_read", plain)

    def test_a_rescore_marks_the_profile_for_caching_but_scoring_by_default_does_not(self):
        self.add(self.uid, "a", 40)
        client = FakeClaude()
        self.run_rescore(client=client)
        self.assertEqual(client.cached, [True])
        sent = []
        spy = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: sent.append(kw) or reply(60)))
        scorer.score_job(spy, NEW, PROFILE, self.JOB, 0)
        self.assertIsInstance(sent[0]["messages"][0]["content"], str)      # the live scoring is unchanged

    def test_reading_the_cache_is_cheaper_by_model(self):
        self.assertEqual(rescore.cache_read_multiplier("claude-opus-5-5"), 0.05)
        self.assertEqual(rescore.cache_read_multiplier("claude-sonnet-5-5"), 0.05)
        self.assertEqual(rescore.cache_read_multiplier("claude-fable-5-2"), 0.025)
        self.assertEqual(rescore.cache_read_multiplier("claude-haiku-5-5"), 0.1)

    def test_the_estimate_drops_with_caching_and_again_with_batch(self):
        rows = [{"analysis": json.dumps({"_in": 5000, "_out": 900, "_model": "claude-opus-5-5"})}] * 10
        price = {"input": 4.0, "output": 20.0}
        plain = rescore.estimate(rows, "claude-opus-5-5", price)
        cached = rescore.estimate(rows, "claude-opus-5-5", price, share=0.8)
        batched = rescore.estimate(rows, "claude-opus-5-5", price, share=0.8, batch=True)
        self.assertAlmostEqual(plain["cost"], plain["standard_cost"])
        self.assertAlmostEqual(cached["standard_cost"], plain["cost"])
        self.assertLess(cached["cost"], plain["cost"])
        self.assertLess(batched["cost"], cached["cost"])
        self.assertGreater(batched["cost"], cached["cost"] / 2 * 0.9)         # half price, but caching saves a little less in a batch
        # 5,000 tokens in at $4 and 1,000 out (a Claude 5 model's thinking included) at $20; with caching the 80% shared part costs 95% * 0.05 + 5% * 1.25 of the input price
        expected_in = 5000 * (0.2 + 0.8 * (0.95 * 0.05 + 0.05 * 1.25)) * 4.0
        self.assertAlmostEqual(cached["cost"], 10 * (expected_in + rescore.THINKING_OUT * 20.0) / 1e6)

    def test_the_run_reports_what_the_cache_saved(self):
        self.add(self.uid, "a", 40)
        _, out = self.run_rescore(client=FakeClaude())
        self.assertIn("Cost about $", out)
        self.assertIn("were read from the cache", out)


class Batching(RescoreBase):
    def test_a_batch_scores_every_job_through_the_batch_api_and_not_one_by_one(self):
        self.add(self.uid, "a", 40, verdict="skip")
        self.add(self.uid, "b", 90, verdict="apply")
        client = FakeClaude(score=75, verdict="maybe")
        code, out = self.run_rescore(client=client, batch=True)
        self.assertEqual(code, 0)
        self.assertEqual(client.titles, [])                                   # no call outside the batch
        (requests,) = client.batches.made.values()
        self.assertEqual(len(requests), 2)
        for r in requests:
            self.assertRegex(r["custom_id"], r"^[a-zA-Z0-9_-]{1,64}$")        # what the API accepts: job ids with colons would not be
            self.assertEqual(r["params"]["messages"][0]["content"][0]["cache_control"], {"type": "ephemeral", "ttl": "1h"})
            self.assertEqual(r["params"]["model"], NEW)
        state = self.state(self.uid)
        self.assertEqual([(state[j]["score"], state[j]["verdict"]) for j in "ab"], [(75, "maybe"), (75, "maybe")])
        self.assertTrue(all(json.loads(state[j]["analysis"])["_model"] == NEW for j in "ab"))
        self.assertIn("Scored 2 of 2", out)
        self.assertIsNone(self.waiting())                                     # nothing left to collect
        self.assertEqual(self.usage(self.uid)["rescore"][0], 2)

    def test_answers_that_come_back_in_any_order_land_on_the_right_job(self):
        for jid, score in (("a", 10), ("b", 20), ("c", 30)):
            self.add(self.uid, jid, score)
        self.run_rescore(client=FakeClaude(by_title={"Engineer a": 71, "Engineer b": 72, "Engineer c": 73}), batch=True)
        self.assertEqual([self.state(self.uid)[j]["score"] for j in "abc"], [71, 72, 73])

    def test_jobs_with_colons_in_their_ids_are_mapped_through_short_custom_ids(self):
        self.add(self.uid, "gh:stripe:123", 40, title="Stripe engineer")
        client = FakeClaude(score=66)
        self.run_rescore(client=client, batch=True)
        self.assertEqual(self.state(self.uid)["gh:stripe:123"]["score"], 66)

    def test_a_big_list_is_sent_in_chunks(self):
        for i in range(5):
            self.add(self.uid, f"j{i}", 50 + i)
        client = FakeClaude(score=61)
        with mock.patch.object(rescore, "BATCH_CHUNK", 2):
            code, out = self.run_rescore(client=client, batch=True)
        self.assertEqual([len(r) for r in client.batches.made.values()], [2, 2, 1])
        self.assertEqual({s["score"] for s in self.state(self.uid).values()}, {61})
        self.assertIn("Scored 5 of 5", out)

    def test_a_job_whose_answer_fails_keeps_its_score_and_is_done_by_the_next_run(self):
        for jid, score in (("a", 40), ("b", 90), ("c", 55)):
            self.add(self.uid, jid, score)
        client = FakeClaude(score=70, bad={"Engineer b"}, errored={"Engineer c"})
        code, out = self.run_rescore(client=client, batch=True)
        self.assertEqual(code, 0)
        self.assertEqual([self.state(self.uid)[j]["score"] for j in "abc"], [70, 90, 55])
        self.assertIn("2 failed and kept their old score", out)
        self.assertIn("no access", out)                                         # the reason the API gave
        self.assertIn("run the same command again", out.lower())
        self.assertIsNone(self.waiting())
        again = FakeClaude(score=70)
        self.run_rescore(client=again, batch=True)
        (second,) = again.batches.made.values()
        self.assertEqual(sorted(re.search(r"Title: (.*)", scorer.request_text(r["params"])).group(1) for r in second), ["Engineer b", "Engineer c"])

    def test_stopping_the_wait_loses_nothing_and_the_next_run_collects_the_same_batch(self):
        self.add(self.uid, "a", 40)
        self.add(self.uid, "b", 90)
        client = FakeClaude(score=72, polls=1)

        def interrupt(seconds):
            raise KeyboardInterrupt

        code, out = self.run_rescore(client=client, batch=True, sleep=interrupt)
        self.assertEqual(code, 1)
        self.assertIn("run the same command again", out)
        self.assertEqual([self.state(self.uid)[j]["score"] for j in "ab"], [40, 90])      # nothing applied yet
        self.assertEqual(len(self.waiting()["batches"]), 1)                                # but the batch is remembered
        self.lines.clear()
        code, out = self.run_rescore(client=client, batch=True)
        self.assertEqual(code, 0)
        self.assertEqual(len(client.batches.made), 1)                                      # the same batch: not paid for twice
        self.assertIn("submitted earlier", out)
        self.assertEqual([self.state(self.uid)[j]["score"] for j in "ab"], [72, 72])
        self.assertIsNone(self.waiting())
        self.assertEqual(self.usage(self.uid)["rescore"][0], 2)

    def test_a_batch_that_cannot_be_made_says_so_and_changes_nothing(self):
        self.add(self.uid, "a", 40)
        code, out = self.run_rescore(client=FakeClaude(raises=AuthenticationError("bad key")), batch=True)
        self.assertEqual(code, 1)
        self.assertIn("Could not submit", out)
        self.assertEqual(self.state(self.uid)["a"]["score"], 40)
        self.assertIsNone(self.waiting())

    def test_a_dry_run_with_batch_shows_the_lower_price_and_submits_nothing(self):
        self.add(self.uid, "a", 40)
        client = FakeClaude()
        code, out = self.run_rescore(client=client, batch=True, dry_run=True)
        self.assertEqual((code, client.batches.made), (0, {}))
        self.assertIn("batch pricing", out)
        self.assertIsNone(self.waiting())

    def test_waiting_batches_belong_to_their_person(self):
        other = self.user("other@example.com")
        self.add(self.uid, "a", 40)
        self.add(other, "z", 33)
        client = FakeClaude(polls=1)

        def interrupt(seconds):
            raise KeyboardInterrupt

        self.run_rescore(client=client, batch=True, sleep=interrupt)
        conn = pgtest.connect(self.schema, user=other)
        self.assertIsNone(rescore.load_state(conn, other))
        conn.close()
        self.assertEqual(self.state(other)["z"]["score"], 33)

    def test_without_batch_nothing_is_submitted(self):
        self.add(self.uid, "a", 40)
        client = FakeClaude()
        self.run_rescore(client=client)
        self.assertEqual(client.batches.made, {})


if __name__ == "__main__":
    unittest.main()
