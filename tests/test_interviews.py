"""Offline tests for interview rounds (db.py) and interview prep (tips.py)."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import db
import sources
import tailor
import tips

ANSWER = "## Summary\nReports are thin.\n## Interview process\n### Recruiter call (Reported)\n30 minutes.\n## Topics to study\n- **Java (High priority)**: reported."


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "profile.json").write_text(json.dumps({"name": "James Hall", "strong_skills": ["Java"]}), encoding="utf-8")
        self.conn = db.connect(str(self.root / "jobs.db"))
        db.add_job(self.conn, sources.Job("gh:acme:1", "greenhouse", "Acme", "Software Engineer II", "Austin, TX",
                                          "https://example.com/1", description="Build services in Java."), 1.0)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()


class RoundTests(Base):
    def test_rounds_are_numbered_ordered_and_editable(self):
        a = db.add_round(self.conn, "gh:acme:1")
        b = db.add_round(self.conn, "gh:acme:1", "System design", "2026-11-05", "waiting", "Went well")
        rounds = db.rounds_for(self.conn, "gh:acme:1")
        self.assertEqual([(r["kind"], r["date"], r["outcome"]) for r in rounds],
                         [("Round 1", None, "scheduled"), ("System design", "2026-11-05", "waiting")])
        self.assertEqual(db.update_round(self.conn, a, "Phone screen", "2026-11-01", "passed", ""), 1)
        self.assertEqual(db.rounds_by_job(self.conn)["gh:acme:1"][0]["kind"], "Phone screen")
        self.assertEqual(db.delete_round(self.conn, b), 1)
        self.assertEqual(len(db.rounds_for(self.conn, "gh:acme:1")), 1)
        self.assertEqual(db.delete_round(self.conn, 999), 0)

    def test_an_older_database_gets_the_new_tables(self):
        import sqlite3
        c = sqlite3.connect(str(self.root / "old.db"))
        c.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, source TEXT, company TEXT, title TEXT, location TEXT, url TEXT,"
                  " posted TEXT, pay_min INTEGER, pay_max INTEGER, description TEXT, quick_score REAL, score INTEGER,"
                  " verdict TEXT, analysis TEXT, status TEXT DEFAULT 'new', first_seen REAL, notes TEXT)")
        c.commit()
        c.close()
        conn = db.connect(str(self.root / "old.db"))
        self.assertEqual(db.rounds_for(conn, "x"), [])
        self.assertIsNone(db.get_tips(conn, "x"))
        self.assertEqual(db.jobs_with_tips(conn), [])
        conn.close()


# ---- a fake streaming client: each turn is (events, stop_reason, web searches run)

def ev(kind, **kw):
    return SimpleNamespace(type=kind, **kw)


class FakeStream:
    def __init__(self, events, final):
        self.events, self.final = events, final

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter(self.events)

    def get_final_message(self):
        return self.final


def text(i, *chunks, cited=()):
    out = [ev("content_block_start", index=i, content_block=SimpleNamespace(type="text", text=""))]
    out += [ev("content_block_delta", index=i, delta=SimpleNamespace(type="text_delta", text=c)) for c in chunks]
    out += [ev("content_block_delta", index=i, delta=SimpleNamespace(type="citations_delta", citation=SimpleNamespace(url=u, title=t))) for u, t in cited]
    return out + [ev("content_block_stop", index=i)]


def search(i, query, results):
    return [ev("content_block_start", index=i, content_block=SimpleNamespace(type="server_tool_use")),
            ev("content_block_delta", index=i, delta=SimpleNamespace(type="input_json_delta", partial_json=json.dumps({"query": query}))),
            ev("content_block_stop", index=i),
            ev("content_block_start", index=i + 1, content_block=SimpleNamespace(
                type="web_search_tool_result", content=[SimpleNamespace(url=u, title=t) for u, t in results])),
            ev("content_block_stop", index=i + 1)]


def scripted(*turns):
    calls, it = [], iter(turns)

    def stream(**kw):
        calls.append(kw)
        events, stop, searches = next(it)
        final = SimpleNamespace(stop_reason=stop, content=["<paused turn>"],
                                usage=SimpleNamespace(server_tool_use=SimpleNamespace(web_search_requests=searches)))
        return FakeStream(events, final)
    return SimpleNamespace(messages=SimpleNamespace(stream=stream)), calls


GD = ("https://glassdoor.example/a", "Real title")
ONE_SEARCH = (text(0, "Let me search {for} it.") + search(1, "acme interview", [GD, ("https://other.example/b", "Other")])
              + text(3, ANSWER[:30], ANSWER[30:], cited=[GD, ("https://evil.example/x", "Not searched")]), "end_turn", 1)


class TipsTests(Base):
    def run_tips(self, client, **kw):
        with mock.patch.object(tailor, "ROOT", self.root):
            return list(tips.stream_tips(self.conn, "gh:acme:1", client=client, **kw))

    def test_the_answer_streams_after_the_search_and_is_saved(self):
        client, calls = scripted(ONE_SEARCH)
        events = self.run_tips(client)
        self.assertEqual(events[0]["type"], "start")
        shown = "".join(e["text"] for e in events if e["type"] == "text")
        self.assertEqual(shown, ANSWER)                         # the "Let me search" chatter before the search was never shown
        self.assertIn("Searching: acme interview", [e["text"] for e in events if e["type"] == "status"])
        done = events[-1]["result"]
        self.assertEqual(done["content"]["markdown"], ANSWER)
        self.assertEqual(done["content"]["sources"], [{"title": "Real title", "url": "https://glassdoor.example/a"}])   # cited and really found
        self.assertEqual((done["content"]["searches"], done["cached"], done["model"]), (1, False, "claude-haiku-5-5"))
        self.assertIn("not your actual interview plan", done["disclaimer"])
        self.assertEqual(calls[0]["tools"][0]["name"], "web_search")
        self.assertNotIn("output_config", calls[0])             # Haiku is not sent an effort setting
        self.assertEqual(calls[0]["thinking"], {"type": "disabled"})   # thinking only delayed the first word

    def test_with_nothing_cited_the_sources_are_the_pages_the_search_returned(self):
        client, _ = scripted((text(0, "x") + search(1, "q", [GD, ("javascript:alert(1)", "Bad")]) + text(3, ANSWER), "end_turn", 1))
        events = self.run_tips(client)
        self.assertEqual(events[-1]["result"]["content"]["sources"], [{"title": "Real title", "url": "https://glassdoor.example/a"}])

    def test_a_saved_answer_comes_back_at_once_without_a_call(self):
        client, calls = scripted(ONE_SEARCH)
        self.run_tips(client)
        again = self.run_tips(client)
        self.assertEqual([e["type"] for e in again], ["start", "done"])
        self.assertTrue(again[-1]["result"]["cached"])
        self.assertEqual(len(calls), 1)
        client2, calls2 = scripted(ONE_SEARCH)
        self.run_tips(client2, refresh=True)
        self.assertEqual(len(calls2), 1)                        # refresh asks again
        self.assertEqual([r["job_id"] for r in self.conn.execute("SELECT job_id FROM interview_tips")], ["gh:acme:1"])

    def test_a_second_search_clears_what_was_shown(self):
        client, _ = scripted((text(0, "x") + search(1, "q1", [GD]) + text(3, "## Summary\nfirst draft") + search(4, "q2", [GD])
                              + text(6, ANSWER), "end_turn", 2))
        events = self.run_tips(client)
        kinds = [e["type"] for e in events]
        self.assertIn("reset", kinds)
        self.assertEqual(events[-1]["result"]["content"]["markdown"], ANSWER)

    def test_a_paused_turn_is_continued_and_searches_are_added_up(self):
        client, calls = scripted((text(0, "x") + search(1, "q1", [GD]), "pause_turn", 1), (text(3, ANSWER), "end_turn", 2))
        events = self.run_tips(client)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["messages"][-1], {"role": "assistant", "content": ["<paused turn>"]})
        self.assertEqual(events[-1]["result"]["content"]["searches"], 3)

    def test_with_search_turned_off_everything_streams_and_no_tool_is_sent(self):
        (self.root / "config.yaml").write_text("tips_web_search: false\n", encoding="utf-8")
        client, calls = scripted((text(0, ANSWER), "end_turn", 0))
        events = self.run_tips(client)
        self.assertNotIn("tools", calls[0])
        self.assertEqual("".join(e["text"] for e in events if e["type"] == "text"), ANSWER)

    def test_the_prompt_has_the_posting_and_the_rounds_so_far(self):
        db.add_round(self.conn, "gh:acme:1", "Phone screen", "2026-11-01", "passed", "Asked about Kubernetes")
        client, calls = scripted(ONE_SEARCH)
        self.run_tips(client)
        sent = calls[0]["messages"][0]["content"]
        self.assertIn("Build services in Java.", sent)
        self.assertIn("Phone screen (2026-11-01, passed): Asked about Kubernetes", sent)
        self.assertIn("## Topics to study", calls[0]["system"])

    def test_refusal_a_cut_off_answer_a_bad_shape_and_an_unknown_job_save_nothing(self):
        for stop, bad_text, error in (("refusal", ANSWER, RuntimeError), ("max_tokens", ANSWER, RuntimeError), ("end_turn", "no headings", ValueError)):
            client, _ = scripted((text(0, "x") + search(1, "q", [GD]) + text(3, bad_text), stop, 1))
            with self.assertRaises(error):
                self.run_tips(client)
        self.assertIsNone(db.get_tips(self.conn, "gh:acme:1"))
        with self.assertRaises(LookupError):
            with mock.patch.object(tailor, "ROOT", self.root):
                list(tips.stream_tips(self.conn, "nope", client=scripted()[0]))

    def test_an_answer_saved_by_the_first_version_still_displays(self):
        old = {"basis": "company", "summary": "Old summary.", "stages": [{"name": "Phone", "format": "30 min.", "what_to_expect": "Logistics.", "confidence": "reported"}],
               "topics": ["Java"], "prep": ["Review."], "questions_to_ask": ["Team size?"], "sources": [{"title": "t", "url": "https://x.example"}]}
        db.save_tips(self.conn, "gh:acme:1", old, "claude-sonnet-5-5", "2026-10-31")
        got = tips.cached(self.conn, "gh:acme:1")
        md = got["content"]["markdown"]
        self.assertIn("### Phone (Reported)", md)
        self.assertIn("## Topics to study\n- Java", md)
        self.assertEqual(got["content"]["sources"], old["sources"])

    def test_jobs_with_prep_are_listed_newest_first(self):
        db.add_job(self.conn, sources.Job("gh:b:2", "greenhouse", "B", "Dev", "Austin", "https://example.com/2", description="d"), 1.0)
        db.save_tips(self.conn, "gh:acme:1", {"markdown": "## x"}, "m", "2026-10-30")
        db.save_tips(self.conn, "gh:b:2", {"markdown": "## y"}, "m", "2026-10-31")
        self.assertEqual([r["id"] for r in db.jobs_with_tips(self.conn)], ["gh:b:2", "gh:acme:1"])


if __name__ == "__main__":
    unittest.main()
