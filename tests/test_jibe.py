"""Retailers on iCIMS Jibe (Sprouts, Ulta, JCPenney, PetSmart, Costco, H-E-B, Foot Locker, Sheetz), read from saved copies of the real
`/api/jobs` answers in tests/data/jibe_*.json (a few postings each; the longest texts are cut, and the site-wide facets and
per-posting `meta_data` are left out)."""
import json
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

import retail_jibe as rj
import sources
from tests.retailtools import Resp, Session

DATA = Path(__file__).parent / "data"
saved = lambda key: json.loads((DATA / f"jibe_{key}_search.json").read_text(encoding="utf-8"))
parsed = lambda key: {j.title: j for j in rj.parse_jibe_results(saved(key), key)}


def posting(n, **extra):
    d = {"req_id": str(n), "slug": str(n), "title": f"Software Engineer {n}", "description": f"Build things {n}.", "city": "Austin", "state": "Texas",
         "country_code": "US", "full_location": "Austin, Texas", "posted_date": "2026-10-20T10:00:00+0000"}
    return {"data": {**d, **extra}}


def answer(hits, size=rj.PAGE_SIZE):
    """A fake site: `hits` maps a search word to how many postings it has; the pages are cut at the `limit` asked for."""
    def reply(method, url, kw):
        q = parse_qs(urlparse(url).query)
        word, page, limit = q["keywords"][0], int(q["page"][0]), int(q["limit"][0])
        total = hits.get(word, 0)
        start = (page - 1) * limit
        # the first posting's number is unique to the word, so two words never share a posting unless a test makes them
        base = 1000 * (sorted(hits).index(word) + 1) if word in hits else 0
        jobs = [posting(base + n) for n in range(start, min(total, start + limit))]
        return Resp({"jobs": jobs, "totalCount": total, "count": total})
    return reply


def fetch(reply, terms, pages=20, delay=0.4, source="heb"):
    session = Session(reply)
    with mock.patch.object(sources, "_session", lambda: session), mock.patch.object(rj.time, "sleep") as sleep:
        jobs = rj.fetch_jibe(source, terms, pages, delay)
    return jobs, [u for _, u, _ in session.asked], [c.args[0] for c in sleep.call_args_list], session


class Registered(unittest.TestCase):
    def test_every_key_has_a_fetcher_and_the_text_comes_with_the_list(self):
        self.assertEqual(set(rj.READERS), {"sprouts", "ulta", "jcpenney", "petsmart", "costco", "heb", "footlocker", "sheetz"})
        for key, (label, fn) in rj.READERS.items():
            self.assertTrue(callable(fn) and label, key)
            self.assertNotIn(key, sources.DETAIL_FILLERS)       # the list holds the full text, so no posting is read again later

    def test_the_crawl_delay_every_robots_txt_asks_for(self):
        self.assertEqual(rj.CRAWL_DELAY, 5)


class Parsing(unittest.TestCase):
    def test_a_posting(self):
        j = parsed("ulta")["Engineering Manager, Agent Discovery & Commerce Engineering"]
        self.assertEqual((j.id, j.source, j.company, j.posted), ("ulta:513428", "ulta", "Ulta Beauty", "2026-08-28"))
        self.assertEqual(j.location, "Bolingbrook, Illinois, United States")
        self.assertEqual(j.url, "https://careers.ulta.com/careers/jobs/513428")     # the page a person opens, not the iCIMS apply link
        self.assertEqual((j.pay_min, j.pay_max, j.pay_unit), (119300, 175000, "year"))
        self.assertIn("Commerce", j.description)
        self.assertEqual(j.detail, {})                                              # the full text is already here

    def test_the_job_pages_of_each_site(self):
        for key, title, url in [("sprouts", "Cashier", "https://jobs.sprouts.com/jobs/382301"),
                                ("jcpenney", "Cashier - Pueblo Mall", "https://jobs.jcp.com/careers-home/jobs/1096815"),
                                ("costco", "Majors Sales Assistant", "https://careers.costco.com/jobs/2680"),
                                ("sheetz", "Store Team Member - #741", "https://jobs.sheetz.com/jobs/82611"),
                                ("petsmart", "Senior Automation Developer - UiPath", "https://careers.petsmart.com/jobs/7584")]:
            self.assertEqual(parsed(key)[title].url, url, key)

    def test_the_company_is_the_banner_of_a_shared_site(self):
        self.assertEqual(parsed("jcpenney")["Assistant Store Manager"].company, "Brooks Brothers")
        self.assertEqual(parsed("jcpenney")["Cashier - Pueblo Mall"].company, "JCPenney")
        self.assertEqual(parsed("jcpenney")["Lead Engineer-Marketing Technology"].company, "Catalyst Brands")
        heb = parsed("heb")
        self.assertEqual(heb["Cost & Pricing Staff Software Engineer"].company, "H-E-B")        # the site's brand is 'HEB'
        self.assertEqual(heb["Inventory Flow (Center Store) Manager - Joe V's Smart Shop - Houston, TX and Surrounding Areas"].company, "Joe V's Smart Shop")
        self.assertEqual(parsed("sheetz")["Store Team Member - #741"].company, "Sheetz")          # 'Sheetz, Inc' is not shown

    def test_a_posting_open_in_several_places_lists_them_all_once(self):
        j = parsed("heb")["Top Store Leader - Central Market"]
        self.assertTrue(j.location.startswith("Fort Worth, Texas, United States; Austin, Texas, United States; San Antonio, Texas, United States"))
        self.assertEqual(len(j.location.split("; ")), len(set(j.location.split("; "))))
        self.assertEqual(parsed("sheetz")["Assistant Manager - Hospitality - #667"].location,
                         "PRINCE GEORGE, Virginia, United States; COLONIAL HEIGHTS, Virginia, United States; PETERSBURG, Virginia, United States")

    def test_places_outside_the_united_states_and_remote_work(self):
        fl = parsed("footlocker")
        self.assertEqual(fl["Store Manager - Foot Locker Groningen"].location, "Groningen, Netherlands")
        self.assertEqual(fl["Senior Engineer, Customer Store Experience"].location, "United States (Remote)")     # the place is named 'Remote'
        self.assertEqual(parsed("jcpenney")["Senior Engineer 1"].location, "Bangalore, India")
        self.assertEqual(parsed("jcpenney")["Assistant Store Manager"].location, "New York, NY, United States")    # JCPenney uses state codes

    def test_remote_in_the_title_counts_too(self):
        d = posting(5, title="Remote Customer Service Representative")["data"]
        self.assertEqual(rj.parse_jibe_results({"jobs": [{"data": d}]}, "costco")[0].location, "Austin, Texas, United States (Remote)")
        d = posting(6, title="Virtual Reality Engineer")["data"]      # 'virtual' in a title is a product, not a place
        self.assertEqual(rj.parse_jibe_results({"jobs": [{"data": d}]}, "costco")[0].location, "Austin, Texas, United States")

    def test_a_place_made_from_city_state_and_country_when_the_site_gives_no_line(self):
        d = posting(7, full_location="", city="Reno", state="Nevada")["data"]
        self.assertEqual(rj.parse_jibe_results({"jobs": [{"data": d}]}, "sprouts")[0].location, "Reno, Nevada, United States")
        d = posting(8, full_location="", city=None, state=None, country=None, country_code=None)["data"]
        self.assertEqual(rj.parse_jibe_results({"jobs": [{"data": d}]}, "sprouts")[0].location, "")

    def test_pay_is_numbers_for_petsmart_and_text_elsewhere(self):
        ps = parsed("petsmart")
        j = ps["Retail Sales Associate Part Time"]
        self.assertEqual((j.pay_min, j.pay_max, j.pay_unit), (15, 22, "hour"))
        j = ps["Retail Store Manager"]
        self.assertEqual((j.pay_min, j.pay_max, j.pay_unit), (70000, 111000, "year"))
        j = parsed("footlocker")["Store Manager"]
        self.assertEqual((j.pay_min, j.pay_max), (66000, 70000))
        self.assertEqual(parsed("costco")["Cashier (Front End)"].pay_min, None)

    def test_a_yearly_figure_marked_hourly_is_not_taken_as_a_rate(self):
        d = posting(9, salary_min_value=79000, salary_max_value=130000, salary_frequency="HOURLY", description="Pay: $50,000 - $60,000 a year.")["data"]
        j = rj.parse_jibe_results({"jobs": [{"data": d}]}, "petsmart")[0]
        self.assertEqual((j.pay_min, j.pay_max, j.pay_unit), (50000, 60000, "year"))       # falls back to the text
        d = posting(10, salary_min_value=15.75, salary_max_value=25.75, salary_frequency="HOURLY")["data"]
        j = rj.parse_jibe_results({"jobs": [{"data": d}]}, "petsmart")[0]
        self.assertEqual((j.pay_min, j.pay_max, j.pay_unit), (15.75, 25.75, "hour"))

    def test_the_text_holds_what_the_site_keeps_apart_without_repeating_it(self):
        kept = rj.parse_jibe_results({"jobs": [posting(1, description="About the job.", qualifications="You know Java.", responsibilities="You ship code.")]},
                                     "ulta")[0].description
        self.assertEqual(kept, "About the job.\n\nYou know Java.\n\nYou ship code.")
        repeated = posting(2, description="About the job. You know Java. You ship code.", qualifications="You know Java.", responsibilities="You ship  code.")
        self.assertEqual(rj.parse_jibe_results({"jobs": [repeated]}, "ulta")[0].description, "About the job. You know Java. You ship code.")
        html = posting(3, description="<p>Hello</p><ul><li>One</li><li>Two</li></ul>")
        self.assertEqual(rj.parse_jibe_results({"jobs": [html]}, "heb")[0].description, "Hello\n\n- One\n- Two")

    def test_a_posting_with_no_id_or_title_is_skipped(self):
        data = {"jobs": [{"data": {"title": "No id"}}, posting(1, title=""), {"data": None}, "junk", posting(2)]}
        self.assertEqual([j.id for j in rj.parse_jibe_results(data, "heb")], ["heb:2"])
        self.assertEqual(rj.parse_jibe_results({"jobs": []}, "heb"), [])

    def test_an_answer_that_is_not_a_job_list_is_an_error(self):
        for page in ({"timestamp": "2026-11-03", "path": "/api/jobs", "data": {"error": "An unexpected error occurred"}}, [], {"jobs": None}, "Access Denied"):
            with self.assertRaises(ValueError):
                rj.parse_jibe_results(page, "heb")

    def test_a_posting_with_no_date_has_none(self):
        self.assertIsNone(rj.parse_jibe_results({"jobs": [posting(1, posted_date=None, create_date=None)]}, "heb")[0].posted)
        self.assertEqual(rj.parse_jibe_results({"jobs": [posting(1, posted_date=None, create_date="2026-09-01T10:00:00+0000")]}, "heb")[0].posted, "2026-09-01")


class Fetching(unittest.TestCase):
    def test_each_word_is_asked_for_the_biggest_page_and_the_crawl_delay_is_waited(self):
        jobs, asked, slept, session = fetch(answer({"software engineer": 3, "cashier": 2}), ["software engineer", "cashier"], delay=0.4)
        self.assertEqual(asked, ["https://careers.heb.com/api/jobs?keywords=cashier&page=1&limit=100",
                                 "https://careers.heb.com/api/jobs?keywords=software%20engineer&page=1&limit=100"])     # the one-word search first
        self.assertEqual(len(jobs), 5)
        self.assertEqual(slept, [5.0])            # between the two requests, not before the first
        self.assertTrue(all(kw["allow_redirects"] is False and kw["timeout"] == sources.TIMEOUT for _, _, kw in session.asked))

    def test_a_longer_delay_asked_for_is_kept(self):
        self.assertEqual(fetch(answer({"a": 1, "b": 1}), ["a", "b"], delay=8)[2], [8])

    def test_the_site_chosen_decides_the_address(self):
        _, asked, _, _ = fetch(answer({"cashier": 1}), ["cashier"], source="sprouts")
        self.assertEqual(asked, ["https://jobs.sprouts.com/api/jobs?keywords=cashier&page=1&limit=100"])
        self.assertEqual(rj.READERS["ulta"][1].__name__, "fetch_ulta")

    def test_a_word_with_more_hits_than_a_page_is_read_page_by_page_to_the_end(self):
        jobs, asked, slept, _ = fetch(answer({"cashier": 250}), ["cashier"])
        self.assertEqual([parse_qs(urlparse(u).query)["page"][0] for u in asked], ["1", "2", "3"])
        self.assertEqual((len(jobs), slept), (250, [5.0, 5.0]))

    def test_the_first_page_of_every_word_is_read_before_the_second_page_of_any(self):
        _, asked, _, _ = fetch(answer({"cashier": 250, "pharmacist": 150, "driver": 5}), ["cashier", "pharmacist", "driver"])
        order = [(parse_qs(urlparse(u).query)["keywords"][0], parse_qs(urlparse(u).query)["page"][0]) for u in asked]
        self.assertEqual(order, [("cashier", "1"), ("pharmacist", "1"), ("driver", "1"), ("cashier", "2"), ("pharmacist", "2"), ("cashier", "3")])

    def test_no_more_pages_than_asked_for_per_word(self):
        _, asked, _, _ = fetch(answer({"cashier": 1000}), ["cashier"], pages=2)
        self.assertEqual(len(asked), 2)
        _, asked, _, _ = fetch(answer({"cashier": 1000}), ["cashier"], pages=1)
        self.assertEqual(len(asked), 1)

    def test_a_site_is_never_asked_for_more_than_max_requests(self):
        words = [f"word{n}" for n in range(30)]
        with mock.patch.object(rj, "MAX_REQUESTS", 12):
            _, asked, slept, _ = fetch(answer({w: 500 for w in words}), words)
        self.assertEqual((len(asked), len(slept)), (12, 11))
        self.assertTrue(all("page=1&" in u for u in asked))      # all twelve went to first pages: the budget goes to many words first
        with mock.patch.object(rj, "MAX_REQUESTS", 8):           # leftover budget goes to second pages
            _, asked, _, _ = fetch(answer({"a": 500, "b": 500, "c": 9}), ["a", "b", "c"])
        self.assertEqual([parse_qs(urlparse(u).query)["page"][0] for u in asked], ["1", "1", "1", "2", "2", "3", "3", "4"])

    def test_a_posting_found_by_two_words_is_kept_once(self):
        def reply(method, url, kw):
            return Resp({"jobs": [posting(1), posting(2 if "cashier" in url else 3)], "totalCount": 2})
        jobs, _, _, _ = fetch(reply, ["cashier", "clerk"])
        self.assertEqual(sorted(j.id for j in jobs), ["heb:1", "heb:2", "heb:3"])

    def test_a_longer_word_is_skipped_when_a_shorter_one_inside_it_was_read_to_the_end(self):
        words = ["senior software engineer", "software engineer", "software", "cashier"]
        _, asked, _, _ = fetch(answer({"software": 4, "software engineer": 2, "cashier": 9, "senior software engineer": 1}), words)
        self.assertEqual([parse_qs(urlparse(u).query)["keywords"][0] for u in asked], ["software", "cashier"])

    def test_a_longer_word_is_read_when_the_shorter_one_was_not_read_to_the_end(self):
        words = ["software", "senior software engineer"]
        _, asked, _, _ = fetch(answer({"software": 150, "senior software engineer": 1}), words, pages=1)
        self.assertEqual([parse_qs(urlparse(u).query)["keywords"][0] for u in asked], ["software", "senior software engineer"])

    def test_words_that_only_share_letters_are_not_covered(self):
        _, asked, _, _ = fetch(answer({"data": 2, "database administrator": 1, "data entry": 1}), ["database administrator", "data", "data entry"])
        # 'data entry' holds the word 'data' and is covered; 'database administrator' only starts with the same letters
        self.assertEqual([parse_qs(urlparse(u).query)["keywords"][0] for u in asked], ["data", "database administrator"])

    def test_repeated_and_empty_words_are_asked_once_or_not_at_all(self):
        _, asked, _, _ = fetch(answer({"cashier": 1}), ["Cashier", "cashier ", "", "  "])
        self.assertEqual(len(asked), 1)

    def test_special_characters_are_encoded(self):
        _, asked, _, _ = fetch(answer({"c++ developer": 1}), ["C++ Developer"])
        self.assertEqual(asked, ["https://careers.heb.com/api/jobs?keywords=C%2B%2B%20Developer&page=1&limit=100"])

    def test_a_word_with_no_hits_is_fine(self):
        jobs, asked, _, _ = fetch(answer({}), ["software engineer"])
        self.assertEqual((jobs, len(asked)), ([], 1))


class Failing(unittest.TestCase):
    def test_a_turned_away_first_request_is_an_error(self):
        for resp in (Resp("Access Denied", 403), Resp({"data": {"error": "An unexpected error occurred"}}, 422)):
            with self.assertRaises(RuntimeError):
                fetch(lambda m, u, kw: resp, ["cashier"])

    def test_a_page_that_is_not_json_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "JSON"):
            fetch(lambda m, u, kw: Resp("<html><body>Please verify you are human</body></html>"), ["cashier"])

    def test_a_json_answer_of_another_shape_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "Jibe"):
            fetch(lambda m, u, kw: Resp({"results": []}), ["cashier"])

    def test_a_redirect_is_not_followed(self):
        with self.assertRaisesRegex(ValueError, "redirected.*https://elsewhere.example/"):
            fetch(lambda m, u, kw: Resp("", 301, {"location": "https://elsewhere.example/"}), ["cashier"])

    def test_a_failure_after_postings_were_read_ends_the_run_with_what_was_read(self):
        good = answer({"a": 2, "b": 2})

        def reply(method, url, kw):
            if "keywords=c" in url:
                return Resp("Too Many Requests", 429)
            return good(method, url, kw)
        jobs, asked, _, _ = fetch(reply, ["a", "b", "c", "d"])
        self.assertEqual((len(jobs), len(asked)), (4, 3))         # nothing is asked after the refusal

    def test_a_failure_after_nothing_was_read_is_still_an_error(self):
        calls = []

        def reply(method, url, kw):
            calls.append(url)
            return Resp({"jobs": [], "totalCount": 0}) if len(calls) == 1 else Resp("Forbidden", 403)
        with self.assertRaises(RuntimeError):
            fetch(reply, ["a", "b"])


if __name__ == "__main__":
    unittest.main()
