"""Offline tests for tailor.py: file names, checking the model's answer, and the whole job with a fake client and no browser."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import db
import main
import sources
import tailor

ANSWER = {
    "resume": {"summary": "Engineer.", "skills": [{"label": "Languages", "text": "Python"}],
               "jobs": [{"heading": "Acme | Engineer", "dates": "2023 - 2026", "bullets": ["Built things."]}],
               "education": {"heading": "State U | B.S.", "dates": "2023", "detail": "GPA 3.2"}, "interests": "Lifting."},
    "cover_letter": {"paragraphs": ["I want this job.", "Here is why."]},
}


def fake_client(answer=None, stop="end_turn"):
    calls = []

    def create(**kw):
        calls.append(kw)
        return SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type="text", text=json.dumps(answer or ANSWER))],
                               usage=SimpleNamespace(input_tokens=1000, output_tokens=500))
    return SimpleNamespace(messages=SimpleNamespace(create=create)), calls


class NameTests(unittest.TestCase):
    def test_file_stem_cuts_long_roles_and_companies(self):
        self.assertEqual(tailor.file_stem("James Hall", "Software and AI Engineer, IT Systems (Strategy)", "CoreWeave"),
                         "James_Hall_Software_and_AI_Engineer_CoreWeave")
        self.assertEqual(tailor.file_stem("James Hall", "Engineer II", "Acme, Inc."), "James_Hall_Engineer_II_Acme")

    def test_file_stem_has_no_path_characters(self):
        stem = tailor.file_stem("J H", "../../evil\role", "C:/x")
        self.assertNotRegex(stem, r"[\/:.]")


class PromptRuleTests(unittest.TestCase):
    def test_unmentioned_skills_are_unknown_not_a_gap(self):
        """A skill the profile never mentions must not be written up as something the candidate lacks (it once said
        they had not used AI coding tools, which they use daily). Only do_not_assume items may be named as gaps."""
        self.assertIn("UNKNOWN", tailor.SYSTEM)
        self.assertNotIn("or not mentioned at all, is something", tailor.SYSTEM)

    def test_no_gushing_about_one_part_of_the_job_unless_asked(self):
        self.assertIn("Do not sound especially passionate", tailor.SYSTEM)
        self.assertIn("Only do this if the candidate's own request asks for it", tailor.SYSTEM)

    def test_the_letter_never_lists_what_the_candidate_lacks(self):
        """A letter that said 'I have not built RAG systems or worked in financial services' made a strong candidate look
        merely okay. The employer has the resume; the letter sells and does not confess."""
        self.assertIn("Never write about what the candidate lacks", tailor.SYSTEM)
        self.assertNotIn("has not done it", tailor.SYSTEM)
        self.assertNotIn("named as a gap", tailor.SYSTEM)


class CleanRequestTests(unittest.TestCase):
    def test_plain_text_passes_unchanged(self):
        self.assertEqual(tailor.clean_request("Lead with Kubernetes.\nKeep it short."), "Lead with Kubernetes.\nKeep it short.")
        self.assertEqual(tailor.clean_request("Caf\u00e9 \u2014 100% <b>ok</b> {x}"), "Caf\u00e9 \u2014 100% <b>ok</b> {x}")

    def test_control_invisible_and_surrogate_characters_are_dropped(self):
        dirty = "a\x00b\x1b[31mc\u202ed\u200be\ufeff\ud800f\ue000g\x7fh"
        self.assertEqual(tailor.clean_request(dirty), "ab[31mcdefgh")
        self.assertEqual(tailor.clean_request("x\u00a0y\u3000z"), "x y z")
        tailor.clean_request("a\ud83d").encode("utf-8")   # an unpaired surrogate can no longer break encoding

    def test_length_is_capped_and_blank_lines_collapse(self):
        self.assertEqual(len(tailor.clean_request("y" * 5000)), tailor.REQUEST_LIMIT)
        self.assertEqual(tailor.clean_request("a\n\n\n\n\nb"), "a\n\nb")
        self.assertEqual(tailor.clean_request(None), "")
        self.assertEqual(tailor.clean_request(12345), "12345")


class CheckTests(unittest.TestCase):
    def test_good_answer_passes_and_missing_parts_fail(self):
        self.assertIs(tailor.check(ANSWER), ANSWER)
        for broken in ({}, {"resume": ANSWER["resume"]}, {**ANSWER, "cover_letter": {"paragraphs": []}},
                       {**ANSWER, "resume": {**ANSWER["resume"], "jobs": []}}):
            with self.assertRaises(ValueError):
                tailor.check(broken)


class TailorJobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "profile.json").write_text(json.dumps({"name": "James Hall", "location": "Austin, TX"}), encoding="utf-8")
        self.conn = db.connect(str(self.root / "jobs.db"))
        db.add_job(self.conn, sources.Job("gh:co:1", "greenhouse", "CoreWeave", "Software and AI Engineer", "Bellevue, WA",
                                          "https://example.com/1", description="Build agents."), 1.0)
        self.written = []

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def run_job(self, client, job_id="gh:co:1", pages=1):
        raw = "James Hall\nAustin, TX  |  (555) 010-0000  |  jh@example.com\nSUMMARY\nx"
        with mock.patch.object(tailor, "ROOT", self.root), mock.patch.object(main, "load_resume", return_value="RESUME TEXT"), \
                mock.patch.object(main, "read_resume_file", return_value=raw), \
                mock.patch.object(tailor, "html_to_pdf", lambda page, out: self.written.append((out.name, page))), \
                mock.patch.object(tailor, "page_count", return_value=pages):
            return tailor.tailor_job(self.conn, job_id, out_dir=self.root / "Optimized Resumes", client=client)

    def test_writes_both_files_named_after_the_job(self):
        client, calls = fake_client()
        result = self.run_job(client)
        self.assertEqual(result["files"], ["James_Hall_Software_and_AI_Engineer_CoreWeave_resume.pdf",
                                           "James_Hall_Software_and_AI_Engineer_CoreWeave_cover_letter.pdf"])
        self.assertEqual(result["tokens"], {"in": 1000, "out": 500})
        self.assertEqual(calls[0]["model"], "claude-fable-5-1")
        pages = dict(self.written)
        self.assertIn("jh@example.com", pages[result["files"][0]])        # contact line copied from the resume file
        self.assertIn("Dear CoreWeave Hiring Team,", pages[result["files"][1]])

    def test_company_name_comes_from_the_posting_when_the_model_gives_it(self):
        client, _ = fake_client({**ANSWER, "company_name": "CoreWeave"})
        result = self.run_job(client)
        self.assertTrue(result["files"][0].startswith("James_Hall_Software_and_AI_Engineer_CoreWeave_"))
        self.assertIn("Dear CoreWeave Hiring Team,", dict(self.written)[result["files"][1]])

    def test_custom_requests_reach_the_prompt_only_when_given(self):
        client, calls = fake_client()
        self.run_job(client)
        self.assertNotIn("OWN REQUEST", calls[0]["messages"][0]["content"])
        client, calls = fake_client()
        with mock.patch.object(tailor, "ROOT", self.root), mock.patch.object(main, "load_resume", return_value="R"), \
                mock.patch.object(main, "read_resume_file", return_value="x"), mock.patch.object(tailor, "html_to_pdf"), \
                mock.patch.object(tailor, "page_count", return_value=1):
            tailor.tailor_job(self.conn, "gh:co:1", out_dir=self.root / "o", client=client,
                              resume_request="Lead with Kubernetes", letter_request="  Keep it short  ")
        sent = calls[0]["messages"][0]["content"]
        self.assertIn("OWN REQUEST FOR THE RESUME (follow it; it overrides the style rules above, but never the rules about facts):\nLead with Kubernetes", sent)
        self.assertIn("OWN REQUEST FOR THE COVER LETTER (follow it; it overrides the style rules above, but never the rules about facts):\nKeep it short", sent)

    def test_phone_and_email_are_not_sent_to_the_api(self):
        client, calls = fake_client()
        self.run_job(client)
        sent = json.dumps(calls[0]["messages"]) + calls[0]["system"]
        self.assertNotIn("jh@example.com", sent)
        self.assertNotIn("555", sent)
        self.assertIn("RESUME TEXT", sent)
        self.assertIn("Build agents.", sent)

    def test_an_over_long_page_is_retried_smaller_then_reported(self):
        client, _ = fake_client()
        result = self.run_job(client, pages=2)
        self.assertEqual(result["pages"], {"resume": 2, "cover_letter": 2})
        self.assertEqual(len(self.written), len(tailor.RESUME_SIZES) + len(tailor.LETTER_SIZES))

    def test_unknown_job_and_refusal(self):
        client, _ = fake_client()
        with self.assertRaises(LookupError):
            self.run_job(client, job_id="nope")
        refusing, _ = fake_client(stop="refusal")
        with self.assertRaises(RuntimeError):
            self.run_job(refusing)


if __name__ == "__main__":
    unittest.main()
