"""Retailers on Oracle Recruiting Cloud (Albertsons and its banners, Kroger, Dollar General, Macy's, ...), read from saved copies of the real
answers of the career sites' public REST resource in tests/data/oracle_*.json."""
import copy
import json
import unittest
from pathlib import Path
from unittest import mock

import retail_oracle as ro
import retail_sources
import sources
from tests.retailtools import Resp, Session

DATA = Path(__file__).parent / "data"
load = lambda name: json.loads((DATA / f"oracle_{name}.json").read_text(encoding="utf-8"))

ALBERTSONS_HOST = "eofd.fa.us6.oraclecloud.com"


class Clock:
    """Time that only moves when the code sleeps, so a test sees how long a run would wait."""

    def __init__(self):
        self.now, self.slept = 1000.0, []

    def sleep(self, seconds):
        self.slept.append(round(seconds, 6))
        self.now += seconds

    def monotonic(self):
        return self.now


def row(n, title="Cashier", **extra):
    return {"Id": str(n), "Title": f"{title} {n}" if title else "", "PostedDate": "2026-11-02", "PrimaryLocation": "Charlotte, NC, United States", **extra}


def page(rows, total=None, held=None):
    """An answer to a search: `rows` listed, `total` found in all."""
    return {"items": [{"TotalJobsCount": len(rows) if total is None else total, "requisitionList": rows}], "count": 1}


def jobs_of(key, name):
    return ro.parse_oracle_list(load(name), key)[0]


class Registered(unittest.TestCase):
    def test_every_retailer_has_a_fetcher_and_a_posting_reader(self):
        self.assertEqual(set(ro.READERS), set(ro.ORACLE_SITES))
        for key, (label, fetch) in ro.READERS.items():
            self.assertTrue(callable(fetch), key)
            self.assertTrue(label, key)
            self.assertIs(sources.DETAIL_FILLERS[key], ro.fill_oracle_detail)
            self.assertEqual(key, key.lower())

    def test_a_banner_is_not_a_retailer(self):
        for banner in ("safeway", "vons", "jewelosco", "aerie", "potterybarn", "westelm", "restorationhardware"):
            self.assertNotIn(banner, ro.READERS)

    def test_the_retail_module_picks_them_up_once(self):
        keys = [k for k, *_ in retail_sources.RETAIL_SOURCES]
        self.assertEqual(len(keys), len(set(keys)))
        for key in ro.READERS:
            self.assertIn(key, keys)
            self.assertTrue(callable(getattr(retail_sources, f"fetch_{key}")))

    def test_the_sites_are_the_ones_in_the_survey(self):
        s = ro.ORACLE_SITES
        self.assertEqual(s["albertsons"][:2], (ALBERTSONS_HOST, "CX_1001"))
        self.assertEqual(s["kroger"][:2], ("eluq.fa.us2.oraclecloud.com", "CX_2001"))
        self.assertEqual(s["dollargeneral"][:2], ("ibxwjb.fa.ocs.oraclecloud.com", "CX_1"))
        self.assertEqual((s["macys"][0], s["macys"][1], s["bloomingdales"][0], s["bloomingdales"][1]),
                         ("ebwh.fa.us2.oraclecloud.com", "CX_1001", "ebwh.fa.us2.oraclecloud.com", "CX_1002"))
        for key, (host, site, label) in s.items():
            self.assertTrue(host.endswith("oraclecloud.com"), key)      # never a retailer's own site or a Radancy front end
            self.assertTrue(site and label, key)


class ListPages(unittest.TestCase):
    def test_albertsons_banner_pay_and_links(self):
        jobs = jobs_of("albertsons", "albertsons_list")
        self.assertEqual([j.id for j in jobs], ["albertsons:787292", "albertsons:787929", "albertsons:787947"])
        j = jobs[0]
        self.assertEqual((j.source, j.company, j.title, j.location, j.posted), ("albertsons", "Safeway", "Pharmacy Technician", "Glendale, AZ, United States", "2026-11-02"))
        self.assertEqual(j.url, "https://eofd.fa.us6.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/787292")
        # the list holds only a teaser: no text, and the way to read the whole posting later
        self.assertEqual(j.description, "")
        self.assertEqual(j.detail, {"api": "https://eofd.fa.us6.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
                                         "?expand=all&onlyData=true&finder=ById;Id=%22787292%22,siteNumber=CX_1001"})
        self.assertEqual((j.pay_min, j.pay_max), (None, None))
        # another banner of the same board, with the hourly pay the posting states
        self.assertEqual((jobs[1].company, jobs[1].pay_min, jobs[1].pay_max, jobs[1].pay_unit), ("Jewel-Osco", 17.05, 18.55, "hour"))

    def test_kroger_banner_names(self):
        jobs = jobs_of("kroger", "kroger_list")
        self.assertEqual([j.company for j in jobs], ["Fred Meyer Jewelers", "Ralphs", "Smith's Food and Drug"])
        self.assertEqual(jobs[0].id, "kroger:246646")
        self.assertEqual(jobs[0].url, "https://eluq.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_2001/job/246646")

    def test_a_remote_posting_says_so(self):
        jobs = jobs_of("dollargeneral", "dollargeneral_list")
        self.assertEqual((jobs[0].company, jobs[0].title, jobs[0].location, jobs[0].posted), ("Dollar General", "PRINCIPAL ARCHITECT ENGINEER", "TN, United States (Remote)", "2026-10-30"))
        self.assertEqual(jobs[1].location, "TN, United States")

    def test_every_place_of_a_posting_open_in_several_and_a_place_abroad(self):
        jobs = jobs_of("sherwinwilliams", "sherwin_list")
        self.assertEqual(jobs[0].location, "Wilayah Persekutuan Kuala Lumpur, Malaysia")
        self.assertEqual(jobs[1].location, "Lubbock, TX, United States; Midland, TX, United States; Odessa, TX, United States;"
                                           " San Angelo, TX, United States; Amarillo, TX, United States; Abilene, TX, United States")
        self.assertEqual(jobs[2].location, "Austin, TX, United States")             # the bare country beside the real place is dropped
        self.assertEqual(jobs[1].company, "Sherwin-Williams")

    def test_a_brand_in_the_title_names_the_company(self):
        wsi = jobs_of("williamssonoma", "williamssonoma_list")
        self.assertEqual([j.company for j in wsi], ["Pottery Barn", "West Elm", "Williams-Sonoma"])
        self.assertEqual(wsi[1].location.count(";"), 2)
        self.assertEqual(wsi[2].location, "United States (Remote)")
        aeo = jobs_of("americaneagle", "americaneagle_list")
        self.assertEqual([j.company for j in aeo], ["Aerie", "American Eagle Outfitters", "American Eagle Outfitters"])

    def test_a_posting_with_no_id_or_title_is_skipped_but_still_counts_as_a_row(self):
        data = page([{"Title": "No id"}, row(1, title=""), row(2), "junk"], total=40)
        jobs, total, held = ro.parse_oracle_list(data, "macys")
        self.assertEqual([j.id for j in jobs], ["macys:2"])
        self.assertEqual((total, held), (40, 4))
        self.assertEqual(jobs[0].company, "Macy's")

    def test_a_date_that_is_not_one_is_left_empty(self):
        self.assertIsNone(ro.parse_oracle_list(page([row(1, PostedDate="soon")]), "macys")[0][0].posted)
        self.assertIsNone(ro.parse_oracle_list(page([row(1, PostedDate=None)]), "macys")[0][0].posted)

    def test_pay_fields(self):
        flex = lambda *fields: {"requisitionFlexFields": [{"Prompt": p, "Value": v} for p, v in fields]}
        self.assertEqual(ro._flex_pay(flex(("Expected Pay", "$150,000-$160,000"))), (150000, 160000, "year"))
        self.assertEqual(ro._flex_pay(flex(("Minimum Pay Rate", "17.5"), ("Maximum Pay Rate", "$24.00"))), (17.5, 24, "hour"))
        self.assertEqual(ro._flex_pay(flex(("Banner", "Safeway"), ("Minimum Pay Rate", None))), (None, None, "year"))
        self.assertEqual(ro._flex_pay(flex(("Expected Pay", "Competitive"))), (None, None, "year"))
        self.assertEqual(ro._flex_pay(flex(("Expected Pay", "401 and 402"))), (None, None, "year"))      # not a rate
        self.assertEqual(ro._flex_pay(flex(("Expected Pay", "$17 - $19 an hour, plus 401k"))), (17, 19, "hour"))
        self.assertEqual(ro._flex_pay({}), (None, None, "year"))

    def test_an_answer_that_is_not_a_search_is_an_error(self):
        for bad in ({}, {"items": []}, {"items": [{"TotalJobsCount": 3}]}, {"items": ["x"]}, {"title": "Access Denied"}):
            with self.assertRaises(ValueError, msg=bad):
                ro.parse_oracle_list(bad, "macys")

    def test_a_search_with_no_hits_is_empty_not_an_error(self):
        self.assertEqual(ro.parse_oracle_list(page([], 0), "macys"), ([], 0, 0))


class Urls(unittest.TestCase):
    def test_the_search_the_career_page_sends(self):
        self.assertEqual(
            ro.list_url("kroger", "store manager", 0),
            "https://eluq.fa.us2.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true"
            "&expand=requisitionList.secondaryLocations,requisitionList.requisitionFlexFields"
            "&finder=findReqs;siteNumber=CX_2001,limit=200,offset=0,keyword=%22store%20manager%22,sortBy=RELEVANCY")
        self.assertIn("offset=400,", ro.list_url("kroger", "cashier", 400))

    def test_a_word_cannot_break_the_finder(self):
        url = ro.list_url("macys", 'a,b;c="d" (e)', 0)
        self.assertIn("keyword=%22a%20b%20c%20d%20e%22,sortBy", url)
        self.assertIn("keyword=%22q%26a%22,", ro.list_url("macys", "q&a", 0))

    def test_the_whole_posting(self):
        self.assertEqual(ro.detail_url("staples", "77035"),
                         "https://fa-exhh-saasfaprod1.fa.ocs.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
                         "?expand=all&onlyData=true&finder=ById;Id=%2277035%22,siteNumber=StaplesInc")
        self.assertEqual(ro.job_url("warbyparker", "REQ_1"), "https://fa-evdi-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/REQ_1")


class Fetching(unittest.TestCase):
    def setUp(self):
        ro._last_request.clear()
        self.clock = Clock()

    def fetch(self, key, answer, terms=("software engineer",), pages=20, delay=0.4):
        session = Session(answer)
        with mock.patch.object(sources, "_session", lambda: session), mock.patch.object(ro, "time", self.clock):
            return ro.READERS[key][1](list(terms), pages, delay), [(url, kw) for _, url, kw in session.asked]

    def asked_terms(self, asked):
        return [(u.split("keyword=%22")[1].split("%22")[0], int(u.split("offset=")[1].split(",")[0])) for u, _ in asked]

    def test_paging_reads_on_from_however_many_rows_a_page_held(self):
        def answer(method, url, kw):
            start = int(url.split("offset=")[1].split(",")[0])
            return Resp(page([row(start + i, "Software Engineer") for i in range(min(4, 10 - start))], 10))
        jobs, asked = self.fetch("dollargeneral", answer)
        self.assertEqual([j.id for j in jobs], [f"dollargeneral:{n}" for n in range(10)])
        self.assertEqual(self.asked_terms(asked), [("software%20engineer", 0), ("software%20engineer", 4), ("software%20engineer", 8)])
        self.assertTrue(all(kw == {"timeout": sources.TIMEOUT} for _, kw in asked))

    def test_a_short_first_page_is_the_only_page(self):
        jobs, asked = self.fetch("macys", lambda m, u, kw: Resp(page([row(1, "Software Engineer"), row(2, "Software Engineer")])))
        self.assertEqual((len(jobs), len(asked)), (2, 1))

    def test_the_page_limit_is_per_word_and_the_words_go_breadth_first(self):
        always = lambda m, u, kw: Resp(page([row(int(u.split("offset=")[1].split(",")[0]) + i, "Cashier Stocker") for i in range(5)], 500))
        jobs, asked = self.fetch("kroger", always, ("cashier", "stocker"), pages=2)
        self.assertEqual(self.asked_terms(asked), [("cashier", 0), ("stocker", 0), ("cashier", 5), ("stocker", 5)])
        self.assertEqual(len(jobs), 10)         # the same ten come back for the second word and count once

    def test_words_are_cleaned_and_not_asked_twice(self):
        jobs, asked = self.fetch("kroger", lambda m, u, kw: Resp(page([])), (" Cashier ", "cashier", "", "CASHIER"))
        self.assertEqual(self.asked_terms(asked), [("cashier", 0)])
        self.assertEqual(jobs, [])

    def test_a_word_stops_when_a_page_holds_no_title_with_it(self):
        # ordered by relevance: after a page of text-only matches, the next pages are worse
        calls = []

        def answer(m, u, kw):
            calls.append(u)
            return Resp(page([row(i, "Warehouse Associate") for i in range(3)], 900))
        self.fetch("kroger", answer, ("pharmacy technician",))
        self.assertEqual(len(calls), 1)
        calls.clear()
        self.fetch("kroger", lambda m, u, kw: (calls.append(u), Resp(page([row(1, "Pharmacy Tech")], 900)))[1], ("pharmacy technician",), pages=3)
        self.assertEqual(len(calls), 3)

    def test_the_request_cap_cuts_the_last_pages_not_the_last_words(self):
        always = lambda m, u, kw: Resp(page([row(int(u.split("offset=")[1].split(",")[0]) + i, "Cashier") for i in range(5)], 500))
        with mock.patch.dict(ro.MAX_REQUESTS, {"kroger": 5}):
            jobs, asked = self.fetch("kroger", always, ("cashier", "a cashier", "b cashier"), pages=20)
        self.assertEqual(self.asked_terms(asked), [("cashier", 0), ("a%20cashier", 0), ("b%20cashier", 0), ("cashier", 5), ("a%20cashier", 5)])
        with mock.patch.dict(ro.MAX_REQUESTS, {}, clear=True), mock.patch.object(ro, "MAX_REQUESTS_DEFAULT", 3):
            jobs, asked = self.fetch("kroger", always, ("cashier",), pages=20)
        self.assertEqual(len(asked), 3)

    def test_the_caps_in_the_module(self):
        self.assertEqual(ro.MAX_REQUESTS["albertsons"], 60)
        self.assertGreaterEqual(ro.MAX_REQUESTS_DEFAULT, 100)
        self.assertEqual(ro.PAGE_SIZE, 200)

    def test_the_delay_between_requests(self):
        always = lambda m, u, kw: Resp(page([row(1, "Cashier Stocker"), row(2, "Cashier Stocker")], 500))
        self.fetch("kroger", always, ("cashier", "stocker"), pages=2, delay=0.7)
        self.assertEqual(self.clock.slept, [0.7, 0.7, 0.7])          # none before the first request

    def test_albertsons_waits_its_crawl_delay_whatever_delay_is_asked(self):
        self.assertEqual(ro.CRAWL_DELAY[ALBERTSONS_HOST], 10.0)
        always = lambda m, u, kw: Resp(page([row(1, "Cashier")], 500))
        jobs, asked = self.fetch("albertsons", always, ("cashier", "stocker", "pharmacy"), pages=1, delay=0.4)
        self.assertTrue(all(u.startswith(f"https://{ALBERTSONS_HOST}/") and "siteNumber=CX_1001," in u for u, _ in asked))
        self.assertEqual(self.clock.slept, [10.0, 10.0])
        self.clock.slept.clear()
        self.fetch("kroger", always, ("cashier", "stocker"), pages=1, delay=0.4)
        self.assertEqual(self.clock.slept, [0.4])                    # other sites keep the configured delay

    def test_two_sites_on_one_host_share_the_delay(self):
        always = lambda m, u, kw: Resp(page([row(1, "Manager")]))
        self.fetch("macys", always, ("manager",))
        self.fetch("bloomingdales", always, ("manager",))
        self.assertEqual(self.clock.slept, [0.4])

    def test_a_page_that_is_not_the_site_raises_so_the_run_says_so(self):
        with self.assertRaises(ValueError):
            self.fetch("macys", lambda m, u, kw: Resp("<html>Access Denied</html>"))
        with self.assertRaises(ValueError):
            self.fetch("macys", lambda m, u, kw: Resp({"items": [{"Message": "changed"}]}))
        with self.assertRaises(ValueError):
            self.fetch("macys", lambda m, u, kw: Resp("[1, 2]"))
        with self.assertRaises(RuntimeError):
            self.fetch("macys", lambda m, u, kw: Resp("", 403))

    def test_every_retailer_asks_its_own_host(self):
        for key, (host, site, _) in ro.ORACLE_SITES.items():
            jobs, asked = self.fetch(key, lambda m, u, kw: Resp(page([row(1, "Software Engineer")])))
            self.assertEqual(len(asked), 1, key)
            self.assertTrue(asked[0][0].startswith(f"https://{host}/hcmRestApi/"), key)
            self.assertIn(f"siteNumber={site},", asked[0][0])
            self.assertEqual((jobs[0].id, jobs[0].source), (f"{key}:1", key))
            self.assertTrue(jobs[0].url.startswith(f"https://{host}/hcmUI/"), key)


class WholePosting(unittest.TestCase):
    def setUp(self):
        ro._last_request.clear()
        self.clock = Clock()

    def read(self, job, body, status=200):
        session = Session(lambda m, u, kw: Resp(body, status))
        with mock.patch.object(sources, "_session", lambda: session), mock.patch.object(ro, "time", self.clock):
            sources.fill_detail(job)
        return session.asked

    def job(self, key, name):
        return jobs_of(key, name)[0]

    def test_text_title_place_date_and_the_pay_field(self):
        j = self.job("dollargeneral", "dollargeneral_list")
        detail = load("dollargeneral_detail")
        detail["items"][0]["CorporateDescriptionStr"] = "<div>Work Where You Matter</div>"
        asked = self.read(j, detail)
        self.assertEqual([url for _, url, _ in asked], [j.detail["api"]])
        self.assertIn("senior technical leadership for architecture", j.description)
        self.assertIn("Job Responsibility", j.description)
        self.assertTrue(j.description.rstrip().endswith("Expected Pay: $150,000-$160,000"))
        self.assertNotIn("<", j.description)
        self.assertNotIn("Work Where You Matter", j.description)          # the company boilerplate is left out
        self.assertEqual((j.pay_min, j.pay_max, j.pay_unit), (150000, 160000, "year"))
        self.assertEqual((j.title, j.posted, j.location, j.company), ("PRINCIPAL ARCHITECT ENGINEER", "2026-10-30", "TN, United States (Remote)", "Dollar General"))

    def test_the_banner_comes_with_the_posting(self):
        j = self.job("albertsons", "albertsons_list")
        j.company = "Albertsons"
        self.read(j, load("albertsons_detail"))
        self.assertEqual((j.company, j.posted, j.title, j.location), ("Safeway", "2026-11-02", "Pharmacy Technician", "Glendale, AZ, United States"))
        self.assertGreater(len(j.description), 1000)
        self.assertEqual(self.clock.slept, [])          # a first request waits for nothing

    def test_a_pay_range_in_the_text_is_used_and_hourly_pay_stays_for_fill_pay(self):
        detail = load("williamssonoma_detail")
        j = jobs_of("williamssonoma", "williamssonoma_list")[2]
        self.read(j, detail)
        self.assertEqual((j.title, j.location, j.posted, j.company), ("Asset Protection Investigator", "United States (Remote)", "2026-10-05", "Williams-Sonoma"))
        self.assertGreater(len(j.description), 500)
        self.assertEqual(j.pay_unit, "year")
        text = copy.deepcopy(detail)
        text["items"][0]["ExternalResponsibilitiesStr"] = "<p>Pay range: $80,000 - $95,000 a year.</p>"
        self.read(j, text)
        self.assertEqual((j.pay_min, j.pay_max), (80000, 95000))
        self.assertTrue(j.description.rstrip().endswith("Pay range: $80,000 - $95,000 a year."))

    def test_hourly_pay_from_the_pay_fields(self):
        detail = load("albertsons_detail")
        detail["items"][0]["requisitionFlexFields"] += [{"Prompt": "Minimum Pay Rate", "Value": "16.25"}, {"Prompt": "Maximum Pay Rate", "Value": "18"}]
        j = self.job("albertsons", "albertsons_list")
        self.read(j, detail)
        self.assertEqual((j.pay_min, j.pay_max, j.pay_unit), (16.25, 18, "hour"))

    def test_a_posting_without_pay_keeps_the_pay_the_list_gave(self):
        j = jobs_of("albertsons", "albertsons_list")[1]
        self.assertEqual(j.pay_unit, "hour")
        self.read(j, load("albertsons_detail"))
        self.assertEqual((j.pay_min, j.pay_max, j.pay_unit), (17.05, 18.55, "hour"))

    def test_albertsons_waits_its_crawl_delay_between_postings(self):
        a, b = jobs_of("albertsons", "albertsons_list")[:2]
        self.read(a, load("albertsons_detail"))
        self.read(b, load("albertsons_detail"))
        self.assertEqual(self.clock.slept, [10.0])

    def test_a_page_that_is_not_the_posting_is_an_error_so_it_is_tried_again_next_time(self):
        for body in ({"items": []}, {"items": [{"Id": "1", "Title": "x", "ExternalDescriptionStr": ""}]}, {"items": [{"ExternalDescriptionStr": None}]},
                     {"count": 0}, "<html>Access Denied</html>", "[]"):
            j = self.job("kroger", "kroger_list")
            with self.assertRaises(ValueError, msg=body):
                self.read(j, body)
            self.assertEqual(j.description, "")
        with self.assertRaises(RuntimeError):
            self.read(self.job("kroger", "kroger_list"), "", 404)       # a posting that was taken down

    def test_a_job_stored_without_its_address_is_looked_up_by_id(self):
        j = sources.Job("macys:94314", "macys", "Macy's", "Manager", "Atlanta, GA, United States", "https://x", detail={"api": "x"})
        j.detail = {"x": 1}
        asked = self.read(j, {"items": [{"Title": "Manager, Business Administration", "ExternalDescriptionStr": "<p>Lead the team.</p>", "PrimaryLocation": "Atlanta, GA, United States"}]})
        self.assertEqual(asked[0][1], ro.detail_url("macys", "94314"))
        self.assertEqual((j.title, j.description, j.company), ("Manager, Business Administration", "Lead the team.", "Macy's"))


if __name__ == "__main__":
    unittest.main()
