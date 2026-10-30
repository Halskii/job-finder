"""Offline check: the resume reaches the prompt and contact details do not."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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


FAKE_RESUME = """Jane Doe
Springfield, US | (555) 123-4567 | jane@example.com | linkedin.com/in/janedoe
555-987-6543
+1 555.222.3333
github.com/janedoe
Built a Java service that handled 100,000 - 120,000 requests per day.
Worked on the 2018-2023 migration and cut build time by 40%.
"""


class ResumeTests(unittest.TestCase):
    def test_contact_lines_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "resume.txt").write_text(FAKE_RESUME, encoding="utf-8")
            with mock.patch.object(main, "ROOT", Path(tmp)):
                text = main.load_resume()
        for banned in ("@", "linkedin.com", "github.com", "555"):
            self.assertNotIn(banned, text)
        self.assertIn("Jane Doe", text)
        self.assertIn("handled 100,000 - 120,000 requests per day", text)   # ordinary numbers survive
        self.assertIn("2018-2023 migration", text)

    def test_no_resume_gives_empty_text(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(main, "ROOT", Path(tmp)):
            self.assertEqual(main.load_resume(), "")

    def test_order_is_declared_meaningless(self):
        self.assertIn("UNORDERED", scorer.SYSTEM)

    def test_resume_in_prompt(self):
        job = {"description": "d", "pay_min": None, "pay_max": None, "company": "c", "title": "t", "location": "l", "posted": None}
        scorer.score_job(Capture, "m", {"name": "x"}, job, 80000, resume_text="RESUME BODY HERE")
        self.assertIn("RESUME BODY HERE", Capture.sent)
        self.assertIn("the profile wins", Capture.sent)


if __name__ == "__main__":
    unittest.main()
