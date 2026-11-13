"""A job added by hand: cleaning the pasted text, storing it as the person's own saved job, keeping it from everyone else, scoring it,
and the endpoint."""
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from datetime import date
from types import SimpleNamespace
from unittest import mock

import db
import limits
import manualjob
import pipeline
import sources
import tailor
import web
from errors import UserError
from tests import webtest
from tests.test_limits import configured
from tests.test_pipeline import PROFILE, SETTINGS, Base, add_posting, answer

POSTING = ("About the role\nBuild and run Python services for our platform team. You will work with Docker and Kubernetes.\n\n"
           "The pay range for this role is $140,000 - $170,000 a year.")


def client_saying(payload, stop_reason="end_turn"):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    msg = SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason=stop_reason,
                          usage=SimpleNamespace(input_tokens=300, output_tokens=40))
    calls = []

    def create(**kw):
        calls.append(kw)
        return msg
    return SimpleNamespace(messages=SimpleNamespace(create=create)), calls


class CleaningTests(unittest.TestCase):
    def test_text_loses_invisible_and_control_characters_but_keeps_its_lines(self):
        dirty = "Line one\x00‮ hidden​\r\n\r\n\r\n\r\nLine two\x07\ttab"
        self.assertEqual(manualjob.clean_text(dirty), "Line one hidden\n\nLine two\ttab")

    def test_text_is_cut_to_the_limit_and_none_is_empty(self):
        self.assertEqual(len(manualjob.clean_text("a" * (manualjob.MAX_TEXT + 500))), manualjob.MAX_TEXT)
        self.assertEqual(manualjob.clean_text(None), "")

    def test_a_short_field_is_one_line(self):
        self.assertEqual(manualjob.clean_field("  Senior\n  Engineer \t II "), "Senior Engineer II")
        self.assertEqual(len(manualjob.clean_field("x" * 999)), manualjob.MAX_FIELD)

    def test_only_ordinary_web_addresses_are_kept(self):
        self.assertEqual(manualjob.clean_url(" https://example.com/jobs/1 "), "https://example.com/jobs/1")
        for bad in ("javascript:alert(1)", "file:///etc/passwd", "ftp://x/y", "example.com", "https://", "", None):
            self.assertEqual(manualjob.clean_url(bad), "", bad)

    def test_the_same_posting_gets_the_same_id_however_it_is_pasted(self):
        a = manualjob.job_id("https://Example.com/jobs/1/", "x")
        self.assertEqual(a, manualjob.job_id("https://example.com/jobs/1#apply", "different text"))
        self.assertTrue(a.startswith("manual:"))
        self.assertEqual(manualjob.job_id("", "Build  things\nin Python"), manualjob.job_id("", "build things in python"))
        self.assertNotEqual(manualjob.job_id("", "one posting"), manualjob.job_id("", "another posting"))


class BasicsTests(unittest.TestCase):
    def test_the_company_title_and_place_are_read_from_the_text(self):
        client, calls = client_saying({"company": " Initech ", "title": "Platform\nEngineer", "location": "Austin, TX", "extra": "ignored"})
        found, tok_in, tok_out = manualjob.basics(POSTING, client=client, model="m")
        self.assertEqual(found, {"company": "Initech", "title": "Platform Engineer", "location": "Austin, TX"})
        self.assertEqual((tok_in, tok_out), (300, 40))
        self.assertIn("untrusted", calls[0]["system"])
        self.assertIn("Build and run Python services", calls[0]["messages"][0]["content"])

    def test_what_the_text_does_not_say_comes_back_empty_and_a_refusal_is_reported(self):
        client, _ = client_saying({"company": "", "title": "Engineer"})
        found, _, _ = manualjob.basics(POSTING, client=client, model="m")
        self.assertEqual(found, {"company": "", "title": "Engineer", "location": ""})
        refused, _ = client_saying("", stop_reason="refusal")
        with self.assertRaises(UserError):
            manualjob.basics(POSTING, client=refused, model="m")

    def test_only_the_top_of_a_long_text_is_sent(self):
        client, calls = client_saying({"company": "A", "title": "B", "location": ""})
        manualjob.basics("T" * 50000, client=client, model="m")
        self.assertLessEqual(len(calls[0]["messages"][0]["content"]), manualjob.EXTRACT_CHARS + 100)


class StoreTests(Base):
    def setUp(self):
        super().setUp()
        self.uid = self.user("me@example.com")
        self.conn = pipeline._connect(self.uid)

    def tearDown(self):
        self.conn.close()
        super().tearDown()

    def store(self, **kw):
        fields = {"text": POSTING, "url": "https://careers.example.com/jobs/77", "company": "Initech", "title": "Platform Engineer",
                  "location": "Austin, TX", "today": date(2026, 11, 11)}
        return manualjob.store(self.conn, PROFILE, **{**fields, **kw})

    def test_a_pasted_job_becomes_a_saved_job_of_this_person_with_its_pay(self):
        result = self.store()
        self.assertEqual((result["duplicate"], result["title"], result["company"]), (False, "Platform Engineer", "Initech"))
        row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (result["id"],)).fetchone()
        self.assertEqual((row["source"], row["status"], row["location"], row["posted"]), ("manual", "saved", "Austin, TX", "2026-11-11"))
        self.assertEqual((row["pay_min"], row["pay_max"]), (140000, 170000))
        self.assertEqual(row["url"], "https://careers.example.com/jobs/77")
        self.assertIn("Docker and Kubernetes", row["description"])
        self.assertIsNone(row["score"])
        self.assertIsNotNone(row["quick_score"])

    def test_a_job_with_no_link_or_pay_is_kept_without_them(self):
        result = self.store(url="javascript:alert(1)", text="Short but real posting text for a backend role, nothing about money at all.")
        row = self.conn.execute("SELECT url, pay_min, pay_max FROM jobs WHERE id = ?", (result["id"],)).fetchone()
        self.assertEqual((row["url"], row["pay_min"], row["pay_max"]), ("", None, None))

    def test_the_same_posting_twice_or_the_same_company_and_title_is_not_added_again(self):
        first = self.store()
        same_post = self.store()
        self.assertEqual((same_post["duplicate"], same_post["id"]), (True, first["id"]))
        same_job = self.store(url="https://other.example.com/x", text=POSTING + " Edited.")
        self.assertEqual((same_job["duplicate"], same_job["id"]), (True, first["id"]))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"], 1)

    def test_a_job_the_person_already_has_from_a_fetch_is_found_by_company_and_title(self):
        add_posting(self.admin, "gh:1", "Platform Engineer", company="Initech")
        self.conn.execute("INSERT INTO user_jobs (user_id, job_id, first_seen) VALUES (?, 'gh:1', 1)", (self.uid,))
        self.conn.commit()
        result = self.store()
        self.assertEqual((result["duplicate"], result["id"]), (True, "gh:1"))

    def test_text_that_is_too_short_and_missing_names_are_errors_that_say_what_to_do(self):
        with self.assertRaises(UserError) as ctx:
            self.store(text="Too short")
        self.assertEqual(ctx.exception.code, "text_too_short")
        for missing in ({"company": ""}, {"title": ""}, {"company": "  ", "title": ""}):
            with self.assertRaises(UserError) as ctx:
                self.store(**missing)
            self.assertEqual(ctx.exception.code, "needs_fields")

    def test_nobody_else_is_given_the_job_by_the_matcher(self):
        mine = self.store(title="Software Engineer")
        add_posting(self.admin, "gh:ordinary", "Software Engineer", company="Other Co")
        other = self.user("other@example.com")
        conn = pipeline._connect(other)
        try:
            added, _ = pipeline.match(conn, pipeline.Rules(SETTINGS), PROFILE)
            ids = {r["id"] for r in conn.execute("SELECT id FROM jobs").fetchall()}
        finally:
            conn.close()
        self.assertEqual(added, 1)
        self.assertEqual(ids, {"gh:ordinary"})                    # the ordinary posting, and not the one pasted in by someone else
        self.assertEqual(self.conn.execute("SELECT id FROM jobs").fetchone()["id"], mine["id"])


class ScoreOneTests(Base):
    def setUp(self):
        super().setUp()
        self.uid = self.user("me@example.com")
        conn = pipeline._connect(self.uid)
        self.job = manualjob.store(conn, PROFILE, text=POSTING, company="Initech", title="Platform Engineer", location="Austin, TX")["id"]
        conn.close()

    def score(self, client=None):
        return pipeline.score_one(self.uid, self.job, client=client)

    def row(self):
        conn = pipeline._connect(self.uid)
        try:
            return conn.execute("SELECT score, verdict FROM jobs WHERE id = ?", (self.job,)).fetchone(), \
                conn.execute("SELECT calls FROM usage WHERE kind = 'score'").fetchone()
        finally:
            conn.close()

    def test_the_job_is_scored_with_the_same_scorer_and_counted(self):
        client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: answer(77)))
        self.assertEqual(self.score(client), {"scored": True})
        scored, usage = self.row()
        self.assertEqual((scored["score"], scored["verdict"], usage["calls"]), (77, "apply", 1))

    def test_when_it_cannot_be_scored_the_job_is_kept_and_the_reason_is_in_words(self):
        with mock.patch.object(pipeline.limits, "scoring_left", return_value=0):
            out = self.score(SimpleNamespace())
        self.assertFalse(out["scored"])
        self.assertIn("limit", out["reason"])
        with mock.patch.object(tailor, "make_client", side_effect=UserError("Add your Claude key first.", "no_api_key")):
            self.assertEqual(self.score(), {"scored": False, "reason": "Add your Claude key first."})
        with mock.patch.object(pipeline.limits, "paused", return_value=True):
            self.assertEqual(self.score(SimpleNamespace()), {"scored": False, "reason": limits.PAUSED})
        self.assertIsNone(self.row()[0]["score"])

    def test_a_failed_call_is_reported_not_raised(self):
        def boom(**kw):
            raise RuntimeError("the model is down")
        out = self.score(SimpleNamespace(messages=SimpleNamespace(create=boom)))
        self.assertFalse(out["scored"])
        self.assertTrue(out["reason"])
        self.assertNotIn("RuntimeError", out["reason"])
        self.assertIsNone(self.row()[0]["score"])

    def test_a_person_with_no_profile_and_an_unknown_job_are_handled(self):
        bare = self.user("bare@example.com", profile=None)
        conn = pipeline._connect(bare)
        job = manualjob.store(conn, None, text=POSTING, company="Initech", title="Platform Engineer")["id"]
        conn.close()
        self.assertFalse(pipeline.score_one(bare, job, client=SimpleNamespace())["scored"])
        with self.assertRaises(LookupError):
            pipeline.score_one(self.uid, "manual:nothing")


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

    def post(self, body, token=True):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Cookie"] = f"{web.COOKIE}={self.site.token}"
        req = urllib.request.Request(self.base + "/api/jobs/add", method="POST", data=json.dumps(body).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def row(self, job_id):
        conn = self.site.db()
        try:
            return conn.execute("SELECT status, applied_at, title, company, score FROM jobs WHERE id = ?", (job_id,)).fetchone()
        finally:
            conn.close()

    def test_a_pasted_job_with_its_names_is_added_saved_and_scored_without_any_reading_call(self):
        body = {"text": POSTING + " One.", "company": "Initech", "title": "Platform Engineer", "location": "Austin, TX"}
        with mock.patch.object(manualjob, "basics") as basics, mock.patch.object(pipeline, "score_one", return_value={"scored": True}) as scored:
            status, out = self.post(body)
        self.assertEqual((status, out["duplicate"], out["scored"], out["note"]), (200, False, True, None))
        basics.assert_not_called()                                     # the names were typed, so nothing is read by a model
        scored.assert_called_once()
        self.assertEqual(self.row(out["id"])["status"], "saved")

    def test_blank_names_are_read_from_the_text_and_that_call_counts_toward_its_daily_limit(self):
        found = ({"company": "Hooli", "title": "Backend Engineer", "location": "Remote"}, 120, 30)
        conn = self.site.db()
        allowed = db.usage_today(conn, "addjob") + 1                  # one more reading call today, whatever earlier tests used
        conn.close()
        with mock.patch.object(tailor, "config_value", configured(per_user_per_day={"addjob": allowed})), \
                mock.patch.object(manualjob, "basics", return_value=found) as basics, \
                mock.patch.object(pipeline, "score_one", return_value={"scored": False, "reason": "Today's scoring limit is reached."}):
            status, out = self.post({"text": POSTING + " Two."})
            self.assertEqual((status, out["scored"], out["note"]), (200, False, "Today's scoring limit is reached."))
            self.assertEqual((self.row(out["id"])["title"], self.row(out["id"])["company"]), ("Backend Engineer", "Hooli"))
            status, body = self.post({"text": POSTING + " Three."})
            self.assertEqual((status, body["code"]), (429, "limit_reached"))
            self.assertEqual(basics.call_count, 1)
            status, _ = self.post({"text": POSTING + " Four.", "company": "Hooli", "title": "Data Engineer"})   # typed names: no reading call to limit
            self.assertEqual(status, 200)

    def test_pasting_the_same_job_again_opens_the_one_you_have_and_does_not_read_it_again(self):
        body = {"text": POSTING + " Five.", "company": "Pied Piper", "title": "Compression Engineer"}
        with mock.patch.object(pipeline, "score_one", return_value={"scored": True}):
            first = self.post(body)[1]
            again = self.post({"text": POSTING + " Five.", "url": ""})[1]          # no names this time: not read, the posting is already held
        self.assertEqual((again["duplicate"], again["id"]), (True, first["id"]))

    def test_a_job_you_already_applied_to_goes_straight_to_the_tracker(self):
        body = {"text": POSTING + " Six.", "company": "Aviato", "title": "Software Engineer", "applied": True}
        with mock.patch.object(pipeline, "score_one", return_value={"scored": True}):
            status, out = self.post(body)
        row = self.row(out["id"])
        self.assertEqual((status, row["status"], row["applied_at"]), (200, "applied", date.today().isoformat()))

    def test_bad_input_is_refused_with_words_and_nothing_is_added(self):
        status, body = self.post({"text": "too short", "company": "A", "title": "B"})
        self.assertEqual((status, body["code"]), (422, "text_too_short"))
        with mock.patch.object(manualjob, "basics", return_value=({"company": "", "title": "", "location": ""}, 1, 1)):
            status, body = self.post({"text": POSTING + " Seven."})
        self.assertEqual((status, body["code"]), (422, "needs_fields"))
        for odd in ({"text": 5}, {"text": POSTING, "company": ["x"]}, {"text": POSTING, "applied": "yes"}):
            self.assertEqual(self.post(odd)[0], 400, odd)
        self.assertEqual(self.post({"text": POSTING}, token=False)[0], 401)

    def test_the_page_has_the_button_and_the_box(self):
        req = urllib.request.Request(self.base + "/", headers={"Cookie": f"{web.COOKIE}={self.site.token}"})
        with urllib.request.urlopen(req) as r:
            page = r.read().decode("utf-8")
        for marker in ('id="add-btn"', 'id="add-dlg"', 'id="add-text"', "/api/jobs/add", "I already applied"):
            self.assertIn(marker, page)


if __name__ == "__main__":
    unittest.main()
