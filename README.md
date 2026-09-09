# Job Hunt Dashboard

Daily-refreshed dashboard of senior UK technology leadership roles (CIO /
Group CIO / CTO), matched against the search criteria in
[`config/search_config.yaml`](config/search_config.yaml). A scheduled GitHub
Actions workflow queries the [Adzuna](https://www.adzuna.co.uk/) job search
API — which aggregates listings from Indeed, Reed, Totaljobs, CV-Library and
others — every morning, filters and de-duplicates the results, and publishes
them to a static dashboard via GitHub Pages.

## How it fits together

- `config/search_config.yaml` — what to search for: titles, UK location,
  minimum salary, how far back to look, and title-based include/exclude
  filters. Edit this any time; no code changes needed.
- `scripts/search_jobs.py` — queries the Adzuna API for every entry in the
  config, filters and merges the results, and writes `data/jobs.json`.
  Tracks a `first_seen` date per job so the dashboard can badge new postings.
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

- `searches` — one entry per Adzuna query (exact-phrase title searches by
  default). Add or remove roles here.
- `where` — leave blank for UK-wide, or set a city/region to narrow it.
- `salary_min` — drops listings below this (where Adzuna has salary data).
- `title_must_contain` / `exclude_title_terms` — extra title-based filtering
  on top of the Adzuna query, so results stay precise.

Changes take effect on the next scheduled or manually-triggered run — no
need to touch the workflow or script.
