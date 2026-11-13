"""Career sites of large employers that are not retailers: Apple's reader, and the Honeywell, Texas Instruments, Oracle and Disney rows
that reuse the Oracle and Phenom readers."""
import unittest
from unittest import mock

import corp_misc
import retail_oracle
import retail_sources
import sources
from sources import Job


def result(pid, title, city="Cupertino", state="", date="2026-11-11T02:59:33.340Z"):
    return {"positionId": pid, "postingTitle": title, "transformedPostingTitle": title.lower().replace(", ", "-").replace(" ", "-"),
            "jobSummary": "<p>Summary text.</p>", "postDateInGMT": date, "locations": [{"city": city, "stateProvince": state, "name": city}]}


def page(rows, total):
    return {"res": {"searchResults": rows, "totalRecords": total}}


class AppleParsingTests(unittest.TestCase):
    def test_a_result_becomes_a_job_with_a_link_a_place_a_date_and_a_detail_id(self):
        (job,) = corp_misc.parse_apple_results(page([result("200694406", "Software Engineer, Maps", state="CA")], 1))
        self.assertEqual((job.id, job.source, job.company, job.title), ("apple:200694406", "apple", "Apple", "Software Engineer, Maps"))
        self.assertEqual(job.location, "Cupertino, CA")
        self.assertEqual(job.posted, "2026-11-11")
        self.assertEqual(job.url, "https://jobs.apple.com/en-us/details/200694406/software-engineer-maps")
        self.assertEqual(job.description, "Summary text.")
        self.assertEqual(job.detail, {"id": "200694406"})

    def test_several_places_are_joined_and_repeats_are_dropped(self):
        row = result("200000001", "Engineer")
        row["locations"] = [{"city": "Austin", "stateProvince": "TX"}, {"city": "Austin", "stateProvince": "TX"}, {"name": "Cupertino"}, "junk"]
        self.assertEqual(corp_misc.parse_apple_results(page([row], 1))[0].location, "Austin, TX; Cupertino")

    def test_rows_without_an_id_or_a_title_and_odd_answers_are_skipped(self):
        rows = [result("200000001", "Fine"), {"postingTitle": "No id"}, {"positionId": "5"}, "junk"]
        self.assertEqual([j.id for j in corp_misc.parse_apple_results(page(rows, 4))], ["apple:200000001"])
        for odd in ({}, {"res": None}, {"res": {"searchResults": "x"}}, [], None):
            self.assertEqual(corp_misc.parse_apple_results(odd), [])

    def test_the_pay_range_is_read_in_the_wording_apple_uses(self):
        self.assertEqual(corp_misc._apple_pay("The base pay range for this role is between $119,700 and $225,600, and your base pay"), (119700, 225600))
        self.assertEqual(corp_misc._apple_pay("The range is $90,000 - $120,000.50 a year"), (90000, 120000))
        self.assertEqual(corp_misc._apple_pay("no pay here"), (None, None))


class AppleFetchTests(unittest.TestCase):
    def fetch(self, answers, terms=("software engineer",), pages=8):
        calls = []

        def fake_json(method, url, **kw):
            calls.append((method, url, kw.get("json"), kw.get("headers")))
            return answers.pop(0)
        with mock.patch.object(corp_misc, "_json", side_effect=fake_json), \
                mock.patch.object(corp_misc, "_apple_headers", return_value={"x-apple-csrf-token": "t"}) as token, \
                mock.patch("time.sleep"):
            jobs = corp_misc.fetch_apple(list(terms), pages, 0)
        return jobs, calls, token

    def test_it_pages_through_the_results_for_a_word_in_the_us_and_stops_at_the_total(self):
        first = [result(str(200000000 + i), f"Job {i}") for i in range(20)]
        second = [result("200000100", "Job last")]
        jobs, calls, token = self.fetch([page(first, 21), page(second, 21)])
        self.assertEqual(len(jobs), 21)
        self.assertEqual(len(calls), 2)
        self.assertEqual([c[2]["page"] for c in calls], [1, 2])
        self.assertEqual(calls[0][2]["filters"], {"locations": ["postLocation-USA"]})
        self.assertEqual(calls[0][2]["query"], "software engineer")
        self.assertEqual(calls[0][3], {"x-apple-csrf-token": "t"})
        token.assert_called_once()                       # one token for the whole run

    def test_a_word_stops_when_a_page_is_empty_and_each_word_is_searched(self):
        jobs, calls, _ = self.fetch([page([result("200000001", "A")], 1), page([], 0)], terms=["alpha", "beta", "alpha"])
        self.assertEqual([c[2]["query"] for c in calls], ["alpha", "beta"])      # a repeated word is searched once
        self.assertEqual([j.id for j in jobs], ["apple:200000001"])

    def test_the_request_cap_ends_the_run_and_keeps_what_was_found(self):
        rows = lambda n: page([result(str(200000000 + n * 100 + i), f"J{n}-{i}") for i in range(20)], 10_000)
        answers = [rows(n) for n in range(corp_misc.APPLE_MAX_REQUESTS + 5)]
        jobs, calls, _ = self.fetch(answers, pages=10_000)
        self.assertEqual(len(calls), corp_misc.APPLE_MAX_REQUESTS)
        self.assertEqual(len(jobs), corp_misc.APPLE_MAX_REQUESTS * 20)


class AppleDetailTests(unittest.TestCase):
    def detail(self, res, job=None):
        job = job or Job("apple:200694406", "apple", "Apple", "Old title", "Cupertino", "u", detail={"id": "200694406"})
        with mock.patch.object(corp_misc, "_json", return_value={"res": res}) as get, \
                mock.patch.object(corp_misc, "_apple_headers", return_value={}):
            corp_misc.fill_apple_detail(job)
        return job, get

    def test_the_text_is_the_description_and_both_qualification_lists_and_the_pay_comes_from_the_footer(self):
        job, get = self.detail({
            "description": "Build things.", "minimumQualifications": "- 3 years of Swift", "preferredQualifications": "- A degree",
            "postingTitle": "Software Engineer, Maps", "postDateInGMT": "2026-10-01T00:00:00.000+00:00",
            "locations": [{"city": "Austin", "stateProvince": "Texas"}],
            "postingFooters": [{"localizations": {"en_US": [
                {"name": "EEO Statement", "content": "<p>Equal.</p>"},
                {"name": "Pay & Benefits", "content": "<p>The base pay range is between $119,700 and $225,600.</p>"}]}}]})
        self.assertEqual(job.description, "Build things.\n\n## Minimum Qualifications\n- 3 years of Swift\n\n## Preferred Qualifications\n- A degree")
        self.assertEqual((job.title, job.posted, job.location), ("Software Engineer, Maps", "2026-10-01", "Austin, Texas"))
        self.assertEqual((job.pay_min, job.pay_max), (119700, 225600))
        self.assertIn("/api/v1/jobDetails/200694406", get.call_args.args[1])

    def test_a_posting_without_pay_keeps_what_the_list_gave(self):
        job, _ = self.detail({"description": "Text.", "postingFooters": []})
        self.assertEqual((job.pay_min, job.pay_max, job.title), (None, None, "Old title"))

    def test_an_answer_with_no_text_is_an_error_and_a_bad_id_is_refused_before_any_request(self):
        with self.assertRaises(ValueError):
            self.detail({"description": ""})
        bad = Job("apple:x", "apple", "Apple", "t", "", "u", detail={"id": "../../etc/passwd"})
        with mock.patch.object(corp_misc, "_json") as get, self.assertRaises(ValueError):
            corp_misc.fill_apple_detail(bad)
        get.assert_not_called()


def bofa_row(req, title="Software Engineer", country="United States", place="Charlotte, North Carolina", posted="11/10/2026"):
    return {"jobRequisitionId": req, "postingTitle": title, "country": country, "primaryLocation": place, "postedDate": posted,
            "jcrURL": f"/en-us/job-detail/{req}/software-engineer"}


class BankOfAmericaTests(unittest.TestCase):
    def test_a_result_becomes_a_job_with_a_us_place_a_date_and_a_page_to_read_later(self):
        (job,) = corp_misc.parse_bofa_results({"jobsList": [bofa_row("26030001")]})
        self.assertEqual((job.id, job.source, job.company, job.title), ("bankofamerica:26030001", "bankofamerica", "Bank of America", "Software Engineer"))
        self.assertEqual(job.location, "Charlotte, North Carolina, United States")
        self.assertEqual(job.posted, "2026-11-10")
        self.assertEqual(job.url, "https://careers.bankofamerica.com/en-us/job-detail/26030001/software-engineer")
        self.assertEqual(job.detail, {"url": job.url})

    def test_a_place_outside_the_us_is_left_as_it_is_and_unusable_rows_are_skipped(self):
        rows = [bofa_row("1", country="Australia", place="Sydney, Australia"), {"postingTitle": "No id"}, {"jobRequisitionId": "2", "postingTitle": "x", "jcrURL": "http://evil"}, "junk"]
        jobs = corp_misc.parse_bofa_results({"jobsList": rows})
        self.assertEqual([(j.id, j.location) for j in jobs], [("bankofamerica:1", "Sydney, Australia")])
        for odd in ({}, {"jobsList": None}, {"jobsList": "x"}, [], None):
            self.assertEqual(corp_misc.parse_bofa_results(odd), [])

    def fetch(self, answers, pages=8, terms=("software engineer",)):
        urls = []

        def fake_json(method, url, **kw):
            urls.append(url)
            return answers.pop(0)
        with mock.patch.object(corp_misc, "_json", side_effect=fake_json), mock.patch("time.sleep"):
            return corp_misc.fetch_bankofamerica(list(terms), pages, 0), urls

    def test_rows_is_where_the_window_ends_so_each_page_starts_where_the_last_one_stopped(self):
        full = [bofa_row(str(26000000 + i)) for i in range(50)]
        more = [bofa_row(str(26000100 + i)) for i in range(10)]
        jobs, urls = self.fetch([{"jobsList": full, "totalMatches": 60}, {"jobsList": more, "totalMatches": 60}])
        self.assertEqual(len(jobs), 60)
        self.assertEqual(len(urls), 2)
        self.assertIn("start=0&rows=50", urls[0])
        self.assertIn("start=50&rows=100", urls[1])
        self.assertIn("term=software+engineer", urls[0])

    def test_a_short_or_empty_page_ends_the_word_and_a_repeated_word_is_searched_once(self):
        jobs, urls = self.fetch([{"jobsList": [bofa_row("1")], "totalMatches": 1}, {"jobsList": None, "totalMatches": 0}], terms=["a b", "c", "a b"])
        self.assertEqual(len(urls), 2)
        self.assertEqual([j.id for j in jobs], ["bankofamerica:1"])

    def test_the_whole_posting_comes_from_the_page_and_a_foreign_address_is_refused(self):
        page = ('<script type="application/ld+json">{"@type": "JobPosting", "title": "Engineer", "datePosted": "2026-11-09", '
                '"description": "<p>Build it. Pay range $120,000 - $150,000 a year.</p>"}</script>')
        job = Job("bankofamerica:1", "bankofamerica", "Bank of America", "Engineer", "x", "u", posted="2026-11-10",
                  detail={"url": "https://careers.bankofamerica.com/en-us/job-detail/1/x"})
        response = mock.Mock(text=page)
        with mock.patch.object(sources, "_session") as session:
            session.return_value.get.return_value = response
            corp_misc.fill_bankofamerica_detail(job)
        self.assertIn("Build it.", job.description)
        self.assertEqual((job.pay_min, job.pay_max, job.posted), (120000, 150000, "2026-11-09"))
        foreign = Job("bankofamerica:2", "bankofamerica", "Bank of America", "t", "", "u", detail={"url": "https://evil.example/x"})
        with mock.patch.object(sources, "_session") as session, self.assertRaises(ValueError):
            corp_misc.fill_bankofamerica_detail(foreign)
        session.return_value.get.assert_not_called()


class RowsTests(unittest.TestCase):
    def test_the_new_employers_are_listed_as_sources_and_have_a_detail_reader(self):
        keys = {key for key, *_ in retail_sources.RETAIL_SOURCES}
        self.assertTrue({"apple", "honeywell", "texasinstruments", "oracle", "disney"} <= keys)
        for key in ("apple", "honeywell", "texasinstruments", "oracle"):
            self.assertIn(key, sources.DETAIL_FILLERS)
            self.assertTrue(callable(getattr(retail_sources, f"fetch_{key}")))

    def test_the_oracle_rows_name_a_host_and_a_site(self):
        for key in ("honeywell", "texasinstruments", "oracle"):
            host, site, label = retail_oracle.ORACLE_SITES[key]
            self.assertTrue(host.endswith(".oraclecloud.com") and site and label)
            self.assertTrue(retail_oracle.job_url(key, "1").startswith(f"https://{host}/"))

    def test_the_jibe_and_sitemap_rows_are_listed_as_sources(self):
        import retail_jibe
        import retail_sitemap
        keys = {key for key, *_ in retail_sources.RETAIL_SOURCES}
        self.assertTrue({"amd", "statefarm", "garmin", "fidelity", "bankofamerica"} <= keys)
        for key in ("amd", "statefarm", "garmin"):
            label, site, path = retail_jibe.JIBE_SITES[key]
            self.assertTrue(site.startswith("https://") and "{}" in path)
        self.assertIn("fidelity", retail_sitemap.SITES)
        self.assertRegex("/en/jobs/2120655/principal-data-scientist/", retail_sitemap.SITES["fidelity"].job)

    def test_disney_is_a_phenom_site(self):
        address, company, prefix = retail_sources.PHENOM_SITES["disney"]
        self.assertEqual((address, company, prefix), ("https://join.disneycareers.com/global/en", "Disney", "disney"))


if __name__ == "__main__":
    unittest.main()
