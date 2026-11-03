"""Retailer career sites (Lowe's, CVS, TJX, Dollar Tree and Kohl's on Phenom; Home Depot's own site), read from saved copies of the real pages in tests/data."""
import json
import unittest
from pathlib import Path
from unittest import mock

import main
import retail_sources as rs
import sources
from tests.govoff import retail_off

DATA = Path(__file__).parent / "data"
text = lambda name: (DATA / name).read_text(encoding="utf-8")


class Resp:
    def __init__(self, body, status=200):
        self.text, self.status_code = body, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def search_page(jobs, total):
    ddo = {"eagerLoadRefineSearch": {"status": 200, "hits": len(jobs), "totalHits": total, "data": {"jobs": jobs}}}
    return f"<script>phApp.ddo = {json.dumps(ddo)}; phApp.x = 1;</script>"


def fake_job(n, title="Software Engineer"):
    return {"jobId": f"JR-{n:08d}", "title": f"{title} {n}", "cityState": "Charlotte, North Carolina", "country": "United States of America",
            "postedDate": "2026-10-26T00:00:00.000+0000", "descriptionTeaser": "teaser"}


class Session:
    """Answers each address from a function and remembers what was asked."""

    def __init__(self, answer):
        self.answer, self.asked = answer, []

    def get(self, url, **kw):
        self.asked.append(url)
        return self.answer(url)


class Registered(unittest.TestCase):
    def test_every_retailer_has_a_fetcher_and_a_job_page_reader(self):
        for key, label, fetch in rs.RETAIL_SOURCES:
            self.assertTrue(callable(getattr(rs, fetch)), fetch)
            if key in rs.PHENOM_SITES or key == "homedepot":         # their lists carry no text, so the page is read later
                self.assertIn(key, sources.DETAIL_FILLERS)
            self.assertNotIn(key, main.gov_sources.GOV_SOURCES)     # a retailer is searched with the person's own titles
        self.assertTrue(set(rs.PHENOM_SITES) | {"homedepot"} <= {k for k, *_ in rs.RETAIL_SOURCES})
        keys = [k for k, *_ in rs.RETAIL_SOURCES]
        self.assertEqual(len(keys), len(set(keys)))                  # no two retailers share a key


class PhenomPages(unittest.TestCase):
    def test_the_json_in_a_page_is_found_whatever_follows_it(self):
        data = rs.phenom_data(text("lowes_search.html"))
        self.assertEqual(data["eagerLoadRefineSearch"]["hits"], 2)

    def test_a_page_without_the_data_is_an_error(self):
        with self.assertRaises(ValueError):
            rs.phenom_data("<html><body>Access Denied</body></html>")

    def test_search_results(self):
        jobs = rs.parse_phenom_results(rs.phenom_data(text("lowes_search.html")), "lowes")
        self.assertEqual(len(jobs), 2)
        j = jobs[0]
        self.assertEqual((j.id, j.source, j.company, j.posted), ("lowes:JR-02671814", "lowes", "Lowe's", "2026-10-26"))
        self.assertEqual(j.title, "Frontend Software Engineer - Frameworks & Dev Platforms")
        self.assertEqual(j.location, "Charlotte, North Carolina, United States")
        self.assertEqual(j.url, "https://talent.lowes.com/us/en/job/JR-02671814/Frontend-Software-Engineer-Frameworks-Dev-Platforms")
        # no text from the list: the web app reads the full posting when someone's search selects it, which needs `detail` set
        self.assertEqual((j.description, j.detail), ("", {"api": j.url}))

    def test_a_job_with_no_id_or_title_is_skipped(self):
        data = {"eagerLoadRefineSearch": {"data": {"jobs": [{"title": "No id"}, {"jobId": "JR-1", "title": ""}, fake_job(5)]}}}
        self.assertEqual([j.id for j in rs.parse_phenom_results(data, "lowes")], ["lowes:JR-00000005"])
        self.assertEqual(rs.parse_phenom_results({}, "lowes"), [])

    def test_a_site_that_gives_only_an_address_line_keeps_it(self):
        job = {**fake_job(9), "location": "1600 N Business 45, Corsicana, TX 75110"}
        job.pop("cityState"), job.pop("country")
        self.assertEqual(rs._phenom_places(job), "1600 N Business 45, Corsicana, TX 75110")

    def test_a_job_open_in_several_places_lists_them_all(self):
        job = {**fake_job(7), "isMultiLocation": True, "multi_location_array": [
            {"cityState": "Charlotte, North Carolina", "country": "United States of America"},
            {"cityState": "Austin, Texas", "country": "United States of America"},
            {"cityState": "Charlotte, North Carolina", "country": "United States of America"},
            {"cityState": "Bengaluru, Karnataka", "country": "India"}]}
        self.assertEqual(rs._phenom_places(job), "Charlotte, North Carolina, United States; Austin, Texas, United States; Bengaluru, Karnataka, India")


class Fetching(unittest.TestCase):
    def fetch(self, answer, terms=("software engineer",), pages=8):
        session = Session(answer)
        with mock.patch.object(sources, "_session", lambda: session), mock.patch.object(rs.time, "sleep"):
            return rs.fetch_lowes(list(terms), pages, 0), session.asked

    def test_every_result_page_is_read_and_the_search_words_are_asked_for(self):
        def answer(url):
            start = int(url.rsplit("from=", 1)[1])
            return Resp(search_page([fake_job(start + i) for i in range(10)] if start < 20 else [fake_job(start + i) for i in range(3)], 23))
        jobs, asked = self.fetch(answer)
        self.assertEqual(len(jobs), 23)
        self.assertEqual(asked, [f"https://talent.lowes.com/us/en/search-results?keywords=software%20engineer&from={n}" for n in (0, 10, 20)])
        self.assertFalse(any("s=1" in u for u in asked))      # `s=1` makes the page ignore the words and list every job

    def test_the_next_page_starts_after_however_many_the_last_one_held(self):
        # CVS lists 25 to a page, not 10: stepping by ten would read the same jobs again
        def answer(url):
            start = int(url.rsplit("from=", 1)[1])
            return Resp(search_page([fake_job(start + i) for i in range(min(4, 10 - start))], 10))
        with mock.patch.object(sources, "_session", lambda: Session(answer)), mock.patch.object(rs.time, "sleep"):
            jobs = rs.fetch_phenom("cvs", ["software engineer"], 8, 0)
        self.assertEqual([j.id for j in jobs], [f"cvs:JR-{n:08d}" for n in range(10)])

    def test_a_short_first_page_is_the_only_page(self):
        jobs, asked = self.fetch(lambda url: Resp(search_page([fake_job(1), fake_job(2)], 2)))
        self.assertEqual((len(jobs), len(asked)), (2, 1))

    def test_the_page_limit_and_repeats_across_words(self):
        jobs, asked = self.fetch(lambda url: Resp(search_page([fake_job(int(url.rsplit("from=", 1)[1]) + i) for i in range(10)], 500)), ("a b", "c"), pages=2)
        self.assertEqual(len(asked), 4)                       # two pages for each word
        self.assertEqual(len(jobs), 20)                       # the same twenty come back for the second word and count once

    def test_a_blocked_page_raises_so_the_run_says_so(self):
        with self.assertRaises(ValueError):
            self.fetch(lambda url: Resp("<html>Access Denied</html>"))
        with self.assertRaises(RuntimeError):
            self.fetch(lambda url: Resp("", 403))


class JobPage(unittest.TestCase):
    def job(self):
        return sources.Job("lowes:JR-02671814", "lowes", "Lowe's", "Frontend Software Engineer", "Charlotte, North Carolina, United States",
                           "https://talent.lowes.com/us/en/job/JR-02671814/x", posted="2026-10-20")

    def test_text_date_place_and_pay(self):
        j = self.job()
        with mock.patch.object(sources, "_session", lambda: Session(lambda url: Resp(text("lowes_job.html")))):
            sources.fill_detail(j)
        self.assertIn("Your Impact", j.description)
        self.assertTrue(j.description.rstrip().endswith("Pay Range: $75,300.00 - $143,100.00 annually"))
        self.assertEqual((j.pay_min, j.pay_max, j.posted), (75300, 143100, "2026-10-26"))
        self.assertEqual(j.location, "Charlotte, North Carolina, United States")

    def test_a_page_with_no_posting_is_an_error_so_it_is_retried_next_run(self):
        page = "<script>phApp.ddo = {\"jobDetail\": {\"status\": 200, \"data\": {\"job\": {}}}};</script>"
        with mock.patch.object(sources, "_session", lambda: Session(lambda url: Resp(page))), self.assertRaises(ValueError):
            rs.fill_phenom_detail(self.job())
        with mock.patch.object(sources, "_session", lambda: Session(lambda url: Resp("", 410))), self.assertRaises(RuntimeError):
            rs.fill_phenom_detail(self.job())      # a posting that was taken down


class HomeDepot(unittest.TestCase):
    def test_only_the_addresses_holding_a_search_word_are_kept(self):
        jobs = rs.parse_homedepot_sitemap(text("homedepot_sitemap.xml"), ["software engineer", "data scientist"])
        self.assertEqual([j.id for j in jobs], ["homedepot:23877938", "homedepot:24077598", "homedepot:24090574"])
        j = jobs[1]
        self.assertEqual((j.source, j.company, j.title, j.location), ("homedepot", "The Home Depot", "Software Engineer Ii Remote Remote", ""))
        self.assertEqual(j.url, "https://careers.homedepot.com/job/24077598/software-engineer-ii-remote-remote/")

    def test_a_word_must_be_whole_words_in_the_address(self):
        xml = ("<loc>https://careers.homedepot.com/job/1/software-engineering-manager-atlanta-ga/</loc>"
               "<loc>https://careers.homedepot.com/job/2/sr-software-engineer-atlanta-ga/</loc>"
               "<loc>https://careers.homedepot.com/job/3/software-engineer/</loc>")
        self.assertEqual([j.id for j in rs.parse_homedepot_sitemap(xml, ["Software Engineer"])], ["homedepot:2", "homedepot:3"])
        self.assertEqual(rs.parse_homedepot_sitemap(xml, []), [])
        self.assertEqual(rs.parse_homedepot_sitemap("<html>Access Denied</html>", ["software engineer"]), [])

    def test_the_sitemap_is_fetched_once(self):
        session = Session(lambda url: Resp(text("homedepot_sitemap.xml")))
        with mock.patch.object(sources, "_session", lambda: session):
            jobs = rs.fetch_homedepot(["software engineer"], 8, 0)
        self.assertEqual(session.asked, ["https://careers.homedepot.com/jobsitemap"])
        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0].detail, {"api": jobs[0].url})      # the web app reads the page later
        with mock.patch.object(sources, "_session", lambda: Session(lambda url: Resp("", 403))), self.assertRaises(RuntimeError):
            rs.fetch_homedepot(["software engineer"], 8, 0)

    def job(self, jid="24090574", slug="staff-software-engineer-atlanta-ga"):
        return sources.Job(f"homedepot:{jid}", "homedepot", "The Home Depot", "Staff Software Engineer Atlanta Ga", "", f"https://careers.homedepot.com/job/{jid}/{slug}/")

    def test_the_job_page_gives_title_text_date_and_place(self):
        j = self.job()
        with mock.patch.object(sources, "_session", lambda: Session(lambda url: Resp(text("homedepot_job.html")))):
            sources.fill_detail(j)
        self.assertEqual((j.title, j.posted, j.location), ("Staff Software Engineer", "2026-10-30", "Atlanta, GA"))
        self.assertGreater(len(j.description), 500)
        self.assertNotIn("<", j.description)

    def test_a_virtual_address_means_remote(self):
        j = self.job("24077598", "software-engineer-ii-remote-remote")
        with mock.patch.object(sources, "_session", lambda: Session(lambda url: Resp(text("homedepot_job_remote.html")))):
            sources.fill_detail(j)
        self.assertEqual((j.title, j.location), ("Software Engineer II (Remote)", "Atlanta, GA (Remote)"))

    def test_pay_in_the_text_is_read(self):
        page = ('<script type="application/ld+json">{"@context":"https://schema.org","@type":"JobPosting","title":"Dev","datePosted":"2026-10-01",'
                '"description":"<p>Build things.</p><p>Pay range: $120,000 - $150,000 a year</p>","jobLocation":[{"address":{"addressLocality":"Austin","addressRegion":"TX","streetAddress":"1 Main"}},'
                '{"address":{"addressLocality":"Dallas","addressRegion":"TX","streetAddress":"2 Main"}}]}</script>')
        j = self.job()
        with mock.patch.object(sources, "_session", lambda: Session(lambda url: Resp(page))):
            sources.fill_detail(j)
        self.assertEqual((j.pay_min, j.pay_max, j.location), (120000, 150000, "Austin, TX; Dallas, TX"))

    def test_a_page_without_the_posting_is_an_error_so_it_is_retried_next_run(self):
        for page in ("<html>Access Denied</html>", '<script type="application/ld+json">{"@type":"WebPage"}</script>', '<script type="application/ld+json">not json</script>'):
            with mock.patch.object(sources, "_session", lambda: Session(lambda url: Resp(page))), self.assertRaises(ValueError):
                rs.fill_homedepot_detail(self.job())


class Collecting(unittest.TestCase):
    CFG = {"request_delay": 0, "titles": ["Software Engineer II", "DevOps Engineer"]}

    def setUp(self):
        self.calls = {}                      # retailer -> [(search words, pages)]
        self.found = {}                      # retailer -> what its fetcher returns
        for key, label, fetch in rs.RETAIL_SOURCES:
            def fn(terms, pages, delay, key=key):
                self.calls.setdefault(key, []).append((terms, pages))
                result = self.found.get(key, [])
                if isinstance(result, Exception):
                    raise result
                return result
            patcher = mock.patch.object(rs, fetch, fn)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(main, "log")
        self.log = patcher.start()
        self.addCleanup(patcher.stop)

    def test_every_retailer_is_searched_with_the_title_words_and_logged(self):
        self.found["lowes"] = [fake_jobs()]
        jobs = main.collect_retail(self.CFG)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(set(self.calls), {key for key, *_ in rs.RETAIL_SOURCES})
        self.assertEqual(self.calls["cvs"], [(["software engineer", "devops engineer"], 8)])
        self.assertTrue(any("lowes" in c[0][0] and "1 postings" in c[0][0] for c in self.log.call_args_list))

    def test_retail_titles_are_searched_too_but_not_twice(self):
        main.collect_retail({**self.CFG, "retail_titles": ["Cashier", "Store Manager II", "DevOps Engineer"]})
        self.assertEqual(self.calls["lowes"][0][0], ["software engineer", "devops engineer", "cashier", "store manager"])

    def test_settings_override_and_off_switch(self):
        main.collect_retail({**self.CFG, "retailers": {**retail_off(), "lowes": {"search_terms": ["react"], "pages": 3}}})
        self.assertEqual(self.calls, {"lowes": [(["react"], 3)]})
        self.calls.clear()
        main.collect_retail({**self.CFG, "retailers": retail_off()})
        self.assertEqual(self.calls, {})

    def test_a_site_that_fails_does_not_stop_the_others(self):
        self.found["lowes"] = RuntimeError("blocked")
        self.found["cvs"] = [fake_jobs()]
        self.assertEqual(len(main.collect_retail(self.CFG)), 1)
        self.assertTrue(any("skipped (RuntimeError: blocked)" in c[0][0] for c in self.log.call_args_list))


class WorkdayRetailers(unittest.TestCase):
    def test_a_retail_board_is_searched_with_the_retail_titles_and_its_jobs_are_marked(self):
        seen = {}

        def fake(entry, terms, pages, delay):
            seen[entry["name"]] = terms
            return []
        entries = [{"name": "Plain", "host": "a.wd1.myworkdayjobs.com", "tenant": "a", "site": "s"},
                   {"name": "Store", "host": "b.wd1.myworkdayjobs.com", "tenant": "b", "site": "s", "retail": True}]
        with mock.patch.object(sources, "fetch_workday", fake), mock.patch.object(main, "log"):
            main.collect_workday({"titles": ["Software Engineer"], "retail_titles": ["Cashier"], "workday_workers": 1}, entries, 0)
        self.assertEqual(seen, {"Plain": ["software engineer"], "Store": ["software engineer", "cashier"]})


def fake_jobs():
    return sources.Job("lowes:JR-1", "lowes", "Lowe's", "Software Engineer", "Charlotte, North Carolina, United States", "https://talent.lowes.com/x")


if __name__ == "__main__":
    unittest.main()
