"""Two users on one database: postings are shared, everything else is private, and the database itself enforces it."""
import unittest

import psycopg

import db
import sources
from tests import pgtest


def posting(job_id="gh:acme:1", title="Platform Engineer"):
    return sources.Job(job_id, "greenhouse", "Acme", title, "Austin, TX", "https://example.com/" + job_id, description="Build things.")


class IsolationTests(unittest.TestCase):
    def setUp(self):
        self.a = pgtest.connect(user="a@example.com")
        self.b = pgtest.connect(self.a.schema, user="b@example.com")
        db.add_job(self.a, posting(), 1.0)
        db.add_job(self.b, posting(), 2.0)

    def mine(self, conn):
        return conn.execute("SELECT id, score, status, notes, applied_at FROM jobs").fetchall()

    def test_a_posting_is_stored_once_and_each_user_has_their_own_copy_of_the_rest(self):
        self.assertEqual(self.a.execute("SELECT COUNT(*) AS n FROM postings").fetchone()["n"], 1)
        db.save_score(self.a, "gh:acme:1", 90, "apply", {"reason": "x"})
        db.set_status_exact(self.a, "gh:acme:1", "applied", "my note")
        a, b = self.mine(self.a)[0], self.mine(self.b)[0]
        self.assertEqual((a["score"], a["status"], a["notes"]), (90, "applied", "my note"))
        self.assertEqual((b["score"], b["status"], b["notes"], b["applied_at"]), (None, "new", None, None))
        self.assertEqual(db.applications(self.b), [])
        self.assertEqual(db.application_stats(self.b)["all"]["applications"], 0)

    def test_a_user_only_sees_the_jobs_they_matched(self):
        db.add_job(self.a, posting("gh:acme:2", "SRE"), 1.0)
        self.assertEqual(len(self.mine(self.a)), 2)
        self.assertEqual([r["id"] for r in self.mine(self.b)], ["gh:acme:1"])
        self.assertEqual(self.b.execute("SELECT 1 FROM jobs WHERE id='gh:acme:2'").fetchone(), None)

    def test_interview_rounds_and_prep_are_private(self):
        db.add_round(self.a, "gh:acme:1", "Phone screen")
        db.save_tips(self.a, "gh:acme:1", {"markdown": "## Tips"}, "m", "2026-10-31")
        self.assertEqual(len(db.rounds_for(self.a, "gh:acme:1")), 1)
        self.assertEqual(db.rounds_for(self.b, "gh:acme:1"), [])
        self.assertEqual(db.rounds_by_job(self.b), {})
        self.assertIsNone(db.get_tips(self.b, "gh:acme:1"))
        self.assertEqual(db.jobs_with_tips(self.b), [])
        self.assertEqual(len(db.jobs_with_tips(self.a)), 1)

    def test_a_query_that_forgets_to_filter_by_user_still_sees_only_that_users_rows(self):
        for table in ("user_jobs", "interview_rounds", "interview_tips"):
            self.assertEqual(self.b.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"],
                             self.b.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE user_id = ?", (self.b.user_id,)).fetchone()["n"])
        db.add_round(self.a, "gh:acme:1")
        self.assertEqual(self.b.execute("SELECT COUNT(*) AS n FROM interview_rounds").fetchone()["n"], 0)

    def test_one_user_cannot_change_or_delete_another_users_rows(self):
        db.add_round(self.a, "gh:acme:1")
        db.set_status_exact(self.a, "gh:acme:1", "saved")
        self.assertEqual(self.b.execute("UPDATE user_jobs SET status='skipped' WHERE user_id = ?", (self.a.user_id,)).rowcount, 0)
        self.assertEqual(self.b.execute("DELETE FROM interview_rounds").rowcount, 0)
        self.assertEqual(self.b.execute("UPDATE interview_rounds SET kind='x'").rowcount, 0)
        self.assertEqual(self.mine(self.a)[0]["status"], "saved")
        self.assertEqual(len(db.rounds_for(self.a, "gh:acme:1")), 1)

    def test_a_user_cannot_write_rows_for_someone_else(self):
        db.add_job(self.a, posting("gh:acme:3", "Dev"), 1.0)
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            self.b.execute("INSERT INTO user_jobs (user_id, job_id) VALUES (?, ?)", (self.a.user_id, "gh:acme:3"))
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            self.b.execute("INSERT INTO interview_tips (user_id, job_id, content) VALUES (?, ?, '{}')", (self.a.user_id, "gh:acme:1"))
        self.b.rollback()
        self.assertEqual([r["id"] for r in self.mine(self.b)], ["gh:acme:1"])   # and the connection still works

    def test_a_connection_with_no_user_sees_nothing(self):
        self.b.execute("SELECT set_config('app.user_id', '', false)")
        self.assertEqual(self.mine(self.b), [])
        self.assertEqual(self.b.execute("SELECT COUNT(*) AS n FROM user_jobs").fetchone()["n"], 0)

    def test_a_status_change_by_part_of_the_id_only_touches_your_own_jobs(self):
        self.assertEqual(db.set_status(self.a, "acme", "skipped"), 1)
        self.assertEqual(self.mine(self.b)[0]["status"], "new")

    def test_the_shared_text_is_shared_and_a_users_own_columns_are_not(self):
        self.a.execute("UPDATE postings SET description='Better text', formatted=1")
        self.a.commit()
        self.assertEqual(self.b.execute("SELECT description FROM jobs").fetchone()["description"], "Better text")
        self.a.execute("UPDATE jobs SET notes='private' WHERE id='gh:acme:1'")   # the jobs view routes this to the user's own row
        self.a.commit()
        self.assertEqual(self.mine(self.b)[0]["notes"], None)
        self.assertEqual(self.mine(self.a)[0]["notes"], "private")

    def test_deleting_an_account_removes_only_their_data_and_leaves_the_postings(self):
        db.add_round(self.a, "gh:acme:1")
        self.a.execute("DELETE FROM users WHERE id = ?", (self.a.user_id,))
        self.a.commit()
        self.assertEqual(self.mine(self.b)[0]["id"], "gh:acme:1")
        self.assertEqual(self.b.execute("SELECT COUNT(*) AS n FROM postings").fetchone()["n"], 1)
        self.assertEqual(self.b.execute("SELECT COUNT(*) AS n FROM users WHERE id = ?", (self.a.user_id,)).fetchone()["n"], 0)

    def test_accounts_are_found_by_email_ignoring_case_and_never_duplicated(self):
        again = pgtest.connect(self.a.schema, user="A@Example.com")
        self.assertEqual(again.user_id, self.a.user_id)
        self.assertEqual(self.a.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"], 2)


if __name__ == "__main__":
    unittest.main()
