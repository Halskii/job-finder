"""What an online deployment needs: the health check, host names, the privacy page, deleting an account, and PDFs inside a container."""
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import auth
import db
import sources
import tailor
import userdata
import web
from tests import webtest


class ShipTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.site = webtest.Site(cls.tmp.name)
        cls.base = cls.site.base

    @classmethod
    def tearDownClass(cls):
        cls.site.stop()
        cls.tmp.cleanup()

    def call(self, method, path, body=None, token=None, headers=None):
        h = {"Content-Type": "application/json", **(headers or {})}
        if token:
            h["Cookie"] = f"{web.COOKIE}={token}"
        req = urllib.request.Request(self.base + path, method=method, headers=h, data=None if body is None else json.dumps(body).encode())
        try:
            with urllib.request.urlopen(req) as r:
                status, head, raw = r.status, r.headers, r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            status, head, raw = e.code, e.headers, e.read().decode("utf-8")
        try:
            return status, head, json.loads(raw)
        except ValueError:
            return status, head, raw

    # ------------------------------------------------------------ health and hosts
    def test_the_health_check_needs_no_sign_in_and_reports_a_database_that_is_down(self):
        self.assertEqual(self.call("GET", "/healthz")[::2], (200, {"ok": True}))
        with mock.patch.object(web, "auth_db", side_effect=RuntimeError("down")):
            status, _, body = self.call("GET", "/healthz")
        self.assertEqual((status, body["code"]), (503, "db_down"))

    def test_the_sites_own_host_name_is_accepted_and_this_computers_names_always_are(self):
        with mock.patch.dict(os.environ, {"ALLOWED_HOSTS": "northhelm.example"}):
            for host, expected in (("northhelm.example", 200), ("northhelm.example:443", 200), ("localhost", 200), ("127.0.0.1:8765", 200), ("evil.example", 403), ("northhelm.example.evil.com", 403)):
                self.assertEqual(self.call("GET", "/healthz", headers={"Host": host})[0], expected, host)

    # ------------------------------------------------------------ privacy
    def test_the_privacy_page_is_public_and_shows_a_contact_only_when_there_is_one(self):
        with mock.patch.dict(os.environ, {"CONTACT_EMAIL": ""}):
            status, _, page = self.call("GET", "/privacy")
        self.assertEqual(status, 200)
        for needle in ("What is stored about you", "What is sent to Claude", "Delete my account"):
            self.assertIn(needle, page)
        self.assertNotIn("mailto:", page)
        with mock.patch.dict(os.environ, {"CONTACT_EMAIL": 'me@example.com"><script>x</script>'}):
            page = self.call("GET", "/privacy")[2]
        self.assertNotIn("<script>x", page)                                         # nothing typed into the setting can become markup
        self.assertIn("mailto:me@example.com", page)

    def test_the_sign_in_and_profile_pages_link_to_it(self):
        self.assertIn('href="/privacy"', self.call("GET", "/login")[2])
        page = self.call("GET", "/profile", token=self.site.token)[2]
        for needle in ('href="/privacy"', "Delete my account", "/api/account/delete"):
            self.assertIn(needle, page)

    # ------------------------------------------------------------ deleting an account
    def make_account(self, email):
        uid, token = self.site.new_user(email)
        conn = self.site.db(uid)
        db.add_job(conn, sources.Job(f"gh:{email}:1", "greenhouse", "Co", "Dev", "Austin, TX", "https://x", description="d"), 1.0)
        db.add_round(conn, f"gh:{email}:1", "Phone screen")
        db.add_usage(conn, "tailor")
        conn.close()
        userdata.save_settings(uid, {**userdata.SETTINGS_DEFAULTS, "titles": ["Dev"]})
        userdata.save_resume_text(uid, "Developer with experience building web services and deploying them to the cloud for several years.")
        (userdata.home(uid) / "Optimized Resumes").mkdir()
        (userdata.home(uid) / "Optimized Resumes" / "made.pdf").write_bytes(b"%PDF-1.4")
        return uid, token

    def test_deleting_needs_the_right_email_and_a_session(self):
        uid, token = self.make_account("keep@example.com")
        self.assertEqual(self.call("POST", "/api/account/delete", {"confirm": "someone@else.com"}, token)[0], 400)
        self.assertEqual(self.call("POST", "/api/account/delete", {}, token)[0], 400)
        self.assertEqual(self.call("POST", "/api/account/delete", {"confirm": "keep@example.com"}, "bad-token")[0], 401)
        self.assertEqual(self.call("GET", "/api/me", token=token)[0], 200)                  # still there
        self.assertTrue(userdata.home(uid).exists())

    def test_deleting_removes_the_account_its_data_and_its_files_and_nobody_elses(self):
        uid, token = self.make_account("gone@example.com")
        other, other_token = self.make_account("stays@example.com")
        status, head, _ = self.call("POST", "/api/account/delete", {"confirm": " Gone@Example.com "}, token)
        self.assertEqual(status, 200)
        self.assertIn(f"{web.COOKIE}=", head["Set-Cookie"])                                 # the cookie is cleared
        self.assertEqual(self.call("GET", "/api/me", token=token)[0], 401)                  # and the session is gone
        self.assertEqual(self.call("POST", "/auth/login", {"email": "gone@example.com", "password": webtest.PASSWORD})[0], 401)
        self.assertFalse((userdata.BASE / str(uid)).exists())
        anon = db.connect(**{**web.viewer.DB_ARGS, "user": False})
        counts = {t: anon.execute(f"SELECT COUNT(*) AS n FROM {t} WHERE user_id = ?", (uid,)).fetchone()["n"] for t in ("sessions", "usage", "user_runs")}
        self.assertEqual(sum(counts.values()), 0)
        anon.close()
        survivor = self.site.db(other)
        self.assertEqual(survivor.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"], 1)
        self.assertEqual(len(db.rounds_for(survivor, "gh:stays@example.com:1")), 1)
        survivor.close()
        self.assertTrue((userdata.home(other) / "Optimized Resumes" / "made.pdf").exists())
        self.assertEqual(self.call("GET", "/api/me", token=other_token)[0], 200)


class PdfInContainerTests(unittest.TestCase):
    def run_print(self, env):
        seen = []

        def fake_run(cmd, **kw):
            seen.append(cmd)
            for part in cmd:
                if part.startswith("--print-to-pdf="):
                    Path(part.split("=", 1)[1]).write_bytes(b"%PDF-1.4")

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, env), mock.patch.object(tailor, "find_browser", return_value="chromium"), \
                mock.patch.object(tailor.subprocess, "run", fake_run):
            tailor.html_to_pdf("<p>hi</p>", Path(tmp) / "a.pdf")
        return seen[0]

    def test_the_browser_gets_the_flags_a_container_needs_only_when_asked(self):
        plain = self.run_print({"CHROME_NO_SANDBOX": "0"})
        self.assertNotIn("--no-sandbox", plain)
        boxed = self.run_print({"CHROME_NO_SANDBOX": "1"})
        self.assertIn("--no-sandbox", boxed)
        self.assertIn("--disable-dev-shm-usage", boxed)
        self.assertIn("--headless", boxed)


if __name__ == "__main__":
    unittest.main()
