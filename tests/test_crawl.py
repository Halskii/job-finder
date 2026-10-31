"""Bulk board discovery from Common Crawl: address parsing, index paging, caching, and checking candidates. No network."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import crawl
import sources

CFG = {"titles": ["Software Engineer"], "exclude_title_words": ["senior"], "us_only": True, "request_delay": 0, "min_pay": 0,
       "workday_search_terms": ["software engineer"]}


def job(title, location="Austin, TX"):
    return sources.Job(id="x", source="s", company="c", title=title, location=location, url="u")


class CandidateTests(unittest.TestCase):
    def test_company_boards_are_found_in_addresses(self):
        c = crawl.candidate_from_url
        self.assertEqual(c("greenhouse", "https://boards.greenhouse.io/stripe/jobs/123"), "stripe")
        self.assertEqual(c("greenhouse", "https://job-boards.greenhouse.io/acme"), "acme")
        self.assertEqual(c("greenhouse", "https://boards.greenhouse.io/embed/job_board?for=widgetco"), "widgetco")
        self.assertIsNone(c("greenhouse", "https://boards.greenhouse.io/embed/job_app"))
        self.assertIsNone(c("greenhouse", "https://boards.greenhouse.io/robots.txt"))
        self.assertEqual(c("lever", "https://jobs.lever.co/netflix/abc-123"), "netflix")
        self.assertEqual(c("ashby", "https://jobs.ashbyhq.com/Open%20AI/xyz"), None)       # not a plausible slug
        self.assertEqual(c("ashby", "https://jobs.ashbyhq.com/1password/uuid?utm_source=x"), "1password")
        self.assertEqual(c("recruitee", "https://bunq.recruitee.com/o/data-scientist"), "bunq")
        self.assertIsNone(c("recruitee", "https://www.recruitee.com/"))
        self.assertEqual(c("bamboohr", "https://acme.bamboohr.com/careers/12"), "acme")
        self.assertIsNone(c("bamboohr", "https://acme.bamboohr.com/login.php"))
        self.assertEqual(c("rippling", "https://ats.rippling.com/rippling/jobs/uuid"), "rippling")
        self.assertEqual(c("gem", "https://jobs.gem.com/11x-ai/abc"), "11x-ai")
        self.assertEqual(c("personio", "https://acme.jobs.personio.de/job/5"), "acme")
        self.assertEqual(c("workable", "https://apply.workable.com/huggingface/j/ABC/"), "huggingface")

    def test_smartrecruiters_keeps_its_capitals(self):
        self.assertEqual(crawl.candidate_from_url("smartrecruiters", "https://jobs.smartrecruiters.com/AcmeCo/123-dev"), "AcmeCo")

    def test_workday_addresses_become_site_entries(self):
        e = crawl.candidate_from_url("workday", "https://vanguard.wd5.myworkdayjobs.com/en-US/vanguard_external/job/x_R1")
        self.assertEqual((e["host"], e["tenant"], e["site"]), ("vanguard.wd5.myworkdayjobs.com", "vanguard", "vanguard_external"))
        self.assertIsNone(crawl.candidate_from_url("workday", "https://myworkdayjobs.com/"))


class IndexTests(unittest.TestCase):
    def resp(self, status, text=""):
        return SimpleNamespace(status_code=status, text=text)

    def test_pages_are_read_in_order_and_bad_lines_skipped(self):
        calls = []

        def get(url, params, headers, timeout):
            calls.append(dict(params))
            if params.get("showNumPages"):
                return self.resp(200, json.dumps({"pages": 2, "pageSize": 5, "blocks": 9}))
            return self.resp(200, json.dumps({"url": f"https://jobs.lever.co/co{params['page']}/1"}) + "\nnot json\n")

        with mock.patch.object(crawl.requests, "get", get), mock.patch.object(crawl.time, "sleep"):
            urls = list(crawl.iter_urls("CC-X", "jobs.lever.co/*", 0))
        self.assertEqual(urls, ["https://jobs.lever.co/co0/1", "https://jobs.lever.co/co1/1"])
        self.assertEqual(calls[0]["output"], "json")

    def test_a_timeout_is_retried_and_a_missing_pattern_gives_nothing(self):
        seq = [self.resp(504), self.resp(200, json.dumps({"pages": 0}))]
        with mock.patch.object(crawl.requests, "get", lambda *a, **k: seq.pop(0)), mock.patch.object(crawl.time, "sleep"):
            self.assertEqual(list(crawl.iter_urls("CC-X", "p", 0)), [])
        with mock.patch.object(crawl.requests, "get", lambda *a, **k: self.resp(404)), mock.patch.object(crawl.time, "sleep"):
            self.assertEqual(list(crawl.iter_urls("CC-X", "p", 0)), [])

    def test_an_unanswering_server_is_never_recorded_as_done(self):
        def failing(crawl_id, pattern, pause=1.0):
            yield "https://jobs.lever.co/read-before-failing/1"
            raise crawl.IndexUnavailable("timeout")

        logs = []
        with tempfile.TemporaryDirectory() as d, mock.patch.object(crawl, "iter_urls", failing),                 mock.patch.object(crawl, "crawl_ids", lambda n: ["CC-1", "CC-2"]):
            found = crawl.enumerate_candidates(Path(d), ["lever"], 2, log=logs.append)
            self.assertEqual(found["lever"], ["read-before-failing"])          # what was read is kept
            self.assertEqual(crawl.load_cache(Path(d))["done"], {})            # but the crawl will be read again
            self.assertTrue(any("not answering" in m for m in logs))
            self.assertEqual(sum("not answering" in m for m in logs), 1)       # and the platform is abandoned, not retried per crawl

    def test_a_dead_server_raises_but_a_404_means_nothing_there(self):
        with mock.patch.object(crawl.requests, "get", lambda *a, **k: self.resp(504)), mock.patch.object(crawl.time, "sleep"):
            with self.assertRaises(crawl.IndexUnavailable):
                list(crawl.iter_urls("CC-X", "p", 0))

    def test_a_failed_crawl_list_falls_back_to_the_remembered_one(self):
        with tempfile.TemporaryDirectory() as d:
            crawl.save_cache(Path(d), {"done": {}, "found": {}, "ids": ["CC-9"]})
            read = []

            def fake_iter(crawl_id, pattern, pause=1.0):
                read.append(crawl_id)
                yield "https://jobs.lever.co/zz/1"

            def no_list(n):
                raise crawl.IndexUnavailable("list did not load")

            with mock.patch.object(crawl, "crawl_ids", no_list), mock.patch.object(crawl, "iter_urls", fake_iter):
                found = crawl.enumerate_candidates(Path(d), ["lever"], 3, log=lambda m: None)
            self.assertEqual((found["lever"], read), (["zz"], ["CC-9"]))

    def test_crawls_already_read_are_not_read_again(self):
        urls = {"CC-1": ["https://jobs.lever.co/aa/1", "https://jobs.lever.co/aa/2", "https://jobs.lever.co/bb/1"], "CC-2": ["https://jobs.lever.co/cc/1"]}
        read = []

        def fake_iter(crawl_id, pattern, pause=1.0):
            read.append(crawl_id)
            yield from urls[crawl_id]

        with tempfile.TemporaryDirectory() as d, mock.patch.object(crawl, "iter_urls", fake_iter), \
                mock.patch.object(crawl, "crawl_ids", lambda n: ["CC-1", "CC-2"][:n]):
            first = crawl.enumerate_candidates(Path(d), ["lever"], 1, log=lambda m: None)
            self.assertEqual(first["lever"], ["aa", "bb"])
            second = crawl.enumerate_candidates(Path(d), ["lever"], 2, log=lambda m: None)
            self.assertEqual(second["lever"], ["aa", "bb", "cc"])
            self.assertEqual(read, ["CC-1", "CC-2"])         # CC-1 was not read twice


class ValidateTests(unittest.TestCase):
    def run_validate(self, cands, results, **kw):
        with mock.patch.object(crawl, "check", lambda system, cand, cfg: results[cand]):
            return crawl.validate("lever", cands, CFG, workers=3, log=lambda m: None, **kw)

    def test_only_live_boards_with_a_matching_posting_are_kept(self):
        results = {"match": (5, 2), "nomatch": (5, 0), "dead": (0, 0)}
        self.assertEqual(self.run_validate(["match", "nomatch", "dead"], results), ["match"])
        self.assertEqual(self.run_validate(["match", "nomatch", "dead"], results, keep_all=True), ["match", "nomatch"])

    def test_failed_boards_are_remembered_and_not_asked_again_for_a_while(self):
        results = {"a": (0, 0), "b": (5, 1)}
        asked = []

        def check(system, cand, cfg):
            asked.append(cand)
            return results[cand]

        with tempfile.TemporaryDirectory() as d, mock.patch.object(crawl, "check", check):
            args = dict(cfg=CFG, workers=2, log=lambda m: None, root=Path(d))
            self.assertEqual(crawl.validate("lever", ["a", "b"], **args, now=1_700_000_000), ["b"])
            self.assertEqual(crawl.validate("lever", ["a", "b"], **args, now=1_700_000_000 + 3600), ["b"])      # "a" skipped, "b" asked again
            self.assertEqual(asked, ["a", "b", "b"])
            crawl.validate("lever", ["a"], **args, now=1_700_000_000 + 15 * 86400)                              # window over: asked again
            self.assertEqual(asked, ["a", "b", "b", "a"])

    def test_check_counts_postings_that_match_the_titles_and_us_locations(self):
        jobs = [job("Software Engineer"), job("Software Engineer", "London, UK"), job("Chef"), job("Senior Software Engineer")]
        with mock.patch.object(crawl.discovery, "SYSTEMS", [("lever", lambda slug: jobs)]):
            self.assertEqual(crawl.check("lever", "acme", CFG), (4, 1))

    def test_known_boards_are_recognised(self):
        src = {"lever": ["Netflix"], "workday": [{"host": "h", "tenant": "t", "site": "s", "name": "X"}]}
        self.assertIn("netflix", crawl.known_keys(src, "lever"))
        self.assertEqual(crawl.known_keys(src, "workday"), {crawl._workday_id({"host": "h", "tenant": "t", "site": "s"})})


class CrawlCommandTests(unittest.TestCase):
    def test_an_unreachable_index_is_explained_not_reported_as_zero_boards(self):
        import argparse
        import io
        from contextlib import redirect_stdout

        import main

        def down(n):
            raise crawl.IndexUnavailable("no answer")

        out = io.StringIO()
        args = argparse.Namespace(systems=None, crawls=3, keep_all=False, recheck=False, add=True)
        with tempfile.TemporaryDirectory() as d, mock.patch.object(main, "ROOT", Path(d)), mock.patch.object(crawl, "crawl_ids", down),                 mock.patch.object(crawl.time, "sleep"), redirect_stdout(out):
            main.cmd_crawl(args, {"sources": {}}, {}, None)
        self.assertIn("not answering right now", out.getvalue())
        self.assertNotIn("0 new board", out.getvalue())


if __name__ == "__main__":
    unittest.main()
