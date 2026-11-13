"""`upkeep: manual`: no fetching or crawling on its own, and a button that starts a fetch."""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

import ingest
import tailor
import web
from tests import webtest


def config(**values):
    """Stand-in for tailor.config_value that answers from `values` (and the default for anything else)."""
    return lambda key, default=None: values.get(key, default)


class ManualFetchTests(unittest.TestCase):
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
        web.FETCH.update(running=False, started=0.0, result=None)

    def call(self, method, path):
        req = urllib.request.Request(self.base + path, method=method, headers={"Content-Type": "application/json",
                                     "Cookie": f"{web.COOKIE}={self.site.token}"}, data=b"{}" if method == "POST" else None)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_the_setting_defaults_to_automatic_and_only_the_word_manual_turns_it_off(self):
        for given, expected in ((None, False), ("auto", False), ("", False), ("Manual", True), (" manual ", True), ("off", False)):
            with mock.patch.object(tailor, "config_value", config(upkeep=given)):
                self.assertIs(web.manual_upkeep(), expected, given)
        with mock.patch.object(tailor, "config_value", config()):
            self.assertIs(web.manual_upkeep(), False)

    def test_the_status_tells_the_page_whether_to_show_the_button(self):
        for given, shown in (("manual", True), ("auto", False)):
            with mock.patch.object(tailor, "config_value", config(upkeep=given)):
                status, st = self.call("GET", "/api/fetch/status")
            self.assertEqual((status, st["manual"], st["running"]), (200, shown, False), given)

    def test_the_button_is_refused_while_upkeep_is_automatic(self):
        with mock.patch.object(tailor, "config_value", config(upkeep="auto")), mock.patch.object(web, "RUNS", True), \
                mock.patch.object(ingest, "run_fetch") as fetch:
            status, body = self.call("POST", "/api/fetch")
        self.assertEqual((status, body["code"]), (403, "fetch_off"))
        fetch.assert_not_called()                                          # nobody can start the slow fetch on the deployed site

    def test_the_button_starts_one_fetch_and_a_second_press_is_refused_until_it_ends(self):
        release = threading.Event()
        done = threading.Event()

        def slow_fetch(*args, **kwargs):
            release.wait(10)
            return {"stored": 5, "queued": 1}

        with mock.patch.object(tailor, "config_value", config(upkeep="manual")), mock.patch.object(web, "RUNS", True), \
                mock.patch.object(ingest, "run_fetch", side_effect=slow_fetch) as fetch:
            status, st = self.call("POST", "/api/fetch")
            self.assertEqual((status, st["running"]), (200, True))
            status, body = self.call("POST", "/api/fetch")                 # pressed again while it runs
            self.assertEqual((status, body["code"]), (409, "busy"))
            self.assertIs(self.call("GET", "/api/fetch/status")[1]["running"], True)
            release.set()
            for _ in range(100):
                st = self.call("GET", "/api/fetch/status")[1]
                if not st["running"]:
                    done.set()
                    break
                threading.Event().wait(0.05)
        self.assertTrue(done.is_set(), "the fetch never finished")
        self.assertEqual(st["result"], {"stored": 5, "queued": 1})
        fetch.assert_called_once()

    def test_a_fetch_that_crashes_is_reported_and_the_button_works_again(self):
        with mock.patch.object(tailor, "config_value", config(upkeep="manual")), mock.patch.object(web, "RUNS", True), \
                mock.patch.object(ingest, "run_fetch", side_effect=RuntimeError("boom")):
            self.call("POST", "/api/fetch")
            for _ in range(100):
                st = self.call("GET", "/api/fetch/status")[1]
                if not st["running"]:
                    break
                threading.Event().wait(0.05)
        self.assertFalse(st["running"])
        self.assertIn("error", st["result"])
        self.assertNotIn("boom", st["result"]["error"])                    # the person gets a kind sentence, not the technical message

    def test_the_page_has_the_fetch_bar_and_the_scheduler_skips_its_work_in_manual_mode(self):
        req = urllib.request.Request(self.base + "/", headers={"Cookie": f"{web.COOKIE}={self.site.token}"})
        with urllib.request.urlopen(req) as r:
            page = r.read().decode("utf-8")
        self.assertIn('id="fetchbar"', page)
        self.assertIn("/api/fetch", page)
        with mock.patch.object(tailor, "config_value", config(upkeep="manual")), mock.patch.object(ingest, "next_job") as next_job, \
                mock.patch.object(web.pipeline, "poll_queued") as poll, mock.patch.object(web.WAKE, "wait", side_effect=StopIteration):
            with self.assertRaises(StopIteration):                         # one turn of the scheduler's loop
                web.scheduler()
        next_job.assert_not_called()                                       # it decides nothing on its own...
        poll.assert_called_once()                                          # ...but still hands queued searches to the worker


if __name__ == "__main__":
    unittest.main()
