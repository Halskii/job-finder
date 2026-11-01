"""The live match list and its progress, over HTTP."""
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest import mock

import db
import pipeline
import sources
import web
from tests import webtest


class MatchesWebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.site = webtest.Site(cls.tmp.name)
        cls.base = cls.site.base
        cls.other_id, cls.other_token = cls.site.new_user("other@example.com")
        conn = cls.site.db()
        for i, score in enumerate((92, 75, 40)):
            db.add_job(conn, sources.Job(f"gh:m:{i}", "greenhouse", f"Co{i}", "Software Engineer", "Austin, TX", f"https://x/{i}", description="d"), 1.0)
            db.save_score(conn, f"gh:m:{i}", score, "apply", {"reason": "fits"})
        db.add_job(conn, sources.Job("gh:m:9", "greenhouse", "Unscored", "Software Engineer", "Austin, TX", "https://x/9", description="d"), 1.0)
        conn.close()

    @classmethod
    def tearDownClass(cls):
        cls.site.stop()
        cls.tmp.cleanup()

    def call(self, method, path, body=None, token=None):
        req = urllib.request.Request(self.base + path, method=method, headers={"Content-Type": "application/json",
                                     "Cookie": f"{web.COOKIE}={token or self.site.token}"}, data=None if body is None else json.dumps(body).encode())
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_the_list_holds_the_scored_jobs_at_or_above_the_report_minimum_best_first(self):
        status, data = self.call("GET", "/api/matches")
        self.assertEqual(status, 200)
        self.assertEqual([(j["id"], j["score"]) for j in data["jobs"]], [("gh:m:0", 92), ("gh:m:1", 75)])   # 40 is under 60; the unscored one is not shown yet

    def test_a_discarded_or_applied_job_leaves_the_list(self):
        self.call("POST", "/api/mark", {"id": "gh:m:1", "status": "skipped"})
        try:
            self.assertEqual([j["id"] for j in self.call("GET", "/api/matches")[1]["jobs"]], ["gh:m:0"])
        finally:
            self.call("POST", "/api/mark", {"id": "gh:m:1", "status": "new"})

    def test_another_user_has_no_matches_and_an_idle_status(self):
        self.assertEqual(self.call("GET", "/api/matches", token=self.other_token)[1]["jobs"], [])
        status, st = self.call("GET", "/api/matches/status", token=self.other_token)
        self.assertEqual((status, st["state"], st["scored"]), (200, "idle", 0))

    def test_status_shows_progress_and_what_is_left_of_todays_limit(self):
        conn = self.site.db(self.other_id)
        pipeline._save(conn, state="scoring", scored=12, waiting=30, matched=50)
        conn.close()
        st = self.call("GET", "/api/matches/status", token=self.other_token)[1]
        self.assertEqual((st["state"], st["scored"], st["waiting"], st["matched"]), ("scoring", 12, 30, 50))
        self.assertGreater(st["left_today"], 0)
        self.assertEqual(self.call("GET", "/api/matches/status", token=self.site.token)[1]["state"], "idle")      # not the other user's progress

    def test_people_cannot_start_a_search_themselves_by_default(self):
        with mock.patch.dict(os.environ, {"MANUAL_REFRESH": ""}), mock.patch.object(web, "RUNS", True), mock.patch.object(pipeline.WORKER, "submit") as submit:
            status, body = self.call("POST", "/api/matches/refresh", {}, token=self.other_token)
            st = self.call("GET", "/api/matches/status", token=self.other_token)[1]
        self.assertEqual((status, body["code"]), (403, "refresh_off"))
        self.assertTrue(body["error"].endswith("."))
        submit.assert_not_called()                                                   # nothing was queued, so nothing can be spent
        self.assertIs(st["can_refresh"], False)                                      # and the page is told not to show the link

    def test_with_the_switch_on_asking_for_a_check_queues_a_run_for_that_user_only(self):
        for value in ("on", "1", "true"):
            with mock.patch.dict(os.environ, {"MANUAL_REFRESH": value}), mock.patch.object(web, "RUNS", True), mock.patch.object(pipeline.WORKER, "submit") as submit:
                status, st = self.call("POST", "/api/matches/refresh", {}, token=self.other_token)
            self.assertEqual((status, st["state"], st["can_refresh"]), (200, "queued", True), value)
            submit.assert_called_once_with(self.other_id)
        with mock.patch.dict(os.environ, {"MANUAL_REFRESH": "off"}):
            self.assertEqual(self.call("POST", "/api/matches/refresh", {}, token=self.other_token)[0], 403)

    def test_saving_a_profile_still_starts_the_search_whatever_the_switch_says(self):
        uid, token = self.site.new_user("saver2@example.com")
        with mock.patch.dict(os.environ, {"MANUAL_REFRESH": ""}), mock.patch.object(web, "RUNS", True), mock.patch.object(pipeline.WORKER, "submit") as submit:
            status, _ = self.call("POST", "/api/profile/save", {"settings": {"titles": ["Dev"]}, "profile": {"name": "S"}}, token=token)
        self.assertEqual(status, 200)
        submit.assert_called_once_with(uid)

    def test_saving_a_profile_starts_the_search(self):
        uid, token = self.site.new_user("saver@example.com")
        with mock.patch.object(web, "RUNS", True), mock.patch.object(pipeline.WORKER, "submit") as submit:
            status, _ = self.call("POST", "/api/profile/save", {"settings": {"titles": ["Dev"]}, "profile": {"name": "S"}}, token=token)
        self.assertEqual(status, 200)
        submit.assert_called_once_with(uid)

    def test_signed_out_requests_are_refused_and_the_page_has_the_live_list(self):
        req = urllib.request.Request(self.base + "/api/matches/status")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 401)
        req = urllib.request.Request(self.base + "/", headers={"Cookie": f"{web.COOKIE}={self.site.token}"})
        with urllib.request.urlopen(req) as r:
            page = r.read().decode("utf-8")
        for needle in ("Current matches", "/api/matches/status", "Scoring your matches", "Check for new jobs", "s.can_refresh?[again]:[]"):
            self.assertIn(needle, page)


if __name__ == "__main__":
    unittest.main()


class PageBehaviourTests(unittest.TestCase):
    def test_saving_a_job_keeps_it_on_screen_so_its_filled_bookmark_shows(self):
        import viewer
        # the page moves on to the neighbouring job after discarding or applying, but not after saving
        self.assertIn('const next=status!=="saved"&&at>=0', viewer.PAGE)
        self.assertIn(".save-btn.on svg{fill:currentColor}", viewer.PAGE)                  # a saved job's bookmark is filled
        self.assertIn('className:"btn save-btn"+(saved?" on":"")', viewer.PAGE)
