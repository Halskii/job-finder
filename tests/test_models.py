"""Looking at the scores of more than one model: each model's scores are kept, the person picks which they see, and a model that has not
scored a job has no score for it."""
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from types import SimpleNamespace
from unittest import mock

import db
import pipeline
import userdata
import viewer
import web
from tests import pgtest, webtest
from tests.test_pipeline import Base, PROFILE, add_posting


def analysis(score, model=None):
    return {"fit_score": score, "verdict": "apply", "reason": "ok", **({"_model": model} if model else {})}


class ModelBase(Base):
    def setUp(self):
        super().setUp()
        self.scfg.update(model="m1", models=["m2", "m3"])
        patch = mock.patch.object(viewer, "_db", lambda uid: pgtest.connect(self.schema, user=uid))     # the page's helpers use this test's database
        patch.start()
        self.patches.append(patch)
        self.uid = self.user("me@example.com")

    def tearDown(self):
        self.admin.close()      # the shared base leaves it open, and a full run of the tests uses up the database's connections
        super().tearDown()

    def conn(self, uid=None):
        return pgtest.connect(self.schema, user=uid or self.uid)

    def job(self, jid="j1", title="Software Engineer", **kw):
        add_posting(self.admin, jid, title, company=kw.pop("company", "Acme" + jid), **kw)
        conn = self.conn()
        try:
            conn.execute("INSERT INTO user_jobs (user_id, job_id, quick_score, first_seen) VALUES (?,?,?,?) ON CONFLICT DO NOTHING",
                         (conn.user_id, jid, 0.0, 0.0))
            conn.commit()
        finally:
            conn.close()
        return jid

    def shown(self, jid="j1", uid=None):
        conn = self.conn(uid)
        try:
            r = conn.execute("SELECT score, analysis FROM jobs WHERE id = ?", (jid,)).fetchone()
            return r["score"]
        finally:
            conn.close()

    def kept(self, jid="j1", uid=None):
        conn = self.conn(uid)
        try:
            return {r["model"]: r["score"] for r in conn.execute("SELECT model, score FROM job_scores WHERE job_id = ?", (jid,)).fetchall()}
        finally:
            conn.close()


class ChoiceTests(ModelBase):
    def test_the_models_to_choose_from_are_the_default_first_then_the_listed_ones(self):
        self.assertEqual(pipeline.allowed_models(), ["m1", "m2", "m3"])
        self.scfg["models"] = ["m2", "m1", "m2", "bad name!", 7, "", "  m4  "]
        self.assertEqual(pipeline.allowed_models(), ["m1", "m2", "m4"])         # no repeats, nothing that is not a model name
        del self.scfg["models"]
        self.assertEqual(pipeline.allowed_models(), ["m1"])                       # no list: nothing to choose

    def test_a_model_is_shown_by_a_readable_name(self):
        self.assertEqual(pipeline.model_label("claude-haiku-5-5"), "Haiku 5.5")
        self.assertEqual(pipeline.model_label("claude-opus-5-5"), "Opus 5.5")
        self.assertEqual(pipeline.model_label("something-else"), "something-else")

    def test_only_a_listed_model_can_be_chosen(self):
        with self.assertRaises(ValueError):
            viewer.choose_model(self.uid, "other")
        self.assertEqual(viewer.models_for(self.uid)["active"], "m1")
        self.assertEqual(viewer.choose_model(self.uid, "m2")["active"], "m2")


class StoringTests(ModelBase):
    def test_the_score_of_the_model_being_looked_at_is_shown(self):
        jid = self.job()
        conn = self.conn()
        db.save_score(conn, jid, 70, "apply", analysis(70, "m1"))
        conn.close()
        self.assertEqual((self.shown(), self.kept()), (70, {"m1": 70}))

    def test_a_score_from_another_model_is_kept_but_does_not_replace_what_is_shown(self):
        jid = self.job()
        conn = self.conn()
        db.save_score(conn, jid, 70, "apply", analysis(70, "m1"))
        db.save_score(conn, jid, 95, "apply", analysis(95, "m2"))        # for example a run that began before the person switched
        conn.close()
        self.assertEqual((self.shown(), self.kept()), (70, {"m1": 70, "m2": 95}))

    def test_switching_shows_the_other_models_scores_and_back_again(self):
        jid = self.job()
        conn = self.conn()
        db.save_score(conn, jid, 70, "apply", analysis(70, "m1"))
        db.set_active_model(conn, "m2")
        self.assertIsNone(conn.execute("SELECT score FROM jobs WHERE id = ?", (jid,)).fetchone()["score"])   # m2 has not scored it
        db.save_score(conn, jid, 90, "apply", analysis(90, "m2"))
        self.assertEqual(conn.execute("SELECT score FROM jobs WHERE id = ?", (jid,)).fetchone()["score"], 90)
        db.set_active_model(conn, "m1")
        row = conn.execute("SELECT score, analysis FROM jobs WHERE id = ?", (jid,)).fetchone()
        conn.close()
        self.assertEqual(row["score"], 70)
        self.assertEqual(json.loads(row["analysis"])["_model"], "m1")           # the whole answer comes back, not only the number
        self.assertEqual(self.kept(), {"m1": 70, "m2": 90})

    def test_scores_made_before_models_were_kept_apart_are_not_lost_on_the_first_switch(self):
        jid = self.job()
        conn = self.conn()
        conn.execute("UPDATE user_jobs SET score = 61, verdict = 'maybe', analysis = ? WHERE job_id = ?", (json.dumps({"fit_score": 61}), jid))   # no _model
        conn.commit()
        db.set_active_model(conn, "m2")
        self.assertIsNone(conn.execute("SELECT score FROM jobs WHERE id = ?", (jid,)).fetchone()["score"])
        db.set_active_model(conn, "m1")
        self.assertEqual(conn.execute("SELECT score FROM jobs WHERE id = ?", (jid,)).fetchone()["score"], 61)
        conn.close()

    def test_a_new_score_does_not_destroy_the_one_it_replaces(self):
        jid = self.job()
        conn = self.conn()
        db.save_score(conn, jid, 70, "apply", analysis(70, "m1"))
        self.scfg["model"] = "m2"                                                # the configured model changes and the job is scored again
        db.save_score(conn, jid, 88, "apply", analysis(88, "m2"))
        conn.close()
        self.assertEqual(self.kept(), {"m1": 70, "m2": 88})
        self.assertEqual(self.shown(), 88)

    def test_each_person_looks_at_their_own_model(self):
        other = self.user("other@example.com")
        jid = self.job()
        conn = self.conn(other)
        conn.execute("INSERT INTO user_jobs (user_id, job_id, quick_score, first_seen) VALUES (?,?,?,?)", (other, jid, 0.0, 0.0))
        conn.commit()
        db.save_score(conn, jid, 50, "maybe", analysis(50, "m1"))
        conn.close()
        conn = self.conn()
        db.save_score(conn, jid, 70, "apply", analysis(70, "m1"))
        db.set_active_model(conn, "m3")
        conn.close()
        self.assertEqual(viewer.models_for(other)["active"], "m1")
        self.assertEqual(self.shown(uid=other), 50)
        self.assertEqual(self.kept(uid=other), {"m1": 50})
        self.assertEqual(self.kept(), {"m1": 70})

    def test_the_counts_say_how_many_jobs_each_model_has_scored_and_how_many_wait(self):
        a, b, c = self.job("a"), self.job("b", company="B"), self.job("c", company="C")
        conn = self.conn()
        db.save_score(conn, a, 70, "apply", analysis(70, "m1"))
        db.save_score(conn, b, 60, "maybe", analysis(60, "m1"))
        db.save_score(conn, a, 90, "apply", analysis(90, "m2"))
        conn.close()
        info = viewer.models_for(self.uid)
        self.assertEqual({m["id"]: m["scored"] for m in info["models"]}, {"m1": 2, "m2": 1, "m3": 0})
        self.assertEqual((info["choice"], info["active"], info["looking_at"], info["waiting"]), (True, "m1", 3, 1))   # c has no score from m1
        self.assertEqual(viewer.choose_model(self.uid, "m2")["waiting"], 2)                                        # m2 has scored only a
        self.assertEqual(viewer.choose_model(self.uid, "m3")["waiting"], 3)


class ScoringTests(ModelBase):
    def models_client(self):
        used = []

        def create(**kw):
            used.append(kw["model"])
            return SimpleNamespace(content=[SimpleNamespace(text=json.dumps({"fit_score": 77, "verdict": "apply", "reason": "ok"}))],
                                   usage=SimpleNamespace(input_tokens=10, output_tokens=5))
        return SimpleNamespace(messages=SimpleNamespace(create=create)), used

    def test_a_run_scores_with_the_model_the_person_is_looking_at(self):
        add_posting(self.admin, "p1", "Software Engineer")
        client, used = self.models_client()
        pipeline.run_user(self.uid, client)
        self.assertEqual(used, ["m1"])
        conn = self.conn()
        db.set_active_model(conn, "m2")
        conn.close()
        pipeline.run_user(self.uid, client)                                    # nothing new, but this model has not scored the job
        self.assertEqual(used, ["m1", "m2"])
        self.assertEqual((self.shown("p1"), self.kept("p1")), (77, {"m1": 77, "m2": 77}))

    def test_saved_jobs_are_scored_too(self):
        add_posting(self.admin, "p1", "Software Engineer")
        client, used = self.models_client()
        pipeline.run_user(self.uid, client)
        conn = self.conn()
        db.set_status_exact(conn, "p1", "saved")
        db.set_active_model(conn, "m2")
        conn.close()
        pipeline.run_user(self.uid, client)
        self.assertEqual(used, ["m1", "m2"])

    def test_a_job_added_by_hand_is_scored_by_the_model_being_looked_at(self):
        add_posting(self.admin, "p1", "Software Engineer")
        pipeline.run_user(self.uid, self.models_client()[0])
        conn = self.conn()
        db.set_active_model(conn, "m3")
        conn.close()
        client, used = self.models_client()
        self.assertEqual(pipeline.score_one(self.uid, "p1", client), {"scored": True})
        self.assertEqual(used, ["m3"])


class CapTests(ModelBase):
    TITLES = {"Software Engineer Alpha": 90, "Software Engineer Beta": 70, "Software Engineer Gamma": 50, "Software Engineer Delta": 30}

    def setUp(self):
        super().setUp()
        for i, title in enumerate(self.TITLES):
            add_posting(self.admin, f"p{i}", title, company=f"Co{i}")
        client, _ = self.client(scores=self.TITLES)
        pipeline.run_user(self.uid, client)                       # m1 scores all four
        conn = self.conn()
        db.set_active_model(conn, "m2")
        conn.close()

    def cap(self, n):
        conn = self.conn()
        pipeline._save(conn, score_limit=n)
        conn.close()

    def test_the_jobs_the_other_model_liked_best_are_scored_first(self):
        conn = self.conn()
        rules = pipeline.Rules(userdata.load_settings(self.uid))
        order = [r["title"] for r in conn.execute("SELECT id, title FROM jobs WHERE score IS NULL").fetchall()]
        ids = pipeline.pending(conn, rules, set())
        titles = {r["id"]: r["title"] for r in conn.execute("SELECT id, title FROM jobs").fetchall()}
        conn.close()
        self.assertEqual(len(order), 4)
        self.assertEqual([titles[i] for i in ids], list(self.TITLES))     # Alpha 90, Beta 70, Gamma 50, Delta 30

    def test_a_cap_scores_only_that_many_and_the_best_of_them(self):
        self.cap(2)
        client, calls = self.client()
        pipeline.run_user(self.uid, client)
        self.assertEqual(calls, ["Software Engineer Alpha", "Software Engineer Beta"])
        info = viewer.models_for(self.uid)
        self.assertEqual((info["waiting"], {m["id"]: m["scored"] for m in info["models"]}["m2"]), (2, 2))
        conn = self.conn()
        self.assertIsNone(conn.execute("SELECT score_limit FROM user_runs").fetchone()["score_limit"])       # the cap was for that run only
        self.assertEqual(pipeline.status(conn)["state"], "done")
        conn.close()

    def test_the_rest_are_scored_by_the_next_run_with_no_cap(self):
        self.cap(2)
        pipeline.run_user(self.uid, self.client()[0])
        client, calls = self.client()
        pipeline.run_user(self.uid, client)
        self.assertEqual(calls, ["Software Engineer Gamma", "Software Engineer Delta"])
        self.assertEqual(viewer.models_for(self.uid)["waiting"], 0)

    def test_a_cap_is_dropped_even_when_the_run_has_nothing_to_do(self):
        self.cap(1)
        conn = self.conn()
        conn.execute("UPDATE user_runs SET state = 'idle'")
        conn.commit()
        conn.close()
        for _ in range(2):
            pipeline.run_user(self.uid, self.client()[0])
        conn = self.conn()
        self.assertIsNone(conn.execute("SELECT score_limit FROM user_runs").fetchone()["score_limit"])
        conn.close()


class EndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.site = webtest.Site(cls.tmp.name)
        cls.base = cls.site.base

    @classmethod
    def tearDownClass(cls):
        cls.site.stop()
        cls.tmp.cleanup()

    def setUp(self):
        real = pipeline.scoring_config
        self.patch = mock.patch.object(pipeline, "scoring_config", lambda: {**real(), "model": "m1", "models": ["m2"]})
        self.patch.start()
        conn = self.site.db()
        conn.execute("DELETE FROM user_runs")
        conn.commit()
        conn.close()

    def tearDown(self):
        self.patch.stop()

    def call(self, method, path, body=None, token=True):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Cookie"] = f"{web.COOKIE}={self.site.token}"
        req = urllib.request.Request(self.base + path, method=method, data=None if body is None else json.dumps(body).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_the_page_lists_the_models_and_the_one_in_use(self):
        status, out = self.call("GET", "/api/models")
        self.assertEqual(status, 200)
        self.assertEqual([m["id"] for m in out["models"]], ["m1", "m2"])
        self.assertEqual((out["active"], out["choice"]), ("m1", True))

    def test_choosing_a_model_changes_what_the_page_is_told_it_is_looking_at(self):
        status, out = self.call("POST", "/api/model", {"model": "m2"})
        self.assertEqual((status, out["active"]), (200, "m2"))
        self.assertEqual(self.call("GET", "/api/models")[1]["active"], "m2")
        self.assertEqual(self.call("POST", "/api/model", {"model": "m1"})[1]["active"], "m1")

    def test_a_model_that_is_not_listed_and_bad_input_are_refused(self):
        self.assertEqual(self.call("POST", "/api/model", {"model": "evil"})[0], 400)
        self.assertEqual(self.call("POST", "/api/model", {"model": 5})[0], 400)
        self.assertEqual(self.call("POST", "/api/model", {"model": "m2", "score": "yes"})[0], 400)
        for bad in (0, -1, "5", True, 1.5, 10 ** 9):
            self.assertEqual(self.call("POST", "/api/model", {"model": "m2", "score": True, "limit": bad})[0], 400, bad)
        self.assertEqual(self.call("GET", "/api/models")[1]["active"], "m1")      # nothing changed

    def test_only_a_signed_in_person_can_see_or_change_it(self):
        self.assertEqual(self.call("GET", "/api/models", token=False)[0], 401)
        self.assertEqual(self.call("POST", "/api/model", {"model": "m2"}, token=False)[0], 401)

    def test_a_limit_is_accepted_when_it_is_a_whole_number_and_nothing_is_waiting(self):
        status, out = self.call("POST", "/api/model", {"model": "m2", "score": True, "limit": 100})
        self.assertEqual((status, out["active"], out["waiting"]), (200, "m2", 0))

    def test_the_page_has_the_menu_and_the_bar(self):
        for marker in ('id="model-sel"', 'id="modelbar"', "function switchModel", "function scoreWithModel", "/api/models", "/api/model"):
            self.assertIn(marker, viewer.PAGE)


if __name__ == "__main__":
    unittest.main()
