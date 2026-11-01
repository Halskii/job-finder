"""Matching the shared postings to each user's search, and scoring the matches in batches."""
import json
import tempfile
import threading
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import auth
import db
import pipeline
import tailor
import userdata
from errors import UserError
from tests import pgtest

RECENT = date.today().isoformat()
SETTINGS = {"titles": ["Software Engineer"], "exclude_title_words": ["senior", "staff"], "min_pay": 0, "max_age_days": 30, "us_only": True,
            "exclude_locations": []}
PROFILE = {"name": "T", "strong_skills": ["Python", "Docker"], "familiar_skills": [], "do_not_assume": ["Go"]}


def add_posting(conn, pid, title, location="Austin, TX", description="Build services in Python.", source="greenhouse", company="Acme",
                posted=None, pay_min=None, pay_max=None, fetched=None):
    conn.execute("INSERT INTO postings (id, source, company, title, location, url, posted, pay_min, pay_max, pay_unit, description, dedupe_key,"
                 " formatted, fetched_at, last_seen) VALUES (?,?,?,?,?,?,?,?,?,'year',?,?,1,?,?)",
                 (pid, source, company, title, location, "https://x/" + pid, posted or RECENT, pay_min, pay_max, description,
                  db.dedupe_key(company, title), fetched or time.time(), time.time()))
    conn.commit()


def answer(score=80):
    return SimpleNamespace(content=[SimpleNamespace(text=json.dumps({"fit_score": score, "verdict": "apply", "reason": "ok"}))],
                           usage=SimpleNamespace(input_tokens=100, output_tokens=20))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [mock.patch.object(userdata, "BASE", Path(self.tmp.name))]
        self.schema = pgtest.new_schema()
        self.admin = pgtest.connect(self.schema)                      # the local user: used here only to add shared postings
        self.patches.append(mock.patch.object(pipeline, "connect_as", lambda uid: pgtest.connect(self.schema, user=uid)))
        self.scfg = {"model": "m", "initial_batch": 3, "batch_size": 2, "max_per_user_per_day": 100, "workers": 1}
        self.patches.append(mock.patch.object(pipeline, "scoring_config", lambda: self.scfg))
        for p in self.patches:
            p.start()
        self.anon = pgtest.connect(self.schema, user=False)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.anon.close()
        self.tmp.cleanup()

    def user(self, email="a@example.com", settings=None, profile=PROFILE):
        uid = auth.sign_up(self.anon, email, "a long password")["id"]
        userdata.save_settings(uid, {**SETTINGS, **(settings or {})})
        if profile is not None:
            userdata.save_profile(uid, profile)
        return uid

    def jobs(self, uid, where="TRUE"):
        conn = pgtest.connect(self.schema, user=uid)
        try:
            return [r["id"] for r in conn.execute(f"SELECT id FROM jobs WHERE {where} ORDER BY id").fetchall()]
        finally:
            conn.close()

    def client(self, scores=None, fail=None):
        """A fake Claude. `calls` lists the job each call was about, in order."""
        calls = []

        def create(**kw):
            title = kw["messages"][0]["content"].split("Title: ")[1].split("\n")[0]
            calls.append(title)
            if fail and fail(title):
                raise fail(title)
            return answer((scores or {}).get(title, 80))
        return SimpleNamespace(messages=SimpleNamespace(create=create)), calls


class MatchTests(Base):
    def test_only_postings_that_fit_the_search_are_kept(self):
        for pid, title, kw in [("p1", "Software Engineer II", {}), ("p2", "Senior Software Engineer", {}), ("p3", "Marketing Manager", {}),
                               ("p4", "Software Engineer", {"location": "London, UK", "company": "B"}), ("p5", "Staff Software Engineer", {"company": "C"}),
                               ("p6", "Software Engineer", {"posted": (date.today() - timedelta(days=90)).isoformat(), "company": "D"}),
                               ("p7", "Software Engineer", {"pay_max": 40000, "company": "E"}), ("p8", "Software Engineer", {"location": "Remote", "company": "F"})]:
            add_posting(self.admin, pid, title, **kw)
        uid = self.user(settings={"min_pay": 80000})
        conn = pgtest.connect(self.schema, user=uid)
        rules = pipeline.Rules(userdata.load_settings(uid))
        self.assertEqual(pipeline.match(conn, rules, PROFILE)[0], 2)
        conn.close()
        self.assertEqual(self.jobs(uid), ["p1", "p8"])    # right title, in the US, recent, pay not below the minimum, not too senior

    def test_the_same_job_on_two_boards_is_one_match(self):
        add_posting(self.admin, "g1", "Software Engineer", source="greenhouse")
        add_posting(self.admin, "r1", "Software  Engineer", source="remotive")
        uid = self.user()
        conn = pgtest.connect(self.schema, user=uid)
        self.assertEqual(pipeline.match(conn, pipeline.Rules(SETTINGS), PROFILE)[0], 1)
        conn.close()

    def test_running_again_adds_nothing_and_only_new_postings_are_looked_at(self):
        add_posting(self.admin, "p1", "Software Engineer", fetched=100)
        add_posting(self.admin, "p2", "Software Engineer", company="B", fetched=200, location="London, UK")
        uid = self.user()
        conn = pgtest.connect(self.schema, user=uid)
        rules = pipeline.Rules(SETTINGS)
        self.assertEqual(pipeline.match(conn, rules, PROFILE)[0], 1)
        self.assertEqual(pipeline.match(conn, rules, PROFILE)[0], 0)
        add_posting(self.admin, "p3", "Software Engineer", company="C", fetched=300)
        add_posting(self.admin, "p4", "Software Engineer", company="D", fetched=50)       # older than the watermark: not looked at
        self.assertEqual(pipeline.match(conn, rules, PROFILE, since=250)[0], 1)
        self.assertEqual(pipeline.match(conn, rules, PROFILE)[0], 1)                         # a full pass finds the one that was skipped
        conn.close()
        self.assertEqual(self.jobs(uid), ["p1", "p3", "p4"])

    def test_each_user_gets_their_own_matches(self):
        add_posting(self.admin, "d1", "DevOps Engineer")
        add_posting(self.admin, "s1", "Software Engineer", company="B")
        a = self.user("a@example.com", {"titles": ["Software Engineer"]})
        b = self.user("b@example.com", {"titles": ["DevOps Engineer", "Software Engineer"]})
        for uid in (a, b):
            conn = pgtest.connect(self.schema, user=uid)
            pipeline.match(conn, pipeline.Rules(userdata.load_settings(uid)), PROFILE)
            conn.close()
        self.assertEqual((self.jobs(a), self.jobs(b)), (["s1"], ["d1", "s1"]))

    def test_the_keyword_score_orders_matches_by_the_users_own_skills(self):
        add_posting(self.admin, "p1", "Software Engineer", description="Python and Docker daily.")
        add_posting(self.admin, "p2", "Software Engineer", company="B", description="Go services only.")
        uid = self.user()
        conn = pgtest.connect(self.schema, user=uid)
        pipeline.match(conn, pipeline.Rules(SETTINGS), PROFILE)
        scores = {r["id"]: r["quick_score"] for r in conn.execute("SELECT id, quick_score FROM jobs").fetchall()}
        conn.close()
        self.assertGreater(scores["p1"], scores["p2"])      # Go is on this user's do-not-assume list


class RunTests(Base):
    def populate(self, n, company_prefix="Co"):
        for i in range(n):
            add_posting(self.admin, f"j{i:02d}", "Software Engineer", company=f"{company_prefix}{i}",
                        description="Python " * (n - i))   # j00 has the most keyword hits, so it is scored first

    def test_the_first_batch_comes_first_then_the_rest_in_batches_best_first(self):
        self.populate(8)
        uid = self.user()
        client, calls = self.client()
        with mock.patch.object(pipeline.db, "add_usage", wraps=pipeline.db.add_usage) as usage:
            st = pipeline.run_user(uid, client)
        self.assertEqual((st["state"], st["matched"], st["waiting"], st["scored"]), ("done", 8, 0, 8))
        self.assertEqual(len(calls), 8)
        self.assertEqual([c.args[2] for c in usage.call_args_list], [3, 2, 2, 1])      # the first batch of 3, then batches of 2
        conn = pgtest.connect(self.schema, user=uid)
        self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM jobs WHERE score = 80").fetchone()["n"], 8)
        self.assertEqual(db.usage_today(conn, "score"), 8)
        conn.close()

    def test_the_best_keyword_matches_are_scored_before_the_others(self):
        self.populate(6)
        self.scfg.update(initial_batch=2, batch_size=2, max_per_user_per_day=2)
        uid = self.user()
        client, _ = self.client()
        pipeline.run_user(uid, client)
        self.assertEqual(self.jobs(uid, "score IS NOT NULL"), ["j00", "j01"])

    def test_the_days_limit_stops_scoring_and_says_when_the_rest_will_be(self):
        self.populate(8)
        self.scfg["max_per_user_per_day"] = 5
        uid = self.user()
        client, calls = self.client()
        st = pipeline.run_user(uid, client)
        self.assertEqual((st["state"], st["waiting"], st["left_today"], len(calls)), ("done", 3, 0, 5))
        self.assertIn("tomorrow", st["message"])
        conn = pgtest.connect(self.schema, user=uid)           # a new day: the usage row is from yesterday
        conn.execute("UPDATE usage SET day = ?", ((date.today() - timedelta(days=1)).isoformat(),))
        conn.commit()
        conn.close()
        st = pipeline.run_user(uid, client)
        self.assertEqual((st["state"], st["waiting"], len(calls)), ("done", 0, 8))

    def test_a_failure_every_call_would_repeat_stops_the_run_with_a_message(self):
        self.populate(6)
        uid = self.user()
        client, calls = self.client(fail=lambda t: RuntimeError("Your credit balance is too low"))
        st = pipeline.run_user(uid, client)
        self.assertEqual(st["state"], "error")
        self.assertIn("out of credit", st["message"])
        self.assertLessEqual(len(calls), 3)                    # it did not keep trying
        self.assertEqual(self.jobs(uid, "score IS NOT NULL"), [])

    def test_one_job_that_fails_is_skipped_and_does_not_stop_or_loop_the_run(self):
        self.populate(5)
        uid = self.user()
        client, calls = self.client(fail=lambda t: None)
        broken = {"Co2"}

        def create(**kw):
            content = kw["messages"][0]["content"]
            calls.append(content)
            if any(f"Company: {c}\n" in content for c in broken):
                raise ValueError("model answered nonsense")
            return answer()
        client.messages.create = create
        with self.assertLogs("job-finder", level="ERROR"):
            st = pipeline.run_user(uid, client)
        self.assertEqual((st["state"], st["scored"]), ("done", 4))
        self.assertEqual(len(calls), 5)                        # tried once, not again and again
        self.assertEqual(len(self.jobs(uid, "score IS NULL")), 1)

    def test_no_api_key_is_reported_on_the_page_not_raised(self):
        self.populate(2)
        uid = self.user()
        with mock.patch.object(tailor, "make_client", side_effect=UserError("No Anthropic API key found.", "no_api_key")):
            st = pipeline.run_user(uid)
        self.assertEqual((st["state"], st["message"], st["matched"]), ("error", "No Anthropic API key found.", 2))   # matching still happened

    def test_nothing_to_do_without_titles_or_a_profile(self):
        self.populate(2)
        for kw in ({"settings": {"titles": []}}, {"profile": None}):
            uid = self.user(f"{len(kw)}{list(kw)[0]}@example.com", **kw)
            st = pipeline.run_user(uid, self.client()[0])
            self.assertEqual(st["state"], "idle")
            self.assertIn("Add your job titles", st["message"])
            self.assertEqual(self.jobs(uid), [])

    def test_a_job_that_no_longer_fits_a_changed_search_is_not_paid_for(self):
        add_posting(self.admin, "p1", "Software Engineer", location="Austin, TX")
        add_posting(self.admin, "p2", "Software Engineer", location="Miami, FL", company="B")
        uid = self.user()
        client, calls = self.client()
        conn = pgtest.connect(self.schema, user=uid)
        pipeline.match(conn, pipeline.Rules(SETTINGS), PROFILE)
        conn.close()
        userdata.save_settings(uid, {**SETTINGS, "exclude_locations": ["FL"]})     # then the person excludes Florida
        pipeline.run_user(uid, client)
        self.assertEqual(self.jobs(uid, "score IS NOT NULL"), ["p1"])
        self.assertEqual(len(calls), 1)

    def test_a_changed_search_is_checked_against_everything_but_an_unchanged_one_only_against_new_postings(self):
        add_posting(self.admin, "p1", "Software Engineer", fetched=100)
        add_posting(self.admin, "p2", "Platform Engineer", company="B", fetched=100)
        uid = self.user()
        client, _ = self.client()
        self.assertEqual(pipeline.run_user(uid, client)["matched"], 1)
        userdata.save_settings(uid, {**SETTINGS, "titles": ["Software Engineer", "Platform Engineer"]})
        self.assertEqual(pipeline.run_user(uid, client)["matched"], 2)           # the older Platform Engineer posting is found
        add_posting(self.admin, "p3", "Software Engineer", company="C")
        self.assertEqual(pipeline.run_user(uid, client)["matched"], 3)

    def test_two_users_are_scored_separately_and_one_users_run_does_not_touch_the_other(self):
        self.populate(4)
        a, b = self.user("a@example.com"), self.user("b@example.com")
        client, calls = self.client()
        pipeline.run_user(a, client)
        self.assertEqual(len(calls), 4)
        self.assertEqual(self.jobs(b), [])                                      # b has not run: nothing of a's is theirs
        pipeline.run_user(b, client)
        self.assertEqual(len(calls), 8)                                         # scores are per user, so b's jobs are scored for b
        conn = pgtest.connect(self.schema, user=b)
        self.assertEqual(db.usage_today(conn, "score"), 4)
        conn.close()

    def test_status_before_any_run(self):
        uid = self.user()
        conn = pgtest.connect(self.schema, user=uid)
        self.assertEqual(pipeline.status(conn)["state"], "idle")
        self.assertEqual(pipeline.status(conn)["left_today"], 100)
        conn.close()


class WorkerTests(Base):
    def test_users_are_run_one_at_a_time_and_a_repeat_request_runs_once_more(self):
        order, gate, started = [], threading.Event(), threading.Event()

        def fake_run(uid, client=None):
            order.append(uid)
            if uid == 1 and len([o for o in order if o == 1]) == 1:
                started.set()
                gate.wait(5)
        worker = pipeline.Worker()
        with mock.patch.object(pipeline, "run_user", fake_run):
            worker.submit(1)
            self.assertTrue(started.wait(5))
            worker.submit(1)          # asked for again while running
            worker.submit(1)          # and again: still just one more run
            worker.submit(2)
            gate.set()
            deadline = time.time() + 5
            while len(order) < 4 and time.time() < deadline:
                time.sleep(0.02)
            time.sleep(0.1)
        self.assertEqual(order, [1, 2, 1])      # the repeat waits its turn behind user 2, and is run once, not twice

    def test_a_run_that_blows_up_is_recorded_for_that_user_and_the_worker_carries_on(self):
        uid = self.user()
        done = threading.Event()
        calls = []

        def fake_run(u, client=None):
            calls.append(u)
            if u == uid:
                raise RuntimeError("boom")
            done.set()
        worker = pipeline.Worker()
        with mock.patch.object(pipeline, "run_user", fake_run), self.assertLogs("job-finder", level="WARNING"):
            worker.submit(uid)
            worker.submit(999)
            self.assertTrue(done.wait(5))
        conn = pgtest.connect(self.schema, user=uid)
        self.assertEqual(pipeline.status(conn)["state"], "error")
        conn.close()

    def test_unfinished_runs_are_picked_up_after_a_restart(self):
        a, b, c = self.user("a@example.com"), self.user("b@example.com"), self.user("c@example.com")
        for uid, state in ((a, "scoring"), (b, "done"), (c, "queued")):
            conn = pgtest.connect(self.schema, user=uid)
            pipeline._save(conn, state=state)
            conn.close()
        with mock.patch.object(pipeline.WORKER, "submit") as submit:
            self.assertEqual(pipeline.resume_unfinished(), 2)
        self.assertEqual(sorted(c.args[0] for c in submit.call_args_list), sorted([a, c]))

    def test_request_marks_the_user_queued_at_once(self):
        uid = self.user()
        with mock.patch.object(pipeline.WORKER, "submit") as submit:
            pipeline.request(uid)
        conn = pgtest.connect(self.schema, user=uid)
        self.assertEqual(pipeline.status(conn)["state"], "queued")
        conn.close()
        submit.assert_called_once_with(uid)


if __name__ == "__main__":
    unittest.main()
