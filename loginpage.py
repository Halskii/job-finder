"""The sign-in / create-account page, in the same dark and light styles as the rest of the site."""
from __future__ import annotations

import json

# Google's sign-in button loads a script and a frame from Google, so this page (and only this page) allows them.
CSP = ("default-src 'none'; script-src 'unsafe-inline' https://accounts.google.com/gsi/client; "
       "style-src 'unsafe-inline' https://accounts.google.com/gsi/style; connect-src 'self' https://accounts.google.com/gsi/; "
       "frame-src https://accounts.google.com/gsi/; img-src data: https:; base-uri 'none'; form-action 'self'")

TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign in</title>
<script>try{if(localStorage.getItem("theme")==="light")document.documentElement.dataset.theme="light"}catch(e){}</script>
<style>
:root{color-scheme:dark;--bg:#202124;--panel:#292a2d;--ink:#e8eaed;--mute:#9aa0a6;--line:#3c4043;--accent:#8ab4f8;--bad:#f28b82;--on-accent:#202124}
:root[data-theme="light"]{color-scheme:light;--bg:#f8f9fa;--panel:#fff;--ink:#202124;--mute:#5f6368;--line:#dadce0;--accent:#1a73e8;--bad:#d93025;--on-accent:#fff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;min-height:100vh;display:grid;place-items:center;padding:16px}
main{width:min(400px,100%);background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:28px 26px}
h1{margin:0 0 4px;font-size:22px}p.sub{margin:0 0 20px;color:var(--mute)}
.tabs{display:grid;grid-template-columns:1fr 1fr;border:1px solid var(--line);border-radius:8px;padding:3px;margin-bottom:18px}
.tabs button{border:0;background:none;color:var(--mute);font:inherit;padding:7px;border-radius:6px;cursor:pointer}
.tabs button[aria-selected="true"]{background:var(--accent);color:var(--on-accent);font-weight:600}
label{display:block;font-weight:600;margin:12px 0 4px}
input{width:100%;padding:9px 10px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--ink);font:inherit}
input:focus{outline:2px solid var(--accent);outline-offset:1px}
.hint{font-size:12px;color:var(--mute);margin-top:3px}
button.go{width:100%;margin-top:18px;padding:10px;border:0;border-radius:6px;background:var(--accent);color:var(--on-accent);font:inherit;font-weight:600;cursor:pointer}
button.go:disabled{opacity:.6;cursor:default}
#err{margin-top:14px;color:var(--bad);font-size:14px;min-height:1.4em}
.or{display:flex;align-items:center;gap:10px;margin:18px 0 14px;color:var(--mute);font-size:13px}
.or::before,.or::after{content:"";flex:1;border-top:1px solid var(--line)}
#google{display:flex;justify-content:center;min-height:44px}
[hidden]{display:none!important}
</style></head><body>
<main>
<h1>Job Finder</h1>
<p class="sub">Sign in to see your matches and track your applications.</p>
<div class="tabs" role="tablist">
<button type="button" role="tab" id="tab-in" aria-selected="true">Sign in</button>
<button type="button" role="tab" id="tab-up" aria-selected="false">Create account</button>
</div>
<form id="form" novalidate>
<div id="name-row" hidden><label for="name">Your name</label><input id="name" autocomplete="name" maxlength="80"></div>
<label for="email">Email</label><input id="email" type="email" autocomplete="email" maxlength="320" required>
<label for="password">Password</label><input id="password" type="password" autocomplete="current-password" maxlength="128" required>
<div class="hint" id="pw-hint" hidden>At least 10 characters.</div>
<button class="go" id="go" type="submit">Sign in</button>
<div id="err" role="alert"></div>
</form>
<div id="google-box" hidden><div class="or">or</div><div id="google"></div></div>
</main>
<script>
const CLIENT_ID=__CLIENT_ID__;
const $=s=>document.querySelector(s);
let mode="in";
function setMode(m){
  mode=m;
  $("#tab-in").setAttribute("aria-selected",String(m==="in"));$("#tab-up").setAttribute("aria-selected",String(m==="up"));
  $("#name-row").hidden=m!=="up";$("#pw-hint").hidden=m!=="up";
  $("#go").textContent=m==="in"?"Sign in":"Create account";
  $("#password").autocomplete=m==="in"?"current-password":"new-password";
  $("#err").textContent="";
}
$("#tab-in").onclick=()=>setMode("in");$("#tab-up").onclick=()=>setMode("up");
async function send(path,body){
  $("#err").textContent="";
  let r,d;
  try{r=await fetch(path,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});d=await r.json().catch(()=>null)}
  catch(e){$("#err").textContent="Could not reach the server. Check your connection and try again.";return false}
  if(!r.ok){$("#err").textContent=(d&&d.error)||"Something went wrong. Try again.";return false}
  location.href="/";return true;
}
$("#form").addEventListener("submit",async e=>{
  e.preventDefault();
  const body={email:$("#email").value.trim(),password:$("#password").value};
  if(mode==="up")body.name=$("#name").value.trim();
  $("#go").disabled=true;
  try{await send(mode==="in"?"/auth/login":"/auth/signup",body)}finally{$("#go").disabled=false}
});
if(CLIENT_ID){
  $("#google-box").hidden=false;
  const s=document.createElement("script");s.src="https://accounts.google.com/gsi/client";s.async=true;
  s.onload=()=>{
    google.accounts.id.initialize({client_id:CLIENT_ID,callback:r=>send("/auth/google",{credential:r.credential})});
    google.accounts.id.renderButton($("#google"),{theme:"outline",size:"large",text:"continue_with",width:300});
  };
  document.head.append(s);
}
</script></body></html>
"""


def render(google_client_id: str) -> str:
    """The page, with the Google client id (a public value) filled in, or left out when sign-in with Google is not set up."""
    return TEMPLATE.replace("__CLIENT_ID__", json.dumps(google_client_id or "").replace("</", "<\\/"))


__all__ = ["render", "CSP"]
