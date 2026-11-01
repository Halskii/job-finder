"""Invite-only sign-ups: who may create an account, and that people who already have one are never affected."""
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest import mock

import argparse

import auth
import db
import main
import web
from errors import UserError
from tests import pgtest, webtest
from tests.test_auth import GOOD, google


class InviteTests(unittest.TestCase):
    def setUp(self):
        self.schema = pgtest.new_schema()
        pgtest.connect(self.schema).close()
        self.conn = db.connect(pgtest.url(), schema=self.schema, user=False)

    def tearDown(self):
        self.conn.close()

    def test_a_password_sign_up_needs_an_invite_for_that_email(self):
        with self.assertRaises(UserError) as ctx:
            auth.sign_up(self.conn, "new@example.com", GOOD, invite_only=True)
        self.assertEqual(ctx.exception.code, "not_invited")
        self.assertIn("invite-only", ctx.exception.message)
        auth.add_invites(self.conn, ["New@Example.com "])                                    # case and spaces do not matter
        self.assertEqual(auth.sign_up(self.conn, "new@example.com", GOOD, invite_only=True)["email"], "new@example.com")

    def test_a_google_sign_in_that_would_make_a_new_account_needs_an_invite(self):
        with self.assertRaises(UserError) as ctx:
            auth.google_sign_in(self.conn, "tok", "client", google(), invite_only=True)
        self.assertEqual(ctx.exception.code, "not_invited")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) AS n FROM users WHERE email = 'gina@example.com'").fetchone()["n"], 0)
        auth.add_invites(self.conn, ["gina@example.com"])
        self.assertEqual(auth.google_sign_in(self.conn, "tok", "client", google(), invite_only=True)["email"], "gina@example.com")

    def test_someone_who_already_has_an_account_never_needs_an_invite_even_after_it_is_removed(self):
        auth.add_invites(self.conn, ["gina@example.com"])
        first = auth.google_sign_in(self.conn, "tok", "client", google(), invite_only=True)
        self.assertEqual(auth.remove_invites(self.conn, ["gina@example.com"]), 1)
        again = auth.google_sign_in(self.conn, "tok", "client", google(), invite_only=True)      # found by Google's id
        self.assertEqual(first["id"], again["id"])
        auth.sign_up(self.conn, "pw@example.com", GOOD)                                          # an account made while sign-ups were open
        self.assertEqual(auth.sign_in(self.conn, "pw@example.com", GOOD)["email"], "pw@example.com")
        linked = auth.google_sign_in(self.conn, "tok", "client", google(sub="g-9", email="pw@example.com"), invite_only=True)
        self.assertEqual(linked["email"], "pw@example.com")                                      # linking to an existing account is fine

    def test_the_list_says_who_has_joined(self):
        auth.add_invites(self.conn, ["a@example.com", "b@example.com"], "friend")
        auth.sign_up(self.conn, "a@example.com", GOOD, invite_only=True)
        rows = {r["email"]: r["joined"] for r in auth.list_invites(self.conn)}
        self.assertEqual(rows, {"a@example.com": True, "b@example.com": False})

    def test_a_bad_address_is_refused_when_inviting(self):
        with self.assertRaises(UserError):
            auth.add_invites(self.conn, ["not an email"])

    def test_the_default_is_invite_only_and_open_is_a_choice(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SIGNUP_MODE", None)
            self.assertFalse(auth.signups_open())
        for value, expected in (("open", True), ("OPEN", True), ("invite", False), ("anything", False), ("", False)):
            with mock.patch.dict(os.environ, {"SIGNUP_MODE": value}):
                self.assertEqual(auth.signups_open(), expected, value)


class InviteOverHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.site = webtest.Site(cls.tmp.name)
        cls.base = cls.site.base

    @classmethod
    def tearDownClass(cls):
        cls.site.stop()
        cls.tmp.cleanup()

    def post(self, path, body):
        req = urllib.request.Request(self.base + path, method="POST", headers={"Content-Type": "application/json"}, data=json.dumps(body).encode())
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_in_invite_mode_the_sign_up_page_refuses_strangers_and_accepts_the_invited(self):
        with mock.patch.dict(os.environ, {"SIGNUP_MODE": "invite"}):
            status, body = self.post("/auth/signup", {"email": "stranger@example.com", "password": GOOD})
            self.assertEqual((status, body["code"]), (403, "not_invited"))
            self.assertTrue(body["error"].endswith("."))
            conn = db.connect(**{**web.viewer.DB_ARGS, "user": False})
            auth.add_invites(conn, ["friend@example.com"])
            conn.close()
            self.assertEqual(self.post("/auth/signup", {"email": "friend@example.com", "password": GOOD})[0], 200)
            claims = {"sub": "g-5", "email": "uninvited@example.com", "email_verified": True, "name": "U"}
            with mock.patch.dict(os.environ, {"GOOGLE_CLIENT_ID": "cid"}), mock.patch.object(auth, "verify_google_token", return_value=claims):
                self.assertEqual(self.post("/auth/google", {"credential": "x"})[1]["code"], "not_invited")

    def test_people_who_can_already_sign_in_still_can(self):
        with mock.patch.dict(os.environ, {"SIGNUP_MODE": "invite"}):
            self.assertEqual(self.post("/auth/login", {"email": "tester@example.com", "password": webtest.PASSWORD})[0], 200)

    def test_open_mode_lets_anyone_in(self):
        with mock.patch.dict(os.environ, {"SIGNUP_MODE": "open"}):
            self.assertEqual(self.post("/auth/signup", {"email": "anyone@example.com", "password": GOOD})[0], 200)


class InviteCommandTests(unittest.TestCase):
    def test_add_list_and_remove(self):
        conn = pgtest.connect()
        out = []
        with mock.patch.object(main, "log", out.append):
            main.cmd_invite(argparse.Namespace(action="add", emails=["a@example.com", "B@example.com"], note="two friends"), {}, {}, conn)
            main.cmd_invite(argparse.Namespace(action="list", emails=[], note=None), {}, {}, conn)
            main.cmd_invite(argparse.Namespace(action="remove", emails=["a@example.com"], note=None), {}, {}, conn)
            main.cmd_invite(argparse.Namespace(action="list", emails=[], note=None), {}, {}, conn)
        text = "\n".join(out)
        self.assertIn("Invited 2: a@example.com, b@example.com", text)
        self.assertIn("2 invite(s), 0 signed up.", text)
        self.assertIn("Removed 1 invite(s)", text)
        self.assertIn("1 invite(s), 0 signed up.", text)


if __name__ == "__main__":
    unittest.main()


class UsageCommandTests(unittest.TestCase):
    def test_it_lists_everyones_calls_and_each_persons_own(self):
        schema = pgtest.new_schema()
        anon = pgtest.connect(schema, user=False)
        a = auth.sign_up(anon, "a@example.com", GOOD)["id"]
        b = auth.sign_up(anon, "b@example.com", GOOD)["id"]
        for uid, calls in ((a, 3), (b, 1)):
            c = pgtest.connect(schema, user=uid)
            db.add_usage(c, "score", calls, 100 * calls, 20 * calls)
            c.close()
        out = []
        real = db.connect   # the command opens its own connections; send them to this test's database
        with mock.patch.object(main.db, "connect", lambda *x, **k: real(pgtest.url(), schema=schema, user=k.get("user"))), mock.patch.object(main, "log", out.append):
            main.cmd_usage(argparse.Namespace(days=7), {}, {}, None)
        text = "\n".join(out)
        self.assertRegex(text, r"score\s+4 calls")                              # the two together
        self.assertIn("a@example.com:", text)
        self.assertIn("3 calls", text)
        self.assertIn("b@example.com:", text)
