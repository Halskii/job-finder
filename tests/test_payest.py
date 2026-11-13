"""Estimated pay from the Bureau of Labor Statistics wage survey: title and place mapping, the cache, the fallbacks and the endpoint."""
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from types import SimpleNamespace
from unittest import mock

import db
import payest
import sources
import web
from payest import Unavailable
from tests import pgtest, webtest

YEAR = 2025
AUSTIN = {"11": 96110, "12": 112000, "13": 134120, "14": 160400, "15": 209890}
TEXAS = {"11": 80000, "12": 100000, "13": 130150, "14": 158000, "15": 200000}
NATION = {"11": 82460, "12": 105210, "13": 135980, "14": 171980, "15": 214670}
TABLE = {("M", "0012420"): AUSTIN, ("S", "4800000"): TEXAS, ("N", "0000000"): NATION}


def fake_bls(table=None, calls=None):
    """A stand-in for query_bls: answers from {(area type, area code): {data type: wage}} and counts its calls."""
    table = TABLE if table is None else table

    def query(series, key=None):
        if calls is not None:
            calls.append(list(series))
        out = {}
        for sid in series:
            area_type, area, datatype = sid[3], sid[4:11], sid[-2:]
            wage = (table.get((area_type, area)) or {}).get(datatype)
            out[sid] = (YEAR, wage) if wage else (None, None)
        return out
    return query


class TitleTests(unittest.TestCase):
    def test_titles_map_to_the_occupation_they_are_paid_like(self):
        want = {"Backend Engineer": "151252", "Senior Software Engineer II": "151252", "Site Reliability Engineer": "151252",
                "Data Engineer": "151252", "Machine Learning Engineer": "151252", "Data Scientist": "152051",
                "Security Engineer": "151212", "QA Engineer": "151253", "Software Engineer in Test": "151253", "SDET": "151253",
                "Database Administrator": "151242", "Data Architect": "151243", "Network Engineer": "151241",
                "Systems Administrator": "151244", "IT Support Specialist": "151232", "Web Developer": "151254",
                "Business Systems Analyst": "151211", "Solutions Engineer": "419031", "Technical Writer": "273042",
                "Engineering Manager": "113021"}
        for title, code in want.items():
            self.assertEqual((payest.occupation_for(title) or ("none",))[0], code, title)

    def test_a_job_the_table_does_not_cover_has_no_occupation(self):
        for title in ("Marketing Coordinator", "Nurse Practitioner", "", None):
            self.assertIsNone(payest.occupation_for(title), title)

    def test_level_comes_from_the_words_and_numerals(self):
        want = {"Software Engineer": "mid", "Software Engineer II": "mid", "Software Engineer 2": "mid", "Software Engineer I": "entry",
                "Junior Developer": "entry", "Associate Software Engineer": "entry", "Software Engineer 1": "entry",
                "Senior Software Engineer": "senior", "Sr. Developer": "senior", "Staff Engineer": "senior", "Principal Engineer": "senior",
                "Software Engineer III": "senior", "Lead Developer": "senior", "Senior Associate Engineer": "senior"}
        for title, level in want.items():
            self.assertEqual(payest.level_of(title), level, title)


class PlaceTests(unittest.TestCase):
    def codes(self, location):
        return [(t, a) for t, a, _ in payest.area_chain(location)]

    def test_a_metro_city_gets_its_metro_then_its_state_then_the_country(self):
        self.assertEqual(self.codes("Austin, Texas, United States"), [("M", "0012420"), ("S", "4800000"), ("N", "0000000")])
        self.assertEqual(self.codes("Austin, TX"), [("M", "0012420"), ("S", "4800000"), ("N", "0000000")])
        self.assertEqual(payest.area_chain("Austin, TX")[0][2], "the Austin area")

    def test_suburbs_and_punctuation_are_understood(self):
        self.assertEqual(self.codes("Cupertino, California, United States")[0], ("M", "0041940"))
        self.assertEqual(self.codes("Round Rock, TX")[0], ("M", "0012420"))
        self.assertEqual(self.codes("St. Louis, MO")[0], ("M", "0041180"))
        self.assertEqual(self.codes("Washington, DC")[0], ("M", "0047900"))
        self.assertEqual(self.codes("Austin, TX (Hybrid)")[0], ("M", "0012420"))

    def test_the_first_place_of_several_counts(self):
        self.assertEqual(self.codes("Cupertino, CA; Austin, TX")[0], ("M", "0041940"))

    def test_the_same_city_name_in_another_state_is_not_that_metro(self):
        self.assertEqual(self.codes("Portland, Maine, United States"), [("S", "2300000"), ("N", "0000000")])
        self.assertEqual(self.codes("Portland, OR")[0], ("M", "0038900"))

    def test_an_unknown_city_gets_its_state_and_a_place_with_no_state_gets_the_country(self):
        self.assertEqual(self.codes("Smallville, Kansas, United States"), [("S", "2000000"), ("N", "0000000")])
        for text in ("Remote", "Remote - US", "United States", "", None):
            self.assertEqual(self.codes(text), [("N", "0000000")], text)

    def test_series_ids_have_the_shape_the_bls_expects(self):
        self.assertEqual(payest.series_id("M", "0012420", "151252", "13"), "OEUM001242000000015125213")      # checked against the live API
        self.assertEqual(payest.series_id("N", "0000000", "151252", "11"), "OEUN000000000000015125211")
        self.assertEqual(payest.series_id("S", "4800000", "151252", "13"), "OEUS480000000000015125213")

    def test_every_metro_has_a_unique_code_and_every_place_names_a_state(self):
        codes = [c for c, *_ in payest.METROS]
        self.assertEqual(len(codes), len(set(codes)))
        for (city, state) in payest._METRO_OF:
            self.assertIn(state, payest.STATES, city)


class QueryTests(unittest.TestCase):
    def ask(self, payload, key=None, status=200):
        response = mock.Mock(status_code=status)
        response.json.return_value = payload
        response.raise_for_status.side_effect = None if status < 400 else OSError("bad status")
        with mock.patch.object(sources, "_session") as session:
            session.return_value.post.return_value = response
            result = payest.query_bls(["S1", "S2"], key)
        return result, session.return_value.post

    def test_the_latest_figure_of_each_series_is_returned_and_missing_ones_are_empty(self):
        payload = {"status": "REQUEST_SUCCEEDED", "Results": {"series": [
            {"seriesID": "S1", "data": [{"year": "2025", "value": "134,120"}]}, {"seriesID": "S2", "data": []}]}}
        result, post = self.ask(payload)
        self.assertEqual(result, {"S1": (2025, 134120), "S2": (None, None)})
        self.assertEqual(post.call_args.args[0], payest.API_V1)
        self.assertEqual(post.call_args.kwargs["json"], {"seriesid": ["S1", "S2"], "latest": True})

    def test_a_withheld_figure_is_not_a_number(self):
        payload = {"status": "REQUEST_SUCCEEDED", "Results": {"series": [{"seriesID": "S1", "data": [{"year": "2025", "value": "-"}]}]}}
        self.assertEqual(self.ask(payload)[0]["S1"], (2025, None))

    def test_a_key_switches_to_the_larger_version_of_the_api(self):
        payload = {"status": "REQUEST_SUCCEEDED", "Results": {"series": []}}
        _, post = self.ask(payload, key="abc")
        self.assertEqual(post.call_args.args[0], payest.API_V2)
        self.assertEqual(post.call_args.kwargs["json"]["registrationkey"], "abc")

    def test_the_daily_limit_and_other_trouble_are_told_apart(self):
        limit = {"status": "REQUEST_NOT_PROCESSED", "message": ["Request could not be serviced, as the daily threshold for total number of requests has been reached."]}
        with self.assertRaises(Unavailable) as ctx:
            self.ask(limit)
        self.assertEqual(ctx.exception.reason, "limit")
        with self.assertRaises(Unavailable) as ctx:
            self.ask({"status": "REQUEST_NOT_PROCESSED", "message": ["something else"]})
        self.assertEqual(ctx.exception.reason, "error")
        with self.assertRaises(Unavailable) as ctx:
            self.ask({}, status=500)
        self.assertEqual(ctx.exception.reason, "error")


class EstimateTests(unittest.TestCase):
    def setUp(self):
        self.conn = pgtest.connect()
        self.now = 1_800_000_000.0

    def tearDown(self):
        self.conn.close()

    def est(self, title="Software Engineer II", location="Austin, TX", table=None, calls=None, now=None):
        return payest.estimate(self.conn, title, location, now or self.now, fake_bls(table, calls))

    def test_the_band_follows_the_level_and_is_rounded_to_a_thousand(self):
        mid = self.est("Software Engineer II")
        self.assertEqual((mid["ok"], mid["low"], mid["high"], mid["band"]), (True, 112000, 160000, "25th to 75th percentile"))
        entry = self.est("Software Engineer I")
        self.assertEqual((entry["low"], entry["high"], entry["band"]), (96000, 112000, "10th to 25th percentile"))
        senior = self.est("Senior Software Engineer")
        self.assertEqual((senior["low"], senior["high"], senior["band"]), (134000, 210000, "50th to 90th percentile"))
        self.assertEqual((mid["area"], mid["occupation"], mid["year"], mid["level"]), ("the Austin area", "software developers", YEAR, "mid"))

    def test_the_first_ask_is_one_request_for_every_area_and_later_asks_use_the_stored_figures(self):
        calls = []
        self.est(calls=calls)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(calls[0]), 15)                       # three areas, five percentiles
        again = self.est("Senior Software Engineer", calls=calls)
        self.assertTrue(again["ok"])
        self.assertEqual(len(calls), 1)                           # a different level of the same job in the same place: nothing asked
        self.est(location="Remote", calls=calls)
        self.assertEqual(len(calls), 1)                           # the national figures were stored with the first ask
        self.est(location="Dallas, TX", calls=calls)              # a new metro: only that metro is asked (the state and nation are held)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(calls[1]), 5)

    def test_a_metro_the_survey_withholds_falls_back_to_the_state(self):
        result = self.est(table={("S", "4800000"): TEXAS, ("N", "0000000"): NATION})
        self.assertEqual((result["area"], result["low"], result["high"]), ("Texas", 100000, 158000))

    def test_a_remote_job_uses_the_national_figures(self):
        result = self.est(location="Remote")
        self.assertEqual((result["area"], result["low"], result["high"]), ("the United States", 105000, 172000))

    def test_when_the_survey_has_nothing_the_answer_says_so(self):
        self.assertEqual(self.est(table={}), {"ok": False, "reason": "no_data"})

    def test_a_missing_top_percentile_falls_back_to_the_middle_half(self):
        table = {("N", "0000000"): {"11": 82000, "12": 105000, "13": 136000, "14": 172000}}      # no 90th (the survey tops out)
        result = self.est("Senior Software Engineer", location="Remote", table=table)
        self.assertEqual((result["low"], result["high"], result["band"]), (105000, 172000, "25th to 75th percentile"))

    def test_a_kind_of_job_the_table_does_not_cover_asks_nothing(self):
        calls = []
        self.assertEqual(self.est("Marketing Coordinator", calls=calls), {"ok": False, "reason": "no_occupation"})
        self.assertEqual(calls, [])

    def test_the_daily_limit_is_reported_and_the_bls_is_left_alone_for_a_while(self):
        def limited(series, key=None):
            raise Unavailable("limit", "daily threshold reached")
        self.assertEqual(payest.estimate(self.conn, "Software Engineer", "Austin, TX", self.now, limited), {"ok": False, "reason": "limit"})
        calls = []
        self.assertEqual(self.est(calls=calls), {"ok": False, "reason": "limit"})              # still blocked: not asked again
        self.assertEqual(calls, [])
        later = self.est(calls=calls, now=self.now + (payest.BLOCKED_HOURS + 1) * 3600)
        self.assertTrue(later["ok"])
        self.assertEqual(len(calls), 1)

    def test_other_trouble_is_reported_and_asked_again_next_time(self):
        def broken(series, key=None):
            raise Unavailable("error", "no answer")
        self.assertEqual(payest.estimate(self.conn, "Software Engineer", "Austin, TX", self.now, broken), {"ok": False, "reason": "error"})
        self.assertTrue(self.est()["ok"])

    def test_figures_already_held_for_a_broader_area_are_shown_when_the_bls_cannot_be_asked(self):
        self.est(location="Remote")                                      # holds the national figures
        def limited(series, key=None):
            raise Unavailable("limit")
        result = payest.estimate(self.conn, "Software Engineer", "Austin, TX", self.now, limited)
        self.assertEqual((result["ok"], result["area"]), (True, "the United States"))

    def test_figures_older_than_a_year_are_asked_for_again(self):
        calls = []
        self.est(calls=calls)
        self.est(calls=calls, now=self.now + 100 * 86400)
        self.assertEqual(len(calls), 1)
        self.est(calls=calls, now=self.now + (payest.MAX_AGE_DAYS + 1) * 86400)
        self.assertEqual(len(calls), 2)


class EndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.site = webtest.Site(cls.tmp.name)
        cls.base = cls.site.base
        conn = cls.site.db()
        db.add_job(conn, sources.Job("gh:p:1", "greenhouse", "Co", "Backend Engineer", "Austin, TX", "https://x", description="d"), 1.0)
        db.add_job(conn, sources.Job("gh:p:2", "greenhouse", "Co", "Backend Engineer", "Austin, TX", "https://x", description="d", pay_min=100000, pay_max=120000), 1.0)
        db.add_job(conn, sources.Job("gh:p:3", "greenhouse", "Co", "Marketing Coordinator", "Austin, TX", "https://x", description="d"), 1.0)
        conn.close()

    @classmethod
    def tearDownClass(cls):
        cls.site.stop()
        cls.tmp.cleanup()

    def get(self, path, token=True):
        headers = {"Cookie": f"{web.COOKIE}={self.site.token}"} if token else {}
        try:
            with urllib.request.urlopen(urllib.request.Request(self.base + path, headers=headers)) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_a_job_without_pay_gets_an_estimate_and_one_with_pay_does_not(self):
        with mock.patch.object(payest, "query_bls", side_effect=fake_bls()):
            status, body = self.get("/api/payestimate/gh:p:1")
            self.assertEqual((status, body["ok"], body["low"], body["high"], body["area"]), (200, True, 112000, 160000, "the Austin area"))
            self.assertEqual(self.get("/api/payestimate/gh:p:2"), (200, {"ok": False, "reason": "has_pay"}))
            self.assertEqual(self.get("/api/payestimate/gh:p:3"), (200, {"ok": False, "reason": "no_occupation"}))

    def test_an_unknown_job_is_a_404_and_signing_in_is_required(self):
        self.assertEqual(self.get("/api/payestimate/gh:p:404")[0], 404)
        self.assertEqual(self.get("/api/payestimate/gh:p:1", token=False)[0], 401)

    def test_the_page_decides_from_the_stored_pay_figures_not_from_the_text_shown_for_them(self):
        import viewer
        # A job with no pay is labelled "Salary Non-Disclosed" by the server and has no figures: the page must key off the figures.
        self.assertEqual(sources.pay_label(None, None, "year"), "Salary Non-Disclosed")
        self.assertEqual(viewer.pay_year(None, None, "year"), (None, None))
        self.assertEqual(viewer.pay_year(90000, 110000, "year"), (90000, 110000))
        script = viewer.PAGE[viewer.PAGE.index("function paintPayEst"):]
        script = script[:script.index("\n}")]
        self.assertIn("if(j.pay_low!=null||j.pay_high!=null)return;", script)
        self.assertNotIn("Non-Disclosed", script)               # never matches on the display text

    def test_the_page_has_the_estimate_line(self):
        req = urllib.request.Request(self.base + "/", headers={"Cookie": f"{web.COOKIE}={self.site.token}"})
        with urllib.request.urlopen(req) as r:
            page = r.read().decode("utf-8")
        self.assertIn('id:"pay-est"', page)
        self.assertIn("/api/payestimate/", page)


if __name__ == "__main__":
    unittest.main()
