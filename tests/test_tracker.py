"""Offline tests for the application tracker: dates, week/month/all-time counts, and status handling."""
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

import db
import sources


def job(id_, company="Acme", title="Software Engineer"):
    return sources.Job(id=id_, source="greenhouse", company=company, title=title, location="Austin, TX", url="https://x/" + id_)


class TrackerBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "jobs.db")
        self.conn = db.connect(self.path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def add(self, id_, company="Acme", applied=None, status="new"):
        db.add_job(self.conn, job(id_, company), 0)
        if applied:
            self.conn.execute("UPDATE jobs SET applied_at=?, status=? WHERE id=?", (applied, "applied", id_))
            self.conn.commit()
        elif status != "new":
            self.conn.execute("UPDATE jobs SET status=? WHERE id=?", (status, id_))
            self.conn.commit()

    def row(self, id_):
        return self.conn.execute("SELECT status, applied_at FROM jobs WHERE id=?", (id_,)).fetchone()


class StatsTests(TrackerBase):
    def test_empty_tracker_is_all_zero(self):
        s = db.application_stats(self.conn, date(2026, 10, 30))
        for k in ("week", "month", "all"):
            self.assertEqual((s[k]["applications"], s[k]["companies"]), (0, 0))

    def test_week_runs_monday_through_sunday(self):
        # Friday 2026-10-30: the week is Mon Oct 26 - Sun Nov 1.
        today = date(2026, 10, 30)
        for i, d in enumerate(["2026-10-25", "2026-10-26", "2026-10-29", "2026-11-01", "2026-11-02"]):
            self.add(f"j{i}", applied=d)
        s = db.application_stats(self.conn, today)
        self.assertEqual((s["week"]["start"], s["week"]["end"]), ("2026-10-26", "2026-11-01"))
        self.assertEqual(s["week"]["applications"], 3)   # Oct 26 (Mon), Oct 29, Nov 1 (Sun); not Sun Oct 25 or Mon Nov 2

    def test_week_start_is_correct_on_every_weekday(self):
        for offset in range(7):   # Mon Oct 26 .. Sun Nov 1
            today = date.fromordinal(date(2026, 10, 26).toordinal() + offset)
            s = db.application_stats(self.conn, today)
            self.assertEqual(s["week"]["start"], "2026-10-26", today)
            self.assertEqual(s["week"]["end"], "2026-11-01", today)
        s = db.application_stats(self.conn, date(2026, 11, 2))   # the next Monday starts a new week
        self.assertEqual((s["week"]["start"], s["week"]["end"]), ("2026-11-02", "2026-11-08"))

    def test_month_counts_the_calendar_month_only(self):
        for i, d in enumerate(["2026-09-30", "2026-10-01", "2026-10-31", "2026-11-01"]):
            self.add(f"j{i}", applied=d)
        s = db.application_stats(self.conn, date(2026, 10, 15))
        self.assertEqual(s["month"]["label"], "October 2026")
        self.assertEqual(s["month"]["applications"], 2)
        self.assertEqual(s["all"]["applications"], 4)

    def test_week_and_month_across_the_new_year(self):
        # Thursday 2026-12-31: the week is Mon Dec 28 - Sun Jan 3, but the month is December only.
        for i, d in enumerate(["2026-12-27", "2026-12-28", "2027-01-01", "2027-01-04"]):
            self.add(f"j{i}", applied=d)
        s = db.application_stats(self.conn, date(2026, 12, 31))
        self.assertEqual((s["week"]["start"], s["week"]["end"]), ("2026-12-28", "2027-01-03"))
        self.assertEqual(s["week"]["applications"], 2)
        self.assertEqual(s["month"]["applications"], 2)   # Dec 27 and Dec 28; the January dates are not December
        self.assertEqual(s["all"]["applications"], 4)

    def test_companies_are_counted_once_regardless_of_case(self):
        self.add("a", "Stripe", applied="2026-10-27")
        self.add("b", "stripe", applied="2026-10-28")
        self.add("c", "Datadog", applied="2026-10-29")
        s = db.application_stats(self.conn, date(2026, 10, 30))
        self.assertEqual((s["week"]["applications"], s["week"]["companies"]), (3, 2))
        self.assertEqual((s["all"]["applications"], s["all"]["companies"]), (3, 2))

    def test_jobs_that_were_never_applied_to_do_not_count(self):
        self.add("a")
        self.add("b", status="saved")
        self.add("c", applied="2026-10-27")
        self.assertEqual(db.application_stats(self.conn, date(2026, 10, 30))["all"]["applications"], 1)


class StatusTests(TrackerBase):
    TODAY = date(2026, 10, 30)

    def test_marking_applied_records_the_date(self):
        self.add("a")
        self.assertEqual(db.set_status_exact(self.conn, "a", "applied", today=self.TODAY), 1)
        r = self.row("a")
        self.assertEqual((r["status"], r["applied_at"]), ("applied", "2026-10-30"))

    def test_marking_applied_twice_keeps_the_first_date(self):
        self.add("a")
        db.set_status_exact(self.conn, "a", "applied", today=date(2026, 10, 27))
        db.set_status_exact(self.conn, "a", "applied", today=self.TODAY)
        self.assertEqual(self.row("a")["applied_at"], "2026-10-27")

    def test_interview_implies_applied_and_keeps_the_date(self):
        self.add("a")
        db.set_status_exact(self.conn, "a", "interview", today=self.TODAY)    # applied first, recorded now
        self.assertEqual(self.row("a")["applied_at"], "2026-10-30")
        db.set_status_exact(self.conn, "a", "applied", today=date(2026, 11, 5))
        db.set_status_exact(self.conn, "a", "offer", today=date(2026, 11, 20))
        self.assertEqual(self.row("a")["applied_at"], "2026-10-30")           # later statuses never move the date
        self.assertEqual(db.application_stats(self.conn, date(2026, 11, 20))["all"]["applications"], 1)

    def test_moving_to_a_non_applied_status_removes_it_from_the_tracker(self):
        for status in ("new", "saved", "skipped"):
            self.add(status)
            db.set_status_exact(self.conn, status, "applied", today=self.TODAY)
            db.set_status_exact(self.conn, status, status)
            r = self.row(status)
            self.assertEqual((r["status"], r["applied_at"]), (status, None))

    def test_set_status_by_fragment_still_works_for_the_command_line(self):
        self.add("gh:stripe:123")
        self.assertEqual(db.set_status(self.conn, "stripe:123", "applied", "sent via referral", today=self.TODAY), 1)
        r = self.conn.execute("SELECT status, applied_at, notes FROM jobs").fetchone()
        self.assertEqual((r["status"], r["applied_at"], r["notes"]), ("applied", "2026-10-30", "sent via referral"))

    def test_exact_id_does_not_match_similar_ids(self):
        self.add("gh:a:1")
        self.add("gh:a:10")
        self.assertEqual(db.set_status_exact(self.conn, "gh:a:1", "applied", today=self.TODAY), 1)
        self.assertIsNone(self.row("gh:a:10")["applied_at"])

    def test_changing_the_date_only_works_for_tracked_jobs(self):
        self.add("a")
        self.add("b", applied="2026-10-27")
        self.assertEqual(db.set_applied_date(self.conn, "a", date(2026, 10, 1)), 0)
        self.assertIsNone(self.row("a")["applied_at"])
        self.assertEqual(db.set_applied_date(self.conn, "b", date(2026, 10, 1)), 1)
        self.assertEqual(self.row("b")["applied_at"], "2026-10-01")

    def test_applications_list_is_newest_first(self):
        self.add("a", applied="2026-10-01")
        self.add("b", applied="2026-10-29")
        self.add("c")
        self.assertEqual([r["id"] for r in db.applications(self.conn)], ["b", "a"])


class OutcomeStatusTests(TrackerBase):
    def test_every_outcome_keeps_the_application_counted_with_its_original_date(self):
        self.add("a")
        db.set_status_exact(self.conn, "a", "applied", today=date(2026, 10, 27))
        for outcome in ("interview", "offer", "accepted", "rejected", "withdrawn", "applied"):
            db.set_status_exact(self.conn, "a", outcome, today=date(2026, 11, 20))
            r = self.row("a")
            self.assertEqual((r["status"], r["applied_at"]), (outcome, "2026-10-27"), outcome)
            self.assertEqual(db.application_stats(self.conn, date(2026, 10, 30))["all"]["applications"], 1, outcome)

    def test_jumping_straight_to_an_outcome_still_records_an_application(self):
        for i, outcome in enumerate(("offer", "accepted", "rejected", "withdrawn")):
            self.add(f"j{i}")
            db.set_status_exact(self.conn, f"j{i}", outcome, today=date(2026, 10, 30))
        self.assertEqual(db.application_stats(self.conn, date(2026, 10, 30))["all"]["applications"], 4)

    def test_the_status_lists_agree(self):
        import main
        import viewer
        self.assertEqual(set(viewer.STATUSES), main.STATUSES)
        self.assertTrue(set(db.APPLIED_STATUSES) <= main.STATUSES)


class MigrationTests(unittest.TestCase):
    def test_older_database_gets_the_column_and_keeps_its_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            old = sqlite3.connect(path)
            old.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, source TEXT, company TEXT, title TEXT, location TEXT, url TEXT,"
                        " posted TEXT, pay_min INTEGER, pay_max INTEGER, description TEXT, quick_score REAL, score INTEGER,"
                        " verdict TEXT, analysis TEXT, status TEXT DEFAULT 'new', first_seen REAL, notes TEXT)")
            old.execute("INSERT INTO jobs (id, source, company, title, status) VALUES ('x', 'greenhouse', 'Acme', 'Engineer', 'applied')")
            old.commit()
            old.close()
            conn = db.connect(str(path))
            try:
                r = conn.execute("SELECT status, applied_at FROM jobs").fetchone()
                self.assertEqual((r["status"], r["applied_at"]), ("applied", None))   # no date was ever recorded: not invented
                self.assertEqual(db.application_stats(conn)["all"]["applications"], 0)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
