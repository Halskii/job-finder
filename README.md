# job-finder

A small pipeline that finds job postings that fit you, filters them, scores each one against a **verified profile** and your resume with Claude, and tracks your applications. It never applies for you; you review and submit.

```
public job-board APIs -> title / location / pay / age filters -> keyword pre-rank -> Claude fit scoring -> ranked report + SQLite tracker
(Greenhouse, Lever, Ashby, Workday)
```

## How scoring works

1. **Filters (free, deterministic).** Titles from `config.yaml` (level markers like I / II / III / Associate / Junior are optional, so "Software Engineer I" also matches "Software Engineer"), excluded words (senior, staff, lead, ...), U.S. locations, posting age, and minimum pay. Pay is kept when unknown; a known maximum below `min_pay` drops the job.
2. **Keyword pre-score (free).** Counts how many of your skills appear in the posting and subtracts points for anything in your `do_not_assume` list. It decides which jobs Claude scores first. It only checks presence, so order never matters.
3. **Claude fit score (paid, cached).** For the top candidates, Claude reads your profile, your resume, your pay minimum, and the posting, and returns a 0-100 score, a verdict (`apply`, `maybe`, `skip`), matched skills, missing required skills, dealbreakers, and a suggested resume angle. Scores are cached, so a job is scored once.

Rules baked into the scorer: only facts in your profile count as true, anything in `do_not_assume` is a firm no, the profile and resume are treated as an **unordered** set of facts (position and section order carry no meaning), and a clearance that will be obtained after hire is not a dealbreaker. Bands: 85+ clearly meets nearly all required items, 60-84 is a reasonable stretch, under 40 is a poor fit.

## Setup (Windows)

```powershell
cd C:\Users\James\Documents\GitHub\job-finder
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env      # then paste your key after ANTHROPIC_API_KEY=
```

Put your resume in the project folder as `resume.pdf` (a `resume.txt` or `resume.md` also works; the PDF is preferred if several exist). Use your most general resume, not a tailored one.

## Commands

You can call the virtual environment's Python directly without activating it, for example `.venv/Scripts/python.exe main.py run`.

| Command | What it does |
|---|---|
| `python main.py profile` | Reads `resume.pdf`, extracts a profile with Claude, shows a diff against `profile.json`, and asks before writing |
| `python main.py profile --apply` | Same, but writes without asking |
| `python main.py run --no-llm` | Fetch and filter only. Free. Good first check |
| `python main.py run` | Full run: fetch, filter, score with Claude, write `reports/YYYY-MM-DD.md` |
| `python main.py run --limit 10` | Full run, but score at most 10 jobs |
| `python main.py list --top 15` | Best scored jobs from the database |
| `python main.py mark <id> applied "note"` | Update the tracker. `<id>` can be any unique fragment of a job id |
| `python inspect_db.py` | Peek at what is stored |
| `python -m unittest -v` | Run the offline tests (no network, no API key) |

Statuses: `new, saved, applied, skipped, interview, rejected, offer`. Jobs you mark are excluded from future reports.

Typical first run: `profile` (review the diff, answer `y`), then `run`, then read the newest file in `reports/`.

## Your profile and resume

- **`profile.json`** is the source of truth: `strong_skills`, `familiar_skills`, `do_not_assume`, achievements, citizenship, clearance status, relocation, and fit notes. Keep it accurate. Update it when something changes, for example after passing the AWS exam.
- **`resume.pdf`** is sent to the scorer as extra context. Your email, phone, LinkedIn, and GitHub lines are removed first. If the resume and the profile disagree, the profile wins.
- **`main.py profile`** rebuilds the resume-derived fields from the PDF. It carries over what a resume can't express (citizenship, clearance, relocation, location, `do_not_assume`, fit notes). A skill that is on the resume but also in `do_not_assume` is left out and reported as a conflict, so nothing gets promoted by accident. If it is now true, remove it from `do_not_assume` and run the command again. The previous file is saved as `profile.json.bak`. Read the diff before accepting: the model can misjudge strong versus familiar.

## Configuration (`config.yaml`)

- `min_pay`, `max_age_days`, `us_only`, `request_delay`
- `titles`: your title list (from `filter.txt`), and `exclude_title_words`
- `scoring`: `model`, `max_per_run` (cost cap), `report_min_score`, `report_size`
- `profile_model`: the model used by `main.py profile`
- `sources`: company boards to read
  - Greenhouse, Lever, Ashby: add the slug from the board URL (`boards.greenhouse.io/<slug>`, `jobs.lever.co/<slug>`, `jobs.ashbyhq.com/<slug>`). Bad slugs are skipped with a warning.
  - Workday: copy `host`, `tenant`, and `site` from the careers URL (`https://<host>/en-US/<site>/...`). `workday_search_terms` and `workday_max_pages` control how much is read.

## Files

| File | Purpose |
|---|---|
| `main.py` | Command-line entry point and pipeline |
| `sources.py` | Job-board fetchers (Greenhouse, Lever, Ashby, Workday) and pay parsing |
| `filters.py` | Title, location, pay, and age filters, plus the keyword pre-score |
| `scorer.py` | Claude fit scoring |
| `profile_gen.py` | Builds `profile.json` from the resume |
| `db.py` | SQLite storage, dedupe, and tracker |
| `config.yaml`, `profile.json` | Your settings and verified facts |
| `test_*.py` | Offline tests |
| `.env` | Your API key (git-ignored; never commit it) |

## Notes

- Cost: the pre-score picks which jobs go to Claude first, and `scoring.max_per_run` caps API calls per run. Check your usage after the first full run.
- Privacy: `.env`, `jobs.db`, `reports/`, and your resume files are git-ignored. The resume's contact lines are stripped before any API call.
- Be polite: requests are spaced by `request_delay`. Use this only for your own job search and respect each site's terms.
- Not included on purpose: LinkedIn and Indeed scraping (against their terms and brittle) and auto-submitting applications.
- Known limits: only companies on the supported boards are covered, Workday postings are re-fetched for detail on each run until they are stored, and the scorer has only been tested offline with a fake client until you run it with a real key.
