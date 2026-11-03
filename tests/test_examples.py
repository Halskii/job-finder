"""The example files a fresh clone starts from must load and work, and a clone with no config must get a clear message."""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import discovery
import filters
import main
import profile_gen

ROOT = Path(__file__).parent.parent
PROFILE_KEYS = {"name", "headline", "years_experience", "education", "location", "work_authorization", "security_clearance",
                "relocation", "certifications", "strong_skills", "familiar_skills", "do_not_assume", "achievements", "best_fit_notes"}


class ExampleConfigTests(unittest.TestCase):
    def test_loads_and_matches_a_typical_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copy(ROOT / "config.example.yaml", Path(tmp) / "config.yaml")
            cfg = discovery.load_config(Path(tmp))
        patterns = filters.base_titles(cfg["titles"])
        exclude = [w.lower() for w in cfg["exclude_title_words"]]
        self.assertTrue(filters.title_ok("Software Engineer II", patterns, exclude))
        self.assertFalse(filters.title_ok("Senior Software Engineer", patterns, exclude))
        self.assertEqual(cfg["min_pay"], 0)                     # no personal pay floor in the shared template
        self.assertEqual(cfg["exclude_locations"], [])
        self.assertTrue(cfg["government"]["texas_hhs"]["enabled"])
        self.assertIn("Programmer", cfg["gov_titles"])
        self.assertIsInstance(cfg["sources"]["greenhouse"], list)


class ExampleProfileTests(unittest.TestCase):
    def setUp(self):
        self.profile = json.loads((ROOT / "profile.example.json").read_text(encoding="utf-8"))

    def test_has_every_field_the_program_reads(self):
        self.assertEqual(set(self.profile), PROFILE_KEYS)

    def test_works_with_the_keyword_prescore_and_the_profile_merge(self):
        self.assertGreater(filters.quick_score("We use Python and SQL daily", self.profile), 0)
        self.assertLess(filters.quick_score("We use Kafka and Spark", self.profile), 0)     # do_not_assume costs points
        merged, _ = profile_gen.merge(self.profile, {"headline": "x", "years_experience": 1, "education": "y", "certifications": [],
                                                      "strong_skills": ["Python"], "familiar_skills": [], "achievements": []})
        self.assertEqual(merged["work_authorization"], self.profile["work_authorization"])  # carried over, not regenerated


class MissingFileTests(unittest.TestCase):
    def run_main(self, root: Path, command=("list",)):
        with mock.patch.object(main, "ROOT", root), mock.patch.object(sys, "argv", ["main.py", *command]):
            with self.assertRaises(SystemExit) as ctx:
                main.main()
        return str(ctx.exception)

    def test_missing_config_points_to_the_example(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIn("config.example.yaml", self.run_main(Path(tmp)))

    def test_missing_profile_points_to_the_example_for_the_commands_that_read_it(self):
        self.assertEqual(main.NEEDS_PROFILE, ("run", "profile"))
        for command in ("run", "profile"):
            with tempfile.TemporaryDirectory() as tmp:
                shutil.copy(ROOT / "config.example.yaml", Path(tmp) / "config.yaml")
                self.assertIn("profile.example.json", self.run_main(Path(tmp), (command,)), command)

    def test_the_other_commands_work_without_a_profile_file(self):
        """The server keeps each person's profile in their own folder and has no profile.json: `workday-add`, `rescore` and the rest must not ask for one."""
        commands = {"workday-add": ("workday-add", "https://x.wd1.myworkdayjobs.com/s"), "rescore": ("rescore", "--user", "1"), "list": ("list",),
                    "discover": ("discover", "acme"), "crawl": ("crawl",), "reformat": ("reformat",), "fetch": ("fetch",), "usage": ("usage",),
                    "invite": ("invite", "list"), "mark": ("mark", "x", "saved")}
        for name, argv in commands.items():
            with tempfile.TemporaryDirectory() as tmp:
                shutil.copy(ROOT / "config.example.yaml", Path(tmp) / "config.yaml")
                handler = mock.Mock()
                with mock.patch.object(main, "ROOT", Path(tmp)), mock.patch.object(sys, "argv", ["main.py", *argv]), mock.patch.object(main, "load_env"),                         mock.patch.object(main.db, "connect"), mock.patch.object(main.discovery, "load_config", return_value={}),                         mock.patch.object(main, "cmd_" + name.replace("-", "_"), handler):
                    main.main()
                handler.assert_called_once()
                self.assertEqual(handler.call_args[0][2], {}, name)        # no profile: they are given an empty one


class RepositoryHygieneTests(unittest.TestCase):
    def test_gitignore_covers_private_files(self):
        lines = {ln.strip() for ln in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()}
        for private in (".env", "config.yaml", "profile.json", "jobs.db", "reports/", "resume.pdf", "discovered_sources.yaml"):
            self.assertIn(private, lines)

    def test_license_reserves_all_rights_and_is_not_an_open_source_license(self):
        text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("All rights reserved", text)
        self.assertIn("may not copy", text)
        self.assertNotIn("Permission is hereby granted", text)   # the MIT grant must not come back by accident

    def test_no_test_is_hidden_from_git(self):
        self.assertNotIn("test_exclusions.py", (ROOT / ".gitignore").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
