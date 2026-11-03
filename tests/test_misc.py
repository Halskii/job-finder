"""The one-off retailer readers (Ahold Delhaize USA, Dillard's, Victoria's Secret, Hobby Lobby, Harbor Freight, Burlington),
read from saved copies of the real answers in tests/data/misc_*."""
import copy
import json
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

import retail_misc as rm
import retail_sources
import sources
from tests.retailtools import Resp, Session

DATA = Path(__file__).parent / "data"
text = lambda name: (DATA / name).read_text(encoding="utf-8")
load = lambda name: json.loads(text(name))


def fetching(fn, answer, *args):
    """Run a fetch function with a session that answers from `answer`; returns (jobs, the session, the sleep mock)."""
    session = Session(answer)
    with mock.patch.object(sources, "_session", lambda: session), mock.patch.object(rm.time, "sleep") as sleep:
        return fn(*args), session, sleep


class Registered(unittest.TestCase):
    def test_every_reader_is_registered_with_a_fetcher(self):
        self.assertEqual(set(rm.READERS), {"ahold", "dillards", "victoriassecret", "hobbylobby", "harborfreight"})   # Burlington is built but off
        for key, (label, fetch) in rm.READERS.items():
            self.assertTrue(label and callable(fetch), key)
        keys = [k for k, *_ in retail_sources.RETAIL_SOURCES]
        self.assertEqual(len(keys), len(set(keys)))                  # no key is shared with another module
        self.assertTrue(set(rm.READERS) <= set(keys))

    def test_the_readers_that_leave_the_text_out_have_a_job_page_reader(self):
        for key in ("ahold", "harborfreight"):
            self.assertIn(key, sources.DETAIL_FILLERS)
        for key in ("dillards", "victoriassecret", "hobbylobby", "burlington"):      # the list holds the whole text
            self.assertNotIn(key, sources.DETAIL_FILLERS)

    def test_the_cap_and_the_wait_between_requests(self):
        pace = rm._Pace(2, 0.7)
        with mock.patch.object(rm.time, "sleep") as sleep:
            self.assertEqual([pace.more(), pace.more(), pace.more()], [True, True, False])
        sleep.assert_called_once_with(0.7)


def ahold_page(n, total, start=0):
    return {"tool": "search_jobs", "totalCount": total, "page": 1, "pageSize": 100, "results": [
        {"requisitionId": f"{start + i}_external_USA-MA-Quincy", "title": f"Store Manager {start + i}", "location": "Quincy, MA, USA", "datePosted": "2026-10-20",
         "description": "Category..."} for i in range(n)]}


class Ahold(unittest.TestCase):
    def test_search_results(self):
        jobs = rm.parse_ahold_results(load("misc_ahold_search.json"), "stopandshop")
        self.assertEqual(len(jobs), 3)
        j = jobs[0]
        self.assertEqual((j.id, j.source, j.company, j.title, j.location, j.posted), (
            "ahold:559089_external_USA-MA-Hudson", "ahold", "Stop & Shop", "Store Manager", "Hudson, MA", "2026-10-20"))
        self.assertEqual(j.url, "https://stopandshop.careerswithus.com/job/559089_external_USA-MA-Hudson")
        # the list gives only the start of a text: the web app reads the whole posting later
        self.assertEqual(j.description, "")
        self.assertEqual(j.detail, {"api": "https://stopandshop.careerswithus.com/api/mcp/jobs?tool=get_job&jobId=559089_external_USA-MA-Hudson"})

    def test_the_company_is_the_banner(self):
        data = ahold_page(1, 1)
        self.assertEqual(rm.parse_ahold_results(data, "foodlion")[0].company, "Food Lion")
        self.assertEqual(rm.parse_ahold_results(data, "thegiantcompany")[0].company, "The GIANT Company")

    def test_a_job_with_no_id_or_title_is_skipped_and_a_changed_answer_is_an_error(self):
        data = {"results": [{"title": "No id"}, {"requisitionId": "1_x", "title": ""}, {"requisitionId": "2_x", "title": "Cashier"}]}
        self.assertEqual([j.id for j in rm.parse_ahold_results(data, "foodlion")], ["ahold:2_x"])
        with self.assertRaises(ValueError):
            rm.parse_ahold_results({"error": "x"}, "foodlion")

    def test_every_banner_is_asked_for_each_word_a_hundred_a_page(self):
        def answer(method, url, kw):
            q = parse_qs(urlparse(url).query)
            if urlparse(url).netloc == "foodlion.careerswithus.com":
                return Resp(ahold_page(100, 130) if q["page"] == ["1"] else ahold_page(30, 130, start=100))
            return Resp(ahold_page(0, 0))
        jobs, session, sleep = fetching(rm.fetch_ahold, answer, ["store manager", "pharmacist"], 8, 0.4)
        urls = [u for _, u, _ in session.asked]
        self.assertEqual(urls[:3], [
            "https://foodlion.careerswithus.com/api/mcp/jobs?tool=search_jobs&search=store%20manager&page=1&pageSize=100",
            "https://foodlion.careerswithus.com/api/mcp/jobs?tool=search_jobs&search=store%20manager&page=2&pageSize=100",
            "https://giantfood.careerswithus.com/api/mcp/jobs?tool=search_jobs&search=store%20manager&page=1&pageSize=100"])
        self.assertEqual(len(urls), 2 * (2 + 4))                       # Food Lion has two pages, the other four banners one each
        self.assertEqual(urls[6].split("search=")[1].split("&")[0], "pharmacist")
        self.assertEqual(len(jobs), 130)                               # the same 130 come back for the second word and count once
        self.assertEqual({c.args[0] for c in sleep.call_args_list}, {0.4})
        self.assertEqual(len(sleep.call_args_list), len(urls) - 1)

    def test_the_page_limit(self):
        jobs, session, _ = fetching(rm.fetch_ahold, lambda m, u, kw: Resp(ahold_page(100, 5000)), ["x"], 2, 0)
        self.assertEqual(len(session.asked), 5 * 2)

    def test_the_request_cap_stops_a_long_run(self):
        with mock.patch.object(rm, "AHOLD_MAX_REQUESTS", 7):
            jobs, session, _ = fetching(rm.fetch_ahold, lambda m, u, kw: Resp(ahold_page(1, 1)), [f"w{n}" for n in range(30)], 8, 0)
        self.assertEqual(len(session.asked), 7)
        self.assertEqual({u.split("search=")[1].split("&")[0] for _, u, _ in session.asked}, {"w0", "w1"})     # the first words, every banner

    def test_a_blocked_or_changed_site_is_an_error(self):
        with self.assertRaises(ValueError):
            fetching(rm.fetch_ahold, lambda m, u, kw: Resp("<html>Access Denied</html>"), ["x"], 1, 0)
        with self.assertRaises(RuntimeError):
            fetching(rm.fetch_ahold, lambda m, u, kw: Resp("", 503), ["x"], 1, 0)

    def job(self):
        return sources.Job("ahold:560976_external_USA-VA-Quinton", "ahold", "Food Lion", "PT Pharmacy Technician", "Quinton, VA", "https://foodlion.careerswithus.com/job/560976",
                           detail={"api": "https://foodlion.careerswithus.com/api/mcp/jobs?tool=get_job&jobId=560976_external_USA-VA-Quinton"})

    def test_the_whole_posting_with_an_hourly_rate(self):
        j = self.job()
        session = Session(lambda m, u, kw: Resp(text("misc_ahold_job.json")))
        with mock.patch.object(sources, "_session", lambda: session):
            sources.fill_detail(j)
        self.assertEqual(session.asked[0][1], j.detail["api"])
        self.assertEqual((j.title, j.posted, j.location), ("PT Pharmacy Technician Certified", "2026-10-26", "Quinton, VA"))
        self.assertIn("DUTIES AND RESPONSIBILITIES", j.description)
        self.assertNotIn("<", j.description)
        self.assertTrue(j.description.endswith("Pay: $16.60 - $24.90 per hour"))
        sources.fill_pay(j)                                            # the web app does this after the posting is read
        self.assertEqual((j.pay_min, j.pay_max, j.pay_unit), (16.6, 24.9, "hour"))

    def test_a_yearly_range_goes_straight_onto_the_job(self):
        data = load("misc_ahold_job.json")
        data["result"].update(salaryMin=85000, salaryMax=120000, salRateType="Annual (YEARLY)")
        j = self.job()
        with mock.patch.object(sources, "_session", lambda: Session(lambda m, u, kw: Resp(data))):
            rm.fill_ahold_detail(j)
        self.assertEqual((j.pay_min, j.pay_max), (85000, 120000))
        self.assertNotIn("per year", j.description)

    def test_a_page_that_is_not_the_posting_is_an_error(self):
        for body in ({"tool": "get_job", "result": None}, {"result": {"title": "x", "description": ""}}, "<html>Access Denied</html>"):
            with mock.patch.object(sources, "_session", lambda: Session(lambda m, u, kw: Resp(body))), self.assertRaises(ValueError):
                rm.fill_ahold_detail(self.job())
        with mock.patch.object(sources, "_session", lambda: Session(lambda m, u, kw: Resp("", 404))), self.assertRaises(RuntimeError):
            rm.fill_ahold_detail(self.job())      # a posting that was taken down
        j = self.job()
        j.detail = {"api": "https://example.com/api/mcp/jobs?tool=get_job&jobId=1"}
        with self.assertRaises(ValueError):
            rm.fill_ahold_detail(j)


class Dillards(unittest.TestCase):
    def test_a_view_with_the_whole_text(self):
        jobs = rm.parse_dillards(load("misc_dillards_it.json"))
        self.assertEqual([j.id for j in jobs], ["dillards:6ae0dc5e4ef98d3965bc1304", "dillards:6ae4f96fe8217d3eaa840432"])
        j = jobs[0]
        self.assertEqual((j.source, j.company, j.title, j.location, j.posted), ("dillards", "Dillard's", "Senior Developer", "Little Rock, AR", "2026-10-27"))
        self.assertEqual(j.url, "https://job-boards.greenhouse.io/dillards/jobs/5103839007")       # the page the site itself sends a person to
        self.assertIn("## Responsibilities", j.description)
        self.assertIn("## Required skills", j.description)
        self.assertNotIn("\r", j.description)
        self.assertEqual(j.detail, {})                                 # nothing more to read
        self.assertEqual(jobs[1].url, "https://careers.dillards.com/Careers/")         # no posting page of its own: the careers page

    def test_a_job_with_no_id_or_title_is_skipped_and_a_changed_answer_is_an_error(self):
        self.assertEqual(rm.parse_dillards({"data": [{"title": "x"}, {"id": "1", "title": ""}]}), [])
        with self.assertRaises(ValueError):
            rm.parse_dillards({"message": "x"})

    def test_pay_in_the_text_is_read(self):
        data = {"data": [{"id": "9", "title": "Buyer", "description": "Pay: $90,000 - $110,000 a year", "locationInfo": {"city": "Mesa", "state": "AZ"}}]}
        self.assertEqual((rm.parse_dillards(data)[0].pay_min, rm.parse_dillards(data)[0].pay_max), (90000, 110000))

    def test_each_view_is_read_once_and_repeats_count_once(self):
        views = {"All": load("misc_dillards_merch.json"), "Information Technology": load("misc_dillards_it.json"),
                 "Merchandising": load("misc_dillards_merch.json"), "Call Center": {"data": []}}
        jobs, session, _ = fetching(rm.fetch_dillards, lambda m, u, kw: Resp(views[parse_qs(urlparse(u).query)["filter"][0]]), ["software engineer"], 1, 0.4)
        self.assertEqual([u for _, u, _ in session.asked], [
            "https://careers.dillards.com/Careers/api/jobs/list?filter=All", "https://careers.dillards.com/Careers/api/jobs/list?filter=Information+Technology",
            "https://careers.dillards.com/Careers/api/jobs/list?filter=Merchandising", "https://careers.dillards.com/Careers/api/jobs/list?filter=Call+Center"])
        self.assertEqual(len(jobs), 3)

    def test_a_refused_request_says_why(self):
        with self.assertRaisesRegex(RuntimeError, "user agent"):
            fetching(rm.fetch_dillards, lambda m, u, kw: Resp("<H1>Access Denied</H1>", 403), [], 1, 0)
        with self.assertRaises(ValueError):
            fetching(rm.fetch_dillards, lambda m, u, kw: Resp("<html>nothing</html>"), [], 1, 0)


class VictoriasSecret(unittest.TestCase):
    def test_search_results(self):
        jobs = rm.parse_vs_results(load("misc_vs_search.json"))
        self.assertEqual(len(jobs), 4)
        j = jobs[0]
        self.assertEqual((j.id, j.source, j.company, j.title, j.location, j.posted), (
            "victoriassecret:23670279", "victoriassecret", "Victoria's Secret", "Industrial Engineer II", "Reynoldsburg, OH", "2026-08-05"))
        self.assertEqual(j.url, "https://careers.victoriassecret.com/en/job/23670279/industrial-engineer-ii-reynoldsburg-oh/")
        self.assertIn("Industrial Engineering", j.description)
        self.assertNotIn("<", j.description)
        self.assertEqual(j.detail, {})                                 # the list holds the whole text
        self.assertEqual((jobs[1].pay_min, jobs[1].pay_max), (90800, 109000))
        self.assertEqual(jobs[1].location, "Scottsdale, AZ")

    def test_an_hourly_rate_is_added_to_the_text_for_fill_pay(self):
        cashier = rm.parse_vs_results(load("misc_vs_search.json"))[3]
        self.assertTrue(cashier.description.endswith("Pay: $15.90 - $20.89 per hour"))
        self.assertEqual((cashier.pay_min, cashier.pay_max), (None, None))
        sources.fill_pay(cashier)
        self.assertEqual((cashier.pay_min, cashier.pay_max, cashier.pay_unit), (15.9, 20.89, "hour"))

    def test_a_remote_job_says_so(self):
        data = copy.deepcopy(load("misc_vs_search.json"))
        data["searchResults"][0]["job"]["location_type"] = "Remote"
        self.assertEqual(rm.parse_vs_results(data)[0].location, "Reynoldsburg, OH (Remote)")

    def test_a_posting_with_no_page_or_title_is_skipped_and_a_changed_answer_is_an_error(self):
        data = copy.deepcopy(load("misc_vs_search.json"))
        data["searchResults"][0]["job"]["url"] = "https://elsewhere.example/x"
        data["searchResults"][1]["job"]["title"] = ""
        self.assertEqual(len(rm.parse_vs_results(data)), 2)
        with self.assertRaises(ValueError):
            rm.parse_vs_results({"error": "x"})

    def test_pages_follow_the_offset_and_stop_at_the_count(self):
        def hit(n):
            return {"job": {"id": n, "title": f"Store Manager {n}", "url": f"https://careers.victoriassecret.com/en/job/{n}/x/", "primary_city": "Austin", "primary_state": "TX",
                            "description": "<p>Run the store.</p>", "open_date": "2026-10-01T10:00:00"}}

        def answer(method, url, kw):
            offset = int(parse_qs(urlparse(url).query)["offset"][0])
            return Resp({"totalHits": 150, "searchResults": [hit(offset + i + 1) for i in range(100 if offset == 0 else 50)]})
        jobs, session, sleep = fetching(rm.fetch_victoriassecret, answer, ["store manager"], 8, 0.4)
        self.assertEqual([u for _, u, _ in session.asked], [
            "https://jobsapi-google.m-cloud.io/api/job/search?pageSize=100&offset=0&companyName=companies%2F382ab4db-03e8-40cc-b413-51539aca9954&query=store+manager",
            "https://jobsapi-google.m-cloud.io/api/job/search?pageSize=100&offset=100&companyName=companies%2F382ab4db-03e8-40cc-b413-51539aca9954&query=store+manager"])
        self.assertEqual(len(jobs), 150)
        sleep.assert_called_once_with(0.4)

    def test_the_page_limit_the_cap_and_repeats_across_words(self):
        answer = lambda m, u, kw: Resp({"totalHits": 9999, "searchResults": [{"job": {"id": 1, "title": "Cashier", "url": "https://careers.victoriassecret.com/en/job/1/x/"}}] * 100})
        jobs, session, _ = fetching(rm.fetch_victoriassecret, answer, ["a", "b"], 2, 0)
        self.assertEqual((len(session.asked), len(jobs)), (4, 1))
        with mock.patch.object(rm, "VICTORIASSECRET_MAX_REQUESTS", 3):
            jobs, session, _ = fetching(rm.fetch_victoriassecret, answer, ["a", "b", "c"], 8, 0)
        self.assertEqual(len(session.asked), 3)

    def test_a_blocked_site_is_an_error(self):
        with self.assertRaises(ValueError):
            fetching(rm.fetch_victoriassecret, lambda m, u, kw: Resp("<html>Access Denied</html>"), ["x"], 1, 0)
        with self.assertRaises(RuntimeError):
            fetching(rm.fetch_victoriassecret, lambda m, u, kw: Resp("", 403), ["x"], 1, 0)


class BrassRing(unittest.TestCase):
    def test_hobby_lobby_has_the_whole_text_in_the_list(self):
        jobs = rm.parse_brassring(load("misc_hobbylobby_search.json"), "hobbylobby", "5295")
        self.assertEqual(len(jobs), 2)
        j = jobs[0]
        self.assertEqual((j.id, j.source, j.company, j.title, j.posted), ("hobbylobby:867143", "hobbylobby", "Hobby Lobby", "Senior Assistant Manager", "2026-11-02"))
        self.assertEqual(j.location, "Redding, California, United States")
        self.assertEqual(j.url, "https://sjobs.brassring.com/TGnewUI/Search/home/HomeWithPreLoad?partnerid=25879&siteid=5295&PageType=JobDetails&jobid=867143")
        self.assertGreater(len(j.description), 300)
        self.assertNotIn("<", j.description)
        self.assertEqual(j.detail, {})
        self.assertEqual((j.pay_min, j.pay_max), (70261, 76971))

    def test_harbor_freight_leaves_the_text_to_the_job_page(self):
        jobs = rm.parse_brassring(load("misc_harborfreight_6660.json"), "harborfreight", "6660")
        self.assertEqual([j.id for j in jobs], ["harborfreight:1438996", "harborfreight:1437541", "harborfreight:1436579"])
        j = jobs[0]
        self.assertEqual((j.company, j.location, j.posted), ("Harbor Freight Tools", "Piedmont, SC, United States", "2026-10-28"))
        self.assertEqual((j.description, j.detail), ("", {"api": j.url}))
        self.assertEqual(jobs[1].location, "Calabasas, CA (Remote)")      # the title says Remote
        self.assertEqual(jobs[1].title, "Data Analytics Engineer- Remote")

    def test_a_store_gives_a_city_in_capitals_and_a_state_code(self):
        jobs = rm.parse_brassring(load("misc_harborfreight_6657.json"), "harborfreight", "6657")
        self.assertEqual([j.location for j in jobs], ["Auburndale, FL", "Cumming, GA, United States"])

    def test_html_in_a_title_is_decoded(self):
        data = {"Jobs": {"Job": [{"Link": "", "Questions": [{"QuestionName": "reqid", "Value": "5"}, {"QuestionName": "jobtitle", "Value": "Testing Technician III (HVAC, Plumbing, &amp; Electrical)"}]}]}}
        j = rm.parse_brassring(data, "harborfreight", "6660")[0]
        self.assertEqual(j.title, "Testing Technician III (HVAC, Plumbing, & Electrical)")
        self.assertEqual(j.url, "https://sjobs.brassring.com/TGnewUI/Search/home/HomeWithPreLoad?partnerid=26281&siteid=6660&PageType=JobDetails&jobid=5")

    def test_a_job_with_no_id_or_title_is_skipped_and_a_changed_answer_is_an_error(self):
        data = {"Jobs": {"Job": [{"Questions": [{"QuestionName": "jobtitle", "Value": "No id"}]}, {"Questions": [{"QuestionName": "reqid", "Value": "1"}]}]}}
        self.assertEqual(rm.parse_brassring(data, "hobbylobby", "5295"), [])
        for bad in ({}, {"Jobs": None}, {"Jobs": {"Job": "x"}}):
            with self.assertRaises(ValueError):
                rm.parse_brassring(bad, "hobbylobby", "5295")

    def page(self, n, total, start=0):
        return {"JobsCount": total, "Jobs": {"Job": [{"Link": "", "Questions": [{"QuestionName": "reqid", "Value": str(start + i)},
                                                                              {"QuestionName": "jobtitle", "Value": f"Cashier {start + i}"}]} for i in range(n)]}}

    def test_the_search_the_site_sends_is_posted_page_by_page(self):
        def answer(method, url, kw):
            return Resp(self.page(50, 120) if kw["json"]["PageNumber"] == 1 else self.page(50, 120, 50) if kw["json"]["PageNumber"] == 2 else self.page(20, 120, 100))
        jobs, session, sleep = fetching(rm.fetch_hobbylobby, answer, ["cashier"], 8, 0.4)
        self.assertEqual(len(jobs), 120)
        self.assertEqual([(m, u) for m, u, _ in session.asked], [("POST", "https://sjobs.brassring.com/TgNewUI/Search/Ajax/ProcessSortAndShowMoreJobs")] * 3)
        self.assertEqual([kw["json"] for _, _, kw in session.asked], [rm.brassring_body("25879", "5295", "cashier", n) for n in (1, 2, 3)])
        body = session.asked[0][2]["json"]
        self.assertEqual((body["partnerId"], body["siteId"], body["keyword"], body["PageNumber"]), ("25879", "5295", "cashier", 1))
        self.assertNotIn("Limit", body)
        self.assertEqual(len(sleep.call_args_list), 2)

    def test_harbor_freight_reads_both_of_its_sites_for_each_word(self):
        jobs, session, _ = fetching(rm.fetch_harborfreight, lambda m, u, kw: Resp(self.page(2, 2, 0 if kw["json"]["siteId"] == "6657" else 10)), ["cashier", "engineer"], 8, 0)
        self.assertEqual([(kw["json"]["siteId"], kw["json"]["keyword"]) for _, _, kw in session.asked],
                         [("6657", "cashier"), ("6660", "cashier"), ("6657", "engineer"), ("6660", "engineer")])
        self.assertEqual({kw["json"]["partnerId"] for _, _, kw in session.asked}, {"26281"})
        self.assertEqual(len(jobs), 4)                                  # the same four come back for the second word and count once

    def test_the_page_limit_and_the_cap(self):
        jobs, session, _ = fetching(rm.fetch_hobbylobby, lambda m, u, kw: Resp(self.page(50, 5000, 50 * kw["json"]["PageNumber"])), ["a"], 3, 0)
        self.assertEqual(len(session.asked), 3)
        with mock.patch.object(rm, "BRASSRING_MAX_REQUESTS", 5):
            jobs, session, _ = fetching(rm.fetch_hobbylobby, lambda m, u, kw: Resp(self.page(1, 1)), [f"w{n}" for n in range(30)], 8, 0)
        self.assertEqual(len(session.asked), 5)

    def test_an_empty_answer_ends_the_word(self):
        jobs, session, _ = fetching(rm.fetch_hobbylobby, lambda m, u, kw: Resp(self.page(0, 0)), ["nothing"], 8, 0)
        self.assertEqual((jobs, len(session.asked)), ([], 1))

    def test_a_blocked_site_is_an_error(self):
        with self.assertRaises(ValueError):
            fetching(rm.fetch_hobbylobby, lambda m, u, kw: Resp("<html>Access Denied</html>"), ["x"], 1, 0)
        with self.assertRaises(RuntimeError):
            fetching(rm.fetch_harborfreight, lambda m, u, kw: Resp("", 403), ["x"], 1, 0)

    def job(self):
        url = "https://sjobs.brassring.com/TGnewUI/Search/home/HomeWithPreLoad?partnerid=26281&siteid=6660&PageType=JobDetails&jobid=1437541"
        return sources.Job("harborfreight:1437541", "harborfreight", "Harbor Freight Tools", "Data Analytics Engineer- Remote", "Calabasas, CA (Remote)", url, detail={"api": url})

    def test_the_job_page_gives_text_title_date_and_place(self):
        j = self.job()
        j.title, j.location, j.posted = "x", "", None
        session = Session(lambda m, u, kw: Resp(text("misc_harborfreight_job.html")))
        with mock.patch.object(sources, "_session", lambda: session):
            sources.fill_detail(j)
        self.assertEqual(session.asked[0][1], j.url)
        self.assertEqual((j.title, j.posted, j.location), ("Data Analytics Engineer- Remote", "2026-10-27", "Calabasas, CA (Remote)"))
        self.assertIn("Analyze large data sets", j.description)
        self.assertNotIn("<", j.description)

    def test_a_page_with_no_posting_is_an_error_so_it_is_retried_next_run(self):
        for page in ("<html>Access Denied</html>", '<script type="application/ld+json">{"@type":"WebPage"}</script>', '<script type="application/ld+json">not json</script>',
                     '<script type="application/ld+json">{"@type":"JobPosting","title":"x","description":""}</script>'):
            with mock.patch.object(sources, "_session", lambda: Session(lambda m, u, kw: Resp(page))), self.assertRaises(ValueError):
                rm.fill_harborfreight_detail(self.job())
        with mock.patch.object(sources, "_session", lambda: Session(lambda m, u, kw: Resp("", 410))), self.assertRaises(RuntimeError):
            rm.fill_harborfreight_detail(self.job())
        j = self.job()
        j.url = "https://example.com/job"
        with self.assertRaises(ValueError):
            rm.fill_harborfreight_detail(j)


class Burlington(unittest.TestCase):
    def test_search_results(self):
        jobs = rm.parse_burlington(load("misc_burlington_search.json"))
        self.assertEqual(len(jobs), 2)
        j = jobs[0]
        self.assertEqual((j.id, j.source, j.company, j.title, j.location, j.posted), (
            "burlington:9D7295E78EE0455F911EA72DBD4D0F92", "burlington", "Burlington", "Senior Cybersecurity Engineer", "Edgewater Park, NJ", "2026-05-20"))
        self.assertEqual(j.url, "https://burlingtonstores.jobs/edgewater-park-nj/senior-cybersecurity-engineer/9D7295E78EE0455F911EA72DBD4D0F92/job/")
        self.assertIn("Position Overview", j.description)
        self.assertEqual(j.detail, {})                                 # the list holds the whole text
        self.assertEqual((j.pay_min, j.pay_max), (95000, 140000))

    def test_a_remote_job_says_so(self):
        data = copy.deepcopy(load("misc_burlington_search.json"))
        data["jobs"][0]["other"] = '{"Remote Type":"Remote"}'
        self.assertEqual(rm.parse_burlington(data)[0].location, "Edgewater Park, NJ (Remote)")
        self.assertEqual(rm.parse_burlington(data)[1].location, "Edgewater Park, NJ")      # hybrid is not remote

    def test_a_job_with_no_id_or_title_is_skipped_and_a_changed_answer_is_an_error(self):
        self.assertEqual(rm.parse_burlington({"jobs": [{"title_exact": "x"}, {"guid": "A", "title_exact": ""}]}), [])
        with self.assertRaises(ValueError):
            rm.parse_burlington({"errors": "x"})

    def page(self, n, more, start=0):
        return {"jobs": [{"guid": f"G{start + i}", "title_exact": f"Store Manager {start + i}", "city_exact": "Austin", "state_short": "TX", "title_slug": "store-manager",
                          "date_new": "2026-10-01T00:00:00Z", "description": "Run the store."} for i in range(n)], "pagination": {"has_more_pages": more}}

    def test_pages_are_asked_for_with_the_sites_own_header_until_there_are_no_more(self):
        def answer(method, url, kw):
            page = int(parse_qs(urlparse(url).query)["page"][0])
            return Resp(self.page(10, page < 3, 10 * (page - 1)))
        jobs, session, sleep = fetching(rm.fetch_burlington, answer, ["store manager"], 8, 0.4)
        self.assertEqual([u for _, u, _ in session.asked], [f"https://prod-search-api.jobsyn.org/api/v1/solr/search?q=store+manager&page={n}" for n in (1, 2, 3)])
        self.assertEqual({tuple(kw["headers"].items()) for _, _, kw in session.asked}, {(("x-origin", "burlingtonstores.jobs"),)})
        self.assertEqual(len(jobs), 30)
        self.assertEqual(len(sleep.call_args_list), 2)

    def test_the_page_limit_the_cap_and_repeats_across_words(self):
        jobs, session, _ = fetching(rm.fetch_burlington, lambda m, u, kw: Resp(self.page(10, True)), ["a", "b"], 2, 0)
        self.assertEqual((len(session.asked), len(jobs)), (4, 10))
        with mock.patch.object(rm, "BURLINGTON_MAX_REQUESTS", 5):
            jobs, session, _ = fetching(rm.fetch_burlington, lambda m, u, kw: Resp(self.page(10, True)), ["a", "b", "c"], 8, 0)
        self.assertEqual(len(session.asked), 5)

    def test_a_blocked_site_is_an_error(self):
        with self.assertRaises(ValueError):
            fetching(rm.fetch_burlington, lambda m, u, kw: Resp("<html>Access Denied</html>"), ["x"], 1, 0)
        with self.assertRaises(RuntimeError):
            fetching(rm.fetch_burlington, lambda m, u, kw: Resp("", 403), ["x"], 1, 0)


if __name__ == "__main__":
    unittest.main()
