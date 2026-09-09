#!/usr/bin/env python3
"""Daily job search.

Queries the Adzuna API for each entry in config/search_config.yaml, filters
and de-duplicates the results, and writes data/jobs.json for the static
dashboard (index.html). Run by .github/workflows/job-search.yml on a daily
schedule; also safe to run by hand.

Requires ADZUNA_APP_ID and ADZUNA_APP_KEY in the environment (free keys from
https://developer.adzuna.com/).
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "search_config.yaml"
DATA_PATH = ROOT / "data" / "jobs.json"

ADZUNA_APP_ID = os.environ.get("ADZUNA_APP_ID")
ADZUNA_APP_KEY = os.environ.get("ADZUNA_APP_KEY")


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def load_previous_jobs() -> dict:
    """Map of job id -> previously-written job record, used to preserve
    first_seen dates across runs."""
    if not DATA_PATH.exists():
        return {}
    with open(DATA_PATH) as f:
        data = json.load(f)
    return {job["id"]: job for job in data.get("jobs", [])}


def fetch_query(query: dict, config: dict) -> list:
    country = config.get("country", "gb")
    url = f"https://api.adzuna.com/v1/api/jobs/{country}/search/1"
    params = {
        "app_id": ADZUNA_APP_ID,
        "app_key": ADZUNA_APP_KEY,
        "results_per_page": config.get("results_per_page", 50),
        "content-type": "application/json",
    }
    if "what_phrase" in query:
        params["what_phrase"] = query["what_phrase"]
    elif "what" in query:
        params["what"] = query["what"]

    where = query.get("where", config.get("where"))
    if where:
        params["where"] = where

    salary_min = config.get("salary_min")
    if salary_min:
        params["salary_min"] = salary_min

    max_days_old = config.get("max_days_old")
    if max_days_old:
        params["max_days_old"] = max_days_old

    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json().get("results", [])


def passes_filters(job: dict, config: dict) -> bool:
    title = (job.get("title") or "").lower()

    for term in config.get("exclude_title_terms", []):
        if term.lower() in title:
            return False

    must_contain = config.get("title_must_contain")
    if must_contain and not any(term.lower() in title for term in must_contain):
        return False

    return True


def normalise(job: dict) -> dict:
    return {
        "id": job.get("id"),
        "title": (job.get("title") or "").strip(),
        "company": (job.get("company") or {}).get("display_name", "Unknown"),
        "location": (job.get("location") or {}).get("display_name", ""),
        "salary_min": job.get("salary_min"),
        "salary_max": job.get("salary_max"),
        "created": job.get("created"),
        "url": job.get("redirect_url"),
        "description": (job.get("description") or "").strip()[:400],
        "category": (job.get("category") or {}).get("label", ""),
    }


def main() -> None:
    if not ADZUNA_APP_ID or not ADZUNA_APP_KEY:
        print(
            "ERROR: ADZUNA_APP_ID / ADZUNA_APP_KEY environment variables are not set.",
            file=sys.stderr,
        )
        sys.exit(1)

    config = load_config()
    previous = load_previous_jobs()
    today = datetime.now(timezone.utc).date().isoformat()

    merged: dict[str, dict] = {}
    for query in config.get("searches", []):
        try:
            results = fetch_query(query, config)
        except requests.RequestException as e:
            print(f"WARN: query {query!r} failed: {e}", file=sys.stderr)
            continue

        for raw_job in results:
            if not passes_filters(raw_job, config):
                continue
            job = normalise(raw_job)
            if not job["id"]:
                continue
            merged[job["id"]] = job

    jobs = []
    for job_id, job in merged.items():
        prev = previous.get(job_id)
        first_seen = prev["first_seen"] if prev else today
        job["first_seen"] = first_seen
        job["is_new"] = first_seen == today
        jobs.append(job)

    # Newest posting first within each group, new-today jobs surfaced above
    # everything else.
    jobs.sort(key=lambda j: j.get("created") or "", reverse=True)
    jobs.sort(key=lambda j: j["is_new"], reverse=True)

    output = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "total": len(jobs),
        "new_today": sum(1 for j in jobs if j["is_new"]),
        "jobs": jobs,
    }

    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(DATA_PATH, "w") as f:
        json.dump(output, f, indent=2)
        f.write("\n")

    print(f"Wrote {len(jobs)} jobs ({output['new_today']} new today) to {DATA_PATH}")


if __name__ == "__main__":
    main()
