"""The list filters: the pay a job carries for the salary filter, the page's markup, and (where Node is installed) the page's own place
detection and filtering logic, run on real-looking location lines."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import viewer


class PayYearTests(unittest.TestCase):
    def test_pay_is_dollars_a_year_and_hourly_counts_as_a_full_time_year(self):
        self.assertEqual(viewer.pay_year(120000, 160000, "year"), (120000, 160000))
        self.assertEqual(viewer.pay_year(40, 45, "hour"), (83200, 93600))
        self.assertEqual(viewer.pay_year(None, None, "year"), (None, None))
        self.assertEqual(viewer.pay_year(90000, None, None), (90000, None))

    def test_every_job_the_page_gets_carries_it(self):
        row = {"id": "gh:a:1", "score": 80, "title": "Engineer", "company": "Acme", "location": "Austin, TX", "pay_min": 40, "pay_max": 50, "pay_unit": "hour",
               "posted": "2026-11-01", "verdict": "apply", "analysis": "{}", "url": "https://example.com/1", "description": "d", "status": "new", "notes": "",
               "applied_at": None, "formatted": 1}
        job = viewer.job_dict(row)
        self.assertEqual((job["pay_low"], job["pay_high"]), (83200, 104000))
        job = viewer.job_dict({**row, "pay_min": None, "pay_max": None})
        self.assertEqual((job["pay_low"], job["pay_high"]), (None, None))

    def test_every_job_the_page_gets_says_how_the_work_is_done(self):
        row = {"id": "gh:a:1", "score": 80, "title": "Engineer", "company": "Acme", "location": "Remote - US", "pay_min": None, "pay_max": None, "pay_unit": "year",
               "posted": "2026-11-01", "verdict": "apply", "analysis": "{}", "url": "https://example.com/1", "description": "d", "status": "new", "notes": "",
               "applied_at": None, "formatted": 1}
        self.assertEqual(viewer.job_dict(row)["modes"], ["remote"])
        self.assertEqual(viewer.job_dict({**row, "location": "Austin, TX", "description": "We offer a hybrid work schedule."})["modes"], ["hybrid"])
        self.assertEqual(viewer.job_dict({**row, "location": "Austin, TX"})["modes"], [])


class ModesAreRememberedTests(unittest.TestCase):
    def test_a_posting_is_read_once_until_its_text_changes(self):
        from unittest import mock
        viewer._MODES.clear()
        with mock.patch.object(viewer.workmode, "detect", wraps=viewer.workmode.detect) as detect:
            for _ in range(3):
                self.assertEqual(viewer.modes_for("gh:a:1", "Engineer", "Remote - US", "text"), ["remote"])
            self.assertEqual(detect.call_count, 1)
            self.assertEqual(viewer.modes_for("gh:a:1", "Engineer", "Austin, TX", "We offer a hybrid work schedule."), ["hybrid"])   # the posting changed
            self.assertEqual(detect.call_count, 2)

    def test_the_answer_is_a_copy_so_one_caller_cannot_change_what_the_next_gets(self):
        viewer._MODES.clear()
        first = viewer.modes_for("gh:a:2", "Engineer", "Remote", "")
        first.append("onsite")
        self.assertEqual(viewer.modes_for("gh:a:2", "Engineer", "Remote", ""), ["remote"])


class PageMarkupTests(unittest.TestCase):
    def test_the_search_box_filter_button_chips_and_panel_are_on_the_page(self):
        for needle in ('id="q"', 'id="filter-btn"', 'id="chips"', 'id="filters"', "function placeOf", "function passes", "Match score", "Where the work is done", "Always include remote jobs when filtering by place", "Include jobs that don't list a salary"):
            self.assertIn(needle, viewer.PAGE, needle)


NODE = shutil.which("node")
HARNESS = r'''
const el=()=>{};const jobs=[];const hiddenFromList=()=>false;const localStorage={getItem(){return null},setItem(){}};
%(code)s
const out={};
const places=%(places)s;
for(const loc of places)out[loc]={states:[...placeOf(loc).states].sort(),countries:[...placeOf(loc).countries].sort()};
const j=(loc,score,lo,hi)=>({location:loc,score,pay_low:lo,pay_high:hi});
const check=(name,ok)=>{out["check: "+name]=ok};
F.states=new Set(["TX","CA"]);
check("state", passes(j("Austin, TX",80))&&!passes(j("Boston, MA",80))&&passes(j("Boston, MA | Dallas, TX",80)));
F.states=new Set();F.countries=new Set(["Canada"]);
check("country", passes(j("Toronto, ON",70))&&!passes(j("Austin, TX",70)));
F.countries=new Set();F.score=80;
check("score", passes(j("x",80))&&!passes(j("x",79)));
F.score=0;F.salary=100000;F.unlisted=true;
check("salary with unlisted allowed", passes(j("x",50))&&passes(j("x",50,90000,120000))&&!passes(j("x",50,60000,90000)));
F.unlisted=false;
check("salary with unlisted hidden", !passes(j("x",50)));
F.unlisted=true;F.states=new Set(["TX"]);F.score=70;F.salary=100000;
check("all together", passes(j("Austin, TX",75,90000,130000))&&!passes(j("Austin, TX",65,90000,130000))&&!passes(j("Austin, TX",75,50000,70000))&&!passes(j("Reno, NV",75,90000,130000)));
F.states=new Set();F.countries=new Set();F.score=0;F.salary=0;
const rj=(loc,modes)=>({location:loc,score:70,modes:modes||[]});
F.states=new Set(["TX"]);F.remoteEverywhere=true;
check("remote jobs are included in every state", passes(rj("Remote"))&&passes(rj("Remote - US"))&&passes(rj("Boston, MA",["remote"]))&&passes(rj("San Francisco, CA | Remote")));
check("a remote job in another country is not", !passes(rj("Remote - Canada"))&&!passes(rj("London, United Kingdom",["remote"])));
check("an in-person job elsewhere is not", !passes(rj("Boston, MA",["onsite"]))&&!passes(rj("Boston, MA")));
F.remoteEverywhere=false;
check("the remote switch can be turned off", !passes(rj("Remote"))&&!passes(rj("Boston, MA",["remote"]))&&passes(rj("Austin, TX",["remote"])));
F.remoteEverywhere=true;F.states=new Set();F.countries=new Set(["Canada"]);
check("remote jobs with no country are in every country", passes(rj("Remote"))&&passes(rj("Toronto, ON"))&&!passes(rj("Remote - US"))&&!passes(rj("Austin, TX",["remote"])));
F.countries=new Set(["United States"]);
check("remote - US is in the United States", passes(rj("Remote - US"))&&passes(rj("Remote")));
F.countries=new Set();
F.modes=new Set(["hybrid"]);
check("work arrangement", passes(rj("x",["hybrid"]))&&passes(rj("x",["remote","hybrid"]))&&!passes(rj("x",["remote"]))&&!passes(rj("x",[]))&&!passes(rj("x")));
F.modes=new Set(["unstated"]);
check("not stated", passes(rj("x",[]))&&passes(rj("x"))&&!passes(rj("x",["onsite"])));
F.modes=new Set(["remote","onsite"]);
check("any of the arrangements picked", passes(rj("x",["remote"]))&&passes(rj("x",["onsite"]))&&!passes(rj("x",["hybrid"])));
F.modes=new Set(["remote"]);F.states=new Set(["TX"]);F.score=60;
check("arrangement with a place and a score", passes({location:"Remote",score:75,modes:["remote"]})&&!passes({location:"Remote",score:50,modes:["remote"]})&&!passes({location:"Austin, TX",score:75,modes:["onsite"]}));
console.log(JSON.stringify(out));
'''
PLACES = ["Austin, TX", "Remote - US", "San Francisco, CA | Remote", "Peoria, AZ, Surprise, AZ", "United States", "Multiple Locations", "Toronto, ON", "Vancouver, BC, Canada",
          "London, United Kingdom", "Berlin, Germany", "Washington, DC", "Seattle, Washington", "West Virginia", "Virginia Beach, VA", "Remote", "", "New York, NY or Boston, MA",
          "Remote - Canada", "Chicago, Illinois", "Leavenworth County, KS"]


@unittest.skipUnless(NODE, "Node is not installed")
class PlaceAndFilterLogicTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        page = viewer.PAGE
        code = page[page.index("const STATE_NAMES="):page.index("function clearFilters()")].replace("const money=n=>", "const money_unused=n=>")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "harness.js"
            path.write_text(HARNESS % {"code": code, "places": json.dumps(PLACES)}, encoding="utf-8")
            done = subprocess.run([NODE, str(path)], capture_output=True, text=True, timeout=60)
        if done.returncode:
            raise RuntimeError(done.stderr[:1000])
        cls.out = json.loads(done.stdout)

    def where(self, loc):
        return self.out[loc]["states"], self.out[loc]["countries"]

    def test_states_and_countries_are_read_from_location_lines(self):
        self.assertEqual(self.where("Austin, TX"), (["TX"], ["United States"]))
        self.assertEqual(self.where("San Francisco, CA | Remote"), (["CA"], ["United States"]))
        self.assertEqual(self.where("Peoria, AZ, Surprise, AZ"), (["AZ"], ["United States"]))
        self.assertEqual(self.where("New York, NY or Boston, MA"), (["MA", "NY"], ["United States"]))
        self.assertEqual(self.where("Chicago, Illinois"), (["IL"], ["United States"]))
        self.assertEqual(self.where("Leavenworth County, KS"), (["KS"], ["United States"]))

    def test_the_longer_state_name_wins_and_washington_dc_is_not_the_state(self):
        self.assertEqual(self.where("West Virginia"), (["WV"], ["United States"]))
        self.assertEqual(self.where("Seattle, Washington"), (["WA"], ["United States"]))
        self.assertEqual(self.where("Washington, DC"), (["DC"], ["United States"]))

    def test_other_countries(self):
        self.assertEqual(self.where("Toronto, ON"), ([], ["Canada"]))
        self.assertEqual(self.where("Vancouver, BC, Canada"), ([], ["Canada"]))
        self.assertEqual(self.where("London, United Kingdom"), ([], ["United Kingdom"]))
        self.assertEqual(self.where("Berlin, Germany"), ([], ["Germany"]))
        self.assertEqual(self.where("Remote - Canada"), ([], ["Canada"]))

    def test_a_country_alone_or_nothing_at_all(self):
        self.assertEqual(self.where("Remote - US"), ([], ["United States"]))
        self.assertEqual(self.where("United States"), ([], ["United States"]))
        for none in ("Multiple Locations", "Remote", ""):
            self.assertEqual(self.where(none), ([], []), none)

    def test_each_filter_and_all_of_them_together(self):
        for name, ok in self.out.items():
            if name.startswith("check: "):
                self.assertTrue(ok, name)


if __name__ == "__main__":
    unittest.main()
