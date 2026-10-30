"""Offline tests: no network, no API key. Run with: python -m unittest -v"""
import json
import unittest

import filters
import scorer
import sources

TITLES = ["Software Engineer I", "Associate Software Engineer", "AI Engineer II", "Platform Engineer I", "Data Engineer I"]
EXCLUDE = ["senior", "staff", "lead", "principal", "manager"]
PATTERNS = filters.base_titles(TITLES)


class TitleTests(unittest.TestCase):
    def test_levels_are_optional(self):
        self.assertTrue(filters.title_ok("Software Engineer", PATTERNS, EXCLUDE))
        self.assertTrue(filters.title_ok("Software Engineer II", PATTERNS, EXCLUDE))
        self.assertTrue(filters.title_ok("Backend Software Engineer, Payments", PATTERNS, EXCLUDE))

    def test_exclusions(self):
        self.assertFalse(filters.title_ok("Senior Software Engineer", PATTERNS, EXCLUDE))
        self.assertFalse(filters.title_ok("Staff+ Software Engineer, Deployment", PATTERNS, EXCLUDE))
        self.assertFalse(filters.title_ok("Engineering Manager, Platform Engineer", PATTERNS, EXCLUDE))

    def test_word_boundaries(self):
        self.assertFalse(filters.title_ok("AI Engineering Manager", PATTERNS, []))   # 'engineering' != 'engineer'
        self.assertTrue(filters.title_ok("AI Engineer", PATTERNS, EXCLUDE))

    def test_unrelated(self):
        self.assertFalse(filters.title_ok("Account Executive", PATTERNS, EXCLUDE))


class LocationTests(unittest.TestCase):
    def test_us(self):
        for loc in ["Austin, TX", "Remote - US", "United States", "US-SF, US-NYC, US-Remote", "Athens, GA", ""]:
            self.assertTrue(filters.location_ok(loc, True), loc)

    def test_non_us(self):
        for loc in ["Costa Rica", "London, UK", "Toronto, Canada", "Bengaluru, India", "Berlin, Germany"]:
            self.assertFalse(filters.location_ok(loc, True), loc)

    def test_disabled(self):
        self.assertTrue(filters.location_ok("London, UK", False))


class PayTests(unittest.TestCase):
    def test_extract(self):
        self.assertEqual(sources.extract_pay("Pay range: $130,332 - $195,498 per year"), (130332, 195498))
        self.assertEqual(sources.extract_pay("Salary $120k to $160k"), (120000, 160000))
        self.assertEqual(sources.extract_pay("no numbers here"), (None, None))

    def test_filter(self):
        self.assertFalse(filters.pay_ok(70000, 60000, 80000))
        self.assertTrue(filters.pay_ok(120000, 100000, 80000))
        self.assertTrue(filters.pay_ok(None, None, 80000))   # unknown pay is kept


class FakeClient:
    class messages:
        @staticmethod
        def create(**kw):
            payload = {"fit_score": 142, "verdict": "bogus", "matched": ["Terraform"], "missing_required": ["Go"],
                       "dealbreakers": [], "pay_notes": "", "location_notes": "", "resume_angle": "x", "reason": "y"}
            text = "Here you go:\n```json\n" + json.dumps(payload) + "\n```"
            return type("M", (), {"content": [type("C", (), {"text": text})()]})()


class ScorerTests(unittest.TestCase):
    def test_parse_and_clamp(self):
        job = {"description": "d", "pay_min": None, "pay_max": None, "company": "c", "title": "t", "location": "l", "posted": None}
        res = scorer.score_job(FakeClient, "model", {"name": "x"}, job, 80000)
        self.assertEqual(res["fit_score"], 100)          # clamped
        self.assertEqual(res["verdict"], "maybe")        # invalid verdict repaired from score
        self.assertEqual(res["missing_required"], ["Go"])


if __name__ == "__main__":
    unittest.main()
