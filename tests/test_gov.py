"""Offline tests for the government sources (USAJobs, Texas HHS) and their title handling. No network.

The USAJobs response shape below follows the published API documentation; it has NOT been checked against the live
API, because that needs a key. The Texas HHS fixture (tests/data/hhs_search_tiles.html) is two real result tiles."""
import os
import unittest
from pathlib import Path
from unittest import mock

import filters
import gov_sources as g
import main
import sources
from tests.govoff import gov_off

FL_FIXTURE = Path(__file__).parent / "data" / "florida_results.html"
VT_FIXTURE = Path(__file__).parent / "data" / "vermont_results.html"
MD_FIXTURE = Path(__file__).parent / "data" / "jobaps_md_results.html"
CT_FIXTURE = Path(__file__).parent / "data" / "jobaps_ct_results.html"
NY_FIXTURE = Path(__file__).parent / "data" / "ny_results.html"
FIXTURE = Path(__file__).parent / "data" / "hhs_search_tiles.html"

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
        self.assertIn("Build **Java** services.", job.description)
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


class FloridaTests(unittest.TestCase):
    def test_parse_real_result_rows(self):
        jobs = g.parse_florida_results(FL_FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(len(jobs), 3)                       # each row repeats the job in a phone layout; one Job per row
        j = jobs[0]
        self.assertEqual((j.id, j.source, j.title), ("fl:1375250800", "florida", ".NET Software Developer (Angular / C#)"))
        self.assertEqual((j.company, j.location, j.posted), ("Florida Legislature", "Tallahassee, FL", "2026-10-09"))
        self.assertEqual(j.url, "https://jobs.myflorida.com/job/TALLAHASSEE-_NET-Software-Developer-%28Angular-C%29-Tallahassee%2C-Florida-FL-32399/1375250800/")
        self.assertEqual(j.detail["category"], "Other")

    def test_agency_names_get_a_florida_prefix_and_titles_lose_position_numbers(self):
        jobs = {j.id: j for j in g.parse_florida_results(FL_FIXTURE.read_text(encoding="utf-8"))}
        commerce = jobs["fl:1443809400"]
        self.assertEqual((commerce.company, commerce.title), ("Florida Commerce", "GOVERNMENT OPERATIONS CONSULTANT III"))
        self.assertEqual(jobs["fl:1441023800"].title, "APPLICATION SYSTEMS PROGRAMMER III")

    def test_title_cleaning(self):
        self.assertEqual(g._sf_title("Nurse - Miami, Florida", "Florida"), "Nurse")
        self.assertEqual(g._sf_title("12345 - Analyst II - 60012345 1", "Florida"), "Analyst II")
        self.assertEqual(g._sf_title("Software Developer - Phase 2", "Florida"), "Software Developer - Phase 2")   # a real dash stays

    def test_layout_change_yields_nothing_instead_of_garbage(self):
        self.assertEqual(g.parse_florida_results("<html><body><p>Maintenance</p></body></html>"), [])
        self.assertEqual(g.parse_florida_results('<tr class="data-row"><td>new layout</td></tr>'), [])

    def test_fetch_pages_until_a_short_page(self):
        full = FL_FIXTURE.read_text(encoding="utf-8")
        seen = []

        class Resp:
            def __init__(self, text): self.text = text
            def raise_for_status(self): pass

        def fake_get(url, headers, timeout, params):
            seen.append((url, params["q"], params["startrow"]))
            return Resp(full)

        with mock.patch.object(g.requests, "get", fake_get):
            jobs = g.fetch_florida(["developer", "programmer"], pages=3, delay=0)
        self.assertEqual(len(jobs), 3)                       # the same three jobs for both terms, deduped
        self.assertEqual(seen, [("https://jobs.myflorida.com/search/", "developer", 0), ("https://jobs.myflorida.com/search/", "programmer", 0)])

    def test_registered_for_detail_fetching_and_government_titles(self):
        self.assertIn("florida", sources.DETAIL_FILLERS)
        self.assertIn("florida", g.GOV_SOURCES)


class VermontTests(unittest.TestCase):
    """Vermont runs the same SuccessFactors site software as Florida, with no facility column: the department is the agency."""

    def test_parse_real_result_rows(self):
        jobs = g.parse_vermont_results(VT_FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(len(jobs), 3)
        j = jobs[0]
        self.assertEqual((j.id, j.source, j.title), ("vt:1423907100", "vermont", "Procurement Technical Assistance Counselor I"))
        self.assertEqual((j.company, j.location, j.posted), ("Vermont Commerce & Community Development Agency", "Montpelier, VT", "2026-10-20"))
        self.assertTrue(j.url.startswith("https://careers.vermont.gov/job/Montpelier-"))
        self.assertEqual(j.detail["category"], "")

    def test_fetch_uses_the_vermont_site(self):
        seen = []

        class Resp:
            text = VT_FIXTURE.read_text(encoding="utf-8")
            def raise_for_status(self): pass

        with mock.patch.object(g.requests, "get", lambda url, **k: seen.append(url) or Resp()):
            jobs = g.fetch_vermont(["developer"], pages=2, delay=0)
        self.assertEqual((len(jobs), seen), (3, ["https://careers.vermont.gov/search/"]))

    def test_registered_for_detail_fetching_and_government_titles(self):
        self.assertIn("vermont", sources.DETAIL_FILLERS)
        self.assertIn("vermont", g.GOV_SOURCES)


class JobApsTests(unittest.TestCase):
    """Maryland and Connecticut both run JobAps: one home page lists every current recruitment."""

    def test_parse_maryland_rows(self):
        jobs = g.parse_jobaps_results(MD_FIXTURE.read_text(encoding="utf-8"), "maryland")
        self.assertEqual(len(jobs), 3)                       # the commented-out practice row is not a job
        j = jobs[0]
        self.assertEqual((j.id, j.source, j.title), ("md:26-003492-0003", "maryland", "HEARINGS INTERPRETER"))   # "**REPOST**" is dropped
        self.assertEqual((j.company, j.location), ("Maryland Worker's Compensation Commission", "Baltimore City, MD"))
        self.assertEqual((j.pay_min, j.pay_max), (27855, 33292))
        self.assertEqual(j.url, "https://www.jobapscloud.com/MD/sup/bulpreview.asp?b=&R1=26&R2=003492&R3=0003")

    def test_parse_connecticut_rows(self):
        jobs = g.parse_jobaps_results(CT_FIXTURE.read_text(encoding="utf-8"), "connecticut")
        self.assertEqual(len(jobs), 3)
        j = jobs[0]
        self.assertEqual((j.id, j.source, j.location), ("ct:261027-0965FP-001", "connecticut", "New Haven, CT"))
        self.assertTrue(j.company.startswith("Connecticut "))
        self.assertEqual((j.pay_min, j.pay_max), (None, None))      # Connecticut's list has no salary column; the job page does

    def test_delaware_locations_are_cut_down_to_the_town(self):
        self.assertEqual(g._jobaps_place("Stockley Center: (26351 Patriots Way, Georgetown, DE, 19947) <br />", "DE"), "Georgetown, DE")
        self.assertEqual(g._jobaps_place("Kent", "DE"), "Kent, DE")
        self.assertEqual(g._jobaps_place("Dover, DE", "DE"), "Dover, DE")

    def test_a_single_pay_amount_is_a_pay_too_and_practice_applications_are_not_jobs(self):
        self.assertEqual(g._jobaps_pay("$54.01 /hour"), (112340, 112340))
        row = ('<tr><th><a href="https://www.jobapscloud.com/DE/sup/bulpreview.asp?b=&amp;R1=PR0000&amp;R2=000000&amp;R3=010101" class="JobTitle">'
               'Practice Application</a></th></tr>')
        self.assertEqual(g.parse_jobaps_results(row, "delaware"), [])

    def test_pay_text_is_made_yearly(self):
        self.assertEqual(g._jobaps_pay("$55,710.00 - $57,961.00/ year with the potential to grow"), (55710, 57961))
        self.assertEqual(g._jobaps_pay("$30.00 - $40.00 / hour"), (62400, 83200))
        self.assertEqual(g._jobaps_pay("$5,000 - $6,000/ month"), (60000, 72000))
        self.assertEqual(g._jobaps_pay("Salary varies"), (None, None))

    def test_layout_change_yields_nothing_instead_of_garbage(self):
        self.assertEqual(g.parse_jobaps_results("<html><body><p>Maintenance</p></body></html>", "maryland"), [])
        self.assertEqual(g.parse_jobaps_results('<tr><td><a class="JobTitle" href="/x">new layout</a></td></tr>', "maryland"), [])

    def test_fetch_reads_the_states_home_page(self):
        seen = []

        class Resp:
            content = CT_FIXTURE.read_bytes()
            def raise_for_status(self): pass

        with mock.patch.object(g.requests, "get", lambda url, **k: seen.append(url) or Resp()):
            self.assertEqual(len(g.fetch_connecticut(["ignored"], 5, 0)), 3)
        self.assertEqual(seen, ["https://www.jobapscloud.com/CT/"])

    def test_detail_reads_the_posting_data(self):
        page = (b'<html><script type="application/ld+json">{"@type":"JobPosting","title":"Dev","datePosted":"2026-10-27",'
                b'"description":"<p>Introduction</p><p>\\r\\nBuild <b>things</b>.\\r\\n\xef\xbf\xbd</p>",'
                b'"baseSalary":{"value":{"minValue":60000,"maxValue":80000,"unitText":"YEAR"}}}</script></html>')

        class Resp:
            content = page
            def raise_for_status(self): pass

        job = sources.Job("md:1", "maryland", "Maryland X", "Dev", "Anne Arundel, MD", "https://www.jobapscloud.com/MD/sup/bulpreview.asp?R1=1&R2=2&R3=3")
        with mock.patch.object(g.requests, "get", lambda *a, **k: Resp()):
            sources.fill_detail(job)
        self.assertIn("Build **things**.", job.description)
        self.assertNotIn("\\r", job.description)
        self.assertNotIn("\ufffd", job.description)
        self.assertEqual((job.posted, job.pay_min, job.pay_max), ("2026-10-27", 60000, 80000))

    def test_detail_with_unknown_layout_raises_so_the_job_is_skipped(self):
        class Resp:
            content = b"<html>nothing here</html>"
            def raise_for_status(self): pass

        job = sources.Job("md:1", "maryland", "c", "t", "l", "https://www.jobapscloud.com/MD/sup/bulpreview.asp?R1=1&R2=2&R3=3")
        with mock.patch.object(g.requests, "get", lambda *a, **k: Resp()):
            with self.assertRaises(ValueError):
                sources.fill_detail(job)

    def test_registered_for_detail_fetching_and_government_titles(self):
        for source in ("maryland", "connecticut", "delaware"):
            self.assertIn(source, sources.DETAIL_FILLERS)
            self.assertIn(source, g.GOV_SOURCES)


class NewYorkTests(unittest.TestCase):
    def test_parse_real_table_rows(self):
        jobs = g.parse_ny_results(NY_FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(len(jobs), 3)
        j = jobs[0]
        self.assertEqual((j.id, j.source, j.title), ("ny:228039", "new_york", "Senior Superintendent of Construction - NY HELPS"))
        self.assertEqual((j.company, j.location, j.posted), ("New York Department of Environmental Conservation", "Albany, NY", "2026-10-30"))
        self.assertEqual(j.url, "https://statejobsny.com/public/vacancyDetailsView.cfm?id=228039")
        self.assertEqual(j.detail, {"grade": "19", "deadline": "2026-11-18"})
        self.assertEqual(jobs[1].company, "New York State Education Department")

    def test_agency_names_read_the_right_way_round(self):
        self.assertEqual(g._ny_agency("Attorney General, Office of the"), "New York Office of the Attorney General")
        self.assertEqual(g._ny_agency("Education Department, State"), "New York State Education Department")
        self.assertEqual(g._ny_agency("Port Authority of New York"), "Port Authority of New York")
        self.assertEqual(g._ny_agency("Thruway Authority"), "New York Thruway Authority")

    def test_layout_change_yields_nothing_instead_of_garbage(self):
        self.assertEqual(g.parse_ny_results("<html><body><p>Maintenance</p></body></html>"), [])
        self.assertEqual(g.parse_ny_results('<tr class="odd"><td>1</td><td>new layout</td></tr>'), [])

    def test_fetch_reads_the_one_page(self):
        seen = []

        class Resp:
            text = NY_FIXTURE.read_text(encoding="utf-8")
            def raise_for_status(self): pass

        with mock.patch.object(g.requests, "get", lambda url, **k: seen.append(url) or Resp()):
            jobs = g.fetch_new_york(["ignored"], pages=5, delay=0)
        self.assertEqual((len(jobs), seen), (3, ["https://statejobsny.com/public/vacancyTable.cfm"]))

    def test_detail_fills_description_and_pay(self):
        page = ('<p class="row"><span class="leftCol">Salary Range</span><span class="rightCol">From $90582 to $114585 Annually</span></p>'
                '<p class="row"><span class="leftCol"> Duties Description </span><span class="rightCol">Run the <b>cloud</b> team.<br />Other duties.</span></p>'
                '<p class="row"><span class="leftCol"> Minimum Qualifications </span><span class="rightCol">Two years of experience.</span></p>')

        class Resp:
            text = page
            def raise_for_status(self): pass

        job = sources.Job("ny:1", "new_york", "New York DOT", "IT Specialist 3", "Albany, NY", "https://statejobsny.com/public/vacancyDetailsView.cfm?id=1")
        with mock.patch.object(g.requests, "get", lambda *a, **k: Resp()):
            sources.fill_detail(job)
        self.assertIn("Run the **cloud** team.", job.description)
        self.assertIn("Two years of experience.", job.description)
        self.assertEqual((job.pay_min, job.pay_max), (90582, 114585))

    def test_detail_with_unknown_layout_raises_so_the_job_is_skipped(self):
        class Resp:
            text = "<html>nothing here</html>"
            def raise_for_status(self): pass

        job = sources.Job("ny:1", "new_york", "c", "t", "l", "https://statejobsny.com/public/vacancyDetailsView.cfm?id=1")
        with mock.patch.object(g.requests, "get", lambda *a, **k: Resp()):
            with self.assertRaises(ValueError):
                sources.fill_detail(job)

    def test_registered_for_detail_fetching_and_government_titles(self):
        self.assertIn("new_york", sources.DETAIL_FILLERS)
        self.assertIn("new_york", g.GOV_SOURCES)


class CollectGovernmentTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        for k in ("USAJOBS_API_KEY", "USAJOBS_EMAIL"):
            os.environ.pop(k, None)
        self.no_env_file = mock.patch.object(main, "load_env")   # never read the developer's real .env in tests
        self.no_env_file.start()
        self.no_network = [mock.patch.object(g, fetch, return_value=[]) for _, _, fetch, _ in g.STATE_SOURCES]   # a test patches the ones it cares about
        for patch in self.no_network:
            patch.start()

    def tearDown(self):
        for patch in self.no_network:
            patch.stop()
        self.no_env_file.stop()
        self.env.stop()

    def test_both_on_by_default_and_usajobs_skipped_without_a_key(self):
        job = sources.Job("tx:1", "texas_hhs", "Texas DFPS", "Java Developer", "AUSTIN, TX", "u")
        with mock.patch.object(g, "fetch_texas_hhs", return_value=[job]) as tx, mock.patch.object(g, "fetch_usajobs") as uj,                 mock.patch.object(g, "fetch_florida", return_value=[]) as fl, \
                mock.patch.object(g, "fetch_vermont", return_value=[]) as vt, mock.patch.object(g, "fetch_new_york", return_value=[]) as ny:
            self.assertEqual(main.collect_government({"request_delay": 0}), [job])
            uj.assert_not_called()
            tx.assert_called_once()
            fl.assert_called_once()
            vt.assert_called_once()
            ny.assert_called_once()

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
                mock.patch.object(g, "fetch_texas_hhs", side_effect=RuntimeError("timeout")),                 mock.patch.object(g, "fetch_florida", side_effect=RuntimeError("timeout")), \
                mock.patch.object(g, "fetch_new_york", side_effect=RuntimeError("timeout")):
            self.assertEqual(main.collect_government({"request_delay": 0}), [])

    def test_every_source_can_be_turned_off(self):
        cfg = {"government": gov_off()}
        with mock.patch.object(g, "fetch_usajobs") as uj, mock.patch.object(g, "fetch_texas_hhs") as tx, \
                mock.patch.object(g, "fetch_florida") as fl, mock.patch.object(g, "fetch_vermont") as vt, mock.patch.object(g, "fetch_new_york") as ny, \
                mock.patch.object(g, "fetch_maryland") as md, mock.patch.object(g, "fetch_connecticut") as ct:
            self.assertEqual(main.collect_government(cfg), [])
            for fetch in (uj, tx, fl, vt, ny, md, ct):
                fetch.assert_not_called()


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

    def test_the_example_config_has_government_titles_and_sources_on(self):
        import yaml
        cfg = yaml.safe_load((Path(__file__).parent.parent / "config.example.yaml").read_text(encoding="utf-8"))
        self.assertIn("Programmer", cfg["gov_titles"])
        self.assertTrue(cfg["government"]["usajobs"]["enabled"])
        self.assertTrue(cfg["government"]["texas_hhs"]["enabled"])
        self.assertTrue(cfg["government"]["florida"]["enabled"])
        self.assertTrue(cfg["government"]["vermont"]["enabled"])
        self.assertTrue(cfg["government"]["new_york"]["enabled"])
        self.assertTrue(cfg["government"]["maryland"]["enabled"])
        self.assertTrue(cfg["government"]["connecticut"]["enabled"])
        self.assertTrue(cfg["government"]["delaware"]["enabled"])


if __name__ == "__main__":
    unittest.main()
