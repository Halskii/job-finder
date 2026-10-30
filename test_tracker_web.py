"""Offline tests for the tracker endpoints and page of viewer.py. Each test gets its own database and server."""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

import db
import sources
import viewer

REPORT = """# Job matches, 2026-10-30

## 91 | Platform Engineer at Stripe
- Location: Remote | Pay: $120,000 - $160,000 | Posted: 2026-10-20
- Verdict: **apply** | Good.
- Matched: Kubernetes
- Missing required: none noted
- Dealbreakers: none
- Resume angle: Lead with platform work.
- Link: https://example.com/jobs/1
- ID: `greenhouse:stripe:1`

## 70 | Software Engineer at Acme
- Location: Austin, TX | Pay: n/a - n/a | Posted: unknown
- Verdict: **maybe** | Stretch.
- Matched: n/a
- Missing required: none noted
- Dealbreakers: none
- Resume angle:
- Link: https://example.com/jobs/2
- ID: `greenhouse:acme:2`
"""


class TrackerWebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "reports").mkdir()
        (root / "reports" / "2026-10-30.md").write_text(REPORT, encoding="utf-8")
        conn = db.connect(str(root / "jobs.db"))
        for id_, company, title in (("greenhouse:stripe:1", "Stripe", "Platform Engineer"), ("greenhouse:acme:2", "Acme", "Software Engineer")):
            db.add_job(conn, sources.Job(id_, "greenhouse", company, title, "Remote", "https://example.com/jobs/" + id_[-1], description="d"), 1.0)
        conn.close()
        self.saved = (viewer.REPORTS, viewer.DB_PATH)
        viewer.REPORTS, viewer.DB_PATH = root / "reports", root / "jobs.db"
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), viewer.Handler)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        viewer.REPORTS, viewer.DB_PATH = self.saved
        self.tmp.cleanup()

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def post(self, path, body):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def job(self, id_):
        return next(j for j in self.get("/api/report/2026-10-30.md")[1]["jobs"] if j["id"] == id_)

    # ---------------------------------------------------------------- marking applied
    def test_empty_tracker(self):
        status, body = self.get("/api/tracker")
        self.assertEqual(status, 200)
        self.assertEqual(body["applications"], [])
        self.assertEqual(body["stats"]["all"], {"applications": 0, "companies": 0})
        self.assertEqual(body["stats"]["today"], date.today().isoformat())

    def test_mark_applied_adds_it_to_the_tracker_with_todays_date(self):
        status, body = self.post("/api/mark", {"id": "greenhouse:stripe:1", "status": "applied"})
        self.assertEqual((status, body), (200, {"updated": 1, "applied_at": date.today().isoformat()}))
        t = self.get("/api/tracker")[1]
        self.assertEqual([(a["company"], a["title"], a["applied_at"], a["status"]) for a in t["applications"]],
                         [("Stripe", "Platform Engineer", date.today().isoformat(), "applied")])
        self.assertEqual(t["applications"][0]["url"], "https://example.com/jobs/1")
        for k in ("week", "month", "all"):
            self.assertEqual(t["stats"][k]["applications"], 1, k)

    def test_the_report_shows_the_applied_date(self):
        self.assertIsNone(self.job("greenhouse:stripe:1")["applied_at"])
        self.post("/api/mark", {"id": "greenhouse:stripe:1", "status": "applied"})
        self.assertEqual(self.job("greenhouse:stripe:1")["applied_at"], date.today().isoformat())
        self.assertIsNone(self.job("greenhouse:acme:2")["applied_at"])

    def test_undo_removes_it_from_the_tracker(self):
        self.post("/api/mark", {"id": "greenhouse:stripe:1", "status": "applied"})
        status, body = self.post("/api/mark", {"id": "greenhouse:stripe:1", "status": "new"})
        self.assertEqual((status, body), (200, {"updated": 1, "applied_at": None}))
        self.assertEqual(self.get("/api/tracker")[1]["stats"]["all"]["applications"], 0)

    def test_interview_after_applied_keeps_the_date_and_the_count(self):
        self.post("/api/mark", {"id": "greenhouse:stripe:1", "status": "applied"})
        self.post("/api/mark", {"id": "greenhouse:stripe:1", "status": "interview"})
        t = self.get("/api/tracker")[1]
        self.assertEqual((t["applications"][0]["status"], t["applications"][0]["applied_at"]), ("interview", date.today().isoformat()))
        self.assertEqual(t["stats"]["all"]["applications"], 1)

    def test_two_applications_at_two_companies(self):
        self.post("/api/mark", {"id": "greenhouse:stripe:1", "status": "applied"})
        self.post("/api/mark", {"id": "greenhouse:acme:2", "status": "applied"})
        s = self.get("/api/tracker")[1]["stats"]
        self.assertEqual((s["all"]["applications"], s["all"]["companies"]), (2, 2))

    def test_changing_the_status_from_the_tracker_keeps_the_date_and_count(self):
        self.post("/api/mark", {"id": "greenhouse:stripe:1", "status": "applied"})
        self.post("/api/applied-date", {"id": "greenhouse:stripe:1", "date": (date.today() - timedelta(days=1)).isoformat()})
        before = self.get("/api/tracker")[1]
        for outcome in ("interview", "offer", "accepted", "rejected", "withdrawn"):
            status, body = self.post("/api/mark", {"id": "greenhouse:stripe:1", "status": outcome})
            self.assertEqual((status, body["updated"], body["applied_at"]), (200, 1, before["applications"][0]["applied_at"]), outcome)
            row = self.get("/api/tracker")[1]["applications"][0]
            self.assertEqual((row["status"], row["applied_at"]), (outcome, before["applications"][0]["applied_at"]), outcome)
        self.assertEqual(self.get("/api/tracker")[1]["stats"]["all"], before["stats"]["all"])
        self.assertEqual(self.job("greenhouse:stripe:1")["status"], "withdrawn")   # the Matches list sees it too

    def test_unknown_status_is_refused(self):
        self.assertEqual(self.post("/api/mark", {"id": "greenhouse:stripe:1", "status": "ghosted"})[0], 400)

    def test_page_offers_every_outcome_in_the_dropdown(self):
        with urllib.request.urlopen(self.base + "/") as r:
            page = r.read().decode("utf-8")
        for label in ("Applied", "Interview", "Offer received", "Offer accepted", "Rejected", "Withdrawn"):
            self.assertIn(label, page)

    # ---------------------------------------------------------------- changing the date
    def test_change_the_date_moves_it_between_week_month_and_all_time(self):
        self.post("/api/mark", {"id": "greenhouse:stripe:1", "status": "applied"})
        old = date.today().replace(day=1) - timedelta(days=1)   # last day of the previous month
        self.assertEqual(self.post("/api/applied-date", {"id": "greenhouse:stripe:1", "date": old.isoformat()}), (200, {"updated": 1}))
        s = self.get("/api/tracker")[1]["stats"]
        self.assertEqual((s["week"]["applications"], s["month"]["applications"], s["all"]["applications"]), (0, 0, 1))

    def test_date_validation(self):
        self.post("/api/mark", {"id": "greenhouse:stripe:1", "status": "applied"})
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        for bad in (tomorrow, "2026-13-40", "10/30/2026", "20261030", "", None, 20261030, "2026-10-30; DROP TABLE jobs"):
            status, _ = self.post("/api/applied-date", {"id": "greenhouse:stripe:1", "date": bad})
            self.assertEqual(status, 400, bad)
        self.assertEqual(self.get("/api/tracker")[1]["applications"][0]["applied_at"], date.today().isoformat())   # unchanged

    def test_cannot_set_a_date_on_a_job_that_is_not_tracked(self):
        status, body = self.post("/api/applied-date", {"id": "greenhouse:acme:2", "date": date.today().isoformat()})
        self.assertEqual((status, body), (200, {"updated": 0}))
        self.assertEqual(self.get("/api/tracker")[1]["stats"]["all"]["applications"], 0)

    def test_missing_fields_and_wrong_content_type(self):
        self.assertEqual(self.post("/api/applied-date", {"id": "greenhouse:stripe:1"})[0], 400)
        req = urllib.request.Request(self.base + "/api/applied-date", data=b"{}", method="POST", headers={"Content-Type": "text/plain"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 400)

    # ---------------------------------------------------------------- the page
    def test_page_has_the_tracker_tab_and_apply_button(self):
        with urllib.request.urlopen(self.base + "/") as r:
            page = r.read().decode("utf-8")
        for needle in ("Mark as applied", 'id="tab-tracker"', "This week", "This month", "All time", "/api/tracker", "/api/applied-date"):
            self.assertIn(needle, page)

    def test_bad_host_is_rejected_for_the_new_endpoint(self):
        req = urllib.request.Request(self.base + "/api/tracker", headers={"Host": "evil.example.com"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 403)

    def test_an_unsafe_link_never_reaches_the_tracker_page(self):
        conn = db.connect(str(viewer.DB_PATH))
        conn.execute("UPDATE jobs SET url='javascript:alert(1)' WHERE id='greenhouse:acme:2'")
        conn.commit()
        conn.close()
        self.post("/api/mark", {"id": "greenhouse:acme:2", "status": "applied"})
        self.assertEqual(self.get("/api/tracker")[1]["applications"][0]["url"], "")


if __name__ == "__main__":
    unittest.main()
