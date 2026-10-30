"""The fetch -> filter -> store part of `main.py run`, with the network replaced by fake postings."""
import argparse
import io
import os
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import db
import main
import sources

CFG = {"titles": ["Software Engineer"], "exclude_title_words": ["senior"], "exclude_locations": ["California"], "us_only": True,
       "min_pay": 80000, "max_age_days": 30, "request_delay": 0, "detail_workers": 4}


def job(id_, title="Software Engineer", company="Acme", source="greenhouse", location="Austin, TX", **kw):
    kw.setdefault("posted", "2099-01-01")
    return sources.Job(id=id_, source=source, company=company, title=title, location=location, url=f"https://{company}.example/{id_}", **kw)


def wd(id_, **kw):
    return job(id_, source="workday", company=kw.pop("company", "BigCo"), detail={"api": f"https://bigco.example/api/{id_}"}, **kw)


class PipelineBase(unittest.TestCase):
    """Shared setup: a temporary database and a way to run the pipeline on fake postings."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(str(Path(self.tmp.name) / "jobs.db"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def run_pipeline(self, jobs, fill=None, cfg=None):
        def default_fill(j):
            j.description, j.posted = "Build things.", "2099-01-01"
            j.pay_min = j.pay_max = None

        out = io.StringIO()
        with mock.patch.object(main, "collect", return_value=jobs), mock.patch.object(sources, "fill_detail", fill or default_fill), \
                redirect_stdout(out):
            main.cmd_run(argparse.Namespace(no_llm=True, limit=None), cfg or CFG, {}, self.conn)
        return out.getvalue()

    def stored(self):
        return {r["id"] for r in self.conn.execute("SELECT id FROM jobs")}


class PipelineTests(PipelineBase):
    def test_every_filter_counts_and_only_good_jobs_are_stored(self):
        db.add_job(self.conn, job("gh:seen:1", company="Seen"), 0)
        jobs = [
            job("gh:good:1", company="Good"),
            job("gh:chef:1", title="Chef"),
            job("gh:uk:1", location="London, UK"),
            job("gh:ca:1", location="San Francisco, California"),
            job("gh:seen:1", company="Seen"),
            job("rm:dupe:1", source="remotive", company="Good", location="Remote"),      # same company+title as gh:good:1, other source
            job("gh:senior:1", title="Senior Software Engineer"),
            job("gh:poor:1", company="Poor", pay_min=40000, pay_max=60000),
            wd("wd:big:1", company="BigCo"),
            wd("wd:big:2", company="BigCo2"),
        ]
        text = self.run_pipeline(jobs)
        self.assertEqual(self.stored(), {"gh:seen:1", "gh:good:1", "wd:big:1", "wd:big:2"})
        self.assertIn("Fetched 10", text)
        self.assertIn("title match 8", text)          # all but the chef and the senior title
        self.assertIn("after location 7", text)       # minus the UK posting
        self.assertIn("excluded locations 1", text)
        self.assertIn("already seen 1", text)
        self.assertIn("duplicates 1", text)
        self.assertIn("below pay 1", text)
        self.assertIn("NEW 3", text)

    def test_detail_failures_are_skipped_without_stopping_the_run(self):
        def fill(j):
            if j.id == "wd:big:bad":
                raise RuntimeError("site down")
            j.description, j.posted = "ok", "2099-01-01"

        text = self.run_pipeline([wd("wd:big:1"), wd("wd:big:bad"), wd("wd:big:2")], fill=fill)
        self.assertEqual(self.stored(), {"wd:big:1", "wd:big:2"})
        self.assertIn("NEW 2", text)

    def test_age_and_pay_are_judged_from_the_fetched_detail(self):
        def fill(j):
            j.description = "d"
            if j.id == "wd:big:old":
                j.posted = "2020-01-01"
            elif j.id == "wd:big:cheap":
                j.posted, j.pay_min, j.pay_max = "2099-01-01", 30000, 40000
            else:
                j.posted, j.pay_min, j.pay_max = "2099-01-01", 90000, 120000

        text = self.run_pipeline([wd("wd:big:old"), wd("wd:big:cheap"), wd("wd:big:fine")], fill=fill)
        self.assertEqual(self.stored(), {"wd:big:fine"})
        self.assertIn("too old 1", text)
        self.assertIn("below pay 1", text)
        row = self.conn.execute("SELECT description, pay_min, pay_max FROM jobs").fetchone()
        self.assertEqual((row["description"], row["pay_min"], row["pay_max"]), ("d", 90000, 120000))

    def test_details_are_only_fetched_for_jobs_that_passed_the_cheap_filters(self):
        fetched = []

        def fill(j):
            fetched.append(j.id)
            j.description, j.posted = "x", "2099-01-01"

        db.add_job(self.conn, wd("wd:big:seen"), 0)
        self.run_pipeline([wd("wd:big:chef", title="Chef"), wd("wd:big:uk", location="London, UK"), wd("wd:big:seen"), wd("wd:big:new")], fill=fill)
        self.assertEqual(fetched, ["wd:big:new"])

    def test_postings_too_old_by_the_list_date_are_never_looked_up(self):
        fetched = []

        def fill(j):
            fetched.append(j.id)
            j.description = "x"

        self.run_pipeline([wd("wd:big:old", posted="2020-01-01"), wd("wd:big:fresh", posted="2099-01-01")], fill=fill)
        self.assertEqual(fetched, ["wd:big:fresh"])
        self.assertEqual(self.stored(), {"wd:big:fresh"})

    def test_a_second_run_stores_nothing_new(self):
        jobs = [job("gh:good:1", company="Good"), wd("wd:big:1")]
        self.run_pipeline(jobs)
        jobs2 = [job("gh:good:1", company="Good"), wd("wd:big:1")]
        text = self.run_pipeline(jobs2)
        self.assertIn("already seen 2", text)
        self.assertIn("NEW 0", text)

    def test_details_run_in_parallel_across_hosts_but_one_at_a_time_per_host(self):
        lock = threading.Lock()
        active, peak_per_host, peak_total = {}, {}, [0]

        def fill(j):
            host = j.detail["api"].split("/")[2]
            with lock:
                active[host] = active.get(host, 0) + 1
                peak_per_host[host] = max(peak_per_host.get(host, 0), active[host])
                peak_total[0] = max(peak_total[0], sum(active.values()))
            time.sleep(0.03)
            with lock:
                active[host] -= 1
            j.description, j.posted = "x", "2099-01-01"

        jobs = []
        for site in ("a", "b", "c", "d"):
            for n in range(4):
                j = wd(f"wd:{site}:{n}", company=f"Co{site}{n}")
                j.detail = {"api": f"https://{site}.example/api/{n}"}
                jobs.append(j)
        self.run_pipeline(jobs, fill=fill)
        self.assertEqual(len(self.stored()), 16)
        self.assertEqual(max(peak_per_host.values()), 1)       # never two requests at once to the same site
        self.assertGreater(peak_total[0], 1)                     # but different sites overlap


class WorkdayDetailLocationTests(unittest.TestCase):
    def fill(self, list_location, info):
        j = wd("wd:x:1", location=list_location)
        with mock.patch.object(sources, "_get", lambda url, **kw: {"jobPostingInfo": {"jobDescription": "<p>d</p>", **info}}):
            sources.fill_workday_detail(j)
        return j

    def test_multi_location_placeholder_is_replaced_by_the_real_places(self):
        j = self.fill("2 Locations", {"location": "Scoresby, Victoria, Australia", "additionalLocations": ["Canberra, Australia"]})
        self.assertEqual(j.location, "Scoresby, Victoria, Australia; Canberra, Australia")
        self.assertEqual(self.fill("1 Location", {"location": "Austin, TX"}).location, "Austin, TX")

    def test_a_real_location_from_the_list_is_kept(self):
        j = self.fill("Austin, TX", {"location": "Dallas, TX", "additionalLocations": ["Plano, TX"]})
        self.assertEqual(j.location, "Austin, TX")

    def test_missing_location_is_filled_and_an_unknown_placeholder_is_left_alone(self):
        self.assertEqual(self.fill("", {"location": "Boise, ID"}).location, "Boise, ID")
        self.assertEqual(self.fill("6 Locations", {}).location, "6 Locations")


class LateLocationTests(PipelineBase):
    """After the full posting is fetched, the location filters run again on the real places."""

    def fill_with(self, places):
        def fill(j):
            j.description, j.posted = "d", "2099-01-01"
            j.location = places[j.id]
        return fill

    def test_non_us_and_excluded_locations_are_dropped_after_details_and_the_rest_kept(self):
        places = {"wd:a:aus": "Scoresby, Victoria, Australia; Canberra, Australia",
                  "wd:b:ca": "San Francisco, California; San Jose, California",
                  "wd:c:mixed": "London, United Kingdom; Austin, TX",
                  "wd:d:us": "Austin, TX; Remote - US"}
        jobs = [wd(i, company=i.split(":")[1], location="2 Locations") for i in places]
        text = self.run_pipeline(jobs, fill=self.fill_with(places))
        self.assertEqual(self.stored(), {"wd:c:mixed", "wd:d:us"})
        self.assertIn("dropped by full location 2", text)
        self.assertIn("NEW 2", text)
        self.assertEqual(self.conn.execute("SELECT location FROM jobs WHERE id='wd:c:mixed'").fetchone()[0], places["wd:c:mixed"])

    def test_a_job_with_a_real_us_location_in_the_list_is_unaffected(self):
        text = self.run_pipeline([wd("wd:e:1", location="Austin, TX")], fill=self.fill_with({"wd:e:1": "Austin, TX"}))
        self.assertIn("dropped by full location 0", text)
        self.assertEqual(self.stored(), {"wd:e:1"})


class CollectWorkdayTests(unittest.TestCase):
    ENTRIES = [{"name": f"Site{i}", "host": f"s{i}.wd1.myworkdayjobs.com", "tenant": f"s{i}", "site": "ext"} for i in range(6)]

    def collect(self, fake, cfg=None):
        out = io.StringIO()
        with mock.patch.object(sources, "fetch_workday", fake), redirect_stdout(out):
            jobs = main.collect_workday(cfg or {"workday_search_terms": ["software engineer"], "workday_workers": 4}, self.ENTRIES, 0)
        return jobs, out.getvalue()

    def test_a_failing_site_is_reported_and_the_others_still_load_in_order(self):
        def fake(entry, terms, pages, delay):
            if entry["tenant"] == "s2":
                raise RuntimeError("site renamed")
            return [wd(f"wd:{entry['tenant']}:1", company=entry["name"])]

        jobs, text = self.collect(fake)
        self.assertEqual([j.id for j in jobs], ["wd:s0:1", "wd:s1:1", "wd:s3:1", "wd:s4:1", "wd:s5:1"])   # list order kept
        self.assertIn("Site2", text)
        self.assertIn("skipped (RuntimeError: site renamed)", text)
        self.assertEqual(text.count("postings"), 5)

    def test_sites_are_read_at_the_same_time_up_to_the_worker_limit(self):
        lock = threading.Lock()
        state = {"now": 0, "peak": 0}

        def fake(entry, terms, pages, delay):
            with lock:
                state["now"] += 1
                state["peak"] = max(state["peak"], state["now"])
            time.sleep(0.05)
            with lock:
                state["now"] -= 1
            return []

        self.collect(fake, {"workday_search_terms": ["x"], "workday_workers": 3})
        self.assertEqual(state["peak"], 3)
        state.update(now=0, peak=0)
        self.collect(fake, {"workday_search_terms": ["x"], "workday_workers": 1})
        self.assertEqual(state["peak"], 1)

    def test_the_configured_search_words_and_page_cap_reach_every_site(self):
        seen = []

        def fake(entry, terms, pages, delay):
            seen.append((tuple(terms), pages))
            return []

        self.collect(fake, {"titles": ["Software Engineer II", "DevOps Engineer"], "workday_search_terms": "auto", "workday_max_pages": 2})
        self.assertEqual(set(seen), {(("software engineer", "devops engineer"), 2)})
        self.assertEqual(len(seen), 6)


class CollectBoardsTests(unittest.TestCase):
    """Greenhouse / Lever / Ashby boards are read several at a time, results kept in config order."""

    CFG = {"sources": {"greenhouse": ["g1", "g2", "g3"], "lever": ["l1"], "ashby": ["a1"]}, "request_delay": 0, "fetch_workers": 4,
           "feeds": {}, "government": {"usajobs": {"enabled": False}, "texas_hhs": {"enabled": False}}}

    def test_order_failures_and_overlap(self):
        lock = threading.Lock()
        state = {"now": 0, "peak": 0}

        def fetcher(kind):
            def fn(slug):
                with lock:
                    state["now"] += 1
                    state["peak"] = max(state["peak"], state["now"])
                time.sleep(0.04)
                with lock:
                    state["now"] -= 1
                if slug == "g2":
                    raise RuntimeError("no such board")
                return [job(f"{kind}:{slug}", company=slug)]
            return fn

        out = io.StringIO()
        with mock.patch.object(sources, "fetch_greenhouse", fetcher("gh")), mock.patch.object(sources, "fetch_lever", fetcher("lv")), \
                mock.patch.object(sources, "fetch_ashby", fetcher("ab")), mock.patch.object(sources, "fetch_smartrecruiters", fetcher("sr")), \
                redirect_stdout(out):
            jobs = main.collect(self.CFG, None)
        self.assertEqual([j.id for j in jobs], ["gh:g1", "gh:g3", "lv:l1", "ab:a1"])
        self.assertIn("skipped (RuntimeError: no such board)", out.getvalue())
        self.assertGreater(state["peak"], 1)
        self.assertLessEqual(state["peak"], 4)


class ScoringTests(PipelineBase):
    """Claude scoring runs in several threads; results are stored by the main thread."""

    def run_scoring(self, n_jobs, score_fn, workers=3):
        for i in range(n_jobs):
            db.add_job(self.conn, job(f"gh:s:{i}", title=f"Software Engineer {i}", company=f"Co{i}"), 0)
        cfg = {**CFG, "scoring": {"model": "m", "max_per_run": 100, "workers": workers,
                                  "price_per_million_tokens": {"input": 1.0, "output": 5.0}}}
        out = io.StringIO()
        fake_anthropic = mock.MagicMock()
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test"}), mock.patch.object(main, "load_env"), \
                mock.patch.object(main, "load_resume", return_value=""), mock.patch.object(main, "write_report"), \
                mock.patch.object(main, "collect", return_value=[]), mock.patch.dict("sys.modules", {"anthropic": fake_anthropic}), \
                mock.patch.object(main.scorer, "score_job", score_fn), redirect_stdout(out):
            main.cmd_run(argparse.Namespace(no_llm=False, limit=None), cfg, {}, self.conn)
        return out.getvalue()

    def test_all_jobs_are_scored_in_parallel_and_saved(self):
        lock = threading.Lock()
        state = {"now": 0, "peak": 0}

        def score(client, model, profile, row, min_pay, resume):
            with lock:
                state["now"] += 1
                state["peak"] = max(state["peak"], state["now"])
            time.sleep(0.03)
            with lock:
                state["now"] -= 1
            return {"fit_score": 70, "verdict": "maybe", "_in": 1000, "_out": 100}

        text = self.run_scoring(9, score, workers=3)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM jobs WHERE score IS NOT NULL").fetchone()[0], 9)
        self.assertEqual(state["peak"], 3)
        self.assertIn("Tokens: 9,000 in / 900 out", text)
        self.assertIn("Scoring took", text)

    def test_one_failed_call_does_not_stop_the_rest_and_stays_unscored(self):
        def score(client, model, profile, row, min_pay, resume):
            if row["company"] == "Co2":
                raise RuntimeError("overloaded")
            return {"fit_score": 60, "verdict": "maybe", "_in": 1, "_out": 1}

        text = self.run_scoring(5, score)
        scored = {r["company"] for r in self.conn.execute("SELECT company FROM jobs WHERE score IS NOT NULL")}
        self.assertEqual(scored, {"Co0", "Co1", "Co3", "Co4"})
        self.assertIn("error on Software Engineer 2: RuntimeError: overloaded", text)
        self.assertEqual(len(db.unscored(self.conn, 10)), 1)       # retried on the next run


if __name__ == "__main__":
    unittest.main()
