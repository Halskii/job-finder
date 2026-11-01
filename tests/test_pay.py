"""Offline tests for hourly pay: finding the rate, comparing it with a yearly minimum, and showing it."""
import json

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import db
from tests import pgtest
import filters
import scorer
import sources


class ExtractTests(unittest.TestCase):
    def test_the_formats_postings_use(self):
        for text, expected in (("the anticipated salary range is **$40/hr-45/hr.** Please note", (40, 45)),
                               ("Pay: $50 - $65 per hour", (50, 65)), ("$50 - $65 USD / hour", (50, 65)),
                               ("$22.50 to $30.00 an hour", (22.5, 30)), ("paid $85/hr", (85, 85)),
                               ("$18.5 - 21 hourly", (18.5, 21))):
            self.assertEqual(sources.extract_hourly(text), expected, text)

    def test_things_that_are_not_an_hourly_rate(self):
        for text in ("$120,000 - $160,000 per year", "salary $120,000", "5 hours a week", "$3 per hour", "$900/hr", "", None):
            self.assertEqual(sources.extract_hourly(text), (None, None), text)

    def test_yearly_pay_is_never_overwritten_and_hourly_is_marked(self):
        yearly = sources.Job("a", "x", "C", "T", "L", "u", description="Pay $100,000 - $120,000 or $40/hr", pay_min=100000, pay_max=120000)
        sources.fill_pay(yearly)
        self.assertEqual((yearly.pay_min, yearly.pay_unit), (100000, "year"))
        hourly = sources.Job("b", "x", "C", "T", "L", "u", description="Range **$40/hr-45/hr**")
        sources.fill_pay(hourly)
        self.assertEqual((hourly.pay_min, hourly.pay_max, hourly.pay_unit), (40, 45, "hour"))
        none = sources.Job("c", "x", "C", "T", "L", "u", description="No pay here")
        sources.fill_pay(none)
        self.assertEqual((none.pay_min, none.pay_unit), (None, "year"))

    def test_how_pay_reads(self):
        self.assertEqual(sources.format_pay(92000, 147000), "$92,000 - $147,000")
        self.assertEqual(sources.format_pay(40, 45, "hour"), "$40 - $45 / hr")
        self.assertEqual(sources.format_pay(22.5, 22.5, "hour"), "$22.50 / hr")
        self.assertEqual(sources.format_pay(120000, None), "$120,000")
        self.assertEqual((sources.format_pay(None, None), sources.format_pay(0, 0)), ("", ""))
        self.assertEqual(sources.pay_label(None, None), "Salary Non-Disclosed")
        self.assertEqual(sources.pay_label(40, 45, "hour"), "$40 - $45 / hr")


class FilterTests(unittest.TestCase):
    def test_an_hourly_rate_is_compared_as_a_full_time_year(self):
        self.assertTrue(filters.pay_ok(45, 40, 80000, "hour"))      # 45 * 2080 = 93,600
        self.assertFalse(filters.pay_ok(30, 25, 80000, "hour"))     # 62,400
        self.assertFalse(filters.pay_ok(45, 40, 80000))             # the same numbers as a yearly figure would fail
        self.assertTrue(filters.pay_ok(None, None, 80000, "hour"))  # no pay listed always passes


class ScorerAndStorageTests(unittest.TestCase):
    def test_the_scorer_is_told_the_unit_and_what_it_comes_to_in_a_year(self):
        sent = []
        client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: sent.append(kw) or SimpleNamespace(
            content=[SimpleNamespace(text='{"fit_score": 50, "verdict": "maybe"}')], usage=None)))
        job = {"company": "C", "title": "T", "location": "L", "posted": None, "description": "d", "pay_min": 40, "pay_max": 45, "pay_unit": "hour"}
        scorer.score_job(client, "m", {}, job, 80000)
        self.assertIn("Listed pay: $40 - $45 / hr (about $83,200 - $93,600 a year full-time)", sent[0]["messages"][0]["content"])
        scorer.score_job(client, "m", {}, {**job, "pay_min": 90000, "pay_max": 100000, "pay_unit": "year"}, 80000)
        self.assertIn("Listed pay: $90,000 - $100,000\n", sent[1]["messages"][0]["content"])

    def test_the_unit_and_fractions_of_a_dollar_are_stored(self):
        conn = pgtest.connect()
        db.add_job(conn, sources.Job("gh:a:4", "x", "C", "T", "L", "u", description="d", pay_min=22.5, pay_max=30, pay_unit="hour"), 1.0)
        db.add_job(conn, sources.Job("gh:a:5", "x", "C", "T2", "L", "u", description="d", pay_min=100000, pay_max=120000), 1.0)
        rows = {r["id"]: (r["pay_min"], r["pay_max"], r["pay_unit"]) for r in conn.execute("SELECT * FROM jobs")}
        self.assertEqual(rows, {"gh:a:4": (22.5, 30, "hour"), "gh:a:5": (100000, 120000, "year")})
        conn.close()


if __name__ == "__main__":
    unittest.main()
