"""Accounts, passwords, Google sign-in and sessions."""
import time
import unittest

import auth
import db
from errors import UserError
from tests import pgtest

GOOD = "correct horse battery"


def google(sub="g-1", email="gina@example.com", verified=True, name="Gina G"):
    return lambda token, client_id: {"sub": sub, "email": email, "email_verified": verified, "name": name}


class Base(unittest.TestCase):
    def setUp(self):
        self.schema = pgtest.new_schema()
        pgtest.connect(self.schema).close()   # creates the tables
        self.conn = db.connect(pgtest.url(), schema=self.schema, user=False)

    def tearDown(self):
        self.conn.close()

    def users(self):
        """Every account except the local one that connecting to a fresh database makes."""
        return self.conn.execute("SELECT * FROM users WHERE email <> ? ORDER BY id", (db.LOCAL_USER,)).fetchall()


class PasswordTests(unittest.TestCase):
    def test_hashes_differ_each_time_and_verify(self):
        a, b = auth.hash_password(GOOD), auth.hash_password(GOOD)
        self.assertNotEqual(a, b)
        self.assertNotIn(GOOD, a)
        self.assertTrue(auth.verify_password(GOOD, a))
        self.assertFalse(auth.verify_password(GOOD + "x", a))
        self.assertFalse(auth.verify_password(GOOD, None))      # an account with no password never matches
        self.assertFalse(auth.verify_password(GOOD, "garbage"))

    def test_the_rules(self):
        for bad in ("short", "x" * 129, None, 12345678901):
            with self.assertRaises(UserError) as ctx:
                auth.check_password_rules(bad)
            self.assertEqual(ctx.exception.code, "weak_password")
        self.assertEqual(auth.check_password_rules(GOOD), GOOD)

    def test_emails_are_cleaned_and_checked(self):
        self.assertEqual(auth.clean_email("  Me@Example.COM "), "me@example.com")
        for bad in ("", "nope", "a@b", "a b@c.com", None, "x@@y.com"):
            with self.assertRaises(UserError):
                auth.clean_email(bad)


class SignUpAndInTests(Base):
    def test_sign_up_then_sign_in(self):
        user = auth.sign_up(self.conn, "Me@Example.com", GOOD, "  Me   Myself ")
        self.assertEqual((user["email"], user["name"]), ("me@example.com", "Me Myself"))
        self.assertNotIn("password_hash", user)
        self.assertEqual(auth.sign_in(self.conn, "ME@example.com", GOOD)["id"], user["id"])

    def test_an_email_can_only_sign_up_once_whatever_its_case(self):
        auth.sign_up(self.conn, "me@example.com", GOOD)
        with self.assertRaises(UserError) as ctx:
            auth.sign_up(self.conn, "ME@EXAMPLE.COM", GOOD)
        self.assertEqual(ctx.exception.code, "email_taken")
        self.assertEqual(len(self.users()), 1)

    def test_a_wrong_password_and_an_unknown_email_get_the_same_answer(self):
        auth.sign_up(self.conn, "me@example.com", GOOD)
        codes = []
        for email, pw in (("me@example.com", "wrong password!"), ("nobody@example.com", GOOD)):
            with self.assertRaises(UserError) as ctx:
                auth.sign_in(self.conn, email, pw)
            codes.append((ctx.exception.code, ctx.exception.message))
        self.assertEqual(codes[0], codes[1])

    def test_too_many_wrong_passwords_lock_that_email_even_for_the_right_one(self):
        auth.sign_up(self.conn, "me@example.com", GOOD)
        for _ in range(auth.FAIL_LIMIT):
            with self.assertRaises(UserError):
                auth.sign_in(self.conn, "me@example.com", "wrong password!", "1.2.3.4")
        with self.assertRaises(UserError) as ctx:
            auth.sign_in(self.conn, "me@example.com", GOOD)
        self.assertEqual(ctx.exception.code, "too_many_attempts")
        self.conn.execute("UPDATE login_failures SET at = at - 3600")   # an hour later
        self.conn.commit()
        self.assertEqual(auth.sign_in(self.conn, "me@example.com", GOOD)["email"], "me@example.com")

    def test_a_good_sign_in_clears_the_failures(self):
        auth.sign_up(self.conn, "me@example.com", GOOD)
        for _ in range(3):
            with self.assertRaises(UserError):
                auth.sign_in(self.conn, "me@example.com", "wrong password!")
        auth.sign_in(self.conn, "me@example.com", GOOD)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) AS n FROM login_failures").fetchone()["n"], 0)

    def test_the_local_account_and_google_only_accounts_cannot_sign_in_with_a_password(self):
        db.connect(pgtest.url(), schema=self.schema).close()   # makes the local account
        for email in (db.LOCAL_USER, "x@example.com"):
            with self.assertRaises(UserError):
                auth.sign_in(self.conn, email, GOOD)


class GoogleTests(Base):
    def test_first_visit_creates_a_verified_account_and_the_second_finds_it(self):
        a = auth.google_sign_in(self.conn, "tok", "client", google())
        b = auth.google_sign_in(self.conn, "tok", "client", google(email="renamed@example.com"))   # found by Google's id, not the email
        self.assertEqual(a["id"], b["id"])
        self.assertEqual((a["email"], a["name"]), ("gina@example.com", "Gina G"))
        self.assertTrue(self.users()[0]["email_verified"])
        self.assertEqual(len(self.users()), 1)

    def test_an_unverified_google_email_is_refused(self):
        for claims in (google(verified=False), google(email=""), google(sub="")):
            with self.assertRaises(UserError):
                auth.google_sign_in(self.conn, "tok", "client", claims)
        self.assertEqual(self.users(), [])

    def test_without_a_client_id_google_is_off(self):
        with self.assertRaises(UserError) as ctx:
            auth.google_sign_in(self.conn, "tok", "", google())
        self.assertEqual(ctx.exception.code, "google_off")

    def test_a_password_account_with_the_same_email_is_linked_and_its_password_removed(self):
        mine = auth.sign_up(self.conn, "gina@example.com", GOOD)   # never confirmed: anyone could have typed this address
        token = auth.new_session(self.conn, mine["id"])
        who = auth.google_sign_in(self.conn, "tok", "client", google())
        self.assertEqual(who["id"], mine["id"])
        row = self.users()[0]
        self.assertIsNone(row["password_hash"])
        self.assertEqual((row["google_sub"], row["email_verified"]), ("g-1", True))
        self.assertIsNone(auth.user_for_session(self.conn, token))     # the session the sign-up made is gone too
        with self.assertRaises(UserError):
            auth.sign_in(self.conn, "gina@example.com", GOOD)

    def test_a_confirmed_account_keeps_its_password_when_google_is_added(self):
        auth.google_sign_in(self.conn, "tok", "client", google(sub="g-1"))
        self.conn.execute("UPDATE users SET password_hash = ?", (auth.hash_password(GOOD),))
        self.conn.commit()
        auth.google_sign_in(self.conn, "tok", "client", google(sub="g-2"))   # a second Google identity with the same confirmed email
        self.assertEqual(auth.sign_in(self.conn, "gina@example.com", GOOD)["email"], "gina@example.com")

    def test_a_bad_token_is_refused_without_reaching_the_network(self):
        for token in ("", "not.a.jwt", "x" * 50):
            with self.assertRaises(UserError) as ctx:
                auth.verify_google_token(token, "client")
            self.assertEqual(ctx.exception.code, "bad_google_token")


class SessionTests(Base):
    def setUp(self):
        super().setUp()
        self.user = auth.sign_up(self.conn, "me@example.com", GOOD)

    def test_a_session_finds_its_user_and_ends(self):
        token = auth.new_session(self.conn, self.user["id"])
        self.assertEqual(auth.user_for_session(self.conn, token)["email"], "me@example.com")
        self.assertNotIn(token, str(self.conn.execute("SELECT * FROM sessions").fetchall()))   # only a hash is stored
        auth.end_session(self.conn, token)
        self.assertIsNone(auth.user_for_session(self.conn, token))

    def test_tokens_are_unique_and_guessing_fails(self):
        a, b = auth.new_session(self.conn, self.user["id"]), auth.new_session(self.conn, self.user["id"])
        self.assertNotEqual(a, b)
        for token in (None, "", "guess", a[:-1] + ("A" if a[-1] != "A" else "B"), "x" * 500):
            self.assertIsNone(auth.user_for_session(self.conn, token))

    def test_an_expired_session_does_not_work(self):
        token = auth.new_session(self.conn, self.user["id"])
        self.conn.execute("UPDATE sessions SET expires_at = ?", (time.time() - 1,))
        self.conn.commit()
        self.assertIsNone(auth.user_for_session(self.conn, token))

    def test_deleting_the_account_ends_its_sessions(self):
        token = auth.new_session(self.conn, self.user["id"])
        self.conn.execute("DELETE FROM users WHERE id = ?", (self.user["id"],))
        self.conn.commit()
        self.assertIsNone(auth.user_for_session(self.conn, token))

    def test_an_anonymous_connection_sees_no_job_data(self):
        owner = pgtest.connect(self.schema, user="me@example.com")
        import sources
        db.add_job(owner, sources.Job("gh:a:1", "greenhouse", "A", "Dev", "x", "https://x", description="d"), 1.0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) AS n FROM user_jobs").fetchone()["n"], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"], 0)


if __name__ == "__main__":
    unittest.main()
