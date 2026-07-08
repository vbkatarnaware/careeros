"""Stage: dedupe. Deterministic. Zero AI, zero tokens.

Three dedupe passes:

1. In-run (Job.id, sha1 of source|company|title|location): drop duplicate ids
   surfaced by the same discovery pass (a job posted on two boards the
   provider both scraped, or a provider returning the same posting twice).
2. Cross-location (location-INDEPENDENT): the same role at the same company,
   posted once per country/office, is a single real opportunity, not N —
   found live in the P2.6 benchmark (2026-07-08): segmented discovery's own
   remote queries surface the same multi-country posting multiple times (e.g.
   one role appearing separately for Poland/Bulgaria/Spain), each of which
   `dedupe_in_run` treats as distinct because `Job.id` includes location. Left
   uncaught, each variant gets independently gated/evaluated (wasted AI) and
   would produce duplicate Sheet rows.
3. Against history: drop ids already seen in a prior run, tracked in
   `.careeros/seen.jsonl` — a flat append-only log, not a database, because
   the only operation it needs to support is "have we seen this id before."

Sheet-based dedup (checking the Sheet's own Job ID column) is layered on top
by the caller when a live Sheets connection is available — this module stays
pure and testable without any Sheets/network dependency.
"""

from __future__ import annotations

import json
from pathlib import Path

from careeros.models import Job


def dedupe_in_run(jobs: list[Job]) -> tuple[list[Job], list[Job]]:
    """Returns (unique, dropped) for one discovery batch."""
    seen_ids: set[str] = set()
    unique: list[Job] = []
    dropped: list[Job] = []
    for job in jobs:
        if job.id in seen_ids:
            dropped.append(job)
        else:
            seen_ids.add(job.id)
            unique.append(job)
    return unique, dropped


def _cross_location_key(job: Job) -> tuple[str, str, str]:
    """Company + title + the first 300 chars of the description, normalized
    (case/whitespace-insensitive). The description prefix is included, not
    just company+title, so two genuinely different simultaneous openings that
    happen to share a title (e.g. two "Software Engineer" reqs in different
    cities) aren't wrongly collapsed — verified live that same-role reposts
    across countries share an identical description prefix (only trailing,
    country-specific boilerplate differs), while unrelated postings don't.
    """
    company = (job.company or "").strip().lower()
    title = (job.title or "").strip().lower()
    desc_prefix = " ".join((job.description or "")[:300].split()).lower()
    return (company, title, desc_prefix)


def dedupe_cross_location(jobs: list[Job]) -> tuple[list[Job], list[Job]]:
    """Returns (unique, dropped): collapses the same role posted once per
    country/office into a single entry. Keeps the FIRST occurrence in list
    order — since `discover` runs segmented queries in `profile.work_mode_
    priority` order and appends results in that same sequence, the surviving
    copy is naturally the one from the candidate's highest-priority work-mode
    tier (e.g. a role also posted in a lower-priority country is dropped in
    favor of the same role's higher-priority-tier posting), with zero extra
    ranking logic needed here.
    """
    seen_keys: set[tuple[str, str, str]] = set()
    unique: list[Job] = []
    dropped: list[Job] = []
    for job in jobs:
        key = _cross_location_key(job)
        if key in seen_keys:
            dropped.append(job)
        else:
            seen_keys.add(key)
            unique.append(job)
    return unique, dropped


def load_seen_ids(seen_path: Path | str) -> set[str]:
    seen_path = Path(seen_path)
    if not seen_path.exists():
        return set()
    ids: set[str] = set()
    with open(seen_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            ids.add(record["id"])
    return ids


def append_seen_ids(seen_path: Path | str, jobs: list[Job], run_date: str) -> None:
    seen_path = Path(seen_path)
    seen_path.parent.mkdir(parents=True, exist_ok=True)
    with open(seen_path, "a") as f:
        for job in jobs:
            f.write(json.dumps({"id": job.id, "first_seen": run_date}) + "\n")


def dedupe_against_history(
    jobs: list[Job], seen_path: Path | str
) -> tuple[list[Job], list[Job]]:
    """Returns (unique, dropped) after filtering out ids already in seen.jsonl.
    Caller is responsible for appending the surviving `unique` jobs back to
    seen.jsonl once the run completes (see cli.py's dedupe command) — this
    function only reads, so it stays safe to call repeatedly (e.g. for a
    dry-run) without mutating state.
    """
    seen_ids = load_seen_ids(seen_path)
    unique: list[Job] = []
    dropped: list[Job] = []
    for job in jobs:
        if job.id in seen_ids:
            dropped.append(job)
        else:
            unique.append(job)
    return unique, dropped


def dedupe_against_sheet_ids(
    jobs: list[Job], sheet_job_ids: set[str]
) -> tuple[list[Job], list[Job]]:
    """Same shape as dedupe_against_history, but against ids already present
    in the Google Sheet's Job ID column — this is the "as well as whatever is
    stored in the excel sheet" half of the requirement. Kept as a pure
    function so it's testable without a live Sheets connection: pass in
    whatever set of ids `sheets.read_existing_job_ids()` returns.
    """
    unique: list[Job] = []
    dropped: list[Job] = []
    for job in jobs:
        if job.id in sheet_job_ids:
            dropped.append(job)
        else:
            unique.append(job)
    return unique, dropped
