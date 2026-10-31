"""Offline tests for viewer.py: report parsing, the round trip with main.write_report, and the local server."""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import db
import main
import sources
import viewer
from errors import UserError

SAMPLE = """# Job matches, 2026-10-30

## 91 | Platform Engineer at Stripe
- Location: San Francisco, CA | Remote - US | Pay: $120,000 - $160,000 | Posted: 2026-10-20
- Verdict: **apply** | Strong overlap with Kubernetes and Terraform.
- Matched: Kubernetes, Terraform, Python
- Missing required: Go
- Dealbreakers: none
- Resume angle: Lead with the platform work.
- Link: https://example.com/jobs/1
- ID: `greenhouse:stripe:1`

## 62 | Engineer at Scale at Acme
- Location: n/a | Pay: n/a - n/a | Posted: unknown
- Verdict: **maybe** | Stretch.
- Matched: n/a
- Missing required: none noted
- Dealbreakers: none
- Resume angle:
- Link: javascript:alert(1)
- ID: `lever:acme:2`
"""


class ParseTests(unittest.TestCase):
    def test_parse_fields(self):
        data = viewer.parse_report(SAMPLE)
        self.assertEqual(data["title"], "Job matches, 2026-10-30")
        a, b = data["jobs"]
        self.assertEqual(a["score"], 91)
        self.assertEqual(a["location"], "San Francisco, CA | Remote - US")   # the '|' inside the location survives
        self.assertEqual(a["pay"], "$120,000 - $160,000")
        self.assertEqual((a["verdict"], a["reason"]), ("apply", "Strong overlap with Kubernetes and Terraform."))
        self.assertEqual(a["matched"], ["Kubernetes", "Terraform", "Python"])
        self.assertEqual(a["missing_required"], ["Go"])
        self.assertEqual(a["dealbreakers"], [])
        self.assertEqual(a["id"], "greenhouse:stripe:1")
        self.assertEqual(b["matched"], [])
        self.assertEqual(b["resume_angle"], "")

    def test_heading_split_uses_last_at(self):
        self.assertEqual(viewer.split_heading("Engineer at Scale at Acme"), ("Engineer at Scale", "Acme"))
        self.assertEqual(viewer.split_heading("No company here"), ("No company here", ""))

    def test_only_http_links_survive(self):
        self.assertEqual(viewer.safe_url("javascript:alert(1)"), "")
        self.assertEqual(viewer.safe_url("data:text/html,x"), "")
        self.assertEqual(viewer.safe_url("https://example.com/a"), "https://example.com/a")


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.db_path = root / "jobs.db"
        (root / "reports").mkdir()
        (root / "reports" / "2026-10-30.md").write_text(SAMPLE, encoding="utf-8")
        (root / "secret.md").write_text("# secret", encoding="utf-8")
        conn = db.connect(str(cls.db_path))
        db.add_job(conn, sources.Job("greenhouse:stripe:1", "greenhouse", "Stripe", "Platform Engineer",
                                     "Remote", "https://example.com/jobs/1", description="Build <b>things</b>."), 1.0)
        conn.close()
        cls._saved = (viewer.ROOT, viewer.REPORTS, viewer.DB_PATH)
        viewer.REPORTS, viewer.DB_PATH = root / "reports", cls.db_path
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), viewer.Handler)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        viewer.ROOT, viewer.REPORTS, viewer.DB_PATH = cls._saved
        cls.tmp.cleanup()

    def get(self, path, headers=None):
        req = urllib.request.Request(self.base + path, headers=headers or {})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8")

    def post(self, path, body, ctype="application/json"):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": ctype})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_page_and_report_list(self):
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("<title>Job matches</title>", body)
        self.assertIn('id="theme-toggle"', body)                    # Dark (the default) and Light
        self.assertIn(':root[data-theme="light"]', body)
        self.assertIn('localStorage.getItem("theme")', body)        # the choice is remembered
        self.assertEqual(json.loads(self.get("/api/reports")[1]), ["2026-10-30.md"])

    def test_report_joins_db_and_blocks_bad_links(self):
        status, body = self.get("/api/report/2026-10-30.md")
        self.assertEqual(status, 200)
        a, b = json.loads(body)["jobs"]
        self.assertEqual(a["description"], "Build <b>things</b>.")   # raw text; the page renders it with textContent
        self.assertEqual(a["company"], "Stripe")
        self.assertEqual(b["url"], "")                               # javascript: link dropped
        self.assertEqual(b["description"], "")                       # not in the DB

    def test_path_traversal_and_unknown_report(self):
        self.assertEqual(self.get("/api/report/..%5Csecret.md")[0], 404)
        self.assertEqual(self.get("/api/report/../secret.md")[0], 404)
        self.assertEqual(self.get("/api/report/missing.md")[0], 404)

    def test_bad_host_rejected(self):
        self.assertEqual(self.get("/api/reports", {"Host": "evil.example.com"})[0], 403)

    def test_mark_status(self):
        status, body = self.post("/api/mark", {"id": "greenhouse:stripe:1", "status": "applied"})
        self.assertEqual((status, body["updated"]), (200, 1))
        data = json.loads(self.get("/api/report/2026-10-30.md")[1])
        self.assertEqual(data["jobs"][0]["status"], "applied")
        status, body = self.post("/api/mark", {"id": "nope", "status": "saved"})
        self.assertEqual((status, body["updated"]), (200, 0))

    def test_mark_rejects_bad_input(self):
        self.assertEqual(self.post("/api/mark", {"id": "greenhouse:stripe:1", "status": "deleted"})[0], 400)
        self.assertEqual(self.post("/api/mark", {"status": "saved"})[0], 400)
        self.assertEqual(self.post("/api/mark", {"id": "x", "status": "saved"}, ctype="text/plain")[0], 400)


    def test_saved_jobs_list_follows_the_saved_status(self):
        self.assertEqual(self.post("/api/mark", {"id": "greenhouse:stripe:1", "status": "saved"})[0], 200)
        status, body = self.get("/api/saved")
        saved = json.loads(body)["jobs"]
        self.assertEqual((status, [j["id"] for j in saved]), (200, ["greenhouse:stripe:1"]))
        self.assertEqual((saved[0]["title"], saved[0]["company"], saved[0]["status"]), ("Platform Engineer", "Stripe", "saved"))
        self.assertEqual(saved[0]["description"], "Build <b>things</b>.")
        self.post("/api/mark", {"id": "greenhouse:stripe:1", "status": "new"})
        self.assertEqual(json.loads(self.get("/api/saved")[1])["jobs"], [])

    def test_interview_rounds_through_the_tracker(self):
        jid = "greenhouse:stripe:1"
        self.assertEqual(self.post("/api/round", {"action": "add", "id": "nope"})[0], 404)
        self.post("/api/mark", {"id": jid, "status": "new"})
        self.assertEqual(self.post("/api/round", {"action": "add", "id": jid})[0], 404)       # not in the tracker yet
        self.post("/api/mark", {"id": jid, "status": "applied"})
        status, body = self.post("/api/round", {"action": "add", "id": jid})
        self.assertEqual(status, 200)
        rid = body["id"]
        self.post("/api/round", {"action": "add", "id": jid, "kind": "System design", "date": "2999-01-02", "outcome": "waiting"})
        app = next(a for a in json.loads(self.get("/api/tracker")[1])["applications"] if a["id"] == jid)
        self.assertEqual(app["status"], "interview")                                          # a first round moves applied to interview
        self.assertEqual([(r["kind"], r["date"], r["outcome"]) for r in app["rounds"]],
                         [("Round 1", None, "scheduled"), ("System design", "2999-01-02", "waiting")])
        self.assertEqual(self.post("/api/round", {"action": "update", "round_id": rid, "kind": "Phone\x00 screen",
                                                  "date": "2026-11-03", "outcome": "passed", "notes": "ok\n\nfine"})[0], 200)
        app = next(a for a in json.loads(self.get("/api/tracker")[1])["applications"] if a["id"] == jid)
        self.assertEqual((app["rounds"][0]["kind"], app["rounds"][0]["notes"]), ("Phone screen", "ok fine"))
        for bad in ({"action": "update", "round_id": rid, "kind": "x", "date": "2026-13-45"},
                    {"action": "update", "round_id": rid, "kind": "x", "outcome": "hired"},
                    {"action": "update", "round_id": rid, "kind": "  "},
                    {"action": "explode"}, {"action": "update"}):
            self.assertEqual(self.post("/api/round", bad)[0], 400, bad)
        self.assertEqual(self.post("/api/round", {"action": "delete", "round_id": rid})[0], 200)
        self.assertEqual(self.post("/api/round", {"action": "delete", "round_id": rid})[0], 404)
        self.assertEqual(self.post("/api/round", {"action": "add", "id": jid}, ctype="text/plain")[0], 400)
        self.post("/api/mark", {"id": jid, "status": "new"})

    def stream_tips(self, body):
        req = urllib.request.Request(self.base + "/api/tips", data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.headers["Content-Type"], [json.loads(line) for line in r.read().decode("utf-8").splitlines() if line]
        except urllib.error.HTTPError as e:
            return e.code, e.headers["Content-Type"], json.loads(e.read())

    def test_tips_endpoint_streams_one_json_event_per_line(self):
        from unittest import mock
        self.assertEqual(self.post("/api/tips", {})[0], 400)
        self.assertEqual(self.stream_tips({"id": "nope"})[0], 404)
        events = [{"type": "start", "disclaimer": "d"}, {"type": "text", "text": "## Hi"}, {"type": "done", "result": {}}]
        with mock.patch.object(viewer, "tips_events", return_value=iter(events)) as f:
            status, ctype, got = self.stream_tips({"id": "greenhouse:stripe:1"})
            self.assertEqual((status, got), (200, events))
            self.assertIn("ndjson", ctype)
            f.assert_called_once_with("greenhouse:stripe:1", False)
        with mock.patch.object(viewer, "tips_events", return_value=iter([])) as f:
            self.stream_tips({"id": "greenhouse:stripe:1", "refresh": True})
            f.assert_called_once_with("greenhouse:stripe:1", True)
        with viewer._tipping:   # another job's prep is being written
            self.assertEqual(self.stream_tips({"id": "greenhouse:stripe:1"})[0], 409)

    def test_a_failure_while_writing_becomes_an_error_event(self):
        from unittest import mock
        with mock.patch.object(viewer.tips, "stream_tips", side_effect=UserError("No Anthropic API key found. Add it to .env.", "no_api_key")):
            got = list(viewer.tips_events("greenhouse:stripe:1"))
        self.assertEqual(got, [{"type": "error", "error": "No Anthropic API key found. Add it to .env.", "code": "no_api_key"}])
        with mock.patch.object(viewer.tips, "stream_tips", side_effect=RuntimeError("boom at C:\\secret\\file.py line 3")), \
                self.assertLogs("job-finder", level="ERROR"):
            got = list(viewer.tips_events("greenhouse:stripe:1"))
        self.assertEqual((got[0]["type"], got[0]["code"]), ("error", "internal"))
        self.assertNotIn("secret", got[0]["error"])          # details of an unexpected error stay in the terminal
        self.assertNotIn("RuntimeError", got[0]["error"])

    def test_interview_prep_list_holds_jobs_with_saved_prep(self):
        conn = db.connect(str(viewer.DB_PATH))
        try:
            self.assertEqual(json.loads(self.get("/api/prep")[1])["jobs"], [])
            db.save_tips(conn, "greenhouse:stripe:1", {"markdown": "## Summary"}, "m", "2026-10-31")
            jobs = json.loads(self.get("/api/prep")[1])["jobs"]
            self.assertEqual([(j["id"], j["company"], j["prep_created"]) for j in jobs], [("greenhouse:stripe:1", "Stripe", "2026-10-31")])
        finally:
            conn.execute("DELETE FROM interview_tips")
            conn.commit()
            conn.close()

    def test_discarded_jobs_are_listed_in_the_trash_until_restored(self):
        jid = "greenhouse:stripe:1"
        self.post("/api/mark", {"id": jid, "status": "new"})
        self.assertEqual(json.loads(self.get("/api/trash")[1])["jobs"], [])
        self.post("/api/mark", {"id": jid, "status": "skipped"})
        trash = json.loads(self.get("/api/trash")[1])["jobs"]
        self.assertEqual([(j["id"], j["title"], j["status"]) for j in trash], [(jid, "Platform Engineer", "skipped")])
        self.assertEqual(trash[0]["description"], "Build <b>things</b>.")
        self.post("/api/mark", {"id": jid, "status": "new"})
        self.assertEqual(json.loads(self.get("/api/trash")[1])["jobs"], [])

    def test_one_job_by_id_for_the_trackers_details_button(self):
        status, body = self.get("/api/job/greenhouse%3Astripe%3A1")
        job = json.loads(body)
        self.assertEqual((status, job["id"], job["title"], job["company"]), (200, "greenhouse:stripe:1", "Platform Engineer", "Stripe"))
        self.assertEqual(job["description"], "Build <b>things</b>.")
        self.assertEqual(self.get("/api/job/nope")[0], 404)

    def test_tailor_endpoint(self):
        from unittest import mock
        self.assertEqual(self.post("/api/tailor", {"id": "x"}, ctype="text/plain")[0], 400)
        self.assertEqual(self.post("/api/tailor", {})[0], 400)
        with mock.patch.object(viewer, "tailor_for", return_value={"files": ["a.pdf"]}) as f:
            self.assertEqual(self.post("/api/tailor", {"id": "greenhouse:stripe:1"}), (200, {"files": ["a.pdf"]}))
            f.assert_called_once_with("greenhouse:stripe:1", "", "")
        with mock.patch.object(viewer, "tailor_for", return_value={"files": []}) as f:   # the optional requests are passed along, capped
            self.post("/api/tailor", {"id": "x", "resume_request": "lead with k8s", "letter_request": "y" * 5000})
            f.assert_called_once_with("x", "lead with k8s", "y" * 2000)
        with mock.patch.object(viewer, "tailor_for", return_value={"files": []}) as f:   # hidden and control characters never reach the model
            self.post("/api/tailor", {"id": "x", "resume_request": "a\x00b\u202ec", "letter_request": "ok"})
            f.assert_called_once_with("x", "abc", "ok")
        with mock.patch.object(viewer, "tailor_for", side_effect=LookupError("no such job")):
            self.assertEqual(self.post("/api/tailor", {"id": "nope"})[0], 404)
        with mock.patch.object(viewer, "tailor_for", side_effect=UserError("No Anthropic API key found. Add it to .env.", "no_api_key")):
            status, body = self.post("/api/tailor", {"id": "x"})
            self.assertEqual((status, body["code"]), (422, "no_api_key"))   # something the person can fix
            self.assertIn("No Anthropic API key", body["error"])
        with mock.patch.object(viewer, "tailor_for", side_effect=ValueError("secret detail")), self.assertLogs("job-finder", level="ERROR"):
            status, body = self.post("/api/tailor", {"id": "x"})
            self.assertEqual((status, body["code"]), (500, "internal"))
            self.assertNotIn("secret", body["error"])
            self.assertNotIn("ValueError", body["error"])
        with viewer._tailoring:   # a tailoring already running
            self.assertEqual(self.post("/api/tailor", {"id": "x"})[0], 409)


    def test_every_error_response_has_a_sentence_and_a_code_and_never_a_trace(self):
        from unittest import mock
        cases = [self.get("/api/report/nope.md"), self.get("/api/nothing"), self.get("/api/job/nope"), self.get("/api/reports", {"Host": "evil.example"}),
                 self.post("/api/mark", {"id": "x", "status": "deleted"}), self.post("/api/applied-date", {"id": "x", "date": "2999-01-01"}),
                 self.post("/api/mark", {}), self.post("/api/round", {"action": "update", "round_id": 999, "kind": "x"})]
        for status, body in cases:
            data = json.loads(body) if isinstance(body, str) else body
            self.assertGreaterEqual(status, 400, data)
            self.assertTrue(data["error"].endswith("."), data)             # a sentence
            self.assertTrue(data["code"], data)
            for leak in ("Traceback", "Error:", "bad request", ".py", "no such"):
                self.assertNotIn(leak, data["error"], data)
        with mock.patch.object(viewer, "load_tracker", side_effect=RuntimeError("db exploded at /x/y.py")), self.assertLogs("job-finder", level="ERROR"):
            status, body = self.get("/api/tracker")        # an unexpected failure in any route is a friendly 500, not a dropped connection
        self.assertEqual(status, 500)
        self.assertNotIn("exploded", body)

    def test_a_negative_content_length_does_not_hang_the_server(self):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=5)
        conn.putrequest("POST", "/api/tailor")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", "-1")
        conn.endheaders()
        self.assertEqual(conn.getresponse().status, 400)
        conn.close()


class RoundTripTests(unittest.TestCase):
    def test_write_report_output_parses_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            saved_root = main.ROOT
            main.ROOT = Path(tmp)
            try:
                conn = db.connect(str(Path(tmp) / "jobs.db"))
                job = sources.Job("ashby:co:9", "ashby", "Co", "AI Engineer", "Austin, TX | Remote", "https://example.com/9",
                                  posted="2026-10-25", pay_min=100000, pay_max=140000, description="desc")
                db.add_job(conn, job, 2.0)
                db.save_score(conn, job.id, 88, "apply", {"reason": "Good fit.", "matched": ["Python", "LangChain"],
                                                          "missing_required": [], "dealbreakers": [], "resume_angle": "Lead with agents."})
                path = main.write_report(conn, {"scoring": {"report_size": 5, "report_min_score": 60}})
                conn.close()
                j = viewer.parse_report(path.read_text(encoding="utf-8"))["jobs"][0]
            finally:
                main.ROOT = saved_root
        self.assertEqual((j["score"], j["id"], j["url"]), (88, "ashby:co:9", "https://example.com/9"))
        self.assertEqual(j["location"], "Austin, TX | Remote")
        self.assertEqual(j["matched"], ["Python", "LangChain"])
        self.assertEqual((j["verdict"], j["reason"]), ("apply", "Good fit."))
        self.assertEqual(j["resume_angle"], "Lead with agents.")


if __name__ == "__main__":
    unittest.main()
