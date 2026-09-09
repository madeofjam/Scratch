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
import re
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


def contains_term(haystack: str, term: str) -> bool:
    """Whole-word/phrase match, so short terms like "cto" or "intern" don't
    false-positive inside unrelated words ("dire-cto-r", "intern-ational")."""
    return re.search(r"\b" + re.escape(term.lower()) + r"\b", haystack) is not None


NEGATION_RE = re.compile(r"\b(no|not|non|isn'?t|without)\b(?:[\s-]+\w+){0,3}[\s-]*$")


def contains_unnegated_term(haystack: str, term: str) -> bool:
    """Like contains_term, but a match is ignored if it's preceded within a
    few words by a negation ("no remote work", "not a hybrid role") — so
    free-text descriptions don't produce false positives the way a plain
    substring or word-boundary search would."""
    pattern = re.compile(r"\b" + re.escape(term.lower()) + r"\b")
    for m in pattern.finditer(haystack):
        prefix = haystack[max(0, m.start() - 40) : m.start()]
        if NEGATION_RE.search(prefix):
            continue
        return True
    return False


def passes_filters(job: dict, config: dict) -> bool:
    title = (job.get("title") or "").lower()

    for term in config.get("exclude_title_terms", []):
        if contains_term(title, term):
            return False

    must_contain = config.get("title_must_contain")
    if must_contain and not any(contains_term(title, term) for term in must_contain):
        return False

    return True


def passes_location_filter(job: dict, config: dict) -> bool:
    """A job must be based near Farnham or explicitly remote-friendly.

    The location field and title are checked with a plain word-boundary
    match (structured/intentional text, negation is not a concern there);
    the free-text description is checked with the negation-aware variant, so
    "no remote work" in a description doesn't count as a match.
    """
    loc_config = config.get("location") or {}
    commutable = loc_config.get("commutable_areas") or []
    remote_terms = loc_config.get("remote_terms") or []
    if not commutable and not remote_terms:
        return True

    location = (job.get("location") or "").lower()
    if any(contains_term(location, area) for area in commutable):
        return True

    signal = f"{location} {(job.get('title') or '').lower()}"
    if any(contains_term(signal, term) for term in remote_terms):
        return True

    description = (job.get("description") or "").lower()
    return any(contains_unnegated_term(description, term) for term in remote_terms)


def score_cv_keywords(job: dict, config: dict) -> tuple[list[str], int]:
    """Match this job's title+description against the CV skills/experience
    keywords in config. Run against the raw (untruncated) description for
    best recall — normalise() truncates it for display only."""
    keywords = config.get("cv_keywords") or []
    if not keywords:
        return [], 0

    text = f"{job.get('title') or ''} {job.get('description') or ''}".lower()
    matched = [kw for kw in keywords if contains_term(text, kw)]
    return matched, len(matched)


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
    funnel = {"raw": 0, "dropped_title": 0, "dropped_keywords": 0, "dropped_location": 0}
    for query in config.get("searches", []):
        try:
            results = fetch_query(query, config)
        except requests.RequestException as e:
            print(f"WARN: query {query!r} failed: {e}", file=sys.stderr)
            continue

        label = query.get("what_phrase") or query.get("what")
        print(f"[query] {label!r}: {len(results)} raw results")
        funnel["raw"] += len(results)

        for raw_job in results:
            if not passes_filters(raw_job, config):
                funnel["dropped_title"] += 1
                continue

            matched_keywords, match_score = score_cv_keywords(raw_job, config)
            min_matches = config.get("min_keyword_matches", 0)
            if min_matches and match_score < min_matches:
                funnel["dropped_keywords"] += 1
                continue

            job = normalise(raw_job)
            if not job["id"]:
                continue
            if not passes_location_filter(job, config):
                funnel["dropped_location"] += 1
                continue

            job["matched_keywords"] = matched_keywords
            job["match_score"] = match_score
            merged[job["id"]] = job

    print(
        f"[funnel] {funnel['raw']} raw (pre-dedup, sums across queries) "
        f"-> -{funnel['dropped_title']} title filter "
        f"-> -{funnel['dropped_keywords']} keyword filter "
        f"-> -{funnel['dropped_location']} location filter "
        f"-> {len(merged)} unique jobs kept"
    )

    jobs = []
    for job_id, job in merged.items():
        prev = previous.get(job_id)
        first_seen = prev["first_seen"] if prev else today
        job["first_seen"] = first_seen
        job["is_new"] = first_seen == today
        jobs.append(job)

    # Newest posting first within each group, best CV match above weaker
    # matches, new-today jobs surfaced above everything else.
    jobs.sort(key=lambda j: j.get("created") or "", reverse=True)
    jobs.sort(key=lambda j: j.get("match_score", 0), reverse=True)
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
