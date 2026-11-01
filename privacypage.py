"""The privacy page: what the alpha stores about a person, what it sends to Claude, and how to delete it all. Plain words, no tracking."""
from __future__ import annotations

import html

TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Privacy</title>
<script>try{if(localStorage.getItem("theme")==="light")document.documentElement.dataset.theme="light"}catch(e){}</script>
<style>
:root{color-scheme:dark;--bg:#202124;--panel:#292a2d;--ink:#e8eaed;--mute:#9aa0a6;--line:#3c4043;--accent:#8ab4f8}
:root[data-theme="light"]{color-scheme:light;--bg:#f8f9fa;--panel:#fff;--ink:#202124;--mute:#5f6368;--line:#dadce0;--accent:#1a73e8}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.6 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:720px;margin:0 auto;padding:28px 16px 60px}
a{color:var(--accent)}h1{margin:8px 0 4px}h2{font-size:18px;margin:26px 0 6px}p.lead{color:var(--mute);margin-top:0}
ul{padding-left:20px}li{margin:4px 0}.note{border:1px solid var(--line);background:var(--panel);border-radius:10px;padding:12px 16px;margin:18px 0}
</style></head><body><main>
<p><a href="/login">&larr; Back</a></p>
<h1>Privacy</h1>
<p class="lead">This is an early alpha, run by one person. Here is plainly what it does with your information.</p>
<div class="note">Jobs, scores and write-ups come from automated systems and can be wrong or out of date. Check the original posting before you apply.</div>

<h2>What is stored about you</h2>
<ul>
<li>Your email address and name (from sign-up or from Google), and a scrambled form of your password if you chose one. Google sign-in gives us only your email and name.</li>
<li>The resume you upload or paste, the profile facts you confirm, and your search settings (titles, places, pay).</li>
<li>The jobs found for you, their scores, your saved, discarded and applied jobs, your notes, interview rounds and interview prep.</li>
<li>The resumes and cover letters made for you, and counts of how many AI requests you have made each day (to keep costs in check).</li>
</ul>

<h2>What is sent to Claude (Anthropic)</h2>
<ul>
<li>To score jobs, read your resume, write tailored resumes and cover letters, and prepare for interviews, your resume text, your profile facts and the job postings are sent to Anthropic's Claude. Email addresses, phone numbers and links are removed from your resume text first.</li>
<li>Interview prep can also search the web using the company and role names.</li>
<li>Anthropic handles that data under its own terms for API customers.</li>
</ul>

<h2>What is not done</h2>
<ul>
<li>Your information is not sold, and it is not shown to other users. Each account only ever sees its own jobs, notes and files.</li>
<li>There are no advertising or tracking cookies. The site sets one cookie to keep you signed in, and your browser remembers your light or dark choice.</li>
</ul>

<h2>Deleting your data</h2>
<p>Press <strong>Delete my account</strong> at the bottom of your <a href="/profile">Profile</a> page. Your account, resume, profile, jobs, notes and generated files are deleted right away. Automatic database backups may keep a copy for up to 7 days before they expire.</p>
__CONTACT__
</main></body></html>
"""


def render(contact_email: str = "") -> str:
    """The page. `contact_email` (the CONTACT_EMAIL setting) is shown as the place to ask questions, when there is one."""
    contact = ""
    if contact_email.strip():
        e = html.escape(contact_email.strip())
        contact = f'<h2>Questions</h2>\n<p>Write to <a href="mailto:{e}">{e}</a>.</p>'
    return TEMPLATE.replace("__CONTACT__", contact)
