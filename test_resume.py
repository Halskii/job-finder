"""Offline check: the resume reaches the prompt and contact details do not."""
import json
import unittest

import main
import scorer


class Capture:
    sent = ""

    class messages:
        @staticmethod
        def create(**kw):
            Capture.sent = kw["messages"][0]["content"]
            text = json.dumps({"fit_score": 70, "verdict": "maybe", "matched": [], "missing_required": [],
                               "dealbreakers": [], "pay_notes": "", "location_notes": "", "resume_angle": "", "reason": ""})
            return type("M", (), {"content": [type("C", (), {"text": text})()]})()


class ResumeTests(unittest.TestCase):
    def test_contact_lines_removed(self):
        text = main.load_resume()
        self.assertTrue(text, "resume.txt should exist and load")
        for banned in ("@", "linkedin.com", "(737)"):
            self.assertNotIn(banned, text)

    def test_order_is_declared_meaningless(self):
        self.assertIn("UNORDERED", scorer.SYSTEM)

    def test_resume_in_prompt(self):
        job = {"description": "d", "pay_min": None, "pay_max": None, "company": "c", "title": "t", "location": "l", "posted": None}
        scorer.score_job(Capture, "m", {"name": "x"}, job, 80000, resume_text="RESUME BODY HERE")
        self.assertIn("RESUME BODY HERE", Capture.sent)
        self.assertIn("the profile wins", Capture.sent)


if __name__ == "__main__":
    unittest.main()
