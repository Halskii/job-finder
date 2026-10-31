"""Workable, Recruitee, BambooHR, Rippling, Gem, Personio and the aggregator feeds, parsed from sample responses. No network."""
import argparse
import io
import tempfile
from contextlib import redirect_stdout
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import ats_sources as ats
import db
import feed_sources as fs
import main
import sources


class FakeResp:
    def __init__(self, data=None, content=b"", headers=None):
        self._data, self.content, self.headers, self.text = data, content, headers or {"content-type": "application/json"}, ""

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def session(post=None, get=None):
    return lambda: SimpleNamespace(post=post, get=get)


class AtsTests(unittest.TestCase):
    def test_workable_pages_through_the_list_then_fills_the_description(self):
        pages = [{"results": [{"shortcode": "A1", "title": "Software Engineer", "remote": True, "published": "2026-10-01T00:00:00.000Z",
                               "location": {"city": "Austin", "region": "Texas", "country": "United States"}}], "nextPage": "tok"},
                 {"results": [{"shortcode": "B2", "title": "Chef", "location": {"city": "", "country": "France"}}]}]
        bodies = []

        def post(url, json, timeout):
            bodies.append(json)
            return FakeResp(pages[len(bodies) - 1])

        with mock.patch.object(ats, "_session", session(post=post)):
            jobs = ats.fetch_workable("acme")
        self.assertEqual([j.id for j in jobs], ["wk:acme:A1", "wk:acme:B2"])
        self.assertEqual(jobs[0].location, "Austin, Texas, United States, Remote")
        self.assertEqual(jobs[0].posted, "2026-10-01")
        self.assertEqual(bodies[1].get("token"), "tok")
        with mock.patch.object(ats, "_get", lambda url, **k: {"description": "<p>Build</p>", "requirements": "Python", "benefits": "$100,000 - $140,000"}):
            sources.fill_detail(jobs[0])
        self.assertIn("Build", jobs[0].description)
        self.assertEqual(jobs[0].pay_max, 140000)

    def test_recruitee_reads_salary_location_and_description(self):
        offer = {"id": 7, "title": "Backend Developer", "company_name": "Bunq", "location": "Austin, Texas, United States", "remote": True,
                 "careers_url": "https://careers.x/o/7", "published_at": "2026-10-02 10:00:00 UTC", "description": "<p>Go</p>",
                 "requirements": "<ul><li>SQL</li></ul>", "salary": {"min": 90000, "max": 120000, "period": "year"}, "status": "published"}
        with mock.patch.object(ats, "_get", lambda url, **k: {"offers": [offer, {**offer, "id": 8, "status": "closed"}]}):
            jobs = ats.fetch_recruitee("bunq")
        self.assertEqual(len(jobs), 1)
        j = jobs[0]
        self.assertEqual((j.id, j.company, j.posted, j.pay_min, j.pay_max), ("rc:bunq:7", "Bunq", "2026-10-02", 90000, 120000))
        self.assertTrue(j.location.endswith("Remote"))
        self.assertIn("SQL", j.description)

    def test_bamboohr_rejects_a_web_page_and_reads_a_board(self):
        html = FakeResp(None, headers={"content-type": "text/html"})
        with mock.patch.object(ats, "_session", session(get=lambda *a, **k: html)):
            with self.assertRaises(ValueError):
                ats.fetch_bamboohr("notacustomer")
        board = FakeResp({"result": [{"id": "5", "jobOpeningName": "Developer", "isRemote": False,
                                      "atsLocation": {"city": "Austin", "state": "Texas", "country": "United States"}}]})
        with mock.patch.object(ats, "_session", session(get=lambda *a, **k: board)):
            jobs = ats.fetch_bamboohr("acme")
        self.assertEqual((jobs[0].id, jobs[0].location), ("bb:acme:5", "Austin, Texas, United States"))
        with mock.patch.object(ats, "_get", lambda url, **k: {"result": {"jobOpening": {"description": "<b>Code</b>", "datePosted": "2026-10-05"}}}):
            sources.fill_detail(jobs[0])
        self.assertEqual((jobs[0].description, jobs[0].posted), ("**Code**", "2026-10-05"))

    def test_rippling_pages_and_detail(self):
        data = [{"items": [{"id": "u1", "name": "Engineer", "url": "https://ats.rippling.com/x/jobs/u1",
                            "locations": [{"name": "New York, NY"}, {"name": "Austin, TX"}]}], "totalPages": 2},
                {"items": [{"id": "u2", "name": "Designer", "url": "https://ats.rippling.com/x/jobs/u2", "locations": []}], "totalPages": 2}]
        n = []

        def get(url, **k):
            n.append(k["params"]["page"])
            return data[len(n) - 1]

        with mock.patch.object(ats, "_get", get):
            jobs = ats.fetch_rippling("x")
        self.assertEqual((n, [j.location for j in jobs]), ([0, 1], ["New York, NY; Austin, TX", ""]))
        with mock.patch.object(ats, "_get", lambda url, **k: {"description": {"company": "<p>About</p>", "role": "<p>Do</p>"}, "createdOn": "2026-03-31T00:00:00Z"}):
            sources.fill_detail(jobs[0])
        self.assertIn("Do", jobs[0].description)
        self.assertEqual(jobs[0].posted, "2026-03-31")

    def test_gem_keeps_the_description_and_marks_remote(self):
        data = [{"id": "9", "title": "Engineer", "content_plain": "Pay $120,000 - $150,000", "absolute_url": "https://jobs.gem.com/x/9",
                 "location": {"name": "San Francisco"}, "location_type": "remote", "first_published_at": "2026-10-01T00:00:00Z"}]
        with mock.patch.object(ats, "_get", lambda url, **k: data):
            j = ats.fetch_gem("x")[0]
        self.assertEqual((j.id, j.location, j.pay_min, j.posted), ("gm:x:9", "San Francisco, Remote", 120000, "2026-10-01"))

    def test_personio_xml(self):
        xml = (b'<workzag-jobs><position><id>12</id><subcompany>Acme GmbH</subcompany><office>Austin, TX</office><name>Engineer</name>'
               b'<jobDescriptions><jobDescription><name>Role</name><value>&lt;p&gt;Build things&lt;/p&gt;</value></jobDescription></jobDescriptions>'
               b'<createdAt>2026-10-03T08:00:00+00:00</createdAt></position></workzag-jobs>')
        with mock.patch.object(ats, "_session", session(get=lambda *a, **k: FakeResp(content=xml))):
            j = ats.fetch_personio("acme")[0]
        self.assertEqual((j.id, j.company, j.location, j.posted), ("pn:acme:12", "Acme GmbH", "Austin, TX", "2026-10-03"))
        self.assertIn("Build things", j.description)

    def test_the_new_fetchers_are_registered_for_discovery_and_the_run(self):
        import discovery
        names = [n for n, _ in discovery.SYSTEMS]
        for system in ("workable", "recruitee", "bamboohr", "rippling", "gem", "personio"):
            self.assertIn(system, names)
        merged = discovery.merge_sources({"gem": ["a"]}, {"gem": ["A", "b"], "workable": ["w"]})
        self.assertEqual(merged, {"gem": ["a", "b"], "workable": ["w"]})


class FeedTests(unittest.TestCase):
    def test_remoteok_skips_the_legal_notice_and_zero_pay(self):
        data = [{"legal": "link back"}, {"id": "1", "position": "Software Engineer", "company": "Acme", "epoch": "1793318417",
                                          "url": "https://remoteok.com/remote-jobs/1", "description": "<p>x</p>", "salary_min": "0", "salary_max": "0"},
                {"id": "2", "position": "Dev", "company": "B", "salary_min": "100000", "salary_max": "150000", "location": "USA"}]
        with mock.patch.object(fs.sources, "_session", session(get=lambda *a, **k: FakeResp(data))):
            jobs = fs.fetch_remoteok()
        self.assertEqual([j.id for j in jobs], ["ro:1", "ro:2"])
        self.assertEqual((jobs[0].location, jobs[0].pay_min, jobs[1].pay_min, jobs[1].location), ("Remote", None, 100000, "USA"))
        self.assertEqual(jobs[0].posted, "2026-10-30")

    def test_weworkremotely_splits_company_and_title(self):
        xml = ("<rss><channel><item><title>Acme: Python Developer</title><region>USA Only</region>"
               "<description>&lt;p&gt;Great $110,000 - $130,000 job&lt;/p&gt;</description><pubDate>Wed, 28 Oct 2026 10:00:00 +0000</pubDate>"
               "<link>https://weworkremotely.com/remote-jobs/acme-python-developer</link></item></channel></rss>")
        j = fs.parse_wwr(xml)[0]
        self.assertEqual((j.company, j.title, j.location, j.posted, j.pay_min), ("Acme", "Python Developer", "USA Only", "2026-10-28", 110000))
        self.assertEqual(j.id, "wwr:acme-python-developer")

    def test_jobicy_and_workingnomads_and_himalayas(self):
        jobicy = {"jobs": [{"id": 5, "url": "https://jobicy.com/jobs/5", "jobTitle": "Full Stack &amp; Python", "companyName": "C",
                            "jobGeo": "USA", "jobDescription": "<p>d</p>", "pubDate": "2026-10-25T04:10:03+00:00"}]}
        with mock.patch.object(fs, "_get", lambda url, **k: jobicy):
            jobs = fs.fetch_jobicy(["python", "java"])
        self.assertEqual([(j.id, j.title) for j in jobs], [("jb:5", "Full Stack & Python")])          # same job from two searches kept once
        nomads = [{"url": "https://www.workingnomads.com/job/go/77/", "title": "Dev", "company_name": "D", "location": "USA", "pub_date": "2026-10-29T09:50:51-04:00"}]
        with mock.patch.object(fs, "_get", lambda url, **k: nomads):
            self.assertEqual(fs.fetch_workingnomads()[0].id, "wn:77")
        him = {"jobs": [{"title": "SE", "companyName": "H", "companySlug": "h", "guid": "https://himalayas.app/companies/h/jobs/se",
                         "locationRestrictions": ["United States"], "minSalary": 100000, "maxSalary": 130000, "salaryPeriod": "annual",
                         "pubDate": 1793404180, "description": "<p>d</p>"}]}
        with mock.patch.object(fs, "_get", lambda url, **k: him):
            j = fs.fetch_himalayas(["software engineer"], 2)[0]
        self.assertEqual((j.location, j.pay_min, j.url), ("United States", 100000, "https://himalayas.app/companies/h/jobs/se"))

    def test_hacker_news_comments_become_jobs_and_replies_are_ignored(self):
        def comment(i, text, parent="500"):
            return {"objectID": str(i), "parent_id": int(parent), "comment_text": text, "created_at": "2026-10-12T10:00:00Z"}

        top = comment(1, "Acme (https://acme.com) | Software Engineer | Austin, TX | REMOTE<p>We build things. Pay $130,000 - $170,000.")
        reply = comment(2, "Is this still open?<p>Thanks", parent="1")
        thanks = comment(3, "thanks!")
        pages = {"search_by_date": [{"hits": [{"objectID": "500", "title": "Ask HN: Who is hiring? (October 2026)"}]},
                                    {"hits": [top, reply, thanks], "nbPages": 1}]}

        def get(url, **k):
            return pages["search_by_date"].pop(0)

        with mock.patch.object(fs, "_get", get):
            jobs = fs.fetch_hn_hiring()
        self.assertEqual(len(jobs), 1)
        j = jobs[0]
        self.assertEqual((j.id, j.company, j.pay_min), ("hn:1", "Acme", 130000))
        self.assertIn("Software Engineer", j.title)
        self.assertEqual(j.url, "https://news.ycombinator.com/item?id=1")

    def test_adzuna_ignores_predicted_pay_and_keeps_adzuna_links(self):
        rows = [{"id": 11, "title": "Software Engineer I", "company": {"display_name": "Acme"}, "location": {"display_name": "Austin, Texas"},
                 "redirect_url": "https://www.adzuna.com/land/ad/11", "created": "2026-10-20T10:00:00Z", "description": "Build <b>things</b>",
                 "salary_min": 95000, "salary_max": 120000, "salary_is_predicted": "0"},
                {"id": 12, "title": "Dev", "company": {"display_name": "B"}, "location": {"display_name": "Dallas, Texas"},
                 "redirect_url": "https://www.adzuna.com/land/ad/12", "salary_min": 50000, "salary_max": 60000, "salary_is_predicted": "1"}]
        with mock.patch.object(fs, "_get", lambda url, **k: {"results": rows}):
            jobs = fs.fetch_adzuna("id", "key", ["software engineer"])
        self.assertEqual([(j.id, j.pay_min) for j in jobs], [("az:11", 95000), ("az:12", None)])
        self.assertTrue(jobs[0].url.startswith("https://www.adzuna.com/"))
        self.assertEqual((jobs[0].company, jobs[0].location, jobs[0].posted), ("Acme", "Austin, Texas", "2026-10-20"))

    def test_jooble_reads_snippets_and_salary(self):
        body = {"jobs": [{"id": 5, "title": "Python Developer", "company": "C", "location": "Austin, TX", "snippet": "Work with <b>Django</b>",
                          "salary": "$100,000 - $130,000 a year", "link": "https://jooble.org/jdp/5", "updated": "2026-10-28T00:00:00.0000000"}]}
        post = lambda url, json, timeout: FakeResp(body)
        with mock.patch.object(fs.sources, "_session", session(post=post)):
            j = fs.fetch_jooble("key", ["python"])[0]
        self.assertEqual((j.id, j.pay_min, j.posted, j.description), ("jo:5", 100000, "2026-10-28", "Work with Django"))


class FeedCollectTests(unittest.TestCase):
    def test_each_enabled_feed_runs_once_per_window_and_a_failure_does_not_stop_the_others(self):
        calls = []
        fakes = {"remoteok": lambda f, cfg: calls.append("remoteok") or [], "jobicy": lambda f, cfg: (_ for _ in ()).throw(RuntimeError("down")),
                 "hackernews": lambda f, cfg: calls.append("hn") or [sources.Job(id="hn:1", source="hackernews", company="A", title="T", location="", url="u")]}
        cfg = {"feeds": {"remoteok": {"enabled": True}, "jobicy": {"enabled": True}, "hackernews": {"enabled": True, "min_hours": 1}, "himalayas": {}}}
        with tempfile.TemporaryDirectory() as d:
            conn = db.connect(str(Path(d) / "jobs.db"))
            with mock.patch.dict(main.FEEDS, fakes), redirect_stdout(io.StringIO()):
                self.assertEqual([j.id for j in main.collect_feeds(cfg, conn)], ["hn:1"])
                self.assertEqual(main.collect_feeds(cfg, conn), [])        # all inside their windows now
            self.assertEqual(calls, ["remoteok", "hn"])
            conn.close()


    def test_feeds_that_need_a_key_are_skipped_with_a_note_until_the_key_exists(self):
        cfg = {"feeds": {"adzuna": {"enabled": True}}}
        called = []
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as d:
            conn = db.connect(str(Path(d) / "jobs.db"))
            with mock.patch.dict(main.FEEDS, {"adzuna": lambda f, c: called.append(1) or []}), mock.patch.object(main, "load_env"),                     mock.patch.dict("os.environ", {}, clear=True), redirect_stdout(out):
                main.collect_feeds(cfg, conn)
                self.assertEqual(called, [])
                self.assertIn("ADZUNA_APP_ID and ADZUNA_APP_KEY", out.getvalue())
                self.assertIsNone(db.get_meta(conn, "adzuna_last_fetch"))          # not counted as fetched
                with mock.patch.dict("os.environ", {"ADZUNA_APP_ID": "a", "ADZUNA_APP_KEY": "b"}):
                    main.collect_feeds(cfg, conn)
                self.assertEqual(called, [1])
            conn.close()


if __name__ == "__main__":
    unittest.main()
