# Job Hunt Dashboard

Daily-refreshed dashboard of senior UK technology leadership roles (CIO /
Director / Head of / VP), commutable from Farnham or remote, matched against
the search criteria in
[`config/search_config.yaml`](config/search_config.yaml). A scheduled GitHub
Actions workflow queries the [Adzuna](https://www.adzuna.co.uk/) job search
API — which aggregates listings from Indeed, Totaljobs, CV-Library and
others — every morning, plus the [Reed](https://www.reed.co.uk/) API
directly if configured (optional — see setup below), filters and
de-duplicates the combined results, and publishes them to a static
dashboard via GitHub Pages.

Matching happens in three stages: `searches` (domain-anchored technology/
digital/engineering terms) sources a sane candidate pool from Adzuna/Reed —
searching on bare skill words like "GDPR" alone would return thousands of
unrelated junior/compliance postings, and bare seniority words like
"Director" alone return so much cross-industry volume that genuinely
relevant postings get buried past the APIs' 50-results-per-page cap and
never even get fetched (tested live, this really happens); `title_must_contain`
is a loose seniority check (just needs to sound senior, not restate an exact
C-suite phrase); and `relevance_keywords` is the actual relevance gate — a
role must mention at least one broad tech-domain term to make the dashboard,
separate from the specific CV-jargon list (`cv_keywords`) that's too narrow
to gate on but drives the match tags and "Best CV match" sort you see on
each card.

## How it fits together

- `config/search_config.yaml` — what to search for: titles, UK location,
  minimum salary, how far back to look, and title-based include/exclude
  filters. Edit this any time; no code changes needed.
- `scripts/search_jobs.py` — queries Adzuna (and Reed, if configured) for
  every entry in the config, filters and merges the combined results, and
  writes `data/jobs.json`. Tracks a `first_seen` date per job so the
  dashboard can badge new postings.
- `.github/workflows/job-search.yml` — runs the script daily at 07:00 UTC
  (and on demand via "Run workflow"), then commits the updated
  `data/jobs.json` back to the repo.
- `index.html` — the dashboard itself. Static HTML/JS, fetches
  `data/jobs.json` at load time. No build step.

## One-time setup

1. **Get free Adzuna API credentials.** Register at
   [developer.adzuna.com](https://developer.adzuna.com/) — takes a couple of
   minutes and gives you an `App ID` and `App Key`.

2. **Add them as repository secrets.** In this repo on GitHub: *Settings →
   Secrets and variables → Actions → New repository secret*, and add:
   - `ADZUNA_APP_ID`
   - `ADZUNA_APP_KEY`

2b. **Optional: also search Reed.** Register a free API key at
   [reed.co.uk/developers](https://www.reed.co.uk/developers) and add it as
   a repository secret named `REED_API_KEY`. If this secret isn't set, Reed
   is skipped automatically and the dashboard runs on Adzuna alone — nothing
   else to configure either way.

3. **Enable GitHub Pages.** *Settings → Pages → Source: Deploy from a
   branch → Branch: `main` / `(root)`*. The dashboard will then be live at
   `https://<your-username>.github.io/<repo-name>/`.

4. **Merge this branch to `main`** (or your default branch). GitHub only
   runs *scheduled* workflows from the default branch, so the daily 07:00
   UTC run won't fire until this is merged.

5. **Trigger the first run manually** rather than waiting for tomorrow:
   *Actions tab → Daily Job Search → Run workflow*. This populates
   `data/jobs.json` immediately so the dashboard has something to show.

## Tuning your search

Everything about *what* counts as a match lives in
`config/search_config.yaml`:

- `searches` — one entry per query, run against Adzuna (exact-phrase) and
  Reed (general keyword search, if configured). Add or remove roles here.
- `where` — leave blank for UK-wide, or set a city/region to narrow it.
- `salary_min` — drops listings below this (where the source has salary
  data — most senior/exec listings do, but not all).
- `title_must_contain` / `exclude_title_terms` — a loose seniority check
  (does the role sound senior at all?), not a relevance check — see
  `relevance_keywords` below for that.
- `location.commutable_areas` / `location.remote_terms` — a job is kept only
  if its location matches one of `commutable_areas` (currently Farnham and
  the surrounding commutable area, Surrey/Hampshire, and London/South East)
  or it looks remote-friendly per `remote_terms`. Edit these lists to widen
  or narrow the geography.
- `relevance_keywords` / `min_relevance_matches` — the actual relevance
  gate. Broad, common tech-domain words (Technology, Digital, Cybersecurity,
  Cloud, CIO/CTO/CDO phrases, ...) — deliberately generic so a real job-ad
  blurb is likely to contain one, unlike `cv_keywords` below. Raise
  `min_relevance_matches` above 1 if too much noise is getting through;
  lower toward 0 if it's cutting real matches.
- `cv_keywords` — specific skills/experience terms pulled from the CV
  (ISO27001, GDPR, vendor management, chargeback models, hyper-growth
  scaling, etc.). NOT a gate — too narrow for that (see the known
  limitation below) — purely scoring: matched terms show as tags on the
  dashboard and drive "Best CV match" sorting. Update this list whenever
  the CV changes, so the dashboard keeps scoring on current skills.

Changes take effect on the next scheduled or manually-triggered run — no
need to touch the workflow or script.

## Known limitation: role relevance vs. employer's industry

`relevance_keywords` checks a job's full title+description text, and can't
distinguish "this role manages technology" from "this role exists at a
company that happens to be in tech." A CFO role at a SaaS company, or a
Compliance lead at an "AI-native fintech," will often mention "software" or
"technology" while describing the *employer*, which is enough to pass the
gate even though the *role* itself isn't a technology leadership position.
Verified this is the dominant source of residual noise on the dashboard —
these are usually easy to dismiss by eye from the title alone (a "Head of
Pensions Knowledge" or "Construction Director" is obviously not a tech
role, whatever company posted it), and the CV-match tags / "Best CV match"
sort still surface genuinely strong matches above this noise. Fixing it
properly would need something more like classification than keyword
matching; raising `min_relevance_matches` trades this off against losing
real matches with thinner descriptions, so it's left as a known tradeoff
rather than "solved" — tune it if the balance feels wrong.

## Known limitation: Reed dates are day-precision only

Reed's API returns a posting date only (no time), so Reed-sourced jobs show
midnight UTC as their "created" time — the age badge and sort order are
still correct to the day, just not to the hour the way Adzuna's are.

## Known limitation: remote/hybrid detection

Whether a job is "remote" is read from its Adzuna location field, its title,
and (with a simple negation check) its description — so "no remote work" in
a description is correctly ignored, but a longer-distance negation like "not
able to offer remote working" can still slip through, since that needs real
language understanding rather than a keyword search. Treat the dashboard as
a first-pass filter and check the listing itself before ruling a role in or
out on location grounds.
