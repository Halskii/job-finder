"""The shared fetch: storing postings for everyone, dropping stale ones, reading full text on demand, and the scheduler."""
import json
import tempfile
import threading
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import psycopg

import auth
import db
import ingest
import main
import pipeline
import sources
import userdata
import web
from tests import pgtest
from tests.test_pipeline import PROFILE, SETTINGS, Base, add_posting


def job(jid, title="Software Engineer", company="Acme", description="Build things in Python.", detail=None, posted=None, **kw):
    return sources.Job(jid, kw.pop("source", "greenhouse"), company, title, kw.pop("location", "Austin, TX"), "https://x/" + jid,
                       posted=posted or date.today().isoformat(), description=description, detail=detail or {}, **kw)


class StoreTests(Base):
    def count(self):
        return self.admin.execute("SELECT COUNT(*) AS n FROM postings").fetchone()["n"]

    def test_everything_fetched_is_stored_once_and_a_second_fetch_adds_only_what_is_new(self):
        stats = ingest.store(self.admin, [job("a"), job("b", company="B"), job("a")])      # a duplicate within one fetch is fine
        self.assertEqual((stats["fetched"], stats["new"], self.count()), (3, 2, 2))
        stats = ingest.store(self.admin, [job("a"), job("c", company="C")])
        self.assertEqual((stats["new"], self.count()), (1, 3))

    def test_a_posting_still_listed_is_marked_seen_and_keeps_its_text_and_where_it_was_first_fetched(self):
        ingest.store(self.admin, [job("a", description="Original text.")])
        first = self.admin.execute("SELECT fetched_at, last_seen FROM postings WHERE id = 'a'").fetchone()
        time.sleep(0.02)
        ingest.store(self.admin, [job("a", description="Edited text.")])
        row = self.admin.execute("SELECT description, fetched_at, last_seen FROM postings WHERE id = 'a'").fetchone()
        self.assertEqual(row["description"], "Original text.")
        self.assertEqual(row["fetched_at"], first["fetched_at"])             # so users' "new since last time" stays right
        self.assertGreater(row["last_seen"], first["last_seen"])

    def test_a_posting_stored_with_only_a_title_gets_its_text_when_a_later_fetch_has_it(self):
        ingest.store(self.admin, [job("a", description="", detail={"api": "https://x/api"})])
        ingest.store(self.admin, [job("a", description="Now with text.")])
        self.assertEqual(self.admin.execute("SELECT description FROM postings").fetchone()["description"], "Now with text.")

    def test_old_postings_are_not_kept_and_a_title_only_posting_remembers_how_to_read_it(self):
        old = (date.today() - timedelta(days=200)).isoformat()
        stats = ingest.store(self.admin, [job("old", posted=old), job("wd", description="", detail={"api": "https://x/api", "gov": True}), job("nodate", posted="")])
        self.assertEqual((stats["skipped_old"], self.count()), (1, 2))
        row = self.admin.execute("SELECT description, detail FROM postings WHERE id = 'wd'").fetchone()
        self.assertEqual((row["description"], json.loads(row["detail"])), ("", {"api": "https://x/api", "gov": True}))

    def test_an_hourly_rate_in_the_text_is_kept_as_hourly_pay(self):
        ingest.store(self.admin, [job("h", description="The pay range is $40/hr-45/hr.")])
        row = self.admin.execute("SELECT pay_min, pay_max, pay_unit FROM postings").fetchone()
        self.assertEqual((row["pay_min"], row["pay_max"], row["pay_unit"]), (40, 45, "hour"))


class PurgeTests(Base):
    def test_unlisted_postings_go_unless_someone_has_them_as_a_job(self):
        long_ago = time.time() - 30 * 86400
        for pid in ("gone1", "gone2", "kept_by_user", "recent"):
            add_posting(self.admin, pid, "Software Engineer", company=pid)
        self.admin.execute("UPDATE postings SET last_seen = ? WHERE id <> 'recent'", (long_ago,))
        self.admin.commit()
        uid = self.user()
        conn = pgtest.connect(self.schema, user=uid)
        conn.execute("INSERT INTO user_jobs (user_id, job_id) VALUES (?, 'kept_by_user')", (uid,))
        conn.commit()
        conn.close()
        anon = pgtest.connect(self.schema, user=False)      # the fetch cannot see any user's jobs at all, and still must not delete them
        self.assertEqual(ingest.purge(anon, 14), 2)
        left = sorted(r["id"] for r in anon.execute("SELECT id FROM postings").fetchall())
        anon.close()
        self.assertEqual(left, ["kept_by_user", "recent"])
        self.assertEqual(self.jobs(uid), ["kept_by_user"])

    def test_a_big_pile_with_one_kept_posting_in_the_middle_is_still_cleared(self):
        for i in range(450):
            add_posting(self.admin, f"s{i:03d}", "Software Engineer", company=f"C{i}")
        self.admin.execute("UPDATE postings SET last_seen = 1")
        self.admin.commit()
        uid = self.user()
        conn = pgtest.connect(self.schema, user=uid)
        conn.execute("INSERT INTO user_jobs (user_id, job_id) VALUES (?, 's222')", (uid,))
        conn.commit()
        conn.close()
        self.assertEqual(ingest.purge(self.admin, 14), 449)
        self.assertEqual(self.admin.execute("SELECT COUNT(*) AS n FROM postings").fetchone()["n"], 1)


class DetailTests(Base):
    def add(self, pid, **kw):
        add_posting(self.admin, pid, "Software Engineer", description="", **kw)
        self.admin.execute("UPDATE postings SET detail = ? WHERE id = ?", (json.dumps({"api": "https://x/" + pid}), pid))
        self.admin.commit()

    def fake_prefetch(self, fail=(), location="Austin, TX"):
        def prefetch(jobs, workers, delay):
            for j in jobs:
                if j.id not in fail:
                    j.description, j.location, j.pay_min, j.pay_max = "Full text. Pay $120,000 - $150,000.", location, 120000, 150000
            return set(fail)
        return mock.patch.object(main, "prefetch_details", prefetch)

    def test_the_full_text_is_read_and_kept_for_everyone(self):
        self.add("a", location="2 Locations")
        with self.fake_prefetch(location="Austin, TX"):
            self.assertEqual(ingest.ensure_details(self.admin, ["a"]), 0)
        row = self.admin.execute("SELECT description, location, pay_min, detail_tries FROM postings").fetchone()
        self.assertEqual((row["description"][:9], row["location"], row["pay_min"], row["detail_tries"]), ("Full text", "Austin, TX", 120000, 0))

    def test_a_posting_that_cannot_be_read_is_tried_a_few_times_and_then_left_alone(self):
        self.add("a")
        calls = []
        with self.fake_prefetch(fail={"a"}), mock.patch.object(main, "prefetch_details", side_effect=lambda jobs, w, d: calls.append(1) or {j.id for j in jobs}):
            results = [ingest.ensure_details(self.admin, ["a"]) for _ in range(5)]
        self.assertEqual(results, [1, 1, 1, 0, 0])
        self.assertEqual(len(calls), 3)

    def test_postings_that_already_have_text_are_not_asked_for_again(self):
        add_posting(self.admin, "done", "Software Engineer", description="Already here.")
        with mock.patch.object(main, "prefetch_details") as prefetch:
            self.assertEqual(ingest.ensure_details(self.admin, ["done"]), 0)
        prefetch.assert_not_called()


class MatchWithDetailsTests(DetailTests):
    def test_title_only_postings_are_read_then_judged_on_the_full_text(self):
        self.add("ok", location="2 Locations")
        self.add("abroad", location="2 Locations")
        uid = self.user()
        conn = pgtest.connect(self.schema, user=uid)
        rules = pipeline.Rules(SETTINGS)

        def prefetch(jobs, workers, delay):
            for j in jobs:
                j.description = "Full text."
                j.location = "London, UK" if j.id == "abroad" else "Austin, TX"
            return set()
        with mock.patch.object(main, "prefetch_details", prefetch):
            self.assertEqual(pipeline.match(conn, rules, PROFILE, read_missing=False), (0, 0))      # nothing read yet
            self.assertEqual(self.jobs(uid), [])
            self.assertEqual(pipeline.match(conn, rules, PROFILE, read_missing=True), (1, 0))
        conn.close()
        self.assertEqual(self.jobs(uid), ["ok"])         # the other one turned out to be in London

    def test_one_that_could_not_be_read_this_time_is_counted_so_it_is_looked_at_again(self):
        self.add("a")
        uid = self.user()
        conn = pgtest.connect(self.schema, user=uid)
        with self.fake_prefetch(fail={"a"}):
            self.assertEqual(pipeline.match(conn, pipeline.Rules(SETTINGS), PROFILE, read_missing=True), (0, 1))
        conn.close()

    def test_the_first_scores_are_made_before_the_title_only_sites_are_read(self):
        add_posting(self.admin, "described", "Software Engineer", company="D")
        self.add("later", company="L")
        uid = self.user()
        order = []
        client, _ = self.client()
        real_create = client.messages.create
        client.messages.create = lambda **kw: order.append("score") or real_create(**kw)

        def prefetch(jobs, workers, delay):
            order.append("read")
            for j in jobs:
                j.description = "Full text."
            return set()
        with mock.patch.object(main, "prefetch_details", prefetch):
            st = pipeline.run_user(uid, client)
        self.assertEqual(order, ["score", "read", "score"])      # one scored at once, then the read, then the one that needed it
        self.assertEqual((st["state"], st["matched"], st["scored"]), ("done", 2, 2))

    def test_an_unreadable_posting_keeps_the_watermark_back_so_the_next_run_tries_again(self):
        add_posting(self.admin, "described", "Software Engineer", company="D")
        self.add("later", company="L")
        uid = self.user()
        client, _ = self.client()
        with self.fake_prefetch(fail={"later"}):
            pipeline.run_user(uid, client)
        conn = pgtest.connect(self.schema, user=uid)
        self.assertIsNone(conn.execute("SELECT watermark FROM user_runs").fetchone()["watermark"])     # not advanced: the next run checks everything again
        conn.close()
        with self.fake_prefetch():
            st = pipeline.run_user(uid, client)
        self.assertEqual((st["matched"], st["scored"]), (2, 1))
        conn = pgtest.connect(self.schema, user=uid)
        self.assertIsNotNone(conn.execute("SELECT watermark FROM user_runs").fetchone()["watermark"])
        conn.close()


class FetchRunTests(Base):
    def run_fetch(self, jobs, **kw):
        with mock.patch.object(main, "collect", return_value=jobs), mock.patch.object(main.discovery, "load_config", return_value={"titles": ["Platform Engineer"]}):
            return ingest.run_fetch(**kw)

    def test_titles_to_search_are_the_configs_plus_everyones_without_repeats(self):
        self.user("a@example.com", {"titles": ["Software Engineer", "Data Engineer"]})
        self.user("b@example.com", {"titles": ["software engineer", "SRE"]})
        self.assertEqual(ingest.search_titles({"titles": ["Platform Engineer"]}, self.anon),
                         ["Platform Engineer", "Software Engineer", "Data Engineer", "SRE"])

    def test_a_fetch_stores_purges_records_when_it_ran_and_queues_only_users_with_a_search(self):
        ready = self.user("ready@example.com")
        self.user("notitles@example.com", {"titles": []})
        self.user("noprofile@example.com", profile=None)
        add_posting(self.admin, "stale", "Software Engineer", company="S")
        self.admin.execute("UPDATE postings SET last_seen = 1")
        self.admin.commit()
        seen = {}

        def collect(cfg, conn, outcomes=None, sink=None):
            seen["titles"] = cfg["titles"]
            return [job("a"), job("b", company="B")]
        with mock.patch.object(main, "collect", collect), mock.patch.object(main.discovery, "load_config", return_value={"titles": ["Platform Engineer"]}):
            stats = ingest.run_fetch()
        self.assertEqual((stats["fetched"], stats["new"], stats["purged"], stats["queued"]), (2, 2, 1, 1))
        self.assertIn("Software Engineer", seen["titles"])                          # the sources were searched for what users want
        self.assertEqual(self.anon.execute("SELECT COUNT(*) AS n FROM postings").fetchone()["n"], 2)
        conn = pgtest.connect(self.schema, user=ready)
        self.assertEqual(pipeline.status(conn)["state"], "queued")
        conn.close()
        self.assertFalse(ingest.due(self.anon, 24))                                  # it just ran

    def test_postings_handed_over_while_reading_are_stored_and_counted_with_the_rest(self):
        def collect(cfg, conn, outcomes=None, sink=None):
            sink([job("a"), job("b", company="B")])           # what a big reader does as each source finishes
            sink([job("a"), job("c", company="C")])           # "a" again: the same posting listed twice is stored once
            return [job("d", company="D")]                      # and what is left comes back as before
        with mock.patch.object(main, "collect", collect), mock.patch.object(main.discovery, "load_config", return_value={"titles": []}):
            stats = ingest.run_fetch(queue_users=False)
        self.assertEqual((stats["fetched"], stats["new"], stats["skipped_old"]), (5, 4, 0))
        self.assertEqual(self.anon.execute("SELECT COUNT(*) AS n FROM postings").fetchone()["n"], 4)

    def test_the_saver_counts_too_old_postings_and_leaves_no_transaction_open(self):
        saver = ingest.Saver(self.admin, 30)
        self.assertEqual(self.admin.raw.info.transaction_status, psycopg.pq.TransactionStatus.IDLE)   # not held open while the sources are read
        saver([job("a"), job("b", company="B", posted="2020-01-01")])
        self.assertEqual(saver.stats(), {"fetched": 2, "new": 1, "skipped_old": 1})

    def test_give_back_memory_never_fails(self):
        ingest.give_back_memory()                              # on Windows there is no malloc_trim, and that is fine

    def test_queueing_can_be_turned_off_and_nothing_is_run_in_the_command(self):
        self.user()
        stats = self.run_fetch([job("a")], queue_users=False)
        self.assertEqual(stats["queued"], 0)

    def test_only_one_fetch_runs_at_a_time(self):
        other = pgtest.connect(self.schema, user=False)
        self.assertTrue(other.execute("SELECT pg_try_advisory_lock(?) AS ok", (ingest.LOCK,)).fetchone()["ok"])
        with mock.patch.object(main, "collect") as collect:
            stats = ingest.run_fetch({"titles": []})
        collect.assert_not_called()
        self.assertIn("already running", stats["skipped"])
        other.execute("SELECT pg_advisory_unlock(?)", (ingest.LOCK,))
        other.close()
        self.assertNotIn("skipped", self.run_fetch([]))                              # and the lock is free again afterwards

    def test_due_follows_the_last_fetch_and_zero_hours_means_never(self):
        self.assertTrue(ingest.due(self.anon, 24))
        db.set_meta(self.anon, "fetch_all_last", str(time.time() - 3600))
        self.assertFalse(ingest.due(self.anon, 24))
        self.assertTrue(ingest.due(self.anon, 0.5))
        self.assertFalse(ingest.due(self.anon, 0))

    def test_a_new_users_matches_come_from_what_the_shared_fetch_stored(self):
        self.run_fetch([job("a", title="Platform Engineer"), job("b", title="Marketing Lead", company="B")])
        uid = self.user("late@example.com", {"titles": ["Platform Engineer"]})        # signs up after the fetch ran
        client, _ = self.client()
        st = pipeline.run_user(uid, client)
        self.assertEqual((st["matched"], st["scored"]), (1, 1))


class QueueTests(Base):
    def test_a_user_marked_waiting_by_another_process_is_handed_to_the_worker(self):
        uid = self.user()
        pipeline.mark_queued(uid)
        with mock.patch.object(pipeline.WORKER, "submit") as submit:
            self.assertEqual(pipeline.poll_queued(), 1)
            submit.assert_called_once_with(uid)
            pipeline._save(pgtest.connect(self.schema, user=uid), state="done")
            self.assertEqual(pipeline.poll_queued(), 0)


class SchedulerTests(Base):
    class Stop(Exception):
        pass

    def run_one_round(self, job):
        """One pass of the scheduler, with `job` as the answer to 'what is due'. Returns (jobs run, polls made)."""
        ran, started = [], threading.Event()
        with mock.patch.object(web.pipeline, "poll_queued") as poll, mock.patch.object(ingest, "next_job", return_value=job),                 mock.patch.object(ingest, "run_job", side_effect=lambda j, connect=None: ran.append(j) or started.set() or {}),                 mock.patch.object(main.discovery, "load_config", return_value={}),                 mock.patch.object(web.WAKE, "wait", side_effect=self.Stop):
            with self.assertRaises(self.Stop):
                web.scheduler()
            if job:
                self.assertTrue(started.wait(5))
        return ran, poll.call_count

    def test_it_polls_the_queue_and_starts_whatever_upkeep_is_due(self):
        self.assertEqual(self.run_one_round("crawl_new"), (["crawl_new"], 1))

    def test_with_nothing_due_it_only_polls_the_queue(self):
        self.assertEqual(self.run_one_round(None), ([], 1))

    def test_saving_a_profile_wakes_it_at_once(self):
        web.WAKE.clear()
        uid, token = None, None
        from tests import webtest
        with tempfile.TemporaryDirectory() as tmp:
            site = webtest.Site(tmp)
            try:
                import urllib.request
                req = urllib.request.Request(site.base + "/api/profile/save", method="POST", headers={"Content-Type": "application/json", "Cookie": f"{web.COOKIE}={site.token}"},
                                             data=json.dumps({"settings": {"titles": ["Brand New Title"]}, "profile": {"name": "N"}}).encode())
                urllib.request.urlopen(req).read()
                self.assertTrue(web.WAKE.is_set())
            finally:
                site.stop()
                web.WAKE.clear()


class CommandTests(unittest.TestCase):
    def test_the_command_reports_what_the_fetch_did(self):
        out = []
        stats = {"fetched": 1200, "new": 80, "skipped_old": 5, "purged": 9, "queued": 3, "seconds": 41}
        with mock.patch("ingest.run_fetch", return_value=stats) as run, mock.patch.object(main, "log", out.append):
            main.cmd_fetch(mock.Mock(keep_days=7, no_queue=False), {"keep_postings_days": 14}, {}, None)
        self.assertEqual(run.call_args.args[0]["keep_postings_days"], 7)
        self.assertIn("80 new", out[0])
        self.assertIn("Queued 3", out[0])
        with mock.patch("ingest.run_fetch", return_value={"skipped": "another fetch is already running"}), mock.patch.object(main, "log", out.append):
            main.cmd_fetch(mock.Mock(keep_days=None, no_queue=True), {}, {}, None)
        self.assertIn("already running", out[-1])


if __name__ == "__main__":
    unittest.main()
