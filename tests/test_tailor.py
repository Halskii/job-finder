"""Offline tests for tailor.py: file names, checking the model's answer, and the whole job with a fake client and no browser."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import db
from tests import pgtest
import main
import sources
import tailor

RESUME = {"summary": "Engineer.", "skills": [{"label": "Languages", "text": "Python"}],
          "jobs": [{"heading": "Acme | Engineer", "dates": "2023 - 2026", "bullets": ["Built things."]}],
          "education": {"heading": "State U | B.S.", "dates": "2023", "detail": "GPA 3.2"}, "interests": "Lifting."}
LETTER = {"paragraphs": ["I want this job.", "Here is why."]}
RESUME_ANSWER = {"resume": RESUME}
LETTER_ANSWER = {"cover_letter": LETTER}


def fake_client(answer, stop="end_turn"):
    calls = []

    def create(**kw):
        calls.append(kw)
        return SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type="text", text=json.dumps(answer))],
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
        for system in (tailor.RESUME_SYSTEM, tailor.LETTER_SYSTEM):
            self.assertIn("UNKNOWN", system)
            self.assertNotIn("or not mentioned at all, is something", system)

    def test_no_gushing_about_one_part_of_the_job_unless_asked(self):
        self.assertIn("Do not sound especially passionate", tailor.LETTER_SYSTEM)
        self.assertIn("Only do this if the candidate's own request asks for it", tailor.LETTER_SYSTEM)

    def test_the_letter_follows_the_cornell_guide_by_default(self):
        """https://jobs.hr.cornell.edu/us/en/blogarticle/how-to-write-an-effective-cover-letter: 3 or 4 paragraphs on one page; an opening that names
        the job and employer; a body of tailored examples with their impact; why this employer; a closing that asks about next steps and thanks."""
        letter = tailor.LETTER_SYSTEM
        self.assertIn(tailor.LETTER_GUIDE, letter)
        self.assertIn("3 or 4 short paragraphs on one page", letter)
        self.assertNotIn("3 to 5", letter)                                                  # the old length
        self.assertIn("under 330 words in total", letter)
        self.assertIn("the opening at most 70 words, each body paragraph at most 90, the closing at most 50", letter)    # a total alone was ignored: 357 words
        self.assertIn("name the exact job title and the company", letter)                  # opening
        self.assertIn("Do not say how the candidate found the opening unless their own request says so", letter)    # never invent a fact
        self.assertIn("do not repeat the resume", letter)                                    # body
        self.assertIn("Say what the candidate delivered or changed", letter)
        self.assertIn("use only results the resume states", letter)                          # impact, but no invented numbers
        self.assertIn("Never write a bare claim", letter)
        self.assertIn("Do not invent anything about the employer", letter)                  # why this employer: only what the posting says
        self.assertIn("next steps and thank the reader", letter)                             # closing
        self.assertIn("Do not repeat contact details", letter)                               # the letterhead has them

    def test_the_guide_is_for_the_letter_only_and_the_old_rules_stay(self):
        self.assertNotIn("Opening paragraph", tailor.RESUME_SYSTEM)
        self.assertIn("Do not sound especially passionate", tailor.LETTER_SYSTEM)           # warmth about the whole role, not one requirement
        self.assertIn("Never write about what the candidate lacks", tailor.LETTER_SYSTEM)
        self.assertIn("Do not write a date, address, greeting, or sign-off", tailor.LETTER_SYSTEM)
        self.assertEqual(tailor.LETTER_SYSTEM.count("Opening paragraph"), 1)

    def test_the_candidates_own_request_still_overrides_the_guide(self):
        """It is a default: ask() tells the model the request overrides the style rules, so 'make it two paragraphs' wins over '3 or 4'."""
        sent = []
        client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: sent.append(kw) or SimpleNamespace(
            stop_reason="end_turn", content=[SimpleNamespace(type="text", text='{"company_name": "Acme", "cover_letter": {"paragraphs": ["x"]}}')],
            usage=None)))
        row = {"company": "Acme", "title": "Engineer", "location": "Austin", "description": "d"}
        tailor.ask(client, "m", tailor.LETTER_SYSTEM, {}, "resume", row, "COVER LETTER", "Keep it to two paragraphs.")
        self.assertIn("it overrides the style rules above, but never the rules about facts", sent[0]["messages"][0]["content"])
        self.assertIn("Keep it to two paragraphs.", sent[0]["messages"][0]["content"])
        self.assertIn("3 or 4 short paragraphs", sent[0]["system"])

    def test_neither_document_lists_what_the_candidate_lacks(self):
        """A letter that said 'I have not built RAG systems or worked in financial services' made a strong candidate look
        merely okay. The employer has the resume; the letter sells and does not confess."""
        for system in (tailor.RESUME_SYSTEM, tailor.LETTER_SYSTEM):
            self.assertIn("Never write about what the candidate lacks", system)
            self.assertNotIn("has not done it", system)
            self.assertNotIn("named as a gap", system)

    def test_each_prompt_asks_only_for_its_own_document(self):
        self.assertIn('"resume"', tailor.RESUME_SYSTEM)
        self.assertNotIn("cover_letter", tailor.RESUME_SYSTEM)
        self.assertIn('"cover_letter"', tailor.LETTER_SYSTEM)
        self.assertNotIn('"resume"', tailor.LETTER_SYSTEM)

    def test_grammar_comes_first_in_both(self):
        for system in (tailor.RESUME_SYSTEM, tailor.LETTER_SYSTEM):
            self.assertIn("Grammar comes first", system)
            self.assertIn("a single\n  \"and\" before the last one", system)


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
    def test_good_answers_pass_and_missing_parts_fail(self):
        self.assertIs(tailor.check_resume(RESUME_ANSWER), RESUME_ANSWER)
        self.assertIs(tailor.check_letter(LETTER_ANSWER), LETTER_ANSWER)
        for broken in ({}, {"resume": {**RESUME, "jobs": []}}, {"resume": {**RESUME, "summary": " "}}):
            with self.assertRaises(ValueError):
                tailor.check_resume(broken)
        for broken in ({}, {"cover_letter": {"paragraphs": []}}, {"cover_letter": {"paragraphs": ["ok", " "]}}, RESUME_ANSWER):
            with self.assertRaises(ValueError):
                tailor.check_letter(broken)


class TailorJobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "profile.json").write_text(json.dumps({"name": "James Hall", "location": "Austin, TX"}), encoding="utf-8")
        self.conn = pgtest.connect()
        db.add_job(self.conn, sources.Job("gh:co:1", "greenhouse", "CoreWeave", "Software and AI Engineer", "Bellevue, WA",
                                          "https://example.com/1", description="Build agents."), 1.0)
        self.written = []

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def run_job(self, make, client, job_id="gh:co:1", pages=1, **kw):
        raw = "James Hall\nAustin, TX  |  (555) 010-0000  |  jh@example.com\nSUMMARY\nx"
        with mock.patch.object(main, "load_resume", return_value="RESUME TEXT"), \
                mock.patch.object(main, "read_resume_file", return_value=raw), \
                mock.patch.object(tailor, "html_to_pdf", lambda page, out: self.written.append((out.name, page))), \
                mock.patch.object(tailor, "page_count", return_value=pages):
            return make(self.conn, job_id, out_dir=self.root / "Optimized Resumes", client=client, home=self.root, **kw)

    def resume(self, client, **kw):
        return self.run_job(tailor.tailor_resume, client, **kw)

    def letter(self, client, **kw):
        return self.run_job(tailor.write_cover_letter, client, **kw)

    def test_the_resume_button_writes_only_the_resume(self):
        client, calls = fake_client(RESUME_ANSWER)
        result = self.resume(client)
        self.assertEqual(result["files"], ["James_Hall_Software_and_AI_Engineer_CoreWeave_resume.pdf"])
        self.assertEqual(result["word_files"], ["James_Hall_Software_and_AI_Engineer_CoreWeave_resume.docx"])
        self.assertEqual(result["pages"], {"resume": 1})
        self.assertEqual(result["tokens"], {"in": 1000, "out": 500})
        self.assertEqual((len(calls), calls[0]["model"], calls[0]["system"]), (1, "claude-fable-5-1", tailor.RESUME_SYSTEM))
        self.assertIn("jh@example.com", dict(self.written)[result["files"][0]])        # contact line copied from the resume file
        self.assertFalse(list((self.root / "Optimized Resumes").glob("*cover_letter*")))

    def test_the_cover_letter_button_writes_only_the_letter(self):
        client, calls = fake_client(LETTER_ANSWER)
        result = self.letter(client)
        self.assertEqual(result["files"], ["James_Hall_Software_and_AI_Engineer_CoreWeave_cover_letter.pdf"])
        self.assertEqual(result["word_files"], ["James_Hall_Software_and_AI_Engineer_CoreWeave_cover_letter.docx"])
        self.assertEqual(result["pages"], {"cover_letter": 1})
        self.assertEqual(calls[0]["system"], tailor.LETTER_SYSTEM)
        self.assertIn("Dear CoreWeave Hiring Team,", dict(self.written)[result["files"][0]])
        self.assertFalse(list((self.root / "Optimized Resumes").glob("*_resume.*")))

    def test_editable_word_copies_hold_the_same_text(self):
        import docx
        folder = self.root / "Optimized Resumes"
        resume_file = self.resume(fake_client(RESUME_ANSWER)[0])["word_files"][0]
        letter_file = self.letter(fake_client(LETTER_ANSWER)[0])["word_files"][0]
        resume = [p.text for p in docx.Document(str(folder / resume_file)).paragraphs]
        letter = [p.text for p in docx.Document(str(folder / letter_file)).paragraphs]
        self.assertEqual(resume[0], "James Hall")
        self.assertIn("jh@example.com", resume[1])
        self.assertIn("SUMMARY", resume)
        self.assertIn("Dear CoreWeave Hiring Team,", letter)
        self.assertEqual(letter[-1], "James Hall")
        for text in LETTER["paragraphs"]:
            self.assertIn(text, letter)

    def test_company_name_comes_from_the_posting_when_the_model_gives_it(self):
        result = self.letter(fake_client({**LETTER_ANSWER, "company_name": "CoreWeave"})[0])
        self.assertTrue(result["files"][0].startswith("James_Hall_Software_and_AI_Engineer_CoreWeave_"))
        self.assertIn("Dear CoreWeave Hiring Team,", dict(self.written)[result["files"][0]])

    def test_a_custom_request_reaches_the_prompt_only_when_given(self):
        client, calls = fake_client(RESUME_ANSWER)
        self.resume(client)
        self.assertNotIn("OWN REQUEST", calls[0]["messages"][0]["content"])
        client, calls = fake_client(RESUME_ANSWER)
        self.resume(client, request="Lead with Kubernetes")
        self.assertIn("OWN REQUEST FOR THE RESUME (follow it; it overrides the style rules above, but never the rules about facts):\nLead with Kubernetes",
                      calls[0]["messages"][0]["content"])
        client, calls = fake_client(LETTER_ANSWER)
        self.letter(client, request="  Keep it short  ")
        self.assertIn("OWN REQUEST FOR THE COVER LETTER (follow it; it overrides the style rules above, but never the rules about facts):\nKeep it short",
                      calls[0]["messages"][0]["content"])

    def test_phone_and_email_are_not_sent_to_the_api(self):
        for make, answer in ((self.resume, RESUME_ANSWER), (self.letter, LETTER_ANSWER)):
            client, calls = fake_client(answer)
            make(client)
            sent = json.dumps(calls[0]["messages"]) + calls[0]["system"]
            self.assertNotIn("jh@example.com", sent)
            self.assertNotIn("555", sent)
            self.assertIn("RESUME TEXT", sent)
            self.assertIn("Build agents.", sent)

    def test_an_over_long_page_is_retried_smaller_then_reported(self):
        result = self.resume(fake_client(RESUME_ANSWER)[0], pages=2)
        self.assertEqual(result["pages"], {"resume": 2})
        self.assertEqual(len(self.written), len(tailor.RESUME_SIZES))
        self.written.clear()
        result = self.letter(fake_client(LETTER_ANSWER)[0], pages=2)
        self.assertEqual(result["pages"], {"cover_letter": 2})
        self.assertEqual(len(self.written), len(tailor.LETTER_SIZES))

    def test_an_answer_for_the_wrong_document_is_refused_and_writes_nothing(self):
        with self.assertRaises(ValueError):
            self.resume(fake_client(LETTER_ANSWER)[0])
        with self.assertRaises(ValueError):
            self.letter(fake_client(RESUME_ANSWER)[0])
        self.assertEqual(self.written, [])

    def test_unknown_job_and_refusal(self):
        for make, answer in ((self.resume, RESUME_ANSWER), (self.letter, LETTER_ANSWER)):
            with self.assertRaises(LookupError):
                make(fake_client(answer)[0], job_id="nope")
            with self.assertRaises(RuntimeError):
                make(fake_client(answer, stop="refusal")[0])


if __name__ == "__main__":
    unittest.main()
