"""Offline tests for keeping a posting's formatting: the HTML to Markdown conversion, the fetchers, and restoring old postings."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import requests

import db
from tests import pgtest
import reformat
import sources


class ConversionTests(unittest.TestCase):
    def test_headings_paragraphs_bullets_and_bold_survive(self):
        html = ("<h2><strong>About us</strong></h2><p>We are a firm&nbsp;in Bellevue.</p><p><strong>In this role</strong></p>"
                "<ul><li>Design, build &amp; maintain CI/CD</li><li>Write Python<ul><li>nested</li></ul></li></ul><p>Apply now.</p>")
        self.assertEqual(sources.strip_html(html),
                         "## About us\n\nWe are a firm in Bellevue.\n\n**In this role**\n\n- Design, build & maintain CI/CD\n- Write Python\n  - nested\n\nApply now.")

    def test_a_list_item_wrapped_in_divs_and_paragraphs_is_still_one_bullet(self):
        html = ("<ul><li><div><div><p><span>Design CI/CD pipelines.</span></p></div></div></li>"
                "<li><p>First part.</p><p>Second part.</p><ul><li>nested</li></ul></li></ul>")
        self.assertEqual(sources.strip_html(html), "- Design CI/CD pipelines.\n- First part. Second part.\n  - nested")

    def test_a_greenhouse_style_escaped_posting_is_unescaped_first(self):
        self.assertEqual(sources.strip_html("&lt;p&gt;Hello &lt;b&gt;there&lt;/b&gt;&lt;/p&gt;"), "Hello **there**")

    def test_scripts_empty_markup_and_stray_markers_are_dropped(self):
        html = "<p>Hi</p><script>evil()</script><style>p{}</style><h3></h3><ul><li></li></ul><p><strong> </strong>Bye<br>Now</p>"
        out = sources.strip_html(html)
        self.assertNotIn("evil", out)
        self.assertNotIn("**", out)
        self.assertEqual(out, "Hi\n\nBye\nNow")

    def test_a_heading_is_not_also_wrapped_in_bold_markers(self):
        self.assertEqual(sources.strip_html("<h1><b>Title</b></h1>"), "## Title")

    def test_plain_text_keeps_its_own_lines(self):
        self.assertEqual(sources.strip_html("one\ntwo\n\n\nthree"), "one\ntwo\n\nthree")
        self.assertEqual(sources.strip_html(None), "")

    def test_plain_option_removes_all_markup_for_snippets(self):
        self.assertEqual(sources.strip_html("Work with <b>Django</b> and <ul><li>Flask</li></ul>", plain=True), "Work with Django and\nFlask")

    def test_a_pay_range_in_bold_is_still_found(self):
        self.assertEqual(sources.extract_pay(sources.strip_html("<p>Pay: <b>$90,000</b> - <b>$120,000</b></p>")), (90000, 120000))


class FetcherTests(unittest.TestCase):
    def test_greenhouse_description_keeps_its_structure(self):
        board = {"jobs": [{"id": 7, "title": "Dev", "location": {"name": "Austin"}, "absolute_url": "https://x/7",
                           "content": "&lt;h2&gt;Role&lt;/h2&gt;&lt;ul&gt;&lt;li&gt;Build&lt;/li&gt;&lt;/ul&gt;"}]}
        with mock.patch.object(sources, "_get", lambda *a, **k: board):
            job = sources.fetch_greenhouse("acme")[0]
        self.assertEqual(job.description, "## Role\n\n- Build")

    def test_lever_and_ashby_prefer_the_html_over_the_plain_copy(self):
        lever = [{"id": "a", "text": "Dev", "descriptionPlain": "Plain only", "description": "<p>Rich <b>text</b></p>",
                  "lists": [{"text": "Requirements:", "content": "<li>Python</li>"}], "hostedUrl": "https://x"}]
        with mock.patch.object(sources, "_get", lambda *a, **k: lever):
            self.assertEqual(sources.fetch_lever("acme")[0].description, "Rich **text**\n\n### Requirements\n\n- Python")
        ashby = {"jobs": [{"id": "b", "title": "Dev", "descriptionPlain": "Plain only", "descriptionHtml": "<h2>Role</h2><p>Do it</p>", "jobUrl": "https://x"}]}
        with mock.patch.object(sources, "_get", lambda *a, **k: ashby):
            self.assertEqual(sources.fetch_ashby("acme")[0].description, "## Role\n\nDo it")


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "jobs.db")
        self.conn = pgtest.connect()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def add(self, job_id, formatted):
        db.add_job(self.conn, sources.Job(job_id, "greenhouse", "Acme", "Dev", "Austin", "https://x", description="Plain text"), 1.0)
        self.conn.execute("UPDATE jobs SET formatted=? WHERE id=?", (formatted, job_id))
        self.conn.commit()

    def test_new_postings_are_marked_formatted(self):
        db.add_job(self.conn, sources.Job("gh:a:1", "greenhouse", "A", "Dev", "x", "https://x", description="d"), 1.0)
        self.assertEqual(self.conn.execute("SELECT formatted FROM jobs").fetchone()[0], 1)

    def test_an_old_posting_gets_its_formatting_back_from_its_board(self):
        self.add("gh:acme:9", 0)
        other = sources.Job("gh:acme:1", "greenhouse", "Acme", "Other", "x", "https://x", description="## Other")
        mine = sources.Job("gh:acme:9", "greenhouse", "Acme", "Dev", "Austin", "https://x", description="## Role\n\n- Build")
        with mock.patch.object(sources, "fetch_greenhouse", return_value=[other, mine]) as f:
            self.assertEqual(reformat.reformat_job(self.conn, "gh:acme:9"), {"updated": True, "description": "## Role\n\n- Build"})
            f.assert_called_once_with("acme")
            self.assertEqual(reformat.reformat_job(self.conn, "gh:acme:9")["updated"], False)   # now formatted: no second request
            f.assert_called_once()
        self.assertEqual(self.conn.execute("SELECT description, formatted FROM jobs WHERE id='gh:acme:9'").fetchone()[:], ("## Role\n\n- Build", 1))

    def test_nothing_changes_when_the_board_fails_the_posting_is_gone_or_the_source_is_unsupported(self):
        self.add("gh:acme:9", 0)
        with mock.patch.object(sources, "fetch_greenhouse", side_effect=RuntimeError("boom")), self.assertLogs("job-finder", level="WARNING"):
            self.assertFalse(reformat.reformat_job(self.conn, "gh:acme:9")["updated"])
        with mock.patch.object(sources, "fetch_greenhouse", return_value=[]):
            self.assertFalse(reformat.reformat_job(self.conn, "gh:acme:9")["updated"])
        self.add("us:acme:1", 0)
        with mock.patch.object(sources, "fetch_greenhouse") as f:
            self.assertFalse(reformat.reformat_job(self.conn, "us:acme:1")["updated"])
            f.assert_not_called()
        self.assertEqual(self.conn.execute("SELECT description FROM jobs WHERE id='gh:acme:9'").fetchone()[0], "Plain text")
        with self.assertRaises(LookupError):
            reformat.reformat_job(self.conn, "gh:nope:1")

    def test_a_workday_posting_is_rebuilt_from_its_address_and_asked_for_again(self):
        url = "https://acme.wd5.myworkdayjobs.com/en-US/acme_external/job/Austin-TX/Engineer_R-1"
        db.add_job(self.conn, sources.Job("wd:acme:R-1", "workday", "Acme", "Engineer", "Austin", url, description="Plain text"), 1.0)
        self.conn.execute("UPDATE jobs SET formatted=0"); self.conn.commit()
        asked = []
        reply = {"jobPostingInfo": {"jobDescription": "<p><b>Duties</b></p><ul><li>Build</li><li>Ship</li></ul>"}}
        with mock.patch.object(sources, "_get", lambda u, **k: asked.append(u) or reply):
            res = reformat.reformat_job(self.conn, "wd:acme:R-1")
        self.assertEqual(asked, ["https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/acme_external/job/Austin-TX/Engineer_R-1"])
        self.assertEqual(res["description"], "**Duties**\n\n- Build\n- Ship")

    def test_a_workday_403_means_taken_down_and_is_not_warned_about_or_asked_again(self):
        url = "https://acme.wd5.myworkdayjobs.com/en-US/acme_external/job/Austin-TX/Engineer_R-1"
        db.add_job(self.conn, sources.Job("wd:acme:R-1", "workday", "Acme", "Engineer", "Austin", url, description="Plain text"), 1.0)
        self.conn.execute("UPDATE jobs SET formatted=0"); self.conn.commit()
        forbidden = requests.HTTPError("403 Client Error", response=SimpleNamespace(status_code=403))
        with mock.patch.object(sources, "_get", side_effect=forbidden), self.assertNoLogs("job-finder", level="WARNING"):
            self.assertEqual(reformat.restore_all(self.conn, delay=0), {"restored": 0, "gone": 1, "failed": 0})
        self.assertEqual(self.conn.execute("SELECT formatted FROM jobs WHERE id='wd:acme:R-1'").fetchone()[0], 2)
        self.assertTrue(reformat._is_gone(forbidden, "wd"))
        self.assertFalse(reformat._is_gone(forbidden, "gh"))   # a 403 from any other source is still worth retrying

    def test_a_himalayas_posting_is_found_again_by_its_title(self):
        db.add_job(self.conn, sources.Job("hm:devops:blue-pearl", "himalayas", "Blue Pearl", "DevOps engineer", "Remote", "https://himalayas.app/x",
                                          description="Plain text"), 1.0)
        self.conn.execute("UPDATE jobs SET formatted=0"); self.conn.commit()
        found = sources.Job("hm:devops:blue-pearl", "himalayas", "Blue Pearl", "DevOps engineer", "Remote", "https://himalayas.app/x",
                            description="### KEY RESPONSIBILITIES\n\n- Design pipelines")
        with mock.patch.object(reformat.feed_sources, "fetch_himalayas", return_value=[found]) as f:
            res = reformat.reformat_job(self.conn, "hm:devops:blue-pearl")
        f.assert_called_once_with(["DevOps engineer"], pages=3)
        self.assertEqual(res["description"], "### KEY RESPONSIBILITIES\n\n- Design pipelines")

    def test_a_smartrecruiters_posting_is_asked_for_by_its_own_address(self):
        db.add_job(self.conn, sources.Job("sr:acme:744", "smartrecruiters", "Acme", "Engineer", "Austin", "https://jobs.smartrecruiters.com/acme/744",
                                          description="Plain text"), 1.0)
        self.conn.execute("UPDATE jobs SET formatted=0"); self.conn.commit()
        asked = []
        reply = {"jobAd": {"sections": {"jobDescription": {"text": "<p>Build <b>things</b></p><ul><li>One</li></ul>"},
                                        "qualifications": {"text": "<ul><li>Python</li></ul>"}}}, "postingUrl": "https://jobs.smartrecruiters.com/acme/744"}
        with mock.patch.object(sources, "_get", lambda u, **k: asked.append(u) or reply):
            res = reformat.reformat_job(self.conn, "sr:acme:744")
        self.assertEqual(asked, ["https://api.smartrecruiters.com/v1/companies/acme/postings/744"])
        self.assertEqual(res["description"], "Build **things**\n\n- One\n\n- Python")

    def test_everything_waiting_is_restored_best_first_with_one_request_per_board(self):
        for jid, score in (("gh:acme:1", 50), ("gh:acme:2", 90), ("ab:other:7", 70), ("wd:big:R-1", 60), ("us:skip:1", 99)):
            self.add(jid, 0)
            self.conn.execute("UPDATE jobs SET score=?, url=? WHERE id=?", (score, "https://big.wd5.myworkdayjobs.com/en-US/ext/job/X/Y_R-1", jid))
        self.conn.commit()
        order = []

        def board(name):
            def fetch(slug):
                order.append(f"{name}:{slug}")
                return [sources.Job(f"{name}:{slug}:{n}", "x", "A", "T", "L", "u", description=f"## formatted {n}") for n in (1, 2, 7)]
            return fetch
        with mock.patch.object(sources, "fetch_greenhouse", board("gh")), mock.patch.object(sources, "fetch_ashby", board("ab")), \
                mock.patch.object(sources, "_get", lambda u, **k: order.append("wd") or {"jobPostingInfo": {"jobDescription": "<li>Do</li>"}}):
            counts = reformat.restore_all(self.conn, delay=0)
        self.assertEqual(order, ["gh:acme", "ab:other", "wd"])           # acme holds the 90, other the 70, then the Workday job
        self.assertEqual(counts, {"restored": 4, "gone": 0, "failed": 0})   # the USAJobs job is not supported and is left alone
        rows = {r["id"]: (r["formatted"], r["description"]) for r in self.conn.execute("SELECT id, formatted, description FROM jobs")}
        self.assertEqual(rows["gh:acme:2"], (1, "## formatted 2"))
        self.assertEqual(rows["wd:big:R-1"], (1, "- Do"))
        self.assertEqual(rows["us:skip:1"], (0, "Plain text"))

    def test_a_closed_posting_is_marked_and_not_asked_for_again_while_a_failure_is_retried(self):
        self.add("gh:acme:5", 0)
        self.add("gh:down:1", 0)
        with mock.patch.object(sources, "fetch_greenhouse", side_effect=lambda slug: [] if slug == "acme" else (_ for _ in ()).throw(RuntimeError("net"))), \
                self.assertLogs("job-finder", level="WARNING"):
            counts = reformat.restore_all(self.conn, delay=0)
        self.assertEqual(counts, {"restored": 0, "gone": 1, "failed": 1})
        self.assertEqual({r["id"]: r["formatted"] for r in self.conn.execute("SELECT id, formatted FROM jobs")}, {"gh:acme:5": 2, "gh:down:1": 0})
        with mock.patch.object(sources, "fetch_greenhouse") as f:   # the closed one is not asked for again, the failed one is
            f.return_value = []
            reformat.restore_all(self.conn, delay=0)
            f.assert_called_once_with("down")
        self.assertFalse(reformat.can_reformat("gh:acme:5", 2))

    def test_a_404_means_taken_down_but_other_http_errors_are_retried_later(self):
        class HttpError(Exception):
            def __init__(self, code):
                self.response = type("R", (), {"status_code": code})()
        self.add("gh:gone:1", 0)
        self.add("gh:blocked:1", 0)
        with mock.patch.object(sources, "fetch_greenhouse", side_effect=lambda slug: (_ for _ in ()).throw(HttpError(404 if slug == "gone" else 403))), \
                self.assertLogs("job-finder", level="WARNING"):
            counts = reformat.restore_all(self.conn, delay=0)
            one = reformat.reformat_job(self.conn, "gh:blocked:1")
        self.assertEqual(counts, {"restored": 0, "gone": 1, "failed": 1})
        self.assertFalse(one["updated"])
        self.assertEqual({r["id"]: r["formatted"] for r in self.conn.execute("SELECT id, formatted FROM jobs")}, {"gh:gone:1": 2, "gh:blocked:1": 0})

    def test_the_run_can_be_stopped_and_reports_progress(self):
        import threading
        for n in (1, 2, 3):
            self.add(f"gh:b{n}:1", 0)
        stop, seen = threading.Event(), []
        with mock.patch.object(sources, "fetch_greenhouse", return_value=[]):
            reformat.restore_all(self.conn, delay=0, progress=lambda d, t: (seen.append((d, t)), stop.set()), stop=stop)
        self.assertEqual(seen, [(1, 3)])

    def test_which_jobs_can_be_restored(self):
        self.assertTrue(reformat.can_reformat("gh:a:1", 0))
        self.assertTrue(reformat.can_reformat("ab:a:uuid", None))
        self.assertFalse(reformat.can_reformat("gh:a:1", 1))
        self.assertTrue(reformat.can_reformat("wd:a:1", 0))
        self.assertTrue(reformat.can_reformat("hm:job:co", 0))
        self.assertTrue(reformat.can_reformat("sr:a:1", 0))
        self.assertTrue(reformat.can_reformat("rc:a:1", 0))
        for prefix in ("rp", "wk", "bb", "gm"):
            self.assertTrue(reformat.can_reformat(f"{prefix}:a:1", 0), prefix)
        self.assertFalse(reformat.can_reformat("us:a:1", 0))      # the rest keep their plain text

    def test_a_rippling_posting_is_asked_for_by_its_own_address_and_keeps_its_bold_headings(self):
        db.add_job(self.conn, sources.Job("rp:acme:uuid-1", "rippling", "acme", "Engineer", "Austin", "https://ats.rippling.com/acme/jobs/uuid-1",
                                          description="Plain text"), 1.0)
        self.conn.execute("UPDATE jobs SET formatted=0"); self.conn.commit()
        asked = []
        reply = {"description": {"company": "<div><p><b><strong style='font-size:18pt'>About Acme</strong></b></p><p>We build things.</p></div>",
                                 "role": "<div><p>Duties</p><ul><li>Build</li><li>Ship</li></ul></div>"}}
        with mock.patch.object(reformat.ats_sources, "_get", lambda u, **k: asked.append(u) or reply):
            res = reformat.reformat_job(self.conn, "rp:acme:uuid-1")
        self.assertEqual(asked, ["https://ats.rippling.com/api/v2/board/acme/jobs/uuid-1"])
        self.assertEqual(res["description"], "**About Acme**\n\nWe build things.\n\nDuties\n\n- Build\n- Ship")

    def test_nested_bold_is_one_bold(self):
        self.assertEqual(sources.strip_html("<p><b><strong>Heading</strong></b></p><p>Body</p>"), "**Heading**\n\nBody")


if __name__ == "__main__":
    unittest.main()
