"""Offline tests for board discovery, the new fetchers (SmartRecruiters, Remotive), cross-source de-duplication,
and the Remotive request throttle. No network."""
import argparse
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import db
import discovery
import main
import sources

CONFIG = """# my comment
min_pay: 80000
us_only: true
titles: [Software Engineer, Platform Engineer]
exclude_title_words: [senior]
exclude_locations: [California, CA, San Francisco]
request_delay: 0
sources:
  greenhouse: [stripe]
  workday:
    - {name: Vanguard, host: vanguard.wd5.myworkdayjobs.com, tenant: vanguard, site: vanguard_external}
"""


def job(id_, source, company, title, location="Austin, TX", **kw):
    return sources.Job(id=id_, source=source, company=company, title=title, location=location, url="https://x/" + id_, **kw)


class SlugTests(unittest.TestCase):
    def test_candidates(self):
        self.assertEqual(discovery.slug_candidates("Palo Alto Networks, Inc."), ["paloaltonetworks", "palo-alto-networks"])
        self.assertEqual(discovery.slug_candidates("Stripe"), ["stripe"])
        self.assertEqual(discovery.slug_candidates("Block, Inc."), ["block"])
        self.assertEqual(discovery.slug_candidates("Inc."), [])

    def test_probe_uses_hyphen_only_when_joined_misses_and_stops_after_a_hit(self):
        calls = []

        def gh(slug):
            calls.append(("gh", slug))
            return [job("1", "greenhouse", slug, "Software Engineer")] if slug == "palo-alto-networks" else []

        def lv(slug):
            calls.append(("lv", slug))
            raise ValueError("404")   # a bad slug must read as a miss, not crash

        hits = discovery.probe("Palo Alto Networks", delay=0, systems=[("greenhouse", gh), ("lever", lv)])
        self.assertEqual([(h.system, h.slug) for h in hits], [("greenhouse", "palo-alto-networks")])
        self.assertEqual(calls, [("gh", "paloaltonetworks"), ("gh", "palo-alto-networks"),
                                 ("lv", "paloaltonetworks"), ("lv", "palo-alto-networks")])

    def test_probe_stops_at_first_working_slug(self):
        seen = []

        def gh(slug):
            seen.append(slug)
            return [job("1", "greenhouse", slug, "Software Engineer")]

        discovery.probe("Palo Alto Networks", delay=0, systems=[("greenhouse", gh)])
        self.assertEqual(seen, ["paloaltonetworks"])


class WorkdayUrlTests(unittest.TestCase):
    def test_matches_the_entries_already_in_the_config(self):
        self.assertEqual(
            discovery.parse_workday_url("https://vanguard.wd5.myworkdayjobs.com/en-US/vanguard_external/job/Malvern/x_R1", "Vanguard"),
            {"name": "Vanguard", "host": "vanguard.wd5.myworkdayjobs.com", "tenant": "vanguard", "site": "vanguard_external"})
        self.assertEqual(
            discovery.parse_workday_url("https://alignmenthealthcare5.impl-wd12.myworkdayjobs.com/ahc_external"),
            {"name": "Alignmenthealthcare5", "host": "alignmenthealthcare5.impl-wd12.myworkdayjobs.com",
             "tenant": "alignmenthealthcare5", "site": "ahc_external"})

    def test_rejects_other_hosts_and_missing_site(self):
        with self.assertRaises(ValueError):
            discovery.parse_workday_url("https://example.com/careers")
        with self.assertRaises(ValueError):
            discovery.parse_workday_url("https://vanguard.wd5.myworkdayjobs.com/en-US")
        with self.assertRaises(ValueError):
            discovery.parse_workday_url("https://evilmyworkdayjobs.com/en-US/site")


class ConfigMergeTests(unittest.TestCase):
    def test_merge_is_case_insensitive_and_keeps_order(self):
        base = {"greenhouse": ["stripe", "Datadog"], "workday": [{"host": "h", "tenant": "t", "site": "s"}]}
        extra = {"greenhouse": ["STRIPE", "reddit"], "lever": ["plaid"],
                 "workday": [{"host": "h", "tenant": "t", "site": "s"}, {"host": "h2", "tenant": "t2", "site": "s2"}]}
        out = discovery.merge_sources(base, extra)
        self.assertEqual(out["greenhouse"], ["stripe", "Datadog", "reddit"])
        self.assertEqual(out["lever"], ["plaid"])
        self.assertEqual(len(out["workday"]), 2)
        self.assertEqual(base["greenhouse"], ["stripe", "Datadog"])   # inputs are not mutated

    def test_save_then_load_merges_without_touching_config_yaml(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.yaml").write_text(CONFIG, encoding="utf-8")
            discovery.save_discovered(root, {"greenhouse": ["reddit", "stripe"], "smartrecruiters": ["BoschGroup"]})
            discovery.save_discovered(root, {"workday": [{"name": "X", "host": "x.wd1.myworkdayjobs.com", "tenant": "x", "site": "ext"}]})
            cfg = discovery.load_config(root)
            self.assertEqual(cfg["sources"]["greenhouse"], ["stripe", "reddit"])
            self.assertEqual(cfg["sources"]["smartrecruiters"], ["BoschGroup"])
            self.assertEqual([e["tenant"] for e in cfg["sources"]["workday"]], ["vanguard", "x"])
            self.assertEqual((root / "config.yaml").read_text(encoding="utf-8"), CONFIG)   # comments intact

    def test_missing_discovered_file_is_fine(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "config.yaml").write_text(CONFIG, encoding="utf-8")
            self.assertEqual(discovery.load_config(Path(tmp))["sources"]["greenhouse"], ["stripe"])


class MatchingTests(unittest.TestCase):
    def test_counts_only_postings_that_pass_the_filters(self):
        import yaml
        cfg = yaml.safe_load(CONFIG)
        jobs = [job("1", "greenhouse", "a", "Software Engineer"),
                job("2", "greenhouse", "a", "Senior Software Engineer"),
                job("3", "greenhouse", "a", "Software Engineer", location="San Francisco, CA"),
                job("4", "greenhouse", "a", "Software Engineer", location="London, UK"),
                job("5", "greenhouse", "a", "Software Engineer", pay_min=40000, pay_max=60000),
                job("6", "greenhouse", "a", "Chef")]
        self.assertEqual([j.id for j in discovery.matching(jobs, cfg)], ["1"])


class SmartRecruitersTests(unittest.TestCase):
    def test_list_paginates_asks_for_us_and_marks_remote(self):
        pages = {0: {"totalFound": 150, "content": [
                     {"id": str(i), "name": f"Software Engineer {i}", "company": {"name": "Acme Inc"}, "releasedDate": "2026-10-20T10:00:00Z",
                      "location": {"fullLocation": "Austin, TX, United States", "remote": i == 0}} for i in range(100)]},
                 100: {"totalFound": 150, "content": [
                     {"id": str(i), "name": "Platform Engineer", "location": {"city": "Dallas", "region": "TX"}} for i in range(100, 150)]}}
        seen = []

        def fake_get(url, **kw):
            seen.append((url, kw["params"]))
            return pages[kw["params"]["offset"]]

        with mock.patch.object(sources, "_get", fake_get):
            jobs = sources.fetch_smartrecruiters("AcmeCo")
        self.assertEqual(len(jobs), 150)
        self.assertTrue(all(p["country"] == "us" for _, p in seen))
        self.assertEqual(jobs[0].location, "Austin, TX, United States (Remote)")
        self.assertEqual(jobs[1].location, "Austin, TX, United States")
        self.assertEqual(jobs[0].company, "Acme Inc")
        self.assertEqual(jobs[120].company, "AcmeCo")          # falls back to the slug
        self.assertEqual(jobs[120].location, "Dallas, TX")
        self.assertEqual(jobs[0].id, "sr:acmeco:0")
        self.assertEqual(jobs[0].posted, "2026-10-20")
        self.assertEqual(jobs[0].url, "https://jobs.smartrecruiters.com/AcmeCo/0")

    def test_unknown_company_is_an_empty_list(self):
        with mock.patch.object(sources, "_get", lambda url, **kw: {"offset": 0, "limit": 100, "totalFound": 0, "content": []}):
            self.assertEqual(sources.fetch_smartrecruiters("nope"), [])

    def test_detail_fills_description_pay_and_final_url(self):
        j = job("sr:a:1", "smartrecruiters", "Acme", "Software Engineer", detail={"api": "https://api/x"})
        info = {"releasedDate": "2026-10-21T00:00:00Z", "postingUrl": "https://jobs.smartrecruiters.com/Acme/1-software-engineer",
                "jobAd": {"sections": {"jobDescription": {"text": "<p>Build things.</p>"},
                                       "qualifications": {"text": "<ul><li>Java</li></ul>"},
                                       "additionalInformation": {"text": "Pay: $100,000 - $130,000"}}}}
        with mock.patch.object(sources, "_get", lambda url, **kw: info):
            sources.fill_detail(j)
        self.assertIn("Build things.", j.description)
        self.assertIn("Java", j.description)
        self.assertEqual((j.pay_min, j.pay_max), (100000, 130000))
        self.assertEqual(j.url, "https://jobs.smartrecruiters.com/Acme/1-software-engineer")
        self.assertEqual(j.posted, "2026-10-21")


class RemotiveTests(unittest.TestCase):
    def test_dedupes_across_categories_keeps_remotive_link_and_parses_pay(self):
        feed = {"software-development": {"jobs": [
                    {"id": 1, "company_name": "Acme", "title": "Backend Developer", "url": "https://remotive.com/remote-jobs/x-1",
                     "publication_date": "2026-10-27T12:00:00", "candidate_required_location": "USA", "salary": "$90,000 - $120,000",
                     "description": "<p>Java</p>"}]},
                "devops": {"jobs": [
                    {"id": 1, "company_name": "Acme", "title": "Backend Developer", "url": "https://remotive.com/remote-jobs/x-1",
                     "publication_date": "2026-10-27T12:00:00", "candidate_required_location": "USA", "salary": "", "description": "dup"},
                    {"id": 2, "company_name": "Beta", "title": "SRE", "url": "https://remotive.com/remote-jobs/x-2",
                     "publication_date": "2026-10-26T12:00:00", "candidate_required_location": "", "salary": "",
                     "description": "Pay $100k-$130k"}]}}
        with mock.patch.object(sources, "_get", lambda url, **kw: feed[kw["params"]["category"]]):
            jobs = sources.fetch_remotive(["software-development", "devops"])
        self.assertEqual([j.id for j in jobs], ["rm:1", "rm:2"])
        self.assertEqual(jobs[0].source, "remotive")
        self.assertTrue(jobs[0].url.startswith("https://remotive.com/"))
        self.assertEqual((jobs[0].pay_min, jobs[0].pay_max), (90000, 120000))
        self.assertEqual((jobs[1].pay_min, jobs[1].pay_max), (100000, 130000))   # from the description
        self.assertEqual(jobs[1].location, "Remote")
        self.assertEqual(jobs[0].posted, "2026-10-27")


class DedupeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(str(Path(self.tmp.name) / "jobs.db"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_same_company_and_title_from_another_source_is_a_duplicate(self):
        db.add_job(self.conn, job("gh:stripe:1", "greenhouse", "stripe", "Software Engineer, I"), 0)
        self.assertTrue(db.is_cross_source_duplicate(self.conn, job("rm:9", "remotive", "Stripe", "Software Engineer I", "Remote")))

    def test_same_source_same_title_is_not_a_duplicate(self):
        db.add_job(self.conn, job("gh:stripe:1", "greenhouse", "stripe", "Software Engineer", "Austin, TX"), 0)
        self.assertFalse(db.is_cross_source_duplicate(self.conn, job("gh:stripe:2", "greenhouse", "stripe", "Software Engineer", "Chicago, IL")))

    def test_different_company_or_title_is_not_a_duplicate(self):
        db.add_job(self.conn, job("gh:stripe:1", "greenhouse", "stripe", "Software Engineer"), 0)
        self.assertFalse(db.is_cross_source_duplicate(self.conn, job("rm:1", "remotive", "Acme", "Software Engineer")))
        self.assertFalse(db.is_cross_source_duplicate(self.conn, job("rm:2", "remotive", "Stripe", "Platform Engineer")))

    def test_old_database_without_the_column_is_migrated_and_backfilled(self):
        path = Path(self.tmp.name) / "old.db"
        old = sqlite3.connect(path)
        old.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, source TEXT, company TEXT, title TEXT, location TEXT, url TEXT,"
                    " posted TEXT, pay_min INTEGER, pay_max INTEGER, description TEXT, quick_score REAL, score INTEGER,"
                    " verdict TEXT, analysis TEXT, status TEXT DEFAULT 'new', first_seen REAL, notes TEXT)")
        old.execute("INSERT INTO jobs (id, source, company, title) VALUES ('gh:a:1', 'greenhouse', 'Acme', 'Platform Engineer')")
        old.commit()
        old.close()
        conn = db.connect(str(path))
        try:
            self.assertTrue(db.is_cross_source_duplicate(conn, job("rm:1", "remotive", "acme", "Platform Engineer")))
            self.assertEqual(conn.execute("SELECT count(*) FROM jobs").fetchone()[0], 1)   # nothing lost
        finally:
            conn.close()

    def test_meta_roundtrip(self):
        self.assertIsNone(db.get_meta(self.conn, "k"))
        db.set_meta(self.conn, "k", "1")
        db.set_meta(self.conn, "k", "2")
        self.assertEqual(db.get_meta(self.conn, "k"), "2")


class FeedThrottleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(str(Path(self.tmp.name) / "jobs.db"))
        self.cfg = {"feeds": {"remotive": {"enabled": True, "min_hours": 6, "categories": ["devops"]}}}

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_second_call_inside_the_window_does_not_hit_the_network(self):
        with mock.patch.object(sources, "fetch_remotive", return_value=[job("rm:1", "remotive", "A", "SRE")]) as f:
            self.assertEqual(len(main.collect_feeds(self.cfg, self.conn)), 1)
            self.assertEqual(main.collect_feeds(self.cfg, self.conn), [])
            self.assertEqual(f.call_count, 1)
            self.assertEqual(f.call_args.args[0], ["devops"])

    def test_fetches_again_after_the_window(self):
        db.set_meta(self.conn, "remotive_last_fetch", "1")   # long ago
        with mock.patch.object(sources, "fetch_remotive", return_value=[]) as f:
            main.collect_feeds(self.cfg, self.conn)
            self.assertEqual(f.call_count, 1)

    def test_failed_fetch_is_not_retried_immediately(self):
        with mock.patch.object(sources, "fetch_remotive", side_effect=RuntimeError("boom")) as f:
            self.assertEqual(main.collect_feeds(self.cfg, self.conn), [])
            self.assertEqual(main.collect_feeds(self.cfg, self.conn), [])
            self.assertEqual(f.call_count, 1)

    def test_disabled_or_no_database(self):
        with mock.patch.object(sources, "fetch_remotive") as f:
            self.assertEqual(main.collect_feeds({"feeds": {"remotive": {"enabled": False}}}, self.conn), [])
            self.assertEqual(main.collect_feeds({}, self.conn), [])
            self.assertEqual(main.collect_feeds(self.cfg, None), [])
            f.assert_not_called()


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "config.yaml").write_text(CONFIG, encoding="utf-8")
        self.saved_root, main.ROOT = main.ROOT, self.root
        self.cfg = discovery.load_config(self.root)

    def tearDown(self):
        main.ROOT = self.saved_root
        self.tmp.cleanup()

    def run_discover(self, names, add):
        hit = discovery.Hit("greenhouse", "reddit", [job("1", "greenhouse", "reddit", "Software Engineer")])
        known = discovery.Hit("greenhouse", "stripe", [job("2", "greenhouse", "stripe", "Software Engineer")])
        with mock.patch.object(discovery, "probe", side_effect=lambda name, delay: {"Reddit": [hit], "Stripe": [known]}.get(name, [])):
            main.cmd_discover(argparse.Namespace(names=names, file=None, add=add), self.cfg, {}, None)

    def test_without_add_nothing_is_saved(self):
        self.run_discover(["Reddit"], add=False)
        self.assertFalse((self.root / discovery.DISCOVERED_FILE).exists())

    def test_add_saves_new_boards_but_not_known_ones(self):
        self.run_discover(["Reddit", "Stripe", "Nobody"], add=True)
        self.assertEqual(discovery.load_discovered(self.root), {"greenhouse": ["reddit"]})

    def test_names_file_skips_blanks_and_comments(self):
        (self.root / "companies.txt").write_text("# my list\nReddit\n\n  Stripe  \n", encoding="utf-8")
        seen = []
        with mock.patch.object(discovery, "probe", side_effect=lambda name, delay: seen.append(name) or []):
            main.cmd_discover(argparse.Namespace(names=["Zed"], file=str(self.root / "companies.txt"), add=False), self.cfg, {}, None)
        self.assertEqual(seen, ["Zed", "Reddit", "Stripe"])

    def test_workday_add_saves_only_after_a_successful_check(self):
        url = "https://acme.wd1.myworkdayjobs.com/en-US/Acme_Careers/job/x"
        with mock.patch.object(discovery, "check_workday", side_effect=RuntimeError("404")):
            main.cmd_workday_add(argparse.Namespace(url=url, name=None), self.cfg, {}, None)
        self.assertFalse((self.root / discovery.DISCOVERED_FILE).exists())
        with mock.patch.object(discovery, "check_workday", return_value=42):
            main.cmd_workday_add(argparse.Namespace(url=url, name="Acme Corp"), self.cfg, {}, None)
        saved = discovery.load_discovered(self.root)["workday"]
        self.assertEqual(saved, [{"name": "Acme Corp", "host": "acme.wd1.myworkdayjobs.com", "tenant": "acme", "site": "Acme_Careers"}])

    def test_workday_add_rejects_a_bad_address(self):
        with mock.patch.object(discovery, "check_workday") as chk:
            main.cmd_workday_add(argparse.Namespace(url="https://example.com", name=None), self.cfg, {}, None)
            chk.assert_not_called()
        self.assertFalse((self.root / discovery.DISCOVERED_FILE).exists())


if __name__ == "__main__":
    unittest.main()
