"""The profile page: where a person gives their resume, reviews the job titles Claude suggests, and sets their search limits."""
from __future__ import annotations

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Your profile</title>
<script>try{if(localStorage.getItem("theme")==="light")document.documentElement.dataset.theme="light"}catch(e){}</script>
<style>
:root{color-scheme:dark;--bg:#202124;--panel:#292a2d;--ink:#e8eaed;--mute:#9aa0a6;--line:#3c4043;--accent:#8ab4f8;--good:#81c995;--bad:#f28b82;--on-accent:#202124}
:root[data-theme="light"]{color-scheme:light;--bg:#f8f9fa;--panel:#fff;--ink:#202124;--mute:#5f6368;--line:#dadce0;--accent:#1a73e8;--good:#188038;--bad:#d93025;--on-accent:#fff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
header{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:12px 22px;border-bottom:1px solid var(--line);background:var(--panel)}
header a{color:var(--accent);text-decoration:none}
main{max-width:760px;margin:0 auto;padding:22px 16px 80px}
h1{margin:6px 0 4px;font-size:24px}p.lead{margin:0 0 20px;color:var(--mute)}
section{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px 20px;margin-bottom:16px}
section h2{margin:0 0 2px;font-size:17px}section p.hint{margin:0 0 12px;color:var(--mute);font-size:13px}
label{display:block;font-weight:600;margin:14px 0 4px}label small{font-weight:400;color:var(--mute)}
input[type=text],input[type=number],textarea,select{width:100%;padding:8px 10px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--ink);font:inherit}
textarea{min-height:90px;resize:vertical}
input:focus,textarea:focus,select:focus{outline:2px solid var(--accent);outline-offset:1px}
.row{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:600px){.row{grid-template-columns:1fr}}
.btn{padding:8px 14px;border:1px solid var(--line);border-radius:6px;background:var(--panel);color:var(--ink);font:inherit;cursor:pointer}
.btn:hover{border-color:var(--accent)}.btn.primary{background:var(--accent);border-color:var(--accent);color:var(--on-accent);font-weight:600}
.btn:disabled{opacity:.6;cursor:default}
.tabs{display:inline-grid;grid-auto-flow:column;border:1px solid var(--line);border-radius:8px;padding:3px;margin-bottom:12px}
.tabs button{border:0;background:none;color:var(--mute);font:inherit;padding:6px 14px;border-radius:6px;cursor:pointer}
.tabs button[aria-selected="true"]{background:var(--accent);color:var(--on-accent);font-weight:600}
.status{margin:10px 0 0;font-size:14px}.status.ok{color:var(--good)}.status.err{color:var(--bad)}
.banner{border:1px solid var(--accent);border-radius:8px;padding:10px 14px;margin-bottom:16px;font-size:14px}
.actions{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:16px}
.check{display:flex;gap:8px;align-items:center;font-weight:600;margin:14px 0 4px}.check input{width:auto}
.spinner{display:inline-block;width:13px;height:13px;border:2px solid currentColor;border-right-color:transparent;border-radius:50%;animation:spin .8s linear infinite;vertical-align:-2px;margin-right:6px}
@keyframes spin{to{transform:rotate(360deg)}}
[hidden]{display:none!important}
</style></head><body>
<header><a href="/">&larr; Back to your jobs</a><span id="who" class="hint"></span></header>
<main>
<h1>Your profile</h1>
<p class="lead">Tell us about your experience and we will suggest the job titles to search for. Everything is yours to edit, and nothing is saved until you press Save.</p>
<div class="banner" id="banner" hidden></div>

<section id="s-resume">
<h2>1. Your resume</h2>
<p class="hint" id="resume-state">No resume yet.</p>
<div class="tabs" role="tablist"><button type="button" id="tab-pdf" aria-selected="true">Upload a PDF</button><button type="button" id="tab-text" aria-selected="false">Paste or describe</button></div>
<div id="pane-pdf"><input type="file" id="file" accept="application/pdf,.pdf"><div class="hint" style="margin-top:6px;color:var(--mute);font-size:13px">A PDF of at most 5 MB, with text you can select (not a scan).</div>
<div class="actions"><button class="btn" id="upload">Upload</button></div></div>
<div id="pane-text" hidden><textarea id="text" rows="9" maxlength="30000" placeholder="Paste your resume here, or write a few sentences about your jobs, how long you did them, and the tools and skills you use."></textarea>
<div class="actions"><button class="btn" id="save-text">Save text</button></div></div>
<div class="status" id="resume-status" role="status"></div>
<div class="actions"><button class="btn primary" id="analyze" disabled>Read my resume and suggest job titles</button><span class="hint" style="color:var(--mute);font-size:13px">Takes about 15 seconds.</span></div>
<div class="status" id="analyze-status" role="status"></div>
</section>

<section>
<h2>2. Job titles to search for</h2>
<p class="hint">One per line. Add or remove any. Levels such as I, II, Junior and Associate are ignored when matching, so "Software Engineer" also finds "Software Engineer II".</p>
<textarea id="titles" rows="9" spellcheck="false"></textarea>
<label for="exclude">Skip titles containing these words <small>(separate with commas)</small></label>
<input type="text" id="exclude" autocomplete="off">
</section>

<section>
<h2>3. Search limits</h2>
<p class="hint">These decide which postings are kept for you.</p>
<div class="row">
<div><label for="min_pay">Minimum yearly pay in USD <small>(0 = no minimum; postings that do not list pay are kept)</small></label><input type="number" id="min_pay" min="0" max="1000000" step="1000"></div>
<div><label for="max_age">Show postings from the last</label><select id="max_age"><option value="7">7 days</option><option value="14">14 days</option><option value="30">30 days</option><option value="60">60 days</option><option value="90">90 days</option></select></div>
</div>
<label class="check"><input type="checkbox" id="us_only"> Only jobs in the United States</label>
<label for="exclude_locations">Skip jobs in these places <small>(commas; a state, a city, or "Remote - Canada")</small></label>
<input type="text" id="exclude_locations" autocomplete="off">
</section>

<section>
<h2>4. About you</h2>
<p class="hint">Claude uses these facts, and only these, when it scores jobs and writes your resume and cover letters.</p>
<div class="row">
<div><label for="name">Name</label><input type="text" id="name" maxlength="80" autocomplete="name"></div>
<div><label for="location">Where you live</label><input type="text" id="location" maxlength="80" placeholder="Austin, TX"></div>
</div>
<label for="headline">One-line summary</label><input type="text" id="headline" maxlength="300">
<div class="row">
<div><label for="years">Years of experience</label><input type="number" id="years" min="0" max="60"></div>
<div><label for="education">Education</label><input type="text" id="education" maxlength="200"></div>
</div>
<div class="row">
<div><label for="work_auth">Work authorization</label><input type="text" id="work_auth" maxlength="120" list="auth-list" placeholder="U.S. citizen"><datalist id="auth-list"><option value="U.S. citizen"><option value="Permanent resident (green card)"><option value="Authorized to work, no sponsorship needed"><option value="Need visa sponsorship"></datalist></div>
<div><label for="clearance">Security clearance</label><input type="text" id="clearance" maxlength="120" placeholder="None"></div>
</div>
<label for="relocation">Relocation</label><input type="text" id="relocation" maxlength="120" placeholder="Open to relocating for a strong offer">
</section>

<section>
<h2>5. Skills</h2>
<p class="hint">One per line. Only list what is true: these are the facts Claude is allowed to use.</p>
<div class="row">
<div><label for="strong">Skills you use confidently</label><textarea id="strong" rows="8" spellcheck="false"></textarea></div>
<div><label for="familiar">Skills you are still learning</label><textarea id="familiar" rows="8" spellcheck="false"></textarea></div>
</div>
<label for="dna">Things jobs ask for that you do not have <small>(optional: so they are never claimed for you)</small></label><textarea id="dna" rows="3" spellcheck="false" placeholder="Go&#10;Kafka&#10;a held security clearance"></textarea>
<label for="certs">Certifications</label><textarea id="certs" rows="3" spellcheck="false"></textarea>
<label for="achievements">Things you built or improved <small>(one sentence each, with real numbers only)</small></label><textarea id="achievements" rows="5" spellcheck="false"></textarea>
</section>

<div class="actions"><button class="btn primary" id="save">Save and see my jobs</button><button class="btn" id="skip" hidden>Skip for now</button></div>
<div class="status" id="save-status" role="status"></div>
</main>
<script>
const $=s=>document.querySelector(s);
const lines=v=>v.split("\n").map(s=>s.trim()).filter(Boolean);
const commas=v=>v.split(/[,\n]/).map(s=>s.trim()).filter(Boolean);
const NETWORK="We could not reach the server. Check your connection and try again.";
function say(box,text,kind){const b=$(box);b.textContent=text;b.className="status"+(kind?" "+kind:"")}
async function api(path,opts){
  let r;try{r=await fetch(path,opts)}catch(e){throw new Error(NETWORK)}
  const d=await r.json().catch(()=>null);
  if(r.status===401&&d&&d.code==="not_signed_in"){location.href="/login";throw new Error("Your session ended. Sign in again.")}
  if(!r.ok)throw new Error((d&&d.error)||"Something went wrong. Try again.");
  return d;
}
const post=(p,b)=>api(p,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(b||{})});
async function busy(btn,label,fn){
  const old=btn.textContent;btn.disabled=true;btn.replaceChildren(Object.assign(document.createElement("span"),{className:"spinner"}),label);
  try{return await fn()}finally{btn.textContent=old;btn.disabled=false}
}
function showResume(r){
  $("#resume-state").textContent=r.kind==="pdf"?"Resume on file: a PDF.":r.kind==="text"?"Resume on file: text you entered.":"No resume yet.";
  $("#analyze").disabled=!r.kind;
}
function fill(settings,profile){
  $("#titles").value=(settings.titles||[]).join("\n");
  $("#exclude").value=(settings.exclude_title_words||[]).join(", ");
  $("#min_pay").value=settings.min_pay||0;
  $("#max_age").value=String(settings.max_age_days||30);
  if($("#max_age").value!==String(settings.max_age_days)){$("#max_age").add(new Option(settings.max_age_days+" days",String(settings.max_age_days)));$("#max_age").value=String(settings.max_age_days)}
  $("#us_only").checked=settings.us_only!==false;
  $("#exclude_locations").value=(settings.exclude_locations||[]).join(", ");
  setProfile(profile||{},true);
}
function setProfile(p,all){
  const set=(id,v)=>{if(v!==undefined&&v!==null)$(id).value=v};
  if(all||!$("#name").value)set("#name",p.name);
  if(all){set("#location",p.location);set("#work_auth",p.work_authorization);set("#clearance",p.security_clearance);set("#relocation",p.relocation);
    set("#dna",(p.do_not_assume||[]).join("\n"))}
  set("#headline",p.headline);set("#years",p.years_experience);set("#education",p.education);
  set("#strong",(p.strong_skills||[]).join("\n"));set("#familiar",(p.familiar_skills||[]).join("\n"));
  set("#certs",(p.certifications||[]).join("\n"));set("#achievements",(p.achievements||[]).join("\n"));
}
function collect(){
  return {
    settings:{titles:lines($("#titles").value),exclude_title_words:commas($("#exclude").value),min_pay:Number($("#min_pay").value)||0,
      max_age_days:Number($("#max_age").value)||30,us_only:$("#us_only").checked,exclude_locations:commas($("#exclude_locations").value)},
    profile:{name:$("#name").value,headline:$("#headline").value,years_experience:Number($("#years").value)||0,education:$("#education").value,
      location:$("#location").value,work_authorization:$("#work_auth").value,security_clearance:$("#clearance").value,relocation:$("#relocation").value,
      certifications:lines($("#certs").value),strong_skills:lines($("#strong").value),familiar_skills:lines($("#familiar").value),
      do_not_assume:lines($("#dna").value),achievements:lines($("#achievements").value),best_fit_notes:window.bestFit||[]}
  };
}
function mode(m){
  $("#tab-pdf").setAttribute("aria-selected",String(m==="pdf"));$("#tab-text").setAttribute("aria-selected",String(m==="text"));
  $("#pane-pdf").hidden=m!=="pdf";$("#pane-text").hidden=m!=="text";
}
$("#tab-pdf").onclick=()=>mode("pdf");$("#tab-text").onclick=()=>mode("text");

$("#upload").onclick=()=>{
  const f=$("#file").files[0];
  if(!f){say("#resume-status","Choose a PDF file first.","err");return}
  busy($("#upload"),"Uploading",async()=>{
    try{const d=await api("/api/resume",{method:"PUT",headers:{"Content-Type":"application/pdf"},body:f});
      showResume({kind:"pdf"});say("#resume-status","Saved ("+d.pages+(d.pages===1?" page":" pages")+"). Now press the blue button to read it.","ok")}
    catch(e){say("#resume-status",e.message,"err")}
  });
};
$("#save-text").onclick=()=>busy($("#save-text"),"Saving",async()=>{
  try{await post("/api/resume-text",{text:$("#text").value});showResume({kind:"text"});say("#resume-status","Saved. Now press the blue button to read it.","ok")}
  catch(e){say("#resume-status",e.message,"err")}
});
$("#analyze").onclick=()=>busy($("#analyze"),"Reading your resume",async()=>{
  say("#analyze-status","");
  try{
    const d=await post("/api/profile/analyze");
    const s=d.suggestions;
    $("#titles").value=s.titles.join("\n");$("#exclude").value=s.exclude_words.join(", ");
    if(!$("#name").value&&s.name)$("#name").value=s.name;
    setProfile(d.profile,false);
    const b=$("#banner");b.hidden=false;b.textContent="We read your resume. Check the job titles and the facts below, change anything that is wrong, then press Save at the bottom.";
    say("#analyze-status","Done: "+s.titles.length+" job titles suggested.","ok");
    $("#s-resume").scrollIntoView({behavior:"smooth",block:"start"});$("#titles").focus({preventScroll:true});
  }catch(e){say("#analyze-status",e.message,"err")}
});
$("#save").onclick=()=>busy($("#save"),"Saving",async()=>{
  const body=collect();
  if(!body.settings.titles.length){say("#save-status","Add at least one job title to search for.","err");return}
  try{const d=await post("/api/profile/save",body);
    if(d.notes&&d.notes.length){say("#save-status",d.notes.join(" "),"ok");setTimeout(()=>location.href="/",3500)}else location.href="/"}
  catch(e){say("#save-status",e.message,"err")}
});
$("#skip").onclick=async()=>{try{await post("/api/profile/skip");location.href="/"}catch(e){say("#save-status",e.message,"err")}};

(async()=>{
  try{
    const d=await api("/api/profile");
    $("#who").textContent=d.account.email;
    showResume(d.resume);fill(d.settings,d.profile||{name:d.account.name});
    window.bestFit=(d.profile&&d.profile.best_fit_notes)||[];
    $("#skip").hidden=d.set_up;
    if(!d.set_up){const b=$("#banner");b.hidden=false;b.textContent="Welcome. Start with your resume below: we will suggest job titles for you to review."}
  }catch(e){say("#save-status",e.message,"err")}
})();
</script></body></html>
"""
