"""Finding out which postings an employer has taken down, so the list is not full of jobs that are gone."""
import json
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from types import SimpleNamespace
from unittest import mock

import requests

import db
import ingest
import liveness
import pipeline
import reformat
import sources
import userdata
import viewer
import web
from tests import pgtest, webtest
from tests.test_pipeline import Base, add_posting

DAY = 86400


def http_error(status):
    ex = requests.HTTPError(f"{status}")
    ex.response = SimpleNamespace(status_code=status)
    return ex


def job(jid):
    return sources.Job(jid, "greenhouse", "Acme", "Software Engineer", "Austin, TX", "https://x/" + jid)


def wd_row(jid="wd:acme:1", url="https://acme.wd1.myworkdayjobs.com/en-US/Careers/job/Austin/Software-Engineer_R1"):
    return {"id": jid, "url": url, "title": "Software Engineer", "company": "Acme"}


class DecidingTests(unittest.TestCase):
    def test_which_sources_can_be_asked(self):
        for jid in ("gh:acme:1", "lv:acme:1", "ab:acme:1", "wd:acme:1", "rp:acme:1", "wk:acme:1", "bb:acme:1", "rc:acme:1", "pn:acme:1", "gm:acme:1"):
            self.assertTrue(liveness.can_check(jid), jid)
        for jid in ("jo:123", "sr:acme:1", "hm:acme:1", "usajobs:1", "apple:1", "manual:abc", "hn:5", "jb:1"):
            self.assertFalse(liveness.can_check(jid), jid)

    def test_a_job_missing_from_its_board_is_taken_down_and_the_others_are_open(self):
        with mock.patch.object(sources, "fetch_greenhouse", lambda slug: [job("gh:acme:1"), job("gh:acme:3")]):
            out = liveness.check_board("gh", "acme", ["gh:acme:1", "gh:acme:2", "gh:acme:3"])
        self.assertEqual(out, {"gh:acme:1": "open", "gh:acme:2": "closed", "gh:acme:3": "open"})

    def test_a_board_that_is_gone_closes_all_its_jobs_but_a_failed_request_closes_none(self):
        ids = ["gh:acme:1", "gh:acme:2"]
        for status, want in ((404, "closed"), (410, "closed"), (500, "unknown"), (429, "unknown")):
            with mock.patch.object(sources, "fetch_greenhouse", side_effect=http_error(status)):
                self.assertEqual(set(liveness.check_board("gh", "acme", ids).values()), {want}, status)
        with mock.patch.object(sources, "fetch_greenhouse", side_effect=requests.ConnectionError("down")):
            self.assertEqual(set(liveness.check_board("gh", "acme", ids).values()), {"unknown"})

    def test_workday_answers_403_for_a_posting_that_is_gone(self):
        for outcome, want in ((None, "open"), (http_error(403), "closed"), (http_error(404), "closed"), (http_error(500), "unknown"),
                              (requests.Timeout("slow"), "unknown")):
            fake = mock.Mock(side_effect=outcome) if outcome else mock.Mock(return_value="text")
            with mock.patch.dict(reformat.SINGLES, {"wd": fake}):
                self.assertEqual(liveness.check_one(wd_row()), want, outcome)

    def test_other_sources_are_closed_on_404_and_410_only(self):
        row = {"id": "rp:acme:77", "url": "", "title": "t", "company": "c"}
        for status, want in ((404, "closed"), (410, "closed"), (403, "unknown"), (500, "unknown")):
            with mock.patch.dict(reformat.SINGLES, {"rp": mock.Mock(side_effect=http_error(status))}):
                self.assertEqual(liveness.check_one(row), want, status)

    def test_a_workday_job_whose_address_does_not_say_where_to_ask_is_not_guessed_at(self):
        with mock.patch.dict(reformat.SINGLES, {"wd": mock.Mock(return_value="text")}) as fake:
            self.assertEqual(liveness.check_one(wd_row(url="https://elsewhere.example/job/1")), "unknown")
            fake["wd"].assert_not_called()


class LivenessBase(Base):
    def setUp(self):
        super().setUp()
        self.uid = self.user("me@example.com")

    def tearDown(self):
        self.admin.close()      # the shared base leaves it open, and a full run of the tests uses up the database's connections
        super().tearDown()

    def have(self, jid, title="Software Engineer", status="new", score=80, checked=None, company=None):
        add_posting(self.admin, jid, title, company=company or "Co" + jid)
        conn = pgtest.connect(self.schema, user=self.uid)
        try:
            conn.execute("INSERT INTO user_jobs (user_id, job_id, quick_score, first_seen, score, verdict, status) VALUES (?,?,?,?,?,?,?)",
                         (conn.user_id, jid, 0.0, 0.0, score, "apply", status))
            if checked is not None:
                conn.execute("UPDATE postings SET checked_at = ? WHERE id = ?", (checked, jid))
            conn.commit()
        finally:
            conn.close()
        return jid

    def conn(self):
        return pgtest.connect(self.schema, user=self.uid)

    def state(self, jid):
        conn = self.conn()
        try:
            r = conn.execute("SELECT closed_at, checked_at FROM jobs WHERE id = ?", (jid,)).fetchone()
            return r["closed_at"] is not None, r["checked_at"] is not None
        finally:
            conn.close()


class StoringTests(LivenessBase):
    def test_closed_open_and_unknown_answers_are_remembered_on_the_posting(self):
        a, b, c = self.have("gh:acme:1"), self.have("gh:acme:2"), self.have("gh:acme:3")
        conn = self.conn()
        liveness.record(conn, {a: "closed", b: "open", c: "unknown"})
        conn.close()
        self.assertEqual((self.state(a), self.state(b), self.state(c)), ((True, True), (False, True), (False, False)))

    def test_a_job_found_open_again_is_no_longer_closed_and_closing_keeps_the_first_date(self):
        a = self.have("gh:acme:1")
        conn = self.conn()
        liveness.record(conn, {a: "closed"}, now=1000.0)
        liveness.record(conn, {a: "closed"}, now=2000.0)
        self.assertEqual(conn.execute("SELECT closed_at FROM jobs WHERE id = ?", (a,)).fetchone()["closed_at"], 1000.0)
        liveness.record(conn, {a: "open"}, now=3000.0)
        conn.close()
        self.assertEqual(self.state(a), (False, True))

    def test_a_posting_a_fetch_lists_again_is_open_again(self):
        a = self.have("gh:acme:1")
        conn = self.conn()
        liveness.record(conn, {a: "closed"})
        db.add_job(conn, sources.Job(a, "greenhouse", "Co" + a, "Software Engineer", "Austin, TX", "https://x/" + a, description="d"), 0.0)
        conn.close()
        self.assertFalse(self.state(a)[0])

    def test_a_sweep_asks_each_board_once_and_each_workday_job_on_its_own(self):
        rows = [wd_row("wd:acme:1"), wd_row("wd:acme:2"), {"id": "gh:acme:1", "url": "", "title": "t", "company": "c"},
                {"id": "gh:acme:2", "url": "", "title": "t", "company": "c"}, {"id": "jo:9", "url": "", "title": "t", "company": "c"}]
        for r in rows:
            self.have(r["id"])
        asked = []
        fake_gh = lambda slug: asked.append(slug) or [job("gh:acme:1")]
        fake_wd = lambda row: None if row["id"] == "wd:acme:1" else (_ for _ in ()).throw(http_error(403))
        conn = self.conn()
        with mock.patch.object(sources, "fetch_greenhouse", fake_gh), mock.patch.dict(reformat.SINGLES, {"wd": fake_wd}):
            counts = liveness.sweep(conn, rows, delay=0)
        conn.close()
        self.assertEqual(asked, ["acme"])                              # one request for the board, however many of its jobs there are
        self.assertEqual(counts, {"open": 2, "closed": 2, "unknown": 0})   # jo:9 cannot be asked, so it is not counted
        self.assertEqual([self.state(i)[0] for i in ("wd:acme:1", "wd:acme:2", "gh:acme:1", "gh:acme:2", "jo:9")], [False, True, False, True, False])
        self.assertEqual(self.state("jo:9"), (False, False))

    def test_a_workday_site_that_refuses_everything_is_not_taken_for_closed(self):
        rows = [wd_row(f"wd:blocked:{i}") for i in range(liveness.BLOCKED_AFTER)] + [wd_row(f"wd:fine:{i}") for i in range(2)]
        for r in rows:
            self.have(r["id"])
        gone = lambda row: (_ for _ in ()).throw(http_error(403))
        conn = self.conn()
        with mock.patch.dict(reformat.SINGLES, {"wd": gone}):
            counts = liveness.sweep(conn, rows, delay=0)
        conn.close()
        self.assertEqual(counts, {"open": 0, "closed": 2, "unknown": liveness.BLOCKED_AFTER})   # the two on the site with only two jobs are believed
        self.assertFalse(any(self.state(f"wd:blocked:{i}")[0] for i in range(liveness.BLOCKED_AFTER)))

    def test_a_stopped_sweep_records_what_it_found(self):
        rows = [{"id": f"gh:b{i}:1", "url": "", "title": "t", "company": "c"} for i in range(3)]
        for r in rows:
            self.have(r["id"])
        stop = mock.Mock()
        stop.is_set.side_effect = [False, True]
        conn = self.conn()
        with mock.patch.object(sources, "fetch_greenhouse", lambda slug: []):
            counts = liveness.sweep(conn, rows, delay=0, stop=stop)
        conn.close()
        self.assertEqual(counts["closed"], 1)

    def test_which_jobs_are_due(self):
        now = time.time()
        self.have("gh:a:1", score=80)                                  # a match, never asked: due
        self.have("gh:a:2", score=40)                                  # not good enough to be a match
        self.have("gh:a:3", score=40, status="saved")                  # saved: due whatever its score
        self.have("gh:a:4", score=90, checked=now - DAY * 2)           # asked, but long ago: due
        self.have("gh:a:5", score=90, checked=now - 3600)              # asked lately
        self.have("gh:a:6", score=90, status="applied")                # applied: not looking at it
        self.have("jo:7", score=90)                                    # cannot be asked
        conn = self.conn()
        ids = [r["id"] for r in liveness.due_rows(conn, 60, now=now)]
        conn.close()
        self.assertEqual(sorted(ids), ["gh:a:1", "gh:a:3", "gh:a:4"])

    def test_one_job_is_asked_about_unless_it_was_asked_lately(self):
        now = time.time()
        a, b, c = self.have("gh:a:1"), self.have("gh:b:1", checked=now - 3600), self.have("jo:1")
        conn = self.conn()
        with mock.patch.object(sources, "fetch_greenhouse", lambda slug: []) as fetch:
            self.assertEqual(liveness.check_job(conn, a)["state"], "closed")
        with mock.patch.object(sources, "fetch_greenhouse", side_effect=AssertionError("asked again")):
            self.assertEqual(liveness.check_job(conn, b)["state"], "open")      # asked an hour ago and not found gone
        out = liveness.check_job(conn, c)
        conn.close()
        self.assertEqual((out["state"], out["supported"]), ("unknown", False))

    def test_a_job_the_person_does_not_have_is_not_found(self):
        conn = self.conn()
        with self.assertRaises(LookupError):
            liveness.check_job(conn, "gh:nobody:1")
        conn.close()

    def test_the_summary_counts_what_the_page_says(self):
        now = time.time()
        a = self.have("gh:a:1")
        self.have("gh:a:2")
        conn = self.conn()
        liveness.record(conn, {a: "closed"}, now=now)
        out = liveness.summary(conn, 60, now=now)
        conn.close()
        self.assertEqual((out["closed"], out["due"], out["last"]), (1, 1, now))


class EffectTests(LivenessBase):
    def test_a_taken_down_job_is_not_a_match_but_is_still_in_the_saved_list(self):
        a, b = self.have("gh:a:1"), self.have("gh:a:2", status="saved")
        conn = self.conn()
        liveness.record(conn, {a: "closed", b: "closed"})
        self.assertEqual([r["id"] for r in db.top(conn, 10, 60)], [])
        self.assertEqual([r["id"] for r in db.saved(conn)], [b])
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (b,)).fetchone()
        conn.close()
        shown = viewer.job_dict(row)
        self.assertEqual((shown["closed"], shown["can_check"]), (True, True))

    def test_the_page_is_told_when_a_job_is_open_and_when_its_source_cannot_be_asked(self):
        self.have("gh:a:1")
        self.have("jo:2")
        conn = self.conn()
        rows = {r["id"]: viewer.job_dict(r) for r in conn.execute("SELECT * FROM jobs").fetchall()}
        conn.close()
        self.assertEqual((rows["gh:a:1"]["closed"], rows["gh:a:1"]["can_check"]), (False, True))
        self.assertEqual((rows["jo:2"]["closed"], rows["jo:2"]["can_check"]), (False, False))

    def test_a_taken_down_job_is_not_scored(self):
        add_posting(self.admin, "p1", "Software Engineer", company="A")
        add_posting(self.admin, "p2", "Software Engineer", company="B")
        client, calls = self.client()
        conn = self.conn()
        pipeline.match(conn, pipeline.Rules(userdata.load_settings(self.uid)), pipeline.userdata.load_profile(self.uid))
        liveness.record(conn, {"p1": "closed"})
        conn.close()
        pipeline.run_user(self.uid, client)
        self.assertEqual(len(calls), 1)                                # only the open one costs a paid call
        conn = self.conn()
        scores = {r["id"]: r["score"] for r in conn.execute("SELECT id, score FROM jobs").fetchall()}
        conn.close()
        self.assertIsNone(scores["p1"])
        self.assertIsNotNone(scores["p2"])


class FetchTests(unittest.TestCase):
    def test_a_fetch_checks_the_postings_unless_that_is_switched_off(self):
        with mock.patch.object(liveness, "sweep_everyone", return_value={"open": 5, "closed": 2, "unknown": 1}) as sweep:
            self.assertEqual(ingest.check_open({"scoring": {"report_min_score": 55}, "request_delay": 0.1}), 2)
            sweep.assert_called_once_with(55, delay=0.1)
        with mock.patch.object(liveness, "sweep_everyone") as sweep:
            for off in (False, "false", "no", "off", 0):
                self.assertEqual(ingest.check_open({"check_open": off}), 0)
            sweep.assert_not_called()

    def test_a_failed_check_never_fails_the_fetch(self):
        with mock.patch.object(liveness, "sweep_everyone", side_effect=RuntimeError("boom")):
            self.assertEqual(ingest.check_open({}), 0)


class EndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.site = webtest.Site(cls.tmp.name)
        cls.base = cls.site.base

    @classmethod
    def tearDownClass(cls):
        cls.site.stop()
        cls.tmp.cleanup()

    def setUp(self):
        web.CHECKS.clear()

    def call(self, method, path, body=None, token=True):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Cookie"] = f"{web.COOKIE}={self.site.token}"
        req = urllib.request.Request(self.base + path, method=method, data=None if body is None else json.dumps(body).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def add(self, jid, score=80):
        conn = self.site.db()
        add_posting(conn, jid, "Software Engineer", company="Co" + jid)
        conn.execute("INSERT INTO user_jobs (user_id, job_id, quick_score, first_seen, score, verdict, status) VALUES (?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                     (conn.user_id, jid, 0.0, 0.0, score, "apply", "new"))
        conn.commit()
        conn.close()

    def test_one_job_is_asked_about_and_the_answer_comes_back(self):
        self.add("gh:ep:1")
        with mock.patch.object(sources, "fetch_greenhouse", lambda slug: []):
            status, out = self.call("POST", "/api/jobs/check", {"id": "gh:ep:1"})
        self.assertEqual((status, out["state"], out["supported"]), (200, "closed", True))
        status, job = self.call("GET", "/api/job/gh:ep:1")
        self.assertEqual((job["closed"], job["can_check"]), (True, True))
        self.assertIsNotNone(job["checked"])

    def test_bad_input_and_unknown_jobs_and_no_sign_in_are_refused(self):
        self.assertEqual(self.call("POST", "/api/jobs/check", {"id": 5})[0], 400)
        self.assertEqual(self.call("POST", "/api/jobs/check", {})[0], 400)
        self.assertEqual(self.call("POST", "/api/jobs/check", {"id": "gh:nobody:9"})[0], 404)
        self.assertEqual(self.call("POST", "/api/jobs/check", {"id": "gh:ep:1"}, token=False)[0], 401)
        self.assertEqual(self.call("GET", "/api/jobs/check-status", token=False)[0], 401)
        self.assertEqual(self.call("POST", "/api/jobs/check-all", {}, token=False)[0], 401)

    def test_checking_all_runs_in_the_background_and_says_what_it_found(self):
        self.add("gh:all:1")
        self.add("gh:all:2")
        fetch = lambda slug: (time.sleep(0.01), [job("gh:all:1")])[1]
        with mock.patch.object(web, "RUNS", True), mock.patch.object(sources, "fetch_greenhouse", fetch), mock.patch.object(liveness, "DELAY", 0):
            status, out = self.call("POST", "/api/jobs/check-all", {})
            self.assertEqual(status, 200)
            for _ in range(100):
                out = self.call("GET", "/api/jobs/check-status")[1]
                if not out["running"]:
                    break
                time.sleep(0.05)
        self.assertFalse(out["running"])
        self.assertEqual((out["result"]["open"], out["result"]["closed"]), (1, 1))
        self.assertGreaterEqual(out["closed"], 1)
        self.assertEqual(self.call("GET", "/api/job/gh:all:2")[1]["closed"], True)

    def test_a_second_check_while_one_is_running_is_refused(self):
        with web.CHECK_LOCK:
            web.CHECKS[self.site.uid] = {"running": True, "done": 1, "total": 5, "result": None}
        status, out = self.call("POST", "/api/jobs/check-all", {})
        self.assertEqual(status, 409)

    def test_the_page_has_the_banner_and_the_button(self):
        for marker in ('id:"closed-note"', "function paintClosed", 'id:"check-btn"', "/api/jobs/check", "/api/jobs/check-all", "taken down"):
            self.assertIn(marker, viewer.PAGE)


if __name__ == "__main__":
    unittest.main()
