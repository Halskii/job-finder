"""Attaching the data from before accounts existed to a real account."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import auth
import claim_local
import db
import sources
import userdata
from tests import pgtest


class ClaimTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src, self.base = Path(self.tmp.name) / "project", Path(self.tmp.name) / "users"
        (self.src / "reports").mkdir(parents=True)
        (self.src / "reports" / "2026-10-30.md").write_text("# r", encoding="utf-8")
        (self.src / "profile.json").write_text("{}", encoding="utf-8")
        self.local = pgtest.connect()
        db.add_job(self.local, sources.Job("gh:a:1", "greenhouse", "A", "Dev", "x", "https://x", description="d"), 1.0)
        self.anon = db.connect(pgtest.url(), schema=self.local.schema, user=False)

    def tearDown(self):
        self.anon.close()
        self.tmp.cleanup()

    def test_the_local_account_becomes_yours_with_its_jobs_and_files(self):
        with mock.patch.object(userdata, "BASE", self.base), mock.patch.object(userdata, "ROOT", self.src):
            uid, copied = claim_local.claim(self.anon, "Me@Example.com")
            self.assertEqual(uid, self.local.user_id)
            self.assertEqual(sorted(copied), ["profile.json", "reports/"])
            self.assertTrue((self.base / str(uid) / "reports" / "2026-10-30.md").exists())
            self.assertTrue((self.src / "profile.json").exists())                              # the originals stay
            self.assertEqual(userdata.adopt_project_files(uid, self.src), [])                  # nothing is copied over twice
        mine = pgtest.connect(self.local.schema, user="me@example.com")
        self.assertEqual(mine.user_id, uid)
        self.assertEqual(mine.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"], 1)

    def test_an_email_that_already_has_an_account_is_refused(self):
        auth.sign_up(self.anon, "taken@example.com", "a long password")
        with self.assertRaises(SystemExit):
            claim_local.claim(self.anon, "taken@example.com")

    def test_folders_are_named_by_account_number_only(self):
        with mock.patch.object(userdata, "BASE", self.base):
            for bad in (0, -1, "1", "../x", None, 1.5):
                with self.assertRaises(ValueError):
                    userdata.home(bad)
            self.assertEqual(userdata.home(7), self.base / "7")


if __name__ == "__main__":
    unittest.main()
