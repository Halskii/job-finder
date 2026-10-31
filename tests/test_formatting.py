"""Offline tests for keeping a posting's formatting: the HTML to Markdown conversion, the fetchers, and restoring old postings."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import db
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
        self.conn = db.connect(self.path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def add(self, job_id, formatted):
        db.add_job(self.conn, sources.Job(job_id, "greenhouse", "Acme", "Dev", "Austin", "https://x", description="Plain text"), 1.0)
        self.conn.execute("UPDATE jobs SET formatted=? WHERE id=?", (formatted, job_id))
        self.conn.commit()

    def test_new_postings_are_marked_formatted_and_old_rows_are_not(self):
        db.add_job(self.conn, sources.Job("gh:a:1", "greenhouse", "A", "Dev", "x", "https://x", description="d"), 1.0)
        self.assertEqual(self.conn.execute("SELECT formatted FROM jobs").fetchone()[0], 1)
        old = sqlite3.connect(str(Path(self.tmp.name) / "old.db"))
        old.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, source TEXT, company TEXT, title TEXT, location TEXT, url TEXT, posted TEXT,"
                    " pay_min INTEGER, pay_max INTEGER, description TEXT, quick_score REAL, score INTEGER, verdict TEXT, analysis TEXT,"
                    " status TEXT DEFAULT 'new', first_seen REAL, notes TEXT)")
        old.execute("INSERT INTO jobs (id, description) VALUES ('gh:b:2', 'plain')")
        old.commit()
        old.close()
        conn = db.connect(str(Path(self.tmp.name) / "old.db"))
        self.assertEqual(conn.execute("SELECT formatted FROM jobs").fetchone()[0], 0)
        conn.close()

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
        self.add("wd:acme:1", 0)
        with mock.patch.object(sources, "fetch_greenhouse") as f:
            self.assertFalse(reformat.reformat_job(self.conn, "wd:acme:1")["updated"])
            f.assert_not_called()
        self.assertEqual(self.conn.execute("SELECT description FROM jobs WHERE id='gh:acme:9'").fetchone()[0], "Plain text")
        with self.assertRaises(LookupError):
            reformat.reformat_job(self.conn, "gh:nope:1")

    def test_which_jobs_can_be_restored(self):
        self.assertTrue(reformat.can_reformat("gh:a:1", 0))
        self.assertTrue(reformat.can_reformat("ab:a:uuid", None))
        self.assertFalse(reformat.can_reformat("gh:a:1", 1))
        self.assertFalse(reformat.can_reformat("wd:a:1", 0))


if __name__ == "__main__":
    unittest.main()
