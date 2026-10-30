"""Offline tests for resume -> profile generation."""
import json
import unittest

import profile_gen

EXISTING = {
    "name": "James Hall", "location": "Austin, TX", "work_authorization": "U.S. citizen",
    "security_clearance": "None. Willing to obtain one.", "relocation": "Open",
    "do_not_assume": ["Go", "RAG", "Spark"], "best_fit_notes": ["note"],
    "strong_skills": ["Java"], "familiar_skills": ["PostgreSQL"], "achievements": ["old"],
    "headline": "old", "years_experience": 2, "education": "old", "certifications": [],
}
GENERATED = {
    "headline": "Software Engineer", "years_experience": 3, "education": "B.S. CS, UCF, GPA 3.2",
    "certifications": ["AZ-900"],
    "strong_skills": ["Java", "Terraform", "Go", "java"],          # Go conflicts; 'java' duplicates
    "familiar_skills": ["PostgreSQL", "Terraform"],                  # Terraform is strong, not familiar
    "achievements": ["Migrated 100+ projects"],
}


class FakeClient:
    class messages:
        @staticmethod
        def create(**kw):
            text = "```json\n" + json.dumps(GENERATED) + "\n```"
            return type("M", (), {"content": [type("C", (), {"text": text})()]})()


class ProfileTests(unittest.TestCase):
    def test_generate_parses(self):
        self.assertEqual(profile_gen.generate(FakeClient, "m", "resume")["years_experience"], 3)

    def test_carry_over_and_conflicts(self):
        new, notes = profile_gen.merge(EXISTING, GENERATED)
        self.assertEqual(new["work_authorization"], "U.S. citizen")
        self.assertEqual(new["security_clearance"], "None. Willing to obtain one.")
        self.assertEqual(new["do_not_assume"], ["Go", "RAG", "Spark"])
        self.assertNotIn("Go", new["strong_skills"])            # never promoted past do_not_assume
        self.assertTrue(any("'Go'" in n for n in notes))
        self.assertEqual(new["strong_skills"], ["Java", "Terraform"])   # deduped
        self.assertEqual(new["familiar_skills"], ["PostgreSQL"])        # not double-listed
        self.assertEqual(new["education"], "B.S. CS, UCF, GPA 3.2")

    def test_diff(self):
        new, _ = profile_gen.merge(EXISTING, GENERATED)
        d = "\n".join(profile_gen.diff(EXISTING, new))
        self.assertIn("+ strong_skills: Terraform", d)
        self.assertIn("- achievements: old", d)
        self.assertIn("years_experience", d)

    def test_missing_keys_rejected(self):
        class Bad:
            class messages:
                @staticmethod
                def create(**kw):
                    return type("M", (), {"content": [type("C", (), {"text": '{"headline": "x"}'})()]})()
        with self.assertRaises(ValueError):
            profile_gen.generate(Bad, "m", "resume")


if __name__ == "__main__":
    unittest.main()
