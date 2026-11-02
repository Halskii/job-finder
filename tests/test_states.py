"""State job sites read from saved copies of the real pages and responses in tests/data: Arizona, Indiana, Arkansas, Illinois,
Missouri, Wisconsin, Massachusetts, Montana, Utah, Kentucky, South Dakota, California, Kansas, Minnesota, North Dakota and Tennessee. (Florida, Vermont, Maryland, Connecticut, Delaware and New York are in test_gov.py.)"""
import json
import unittest
from pathlib import Path
from unittest import mock

import gov_sources as g
import sources

DATA = Path(__file__).parent / "data"
text = lambda name: (DATA / name).read_text(encoding="utf-8")
blob = lambda name: (DATA / name).read_bytes()


class Resp:
    """A stand-in for requests' answer: the page text, its bytes and its JSON."""

    def __init__(self, body):
        self.content = body if isinstance(body, bytes) else body.encode("utf-8")
        self.text = self.content.decode("utf-8", errors="replace")

    def raise_for_status(self):
        pass

    def json(self):
        return json.loads(self.text)


class Registered(unittest.TestCase):
    def test_every_state_source_is_a_government_source_with_a_detail_reader_and_a_fetcher(self):
        listed_in_full = {"utah", "kentucky", "south_dakota", "north_dakota"}      # their lists carry the text, so there is no job page to read
        for key, label, fetch, words in g.STATE_SOURCES:
            self.assertIn(key, g.GOV_SOURCES, key)
            self.assertTrue(callable(getattr(g, fetch)), fetch)
            self.assertEqual(key in sources.DETAIL_FILLERS, key not in listed_in_full, key)


class SuccessFactorsStates(unittest.TestCase):
    def test_indiana_rows_ignore_the_pay_column_and_get_a_state_name(self):
        jobs = g.parse_indiana_results(text("indiana_results.html"))
        self.assertEqual(len(jobs), 3)
        j = jobs[0]
        self.assertEqual((j.id, j.source, j.company, j.location, j.posted), ("in:1427021700", "indiana", "State of Indiana", "Indianapolis, IN", "2026-11-01"))
        self.assertTrue(j.url.startswith("https://workforindiana.in.gov/job/"))

    def test_arkansas_rows_lose_the_state_letters_in_front_of_agency_names(self):
        jobs = g.parse_arkansas_results(text("arkansas_results.html"))
        self.assertEqual(len(jobs), 3)
        self.assertEqual((jobs[0].id, jobs[0].company, jobs[0].location), ("ar:1416688600", "Arkansas SCHOOL FOR THE DEAF & BLIND", "Little Rock, AR"))
        self.assertEqual(jobs[1].company, "Arkansas DEPT OF HUMAN SERVICES")

    def test_illinois_tiles(self):
        jobs = g.parse_illinois_results(text("illinois_results.html"))
        self.assertEqual(len(jobs), 3)
        j = jobs[0]
        self.assertEqual((j.id, j.title, j.company, j.location, j.posted), ("il:1436335700", "ACTIVITY THERAPIST (UMP)", "Illinois Department of Human Services",
                                                                          "Chester, IL", "2026-10-31"))
        self.assertTrue(j.url.startswith("https://illinois.jobs2web.com/job/Chester-"))

    def test_a_layout_change_yields_nothing(self):
        for parse in (g.parse_indiana_results, g.parse_arkansas_results, g.parse_illinois_results):
            self.assertEqual(parse("<html><body><p>Maintenance</p></body></html>"), [])
        self.assertEqual(g.parse_illinois_results('<li class="job-tile"><p>new layout</p></li>'), [])

    def test_each_is_fetched_from_its_own_site(self):
        for fetch, host in ((g.fetch_indiana, "https://workforindiana.in.gov/search/"), (g.fetch_arkansas, "https://arcareers.arkansas.gov/search/"),
                            (g.fetch_illinois, "https://illinois.jobs2web.com/search/")):
            seen = []
            with mock.patch.object(g.requests, "get", lambda url, **k: seen.append(url) or Resp("<html></html>")):
                self.assertEqual(fetch(["developer"], pages=2, delay=0), [])
            self.assertEqual(seen, [host])


class Arizona(unittest.TestCase):
    def test_rows(self):
        jobs = g.parse_arizona_results(text("arizona_results.html"))
        self.assertEqual(len(jobs), 3)
        j = jobs[0]
        self.assertEqual((j.title, j.company, j.location, j.detail), ("CASE MANAGER 2", "Arizona Dept Of Economic Security", "Peoria, AZ, Surprise, AZ", {"deadline": "2026-11-30"}))
        self.assertTrue(j.id.startswith("az:case-manager-2-peoria"))

    def test_a_posting_address_that_does_not_end_in_a_code_still_gets_an_id(self):
        row = ('<tr data-job-url="https://www.azstatejobs.gov/jobs/correctional-officer-iii-globe-globe-arizona-united-states">'
               '<td class="job-search-results-title"><a href="x"> CORRECTIONAL OFFICER III - GLOBE </a></td></tr>')
        self.assertEqual([j.id for j in g.parse_arizona_results(row)], ["az:correctional-officer-iii-globe-globe-arizona-united-states"])

    def test_pages_are_walked_with_the_sites_five_second_delay(self):
        full = text("arizona_results.html")
        seen, sleeps = [], []
        with mock.patch.object(g, "AZ_PAGE", 3), mock.patch.object(g.requests, "get", lambda url, **k: seen.append(k["params"]["page"]) or Resp(full)), \
                mock.patch.object(g.time, "sleep", sleeps.append):
            jobs = g.fetch_arizona(pages=5, delay=0.4)
        self.assertEqual(len(jobs), 3)                       # every page had the same three, deduped; a full page means "go on"
        self.assertEqual(seen, [1, 2, 3, 4, 5])
        self.assertTrue(sleeps and all(s >= 5 for s in sleeps))

    def test_detail_reads_the_posting_data_and_the_salary_in_it(self):
        page = (b'<script type="application/ld+json">{"@type":"JobPosting","datePosted":"2026-11-01T16:12:39Z",'
                b'"description":"<p>Posting Details: Salary: $49,000 - $51,000 Grade: 17</p><p>Help people.</p>"}</script>')
        job = sources.Job("az:x", "arizona", "Arizona DES", "Case Manager", "Peoria, AZ", "https://www.azstatejobs.gov/jobs/x")
        with mock.patch.object(g.requests, "get", lambda *a, **k: Resp(page)):
            sources.fill_detail(job)
        self.assertEqual((job.posted, job.pay_min, job.pay_max), ("2026-11-01", 49000, 51000))
        self.assertIn("Help people.", job.description)


class Missouri(unittest.TestCase):
    def test_open_postings_are_kept_and_closed_or_old_ones_are_not(self):
        jobs = g.parse_missouri_results(json.loads(text("missouri_results.json")), today="2026-11-01")
        self.assertEqual(len(jobs), 2)                       # four in the file: two open, one closed, one old with no closing date
        j = jobs[0]
        self.assertTrue(j.id.startswith("mo:"))
        self.assertTrue(j.company.startswith("Missouri") or "Missouri" in j.company)
        self.assertTrue(j.url.startswith("https://mocareers.mo.gov/hiretrue/ce3/job-board/5effe9b2-"))

    def test_fetch_asks_for_the_board_then_its_postings(self):
        items = json.loads(text("missouri_results.json"))
        seen = []

        def fake_get(url, **k):
            seen.append(url)
            return Resp(json.dumps({"primaryKey": 417} if url.endswith("/job-board") else items))

        with mock.patch.object(g.requests, "get", fake_get):
            g.fetch_missouri()
        self.assertEqual([u.rsplit("/api/ce3/", 1)[1] for u in seen], ["job-board", "job-board/requisitions"])

    def test_detail_joins_the_sections_and_reads_the_salary(self):
        body = {"salary": "$83,172.96-$86,655.92", "salaryType": "perYear", "descriptionSection1": "<p>Nurse the <b>students</b>.</p>",
                "descriptionSection2": "<p>Second part.</p>"}
        job = sources.Job("mo:5", "missouri", "Missouri DESE", "Nurse", "Kansas City, MO", "u")
        with mock.patch.object(g.requests, "get", lambda *a, **k: Resp(json.dumps(body))):
            sources.fill_detail(job)
        self.assertIn("Nurse the **students**.", job.description)
        self.assertIn("Second part.", job.description)
        self.assertEqual((job.pay_min, job.pay_max), (83172, 86655))


class Wisconsin(unittest.TestCase):
    def test_grid_data_in_the_page(self):
        jobs = g.parse_wisconsin_results(text("wisconsin_results.html"))
        self.assertEqual(len(jobs), 3)
        j = jobs[0]
        self.assertEqual((j.id, j.title, j.company, j.location, j.posted), ("wi:22514", "Specialized Learning Coordinator", "Wisconsin Public Instruction",
                                                                         "Delavan, WI", "2026-10-30"))
        self.assertIn("JobOpeningId=22514", j.url)

    def test_a_layout_change_yields_nothing(self):
        self.assertEqual(g.parse_wisconsin_results("<html>no grid here</html>"), [])
        self.assertEqual(g.parse_wisconsin_results("data: [not json"), [])

    def test_detail_collects_headed_sections_and_the_salary(self):
        page = ("<h2><span id='HRS_SCH_WRK_DESCR100$1lbl'>Introduction</span></h2>"
                "<span class='ps_box-value' id=HRS_SCH_PSTDSC_DESCRLONG$1 ><p>Help <b>schools</b>.</p></span></DIV>"
                "<h2><span id='HRS_SCH_WRK_DESCR100$2lbl'>Salary Information</span></h2>"
                "<span class='ps_box-value' id=HRS_SCH_PSTDSC_DESCRLONG$2 ><p>$60,000 - $70,000 annually</p></span></DIV>")
        job = sources.Job("wi:1", "wisconsin", "Wisconsin DPI", "Coordinator", "Madison, WI", "u")
        with mock.patch.object(g.requests, "get", lambda *a, **k: Resp(page)):
            sources.fill_detail(job)
        self.assertIn("**Introduction**", job.description)
        self.assertIn("Help **schools**.", job.description)
        self.assertEqual((job.pay_min, job.pay_max), (60000, 70000))


class Massachusetts(unittest.TestCase):
    def test_search_results(self):
        jobs = g.parse_massachusetts_results(json.loads(text("massachusetts_results.json")))
        self.assertEqual(len(jobs), 3)
        j = jobs[0]
        self.assertEqual((j.id, j.company, j.location, j.posted), ("ma:1094701", "State of Massachusetts", "Salem, MA", "2026-10-30"))
        self.assertEqual(j.url, "https://massanf.taleo.net/careersection/ex/jobdetail.ftl?job=260007M2&lang=en")
        self.assertEqual(j.detail["category"], "")

    def test_fetch_pages_until_the_total_is_reached(self):
        data = json.loads(text("massachusetts_results.json"))
        data["pagingData"] = {"currentPageNo": 1, "pageSize": 3, "totalCount": 5}
        seen = []

        class Session:
            headers = type("H", (), {"update": lambda self, x: None})()
            def get(self, url, **k): return Resp("")
            def post(self, url, **k):
                seen.append(json.loads(k["data"])["pageNo"])
                return Resp(json.dumps(data))

        with mock.patch.object(g.requests, "Session", Session), mock.patch.object(g.time, "sleep"):
            g.fetch_massachusetts(delay=0)
        self.assertEqual(seen, [1, 2])                        # 3 per page and 5 in all

    def test_montana_runs_on_the_same_reader_and_names_the_department(self):
        jobs = g.parse_taleo_results(json.loads(text("montana_results.json")), "montana")
        self.assertEqual(len(jobs), 3)
        j = jobs[0]
        self.assertEqual((j.id.split(":")[0], j.source, j.company, j.location), ("mt", "montana", "Montana Department of Public Health & Human Services", "Multiple Locations"))
        self.assertIn("/careersection/200/jobdetail.ftl?job=", j.url)
        self.assertIsNone(j.posted)                           # Montana's third column is the department, not a date

    def test_detail_reads_the_text_the_pay_and_the_date(self):
        page = ("<script>api.fillList('requisitionDescriptionInterface', 'descRequisition', ['1','Title','260007M2',"
                "'!*!%3Cp%3EDo%20%3Cb%3Egood%3C%2Fb%3E%20work.%3C%2Fp%3E','Education','Oct 30, 2026, 2:23:47 PM','1','85,000.00','80,000.00','85,000.00','Yearly','Jo']);</script>")
        job = sources.Job("ma:1", "massachusetts", "State of Massachusetts", "Title", "Salem, MA", "u")
        with mock.patch.object(g.requests, "get", lambda *a, **k: Resp(page)):
            sources.fill_detail(job)
        self.assertIn("Do **good** work.", job.description)
        self.assertEqual((job.pay_min, job.pay_max, job.posted), (80000, 85000, "2026-10-30"))

    def test_detail_with_unknown_layout_raises_so_the_job_is_skipped(self):
        job = sources.Job("ma:1", "massachusetts", "c", "t", "l", "u")
        with mock.patch.object(g.requests, "get", lambda *a, **k: Resp("<html>nothing</html>")):
            with self.assertRaises(ValueError):
                sources.fill_detail(job)


class Cornerstone(unittest.TestCase):
    def test_utah_and_kentucky_requisitions(self):
        ut = g.parse_csod_results(json.loads(text("utah_results.json")), "utah")
        self.assertEqual(len(ut), 3)
        j = ut[0]
        self.assertEqual((j.id, j.title, j.company, j.location, j.posted), ("ut:3039", "Correctional Officer", "State of Utah", "Ogden, UT", "2026-11-01"))
        self.assertEqual(j.url, "https://utdgohcm.csod.com/ux/ats/careersite/4/home/requisition/3039?c=utdgohcm")
        self.assertTrue(len(j.description) > 100)             # Utah's list carries the text
        ky = g.parse_csod_results(json.loads(text("kentucky_results.json")), "kentucky")
        self.assertEqual((ky[0].id, ky[0].location), ("ky:82348", "La Grange, KY"))

    def test_fetch_takes_the_token_from_the_page_and_waits_between_requests(self):
        data = json.loads(text("utah_results.json"))
        data["data"]["totalCount"] = 5
        calls, sleeps = [], []

        def fake_get(url, **k):
            calls.append(("GET", url))
            return Resp('<script>csod.context={"token":"abc.def"}</script>')

        def fake_post(url, **k):
            calls.append(("POST", k["headers"]["Authorization"], json.loads(k["data"])["pageNumber"]))
            return Resp(json.dumps(data))

        with mock.patch.object(g.requests, "get", fake_get), mock.patch.object(g.requests, "post", fake_post), mock.patch.object(g.time, "sleep", sleeps.append):
            jobs = g.fetch_utah(pages=5, delay=0.4)
        self.assertEqual(len(jobs), 3)
        self.assertEqual([c for c in calls if c[0] == "POST"], [("POST", "Bearer abc.def", 1), ("POST", "Bearer abc.def", 2)])    # 3 of 5 on page 1, then page 2 adds no new ones
        self.assertTrue(sleeps and all(s >= 10 for s in sleeps))

    def test_a_page_without_a_token_is_an_error_not_an_empty_list(self):
        with mock.patch.object(g.requests, "get", lambda *a, **k: Resp("<html>no token</html>")):
            with self.assertRaises(ValueError):
                g.fetch_kentucky(delay=0)


class SouthDakota(unittest.TestCase):
    def test_double_encoded_board_json(self):
        raw = json.loads(text("south_dakota_results.json"))           # a JSON string holding JSON, as the board sends it
        self.assertIsInstance(raw, str)
        jobs = g.parse_south_dakota_results(raw)
        self.assertEqual(len(jobs), 3)
        self.assertTrue(jobs[0].id.startswith("sd:"))
        self.assertTrue(jobs[0].company.startswith("South Dakota "))
        self.assertTrue(jobs[0].description)
        self.assertEqual(g.parse_south_dakota_results(json.loads(raw)), jobs)       # an already-decoded answer works too

    def test_nothing_usable_yields_nothing(self):
        for junk in ("", "not json", "[]", {}, {"JobList": [{"JobTitle": "x", "JobURL": "no number"}]}):
            self.assertEqual(g.parse_south_dakota_results(junk), [], junk)


class California(unittest.TestCase):
    def test_cards(self):
        jobs = g.parse_california_results(text("california_results.html"))
        self.assertEqual(len(jobs), 3)
        j = jobs[0]
        self.assertEqual((j.id, j.title, j.company, j.posted), ("ca:533177", "FIRE FIGHTER I", "California Department of Forestry & Fire Protection", "2026-11-01"))
        self.assertEqual((j.pay_min, j.pay_max), (55080, 69636))      # $4590 - $5803 a month
        self.assertEqual(j.detail["deadline"], "2026-11-30")
        self.assertEqual(j.url, "https://www.calcareers.ca.gov/CalHrPublic/Jobs/JobPosting.aspx?JobControlId=533177")

    def test_pay_is_monthly_unless_it_looks_hourly(self):
        self.assertEqual(g._ca_pay("$4590.00 - $5803.00"), (55080, 69636))
        self.assertEqual(g._ca_pay("$30.00 - $40.00"), (62400, 83200))
        self.assertEqual(g._ca_pay("Pay varies"), (None, None))

    def test_hidden_fields_are_what_the_next_postback_sends(self):
        page = '<input type="hidden" name="__VIEWSTATE" id="a" value="x&amp;y" /><input type="text" name="q" value="no" /><input type="hidden" name="b" />'
        self.assertEqual(g._hidden_fields(page), {"__VIEWSTATE": "x&y", "b": ""})

    def test_fetch_walks_the_form_and_the_pager_until_a_short_page(self):
        full = text("california_results.html")
        posts = []

        class Session:
            headers = type("H", (), {"update": lambda self, x: None})()
            def get(self, url, **k): return Resp('<input type="hidden" name="__VIEWSTATE" value="v0" />')
            def post(self, url, data, **k):
                posts.append({k2: v for k2, v in data.items() if k2 != "__VIEWSTATE"})
                return Resp(full + '<input type="hidden" name="__VIEWSTATE" value="v1" />')

        with mock.patch.object(g.requests, "Session", Session), mock.patch.object(g.time, "sleep"):
            jobs = g.fetch_california(delay=0)
        self.assertEqual(len(jobs), 3)
        self.assertIn("ctl00$cphMainContent$btnUpdateResults", posts[0])           # step 1: "Update Results" with nothing typed
        self.assertEqual(posts[1]["ctl00$cphMainContent$ddlRowCount"], "100")      # step 2: 100 to a page
        self.assertEqual(len(posts), 2)                                            # 3 jobs is a short page, so no pager click

    def test_detail_reads_the_position_text_and_the_work_location(self):
        page = ("<html><body><h2>Position Details</h2><p><b>Work Location:</b></p><p>Sacramento County</p><p>Duties include investigations of "
                + "licensees. " * 30 + "</p><h2>Application Instructions</h2><p>Mail it.</p></body></html>")
        job = sources.Job("ca:1", "california", "California DCA", "Investigator", "California", "https://www.calcareers.ca.gov/CalHrPublic/Jobs/JobPosting.aspx?JobControlId=1")
        with mock.patch.object(g.requests, "get", lambda *a, **k: Resp(page)):
            sources.fill_detail(job)
        self.assertIn("Duties include investigations", job.description)
        self.assertNotIn("Mail it", job.description)
        self.assertEqual(job.location, "Sacramento County, CA")

    def test_detail_with_unknown_layout_raises_so_the_job_is_skipped(self):
        job = sources.Job("ca:1", "california", "c", "t", "l", "u")
        with mock.patch.object(g.requests, "get", lambda *a, **k: Resp("<html>nothing</html>")):
            with self.assertRaises(ValueError):
                sources.fill_detail(job)


class PeopleSoft(unittest.TestCase):
    def test_kansas_rows_skip_internal_postings(self):
        jobs = g.parse_peoplesoft_results(text("kansas_results.html"), "kansas")
        self.assertEqual(len(jobs), 2)                       # three rows in the file; one is for state employees only
        j = jobs[0]
        self.assertEqual((j.id, j.company, j.location, j.posted), ("ks:223521", "Kansas Lansing Correctional Facility", "Leavenworth County, KS", "2026-11-01"))
        self.assertEqual(j.detail["deadline"], "2026-11-30")
        self.assertTrue(j.url.endswith("Page=HRS_APP_JBPST_FL&Action=U&FOCUS=Applicant&SiteId=1&JobOpeningId=223521&PostingSeq=1"))

    def test_minnesota_rows(self):
        jobs = g.parse_peoplesoft_results(text("minnesota_results.html"), "minnesota")
        self.assertEqual(len(jobs), 2)
        self.assertEqual((jobs[0].id, jobs[0].company, jobs[0].location), ("mn:97894", "Minnesota Administration Dept", "St. Paul, MN"))
        self.assertIn("SiteId=1001", jobs[0].url)

    def test_a_layout_change_yields_nothing(self):
        self.assertEqual(g.parse_peoplesoft_results("<html><body>nothing</body></html>", "kansas"), [])

    def test_fetch_presses_the_more_button_and_sends_back_the_forms_state(self):
        first = text("kansas_results.html")
        more = first.replace("value='1'", "value='2'").replace("</ul>", "<li class='ps_grid-row'><div><span id='SCH_JOB_TITLE$3'>Chef</span>"
                                                                 "<span id='HRS_APP_JBSCH_I_HRS_JOB_OPENING_ID$3'>5</span></div></li></ul>").replace("HRS_AGNT_RSLT_I$hdown$0", "gone")
        sent = []

        class Session:
            headers = type("H", (), {"update": lambda self, x: None})()
            def get(self, url, **k): return Resp(first)
            def post(self, url, data, **k):
                sent.append((data["ICStateNum"], data["ICAction"]))
                return Resp(more)

        with mock.patch.object(g.requests, "Session", Session), mock.patch.object(g.time, "sleep"):
            jobs = g.fetch_kansas(delay=0)
        self.assertEqual(sorted(j.id for j in jobs), ["ks:223046", "ks:223521", "ks:5"])
        self.assertEqual(sent, [("1", "HRS_AGNT_RSLT_I$hdown$0")])          # the state number from the page, and the "more" action; then no button, so it stops

    def test_minnesota_runs_an_empty_search_first_and_keeps_to_the_hours_its_robots_txt_asks_for(self):
        page = text("minnesota_results.html").replace("more</a>", "</a>").replace("HRS_AGNT_RSLT_I$hdown$0", "gone")
        sent = []

        class Session:
            headers = type("H", (), {"update": lambda self, x: None})()
            def get(self, url, **k): return Resp(text("minnesota_results.html"))
            def post(self, url, data, **k):
                sent.append(data["ICAction"])
                return Resp(page)

        with mock.patch.object(g, "PS_VISIT_HOURS", {"minnesota": (0, 24)}), mock.patch.object(g.requests, "Session", Session), mock.patch.object(g.time, "sleep"):
            self.assertEqual(len(g.fetch_minnesota(delay=0)), 2)
        self.assertEqual(sent, ["HRS_SCH_WRK_FLU_HRS_SEARCH_BTN"])
        with mock.patch.object(g, "PS_VISIT_HOURS", {"minnesota": (0, 0)}), mock.patch.object(g.requests, "Session") as session:
            with self.assertRaises(RuntimeError):
                g.fetch_minnesota(delay=0)
            session.assert_not_called()                       # outside those hours it does not even visit

    def test_the_posting_page_is_read_like_wisconsins(self):
        page = ("<h2><span id='HRS_SCH_WRK_DESCR100$1lbl'>Duties</span></h2><span class='ps_box-value' id=HRS_SCH_PSTDSC_DESCRLONG$1 ><p>Guard the "
                "<b>gate</b>.</p></span></DIV><h2><span id='HRS_SCH_WRK_DESCR100$2lbl'>Salary</span></h2>"
                "<span class='ps_box-value' id=HRS_SCH_PSTDSC_DESCRLONG$2 ><p>$24.40 - $35.30 / hourly</p></span></DIV>")
        job = sources.Job("mn:1", "minnesota", "Minnesota X", "Guard", "St. Paul, MN", "u")
        with mock.patch.object(g.requests, "get", lambda *a, **k: Resp(page)):
            sources.fill_detail(job)
        self.assertIn("Guard the **gate**.", job.description)
        self.assertEqual((job.pay_min, job.pay_max), (50752, 73424))


class NorthDakota(unittest.TestCase):
    def test_feed_nodes_become_jobs_with_the_facts_as_text_and_no_contact_details(self):
        jobs = g.parse_north_dakota_results(text("north_dakota_feed.xml"), today="2026-11-01")
        self.assertEqual(len(jobs), 2)                       # three in the file; one closed before the day given
        j = jobs[0]
        self.assertTrue(j.id.startswith("nd:") and j.company.startswith("North Dakota "))
        self.assertIn(j.title, j.description)
        self.assertNotIn("@", j.description)                 # the feed's contact emails are personal addresses
        self.assertTrue(j.url.startswith("https://www.cnd.nd.gov/"))        # a link for the person to follow; the program never reads it
        self.assertIsNotNone(j.pay_min)

    def test_pay_by_the_month_is_made_yearly(self):
        xml = ("<Root><Node><Action>ADD</Action><JobId>1</JobId><JobTitle>Nurse</JobTitle><CloseDate>2027-01-01</CloseDate><PayFreq>M</PayFreq>"
               "<MinRate>6367</MinRate><MaxRate>8490</MaxRate><City>GRAFTON</City><State>ND</State></Node></Root>")
        j = g.parse_north_dakota_results(xml, today="2026-11-01")[0]
        self.assertEqual((j.pay_min, j.pay_max, j.location), (76404, 101880, "Grafton, ND"))

    def test_junk_yields_nothing(self):
        self.assertEqual(g.parse_north_dakota_results("<html>not a feed"), [])
        self.assertEqual(g.parse_north_dakota_results("<Root><Node><Action>ADD</Action></Node></Root>"), [])


class Tennessee(unittest.TestCase):
    def test_report_rows(self):
        jobs = g.parse_tennessee_results(text("tennessee_results.html"))
        self.assertEqual(len(jobs), 3)
        j = jobs[0]
        self.assertEqual((j.id, j.title, j.company, j.location), ("tn:82395", "CERTIFIED PHARMACY TECHNICIAN", "Tennessee Mental Health", "Shelby County, TN"))
        self.assertEqual((j.detail["deadline"], j.detail["category"]), ("2026-11-05", "Health Care - Licensed"))
        self.assertEqual(j.url, "https://careers.edison.tn.gov/psc/hrprdrs/EMPLOYEE/HRMS/c/HRS_HRAM_FL.HRS_CG_SEARCH_FL.GBL?FOCUS=Applicant&Page=HRS_APP_JBPST_FL&SiteId=1&JobOpeningId=82395&PostingSeq=1")

    def test_a_layout_change_yields_nothing(self):
        self.assertEqual(g.parse_tennessee_results("<html><body>nothing</body></html>"), [])

    def test_fetch_sends_the_pages_values_back_for_each_further_page_and_stops_without_a_next_link(self):
        sent = []

        class Session:
            headers = type("H", (), {"update": lambda self, x: None})()
            def get(self, url, **k): return Resp(text("tennessee_results.html"))
            def post(self, url, data, **k):
                sent.append((url, data))
                return Resp(text("tennessee_results_page2.html"))

        with mock.patch.object(g.requests, "Session", Session), mock.patch.object(g.time, "sleep"):
            jobs = g.fetch_tennessee(delay=0)
        self.assertEqual(len(jobs), 5)                        # three on the page, two more from the report's paging request
        self.assertEqual(len(sent), 1)
        url, data = sent[0]
        self.assertEqual(url, "https://hub.edison.tn.gov/ords/wwv_flow.ajax")
        self.assertEqual((data["p_flow_id"], data["p_flow_step_id"], data["p_instance"], data["p_pg_min_row"], data["p_pg_max_rows"]), ("106", "2", "29760311233070", "51", "50"))
        self.assertTrue(data["p_request"].startswith("PLUGIN="))
        self.assertIn("/DWxTr_sI6UiUxEf0pF", data["p_json"])      # the protected value is sent unescaped

    def test_a_search_page_without_the_report_is_an_error_not_an_empty_list(self):
        class Session:
            headers = type("H", (), {"update": lambda self, x: None})()
            def get(self, url, **k): return Resp("<html>Maintenance</html>")

        with mock.patch.object(g.requests, "Session", Session):
            with self.assertRaises(ValueError):
                g.fetch_tennessee(delay=0)

    def test_the_posting_page_gives_the_open_date_and_the_yearly_pay(self):
        page = ("<h2><span id='HRS_SCH_WRK_DESCR100$1lbl'>Job Information</span></h2><span class='ps_box-value' id=HRS_SCH_PSTDSC_DESCRLONG$1 >"
                "<table><tr><td>Opening Date/Time</td><td>10/30/2026 12:00AM Central Time</td></tr><tr><td>Salary (Monthly)</td><td>$2,925.00 - $4,374.00</td></tr>"
                "<tr><td>Salary (Annually)</td><td>$35,100.00 - $52,488.00</td></tr></table></span></DIV>")
        job = sources.Job("tn:1", "tennessee", "Tennessee Mental Health", "Technician", "Shelby County, TN", "u")
        with mock.patch.object(g.requests, "get", lambda *a, **k: Resp(page)):
            sources.fill_detail(job)
        self.assertEqual((job.posted, job.pay_min, job.pay_max), ("2026-10-30", 35100, 52488))


if __name__ == "__main__":
    unittest.main()
