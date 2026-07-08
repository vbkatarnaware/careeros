"""Stage: dedupe. Deterministic. Zero AI, zero tokens.

Two dedupe passes, both keyed on Job.id (sha1 of source|company|title|location):

1. In-run: drop duplicate ids surfaced by the same discovery pass (a job
   posted on two boards the provider both scraped, or a provider returning
   the same posting twice).
2. Against history: drop ids already seen in a prior run, tracked in
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
