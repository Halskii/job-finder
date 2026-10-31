# job-finder

A small pipeline that finds job postings that fit you, filters them, scores each one against a **verified profile** and your resume with Claude, shows the best ones in a local web page, and tracks the jobs you apply to. It never applies for you; you review and submit.

> The source is public for review (for example by prospective employers). It is not licensed for reuse; see [License](#license).

```
company job boards, remote feed, government sites
(Greenhouse, Lever, Ashby, SmartRecruiters, Workday, Remotive, USAJobs, Texas HHS)
   -> title / location / pay / age filters -> de-duplication -> keyword pre-rank
   -> Claude fit scoring -> report (reports/*.md) -> web viewer + application tracker (jobs.db)
```

## Daily use

1. `python main.py run` fetches, filters, scores, and writes today's report. (`--no-llm` fetches and filters without using any API credits.)
2. `python viewer.py` opens the report in your browser. Click a job to read it, **Open original posting** to apply on the real page, then **Mark as applied** to log it.
3. The **Tracker** tab shows how many places you have applied to this week, this month, and all time, and lets you update each one as you hear back.

## How scoring works

1. **Filters (free, deterministic).** Titles from `config.yaml` (level markers like I / II / III / Associate / Junior are optional, so "Software Engineer I" also matches "Software Engineer"), excluded words (senior, staff, lead, ...), U.S. locations, excluded locations, posting age, and minimum pay. Pay is kept when unknown; a known maximum below `min_pay` drops the job.
2. **Keyword pre-score (free).** Counts how many of your skills appear in the posting and subtracts points for anything in your `do_not_assume` list. It decides which jobs Claude scores first. It only checks presence, so order never matters.
3. **Claude fit score (paid, cached).** For the top candidates, Claude reads your profile, your resume, your pay minimum, and the posting, and returns a 0-100 score, a verdict (`apply`, `maybe`, `skip`), matched skills, missing required skills, dealbreakers, and a suggested resume angle. Scores are cached, so a job is scored once.

Rules baked into the scorer: only facts in your profile count as true, anything in `do_not_assume` is a firm no, the profile and resume are treated as an **unordered** set of facts (position and section order carry no meaning), and a clearance that will be obtained after hire is not a dealbreaker. Bands: 85+ clearly meets nearly all required items, 60-84 is a reasonable stretch, under 40 is a poor fit.

## Setup (Windows)

```powershell
git clone https://github.com/Halskii/job-finder.git
cd job-finder
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env                    # then paste your key after ANTHROPIC_API_KEY=
copy config.example.yaml config.yaml      # your settings: titles, pay floor, locations, companies
copy profile.example.json profile.json    # your facts; edit it, then run `python main.py profile`
```

`config.yaml`, `profile.json`, and `.env` are git-ignored, so your personal settings never get committed; the `*.example.*` files are the shared templates. Without them the program stops with a message saying which file to copy.

Put your resume in the project folder as `resume.pdf` (a `resume.txt` or `resume.md` also works; the PDF is preferred if several exist). Use your most general resume, not a tailored one.

`.env` holds your keys and is never committed:

| Key | Needed for |
|---|---|
| `ANTHROPIC_API_KEY` | Scoring and `main.py profile`. Without it, `run` still fetches and stores jobs |
| `USAJOBS_API_KEY` and `USAJOBS_EMAIL` | Optional. Federal jobs from USAJobs (see "Government jobs") |
| `ADZUNA_APP_ID` and `ADZUNA_APP_KEY` | Optional. The Adzuna feed (see "Adding more boards") |
| `JOOBLE_API_KEY` | Optional. The Jooble feed |

## Commands

You can call the virtual environment's Python directly without activating it, for example `.venv/Scripts/python.exe main.py run`. If a run stops with `No module named 'anthropic'` (or the program says the package is not installed for the Python running it), you started it with a different Python than the one in `.venv`. Use the command above, or activate the environment first. The program checks this before fetching anything, so no time is lost.

| Command | What it does |
|---|---|
| `python main.py profile` | Reads `resume.pdf`, extracts a profile with Claude, shows a diff against `profile.json`, and asks before writing |
| `python main.py profile --apply` | Same, but writes without asking |
| `python main.py run --no-llm` | Fetch and filter only. Free. Good first check |
| `python main.py run` | Full run: fetch, filter, score with Claude, write `reports/YYYY-MM-DD.md` |
| `python main.py run --limit 10` | Full run, but score at most 10 jobs |
| `python main.py list --top 15` | Best scored jobs from the database (`--min-score N` to change the cutoff) |
| `python main.py mark <id> applied "note"` | Set a job's status. `<id>` can be any unique fragment of a job id. `applied` also records today as the date applied |
| `python main.py discover Stripe "Palo Alto Networks"` | Look for those companies' job boards (see "Adding more boards"). Add `--add` to save what it finds, or `--file companies.txt` for a list |
| `python main.py crawl --add` | Find company boards **in bulk** from Common Crawl's public web index, check each one, and save the ones with a posting matching your titles (see "Adding more boards") |
| `python main.py workday-add <careers URL>` | Add a Workday careers site from any page address on it |
| `python viewer.py` | Open the report viewer and application tracker in your browser (see below) |
| `python inspect_db.py` | Peek at what is stored (`--where "california"` searches locations, `--odd` lists senior/staff/lead titles that slipped through) |
| `python -m unittest` | Run the offline tests (no network, no API key) |

Statuses: `new, saved, applied, skipped, interview, offer, accepted, rejected, withdrawn`. Jobs marked anything other than `new` or `saved` are excluded from future reports. Every status from `applied` onward (`applied, interview, offer, accepted, rejected, withdrawn`) counts the job as an application in the tracker.

Typical first run: `profile` (review the diff, answer `y`), then `run`, then `viewer.py`.

### Reading a run's output

A run prints a message at every stage:

1. `Fetching postings...`, then one line per source **as it finishes**. Sources are read several at a time, so the order varies from run to run.
2. `Filtering N postings...`, then the cheap filters (title, location, age).
3. `Reading the full text of N postings that passed the filters...`: Workday and similar sites list only titles, so each match needs one more request. This is the slow part of a run, so it prints a percentage line at every 10% (`[ 40%] full postings read: 80 of 200`).
4. The summary line and a `Time:` line (below).
5. `Scoring N jobs with <model>, 8 at a time...`, then one line per job as Claude finishes it (`[12/100]  82 apply Company - Title`) and `Scoring took Ns`.

A quiet stretch of more than a minute or two in stages 3 or 5 means something is stuck.

Each source prints one line, for example `greenhouse stripe 747 postings`, or `skipped (...)` with the reason if it failed (a bad slug, a network error, a missing key). Then one summary line:

`Fetched | title match | after location | excluded locations | already seen | duplicates | dropped by full location | too old | below pay | NEW`

Each number is how many postings were left or dropped at that step; `NEW` is what was stored this run. `dropped by full location` counts Workday postings whose list said only "6 Locations"; the full posting is fetched, its real places are checked against `us_only` and `exclude_locations`, and it is dropped if none are allowed. The `Time:` line after it shows how long listing the sources and reading full postings each took. After scoring it prints the tokens used and an estimated cost (from `scoring.price_per_million_tokens` in `config.yaml`, so check it against Anthropic's pricing page).

## Adding more boards

Postings come from public job-board data that companies publish themselves, from the applicant-tracking systems they use: **Greenhouse, Lever, Ashby, SmartRecruiters, Workable, Recruitee, BambooHR, Rippling, Gem, Personio, and Workday**. On top of that are seven aggregator feeds (below) and two government sources. The more companies you add, the more postings are searched.

Sites that forbid bots in their terms or `robots.txt` (LinkedIn, Indeed, Glassdoor, ZipRecruiter, WorkInTexas, governmentjobs.com) are not used. Most of their postings are copies of ones on the company's own board, which is where this program reads them.

- **`discover`** guesses a company's board address from its name and tries Greenhouse, Lever, Ashby, and SmartRecruiters. For each one it finds, it shows how many postings the board has, how many match your title, location, and pay filters, and one example posting with its link. **Check the example is the company you meant**, since a short name can match someone else's board. Nothing is saved until you run it again with `--add`. A name it can't find is usually a company on Workday or its own system, or one whose board name differs from its brand: for SmartRecruiters you can pass the exact id, for example `discover BoschGroup`.
- **`crawl`** finds boards in bulk, with no company names needed. [Common Crawl](https://commoncrawl.org) publishes a free index of web addresses, and every company on Greenhouse, Ashby, Workday and the rest has addresses such as `boards.greenhouse.io/<company>/jobs/123`. `crawl` lists those, takes the company name out of each, asks the platform's own job API whether the board exists, and keeps boards that currently list a posting matching your titles and locations (`--keep-all` keeps every live board). Run `python main.py crawl` first to see what it would add, then `python main.py crawl --add` to save. `--systems greenhouse,ashby` limits it to some platforms and `--crawls N` reads more or fewer recent crawls (default 4; more crawls find more boards). Common Crawl's index is a shared free service that answers slowly and sometimes refuses connections, so the program reads it gently (one page every couple of seconds) and a platform it cannot read is skipped with a note. If it keeps refusing you, wait an hour or so and run it again; that is not an error in your setup. It remembers what it has read and checked in `crawl_cache.json`, so running it again only does new work; boards that failed are asked again after 14 days, or at once with `--recheck`. Re-run it now and then: a board only joins your list when it has a matching posting on the day it is checked.
- **`workday-add`**: open the company's careers page, click any job, copy the address from the browser bar, and run `python main.py workday-add "<address>"`. It reads the host, tenant, and site out of the address, confirms the site responds, and saves it. Use `--name "Company"` for a nicer label.
- Boards added this way are stored in `discovered_sources.yaml` and merged with `sources` in `config.yaml` each run, so `config.yaml` and its comments are never rewritten. You can also add slugs to `config.yaml` by hand, including a `smartrecruiters:` list (`jobs.smartrecruiters.com/<CompanyId>`, not case sensitive).
- **Feeds** (`feeds:` in `config.yaml`, each on with `enabled: true`): Remotive, Remote OK, We Work Remotely, Jobicy, Working Nomads, Himalayas, and the monthly **Hacker News "Who is hiring?"** threads (one job per top-level comment, read from the first line `Company | Role | Location`). Jobicy and Himalayas are searched with your title words (Himalayas has over 100,000 postings, so it can't be read whole). Two broad aggregators, **Adzuna** and **Jooble**, are off until you add a free key to `.env` and set `enabled: true` (they return only a short snippet of each description, so scoring has less to go on). Each feed is fetched at most once every `min_hours` (default 6; Remotive asks for no more than about four requests a day) no matter how often you run the program. Every feed's job links go to its own site, as the terms of each require, and a job's `source` names it.
- **Duplicates:** if the same company and title is already stored from a different source, for example a job on both a company's board and Remotive, the second copy is skipped (shown as `duplicates` in the run summary). The same title in two different cities from one source is kept, because those are separate postings.
- **Many Workday sites:** `workday-add` and `discovery` can build a list of a hundred or more sites. Sites are read several at a time (`workday_workers`), each with one request at a time. A first run over ~100 sites read 22,000 postings and stored ~700 new matches. With the Workday sites plus about 1,400 Greenhouse, Ashby and SmartRecruiters boards from `crawl` and the feeds, a first run read about 114,000 postings and stored about 2,200 matches in under ten minutes; each run prints how long listing, full-posting lookups, and scoring took. Later runs only fetch details for postings not yet stored, so they are faster. With that many sites, use a short `workday_search_terms` list rather than `auto`.
- Not included: Breezy (its postings are drawn by JavaScript, so there is no text to read), The Muse (needs a key), Lever in bulk (Lever blocks Common Crawl's crawler, so `crawl` finds no Lever boards; add them by name with `discover` or by hand), and Oracle, iCIMS, Taleo, and SuccessFactors career sites (no uniform public data).

### Government jobs (on by default)

Both are under `government:` in `config.yaml`; set `enabled: false` on one to turn it off.

- **Federal (USAJobs)**: the official API. It needs a free key, so until you add one the run prints a "skipped" line and carries on. Request a key at https://developer.usajobs.gov/apirequest, then put `USAJOBS_API_KEY` and `USAJOBS_EMAIL` (the email the key was issued to) in `.env`. `keywords` sets what is searched. This part has been tested only against the API's documented format, not live, because that needs a key; if the first run with your key shows an error, tell me what it says.
- **Texas HHS (HHSC, DFPS, DSHS)** at careers.hhs.texas.gov: the site has no API, so the program reads the same public search and job pages any visitor sees (one request at a time, a few pages per search word). The site's `robots.txt` allows this and its pages state no rule against it. This is the most fragile source: if the site changes its layout the run prints "no postings found" instead of failing. Monthly salaries are converted to a yearly figure so the `min_pay` filter works.
- **Government titles**: these postings are titled differently from tech companies ("Programmer III", "IT Specialist (APPSW)"). `gov_titles` in `config.yaml` adds extra titles that apply **only to government sources** (USAJobs, Texas HHS, and any Workday site marked `gov: true`); the seniority exclusions (senior, lead, manager, ...) still apply.
- **Left out on purpose**: WorkInTexas.com and governmentjobs.com (the NEOGOV site many cities and counties use), because their `robots.txt` tells automated programs not to crawl them. For a city, county, or agency on Workday, use `workday-add`; for one on Greenhouse, Lever, or similar, use `discover`.

## Viewing matches in the browser

`python viewer.py` starts a small local server (`http://127.0.0.1:8765/`, your machine only) and opens it. It reads the Markdown files in `reports/`, so the list in the sidebar is exactly what the report says, and joins each entry to the stored posting in `jobs.db` by its `ID` line.

- Sidebar: pick a report date, filter by title or company, click a job (or press `j` / `k` to move). Each job has its own address (`#id`), so back and forward work. The **Matches** and **Tracker** tabs at the top switch between the job pages and the application tracker.
- Detail pane: score, verdict, matched and missing skills, resume angle, and the full stored posting text. **Open original posting** opens the real page in a new tab. Job sites generally forbid embedding their pages in another page, so the posting is shown from the saved copy instead.
- **Mark as applied** adds the job to the application tracker with today's date (see below). `saved`, `skipped`, and `interview` set the job's status, same as `main.py mark`. Applied and skipped jobs drop out of future reports.
- Options: `--port 8765`, `--no-open`. Stop it with Ctrl+C.

### Application tracker

The **Tracker** tab lists every job you have applied to and counts them three ways:

- **This week**: Monday through Sunday of the current week (the dates are shown on the card).
- **This month**: the current calendar month.
- **All time**: everything in the tracker.

Each card also shows how many different companies those applications are at, since two roles at one company are two applications but one place.

How it works:

- **Adding:** open a job on the Matches tab and click **Mark as applied**. Today's date (your computer's clock) is recorded. Sidebar entries for applied jobs show "✓ applied". Marking a job `interview` or any later status also counts it as applied if it wasn't already.
- **Date:** change it in the tracker table if you applied on a different day; future dates are refused. The week and month counts move with it.
- **Status after applying:** the Status column in the tracker table is a dropdown: Applied, Interview, Offer received, Offer accepted, Rejected, Withdrawn. Change it as things happen (the colour follows: green for offers, red for rejected). A job stays counted whatever you pick, and its date never moves.
- **Removing:** **Undo** on the job, or **Remove** in the tracker table, takes it out of the tracker and counts. Marking an applied job `saved` or `skipped` does the same, after asking you to confirm.
- **From the command line:** `python main.py mark <id> applied` records today's date the same way, so the two stay consistent.
- Only jobs this program has found can be added. Applications you made elsewhere are not in the tracker unless you also find them in a report. The dates are stored in `jobs.db` (column `applied_at`), which is created automatically.

## Your profile and resume

- **`profile.json`** is the source of truth: `strong_skills`, `familiar_skills`, `do_not_assume`, achievements, citizenship, clearance status, relocation, and fit notes. Keep it accurate. Update it when something changes, for example after passing the AWS exam.
- **`resume.pdf`** is sent to the scorer as extra context. Lines with your email, phone number, LinkedIn, or GitHub link are removed first. If the resume and the profile disagree, the profile wins.
- **`main.py profile`** rebuilds the resume-derived fields from the PDF. It carries over what a resume can't express (citizenship, clearance, relocation, location, `do_not_assume`, fit notes). A skill that is on the resume but also in `do_not_assume` is left out and reported as a conflict, so nothing gets promoted by accident. If it is now true, remove it from `do_not_assume` and run the command again. The previous file is saved as `profile.json.bak`. Read the diff before accepting: the model can misjudge strong versus familiar.

## Configuration (`config.yaml`)

- `min_pay`, `max_age_days`, `us_only`, `request_delay`
- `workday_workers`, `detail_workers`, `fetch_workers`, `crawl_workers`: how many different servers are read at the same time (defaults 16, 16, 8, and 16 for boards checked by `crawl`). Each Workday site and each full-posting server still gets only one request at a time, spaced by `request_delay`, so adding many sites makes a run longer but does not hammer any one site. The Greenhouse, Lever, Ashby and SmartRecruiters boards are small lists from shared public APIs, read up to `fetch_workers` at a time. Connections are reused between requests.
- `exclude_locations`: places to drop (California and its major cities are pre-filled). A posting is dropped only if **every** location it lists matches, so "San Francisco, CA | Remote - US" is kept because the remote option is still open to you. Matching is whole-word and case-insensitive, except two-letter state codes, which must be capitalized (`CA`). Add or remove entries freely, or delete the list to turn it off. Check what's stored with `python inspect_db.py --where "california"`.
- `titles`: your title list (from `filter.txt`), and `exclude_title_words`
- `gov_titles`: extra titles used only for government sources
- `scoring`: `model`, `max_per_run` (cost cap), `workers` (API calls at the same time, default 8; lower it if you hit rate limits), `report_min_score`, `report_size`, `price_per_million_tokens`
- `profile_model`: the model used by `main.py profile`
- On/off switches, one per source type. `platforms:` has a `true`/`false` line for each board platform (`greenhouse`, `lever`, `ashby`, `smartrecruiters`, `workable`, `recruitee`, `bamboohr`, `rippling`, `gem`, `personio`, `workday`). Set one to `false` and a run skips every board of that platform, printing a "turned off" note; the boards stay in your lists, so setting it back to `true` restores them. A platform you leave out is on. A misspelled name is reported, not silently ignored. Aggregator feeds (`feeds.<name>.enabled`) and government sources (`government.<name>.enabled`) have their own `enabled:` switch. These switches only affect `run`; `crawl` and `discover` still look for boards on every platform you ask for.
- `sources`: company boards to read
  - Greenhouse, Lever, Ashby: add the slug from the board URL (`boards.greenhouse.io/<slug>`, `jobs.lever.co/<slug>`, `jobs.ashbyhq.com/<slug>`). Bad slugs are skipped with a warning.
  - SmartRecruiters: the company id from `jobs.smartrecruiters.com/<CompanyId>`.
  - Workable, Recruitee, BambooHR, Rippling, Gem, Personio: lists named `workable:`, `recruitee:`, `bamboohr:`, `rippling:`, `gem:`, `personio:`, each holding the company's slug from its board address (`apply.workable.com/<slug>`, `<slug>.recruitee.com`, `<slug>.bamboohr.com`, `ats.rippling.com/<slug>`, `jobs.gem.com/<slug>`, `<slug>.jobs.personio.de`). Most people let `crawl` fill these in.
  - Workday: copy `host`, `tenant`, and `site` from the careers URL (`https://<host>/en-US/<site>/...`), or let `workday-add` do it. Add `gov: true` to an entry for a government employer (a state agency or university, say): it is then also searched with, and matched against, `gov_titles`. `workday_search_terms` (default `auto`: derived from `titles`, so you only edit one list; or give your own list, or `[auto, extra word]`) and `workday_max_pages` control how much is read. Each search word is one search per Workday site, and a search stops as soon as it runs out of results. Deriving the words from a long title list means more searches per site than a short hand-written list: in testing on one 350-posting site, 54 derived words took about 140 requests (3 pages each) and found the same 33 matching jobs as 8 hand-written words did in 37 requests. With many Workday sites, or `workday_max_pages` set high, a run gets slow; keep `workday_max_pages` low (3 is plenty, results come best-match first) or give a short list of your own.
- `feeds.remotive`: `enabled`, `min_hours`, `categories`
- `government.usajobs` (`enabled`, `keywords`, `pages`) and `government.texas_hhs` (`enabled`, `search_terms`, `pages`)

## Files

| File | Purpose |
|---|---|
| `main.py` | Command-line entry point and pipeline |
| `sources.py` | Job-board fetchers (Greenhouse, Lever, Ashby, SmartRecruiters, Workday, Remotive) and pay parsing |
| `ats_sources.py` | More applicant-tracking systems: Workable, Recruitee, BambooHR, Rippling, Gem, Personio |
| `feed_sources.py` | Aggregator feeds: Remote OK, We Work Remotely, Jobicy, Working Nomads, Himalayas, Hacker News "Who is hiring?" |
| `crawl.py` | Bulk board discovery from Common Crawl's public index (the `crawl` command) |
| `gov_sources.py` | Government sources: USAJobs (federal API) and Texas HHS careers |
| `discovery.py` | Board discovery from company names, Workday address parsing, and merging `discovered_sources.yaml` |
| `filters.py` | Title, location, pay, and age filters, plus the keyword pre-score |
| `scorer.py` | Claude fit scoring |
| `profile_gen.py` | Builds `profile.json` from the resume |
| `db.py` | SQLite storage, cross-source de-duplication, job statuses, and the application tracker (dates and week / month / all-time counts) |
| `viewer.py` | Local web viewer for the reports and the application tracker |
| `inspect_db.py` | Quick look at what is stored |
| `config.example.yaml`, `profile.example.json`, `.env.example` | Templates to copy (see Setup) |
| `config.yaml`, `profile.json`, `.env` | Your own settings, verified facts, and keys. Git-ignored; never commit them |
| `discovered_sources.yaml` | Boards added by `discover --add`, `workday-add` and `crawl --add` (created on first use, git-ignored) |
| `crawl_cache.json` | What `crawl` has already read and checked, so reruns are quick (git-ignored; safe to delete) |
| `jobs.db`, `reports/` | Stored postings, scores, and the tracker; daily Markdown reports (git-ignored) |
| `LICENSE` | All rights reserved: viewable for evaluation, not for reuse |
| `tests/` | Offline tests (`tests/data/` holds a saved page sample). Run with `python -m unittest` |

## Notes

- Cost: the pre-score picks which jobs go to Claude first, and `scoring.max_per_run` caps API calls per run. Each full run prints the tokens used and an estimated cost; check your usage on Anthropic's dashboard too.
- Privacy: `.env`, `jobs.db`, `reports/`, `config.yaml`, `profile.json`, `discovered_sources.yaml`, and your resume files are git-ignored. Resume lines containing an email, a LinkedIn or GitHub link, or a phone number are stripped before any API call. The viewer listens only on your own computer and ignores requests addressed to any other host name.
- Be polite: requests are spaced by `request_delay`. Use this only for your own job search and respect each site's terms.
- Not included on purpose: LinkedIn and Indeed scraping (against their terms and brittle), WorkInTexas and governmentjobs.com (their `robots.txt` asks automated programs to stay out), and auto-submitting applications.
- Known limits: only companies on the supported boards are covered. Workday, SmartRecruiters, and Texas HHS postings need one extra request each for the full text, made only for title matches that are not stored yet. Workday's list gives an approximate age ("Posted 30+ Days Ago"), so postings that are clearly too old are dropped without a lookup; ones rejected later for pay are looked up again on later runs. The USAJobs, Adzuna and Jooble sources have not been run against their live APIs (they need keys). The Texas HHS source depends on that site's page layout.

## License

All rights reserved. This repository is public so that it can be read and evaluated, not reused; see [LICENSE](LICENSE). The job-board data this program reads belongs to the companies and sites that publish it.
