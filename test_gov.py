"""Offline tests for the government sources (USAJobs, Texas HHS) and their title handling. No network.

The USAJobs response shape below follows the published API documentation; it has NOT been checked against the live
API, because that needs a key. The Texas HHS fixture (test_data/hhs_search_tiles.html) is two real result tiles."""
import os
import unittest
from pathlib import Path
from unittest import mock

import filters
import gov_sources as g
import main
import sources

FIXTURE = Path(__file__).parent / "test_data" / "hhs_search_tiles.html"

USAJOBS_PAGE = {"SearchResult": {"SearchResultCount": 2, "SearchResultItems": [
    {"MatchedObjectId": "800001", "MatchedObjectDescriptor": {
        "PositionTitle": "IT Specialist (APPSW)", "OrganizationName": "Veterans Health Administration",
        "DepartmentName": "Department of Veterans Affairs", "PositionURI": "https://www.usajobs.gov/job/800001",
        "PublicationStartDate": "2026-10-20", "QualificationSummary": "<p>One year of specialized experience.</p>",
        "PositionLocation": [{"LocationName": "Austin, Texas"}, {"LocationName": "Dallas, Texas"}],
        "PositionRemuneration": [{"MinimumRange": "88520.0000", "MaximumRange": "115079.0000", "RateIntervalCode": "PA", "Description": "Per Year"}],
        "UserArea": {"Details": {"JobSummary": "Develops and maintains applications."}}}},
    {"MatchedObjectId": "800002", "MatchedObjectDescriptor": {
        "PositionTitle": "Software Developer", "DepartmentName": "Department of the Army", "PositionURI": "https://www.usajobs.gov/job/800002",
        "PublicationStartDate": "2026-10-21", "PositionLocation": [{"LocationName": "Remote"}],
        "PositionRemuneration": [{"MinimumRange": "45.00", "MaximumRange": "60.00", "RateIntervalCode": "PH", "Description": "Per Hour"}],
        "UserArea": {"Details": {"JobSummary": "Pay is $100,000 - $130,000 per year."}}}},
    {"MatchedObjectDescriptor": {"PositionTitle": "No id, skipped"}},
]}}


class UsajobsTests(unittest.TestCase):
    def test_parse(self):
        a, b = g.parse_usajobs(USAJOBS_PAGE)
        self.assertEqual((a.id, a.source, a.company), ("usajobs:800001", "usajobs", "Veterans Health Administration"))
        self.assertEqual(a.location, "Austin, Texas; Dallas, Texas")
        self.assertEqual((a.pay_min, a.pay_max), (88520, 115079))
        self.assertEqual(a.posted, "2026-10-20")
        self.assertIn("Develops and maintains applications.", a.description)
        self.assertIn("specialized experience", a.description)
        self.assertEqual(b.company, "Department of the Army")                    # falls back to the department
        self.assertEqual((b.pay_min, b.pay_max), (100000, 130000))                 # hourly range ignored; pay read from the text

    def test_empty_or_odd_responses(self):
        self.assertEqual(g.parse_usajobs({}), [])
        self.assertEqual(g.parse_usajobs({"SearchResult": {"SearchResultItems": None}}), [])

    def test_fetch_sends_required_headers_and_dedupes_across_keywords(self):
        calls = []

        class Resp:
            def raise_for_status(self): pass
            def json(self): return USAJOBS_PAGE

        def fake_get(url, headers, timeout, params):
            calls.append((url, headers, params))
            return Resp()

        with mock.patch.object(g.requests, "get", fake_get):
            jobs = g.fetch_usajobs("KEY", "me@example.com", ["software developer", "programmer"], days=14, pages=2, delay=0)
        self.assertEqual([j.id for j in jobs], ["usajobs:800001", "usajobs:800002"])   # same jobs from both keywords
        self.assertEqual(len(calls), 2)                                                # short page, so no second page
        url, headers, params = calls[0]
        self.assertEqual(url, "https://data.usajobs.gov/api/search")
        self.assertEqual(headers["Authorization-Key"], "KEY")
        self.assertEqual(headers["User-Agent"], "me@example.com")
        self.assertEqual((params["Keyword"], params["DatePosted"], params["Page"]), ("software developer", 14, 1))


class TexasHhsTests(unittest.TestCase):
    def test_parse_real_result_tiles(self):
        jobs = g.parse_hhs_results(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(len(jobs), 2)                       # each tile lists the job once per layout; one Job per tile
        j = jobs[0]
        self.assertEqual((j.id, j.source, j.title), ("tx:1428718700", "texas_hhs", "Senior Java Developer"))
        self.assertEqual((j.location, j.posted), ("AUSTIN, TX", "2026-10-09"))
        self.assertEqual(j.company, "Texas Health & Human Services Comm")
        self.assertEqual(j.url, "https://careers.hhs.texas.gov/hhscjobs/job/AUSTIN-Senior-Java-Developer-TX-73301/1428718700/")
        self.assertEqual(j.detail["category"], "Computer and Mathematical")

    def test_layout_change_yields_nothing_instead_of_garbage(self):
        self.assertEqual(g.parse_hhs_results("<html><body><p>Maintenance</p></body></html>"), [])
        self.assertEqual(g.parse_hhs_results('<div class="job-tile-cell"><p>new layout</p></div>'), [])

    def test_fetch_pages_until_a_short_page(self):
        full = FIXTURE.read_text(encoding="utf-8")
        seen = []

        class Resp:
            def __init__(self, text): self.text = text
            def raise_for_status(self): pass

        def fake_get(url, headers, timeout, params):
            seen.append(params)
            return Resp(full)

        with mock.patch.object(g.requests, "get", fake_get):
            jobs = g.fetch_texas_hhs(["developer", "programmer"], pages=3, delay=0)
        self.assertEqual(len(jobs), 2)                       # same two jobs for both terms, deduped
        self.assertEqual([(p["q"], p["startrow"]) for p in seen], [("developer", 0), ("programmer", 0)])   # 2 < 25: stop

    def test_salary_conversion(self):
        self.assertEqual(g.parse_salary("Salary Range: $5,425.33 - $8,886.16 Pay Frequency: Monthly Salary Group: B-24"), (65103, 106633))
        self.assertEqual(g.parse_salary("Salary Range: $30.00 - $40.00 Pay Frequency: Hourly"), (62400, 83200))
        self.assertEqual(g.parse_salary("Salary Range: $90,000 - $120,000 Pay Frequency: Annual"), (90000, 120000))
        self.assertEqual(g.parse_salary("Salary Range: $90,000 - $120,000"), (90000, 120000))   # no frequency: annual
        self.assertEqual(g.parse_salary("Salary Range: $1 - $2 Pay Frequency: Fortnightly"), (None, None))   # unknown unit: unknown pay
        self.assertEqual(g.parse_salary("Pay is $100,000 - $130,000."), (100000, 130000))
        self.assertEqual(g.parse_salary("no pay info"), (None, None))

    def test_detail_fills_description_pay_and_date(self):
        page = ('<meta itemprop="datePosted" content="Tue Oct 20 00:00:00 UTC 2026"/>'
                '<span class="jobdescription"><p>Build <b>Java</b> services.</p><p>Salary Range: $5,425.33 - $8,886.16 '
                'Pay Frequency: Monthly</p></span></div>')

        class Resp:
            text = page
            def raise_for_status(self): pass

        job = sources.Job("tx:1", "texas_hhs", "Texas DFPS", "Java Developer", "AUSTIN, TX", "https://careers.hhs.texas.gov/x/1/", posted="2026-10-01")
        with mock.patch.object(g.requests, "get", lambda *a, **k: Resp()):
            sources.fill_detail(job)
        self.assertIn("Build Java services.", job.description)
        self.assertEqual((job.pay_min, job.pay_max), (65103, 106633))
        self.assertEqual(job.posted, "2026-10-20")

    def test_detail_with_unknown_layout_raises_so_the_job_is_skipped(self):
        class Resp:
            text = "<html>nothing here</html>"
            def raise_for_status(self): pass

        job = sources.Job("tx:1", "texas_hhs", "c", "t", "l", "https://careers.hhs.texas.gov/x/1/")
        with mock.patch.object(g.requests, "get", lambda *a, **k: Resp()):
            with self.assertRaises(ValueError):
                sources.fill_detail(job)

    def test_registered_for_detail_fetching(self):
        self.assertIn("texas_hhs", sources.DETAIL_FILLERS)


class CollectGovernmentTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        for k in ("USAJOBS_API_KEY", "USAJOBS_EMAIL"):
            os.environ.pop(k, None)
        self.no_env_file = mock.patch.object(main, "load_env")   # never read the developer's real .env in tests
        self.no_env_file.start()

    def tearDown(self):
        self.no_env_file.stop()
        self.env.stop()

    def test_both_on_by_default_and_usajobs_skipped_without_a_key(self):
        job = sources.Job("tx:1", "texas_hhs", "Texas DFPS", "Java Developer", "AUSTIN, TX", "u")
        with mock.patch.object(g, "fetch_texas_hhs", return_value=[job]) as tx, mock.patch.object(g, "fetch_usajobs") as uj:
            self.assertEqual(main.collect_government({"request_delay": 0}), [job])
            uj.assert_not_called()
            tx.assert_called_once()

    def test_usajobs_runs_when_the_key_and_email_are_set(self):
        os.environ.update(USAJOBS_API_KEY="K", USAJOBS_EMAIL="me@example.com")
        fed = sources.Job("usajobs:1", "usajobs", "VA", "IT Specialist", "Austin, Texas", "u")
        cfg = {"request_delay": 0, "max_age_days": 21, "government": {"usajobs": {"keywords": ["a"], "pages": 1}, "texas_hhs": {"enabled": False}}}
        with mock.patch.object(g, "fetch_usajobs", return_value=[fed]) as uj, mock.patch.object(g, "fetch_texas_hhs") as tx:
            self.assertEqual(main.collect_government(cfg), [fed])
            tx.assert_not_called()
            self.assertEqual(uj.call_args.args, ("K", "me@example.com", ["a"], 21, 1, 0))

    def test_a_failing_source_does_not_stop_the_run(self):
        os.environ.update(USAJOBS_API_KEY="K", USAJOBS_EMAIL="me@example.com")
        with mock.patch.object(g, "fetch_usajobs", side_effect=RuntimeError("401")), \
                mock.patch.object(g, "fetch_texas_hhs", side_effect=RuntimeError("timeout")):
            self.assertEqual(main.collect_government({"request_delay": 0}), [])

    def test_both_can_be_turned_off(self):
        cfg = {"government": {"usajobs": {"enabled": False}, "texas_hhs": {"enabled": False}}}
        with mock.patch.object(g, "fetch_usajobs") as uj, mock.patch.object(g, "fetch_texas_hhs") as tx:
            self.assertEqual(main.collect_government(cfg), [])
            uj.assert_not_called()
            tx.assert_not_called()


class GovTitleTests(unittest.TestCase):
    TECH = filters.base_titles(["Software Engineer II", "Platform Engineer"])
    GOV = TECH + filters.base_titles(["Programmer", "IT Specialist", "Java Developer", "Developer"])
    EXCLUDE = ["senior", "lead", "manager"]

    def test_government_titles_only_match_with_the_government_patterns(self):
        for title in ("Programmer III", "IT Specialist (APPSW)", "Java Developer", "Programmer Analyst IV"):
            self.assertTrue(filters.title_ok(title, self.GOV, self.EXCLUDE), title)
            self.assertFalse(filters.title_ok(title, self.TECH, self.EXCLUDE), title)

    def test_seniority_words_still_exclude(self):
        self.assertFalse(filters.title_ok("Senior Java Developer", self.GOV, self.EXCLUDE))
        self.assertFalse(filters.title_ok("Manager VI", self.GOV, self.EXCLUDE))

    def test_unrelated_government_titles_do_not_match(self):
        for title in ("Caseworker II", "Nurse Practitioner", "Data Analyst V"):
            self.assertFalse(filters.title_ok(title, self.GOV, self.EXCLUDE), title)

    def test_the_real_config_has_government_titles_and_sources_on(self):
        import yaml
        cfg = yaml.safe_load((Path(__file__).parent / "config.yaml").read_text(encoding="utf-8"))
        self.assertIn("Programmer", cfg["gov_titles"])
        self.assertTrue(cfg["government"]["usajobs"]["enabled"])
        self.assertTrue(cfg["government"]["texas_hhs"]["enabled"])


if __name__ == "__main__":
    unittest.main()
