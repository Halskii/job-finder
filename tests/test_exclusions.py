"""Offline tests for location exclusions (e.g. California)."""
import unittest

import filters

PATS = filters.compile_exclusions(["California", "CA", "San Francisco", "SF", "Bay Area", "Los Angeles"])


class ExclusionTests(unittest.TestCase):
    def test_dropped_when_every_location_excluded(self):
        for loc in ["San Francisco, CA", "Los Angeles, California", "SF Bay Area", "US-SF", "Remote, CA",
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

    def test_empty_inputs(self):
        self.assertTrue(filters.exclusion_ok("", PATS))
        self.assertTrue(filters.exclusion_ok("San Francisco, CA", []))
        self.assertEqual(filters.compile_exclusions(["", "  "]), [])


if __name__ == "__main__":
    unittest.main()
