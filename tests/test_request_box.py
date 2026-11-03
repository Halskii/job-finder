"""The request box of Tailor resume and Cover letter. The resume box opens filled in with the job's resume angle (what scoring found worth
emphasizing for that posting) and the person can change or clear it; the cover letter box opens empty. The page's own askDoc is run under Node."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import viewer

NODE = shutil.which("node")
HARNESS = r'''
const mk=(extra)=>Object.assign({textContent:"",value:"",placeholder:"",hidden:false,focus(){this.focused=true},setSelectionRange(a,b){this.caret=[a,b]}},extra||{});
const form={onsubmit:null};
const elements={"#tailor-dlg":mk({querySelector:()=>form,showModal(){this.open=true}}),"#tailor-request":mk(),"#tailor-title":mk(),"#tailor-go":mk(),
                "#tailor-for":mk(),"#tailor-hint":mk({hidden:true})};
const $=sel=>elements[sel];
let cleaned=0;const requestBox=()=>{cleaned++};
const made=[];const makeDoc=(kind,j,request)=>made.push([kind,j.id,request]);
%(code)s
const box=elements["#tailor-request"],hint=elements["#tailor-hint"],out={};
const snap=()=>({value:box.value,hint:hint.hidden?"":hint.textContent,hintHidden:hint.hidden,title:elements["#tailor-title"].textContent,go:elements["#tailor-go"].textContent,
                 forWhom:elements["#tailor-for"].textContent,placeholder:box.placeholder,focused:!!box.focused,caret:box.caret,open:elements["#tailor-dlg"].open});
const job=(id,angle)=>({id,title:"Cloud Engineer",company:"Centene",resume_angle:angle});
const submit=value=>form.onsubmit({submitter:{value}});

askDoc("resume",job("a1","  Lead with the Terraform and Azure platform work.  "));
out.resumeOpens=snap();
submit("ok");                                   // sent as it was filled in
askDoc("resume",job("a1","Lead with the Terraform and Azure platform work."));
box.value="Lead with my Kubernetes work instead";submit("ok");      // changed by the person
askDoc("resume",job("a1","Lead with the Terraform and Azure platform work."));
box.value="";submit("ok");                      // cleared: the defaults
askDoc("resume",job("a1","Lead with the Terraform and Azure platform work."));
submit("cancel");                               // cancelled: nothing is made
out.made=made.slice();
askDoc("letter",job("a1","Lead with the Terraform and Azure platform work."));
out.letterOpens=snap();
askDoc("resume",job("b2",""));
out.noAngle=snap();
askDoc("resume",{id:"c3",title:"T",company:"C"});
out.noField=snap();
askDoc("resume",job("d4","   "));
out.blank=snap();
askDoc("resume",job("e5","Lead with X."));
askDoc("resume",job("f6",""));                  // the next job does not inherit the last job's text
out.afterAnother=snap();
out.cleaned=cleaned;
console.log(JSON.stringify(out));
'''


@unittest.skipUnless(NODE, "Node is not installed")
class RequestBoxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        page = viewer.PAGE
        code = page[page.index("const DOCS={"):page.index("const REQUEST_MAX")] + page[page.index("function askDoc("):page.index("async function makeDoc(")]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "harness.js"
            path.write_text(HARNESS % {"code": code}, encoding="utf-8")
            done = subprocess.run([NODE, str(path)], capture_output=True, text=True, timeout=60)
        if done.returncode:
            raise RuntimeError(done.stderr[:1500])
        cls.out = json.loads(done.stdout)

    def test_the_resume_box_opens_with_the_resume_angle_ready_to_edit(self):
        s = self.out["resumeOpens"]
        self.assertEqual(s["value"], "Lead with the Terraform and Azure platform work.")      # trimmed
        self.assertTrue(s["open"] and s["focused"])
        self.assertEqual(s["caret"], [len(s["value"]), len(s["value"])])                       # the cursor is after the text, to add to it
        self.assertEqual((s["title"], s["go"], s["forWhom"]), ("Tailor resume", "Tailor", "Cloud Engineer at Centene"))

    def test_it_says_where_the_text_came_from_and_how_to_change_it(self):
        s = self.out["resumeOpens"]
        self.assertFalse(s["hintHidden"])
        self.assertIn("resume angle", s["hint"])
        self.assertIn("clear the box to use the defaults", s["hint"])

    def test_what_is_sent_is_whatever_the_box_holds_when_the_person_confirms(self):
        self.assertEqual(self.out["made"], [["resume", "a1", "Lead with the Terraform and Azure platform work."],     # left as it was
                                            ["resume", "a1", "Lead with my Kubernetes work instead"],               # changed
                                            ["resume", "a1", ""]])                                                   # cleared: the defaults, as before
        # and cancelling made nothing: the fourth open ended with Cancel

    def test_the_cover_letter_box_still_opens_empty_even_for_a_job_with_an_angle(self):
        s = self.out["letterOpens"]
        self.assertEqual((s["value"], s["hintHidden"], s["title"]), ("", True, "Cover letter"))
        self.assertIn("relocating", s["placeholder"])

    def test_a_job_with_no_angle_gets_an_empty_box_and_no_hint(self):
        for name in ("noAngle", "noField", "blank"):
            s = self.out[name]
            self.assertEqual((s["value"], s["hintHidden"], s["hint"]), ("", True, ""), name)
            self.assertIn("Kubernetes", s["placeholder"], name)                                   # the example text shows when it is empty

    def test_the_next_job_does_not_inherit_the_last_jobs_text(self):
        self.assertEqual((self.out["afterAnother"]["value"], self.out["afterAnother"]["hintHidden"]), ("", True))

    def test_the_counter_and_cleaning_run_each_time_it_opens(self):
        self.assertEqual(self.out["cleaned"], 10)      # the harness opens the dialog ten times


class PageAndDataTests(unittest.TestCase):
    def test_the_dialog_has_a_place_for_the_hint(self):
        self.assertIn('id="tailor-hint"', viewer.PAGE)
        self.assertIn("start:j=>(j.resume_angle||\"\").trim()", viewer.PAGE)

    def test_every_job_the_page_gets_carries_its_resume_angle(self):
        row = {"id": "gh:a:1", "score": 80, "title": "Engineer", "company": "Acme", "location": "Austin, TX", "pay_min": None, "pay_max": None, "pay_unit": "year",
               "posted": "2026-11-01", "verdict": "apply", "url": "https://example.com/1", "description": "d", "status": "new", "notes": "", "applied_at": None,
               "formatted": 1, "analysis": json.dumps({"resume_angle": "Lead with the platform work."})}
        self.assertEqual(viewer.job_dict(row)["resume_angle"], "Lead with the platform work.")
        self.assertEqual(viewer.job_dict({**row, "analysis": "{}"})["resume_angle"], "")


if __name__ == "__main__":
    unittest.main()
