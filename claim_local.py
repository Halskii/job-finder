"""Give the data from before accounts existed to a real account, so it is there when you sign in.

    python claim_local.py you@gmail.com

Until you sign in, everything the command-line tools stored belongs to a built-in account (local@localhost). This renames that
account to your email, so signing in with Google (or creating a password account with the same email) lands on it, and copies
your profile, resume, reports and Optimized Resumes into your folder under data/users/. The originals stay where they are."""
from __future__ import annotations

import sys
from pathlib import Path

import auth
import db
import userdata


def claim(conn, email: str) -> tuple[int, list[str]]:
    """Rename the local account to `email` and copy the project's own files into its folder. Returns (user id, files copied)."""
    email = auth.clean_email(email)
    row = conn.execute("SELECT id FROM users WHERE lower(email) = lower(?)", (db.LOCAL_USER,)).fetchone()
    if row is None:
        raise SystemExit("There is no local account to claim. (It is made the first time the command-line tools or viewer run.)")
    if conn.execute("SELECT 1 FROM users WHERE lower(email) = ?", (email,)).fetchone():
        raise SystemExit(f"{email} already has an account, so the local data cannot be moved onto it.")
    conn.execute("UPDATE users SET email = ?, email_verified = FALSE WHERE id = ?", (email, row["id"]))
    conn.commit()
    return row["id"], userdata.adopt_project_files(row["id"])


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    conn = db.connect(user=False)
    try:
        uid, copied = claim(conn, sys.argv[1])
    finally:
        conn.close()
    print(f"Done. Your data now belongs to {sys.argv[1].strip().lower()} (account {uid}).")
    print("Copied into", userdata.home(uid), ":", ", ".join(copied) or "nothing new")
    env = Path(__file__).parent / ".env"
    if "JOBFINDER_USER=" not in (env.read_text(encoding="utf-8") if env.exists() else ""):
        with env.open("a", encoding="utf-8") as f:
            f.write(f"\n# The account the command-line tools work as (python main.py run, list, ...)\nJOBFINDER_USER={sys.argv[1].strip().lower()}\n")
        print("Added JOBFINDER_USER to .env so the command-line tools keep working on the same account.")
    print("Now run `python web.py` and sign in with this email.")


if __name__ == "__main__":
    main()
