"""Retailers on SAP SuccessFactors career sites (Tractor Supply, Sephora, Adidas, Under Armour), read from saved copies of the real pages in tests/data."""
import unittest
from pathlib import Path
from unittest import mock

import retail_successfactors as rsf
import sources
from tests.retailtools import Resp, Session

DATA = Path(__file__).parent / "data"
text = lambda name: (DATA / name).read_text(encoding="utf-8")
KEYS = ("tractorsupply", "sephora", "adidas", "underarmour")


def row(n, place="Miami, FL, US, 33183", title=None, path="/USA/job"):
    return (f'<tr class="data-row"><td class="colTitle"><span class="jobTitle hidden-phone"><a href="{path}/Town-Job-{n}-FL/{1000 + n}/" class="jobTitle-link">'
            f'{title or f"Job {n}"}</a></span></td><td class="colLocation hidden-phone"><span class="jobLocation">\n{place}\n</span></td>'
            f'<td class="colDate"><span class="jobDate">Oct 10, 2026\n</span></td></tr>')


def results(first, count, total):
    """A search page of `count` rows numbered from `first`."""
    rows = "".join(row(n) for n in range(first, first + count))
    return (f'<div class="searchResultsShell"><span class="paginationLabel">Results <b>{first + 1} - {first + count}</b> of <b>{total}</b></span>'
            f'<table><tbody>{rows}</tbody></table></div>')


class Registered(unittest.TestCase):
    def test_every_key_has_a_fetcher_and_a_job_page_reader(self):
        self.assertEqual(set(rsf.READERS), set(KEYS))
        for key, (label, fetch) in rsf.READERS.items():
            self.assertTrue(callable(fetch) and label, key)
            self.assertIs(sources.DETAIL_FILLERS[key], rsf.fill_successfactors_retail_detail)

    def test_quiktrip_is_not_read(self):
        # its search page is drawn by script: the page holds no postings
        self.assertNotIn("quiktrip", rsf.READERS)

    def test_the_sites_state_no_crawl_delay_and_the_run_is_bounded(self):
        self.assertEqual(rsf.CRAWL_DELAY, 0.0)
        self.assertGreater(rsf.MAX_REQUESTS, 0)


class Places(unittest.TestCase):
    def test_places(self):
        for raw, expected in [
            ("Raceland (Mathews), LA, US, 70394", ("Raceland (Mathews), LA, United States", True)),
            ("Avon, IN, US, USA_522747", ("Avon, IN, United States", True)),          # a requisition number where the postal code goes
            ("Kansas City, KS, US", ("Kansas City, KS, United States", True)),
            ("Remote, US", ("United States (Remote)", True)),
            ("Remote, NC, US, 28202", ("NC, United States (Remote)", True)),
            ("Remote, Remote, US, USA_520000", ("United States (Remote)", True)),      # Sephora repeats it, and cuts it to "Remo" on the job page
            ("Remote, Remo, US", ("United States (Remote)", True)),
            ("Edmonton, AB, CA, T5P 4B3", ("Edmonton, AB, CA", False)),
            ("Landquart, GR, CH", ("Landquart, GR, CH", False)),
            ("Kowloon Bay, HK", ("Kowloon Bay, HK", False)),
            ("", ("", False)),
        ]:
            self.assertEqual(rsf.sf_place(raw), expected, raw)


class SearchPages(unittest.TestCase):
    def test_tractor_supply_rows(self):
        jobs, listed, total = rsf.parse_search_page(text("successfactors_tractorsupply_search.html"), "tractorsupply")
        self.assertEqual((len(jobs), listed, total), (3, 3, 286))
        j = jobs[0]
        self.assertEqual((j.id, j.source, j.company), ("tractorsupply:1412130000", "tractorsupply", "Tractor Supply Company"))
        self.assertEqual(j.title, "Team Member - Cashier/Sales Associate/Retail")
        self.assertEqual(j.location, "Raceland (Mathews), LA, United States")
        self.assertEqual(j.url, "https://www.tractorsupply.careers/job/Raceland-%28Mathews%29-Team-Member-CashierSales-AssociateRetail-LA-70394/1412130000/")
        self.assertIsNone(j.posted)                       # the list has no date; the job page does
        self.assertEqual((j.description, j.detail), ("", {"api": j.url}))

    def test_sephora_keeps_the_united_states_and_reads_the_date(self):
        jobs, listed, total = rsf.parse_search_page(text("successfactors_sephora_search.html"), "sephora")
        self.assertEqual((len(jobs), listed, total), (3, 4, 835))      # the fourth row is a Canadian store
        self.assertEqual([j.id for j in jobs], ["sephora:1364433955", "sephora:1367662155", "sephora:1368036055"])
        j = jobs[0]
        self.assertEqual((j.company, j.posted, j.location), ("Sephora", "2026-10-09", "Avon, IN, United States"))
        self.assertTrue(j.url.startswith("https://jobs.sephora.com/USA/job/"))

    def test_adidas_drops_other_countries(self):
        jobs, listed, total = rsf.parse_search_page(text("successfactors_adidas_search.html"), "adidas")
        self.assertEqual((len(jobs), listed, total), (2, 4, 173))      # a Swiss and a Canadian store are left out
        self.assertEqual((jobs[0].company, jobs[0].location, jobs[0].posted), ("adidas", "Kansas City, KS, United States", "2026-10-27"))
        self.assertEqual(jobs[0].url, "https://jobs.adidas-group.com/job/Kansas-City-Lead-KS/1442017233/")

    def test_under_armour_tiles_with_a_remote_job(self):
        jobs, listed, total = rsf.parse_search_page(text("successfactors_underarmour_search.html"), "underarmour")
        self.assertEqual((len(jobs), listed, total), (3, 3, 33))
        self.assertEqual((jobs[0].id, jobs[0].title, jobs[0].company), ("underarmour:1434557100", "Lead, Application and AI Security Engineer", "Under Armour"))
        self.assertEqual(jobs[0].location, "Baltimore, MD, United States")
        self.assertEqual(jobs[2].location, "United States (Remote)")
        self.assertTrue(all(j.url.startswith("https://careers.underarmour.com/job/") and j.detail == {"api": j.url} for j in jobs))

    def test_a_row_with_no_id_or_title_is_skipped(self):
        page = results(0, 1, 3) + row(5, title=" ") + '<tr class="data-row"><td><a class="jobTitle-link" href="/job/x/">No id</a></td></tr>'
        jobs, listed, _ = rsf.parse_search_page(page, "tractorsupply")
        self.assertEqual(([j.id for j in jobs], listed), (["tractorsupply:1000"], 3))

    def test_a_job_listed_twice_counts_once(self):
        jobs, listed, _ = rsf.parse_search_page(results(0, 2, 2) + row(1), "sephora")
        self.assertEqual((len(jobs), listed), (2, 3))

    def test_a_search_with_no_postings_is_empty(self):
        self.assertEqual(rsf.parse_search_page(text("successfactors_none_search.html"), "sephora"), ([], 0, 0))

    def test_a_page_without_the_expected_layout_raises(self):
        for page in (text("successfactors_script_search.html"), "<html><body>Access Denied</body></html>", ""):
            with self.assertRaises(ValueError):
                rsf.parse_search_page(page, "sephora")


class Fetching(unittest.TestCase):
    def fetch(self, answer, key="sephora", terms=("software engineer",), pages=20, delay=0.4):
        session = Session(answer)
        with mock.patch.object(sources, "_session", lambda: session), mock.patch.object(rsf.time, "sleep") as sleep:
            jobs = rsf.READERS[key][1](list(terms), pages, delay)
        self.session, self.sleep = session, sleep
        return jobs, [url for _, url, _ in session.asked]

    def test_each_word_goes_to_the_sites_own_search_and_pages_follow_the_rows_listed(self):
        def answer(method, url, kw):
            start = int(url.rsplit("startrow=", 1)[1])
            return Resp(results(start, min(25, 60 - start), 60))
        jobs, asked = self.fetch(answer)
        self.assertEqual(len(jobs), 60)
        self.assertEqual(asked, [f"https://jobs.sephora.com/search/?q=software+engineer&locationsearch=US&startrow={n}" for n in (0, 25, 50)])
        self.assertTrue(all(kw == {"timeout": sources.TIMEOUT, "allow_redirects": False} for _, _, kw in self.session.asked))
        self.assertFalse(any("/services/" in u for u in asked))                        # the RSS feed is not allowed

    def test_the_united_states_is_asked_for_where_the_site_lists_the_world(self):
        for key, base, where in (("adidas", "https://jobs.adidas-group.com", "&locationsearch=United+States"),
                                 ("underarmour", "https://careers.underarmour.com", "&locationsearch=United+States"),
                                 ("sephora", "https://jobs.sephora.com", "&locationsearch=US"),
                                 ("tractorsupply", "https://www.tractorsupply.careers", "")):
            _, asked = self.fetch(lambda m, u, kw: Resp(results(0, 1, 1)), key=key, terms=("store manager",))
            self.assertEqual(asked, [f"{base}/search/?q=store+manager{where}&startrow=0"], key)

    def test_a_short_page_ends_a_word_and_the_page_limit_bounds_a_long_one(self):
        jobs, asked = self.fetch(lambda m, u, kw: Resp(results(0, 3, 3)), key="tractorsupply")
        self.assertEqual((len(jobs), len(asked)), (3, 1))
        jobs, asked = self.fetch(lambda m, u, kw: Resp(results(int(u.rsplit("=", 1)[1]), 25, 5000)), key="tractorsupply", terms=("a", "b"), pages=2)
        self.assertEqual(len(asked), 4)                                                # two pages for each word

    def test_a_page_that_reaches_the_stated_total_ends_a_word(self):
        # a full page, and the total says that was all
        jobs, asked = self.fetch(lambda m, u, kw: Resp(results(0, 25, 25)))
        self.assertEqual((len(jobs), len(asked)), (25, 1))

    def test_words_are_read_page_by_page_and_repeats_count_once(self):
        def answer(method, url, kw):
            return Resp(results(int(url.rsplit("=", 1)[1]), 25, 5000))
        jobs, asked = self.fetch(answer, terms=("alpha", "beta"), pages=2)
        self.assertEqual([("alpha" in u, u.rsplit("=", 1)[1]) for u in asked], [(True, "0"), (False, "0"), (True, "25"), (False, "25")])
        self.assertEqual(len(jobs), 50)                                                # the same rows came back for both words

    def test_words_are_lowercased_cut_to_the_search_box_and_not_repeated(self):
        _, asked = self.fetch(lambda m, u, kw: Resp(results(0, 1, 1)), key="tractorsupply", terms=("Cashier", "cashier", " ", "x" * 80))
        self.assertEqual(asked, ["https://www.tractorsupply.careers/search/?q=cashier&startrow=0", f"https://www.tractorsupply.careers/search/?q={'x' * 50}&startrow=0"])

    def test_the_request_cap_stops_a_run(self):
        with mock.patch.object(rsf, "MAX_REQUESTS", 5):
            jobs, asked = self.fetch(lambda m, u, kw: Resp(results(int(u.rsplit("=", 1)[1]), 25, 5000)), key="tractorsupply", terms=tuple("abcdefgh"), pages=3)
        self.assertEqual(len(asked), 5)
        self.assertEqual([u.split("q=")[1][0] for u in asked], list("abcde"))           # page 1 of the first words, not the deep pages of one
        self.assertEqual(len(jobs), 25)

    def test_the_pause_between_requests(self):
        self.fetch(lambda m, u, kw: Resp(results(0, 1, 1)), terms=("a", "b"), delay=0.9)
        self.assertEqual([c.args for c in self.sleep.call_args_list], [(0.9,), (0.9,)])
        with mock.patch.object(rsf, "CRAWL_DELAY", 3.0):
            self.fetch(lambda m, u, kw: Resp(results(0, 1, 1)), delay=0.4)
            self.assertEqual([c.args for c in self.sleep.call_args_list], [(3.0,)])

    def test_a_search_with_no_postings_gives_nothing(self):
        jobs, asked = self.fetch(lambda m, u, kw: Resp(text("successfactors_none_search.html")))
        self.assertEqual((jobs, len(asked)), ([], 1))

    def test_a_changed_or_blocked_page_raises_so_the_run_says_so(self):
        with self.assertRaises(ValueError):
            self.fetch(lambda m, u, kw: Resp(text("successfactors_script_search.html")), key="underarmour")
        with self.assertRaises(RuntimeError):
            self.fetch(lambda m, u, kw: Resp("", 403))

    def test_a_redirect_is_not_followed(self):
        with self.assertRaises(ValueError) as caught:
            self.fetch(lambda m, u, kw: Resp("", 302, {"location": "/errorpage/?errortype=Exception"}))
        self.assertIn("/errorpage/", str(caught.exception))


class JobPage(unittest.TestCase):
    def job(self, key, url):
        return sources.Job(f"{key}:1", key, "Company", "List title", "List place", url, posted="2026-01-01")

    def read(self, key, name, url="https://x.example/job/a/1/", answer=None):
        job = self.job(key, url)
        session = Session(answer or (lambda m, u, kw: Resp(text(name))))
        with mock.patch.object(sources, "_session", lambda: session):
            sources.DETAIL_FILLERS[key](job)
        self.asked = session.asked
        return job

    def test_tractor_supply(self):
        job = self.read("tractorsupply", "successfactors_tractorsupply_job.html")
        self.assertEqual((job.title, job.posted, job.location), ("Team Member - Cashier/Sales Associate/Retail", "2026-10-18", "Raceland (Mathews), LA, United States"))
        self.assertIn("Overall Job Summary", job.description)
        self.assertEqual((job.pay_min, job.pay_max), (None, None))
        self.assertEqual(self.asked[0][1:], ("https://x.example/job/a/1/", {"timeout": sources.TIMEOUT, "allow_redirects": False}))

    def test_under_armour_has_a_yearly_pay_range(self):
        job = self.read("underarmour", "successfactors_underarmour_job.html")
        self.assertEqual((job.title, job.posted, job.location), ("Lead, Application and AI Security Engineer", "2026-10-27", "Baltimore, MD, United States"))
        self.assertEqual((job.pay_min, job.pay_max), (155000, 200000))
        self.assertIn("$155,000.00-$200,000.00 USD", job.description)

    def test_sephora_states_an_hourly_rate_that_fill_pay_reads_later(self):
        job = self.read("sephora", "successfactors_sephora_job.html")
        self.assertEqual((job.title, job.posted, job.location), ("Seasonal Associate", "2026-10-10", "Miami, FL, United States"))
        self.assertIn("$18.00 - $20.50/hr", job.description)
        self.assertEqual((job.pay_min, job.pay_max), (None, None))
        sources.fill_pay(job)
        self.assertEqual((job.pay_min, job.pay_max), (18, 20.5))

    def test_adidas_gives_the_place_and_date_in_labelled_fields(self):
        job = self.read("adidas", "successfactors_adidas_job.html")
        self.assertEqual((job.title, job.posted, job.location), ("Lead", "2026-10-27", "Kansas City, KS, United States"))
        self.assertIn("Purpose:", job.description)

    def test_the_text_runs_to_the_posting_own_closing_tag(self):
        page = ('<span itemprop="title">T</span><span class="jobdescription"><p>One</p><span class="x"><b>two</b></span><div>Three</div>'
                '<span>four</span></span></span><div>after</div>')
        job = self.read("sephora", "", answer=lambda m, u, kw: Resp(page))
        self.assertIn("Three", job.description)
        self.assertIn("four", job.description)
        self.assertNotIn("after", job.description)
        self.assertEqual((job.location, job.posted), ("List place", "2026-01-01"))     # the page gives neither: what the list had stays

    def test_a_page_that_is_not_a_posting_raises(self):
        with self.assertRaises(ValueError):
            self.read("sephora", "", answer=lambda m, u, kw: Resp("<html><body>Access Denied</body></html>"))
        with self.assertRaises(ValueError):
            self.read("sephora", "", answer=lambda m, u, kw: Resp('<span class="jobdescription">  </span></span>'))
        with self.assertRaises(RuntimeError):
            self.read("sephora", "", answer=lambda m, u, kw: Resp("", 404))

    def test_a_redirect_is_not_followed(self):
        with self.assertRaises(ValueError):
            self.read("sephora", "", answer=lambda m, u, kw: Resp("", 302, {"location": "/errorpage/?errortype=Exception"}))


if __name__ == "__main__":
    unittest.main()
