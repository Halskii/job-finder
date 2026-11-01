"""A real web server on a test database, with one signed-in user.

    site = webtest.Site(tmp_dir)       # starts the server
    site.get_headers()                 # the cookie to send with each request
    site.stop()

The user's folder (reports, resumes) is inside tmp_dir, so tests can write files there with site.home."""
from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest import mock

import uvicorn

import auth
import db
import userdata
import viewer
import web
from tests import pgtest

PASSWORD = "a long test password"


class Site:
    def __init__(self, root: Path, email: str = "tester@example.com", schema: str | None = None):
        self.schema = schema or pgtest.new_schema()
        pgtest.connect(self.schema).close()   # makes the tables
        self._base = mock.patch.object(userdata, "BASE", Path(root) / "users")
        self._base.start()
        self._saved = (viewer.DB_ARGS, web.RESTORE)
        viewer.DB_ARGS, web.RESTORE = pgtest.args(self.schema), False
        conn = db.connect(**{**viewer.DB_ARGS, "user": False})
        try:
            self.user = auth.sign_up(conn, email, PASSWORD, "Test User")
            self.token = auth.new_session(conn, self.user["id"])
        finally:
            conn.close()
        self.uid = self.user["id"]
        self.home = userdata.home(self.uid)
        userdata.save_settings(self.uid, {**userdata.SETTINGS_DEFAULTS, "titles": ["Software Engineer"]})   # already set up: `/` is the app, not the profile page
        config = uvicorn.Config(web.app, host="127.0.0.1", port=0, log_level="error")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.time() + 15
        while not self.server.started:
            if time.time() > deadline:
                raise RuntimeError("the test server did not start")
            time.sleep(0.02)
        self.base = f"http://127.0.0.1:{self.server.servers[0].sockets[0].getsockname()[1]}"

    def db(self, user=None):
        """A connection to the test database working as the signed-in user (or as another user)."""
        return pgtest.connect(self.schema, user=self.uid if user is None else user)

    def get_headers(self, extra: dict | None = None) -> dict:
        return {"Cookie": f"{web.COOKIE}={self.token}", **(extra or {})}

    def new_user(self, email: str) -> tuple[int, str]:
        """Another account on the same database: (its id, its session token)."""
        conn = db.connect(**{**viewer.DB_ARGS, "user": False})
        try:
            user = auth.sign_up(conn, email, PASSWORD)
            return user["id"], auth.new_session(conn, user["id"])
        finally:
            conn.close()

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)
        viewer.DB_ARGS, web.RESTORE = self._saved
        self._base.stop()
