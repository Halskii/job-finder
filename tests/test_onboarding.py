"""Setting up an account: the resume, the suggested job titles, and the profile page's endpoints."""
import io
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import onboarding
import tailor
import userdata
import web
from errors import BadModelAnswer, UserError
from tests import webtest

RESUME = ("James Example\nSoftware Engineer with 3 years at Example Corp building backend services in Python and Java, "
          "deploying with Docker and Kubernetes on AWS, and maintaining CI/CD pipelines.\nB.S. Computer Science.")

PROFILE_ANSWER = {"headline": "Software Engineer, 3 years", "years_experience": 3, "education": "B.S. Computer Science",
                  "certifications": [], "strong_skills": ["Python", "Java", "Docker"], "familiar_skills": ["Kubernetes"],
                  "achievements": ["Cut deploy time by 40%."]}
SUGGEST_ANSWER = {"name": "James Example", "level": "mid", "titles": ["Backend Software Engineer", "Platform Engineer", "backend software engineer"],
                  "exclude_words": ["Senior", "staff", "bad;word", "intern"]}


def pdf_with_text(text: str) -> bytes:
    """A one-page PDF whose page says `text`."""
    content = f"BT /F1 10 Tf 20 700 Td ({text}) Tj ET"
    objs = ["<</Type/Catalog/Pages 2 0 R>>", "<</Type/Pages/Kids[3 0 R]/Count 1>>",
            "<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>",
            f"<</Length {len(content)}>>\nstream\n{content}\nendstream", "<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>"]
    out, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    return out + f"trailer\n<</Size {len(objs) + 1}/Root 1 0 R>>\nstartxref\n{xref}\n%%EOF\n".encode()


def blank_pdf(pages=1, password=None) -> bytes:
    from pypdf import PdfWriter
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(612, 792)
    if password:
        w.encrypt(password)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def fake_client(profile=PROFILE_ANSWER, suggest=SUGGEST_ANSWER):
    calls = []

    def create(**kw):
        calls.append(kw)
        answer = suggest if kw["system"].startswith("You help a job seeker") else profile
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(answer))], usage=None, stop_reason="end_turn")

    return SimpleNamespace(messages=SimpleNamespace(create=create)), calls


class CleaningTests(unittest.TestCase):
    def test_lists_lose_blanks_repeats_control_characters_and_excess(self):
        self.assertEqual(onboarding.clean_list(["  Go ", "go", "", "Py\x00thon‮", None, "x" * 500], 10, 5), ["Go", "Pytho", "xxxxx"])   # cut to 5 characters; None is nothing
        self.assertEqual(onboarding.clean_list("a, b\nc", 10, 20), ["a", "b", "c"])
        self.assertEqual(onboarding.clean_list(list("abcdef"), 3, 5), ["a", "b", "c"])
        for bad in (None, 7, {"a": 1}):
            self.assertEqual(onboarding.clean_list(bad, 5, 5), [])

    def test_settings_keep_only_known_keys_inside_their_limits(self):
        s = onboarding.clean_settings({"titles": ["Dev"] * 3 + [f"T{i}" for i in range(100)], "min_pay": "90000", "max_age_days": 99999,
                                       "us_only": False, "exclude_locations": ["FL"], "evil": "x", "exclude_title_words": ["senior"]})
        self.assertEqual(set(s), {"titles", "exclude_title_words", "min_pay", "max_age_days", "us_only", "exclude_locations"})
        self.assertEqual((len(s["titles"]), s["titles"][0], s["min_pay"], s["max_age_days"], s["us_only"]), (40, "Dev", 90000, 365, False))
        d = onboarding.clean_settings("not a dict")
        self.assertEqual((d["titles"], d["min_pay"], d["max_age_days"], d["us_only"]), ([], 0, 30, True))
        self.assertEqual(onboarding.clean_settings({"min_pay": -5, "max_age_days": "x"})["min_pay"], 0)
        self.assertTrue(onboarding.clean_settings({"us_only": "false"})["us_only"])      # only a real false turns it off

    def test_a_skill_the_person_says_they_lack_is_never_kept(self):
        p, notes = onboarding.clean_profile({"name": " Jo  Ann ", "strong_skills": ["Python", "Go"], "familiar_skills": ["python", "Kafka"],
                                             "do_not_assume": ["go", "Kafka"], "years_experience": "3.7", "headline": "h" * 999})
        self.assertEqual((p["name"], p["strong_skills"], p["familiar_skills"], p["do_not_assume"]), ("Jo Ann", ["Python"], [], ["go", "Kafka"]))
        self.assertEqual((p["years_experience"], len(p["headline"])), (3, 300))
        self.assertEqual(len(notes), 2)
        self.assertTrue(all(n.endswith(".") for n in notes))
        self.assertEqual(set(p), {"name", "headline", "years_experience", "education", "location", "work_authorization", "security_clearance",
                                  "relocation", "certifications", "strong_skills", "familiar_skills", "do_not_assume", "achievements",
                                  "best_fit_notes"})


class AnalyzeTests(unittest.TestCase):
    def test_titles_and_facts_come_back_cleaned_and_both_calls_are_made(self):
        client, calls = fake_client()
        out = onboarding.analyze(client, RESUME, "m1", "m2")
        self.assertEqual(sorted(c["model"] for c in calls), ["m1", "m2"])
        self.assertEqual(out["suggestions"]["titles"], ["Backend Software Engineer", "Platform Engineer"])   # the repeat is gone
        self.assertEqual(out["suggestions"]["exclude_words"], ["senior", "staff", "intern"])                  # lowercased, the odd one dropped
        self.assertEqual((out["suggestions"]["name"], out["suggestions"]["level"]), ("James Example", "mid"))
        self.assertEqual(out["profile"]["strong_skills"], ["Python", "Java", "Docker"])
        for c in calls:
            self.assertIn("James Example", c["messages"][0]["content"])

    def test_an_answer_with_no_titles_or_in_the_wrong_shape_is_a_friendly_error(self):
        client, _ = fake_client(suggest={"titles": [], "exclude_words": []})
        with self.assertRaises(BadModelAnswer) as ctx:
            onboarding.analyze(client, RESUME, "m1", "m2")
        self.assertEqual(ctx.exception.code, "no_titles")
        client, _ = fake_client(profile={"headline": "x"})
        with self.assertRaises(BadModelAnswer):
            onboarding.analyze(client, RESUME, "m1", "m2")


class ResumeFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = mock.patch.object(userdata, "BASE", Path(self.tmp.name))
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_text_is_cleaned_saved_and_replaces_a_pdf(self):
        userdata.save_resume_pdf(1, pdf_with_text(RESUME.replace("\n", " ")))
        self.assertEqual(userdata.resume_kind(1), "pdf")
        self.assertEqual(userdata.save_resume_text(1, RESUME + "\x00‮"), len(RESUME))
        self.assertEqual(userdata.resume_kind(1), "text")
        self.assertFalse((userdata.home(1) / "resume.pdf").exists())      # only one resume is kept, so the right one is read
        self.assertEqual((userdata.home(1) / "resume.txt").read_text(encoding="utf-8"), RESUME)

    def test_text_that_is_too_short_is_refused(self):
        with self.assertRaises(UserError) as ctx:
            userdata.save_resume_text(1, "I like computers")
        self.assertEqual(ctx.exception.code, "resume_too_short")

    def test_a_pdf_with_text_is_kept_and_read(self):
        info = userdata.save_resume_pdf(1, pdf_with_text(RESUME.replace("\n", " ")))
        self.assertEqual(info["pages"], 1)
        self.assertGreater(info["chars"], 80)
        import main
        self.assertIn("backend services", main.read_resume_file(userdata.home(1)))

    def test_things_that_are_not_a_readable_resume_pdf_are_refused_with_a_reason(self):
        cases = [(b"hello, I am not a pdf", "not_pdf"), (b"%PDF-1.4 garbage that cannot be parsed", "pdf_unreadable"),
                 (blank_pdf(1), "pdf_no_text"), (blank_pdf(1, "secret"), "pdf_locked"), (blank_pdf(13), "pdf_too_long"),
                 (b"%PDF-" + b"0" * (userdata.MAX_RESUME_BYTES + 1), "too_large")]
        for data, code in cases:
            with self.assertRaises(UserError) as ctx:
                userdata.save_resume_pdf(1, data)
            self.assertEqual(ctx.exception.code, code, code)
        self.assertIsNone(userdata.resume_kind(1))                          # nothing was kept

    def test_settings_and_profile_round_trip_and_defaults_fill_in(self):
        self.assertFalse(userdata.is_set_up(1))
        self.assertEqual(userdata.load_settings(1)["max_age_days"], 30)
        userdata.save_settings(1, {"titles": ["Dev"]})
        self.assertTrue(userdata.is_set_up(1))
        self.assertEqual((userdata.load_settings(1)["titles"], userdata.load_settings(1)["us_only"]), (["Dev"], True))
        self.assertIsNone(userdata.load_profile(1))
        userdata.save_profile(1, {"name": "Zoë"})
        self.assertEqual(userdata.load_profile(1), {"name": "Zoë"})
        self.assertFalse(userdata.is_set_up(2))                              # another user's folder is separate


class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.site = webtest.Site(cls.tmp.name)
        cls.base = cls.site.base
        cls.new_id, cls.new_token = cls.site.new_user("fresh@example.com")      # a brand new account: not set up

    @classmethod
    def tearDownClass(cls):
        cls.site.stop()
        cls.tmp.cleanup()

    def call(self, method, path, body=None, token=None, ctype="application/json", raw=None, headers=None):
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        h = {"Content-Type": ctype, "Cookie": f"{web.COOKIE}={token or self.new_token}", **(headers or {})}
        req = urllib.request.Request(self.base + path, method=method, headers=h, data=data)

        class Stop(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        try:
            r = urllib.request.build_opener(Stop).open(req)
            status, head, text = r.status, r.headers, r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            status, head, text = e.code, e.headers, e.read().decode("utf-8")
        try:
            return status, head, json.loads(text)
        except ValueError:
            return status, head, text

    def test_a_new_account_is_sent_to_the_profile_page_and_an_set_up_one_is_not(self):
        status, head, _ = self.call("GET", "/")
        self.assertEqual((status, head["Location"]), (303, "/profile"))
        self.assertEqual(self.call("GET", "/", token=self.site.token)[0], 200)
        status, _, page = self.call("GET", "/profile")
        self.assertEqual(status, 200)
        self.assertIn("Read my resume and suggest job titles", page)
        self.assertEqual(self.call("GET", "/profile", token="nope")[0], 303)

    def test_the_profile_endpoint_starts_empty_for_a_new_account(self):
        d = self.call("GET", "/api/profile")[2]
        self.assertEqual((d["set_up"], d["resume"]["kind"], d["profile"], d["settings"]["titles"]), (False, None, None, []))
        self.assertEqual(d["account"]["email"], "fresh@example.com")

    def test_uploading_reading_editing_and_saving(self):
        # a PDF goes up (as a PDF, not as JSON)
        status, _, info = self.call("PUT", "/api/resume", ctype="application/pdf", raw=pdf_with_text(RESUME.replace("\n", " ")))
        self.assertEqual((status, info["pages"]), (200, 1))
        self.assertEqual(self.call("GET", "/api/profile")[2]["resume"]["kind"], "pdf")
        # Claude reads it (the paid call is faked)
        client, calls = fake_client()
        with mock.patch.object(tailor, "make_client", return_value=client):
            status, _, out = self.call("POST", "/api/profile/analyze")
        self.assertEqual(status, 200)
        self.assertEqual(out["suggestions"]["titles"][0], "Backend Software Engineer")
        self.assertEqual(len(calls), 2)
        self.assertFalse(userdata.is_set_up(self.new_id))                      # reading saves nothing
        # the person edits and saves
        body = {"settings": {"titles": ["Backend Software Engineer", "Platform Engineer"], "exclude_title_words": ["senior"], "min_pay": 80000,
                             "max_age_days": 14, "us_only": True, "exclude_locations": ["FL"]},
                "profile": {**out["profile"], "name": "Fresh Person", "location": "Austin, TX", "do_not_assume": ["Python"]}}
        status, _, saved = self.call("POST", "/api/profile/save", body)
        self.assertEqual(status, 200)
        self.assertEqual(saved["profile"]["strong_skills"], ["Java", "Docker"])   # Python was listed as something they lack
        self.assertEqual(len(saved["notes"]), 1)
        self.assertTrue(userdata.is_set_up(self.new_id))
        self.assertEqual(userdata.load_settings(self.new_id)["min_pay"], 80000)
        self.assertEqual(userdata.load_profile(self.new_id)["name"], "Fresh Person")
        self.assertEqual(self.call("GET", "/")[0], 200)                         # now they get the app
        # other accounts do not see any of it
        other = self.call("GET", "/api/profile", token=self.site.token)[2]
        self.assertNotEqual(other["profile"], userdata.load_profile(self.new_id))

    def test_saving_needs_a_title_and_unknown_fields_are_dropped(self):
        who, token = self.site.new_user("strict@example.com")
        status, _, err = self.call("POST", "/api/profile/save", {"settings": {"titles": []}, "profile": {}}, token=token)
        self.assertEqual((status, err["code"]), (400, "invalid"))
        status, _, saved = self.call("POST", "/api/profile/save", {"settings": {"titles": ["Dev"], "x": 1}, "profile": {"name": "N", "admin": True}}, token=token)
        self.assertEqual(status, 200)
        self.assertNotIn("admin", userdata.load_profile(who))
        self.assertNotIn("x", userdata.load_settings(who))

    def test_text_resumes_and_the_errors_around_them(self):
        who, token = self.site.new_user("texty@example.com")
        self.assertEqual(self.call("POST", "/api/profile/analyze", token=token)[2]["code"], "no_resume")
        self.assertEqual(self.call("POST", "/api/resume-text", {"text": "hi"}, token=token)[2]["code"], "resume_too_short")
        status, _, d = self.call("POST", "/api/resume-text", {"text": RESUME}, token=token)
        self.assertEqual((status, d["chars"]), (200, len(RESUME)))
        self.assertEqual(self.call("PUT", "/api/resume", token=token, ctype="application/pdf", raw=blank_pdf(1))[2]["code"], "pdf_no_text")
        self.assertEqual(userdata.resume_kind(who), "text")                    # the failed upload did not remove the text resume

    def test_the_upload_is_guarded_like_every_other_write(self):
        pdf = pdf_with_text(RESUME.replace("\n", " "))
        self.assertEqual(self.call("PUT", "/api/resume", ctype="application/json", raw=pdf)[0], 400)        # a form on another site could send this
        status, _, d = self.call("PUT", "/api/resume", ctype="application/pdf", raw=pdf, headers={"Origin": "https://evil.example"})
        self.assertEqual((status, d["code"]), (403, "bad_origin"))
        self.assertEqual(self.call("PUT", "/api/resume", ctype="application/pdf", raw=pdf, token="nope")[0], 401)
        with mock.patch.object(web, "MAX_UPLOAD", 1000):                                                     # a smaller limit, so the test sends little
            status, _, d = self.call("PUT", "/api/resume", ctype="application/pdf", raw=b"%PDF-" + b"0" * 2000)
        self.assertEqual((status, d["code"]), (413, "too_large"))

    def test_skipping_marks_the_account_as_set_up_without_a_profile(self):
        who, token = self.site.new_user("skipper@example.com")
        self.assertEqual(self.call("GET", "/", token=token)[0], 303)
        self.assertEqual(self.call("POST", "/api/profile/skip", {}, token=token)[0], 200)
        self.assertEqual(self.call("GET", "/", token=token)[0], 200)
        self.assertIsNone(userdata.load_profile(who))


if __name__ == "__main__":
    unittest.main()


class ThinkingReplyTests(unittest.TestCase):
    """A model that thinks first sends a block with no text before the answer; the answer is in the text blocks after it."""

    def reply(self, answer):
        thinking = SimpleNamespace(type="thinking", thinking="let me work this out")   # no .text, like the real block
        return SimpleNamespace(content=[thinking, SimpleNamespace(type="text", text=json.dumps(answer))], usage=None, stop_reason="end_turn")

    def test_the_resume_facts_are_read_when_the_reply_starts_with_thinking(self):
        import profile_gen
        client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: self.reply(PROFILE_ANSWER)))
        self.assertEqual(profile_gen.generate(client, "m", RESUME)["strong_skills"], ["Python", "Java", "Docker"])

    def test_the_whole_analysis_works_when_both_replies_start_with_thinking(self):
        client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: self.reply(
            SUGGEST_ANSWER if kw["system"].startswith("You help a job seeker") else PROFILE_ANSWER)))
        out = onboarding.analyze(client, RESUME, "m1", "m2")
        self.assertEqual(out["suggestions"]["titles"][0], "Backend Software Engineer")
        self.assertEqual(out["profile"]["headline"], "Software Engineer, 3 years")

    def test_a_job_score_is_read_when_the_reply_starts_with_thinking(self):
        import scorer
        client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: self.reply({"fit_score": 77, "verdict": "apply"})))
        job = {"company": "C", "title": "T", "location": "L", "posted": None, "description": "d", "pay_min": None, "pay_max": None, "pay_unit": "year"}
        self.assertEqual(scorer.score_job(client, "m", {}, job, 0)["fit_score"], 77)
