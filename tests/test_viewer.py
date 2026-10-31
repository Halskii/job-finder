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
        with mock.patch.object(viewer, "tailor_for", side_effect=RuntimeError("No ANTHROPIC_API_KEY found")):
            status, body = self.post("/api/tailor", {"id": "x"})
            self.assertEqual(status, 500)
            self.assertIn("No ANTHROPIC_API_KEY", body["error"])
        with viewer._tailoring:   # a tailoring already running
            self.assertEqual(self.post("/api/tailor", {"id": "x"})[0], 409)


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
