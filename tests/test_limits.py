"""Daily limits on the calls that cost money, and the pause switch."""
import json
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

import db
import limits
import pipeline
import sources
import tailor
import userdata
import web
from errors import BadModelAnswer, UserError
from tests import pgtest, webtest
from tests.test_pipeline import Base, add_posting


def configured(**given):
    """config_value, with `limits:` replaced by these settings."""
    real = tailor.config_value
    return lambda key, default=None: given if key == "limits" else real(key, default)


class LimitTests(Base):
    def conn(self, uid):
        return pgtest.connect(self.schema, user=uid)

    def test_a_person_stops_at_their_daily_limit_with_a_clear_message(self):
        uid = self.user()
        conn = self.conn(uid)
        with mock.patch.object(tailor, "config_value", configured(per_user_per_day={"tailor": 2})):
            for _ in range(2):
                limits.check(conn, "tailor")
                db.add_usage(conn, "tailor")
            with self.assertRaises(UserError) as ctx:
                limits.check(conn, "tailor")
        self.assertEqual(ctx.exception.code, "limit_reached")
        self.assertIn("today's 2 resume and cover letter sets", ctx.exception.message)
        self.assertTrue(ctx.exception.message.endswith("."))
        conn.close()

    def test_the_whole_site_has_a_limit_too_and_it_counts_everyones_calls(self):
        a, b = self.user("a@example.com"), self.user("b@example.com")
        ca, cb = self.conn(a), self.conn(b)
        with mock.patch.object(tailor, "config_value", configured(all_users_per_day={"tips": 3}, per_user_per_day={"tips": 50})):
            db.add_usage(ca, "tips", 2)
            db.add_usage(cb, "tips", 1)
            self.assertEqual(db.usage_total_today(ca, "tips"), 3)
            with self.assertRaises(UserError) as ctx:
                limits.check(cb, "tips")
        self.assertIn("whole site", ctx.exception.message)
        self.assertEqual((db.usage_today(ca, "tips"), db.usage_today(cb, "tips")), (2, 1))     # each person still sees only their own
        ca.close()
        cb.close()

    def test_the_pause_switch_stops_every_paid_call_at_once(self):
        conn = self.conn(self.user())
        with mock.patch.object(tailor, "config_value", configured(paused=True)):
            for kind in ("score", "tailor", "tips", "analyze"):
                with self.assertRaises(UserError) as ctx:
                    limits.check(conn, kind)
                self.assertEqual(ctx.exception.code, "paused")
        limits.check(conn, "tailor")           # switched off again: fine
        conn.close()

    def test_what_the_config_does_not_say_keeps_its_default(self):
        with mock.patch.object(tailor, "config_value", configured(per_user_per_day={"tailor": 1})):
            s = limits.settings()
        self.assertEqual((s["per_user_per_day"]["tailor"], s["per_user_per_day"]["tips"], s["all_users_per_day"]["score"], s["paused"]), (1, 12, 4000, False))

    def test_a_call_is_counted_when_it_works_and_not_when_the_request_was_the_problem(self):
        conn = self.conn(self.user())
        with limits.paid_call(conn, "analyze") as spent:
            spent.tokens(100, 20)
        row = conn.execute("SELECT calls, tokens_in, tokens_out FROM usage WHERE kind = 'analyze'").fetchone()
        self.assertEqual((row["calls"], row["tokens_in"], row["tokens_out"]), (1, 100, 20))
        with self.assertRaises(UserError):
            with limits.paid_call(conn, "analyze"):
                raise UserError("Add your resume first.", "no_resume")              # nothing was spent
        with self.assertRaises(BadModelAnswer):
            with limits.paid_call(conn, "analyze"):
                raise BadModelAnswer()                                                # the model was paid for, the answer was bad
        with self.assertRaises(RuntimeError):
            with limits.paid_call(conn, "analyze"):
                raise RuntimeError("the network dropped")                            # not counted
        with limits.paid_call(conn, "analyze") as spent:
            spent.free = True                                                         # an answer that was already saved
        self.assertEqual(db.usage_today(conn, "analyze"), 2)
        conn.close()


class ScoringLimitTests(Base):
    def test_the_whole_sites_scoring_limit_stops_a_run_and_the_rest_wait(self):
        for i in range(6):
            add_posting(self.admin, f"j{i}", "Software Engineer", company=f"C{i}", description="Python " * (6 - i))
        a, b = self.user("a@example.com"), self.user("b@example.com")
        client, calls = self.client()
        with mock.patch.object(tailor, "config_value", configured(all_users_per_day={"score": 8})):
            st_a = pipeline.run_user(a, client)
            st_b = pipeline.run_user(b, client)
        self.assertEqual((st_a["state"], st_a["waiting"]), ("done", 0))
        self.assertEqual((st_b["state"], st_b["waiting"], len(calls)), ("done", 4, 8))      # 6 for a, then only 2 were left for the whole site
        self.assertIn("tomorrow", st_b["message"])

    def test_when_paused_nothing_is_scored_and_the_page_says_why(self):
        add_posting(self.admin, "j0", "Software Engineer")
        uid = self.user()
        client, calls = self.client()
        with mock.patch.object(tailor, "config_value", configured(paused=True)):
            st = pipeline.run_user(uid, client)
        self.assertEqual((len(calls), st["state"], st["waiting"]), (0, "done", 1))
        self.assertIn("paused", st["message"])
        self.assertEqual(st["matched"], 1)                                                  # matching is free, so it still happened


class LimitsOverHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.site = webtest.Site(cls.tmp.name)
        cls.base = cls.site.base
        conn = cls.site.db()
        db.add_job(conn, sources.Job("gh:l:1", "greenhouse", "Co", "Software Engineer", "Austin, TX", "https://x", description="d"), 1.0)
        conn.close()

    @classmethod
    def tearDownClass(cls):
        cls.site.stop()
        cls.tmp.cleanup()

    def post(self, path, body, token=None):
        req = urllib.request.Request(self.base + path, method="POST", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "Cookie": f"{web.COOKIE}={token or self.site.token}"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8")

    def test_tailoring_stops_at_the_limit_and_counts_the_tokens(self):
        done = {"files": ["a.pdf"], "tokens": {"in": 1000, "out": 500}}
        with mock.patch.object(tailor, "config_value", configured(per_user_per_day={"tailor": 2})), mock.patch.object(web.viewer, "tailor_for", return_value=done):
            self.assertEqual(self.post("/api/tailor", {"id": "gh:l:1"})[0], 200)
            self.assertEqual(self.post("/api/tailor", {"id": "gh:l:1"})[0], 200)
            status, body = self.post("/api/tailor", {"id": "gh:l:1"})
        self.assertEqual((status, json.loads(body)["code"]), (429, "limit_reached"))
        conn = self.site.db()
        row = conn.execute("SELECT calls, tokens_in, tokens_out FROM usage WHERE kind = 'tailor'").fetchone()
        conn.close()
        self.assertEqual((row["calls"], row["tokens_in"], row["tokens_out"]), (2, 2000, 1000))

    def test_a_tailoring_that_fails_before_anything_is_spent_is_not_counted(self):
        who, token = self.site.new_user("untried@example.com")
        conn = self.site.db(who)
        db.add_job(conn, sources.Job("gh:l:9", "greenhouse", "Co9", "Dev", "Austin, TX", "https://x", description="d"), 1.0)
        conn.close()
        with mock.patch.object(web.viewer, "tailor_for", side_effect=UserError("Upload your resume first.", "no_resume")):
            self.assertEqual(self.post("/api/tailor", {"id": "gh:l:9"}, token)[0], 422)
        c = self.site.db(who)
        self.assertEqual(db.usage_today(c, "tailor"), 0)
        c.close()

    def test_reading_a_resume_stops_at_the_limit_and_the_pause_switch_wins(self):
        who, token = self.site.new_user("reader@example.com")
        userdata.save_resume_text(who, "Software engineer with several years of experience building backend services in Python and Java.")
        with mock.patch.object(tailor, "config_value", configured(per_user_per_day={"analyze": 1})), \
                mock.patch.object(web.onboarding, "analyze", return_value={"profile": {}, "suggestions": {}}), mock.patch.object(tailor, "make_client", return_value=object()):
            self.assertEqual(self.post("/api/profile/analyze", {}, token)[0], 200)
            self.assertEqual(self.post("/api/profile/analyze", {}, token)[0], 429)
        with mock.patch.object(tailor, "config_value", configured(paused=True)):
            status, body = self.post("/api/profile/analyze", {}, token)
        self.assertEqual((status, json.loads(body)["code"]), (503, "paused"))

    def test_interview_prep_counts_a_new_answer_but_never_a_saved_one(self):
        fresh = [{"type": "start", "disclaimer": "d"}, {"type": "done", "result": {"cached": False, "content": {}}}]
        saved = [{"type": "start", "disclaimer": "d"}, {"type": "done", "result": {"cached": True, "content": {}}}]
        who, token = self.site.new_user("prepper@example.com")
        conn = self.site.db(who)
        db.add_job(conn, sources.Job("gh:l:3", "greenhouse", "Co3", "Dev", "Austin, TX", "https://x", description="d"), 1.0)
        conn.close()
        with mock.patch.object(tailor, "config_value", configured(per_user_per_day={"tips": 1})):
            with mock.patch.object(web.viewer, "tips_events", side_effect=lambda *a, **k: iter(saved)):
                self.assertEqual(self.post("/api/tips", {"id": "gh:l:3"}, token)[0], 200)            # no saved answer to show yet, but nothing is spent either
            with mock.patch.object(web.viewer, "tips_events", side_effect=lambda *a, **k: iter(fresh)):
                self.assertEqual(self.post("/api/tips", {"id": "gh:l:3", "refresh": True}, token)[0], 200)
                time.sleep(0.4)                                                                      # the count is written as the stream ends
                status, body = self.post("/api/tips", {"id": "gh:l:3", "refresh": True}, token)
        self.assertEqual((status, json.loads(body)["code"]), (429, "limit_reached"))
        c = self.site.db(who)
        self.assertEqual(db.usage_today(c, "tips"), 1)
        c.close()


if __name__ == "__main__":
    unittest.main()
