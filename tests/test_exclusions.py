"""Offline tests for location exclusions (e.g. California)."""
import unittest

import filters

PATS = filters.compile_exclusions(["California", "CA", "San Francisco", "SF", "Bay Area", "Los Angeles"])


class ExclusionTests(unittest.TestCase):
    def test_dropped_when_every_location_excluded(self):
        for loc in ["San Francisco, CA", "Los Angeles, California", "SF Bay Area", "US-SF", "Hybrid - San Francisco, CA",
                    "San Francisco, CA | Los Angeles, CA", "South San Francisco, CA"]:
            self.assertFalse(filters.exclusion_ok(loc, PATS), loc)

    def test_kept_when_any_location_is_open(self):
        for loc in ["San Francisco, CA | Remote - US", "US-SF, US-NYC, US-Remote", "San Francisco, CA; Austin, TX",
                    "Los Angeles, CA or Denver, CO"]:
            self.assertTrue(filters.exclusion_ok(loc, PATS), loc)

    def test_no_false_positives(self):
        for loc in ["Austin, TX", "Remote - US", "Scottsdale, AZ", "Canada", "Decatur, GA", "Atlanta, GA", "Chicago, IL",
                    "Denver, CO", "United States", "Cary, NC", "Santa Fe, NM"]:
            self.assertTrue(filters.exclusion_ok(loc, PATS), loc)

    def test_state_code_is_case_sensitive(self):
        self.assertTrue(filters.exclusion_ok("Remote, ca", PATS))      # lowercase 'ca' is not the state code

    def test_remote_work_is_kept_whatever_state_it_names(self):
        for loc in ["Remote - California", "Remote, CA", "Remote-California", "California (Remote)", "US-Remote", "Remote - San Francisco",
                    "Remote - CA | San Francisco, CA"]:
            self.assertTrue(filters.exclusion_ok(loc, PATS), loc)
        self.assertFalse(filters.exclusion_ok("San Francisco, CA (Hybrid)", PATS))     # the same places without "remote" are still dropped

    def test_an_exclusion_that_says_remote_still_drops_remote_work(self):
        pats = filters.compile_exclusions(["California", "Remote - Canada"])
        self.assertFalse(filters.exclusion_ok("Remote - Canada", pats))
        self.assertTrue(filters.exclusion_ok("Remote - California", pats))
        self.assertTrue(filters.exclusion_ok("Remote - US", pats))
        self.assertTrue(filters.exclusion_ok("Remote - Canada | Austin, TX", pats))      # another location is still open

    def test_empty_inputs(self):
        self.assertTrue(filters.exclusion_ok("", PATS))
        self.assertTrue(filters.exclusion_ok("San Francisco, CA", []))
        self.assertEqual(filters.compile_exclusions(["", "  "]), [])


if __name__ == "__main__":
    unittest.main()
