"""Workday search words derived from the title list, and Workday paging that stops when a search is exhausted. No network."""
import unittest
from pathlib import Path
from unittest import mock

import yaml

import filters
import sources

ROOT = Path(__file__).parent.parent
TITLES = ["Software Engineer I", "Software Engineer II", "Associate Software Engineer", "Junior Software Engineer",
          "Platform Engineer I", "Platform Engineer II", "DevOps Engineer", "CI/CD Engineer", "AI Engineer I"]


class WorkdayTermsTests(unittest.TestCase):
    def test_base_title_strings_drop_level_markers_and_repeats(self):
        self.assertEqual(filters.base_title_strings(TITLES),
                         ["software engineer", "platform engineer", "devops engineer", "ci/cd engineer", "ai engineer"])

    def test_base_titles_patterns_are_unchanged_by_the_refactor(self):
        patterns = filters.base_titles(TITLES)
        self.assertEqual(len(patterns), 5)
        self.assertTrue(filters.title_ok("Associate Software Engineer", patterns, []))
        self.assertTrue(filters.title_ok("Software Engineer, Platform Access (L2)", patterns, []))
        self.assertFalse(filters.title_ok("Software Engineering Manager", patterns, []))     # whole words only

    def test_auto_uses_the_titles(self):
        self.assertEqual(filters.workday_terms({"titles": TITLES, "workday_search_terms": "auto"}),
                         filters.base_title_strings(TITLES))

    def test_missing_setting_means_auto(self):
        self.assertEqual(filters.workday_terms({"titles": TITLES}), filters.base_title_strings(TITLES))

    def test_an_explicit_list_is_used_as_given(self):
        cfg = {"titles": TITLES, "workday_search_terms": ["devops", "machine learning"]}
        self.assertEqual(filters.workday_terms(cfg), ["devops", "machine learning"])

    def test_auto_can_be_mixed_with_extra_words_without_repeats(self):
        cfg = {"titles": TITLES, "workday_search_terms": ["auto", "machine learning", "Software Engineer", "  "]}
        terms = filters.workday_terms(cfg)
        self.assertEqual(terms[:5], filters.base_title_strings(TITLES))
        self.assertEqual(terms[5:], ["machine learning"])        # the duplicate and the blank entry are dropped

    def test_empty_and_null_settings_give_no_searches(self):
        self.assertEqual(filters.workday_terms({"titles": TITLES, "workday_search_terms": []}), [])
        self.assertEqual(filters.workday_terms({"titles": TITLES, "workday_search_terms": None}), [])

    def test_editing_titles_changes_the_search_words(self):
        before = filters.workday_terms({"titles": TITLES})
        after = filters.workday_terms({"titles": TITLES + ["Kubernetes Engineer"]})
        self.assertEqual(after, before + ["kubernetes engineer"])

    def test_example_config_uses_auto(self):
        cfg = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
        self.assertEqual(cfg["workday_search_terms"], "auto")
        self.assertEqual(filters.workday_terms(cfg), filters.base_title_strings(cfg["titles"]))
        self.assertIn("software engineer", filters.workday_terms(cfg))


class FakeResponse:
    def __init__(self, postings, total):
        self._data = {"jobPostings": postings, "total": total}

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def page(start, n):
    return [{"title": f"Software Engineer {i}", "externalPath": f"/job/Austin/Software-Engineer_R{i}", "locationsText": "Austin, TX"}
            for i in range(start, start + n)]


ENTRY = {"name": "Acme", "host": "acme.wd1.myworkdayjobs.com", "tenant": "acme", "site": "ext"}


class WorkdayPagingTests(unittest.TestCase):
    def run_fetch(self, responses, max_pages=20, terms=("software engineer",)):
        calls = []

        def fake_post(url, json, headers, timeout):
            calls.append(json)
            return responses[len(calls) - 1]

        with mock.patch.object(sources.requests, "post", fake_post):
            jobs = sources.fetch_workday(ENTRY, list(terms), max_pages, 0)
        return jobs, calls

    def test_stops_after_the_last_page_by_total(self):
        jobs, calls = self.run_fetch([FakeResponse(page(0, 20), 45), FakeResponse(page(20, 20), 0), FakeResponse(page(40, 5), 0)])
        self.assertEqual((len(jobs), len(calls)), (45, 3))            # 20 + 20 + 5; no fourth request
        self.assertEqual([c["offset"] for c in calls], [0, 20, 40])

    def test_a_search_with_exactly_one_full_page_is_one_request(self):
        jobs, calls = self.run_fetch([FakeResponse(page(0, 20), 20)])
        self.assertEqual((len(jobs), len(calls)), (20, 1))

    def test_a_short_first_page_is_one_request(self):
        jobs, calls = self.run_fetch([FakeResponse(page(0, 7), 7)])
        self.assertEqual((len(jobs), len(calls)), (7, 1))

    def test_no_results_is_one_request(self):
        jobs, calls = self.run_fetch([FakeResponse([], 0)])
        self.assertEqual((len(jobs), len(calls)), (0, 1))

    def test_a_short_page_stops_even_when_the_total_is_missing(self):
        jobs, calls = self.run_fetch([FakeResponse(page(0, 20), None), FakeResponse(page(20, 3), None)])
        self.assertEqual((len(jobs), len(calls)), (23, 2))

    def test_max_pages_still_caps_a_huge_search(self):
        jobs, calls = self.run_fetch([FakeResponse(page(i * 20, 20), 1000) for i in range(10)], max_pages=3)
        self.assertEqual((len(jobs), len(calls)), (60, 3))

    def test_each_search_word_restarts_at_the_first_page_and_postings_are_deduped(self):
        responses = [FakeResponse(page(0, 5), 5), FakeResponse(page(3, 5), 5)]       # overlapping ids: 3 and 4 appear twice
        jobs, calls = self.run_fetch(responses, terms=("software engineer", "platform engineer"))
        self.assertEqual([c["searchText"] for c in calls], ["software engineer", "platform engineer"])
        self.assertEqual([c["offset"] for c in calls], [0, 0])
        self.assertEqual(len(jobs), 8)


if __name__ == "__main__":
    unittest.main()
