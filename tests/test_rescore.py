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


def reply(score, verdict="apply", tokens=(1000, 200)):
    return SimpleNamespace(content=[SimpleNamespace(text=json.dumps({"fit_score": score, "verdict": verdict, "reason": "ok", "dealbreakers": []}))],
                           usage=SimpleNamespace(input_tokens=tokens[0], output_tokens=tokens[1]))


class AuthenticationError(Exception):      # what the Anthropic package raises for a bad key; errors.explain knows it by name
    status_code = 401


class FakeClaude:
    """Answers every question with one score; a title in `bad` gets an answer that is not JSON, and `raises` makes every call raise."""

    def __init__(self, score=77, verdict="apply", bad=(), raises=None):
        self.score, self.verdict, self.bad, self.raises, self.titles, self.models = score, verdict, set(bad), raises, [], []
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **kw):
        if self.raises:
            raise self.raises
        title = re.search(r"Title: (.*)", kw["messages"][0]["content"]).group(1)
        self.titles.append(title)
        self.models.append(kw["model"])
        if title in self.bad:
            return SimpleNamespace(content=[SimpleNamespace(text="Sorry, I cannot do that.")], usage=SimpleNamespace(input_tokens=5, output_tokens=5))
        return reply(self.score, self.verdict)


class RescoreBase(Base):
    def setUp(self):
        super().setUp()
        self.scfg.update(model=NEW, workers=1, price_per_million_tokens={"input": 0.10, "output": 0.50})
        self.lines = []
        self.uid = self.user("me@example.com")

    def add(self, uid, jid, score, verdict="maybe", status="new", model=None, description="Build services in Python.", title=None):
        """A posting the user has, with the score a model gave it."""
        conn = pgtest.connect(self.schema, user=uid)
        try:
            db.add_job(conn, sources.Job(jid, "greenhouse", "Acme", title or f"Engineer {jid}", "Austin, TX", "https://x/" + jid, posted=RECENT,
                                         description=description), 0.0)
            db.save_score(conn, jid, score, verdict, {"fit_score": score, "verdict": verdict, "_in": 4000, "_out": 500, **({"_model": model} if model else {})})
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
        code = rescore.run(who if who is not None else self.uid, client=client, log=self.lines.append, **kw)
        return code, "\n".join(self.lines)


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
        self.assertEqual(self.usage(self.uid), {"rescore": (3, 3000, 600)})
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
        args = SimpleNamespace(user="7", all=True, limit=5, dry_run=True, workers=3)
        with mock.patch.object(rescore, "run", return_value=0) as run:
            main.cmd_rescore(args, {}, {}, None)
        run.assert_called_once_with("7", redo_all=True, limit=5, dry_run=True, workers=3, log=main.log)
        with mock.patch.object(rescore, "run", return_value=1), self.assertRaises(SystemExit) as ctx:
            main.cmd_rescore(args, {}, {}, None)
        self.assertEqual(ctx.exception.code, 1)

    def test_the_command_is_registered_with_its_options(self):
        with mock.patch.object(sys, "argv", ["main.py", "rescore", "--user", "1", "--dry-run", "--limit", "3"]), \
                mock.patch.object(main, "cmd_rescore") as cmd, mock.patch.object(main, "load_env"), \
                mock.patch.object(main.db, "connect"), mock.patch.object(main.discovery, "load_config", return_value={}):
            main.main()
        seen = cmd.call_args[0][0]
        self.assertEqual((seen.user, seen.dry_run, seen.limit, seen.all), ("1", True, 3, False))

    def test_a_score_records_the_model_that_gave_it(self):
        job = {"description": "d", "pay_min": None, "pay_max": None, "company": "c", "title": "t", "location": "l", "posted": None}
        client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: reply(70)))
        self.assertEqual(scorer.score_job(client, "some-model", {}, job, 0)["_model"], "some-model")


if __name__ == "__main__":
    unittest.main()
