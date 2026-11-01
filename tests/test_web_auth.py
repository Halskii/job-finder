"""Sign-in over HTTP, the cookie it sets, and two users on one server who must not reach each other's data."""
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from unittest import mock

import auth
import db
import sources
import web
from tests import webtest

PASSWORD = webtest.PASSWORD


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.site = webtest.Site(cls.tmp.name)
        cls.base = cls.site.base

    @classmethod
    def tearDownClass(cls):
        cls.site.stop()
        cls.tmp.cleanup()

    def call(self, method, path, body=None, headers=None, cookie=None):
        """(status, headers, parsed body or text) of one request; redirects are not followed."""
        h = {"Content-Type": "application/json", **(headers or {})}
        if cookie:
            h["Cookie"] = f"{web.COOKIE}={cookie}"
        req = urllib.request.Request(self.base + path, method=method, headers=h, data=None if body is None else json.dumps(body).encode())
        opener = urllib.request.build_opener(NoRedirect)
        try:
            r = opener.open(req)
            status, head, raw = r.status, r.headers, r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            status, head, raw = e.code, e.headers, e.read().decode("utf-8")
        try:
            return status, head, json.loads(raw)
        except ValueError:
            return status, head, raw


class SignInTests(Base):
    def test_signed_out_visitors_are_sent_to_the_sign_in_page_and_the_api_refuses_them(self):
        status, head, _ = self.call("GET", "/")
        self.assertEqual((status, head["Location"]), (303, "/login"))
        for path in ("/api/me", "/api/tracker", "/api/saved", "/api/reports", "/api/job/x", "/api/files/a.pdf"):
            status, _, body = self.call("GET", path)
            self.assertEqual((status, body["code"]), (401, "not_signed_in"), path)
        status, _, body = self.call("POST", "/api/mark", {"id": "x", "status": "saved"})
        self.assertEqual((status, body["code"]), (401, "not_signed_in"))

    def test_the_sign_in_page_loads_and_offers_google_only_when_it_is_set_up(self):
        with mock.patch.dict("os.environ", {"GOOGLE_CLIENT_ID": ""}):
            status, head, page = self.call("GET", "/login")
        self.assertEqual(status, 200)
        self.assertIn("Create account", page)
        self.assertIn("const CLIENT_ID=\"\"", page)
        with mock.patch.dict("os.environ", {"GOOGLE_CLIENT_ID": "123.apps.googleusercontent.com"}):
            page = self.call("GET", "/login")[2]
        self.assertIn('const CLIENT_ID="123.apps.googleusercontent.com"', page)

    def test_sign_up_sets_a_safe_cookie_and_opens_the_app(self):
        status, head, body = self.call("POST", "/auth/signup", {"email": "new@example.com", "password": PASSWORD, "name": "New Person"})
        self.assertEqual((status, body["user"]["email"]), (200, "new@example.com"))
        cookie = head["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=lax", cookie)
        self.assertNotIn("Secure", cookie)          # plain http on this computer; it is set when served over https
        token = cookie.split(";")[0].split("=", 1)[1]
        self.assertEqual(self.call("GET", "/api/me", cookie=token)[2]["email"], "new@example.com")
        self.assertEqual(self.call("GET", "/", cookie=token)[0], 200)
        self.assertEqual(self.call("GET", "/login", cookie=token)[0], 303)   # already signed in

    def test_the_cookie_is_secure_over_https_or_when_asked(self):
        with mock.patch.dict("os.environ", {"COOKIE_SECURE": "1"}):
            head = self.call("POST", "/auth/signup", {"email": "sec@example.com", "password": PASSWORD})[1]
        self.assertIn("Secure", head["Set-Cookie"])
        with mock.patch.dict("os.environ", {"TRUST_PROXY": "1"}):
            head = self.call("POST", "/auth/login", {"email": "sec@example.com", "password": PASSWORD}, {"X-Forwarded-Proto": "https"})[1]
        self.assertIn("Secure", head["Set-Cookie"])

    def test_sign_in_sign_out_and_wrong_answers(self):
        self.call("POST", "/auth/signup", {"email": "io@example.com", "password": PASSWORD})
        status, _, body = self.call("POST", "/auth/login", {"email": "io@example.com", "password": "not the password"})
        self.assertEqual((status, body["code"]), (401, "bad_login"))
        self.assertTrue(body["error"].endswith("."))
        status, head, _ = self.call("POST", "/auth/login", {"email": "IO@example.com", "password": PASSWORD})
        self.assertEqual(status, 200)
        token = head["Set-Cookie"].split(";")[0].split("=", 1)[1]
        self.assertEqual(self.call("GET", "/api/me", cookie=token)[0], 200)
        status, head, _ = self.call("POST", "/auth/logout", {}, cookie=token)
        self.assertEqual(status, 200)
        self.assertEqual(self.call("GET", "/api/me", cookie=token)[0], 401)      # the session is gone on the server, not just in the browser
        self.assertEqual(self.call("POST", "/auth/signup", {"email": "io@example.com", "password": PASSWORD})[2]["code"], "email_taken")
        self.assertEqual(self.call("POST", "/auth/signup", {"email": "bad", "password": PASSWORD})[2]["code"], "invalid_email")
        self.assertEqual(self.call("POST", "/auth/signup", {"email": "w@example.com", "password": "short"})[2]["code"], "weak_password")

    def test_google_sign_in(self):
        claims = {"sub": "g-77", "email": "gee@example.com", "email_verified": True, "name": "Gee"}
        with mock.patch.dict("os.environ", {"GOOGLE_CLIENT_ID": "cid"}), mock.patch.object(auth, "verify_google_token", return_value=claims):
            status, head, body = self.call("POST", "/auth/google", {"credential": "token"})
        self.assertEqual((status, body["user"]["email"]), (200, "gee@example.com"))
        self.assertIn("HttpOnly", head["Set-Cookie"])
        with mock.patch.dict("os.environ", {"GOOGLE_CLIENT_ID": "cid"}):
            self.assertEqual(self.call("POST", "/auth/google", {"credential": "garbage"})[2]["code"], "bad_google_token")
            self.assertEqual(self.call("POST", "/auth/google", {})[0], 400)
        with mock.patch.dict("os.environ", {"GOOGLE_CLIENT_ID": ""}):
            self.assertEqual(self.call("POST", "/auth/google", {"credential": "x"})[2]["code"], "google_off")

    def test_requests_from_another_site_or_in_another_format_are_refused(self):
        body = {"email": "csrf@example.com", "password": PASSWORD}
        status, _, data = self.call("POST", "/auth/signup", body, {"Origin": "https://evil.example"})
        self.assertEqual((status, data["code"]), (403, "bad_origin"))
        host = self.base.split("//")[1]
        self.assertEqual(self.call("POST", "/auth/signup", body, {"Origin": f"http://{host}"})[0], 200)
        self.assertEqual(self.call("POST", "/auth/login", body, {"Content-Type": "text/plain"})[0], 400)
        self.assertEqual(self.call("GET", "/login", headers={"Host": "evil.example.com"})[2]["code"], "bad_host")

    def test_every_response_carries_the_security_headers(self):
        head = self.call("GET", "/api/me", cookie=self.site.token)[1]
        self.assertEqual(head["X-Content-Type-Options"], "nosniff")
        self.assertEqual(head["Cache-Control"], "no-store")
        self.assertEqual(head["X-Frame-Options"], "DENY")
        self.assertIn("default-src 'none'", head["Content-Security-Policy"])
        page = self.call("GET", "/", cookie=self.site.token)[1]
        self.assertNotIn("accounts.google.com", page["Content-Security-Policy"])   # only the sign-in page may load Google's script


class TwoUsersTests(Base):
    """A has a saved job, a tracked application with a round, interview prep and a made file; B is signed in and must see none of it."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.a_token = cls.site.token
        cls.b_id, cls.b_token = cls.site.new_user("b@example.com")
        conn = cls.site.db()
        db.add_job(conn, sources.Job("gh:acme:1", "greenhouse", "Acme", "Platform Engineer", "Austin, TX", "https://x/1", description="Secret role text."), 1.0)
        db.add_job(conn, sources.Job("gh:acme:2", "greenhouse", "Acme", "SRE", "Austin, TX", "https://x/2", description="Other."), 1.0)
        db.set_status_exact(conn, "gh:acme:1", "saved")
        db.set_status_exact(conn, "gh:acme:2", "applied")
        db.add_round(conn, "gh:acme:2", "Phone screen")
        db.save_tips(conn, "gh:acme:1", {"markdown": "## Private tips"}, "m", "2026-10-31")
        conn.close()
        folder = cls.site.home / "Optimized Resumes"
        folder.mkdir()
        (folder / "A_Person_resume.pdf").write_bytes(b"%PDF-1.4 a's resume")
        (cls.site.home / "reports").mkdir()
        (cls.site.home / "reports" / "2026-10-30.md").write_text("# Job matches\n\n## 90 | SRE at Acme\n- ID: `gh:acme:2`\n", encoding="utf-8")

    def as_(self, who, method, path, body=None):
        return self.call(method, path, body, cookie=self.a_token if who == "a" else self.b_token)

    def test_the_owner_sees_everything(self):
        self.assertEqual([j["id"] for j in self.as_("a", "GET", "/api/saved")[2]["jobs"]], ["gh:acme:1"])
        self.assertEqual(len(self.as_("a", "GET", "/api/tracker")[2]["applications"]), 1)
        self.assertEqual(len(self.as_("a", "GET", "/api/prep")[2]["jobs"]), 1)
        self.assertEqual(self.as_("a", "GET", "/api/reports")[2], ["2026-10-30.md"])
        self.assertEqual(self.as_("a", "GET", "/api/job/gh:acme:1")[2]["description"], "Secret role text.")

    def test_another_user_sees_none_of_it(self):
        for path in ("/api/saved", "/api/prep", "/api/trash"):
            self.assertEqual(self.as_("b", "GET", path)[2]["jobs"], [], path)
        tracker = self.as_("b", "GET", "/api/tracker")[2]
        self.assertEqual(tracker["applications"], [])
        self.assertEqual(tracker["stats"]["all"]["applications"], 0)
        self.assertEqual(self.as_("b", "GET", "/api/reports")[2], [])
        self.assertEqual(self.as_("b", "GET", "/api/report/2026-10-30.md")[0], 404)
        self.assertEqual(self.as_("b", "GET", "/api/job/gh:acme:1")[0], 404)

    def test_another_user_cannot_change_or_extend_someone_elses_jobs(self):
        self.assertEqual(self.as_("b", "POST", "/api/mark", {"id": "gh:acme:1", "status": "skipped"})[2]["updated"], 0)
        self.assertEqual(self.as_("a", "GET", "/api/job/gh:acme:1")[2]["status"], "saved")
        self.assertEqual(self.as_("b", "POST", "/api/applied-date", {"id": "gh:acme:2", "date": "2026-01-01"})[2]["updated"], 0)
        self.assertEqual(self.as_("b", "POST", "/api/round", {"action": "add", "id": "gh:acme:2"})[0], 404)
        self.assertEqual(self.as_("b", "POST", "/api/tips", {"id": "gh:acme:1"})[0], 404)     # no prep, no paid call, for a job B does not have
        self.assertEqual(self.as_("b", "POST", "/api/tailor", {"id": "gh:acme:1"})[0], 404)
        rounds = self.as_("a", "GET", "/api/tracker")[2]["applications"][0]["rounds"]
        self.assertEqual([r["kind"] for r in rounds], ["Phone screen"])
        rid = rounds[0]["id"]
        self.assertEqual(self.as_("b", "POST", "/api/round", {"action": "delete", "round_id": rid})[0], 404)
        self.assertEqual(self.as_("b", "POST", "/api/round", {"action": "update", "round_id": rid, "kind": "Hacked"})[0], 404)
        self.assertEqual(self.as_("a", "GET", "/api/tracker")[2]["applications"][0]["rounds"][0]["kind"], "Phone screen")

    def test_files_are_only_downloaded_by_their_owner_and_only_from_their_own_folder(self):
        status, head, body = self.as_("a", "GET", "/api/files/A_Person_resume.pdf")
        self.assertEqual((status, head["Content-Type"]), (200, "application/pdf"))
        self.assertIn("attachment", head["Content-Disposition"])
        self.assertEqual(self.as_("b", "GET", "/api/files/A_Person_resume.pdf")[0], 404)
        for name in ("..%2Fprofile.json", "..%5Csecret.md", "A_Person_resume.exe", "%2E%2E%2F%2E%2E%2Fweb.py", "notes.txt", "x" * 300 + ".pdf"):
            self.assertEqual(self.as_("a", "GET", "/api/files/" + name)[0], 404, name)

    def test_a_connection_asked_for_with_a_forged_user_still_cannot_read_another_users_rows(self):
        conn = self.site.db(self.b_id)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM user_jobs WHERE user_id = ?", (self.site.uid,)).fetchone()["n"], 0)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
