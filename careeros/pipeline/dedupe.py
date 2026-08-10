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
3. Against history: drop ids already processed in a prior run, tracked in
   `.careeros/processed.jsonl` — a flat append-only log, not a database,
   because the only operation it needs to support is "have we seen this id
   before."

   v2.0: this file replaces `.careeros/seen.jsonl`, which had a real
   production bug — it was ONLY ever written from `careeros/cli/
   sheets_cmds.py`'s `sheets_append`, AFTER that command's `sheets.enabled`
   early-return, and only for Apply/Consider-tier ids. Consequence: with
   `sheets.enabled: false` (the documented "local mode"), history was NEVER
   recorded, so every gate/eval-rejected job re-entered the pipeline and
   re-burned gate/eval tokens on every single run — found live 2026-07-31 as
   56% same-run duplicates. `processed.jsonl` is written unconditionally by
   `careeros/cli/pipeline.py`'s `threshold` command (the run's final
   deterministic stage, so it can see every terminal outcome: constraints-
   rejected, gate-dropped, evaluate-omitted, consider, and apply), covering
   every job this run reached a decision on — not just the ones that made it
   to a Sheet row. See `load_processed_ids`/`append_processed`.

Sheet-based dedup (checking the Sheet's own Job ID column) is layered on top
by the caller when a live Sheets connection is available — this module stays
pure and testable without any Sheets/network dependency.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Callable, Optional

from careeros.models import Job


def _union_tiers(survivor: Job, dropped_dup: Job) -> None:
    """v2.0: mutates `survivor.tiers` in place to include `dropped_dup`'s
    tiers too, so a job found by more than one query tier before collapsing
    doesn't silently attribute all credit to whichever tier happened to be
    first in list order. Job is a plain (non-frozen) dataclass, so this is a
    real in-place update, not a copy."""
    merged = set(survivor.tiers or []) | set(dropped_dup.tiers or [])
    survivor.tiers = sorted(merged) if merged else None


def dedupe_in_run(jobs: list[Job]) -> tuple[list[Job], list[Job]]:
    """Returns (unique, dropped) for one discovery batch."""
    seen_ids: dict[str, Job] = {}
    unique: list[Job] = []
    dropped: list[Job] = []
    for job in jobs:
        if job.id in seen_ids:
            _union_tiers(seen_ids[job.id], job)
            dropped.append(job)
        else:
            seen_ids[job.id] = job
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


def dedupe_cross_location(
    jobs: list[Job],
    *, on_duplicate: Optional[Callable[[Job, Job], None]] = None,
) -> tuple[list[Job], list[Job]]:
    """Returns (unique, dropped): collapses the same role posted once per
    country/office into a single entry. Keeps the FIRST occurrence in list
    order — since `discover` runs segmented queries in `profile.work_mode_
    priority` order and appends results in that same sequence, the surviving
    copy is naturally the one from the candidate's highest-priority work-mode
    tier (e.g. a role also posted in a lower-priority country is dropped in
    favor of the same role's higher-priority-tier posting), with zero extra
    ranking logic needed here.

    `on_duplicate(survivor, dropped_dup)`, if given, fires once per collapse
    — the only point where both the surviving and dropped Job are in scope
    together. Return signature is UNCHANGED from before this existed (both
    still plain `list[Job]`) specifically so every existing caller/test
    keeps working untouched; this is purely an opt-in side channel for
    `careeros/cli/pipeline.py`'s `dedupe` command to record which source's
    job survived over a Layer 2A one (or vice versa) — see
    `pipeline/ledger.py`'s `compute_run_source_stats` for what reads it.
    """
    seen_keys: dict[tuple[str, str, str], Job] = {}
    unique: list[Job] = []
    dropped: list[Job] = []
    for job in jobs:
        key = _cross_location_key(job)
        if key in seen_keys:
            survivor = seen_keys[key]
            _union_tiers(survivor, job)
            if on_duplicate is not None:
                on_duplicate(survivor, job)
            dropped.append(job)
        else:
            seen_keys[key] = job
            unique.append(job)
    return unique, dropped


def load_processed_ids(
    processed_path: Path | str, *, ttl_days: Optional[int] = None, as_of: Optional[str] = None
) -> set[str]:
    """Reads a processed/seen JSONL log. Each line is `{"id": ..., "date":
    ...}` (v2.0) or the legacy `{"id": ..., "first_seen": ...}` (pre-v2.0
    `seen.jsonl`) — both are accepted so an existing OSS install migrates
    with zero data loss, just by pointing the same reader at a differently-
    named file going forward.

    TTL is opt-in: pass BOTH `ttl_days` and `as_of` (the current run date,
    ISO `YYYY-MM-DD`) to let an entry older than `ttl_days` expire and
    resurface (a materially-changed reposting shouldn't be suppressed
    forever). Omit either and every entry is treated as never-expiring —
    this is what makes `load_seen_ids` below a strict special case of this
    function, not a second code path. An entry whose date can't be parsed
    (a `qa-*`/free-text run label, not an ISO date) is conservatively kept
    forever rather than guessed at.
    """
    processed_path = Path(processed_path)
    if not processed_path.exists():
        return set()
    cutoff = None
    if ttl_days is not None and as_of is not None:
        try:
            cutoff = date.fromisoformat(as_of) - timedelta(days=ttl_days)
        except ValueError:
            cutoff = None  # as_of itself isn't a real date -- don't expire anything
    ids: set[str] = set()
    with open(processed_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            if cutoff is not None:
                date_str = record.get("date") or record.get("first_seen")
                if date_str:
                    try:
                        if date.fromisoformat(date_str) < cutoff:
                            continue  # expired -- allow it to resurface
                    except ValueError:
                        pass  # unparseable -- keep (never expires)
            ids.add(record["id"])
    return ids


def append_processed(processed_path: Path | str, records: list[dict]) -> None:
    """Appends generic `{"id", "date", "terminal_stage", "reason", "score"}`
    records — one per job that reached ANY terminal decision this run
    (constraints-rejected, gate-dropped, evaluate-omitted, consider, or
    apply), not just the ones that made it to a Sheet row. See
    careeros/cli/pipeline.py's `threshold` command, the run's final
    deterministic stage and the only place that can see every stage's
    output at once."""
    processed_path = Path(processed_path)
    processed_path.parent.mkdir(parents=True, exist_ok=True)
    with open(processed_path, "a") as f:
        for record in records:
            f.write(json.dumps(record) + "\n")


def load_seen_ids(seen_path: Path | str) -> set[str]:
    """Backward-compatible alias: `load_processed_ids` with no TTL, i.e.
    every recorded id is treated as never-expiring. Kept as its own name
    because it's the one existing tests/callers already use this way."""
    return load_processed_ids(seen_path)


def append_seen_ids(seen_path: Path | str, jobs: list[Job], run_date: str) -> None:
    """Legacy writer (pre-v2.0). Still used nowhere in the live pipeline —
    `careeros/cli/pipeline.py`'s `threshold` command calls `append_processed`
    unconditionally instead (see this module's docstring for why) — but kept
    for any external tooling/tests still writing the old shape."""
    seen_path = Path(seen_path)
    seen_path.parent.mkdir(parents=True, exist_ok=True)
    with open(seen_path, "a") as f:
        for job in jobs:
            f.write(json.dumps({"id": job.id, "first_seen": run_date}) + "\n")


def dedupe_against_history(
    jobs: list[Job], seen_path: Path | str, *, ttl_days: Optional[int] = None, as_of: Optional[str] = None
) -> tuple[list[Job], list[Job]]:
    """Returns (unique, dropped) after filtering out ids already processed
    (see `load_processed_ids` for the optional TTL). This function only
    reads, so it stays safe to call repeatedly (e.g. for a dry-run) without
    mutating state — the actual append happens once, in `threshold`, via
    `append_processed`.
    """
    seen_ids = load_processed_ids(seen_path, ttl_days=ttl_days, as_of=as_of)
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
