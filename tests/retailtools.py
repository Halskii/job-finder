"""Stand-ins for the tests of the retailer readers: an HTTP answer, and a session that answers from a function and remembers what was asked.

    session = Session(lambda method, url, kw: Resp(page_text))
    with mock.patch.object(sources, "_session", lambda: session):
        jobs = reader.fetch(...)
    session.asked        # [("GET", url, kwargs), ...]
"""
import json


class Resp:
    def __init__(self, body="", status=200, headers=None, url=""):
        self.text = body if isinstance(body, str) else json.dumps(body)
        self.content = self.text.encode("utf-8")
        self.status_code, self.headers, self.url = status, headers or {}, url

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return json.loads(self.text)


class Session:
    def __init__(self, answer):
        self.answer, self.asked, self.headers, self.cookies = answer, [], {}, {}

    def request(self, method, url, **kw):
        self.asked.append((method, url, kw))
        return self.answer(method, url, kw)

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def post(self, url, **kw):
        return self.request("POST", url, **kw)
