"""Retailers read through their job sitemap (Gap, Nike, NAPA, Bath & Body Works, URBN, Ross, Lidl, The Fresh Market, Wayfair, IKEA, The Container
Store, 7-Eleven, Love's, Pilot Flying J, Publix, Walgreens), from saved copies of the real sitemaps and job pages in tests/data/sitemap_*."""
import json
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import filters
import retail_sitemap as rsm
import retail_sources
import sources
from tests.retailtools import Resp, Session

DATA = Path(__file__).parent / "data"
text = lambda name: (DATA / name).read_text(encoding="utf-8")
KEYS = {"gap", "nike", "napa", "bathandbodyworks", "urbn", "ross", "lidl", "freshmarket", "wayfair", "ikea", "containerstore", "seveneleven",
        "loves", "pilot", "publix", "walgreens"}
TERMS = ["software engineer", "cashier", "store manager", "data scientist", "analyst"]


def site_answer(pages: dict):
    """A session that answers each address from a dict of texts (a missing one is a 404)."""
    return lambda method, url, kw: Resp(pages[url]) if url in pages else Resp("", 404)


def fetch(key, pages, terms=TERMS, delay=0):
    session = Session(site_answer(pages))
    with mock.patch.object(sources, "_session", lambda: session), mock.patch.object(rsm.time, "sleep") as sleep, \
            mock.patch.object(rsm, "_today", lambda: date(2026, 11, 2)):
        jobs = rsm.READERS[key][1](terms, 20, delay)
    return jobs, [u for _, u, _ in session.asked], sleep


def listing(key, terms=TERMS):
    """The jobs the first sitemap fixture of a retailer yields."""
    site = rsm.SITES[key]
    entries = rsm._sitemap(text(f"sitemap_{key}.xml"), site.sitemaps[0])[1]
    with mock.patch.object(rsm, "_today", lambda: date(2026, 11, 2)):
        return rsm.parse_sitemap(site, entries, terms, key)


def fill(job, page):
    session = Session(lambda m, u, kw: Resp(page))
    with mock.patch.object(sources, "_session", lambda: session):
        sources.fill_detail(job)
    return session.asked


def job_for(key, address="https://x.example/job", **kw):
    return sources.Job(f"{key}:1", key, rsm.SITES[key].label, "Rough Title", "", address, detail={"api": address}, **kw)


def ld_page(posting, title="Job"):
    return f'<html><head><title>{title}</title><script type="application/ld+json">{json.dumps(posting)}</script></head></html>'


GOOD = {"@type": "JobPosting", "title": "Dev", "description": "<p>Build things.</p>", "datePosted": "2026-10-01",
        "jobLocation": {"address": {"addressLocality": "Austin", "addressRegion": "TX", "addressCountry": "US"}}}


class Registered(unittest.TestCase):
    def test_every_retailer_has_a_fetcher_and_a_job_page_reader(self):
        self.assertEqual(set(rsm.READERS), KEYS)
        for key, (label, fn) in rsm.READERS.items():
            self.assertTrue(callable(fn), key)
            self.assertIn(key, sources.DETAIL_FILLERS)
            self.assertEqual(label, rsm.SITES[key].label)
            self.assertIs(getattr(retail_sources, f"fetch_{key}"), fn)        # picked up by retail_sources through READER_MODULES
        self.assertTrue(KEYS <= {k for k, *_ in retail_sources.RETAIL_SOURCES})
        keys = [k for k, *_ in retail_sources.RETAIL_SOURCES]
        self.assertEqual(len(keys), len(set(keys)))

    def test_the_bounds_are_documented_constants(self):
        self.assertEqual((rsm.MAX_REQUESTS, rsm.MAX_LISTED, rsm.MAX_AGE_DAYS), (6, 3000, 365))


class Listing(unittest.TestCase):
    """Only the sitemap is read: the title is the address read back, and the job's text, date and real place come from the page later."""

    def ids(self, key, terms=TERMS):
        return [j.id for j in listing(key, terms)]

    def test_a_job_is_stored_from_its_address_alone(self):
        j = listing("gap")[2]
        self.assertEqual((j.id, j.source, j.company, j.title, j.location, j.posted), ("gap:w70-57", "gap", "Gap Inc.", "Senior Software Engineer Devops", "", None))
        self.assertEqual(j.url, "https://www.gapinc.com/en-us/jobs/w70/57/senior-software-engineer-devops")
        self.assertEqual((j.description, j.detail), ("", {"api": j.url}))          # the web app reads the page when someone's search selects it

    def test_gap_ids_and_commas_in_the_address(self):
        self.assertEqual(self.ids("gap"), ["gap:w37-10", "gap:w39-47", "gap:w70-57"])
        self.assertEqual(listing("gap")[0].title, "Data Scientist Product To Market P2M Optimization")     # the address has "data-scientist,-product-..."

    def test_each_address_shape(self):
        want = {
            "nike": (["nike:R-95491", "nike:R-87345", "nike:R-86716"], "Nike"),
            "napa": (["napa:102777825984", "napa:101313110896", "napa:101313097024"], "NAPA Auto Parts"),
            "bathandbodyworks": (["bathandbodyworks:05BNO", "bathandbodyworks:05B2Q", "bathandbodyworks:05B4Y"], "Bath & Body Works"),
            "ross": (["ross:cdb9050e-6a84-4309-9d57-335c9c48d8f7", "ross:8b961a3d-7175-4238-8d46-365ff8712a13", "ross:4ec5d8ad-5eeb-4ce2-9610-1724ecf1f2ff",
                      "ross:524c1320-9658-4ff0-b62a-c9d54de73f62"], "Ross Stores"),
            "lidl": (["lidl:751103", "lidl:748867", "lidl:743586"], "Lidl US"),
            "freshmarket": (["freshmarket:102786564880", "freshmarket:102786564896", "freshmarket:102528987072"], "The Fresh Market"),
            "wayfair": (["wayfair:2-18607", "wayfair:2-18611", "wayfair:2-18236"], "Wayfair"),
            "containerstore": (["containerstore:17630", "containerstore:17626", "containerstore:17625"], "The Container Store"),
            "seveneleven": (["seveneleven:98112461552", "seveneleven:102487739760", "seveneleven:102487739392"], "7-Eleven"),
            "loves": (["loves:493850", "loves:493457", "loves:486045"], "Love's Travel Stops"),
            "pilot": (["pilot:00419E63AA7BE214027F8456B6BDF456", "pilot:01C2E354263D9E11771E545461A751EE", "pilot:01E944BDDA21CF92B9D9E0A209D9C5E7"], "Pilot Flying J"),
            "walgreens": (["walgreens:102784265056", "walgreens:102784264768"], "Walgreens"),
        }
        for key, (ids, company) in want.items():
            with self.subTest(key):
                jobs = listing(key)
                self.assertEqual([j.id for j in jobs], ids)
                self.assertEqual({j.company for j in jobs}, {company})
                self.assertTrue(all(j.source == key and j.url.startswith("https://") and j.description == "" and j.detail for j in jobs))

    def test_titles_are_the_address_read_back(self):
        self.assertEqual([j.title for j in listing("wayfair")], ["Assistant Store Manager Retail Sales", "Assistant Store Manager People Operations",
                                                                "Software Engineer Ii Graphql Platform"])      # %2C is a comma
        self.assertEqual({j.title for j in listing("containerstore")}, {"Store Manager"})                     # the place at the end of the address is not title
        self.assertEqual(listing("lidl")[1].title, "Store Manager Trainee Southern Raleigh Garner")
        self.assertEqual(self.ids("lidl", ["raleigh"]), ["lidl:751103", "lidl:748867"])                      # Lidl's address ends with the place

    def test_the_page_is_read_with_in_iframe_for_urbn_but_the_link_stays_plain(self):
        j = listing("urbn")[1]
        self.assertEqual(j.url, "https://homeoffice-na-urbn.icims.com/jobs/32618/urbn-senior-software-engineer/job")
        self.assertEqual(j.detail, {"api": j.url + "?in_iframe=1"})

    def test_urbn_banner_is_the_first_word_of_the_address(self):
        banners = {j.id: j.company for j in listing("urbn", ["software engineer", "analyst", "manager", "design director", "operations"])}
        self.assertEqual(banners["urbn:32697"], "Anthropologie")
        self.assertNotIn("urbn:19234", banners)                         # last changed in 2025-08: over a year before the (fixed) today
        self.assertEqual(banners["urbn:27246"], "Free People")        # fp-movement-...
        entries = [("https://homeoffice-na-urbn.icims.com/jobs/1/free-people-design-director/job", ""), ("https://homeoffice-na-urbn.icims.com/jobs/2/uo-buyer/job", ""),
                   ("https://homeoffice-na-urbn.icims.com/jobs/3/urban-outfitters-buyer/job", ""), ("https://homeoffice-na-urbn.icims.com/jobs/4/fpx-buyer/job", "")]
        got = rsm.parse_sitemap(rsm.SITES["urbn"], entries, ["buyer", "design director"], "urbn")
        self.assertEqual([j.company for j in got], ["Free People", "Urban Outfitters", "Urban Outfitters", "URBN"])    # "fpx" is not "fp"
        self.assertEqual(banners["urbn:36176"], "Nuuly")
        self.assertEqual(banners["urbn:32618"], "URBN")
        self.assertEqual(len([j for j in listing("urbn", ["manager"]) if j.id == "urbn:27246"]), 1)    # listed twice in the sitemap, kept once

    def test_ikea_keeps_the_city_as_the_place_until_the_page_gives_the_country(self):
        jobs = listing("ikea", ["software engineer"])
        self.assertEqual([(j.id, j.location) for j in jobs], [("ikea:102196876752", "San Sebastian De Los Reyes"), ("ikea:90947272384", "Amsterdam"),
                                                           ("ikea:102214843232", "Madrid")])
        self.assertEqual(listing("ikea", ["warehouse associate"])[0].location, "Conshohocken")
        self.assertEqual([j.location for j in listing("gap")], ["", "", ""])      # other sites leave the place to the page

    def test_publix_id_is_the_address_name_and_the_number_that_separates_copies_is_not_title(self):
        jobs = listing("publix", ["cashier", "pharmacy technician"])
        self.assertCountEqual([j.id for j in jobs], ["publix:pharmacy-technician", "publix:cashier", "publix:pharmacy-technician-central-fill-pharmacy-jacksonville-st-johns"])
        self.assertEqual(rsm._words("pharmacist-full-time-floater-20"), "pharmacist-full-time-floater-20")
        site, key = rsm.SITES["publix"], "publix"
        entries = [(f"https://jobs.publix.com/job-opening/pharmacist-full-time-floater-{n}/", "") for n in (20, 34)]
        self.assertEqual([j.title for j in rsm.parse_sitemap(site, entries, ["pharmacist"], key)], ["Pharmacist Full Time Floater"] * 2)

    def test_a_search_word_must_be_whole_words_in_the_title(self):
        entries = [("https://careers.nike.com/software-engineering-manager/job/R-1", ""), ("https://careers.nike.com/sr-software-engineer-itc/job/R-2", ""),
                   ("https://careers.nike.com/software-engineer/job/R-3", ""), ("https://careers.nike.com/softwareengineer/job/R-4", "")]
        got = rsm.parse_sitemap(rsm.SITES["nike"], entries, ["Software Engineer"], "nike")
        self.assertEqual([j.id for j in got], ["nike:R-2", "nike:R-3"])
        self.assertEqual(rsm.parse_sitemap(rsm.SITES["nike"], entries, [], "nike"), [])
        self.assertEqual(rsm.parse_sitemap(rsm.SITES["nike"], entries, ["", " ", "---"], "nike"), [])

    def test_addresses_that_are_not_jobs_are_ignored(self):
        entries = [("https://careers.nike.com/", ""), ("https://careers.nike.com/about/software-engineer", ""), ("https://other.example/x/job/R-1", "")]
        self.assertEqual([j.id for j in rsm.parse_sitemap(rsm.SITES["nike"], entries, ["software engineer"], "nike")], [])

    def test_the_newest_come_first_and_clearly_old_ones_are_skipped(self):
        site = rsm.SITES["loves"]
        entries = [("https://jobs.loves.com/cashier/job/1", "2026-09-01T00:00:00+00:00"), ("https://jobs.loves.com/cashier/job/2", "2026-10-30T00:00:00+00:00"),
                   ("https://jobs.loves.com/cashier/job/3", "2025-10-01T00:00:00+00:00"), ("https://jobs.loves.com/cashier/job/4", ""),
                   ("https://jobs.loves.com/cashier/job/5", "2025-11-03T00:00:00+00:00")]
        with mock.patch.object(rsm, "_today", lambda: date(2026, 11, 2)):
            got = rsm.parse_sitemap(site, entries, ["cashier"], "loves")
        self.assertEqual([j.id for j in got], ["loves:2", "loves:1", "loves:5", "loves:4"])      # 3 is over a year old; an address without a date stays, last

    def test_the_list_is_cut_at_the_maximum(self):
        entries = [(f"https://jobs.loves.com/cashier/job/{n}", f"2026-10-{n:02d}") for n in range(1, 10)]
        with mock.patch.object(rsm, "MAX_LISTED", 3), mock.patch.object(rsm, "_today", lambda: date(2026, 11, 2)):
            got = rsm.parse_sitemap(rsm.SITES["loves"], entries, ["cashier"], "loves")
        self.assertEqual([j.id for j in got], ["loves:9", "loves:8", "loves:7"])


class Sitemaps(unittest.TestCase):
    def test_one_sitemap_is_one_request_with_the_search_words_applied_to_it(self):
        jobs, asked, sleep = fetch("gap", {"https://www.gapinc.com/googlesitemap.xml": text("sitemap_gap.xml")})
        self.assertEqual(asked, ["https://www.gapinc.com/googlesitemap.xml"])
        self.assertEqual([j.id for j in jobs], ["gap:w37-10", "gap:w39-47", "gap:w70-57"])
        sleep.assert_not_called()

    def test_each_retailers_sitemap_address(self):
        want = {"gap": "https://www.gapinc.com/googlesitemap.xml", "nike": "https://careers.nike.com/sitemap.xml", "napa": "https://jobs.genpt.com/sitemap.xml",
                "bathandbodyworks": "https://careers.bathandbodyworks.com/sitemap.xml", "urbn": "https://homeoffice-na-urbn.icims.com/sitemap.xml",
                "ross": "https://jobs.rossstores.com/sitemap.xml", "lidl": "https://careers.lidl.com/sitemap.xml",
                "freshmarket": "https://jobs.thefreshmarket.com/sitemap.xml", "wayfair": "https://www.wayfair.com/careers/sitemap.xml",
                "ikea": "https://jobs.ikea.com/sitemap.xml", "containerstore": "https://www.jobs.containerstore.com/sitemap.xml",
                "seveneleven": "https://careers.7-eleven.com/sitemap.xml", "loves": "https://jobs.loves.com/sitemap.xml",
                "pilot": "https://careers.pilotcompany.com/sitemap.xml", "publix": "https://jobs.publix.com/sitemap.xml",
                "walgreens": "https://jobs.walgreens.com/en/sitemap.xml"}
        for key, address in want.items():
            self.assertEqual(rsm.SITES[key].sitemaps, (address,), key)
        self.assertNotIn("/search-jobs", "".join(a for s in rsm.SITES.values() for a in s.sitemaps))      # blocked in robots.txt on most of these
        self.assertNotIn("avature", "".join(a for s in rsm.SITES.values() for a in s.sitemaps))
        self.assertNotIn("myworkdayjobs", "".join(a for s in rsm.SITES.values() for a in s.sitemaps))

    def test_an_index_lists_the_sitemaps_and_only_the_english_job_one_is_read(self):
        pages = {"https://careers.nike.com/sitemap.xml": text("sitemap_nike_index.xml"), "https://careers.nike.com/sitemap-08018534-en.xml": text("sitemap_nike.xml")}
        jobs, asked, sleep = fetch("nike", pages, delay=0.7)
        self.assertEqual(asked, ["https://careers.nike.com/sitemap.xml", "https://careers.nike.com/sitemap-08018534-en.xml"])    # not -de, not -content
        self.assertEqual([j.id for j in jobs], ["nike:R-95491", "nike:R-87345", "nike:R-86716"])
        self.assertEqual(sleep.call_args_list, [mock.call(0.7)])        # the delay is kept between two requests to the one host

    def test_a_relative_address_in_an_index_is_made_whole(self):
        pages = {"https://careers.lidl.com/sitemap.xml": text("sitemap_lidl_index.xml"), "https://careers.lidl.com/sitemap-jobs.xml": text("sitemap_lidl.xml")}
        jobs, asked, _ = fetch("lidl", pages)
        self.assertEqual(asked, ["https://careers.lidl.com/sitemap.xml", "https://careers.lidl.com/sitemap-jobs.xml"])
        self.assertEqual(len(jobs), 3)

    def test_a_retailer_with_two_job_sitemaps_reads_both(self):
        pages = {"https://careers.pilotcompany.com/sitemap.xml": text("sitemap_pilot_index.xml"),
                 "https://careers.pilotcompany.com/sitemap-4fda8f62-en.xml": text("sitemap_pilot.xml"),
                 "https://careers.pilotcompany.com/sitemap-cc0905f2-en.xml": text("sitemap_pilot_b.xml")}
        jobs, asked, sleep = fetch("pilot", pages, ["cashier", "team member"], delay=0.4)
        self.assertEqual(len(asked), 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertIn("pilot:0A88C6ABE2691189B8956A6506389E36", [j.id for j in jobs])          # from the second sitemap
        self.assertIn("pilot:01C2E354263D9E11771E545461A751EE", [j.id for j in jobs])

    def test_publix_reads_only_its_job_opening_sitemap(self):
        pages = {"https://jobs.publix.com/sitemap.xml": text("sitemap_publix_index.xml"), "https://jobs.publix.com/job_opening-sitemap.xml": text("sitemap_publix.xml")}
        jobs, asked, _ = fetch("publix", pages, ["cashier"])
        self.assertEqual(asked, ["https://jobs.publix.com/sitemap.xml", "https://jobs.publix.com/job_opening-sitemap.xml"])
        self.assertEqual([j.id for j in jobs], ["publix:cashier"])           # listed twice in the sitemap, counted once

    def test_ross_skips_the_career_site_sitemap(self):
        pages = {"https://jobs.rossstores.com/sitemap.xml": text("sitemap_ross_index.xml"), "https://jobs.rossstores.com/jobs-site.xml": text("sitemap_ross.xml")}
        jobs, asked, _ = fetch("ross", pages, ["store manager"])
        self.assertEqual(asked[1:], ["https://jobs.rossstores.com/jobs-site.xml"])
        self.assertEqual(len(jobs), 4)

    def test_the_number_of_sitemaps_is_bounded(self):
        index = '<sitemapindex>' + "".join(f"<sitemap><loc>https://jobs.loves.com/s{n}-en.xml</loc></sitemap>" for n in range(20)) + "</sitemapindex>"
        pages = {"https://jobs.loves.com/sitemap.xml": index, **{f"https://jobs.loves.com/s{n}-en.xml": text("sitemap_loves.xml") for n in range(20)}}
        session = Session(site_answer(pages))
        with mock.patch.object(sources, "_session", lambda: session), mock.patch.object(rsm.time, "sleep"), self.assertRaises(ValueError):
            rsm.READERS["loves"][1](["cashier"], 20, 0)
        self.assertEqual(len(session.asked), rsm.MAX_REQUESTS)

    def test_the_request_has_a_timeout(self):
        session = Session(site_answer({"https://www.gapinc.com/googlesitemap.xml": text("sitemap_gap.xml")}))
        with mock.patch.object(sources, "_session", lambda: session):
            rsm.READERS["gap"][1](["cashier"], 20, 0)
        self.assertEqual(session.asked[0][2], {"timeout": rsm.SITEMAP_TIMEOUT})

    def test_no_search_words_means_no_request(self):
        jobs, asked, _ = fetch("gap", {}, [])
        self.assertEqual((jobs, asked), ([], []))
        jobs, asked, _ = fetch("gap", {}, ["", " "])
        self.assertEqual((jobs, asked), ([], []))

    def test_a_blocked_or_changed_site_raises_so_the_run_says_skipped(self):
        address = "https://www.gapinc.com/googlesitemap.xml"
        with self.assertRaises(ValueError):                    # a block page where the sitemap should be
            fetch("gap", {address: "<html>Access Denied</html>"})
        with self.assertRaises(RuntimeError):
            session = Session(lambda m, u, kw: Resp("", 403))
            with mock.patch.object(sources, "_session", lambda: session):
                rsm.READERS["gap"][1](["cashier"], 20, 0)
        with self.assertRaises(ValueError):                    # a sitemap whose addresses are not jobs any more
            fetch("gap", {address: '<urlset><url><loc>https://www.gapinc.com/en-us/about</loc></url></urlset>'})

    def test_a_sitemap_with_no_match_is_an_empty_list_not_an_error(self):
        jobs, _, _ = fetch("gap", {"https://www.gapinc.com/googlesitemap.xml": text("sitemap_gap.xml")}, ["astronaut"])
        self.assertEqual(jobs, [])

    def test_xml_entities_in_an_address_are_decoded(self):
        more, entries = rsm._sitemap('<urlset><url><loc>https://jobs.loves.com/cashier/job/1?a=1&amp;b=2</loc></url></urlset>', "https://jobs.loves.com/")
        self.assertEqual(entries, [("https://jobs.loves.com/cashier/job/1?a=1&b=2", "")])


class JobPages(unittest.TestCase):
    """The page of a posting: the real title, text, date, places and pay, from one request."""

    def read(self, key, name=None, **kw):
        job = job_for(key, **kw)
        asked = fill(job, text(name or f"sitemap_{key}_job.html"))
        return job, asked

    def test_gap_gives_the_banner_the_date_and_the_place(self):
        j, asked = self.read("gap")
        self.assertEqual((j.title, j.company, j.posted, j.location), ("Principal Engineer, Agentic Applications", "Gap Inc.", "2026-09-22", "Pleasanton, California, United States"))
        self.assertIn("About the role", j.description)
        self.assertNotIn("&lt;", j.description)                      # Gap escapes its HTML twice
        self.assertNotIn("<div>", j.description)
        j, _ = self.read("gap", "sitemap_gap_oldnavy_job.html")
        self.assertEqual((j.title, j.company, j.location), ("Retail Sales Associate - Polaris T/C", "Old Navy", "Columbus, Ohio, United States"))

    def test_gap_company_falls_back_to_the_page_title(self):
        page = ld_page({**GOOD, "hiringOrganization": {"name": ""}}, "Cashier | Banana Republic")
        j = job_for("gap")
        fill(j, page)
        self.assertEqual(j.company, "Banana Republic")
        j = job_for("gap")
        fill(j, ld_page(GOOD, "Cashier"))
        self.assertEqual(j.company, "Gap Inc.")                      # nothing better: the list's label stays

    def test_a_redirect_is_never_followed_and_means_the_job_is_gone(self):
        session = Session(lambda m, u, kw: Resp("", 302, {"Location": "/en-us/careers/job-search?searchOrigin=job_detail_shell_redirect"}))
        with mock.patch.object(sources, "_session", lambda: session), self.assertRaises(ValueError):
            sources.fill_detail(job_for("gap"))
        self.assertEqual(session.asked[0][2]["allow_redirects"], False)
        self.assertEqual(session.asked[0][2]["timeout"], sources.TIMEOUT)

    def test_no_page_is_read_with_redirects_followed(self):
        for key in KEYS:
            _, asked = self.read(key)
            self.assertIs(asked[0][2]["allow_redirects"], False, key)

    def test_every_retailers_page_gives_text_title_and_date(self):
        want = {"nike": ("Software Engineer II, ITC", "2026-08-04"), "napa": ("Driver (Part Time)", "2026-11-02"),
                "bathandbodyworks": ("Senior Analyst, AI & Data Product Delivery", "2026-09-08"), "urbn": ("Nuuly Creative Operations Manager", "2026-11-02"),
                "ross": ("Engineer II", "2026-10-23"), "lidl": ("Regional Real Estate - Senior Manager (Charlotte, NC)", "2026-09-22"),
                "freshmarket": ("Cashier Team Member", "2026-11-03"), "wayfair": ("Software Engineer II- GraphQL Platform", "2026-08-04"),
                "ikea": ("Retail Warehouse Associate (Full-Time)", "2026-10-30"), "containerstore": ("Central Design Specialist", "2026-09-28"),
                "seveneleven": ("Store Leader (Manager) Trainee", "2026-11-02"), "loves": ("Cybersecurity Engineer II", "2026-10-23"),
                "pilot": ("Sr Analyst, HR People Analytics", "2026-09-21"), "publix": ("Software Engineer – WMS Integration and Reporting (Hybrid)", "2026-11-02"),
                "walgreens": ("Pharmacy Technician / Pharm Tech Apprenticeship", "2026-11-02")}
        for key, (title, posted) in want.items():
            with self.subTest(key):
                j, _ = self.read(key)
                self.assertEqual((j.title, j.posted), (title, posted))
                self.assertGreater(len(j.description), 400)
                self.assertNotRegex(j.description, r"</?[a-z]+[ />]")
                self.assertNotIn("&lt;", j.description)

    def test_places(self):
        want = {"napa": "Atlanta, Georgia, United States",       # NAPA gives the city in capitals in some pages: 'ATLANTA' is title-cased
                "bathandbodyworks": "Columbus, OH", "urbn": "Philadelphia, PA", "ross": "New York, New York, United States", "lidl": "Mebane, United States",
                "freshmarket": "Williamsburg, Virginia, United States", "wayfair": "Mountain View, California, United States", "ikea": "Conshohocken, Pennsylvania, United States",
                "containerstore": "Coppell, Texas, United States", "seveneleven": "Katy, TX", "loves": "Oklahoma City, OK", "pilot": "Knoxville, TN",
                "publix": "Lakeland, FL", "walgreens": "Lewisville, TX"}
        for key, place in want.items():
            with self.subTest(key):
                j, _ = self.read(key)
                self.assertEqual(j.location, place)
                self.assertTrue(filters.location_ok(j.location, True))

    def test_a_posting_abroad_is_recognized_by_its_place(self):
        j, _ = self.read("ikea", "sitemap_ikea_abroad_job.html")
        self.assertEqual(j.location, "San Sebastián De Los, Madrid, Spain")
        self.assertFalse(filters.location_ok(j.location, True))          # dropped after the page is read
        j, _ = self.read("nike")
        self.assertEqual(j.location, "Karnataka, Karnātaka, India")     # the code IN is India, not Indiana
        self.assertFalse(filters.location_ok(j.location, True))

    def test_a_country_code_that_looks_like_a_state_is_a_country(self):
        for code, name in (("IN", "India"), ("CA", "Canada"), ("DE", "Germany"), ("IL", "Israel"), ("GB", "United Kingdom")):
            posting = {**GOOD, "jobLocation": {"address": {"addressLocality": "Somewhere", "addressRegion": "XX", "addressCountry": code}}}
            j = job_for("loves")
            fill(j, ld_page(posting))
            self.assertEqual(j.location, f"Somewhere, XX, {name}")
        for us in ("US", "USA", "United States", "United States of America", {"@type": "Country", "name": "US"}):
            posting = {**GOOD, "jobLocation": {"address": {"addressLocality": "Austin", "addressRegion": "Texas", "addressCountry": us}}}
            j = job_for("loves")
            fill(j, ld_page(posting))
            self.assertEqual(j.location, "Austin, Texas, United States")

    def test_several_places_are_all_listed_once(self):
        where = [{"address": {"addressLocality": "Austin", "addressRegion": "TX", "addressCountry": "US"}},
                 {"address": {"addressLocality": "DALLAS", "addressRegion": "TX", "addressCountry": "US"}},
                 {"address": {"addressLocality": "Austin", "addressRegion": "TX", "addressCountry": "US"}}]
        j = job_for("loves")
        fill(j, ld_page({**GOOD, "jobLocation": where}))
        self.assertEqual(j.location, "Austin, TX; Dallas, TX")

    def test_a_remote_posting_says_so(self):
        j = job_for("loves")
        fill(j, ld_page({**GOOD, "jobLocationType": "TELECOMMUTE"}))
        self.assertEqual(j.location, "Austin, TX (Remote)")
        virtual = {"address": {"addressLocality": "Atlanta", "addressRegion": "GA", "streetAddress": "VIRTUAL", "addressCountry": "US"}}
        j = job_for("loves")
        fill(j, ld_page({**GOOD, "jobLocation": [virtual]}))
        self.assertEqual(j.location, "Atlanta, GA (Remote)")
        j = job_for("loves", )
        fill(j, ld_page({**GOOD, "jobLocation": None, "jobLocationType": "TELECOMMUTE"}))
        self.assertEqual(j.location, "Remote")

    def test_no_place_keeps_the_one_from_the_list(self):
        j = job_for("ikea")
        j.location = "Amsterdam"
        fill(j, ld_page({**GOOD, "jobLocation": None}))
        self.assertEqual(j.location, "Amsterdam")

    def test_a_place_that_is_only_text_and_a_country_that_is_missing(self):
        j = job_for("loves")
        fill(j, ld_page({**GOOD, "jobLocation": {"address": "100 Main St, Tulsa, OK 74103"}}))
        self.assertEqual(j.location, "100 Main St, Tulsa, OK 74103")
        j = job_for("walgreens")
        fill(j, ld_page({**GOOD, "jobLocation": [{"address": {"addressLocality": "SAN JUAN", "addressRegion": "PR", "addressCountry": ""}}]}))
        self.assertEqual(j.location, "San Juan, PR")            # Walgreens leaves the country empty

    def test_dates_in_each_retailers_format(self):
        self.assertEqual(rsm.iso_date("2026-10-26T00:00:00.000+0000"), "2026-10-26")
        self.assertEqual(rsm.iso_date("2026-08-04T17:16:19.483000"), "2026-08-04")
        self.assertEqual(rsm.iso_date("10/23/2026"), "2026-10-23")                  # Ross
        self.assertEqual(rsm.iso_date("1/5/2026"), "2026-01-05")
        self.assertEqual(rsm.iso_date("Sep 28, 2026"), "2026-09-28")                # The Container Store
        self.assertEqual(rsm.iso_date("Nov 02, 2026"), "2026-11-02")                # Publix
        self.assertEqual(rsm.iso_date("November 2, 2026"), "2026-11-02")
        for bad in ("", None, "yesterday", "13/45/2026"):
            self.assertIsNone(rsm.iso_date(bad))

    def test_a_missing_date_keeps_the_one_already_known(self):
        j = job_for("loves", posted="2026-10-01")
        fill(j, ld_page({**GOOD, "datePosted": "soon"}))
        self.assertEqual(j.posted, "2026-10-01")

    def test_ross_marks_its_script_with_an_html_entity(self):
        page = text("sitemap_ross_job.html")
        self.assertIn("ld&#x2B;json", page)
        self.assertEqual(rsm._json_ld(page)["title"], "Engineer II")

    def test_yearly_pay_in_the_text_is_read(self):
        j, _ = self.read("wayfair")
        self.assertEqual((j.pay_min, j.pay_max), (165000, 181500))

    def test_hourly_pay_kept_outside_the_text_is_added_so_fill_pay_finds_it(self):
        j, _ = self.read("containerstore")
        self.assertTrue(j.description.rstrip().endswith("Pay: $22.00 - 24.00 per hour"))
        self.assertEqual((j.pay_min, j.pay_max), (None, None))
        sources.fill_pay(j)
        self.assertEqual((j.pay_min, j.pay_max, j.pay_unit), (22, 24, "hour"))

    def test_a_pay_figure_in_numbers_and_an_empty_shell(self):
        salary = {"@type": "MonetaryAmount", "currency": "USD", "value": {"@type": "QuantitativeValue", "minValue": 18, "maxValue": 21.5, "unitText": "HOUR"}}
        j = job_for("loves")
        fill(j, ld_page({**GOOD, "baseSalary": salary}))
        self.assertTrue(j.description.endswith("Pay: $18 - $21.50 per hour"))
        salary["value"] = {"minValue": 90000, "maxValue": 120000, "unitText": "YEAR"}
        j = job_for("loves")
        fill(j, ld_page({**GOOD, "baseSalary": salary}))
        self.assertEqual((j.pay_min, j.pay_max), (90000, 120000))
        shell = {"@type": "MonetaryAmount", "currency": "", "value": {"@type": "QuantitativeValue", "unitText": ""}}
        j = job_for("loves")
        fill(j, ld_page({**GOOD, "baseSalary": shell}))
        self.assertEqual((j.description, j.pay_min, j.pay_max), ("Build things.", None, None))

    def test_publix_pay_is_monthly_in_its_header_and_is_given_as_a_year(self):
        j, _ = self.read("publix")
        self.assertEqual((j.pay_min, j.pay_max), (83160, 124800))                   # $6,930 - $10,400 a month
        self.assertTrue(j.description.endswith("Pay: $83,160 - $124,800 per year"))
        self.assertIn("Your impact", j.description)
        self.assertEqual(rsm._pay_line("17.5", "19", "Hourly"), "Pay: $17.50 - $19 per hour")
        self.assertEqual(rsm._pay_line("1000", "1000", "Weekly"), "Pay: $52,000 per year")
        self.assertEqual(rsm._pay_line("5", "6", ""), "")

    def test_publix_place_without_a_state_is_still_a_us_place(self):
        page = text("sitemap_publix_job.html").replace("Lakeland, FL", "All retail locations")
        j = job_for("publix")
        fill(j, page)
        self.assertEqual(j.location, "All retail locations, United States")

    def test_urbn_page_is_read_from_the_light_address(self):
        job = listing("urbn")[1]
        asked = fill(job, text("sitemap_urbn_job.html"))
        self.assertEqual(job.id, "urbn:32618")
        self.assertEqual(asked[0][1], "https://homeoffice-na-urbn.icims.com/jobs/32618/urbn-senior-software-engineer/job?in_iframe=1")
        self.assertEqual(job.url, "https://homeoffice-na-urbn.icims.com/jobs/32618/urbn-senior-software-engineer/job")

    def test_napa_banner_follows_the_industry(self):
        j, _ = self.read("napa")
        self.assertEqual(j.company, "NAPA Auto Parts")
        page = ld_page({**GOOD, "industry": "MOTION", "hiringOrganization": {"name": "Genuine Parts Company"}})
        j = job_for("napa")
        fill(j, page)
        self.assertEqual(j.company, "Genuine Parts Company")

    def test_the_page_is_read_from_detail_api_first(self):
        j = job_for("loves", "https://jobs.loves.com/a/job/1")
        j.detail = {"api": "https://jobs.loves.com/a/job/1?x=1"}
        self.assertEqual(fill(j, ld_page(GOOD))[0][1], "https://jobs.loves.com/a/job/1?x=1")
        j.detail = {}
        self.assertEqual(fill(j, ld_page(GOOD))[0][1], "https://jobs.loves.com/a/job/1")

    def test_a_page_that_is_not_the_posting_is_an_error_so_it_is_retried_next_run(self):
        for key in KEYS:
            for page in ("<html>Access Denied</html>", '<script type="application/ld+json">{"@type":"WebSite"}</script>', '<script type="application/ld+json">not json</script>',
                         ld_page({"@type": "JobPosting", "title": "No text", "datePosted": "2026-10-01"}), ld_page({"@type": "JobPosting", "description": " "})):
                with self.subTest(key=key, page=page[:40]), self.assertRaises(ValueError):
                    fill(job_for(key), page)

    def test_an_old_wayfair_page_with_only_site_data_raises_cleanly(self):
        page = '<script type="application/ld+json">{"@context":"https://schema.org/","@type":"WebSite","name":"Wayfair"}</script>'
        with self.assertRaises(ValueError):
            fill(job_for("wayfair"), page)

    def test_a_publix_page_that_changed_raises_cleanly(self):
        for page in ("<html>Access Denied</html>", '<script id="__NEXT_DATA__" type="application/json">{"props":{}}</script>',
                     '<script id="__NEXT_DATA__" type="application/json">not json</script>',
                     '<script id="__NEXT_DATA__" type="application/json">{"props":{"pageProps":{"fallback":{"k":{"result":{"content":{"rendered":"<p>x</p>"}}}}}}}</script>'):
            with self.subTest(page=page[:60]), self.assertRaises(ValueError):
                fill(job_for("publix"), page)

    def test_a_posting_taken_down_is_an_error(self):
        for status in (404, 410, 500):
            session = Session(lambda m, u, kw: Resp("", status))
            with mock.patch.object(sources, "_session", lambda: session), self.assertRaises(RuntimeError):
                sources.fill_detail(job_for("loves"))
        with mock.patch.object(sources, "_session", lambda: Session(lambda m, u, kw: Resp("", 301))), self.assertRaises(ValueError):
            sources.fill_detail(job_for("walgreens"))               # a redirect is not followed for any of them

    def test_a_json_ld_list_or_graph_is_searched(self):
        posting = {**GOOD}
        for data in ([{"@type": "WebSite"}, posting], {"@graph": [{"@type": "WebSite"}, posting]}, {**posting, "@type": ["JobPosting"]}):
            self.assertEqual(rsm._json_ld(f'<script type="application/ld+json">{json.dumps(data)}</script>')["title"], "Dev")
        self.assertEqual(rsm._json_ld("<p>none</p>"), {})


if __name__ == "__main__":
    unittest.main()
