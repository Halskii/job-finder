"""Stored boards, dropping dead ones, and the crawl: monthly, and at once for titles nobody has crawled."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import requests
import yaml

import boards
import crawl
import db
import discovery
import ingest
import main
import sources
from tests import pgtest
from tests.test_pipeline import Base

WD = {"name": "Big Co", "host": "big.wd1.myworkdayjobs.com", "tenant": "big", "site": "External"}


def http_error(code):
    ex = requests.HTTPError(f"{code} Client Error")
    ex.response = mock.Mock(status_code=code)
    return ex


class BoardStoreTests(Base):
    def test_boards_are_stored_once_whatever_their_case_and_read_back_as_sources(self):
        self.assertEqual(boards.add(self.admin, {"greenhouse": ["Stripe", "reddit"], "workday": [WD]}), 3)
        self.assertEqual(boards.add(self.admin, {"greenhouse": ["stripe", "newco"], "workday": [WD]}), 1)
        got = boards.load(self.admin)
        self.assertEqual(sorted(g.lower() for g in got["greenhouse"]), ["newco", "reddit", "stripe"])
        self.assertEqual(got["workday"], [WD])

    def test_the_older_yaml_file_is_copied_in_once_and_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "discovered_sources.yaml").write_text(yaml.safe_dump({"greenhouse": ["a", "b"], "workday": [WD]}), encoding="utf-8")
            self.assertEqual(boards.import_yaml(self.admin, tmp), 3)
            self.assertEqual(boards.import_yaml(self.admin, tmp), 0)               # not again, even if the file changes
            (Path(tmp) / "discovered_sources.yaml").write_text(yaml.safe_dump({"greenhouse": ["c"]}), encoding="utf-8")
            self.assertEqual(boards.import_yaml(self.admin, tmp), 0)
            self.assertTrue((Path(tmp) / "discovered_sources.yaml").exists())
        self.assertEqual(len(boards.load(self.admin)["greenhouse"]), 2)

    def test_the_config_merges_with_the_stored_boards(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "config.yaml").write_text(yaml.safe_dump({"titles": ["X"], "sources": {"greenhouse": ["mine"]}}), encoding="utf-8")
            boards.add(self.admin, {"greenhouse": ["Mine", "found"]})
            cfg = discovery.load_config(Path(tmp), self.admin)
            self.assertEqual(sorted(s.lower() for s in cfg["sources"]["greenhouse"]), ["found", "mine"])      # config's own board is not repeated

    def test_a_board_found_gone_three_fetches_running_is_dropped_but_other_trouble_never_counts(self):
        boards.add(self.admin, {"greenhouse": ["dead", "flaky", "fine"]})
        for _ in range(2):
            boards.record(self.admin, [("greenhouse", "dead", "gone"), ("greenhouse", "flaky", "error"), ("greenhouse", "fine", "ok")])
        self.assertEqual(boards.prune(self.admin), [])
        boards.record(self.admin, [("greenhouse", "dead", "gone"), ("greenhouse", "flaky", "error")])
        self.assertEqual(boards.prune(self.admin), [("greenhouse", "dead")])
        self.assertEqual(sorted(boards.load(self.admin)["greenhouse"]), ["fine", "flaky"])

    def test_one_good_answer_clears_the_count(self):
        boards.add(self.admin, {"greenhouse": ["wobbly"]})
        boards.record(self.admin, [("greenhouse", "wobbly", "gone")] * 1)
        boards.record(self.admin, [("greenhouse", "wobbly", "gone")])
        boards.record(self.admin, [("greenhouse", "wobbly", "ok")])
        boards.record(self.admin, [("greenhouse", "wobbly", "gone")])
        self.assertEqual(boards.prune(self.admin), [])

    def test_boards_typed_into_the_config_are_never_touched(self):
        boards.record(self.admin, [("greenhouse", "from-config", "gone")] * 5)       # not stored: nothing to update, nothing to drop
        self.assertEqual(boards.prune(self.admin), [])

    def test_how_a_fetch_error_is_read(self):
        self.assertEqual([boards.status_of(e) for e in (None, http_error(404), http_error(410), http_error(500), http_error(429), TimeoutError())],
                         ["ok", "gone", "gone", "error", "error", "error"])

    def test_the_fetch_reports_how_each_board_answered_and_drops_the_dead(self):
        boards.add(self.admin, {"greenhouse": ["dead", "alive"]})
        cfg = {"titles": [], "sources": boards.load(self.admin), "platforms": {}}

        def fetch(slug):
            if slug == "dead":
                raise http_error(404)
            return [sources.Job("gh:alive:1", "greenhouse", "Alive", "Software Engineer", "Austin, TX", "https://x", description="d")]
        with mock.patch.object(discovery.sources, "fetch_greenhouse", fetch), mock.patch.object(main, "collect_feeds", return_value=[]), \
                mock.patch.object(main, "collect_government", return_value=[]), mock.patch.object(main, "collect_retail", return_value=[]), \
                mock.patch.object(main.time, "sleep"):
            for _ in range(3):
                stats = ingest.run_fetch({**cfg, "request_delay": 0, "sources": boards.load(self.admin)}, queue_users=False)
        self.assertEqual(stats["boards_removed"], 1)
        self.assertEqual(boards.load(self.admin)["greenhouse"], ["alive"])


class CrawlTitlesTests(Base):
    def cfg(self, **kw):
        return {"titles": ["Software Engineer"], "sources": {}, **kw}

    def test_with_a_crawl_already_behind_it_the_configs_titles_count_as_crawled(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(main, "ROOT", Path(tmp)):
            (Path(tmp) / crawl.CACHE_FILE).write_text("{}", encoding="utf-8")
            self.assertEqual(ingest.uncrawled_titles(self.admin, self.cfg()), [])
            self.user("a@example.com", {"titles": ["Data Engineer"]})
            self.assertEqual(ingest.uncrawled_titles(self.admin, self.cfg()), ["Data Engineer"])

    def test_on_a_fresh_system_every_title_needs_a_crawl(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(main, "ROOT", Path(tmp)):
            self.assertEqual(ingest.uncrawled_titles(self.admin, self.cfg()), ["Software Engineer"])

    def test_levels_do_not_make_a_title_new(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(main, "ROOT", Path(tmp)):
            db.set_meta(self.admin, "crawl_titles", json.dumps(["software engineer"]))
            db.set_meta(self.admin, "crawl_last", str(time.time()))
            self.user("a@example.com", {"titles": ["Software Engineer II", "Junior Software Engineer", "Associate Platform Engineer"]})
            self.assertEqual(ingest.uncrawled_titles(self.admin, self.cfg()), ["Associate Platform Engineer"])

    def test_the_boards_are_judged_on_everyones_titles_and_nobodys_skip_words(self):
        judge = crawl.crawl_config({"titles": ["x"], "exclude_title_words": ["senior"], "exclude_locations": ["FL"], "min_pay": 90000, "us_only": True},
                                   ["Software Engineer", "Nurse"])
        self.assertEqual((judge["titles"], judge["exclude_title_words"], judge["exclude_locations"], judge["min_pay"], judge["us_only"]),
                         (["Software Engineer", "Nurse"], [], [], 0, True))


class CrawlRunTests(Base):
    """The crawl itself, with Common Crawl's index and the boards' own sites faked."""

    def setUp(self):
        super().setUp()
        self.tmp2 = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp2.name)
        self.patches2 = [mock.patch.object(main, "ROOT", self.root), mock.patch.object(crawl, "resolve_ids", return_value=["CC-1"])]
        self.checked = []
        boards_live = {"gh-nurse": 3, "gh-dev": 3, "gh-none": 0}
        titles_on = {"gh-nurse": "Registered Nurse", "gh-dev": "Platform Engineer", "gh-none": "x"}

        def check(system, cand, cfg):
            self.checked.append((cand, tuple(cfg["titles"])))
            if cand not in boards_live:
                raise http_error(404)
            matched = int(any(t.lower() in titles_on[cand].lower() for t in cfg["titles"]))
            return boards_live[cand], matched
        self.patches2 += [mock.patch.object(crawl, "check", check),
                          mock.patch.object(crawl, "enumerate_candidates", lambda root, systems, crawls, log, ids=None, problems=None: {systems[0]: ["gh-nurse", "gh-dev", "gh-none", "gh-gone"]}),
                          mock.patch.object(crawl.time, "sleep")]
        for p in self.patches2:
            p.start()

    def tearDown(self):
        for p in self.patches2:
            p.stop()
        self.tmp2.cleanup()
        super().tearDown()

    def run_crawl(self, **kw):
        cfg = {"titles": ["Platform Engineer"], "sources": boards.load(self.admin), "request_delay": 0}
        return ingest.run_crawl(self.admin, cfg, self.root, log=lambda *a: None, **kw)

    def only(self, system):
        return mock.patch.object(crawl, "PATTERNS", {system: ["x"]})

    def test_only_boards_that_list_a_posting_for_someones_titles_are_kept(self):
        self.user("n@example.com", {"titles": ["Registered Nurse"]})
        with self.only("greenhouse"):
            res = self.run_crawl()
        self.assertEqual(res["new"], 2)
        self.assertEqual(sorted(boards.load(self.admin)["greenhouse"]), ["gh-dev", "gh-nurse"])      # the one with no matching posting, and the dead one, are not
        self.assertEqual(sorted(res["titles"]), ["Platform Engineer", "Registered Nurse"])

    def test_it_remembers_which_titles_it_covered_and_when(self):
        with self.only("greenhouse"):
            self.run_crawl()
        self.assertEqual(ingest.uncrawled_titles(self.admin, {"titles": ["Platform Engineer"]}), [])
        self.assertLess(time.time() - float(db.get_meta(self.admin, "crawl_last")), 60)

    def test_a_new_title_crawls_only_that_title_and_asks_again_about_boards_it_rejected_before(self):
        db.set_meta(self.admin, "crawl_titles", json.dumps(["platform engineer"]))
        db.set_meta(self.admin, "crawl_last", str(time.time()))
        with self.only("greenhouse"):
            self.assertEqual(self.run_crawl(only_new=True)["titles"], [])                        # nothing new: nothing to do
            self.assertEqual(self.checked, [])
            self.user("n@example.com", {"titles": ["Registered Nurse"]})
            first = self.run_crawl(only_new=True)
            self.assertEqual(first["titles"], ["Registered Nurse"])
            self.assertEqual({c for c, t in self.checked}, {"gh-nurse", "gh-dev", "gh-none", "gh-gone"})     # every candidate, rejected or not
            self.assertTrue(all(t == ("Registered Nurse",) for _, t in self.checked))
            self.assertEqual(boards.load(self.admin)["greenhouse"], ["gh-nurse"])
            self.assertEqual(self.run_crawl(only_new=True)["titles"], [])                        # and now it is covered

    def test_a_new_title_does_not_restart_the_monthly_clock(self):
        db.set_meta(self.admin, "crawl_titles", json.dumps(["platform engineer"]))
        db.set_meta(self.admin, "crawl_last", "1000")
        self.user("n@example.com", {"titles": ["Registered Nurse"]})
        with self.only("greenhouse"):
            self.run_crawl(only_new=True)
        self.assertEqual(db.get_meta(self.admin, "crawl_last"), "1000")

    def test_when_the_index_does_not_answer_nothing_is_marked_as_covered(self):
        with self.only("greenhouse"), mock.patch.object(crawl, "resolve_ids", return_value=None):
            res = self.run_crawl()
        self.assertTrue(res["unavailable"])
        self.assertEqual(ingest.uncrawled_titles(self.admin, {"titles": ["Platform Engineer"]}), ["Platform Engineer"])

    def test_only_one_crawl_runs_at_a_time(self):
        other = pgtest.connect(self.schema, user=False)
        other.execute("SELECT pg_try_advisory_lock(?)", (ingest.CRAWL_LOCK,))
        with self.only("greenhouse"):
            self.assertIn("already running", self.run_crawl()["skipped"])
        other.close()


class NextJobTests(Base):
    def setUp(self):
        super().setUp()
        self.cfg = {"titles": ["Software Engineer"], "sources": {}}
        db.set_meta(self.admin, "crawl_titles", json.dumps(["software engineer"]))

    def job(self, now=None, crawl_days=30, fetch_hours=0):
        return ingest.next_job(self.admin, self.cfg, crawl_days, fetch_hours, now)

    def test_new_titles_come_first_then_the_monthly_crawl_then_the_fetch(self):
        now = time.time()
        db.set_meta(self.admin, "crawl_last", str(now))
        self.assertIsNone(self.job(now))
        self.user("n@example.com", {"titles": ["Registered Nurse"]})
        self.assertEqual(self.job(now), "crawl_new")
        db.set_meta(self.admin, "crawl_titles", json.dumps(["software engineer", "registered nurse"]))
        self.assertIsNone(self.job(now))
        self.assertEqual(self.job(now + 31 * 86400), "crawl")                      # a month later
        self.assertIsNone(self.job(now + 29 * 86400))

    def test_the_daily_fetch_only_when_asked_for_and_due(self):
        now = time.time()
        db.set_meta(self.admin, "crawl_last", str(now))
        self.assertIsNone(self.job(now, fetch_hours=0))
        self.assertEqual(self.job(now, fetch_hours=24), "fetch")                  # never fetched
        db.set_meta(self.admin, "fetch_all_last", str(now - 3600))
        self.assertIsNone(self.job(now, fetch_hours=24))

    def test_the_monthly_crawl_can_be_turned_off(self):
        self.assertIsNone(self.job(time.time() + 400 * 86400, crawl_days=0))

    def test_a_job_started_in_the_last_hour_is_not_started_again_in_case_it_failed(self):
        now = time.time()
        self.user("n@example.com", {"titles": ["Registered Nurse"]})
        db.set_meta(self.admin, "crawl_attempt", str(now - 600))
        self.assertIsNone(self.job(now))
        self.assertEqual(self.job(now + 3700), "crawl_new")
        db.set_meta(self.admin, "crawl_last", str(now))
        db.set_meta(self.admin, "crawl_attempt", str(now - 4000))
        db.set_meta(self.admin, "crawl_titles", json.dumps(["software engineer", "registered nurse"]))
        db.set_meta(self.admin, "fetch_attempt", str(now - 60))
        self.assertIsNone(self.job(now, fetch_hours=24))


    def test_retry_hours_stretches_the_wait_so_a_crawl_that_never_finishes_cleanly_runs_once_a_day(self):
        now = time.time()
        self.user("n@example.com", {"titles": ["Registered Nurse"]})           # its titles never get marked as covered
        db.set_meta(self.admin, "crawl_attempt", str(now - 3 * 3600))
        self.assertEqual(ingest.next_job(self.admin, self.cfg, 30, 0, now), "crawl_new")              # the default: tried again after an hour
        self.assertIsNone(ingest.next_job(self.admin, self.cfg, 30, 0, now, retry_hours=24))
        self.assertEqual(ingest.next_job(self.admin, self.cfg, 30, 0, now + 22 * 3600, retry_hours=24), "crawl_new")
        db.set_meta(self.admin, "crawl_attempt", str(now - 7200))
        db.set_meta(self.admin, "crawl_titles", json.dumps(["software engineer", "registered nurse"]))
        db.set_meta(self.admin, "crawl_last", str(now))
        db.set_meta(self.admin, "fetch_attempt", str(now - 7200))
        self.assertIsNone(ingest.next_job(self.admin, self.cfg, 30, 24, now, retry_hours=24))        # fetch_all_last is old, but a fetch was started
        self.assertEqual(ingest.next_job(self.admin, self.cfg, 30, 24, now), "fetch")                 # the default wait of an hour would start it again


class RunJobTests(Base):
    def test_a_crawl_that_finds_boards_is_followed_by_a_fetch_and_one_that_finds_none_is_not(self):
        with mock.patch.object(ingest, "run_crawl", return_value={"new": 3}) as crawled, mock.patch.object(ingest, "run_fetch", return_value={"fetched": 5}) as fetched, \
                mock.patch.object(main.discovery, "load_config", return_value={"titles": []}):
            out = ingest.run_job("crawl_new")
            self.assertEqual((out["fetch"], crawled.call_args.kwargs["only_new"]), ({"fetched": 5}, True))
            ingest.run_job("crawl")
            self.assertFalse(crawled.call_args.kwargs["only_new"])
        with mock.patch.object(ingest, "run_crawl", return_value={"new": 0}), mock.patch.object(ingest, "run_fetch") as fetched, \
                mock.patch.object(main.discovery, "load_config", return_value={"titles": []}):
            self.assertNotIn("fetch", ingest.run_job("crawl"))
            fetched.assert_not_called()


if __name__ == "__main__":
    unittest.main()
