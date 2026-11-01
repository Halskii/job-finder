"""Accounts: signing up, signing in with a password or with Google, and the sessions that keep a person signed in.

Every function here takes a connection opened with db.connect(user=False) (no user, so it can see accounts and sessions but
none of anyone's job data) and raises UserError with a message fit to show on the sign-in page."""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import time

from errors import UserError

SESSION_DAYS = 30
MIN_PASSWORD, MAX_PASSWORD = 10, 128
FAIL_LIMIT, FAIL_WINDOW = 8, 15 * 60   # this many wrong passwords for one email within 15 minutes locks sign-in for that email
SCRYPT = (2 ** 14, 8, 1)               # cost, block size, parallelism
GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[A-Za-z0-9.-]{1,255}\.[A-Za-z]{2,}$")


# ---------------------------------------------------------------- passwords

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    n, r, p = SCRYPT
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    """True when the password matches the stored hash. Always does the same work, so timing does not show whether the account exists."""
    try:
        _, n, r, p, salt, digest = (stored or DUMMY_HASH).split("$")
        got = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(got, bytes.fromhex(digest)) and stored is not None
    except (ValueError, TypeError):
        return False


DUMMY_HASH = hash_password("not-a-real-password")


def check_password_rules(password) -> str:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD:
        raise UserError(f"Choose a password of at least {MIN_PASSWORD} characters.", "weak_password")
    if len(password) > MAX_PASSWORD:
        raise UserError(f"Passwords can be at most {MAX_PASSWORD} characters.", "weak_password")
    return password


def clean_email(email) -> str:
    email = (email or "").strip().lower() if isinstance(email, str) else ""
    if not EMAIL_RE.match(email):
        raise UserError("Enter a valid email address.", "invalid_email")
    return email


# ---------------------------------------------------------------- accounts

NOT_INVITED = ("This alpha is invite-only. Sign up with the email address you were invited at, or ask for an invite.", "not_invited")


def signups_open() -> bool:
    """False (the default) while only invited people may create accounts. Set SIGNUP_MODE=open to let anyone."""
    return os.environ.get("SIGNUP_MODE", "invite").strip().lower() == "open"


def is_invited(conn, email: str) -> bool:
    return conn.execute("SELECT 1 FROM invites WHERE email = ?", (email.strip().lower(),)).fetchone() is not None


def add_invites(conn, emails, note: str = "") -> list[str]:
    """Let these email addresses create an account. Returns the cleaned addresses."""
    cleaned = [clean_email(e) for e in emails]
    conn.executemany("INSERT INTO invites (email, added, note) VALUES (?, ?, ?) ON CONFLICT (email) DO NOTHING", [(e, time.time(), note) for e in cleaned])
    conn.commit()
    return cleaned


def remove_invites(conn, emails) -> int:
    n = 0
    for e in emails:
        n += conn.execute("DELETE FROM invites WHERE email = ?", (clean_email(e),)).rowcount
    conn.commit()
    return n


def list_invites(conn) -> list:
    """Every invite, with whether that person has made an account yet."""
    return conn.execute("SELECT i.email, i.added, i.note, u.id IS NOT NULL AS joined FROM invites i LEFT JOIN users u ON lower(u.email) = i.email"
                        " ORDER BY i.added, i.email").fetchall()


def public(row) -> dict:
    """The part of an account that is safe to send to the page."""
    return {"id": row["id"], "email": row["email"], "name": row["name"] or ""}


def sign_up(conn, email, password, name: str = "", invite_only: bool = False) -> dict:
    email = clean_email(email)
    check_password_rules(password)
    if invite_only and not is_invited(conn, email):
        raise UserError(*NOT_INVITED)
    name = " ".join(str(name or "").split())[:80]
    row = conn.execute("INSERT INTO users (email, password_hash, name) VALUES (?, ?, ?) ON CONFLICT (lower(email)) DO NOTHING"
                       " RETURNING id, email, name", (email, hash_password(password), name)).fetchone()
    conn.commit()
    if row is None:
        raise UserError("An account with that email already exists. Sign in instead.", "email_taken")
    return public(row)


def sign_in(conn, email, password, ip: str = "") -> dict:
    email = clean_email(email)
    now = time.time()
    recent = conn.execute("SELECT COUNT(*) AS n FROM login_failures WHERE email = ? AND at > ?", (email, now - FAIL_WINDOW)).fetchone()["n"]
    if recent >= FAIL_LIMIT:
        raise UserError("Too many wrong passwords. Wait a few minutes and try again.", "too_many_attempts")
    row = conn.execute("SELECT id, email, name, password_hash FROM users WHERE lower(email) = ?", (email,)).fetchone()
    ok = verify_password(password if isinstance(password, str) else "", row["password_hash"] if row else None)
    if not (row and ok):
        conn.execute("INSERT INTO login_failures (email, ip, at) VALUES (?, ?, ?)", (email, ip[:64], now))
        conn.execute("DELETE FROM login_failures WHERE at < ?", (now - 24 * 3600,))
        conn.commit()
        raise UserError("That email and password do not match. If you signed up with Google, use the Google button.", "bad_login")
    conn.execute("DELETE FROM login_failures WHERE email = ?", (email,))
    conn.commit()
    return public(row)


def verify_google_token(token: str, client_id: str) -> dict:
    """The verified claims of a Google ID token (signature, audience, issuer and expiry all checked)."""
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token
    try:
        claims = id_token.verify_oauth2_token(token, google_requests.Request(), client_id, clock_skew_in_seconds=10)
    except Exception:
        raise UserError("Google sign-in could not be verified. Try again.", "bad_google_token") from None
    if claims.get("iss") not in GOOGLE_ISSUERS:
        raise UserError("Google sign-in could not be verified. Try again.", "bad_google_token")
    return claims


def google_sign_in(conn, token: str, client_id: str, verify=verify_google_token, invite_only: bool = False) -> dict:
    """Sign in with a Google ID token, creating the account on the first visit. An account made earlier with the same email
    is linked; if that one was a password account whose email was never confirmed, its password is removed first, so someone
    who signed up with another person's address cannot keep access after the real owner arrives."""
    if not client_id:
        raise UserError("Google sign-in is not set up on this server.", "google_off")
    claims = verify(token, client_id)
    sub, email = claims.get("sub"), (claims.get("email") or "").strip().lower()
    if not sub or not email or claims.get("email_verified") is not True:
        raise UserError("Google did not confirm your email address, so we cannot sign you in with it.", "email_unverified")
    name = " ".join(str(claims.get("name") or "").split())[:80]
    row = conn.execute("SELECT id, email, name FROM users WHERE google_sub = ?", (sub,)).fetchone()
    if row is None:
        row = conn.execute("SELECT id, email, name, email_verified FROM users WHERE lower(email) = ?", (email,)).fetchone()
        if row is not None:
            if not row["email_verified"]:
                conn.execute("UPDATE users SET password_hash = NULL WHERE id = ?", (row["id"],))
                conn.execute("DELETE FROM sessions WHERE user_id = ?", (row["id"],))
            conn.execute("UPDATE users SET google_sub = ?, email_verified = TRUE, name = CASE WHEN name = '' THEN ? ELSE name END"
                         " WHERE id = ?", (sub, name, row["id"]))
        else:
            if invite_only and not is_invited(conn, email):   # a new account needs an invite; an existing one never does
                raise UserError(*NOT_INVITED)
            row = conn.execute("INSERT INTO users (email, google_sub, name, email_verified) VALUES (?, ?, ?, TRUE)"
                               " RETURNING id, email, name", (email, sub, name)).fetchone()
    conn.commit()
    return public(conn.execute("SELECT id, email, name FROM users WHERE id = ?", (row["id"],)).fetchone())


# ---------------------------------------------------------------- sessions

def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_session(conn, user_id: int) -> str:
    """Start a session and return its token (what goes in the cookie). Only a hash of it is stored."""
    token = secrets.token_urlsafe(32)
    now = time.time()
    conn.execute("INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                 (_hash(token), user_id, now, now + SESSION_DAYS * 86400))
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
    conn.commit()
    return token


def user_for_session(conn, token: str | None) -> dict | None:
    if not token or len(token) > 200:
        return None
    row = conn.execute("SELECT u.id, u.email, u.name FROM sessions s JOIN users u ON u.id = s.user_id"
                       " WHERE s.token_hash = ? AND s.expires_at > ?", (_hash(token), time.time())).fetchone()
    return public(row) if row else None


def end_session(conn, token: str | None) -> None:
    if token:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash(token),))
        conn.commit()
