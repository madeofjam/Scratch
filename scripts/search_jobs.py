#!/usr/bin/env python3
"""Daily job search.

Queries Adzuna (required) and Reed (optional) for each entry in
config/search_config.yaml, filters and de-duplicates the combined results,
and writes data/jobs.json for the static dashboard (index.html). Run by
.github/workflows/job-search.yml on a daily schedule; also safe to run by
hand.

Requires ADZUNA_APP_ID and ADZUNA_APP_KEY in the environment (free keys from
https://developer.adzuna.com/). Set REED_API_KEY (free key from
https://www.reed.co.uk/developers) to also search Reed; if unset, Reed is
skipped and only Adzuna results are used — existing setups keep working
unchanged.
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
REED_API_KEY = os.environ.get("REED_API_KEY")


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


def fetch_adzuna_query(query: dict, config: dict) -> list:
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


def fetch_reed_query(query: dict, config: dict) -> list:
    """Reed's search has no exact-phrase mode (unlike Adzuna's what_phrase)
    and no "posted within N days" filter — age filtering for Reed results
    happens client-side in main() via within_max_age()."""
    keywords = query.get("what_phrase") or query.get("what")
    if not keywords:
        return []

    params = {
        "keywords": keywords,
        "resultsToTake": config.get("results_per_page", 50),
    }
    where = query.get("where", config.get("where"))
    if where:
        params["locationName"] = where
    salary_min = config.get("salary_min")
    if salary_min:
        params["minimumSalary"] = salary_min

    resp = requests.get(
        "https://www.reed.co.uk/api/1.0/search",
        params=params,
        auth=(REED_API_KEY, ""),
        timeout=30,
    )
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

    # Exclusions stay title-only and deliberately narrow (junior/deputy/etc):
    # scanning the description too would drop senior roles that merely
    # mention junior team members they'd be managing.
    for term in config.get("exclude_title_terms", []):
        if contains_term(title, term):
            return False

    # title_must_contain checks title+description, not title alone: Adzuna's
    # own what_phrase query already matched the phrase somewhere in the full
    # text, so requiring it again in the title specifically just throws away
    # real candidates whose title doesn't happen to restate it (e.g. "Head
    # of Engineering" whose description says "reports to the CIO").
    text = f"{title} {(job.get('description') or '').lower()}"
    must_contain = config.get("title_must_contain")
    if must_contain and not any(contains_term(text, term) for term in must_contain):
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
    keywords in config. Expects a normalised job (title/description keys
    consistent across sources) with its description still untruncated —
    main() truncates it for display only, after this runs."""
    keywords = config.get("cv_keywords") or []
    if not keywords:
        return [], 0

    text = f"{job.get('title') or ''} {job.get('description') or ''}".lower()
    matched = [kw for kw in keywords if contains_term(text, kw)]
    return matched, len(matched)


def normalise_adzuna(job: dict) -> dict:
    return {
        "id": job.get("id"),
        "title": (job.get("title") or "").strip(),
        "company": (job.get("company") or {}).get("display_name", "Unknown"),
        "location": (job.get("location") or {}).get("display_name", ""),
        "salary_min": job.get("salary_min"),
        "salary_max": job.get("salary_max"),
        "created": job.get("created"),
        "url": job.get("redirect_url"),
        # Left untruncated: passes_filters/score_cv_keywords/
        # passes_location_filter all run against this, and want the full
        # text for best recall. main() truncates it for display only, right
        # before writing the final output.
        "description": (job.get("description") or "").strip(),
        "category": (job.get("category") or {}).get("label", ""),
    }


def normalise_reed(job: dict) -> dict:
    # "reed-" prefix keeps Reed IDs from ever colliding with Adzuna's
    # (purely numeric) IDs; Adzuna IDs stay unprefixed so first_seen
    # tracking for already-seen Adzuna jobs isn't disturbed by this change.
    job_id = job.get("jobId")
    created = None
    date_str = job.get("date")
    if date_str:
        try:
            created = (
                datetime.strptime(date_str, "%d/%m/%Y")
                .replace(tzinfo=timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )
        except ValueError:
            created = None

    return {
        "id": f"reed-{job_id}" if job_id is not None else None,
        "title": (job.get("jobTitle") or "").strip(),
        "company": job.get("employerName") or "Unknown",
        "location": job.get("locationName") or "",
        "salary_min": job.get("minimumSalary"),
        "salary_max": job.get("maximumSalary"),
        "created": created,
        "url": job.get("jobUrl"),
        # Untruncated for the same reason as normalise_adzuna above.
        "description": (job.get("jobDescription") or "").strip(),
        "category": "",
    }


def within_max_age(created_iso: str | None, max_days_old: int) -> bool:
    """Adzuna already filters by age server-side; this is the actual
    filter for Reed (which has no equivalent param) and a harmless
    double-check for Adzuna."""
    if not created_iso:
        return True
    try:
        created = datetime.fromisoformat(created_iso.replace("Z", "+00:00"))
    except ValueError:
        return True
    return (datetime.now(timezone.utc) - created).days <= max_days_old


def main() -> None:
    if not ADZUNA_APP_ID or not ADZUNA_APP_KEY:
        print(
            "ERROR: ADZUNA_APP_ID / ADZUNA_APP_KEY environment variables are not set.",
            file=sys.stderr,
        )
        sys.exit(1)

    sources = [("adzuna", fetch_adzuna_query, normalise_adzuna)]
    if REED_API_KEY:
        sources.append(("reed", fetch_reed_query, normalise_reed))
    else:
        print("INFO: REED_API_KEY not set — searching Adzuna only.", file=sys.stderr)

    config = load_config()
    previous = load_previous_jobs()
    today = datetime.now(timezone.utc).date().isoformat()
    max_days_old = config.get("max_days_old")

    merged: dict[str, dict] = {}
    funnel = {"raw": 0, "dropped_title": 0, "dropped_keywords": 0, "dropped_location": 0, "dropped_age": 0}
    for query in config.get("searches", []):
        label = query.get("what_phrase") or query.get("what")

        for source_name, fetch_fn, normalise_fn in sources:
            try:
                raw_results = fetch_fn(query, config)
            except requests.RequestException as e:
                print(f"WARN: {source_name} query {label!r} failed: {e}", file=sys.stderr)
                continue

            print(f"[{source_name}] {label!r}: {len(raw_results)} raw results")
            funnel["raw"] += len(raw_results)

            for raw_job in raw_results:
                job = normalise_fn(raw_job)
                if not job["id"] or not job["title"]:
                    continue

                if not passes_filters(job, config):
                    funnel["dropped_title"] += 1
                    continue

                matched_keywords, match_score = score_cv_keywords(job, config)
                min_matches = config.get("min_keyword_matches", 0)
                if min_matches and match_score < min_matches:
                    funnel["dropped_keywords"] += 1
                    continue

                if not passes_location_filter(job, config):
                    funnel["dropped_location"] += 1
                    continue

                if max_days_old and not within_max_age(job.get("created"), max_days_old):
                    funnel["dropped_age"] += 1
                    continue

                job["matched_keywords"] = matched_keywords
                job["match_score"] = match_score
                job["description"] = job["description"][:400]
                merged[job["id"]] = job

    print(
        f"[funnel] {funnel['raw']} raw (pre-dedup, sums across queries/sources) "
        f"-> -{funnel['dropped_title']} title filter "
        f"-> -{funnel['dropped_keywords']} keyword filter "
        f"-> -{funnel['dropped_location']} location filter "
        f"-> -{funnel['dropped_age']} age filter (Reed only, Adzuna pre-filters server-side) "
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
