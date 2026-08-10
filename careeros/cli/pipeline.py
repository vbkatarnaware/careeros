"""Deterministic pipeline stages: normalize, dedupe, constraints, threshold."""

from __future__ import annotations

import json
import time
from typing import Optional

import typer

from careeros import runmeta
from careeros import sheets as sheets_mod
from careeros.cli import app
from careeros.cli._shared import _config, _load_profile, _today
from careeros.models import Eval, Job, dumps
from careeros.pipeline.constraints import evaluate_constraints
from careeros.pipeline.dedupe import (
    append_processed, dedupe_against_history, dedupe_against_sheet_ids,
    dedupe_cross_location, dedupe_in_run,
)
from careeros.pipeline.normalize import normalize_all
from careeros.pipeline.outcomes import build_outcomes
from careeros.pipeline.threshold import partition_evals
from careeros.providers.registry import get as get_provider


# ── normalize ─────────────────────────────────────────────────────────────

@app.command(hidden=True)
def normalize(date: str = typer.Option(None, help="Run date, default today")):
    """[dev] Normalize: 01_discover/raw.json -> 02_normalize/jobs.json.

    v1.2: raw.json holds one item-list PER provider that ran (see
    `discover`) — this maps each provider's items with ITS OWN
    `to_job_dict`, then concatenates every provider's jobs into ONE flat
    list, in the same order `discover` ran them. Every stage from here on
    (dedupe onward) reads that flat list and has no idea how many providers
    contributed to it — that's what keeps the rest of the pipeline
    completely provider-agnostic."""
    cfg = _config()
    date = date or _today()

    raw_path = runmeta.stage_dir(cfg.runs_dir, date, "discover") / "raw.json"
    if not raw_path.exists():
        typer.echo(f"No {raw_path} — run `careeros discover` first.", err=True)
        raise typer.Exit(1)

    with open(raw_path) as f:
        raw = json.load(f)

    start = time.time()
    jobs: list[Job] = []
    total_raw = 0
    provenance_by_provider = raw.get("provenance", {})
    for provider_name in raw.get("providers", []):
        raw_items = raw.get("items", {}).get(provider_name, [])
        total_raw += len(raw_items)
        if not raw_items:
            continue
        p = get_provider(provider_name)
        jobs.extend(normalize_all(
            raw_items, p, source=provider_name,
            description_max_chars=cfg.description_max_chars,
            provenance=provenance_by_provider.get(provider_name),
        ))
    elapsed = time.time() - start

    out_path = runmeta.stage_dir(cfg.runs_dir, date, "normalize") / "jobs.json"
    with open(out_path, "w") as f:
        f.write(dumps([j.to_dict() for j in jobs]))

    typer.echo(f"[normalize] {total_raw} raw -> {len(jobs)} jobs ({elapsed:.1f}s)")
    runmeta.record_stage(cfg.runs_dir, date, "normalize",
                          count_in=total_raw, count_out=len(jobs), seconds=elapsed)


# ── dedupe ────────────────────────────────────────────────────────────────

# v2.0: how long a processed.jsonl entry suppresses a job before it's
# allowed to resurface (a materially-changed reposting shouldn't be
# suppressed forever). See careeros/pipeline/dedupe.py's load_processed_ids.
PROCESSED_TTL_DAYS = 30


@app.command(hidden=True)
def dedupe(
    date: str = typer.Option(None, help="Run date, default today"),
    against_sheet: bool = typer.Option(True, help="Also dedupe against the Sheet's existing Job IDs (only if sheets.enabled)"),
    replay: bool = typer.Option(
        False, "--replay",
        help="Bypass the vs-history check for this run only (processed.jsonl is not read or written to differently -- "
             "every job that was already processed is let through again). Use to re-run a day's jobs through gate/"
             "evaluate deliberately, e.g. after a prompt change you want to re-verify against known jobs.",
    ),
):
    """[dev] Dedupe: in-run + cross-location + vs history (+ vs Sheet) ->
    03_dedupe/{unique,dropped}.json.

    v2.0: "vs history" now reads `.careeros/processed.jsonl`, written
    unconditionally by `threshold` (this run's final deterministic stage)
    for EVERY job that reached a terminal decision — constraints-rejected,
    gate-dropped, evaluate-omitted, consider, or apply. This replaced the
    old `.careeros/seen.jsonl`, which was only ever written from
    `sheets_append` post-`sheets.enabled`-check and only for Apply/Consider
    ids — meaning local-mode users (sheets.enabled: false) never got ANY
    history tracking at all, so every rejected job re-entered the pipeline
    and re-burned gate/eval tokens on every run (56% same-run duplicates,
    found live 2026-07-31). `load_processed_ids` also reads the legacy
    `first_seen` key, so an existing `seen.jsonl` keeps working with zero
    migration step if you point this at it."""
    cfg = _config()
    date = date or _today()

    jobs_path = runmeta.stage_dir(cfg.runs_dir, date, "normalize") / "jobs.json"
    if not jobs_path.exists():
        typer.echo(f"No {jobs_path} — run `careeros normalize` first.", err=True)
        raise typer.Exit(1)

    with open(jobs_path) as f:
        jobs = [Job.from_dict(d) for d in json.load(f)]

    start = time.time()
    unique, dropped_in_run = dedupe_in_run(jobs)
    # v2.1: record which SOURCE survived over which, for every cross-location
    # collapse — the only point survivor+dropped are both in scope together.
    # Read by `pipeline/ledger.py`'s `compute_run_source_stats` to answer
    # "how many Layer 2A (ats-watchlist) jobs were duplicates of a Layer 1
    # (ats-dataset) job" — a job.id already appearing once here is dropped
    # exactly once, so this dict is never overwritten mid-run.
    duplicate_survivor_source: dict[str, str] = {}

    def _record_cross_location_duplicate(survivor: Job, dropped_dup: Job) -> None:
        duplicate_survivor_source[dropped_dup.id] = survivor.source

    unique, dropped_cross_location = dedupe_cross_location(
        unique, on_duplicate=_record_cross_location_duplicate,
    )

    if replay:
        dropped_history: list[Job] = []
        typer.echo("[dedupe] --replay: vs-history check skipped for this run.")
    else:
        processed_path = cfg.careeros_dir / "processed.jsonl"
        unique, dropped_history = dedupe_against_history(
            unique, processed_path, ttl_days=PROCESSED_TTL_DAYS, as_of=date,
        )

    dropped_sheet: list[Job] = []
    if against_sheet and cfg.sheets.get("enabled", False):
        try:
            sheet_ids = sheets_mod.read_existing_job_ids(cfg)
            unique, dropped_sheet = dedupe_against_sheet_ids(unique, sheet_ids)
        except RuntimeError as e:
            typer.echo(f"[dedupe] Sheets dedupe skipped: {e}")
    elif against_sheet:
        typer.echo("[dedupe] Sheets dedupe skipped: sheets.enabled is false.")

    elapsed = time.time() - start
    all_dropped = dropped_in_run + dropped_cross_location + dropped_history + dropped_sheet

    stage_path = runmeta.stage_dir(cfg.runs_dir, date, "dedupe")
    with open(stage_path / "unique.json", "w") as f:
        f.write(dumps([j.to_dict() for j in unique]))
    # v2.0: each dropped job keeps a `_drop_reason` (in-run | cross-location |
    # history | sheet), mirroring the `_reject_reasons` convention constraints
    # already uses -- previously this was a flat concat with no way to tell
    # which pass dropped a given job after the fact.
    # v2.1: a `cross-location` entry ALSO gets `_duplicate_of_source` — the
    # source of the job that survived over it, from `duplicate_survivor_
    # source` above. Only meaningful for this one drop reason (in-run/
    # history/sheet duplicates were never compared against a DIFFERENT
    # source's job in the first place).
    def _tag(reason: str, batch: list[Job]) -> list[dict]:
        tagged = []
        for j in batch:
            d = {**j.to_dict(), "_drop_reason": reason}
            if reason == "cross-location" and j.id in duplicate_survivor_source:
                d["_duplicate_of_source"] = duplicate_survivor_source[j.id]
            tagged.append(d)
        return tagged

    tagged_dropped = (
        _tag("in-run", dropped_in_run) + _tag("cross-location", dropped_cross_location)
        + _tag("history", dropped_history) + _tag("sheet", dropped_sheet)
    )
    with open(stage_path / "dropped.json", "w") as f:
        f.write(dumps(tagged_dropped))

    typer.echo(f"[dedupe] {len(jobs)} in -> {len(unique)} unique, {len(all_dropped)} dropped "
               f"(in-run: {len(dropped_in_run)}, cross-location: {len(dropped_cross_location)}, "
               f"history: {len(dropped_history)}, sheet: {len(dropped_sheet)}) ({elapsed:.1f}s)")
    runmeta.record_stage(cfg.runs_dir, date, "dedupe",
                          count_in=len(jobs), count_out=len(unique), seconds=elapsed)


# ── constraints (deterministic hard deal-breakers) ───────────────────────

@app.command(hidden=True)
def constraints(date: str = typer.Option(None, help="Run date, default today")):
    """[dev] Constraints: apply hard deal-breakers (location, salary) to
    03_dedupe/unique.json -> 04_constraints/{eligible,rejected}.json.
    Rejected jobs never reach the AI gate, so no tokens are spent on them."""
    cfg = _config()
    date = date or _today()

    unique_path = runmeta.stage_dir(cfg.runs_dir, date, "dedupe") / "unique.json"
    if not unique_path.exists():
        typer.echo(f"No {unique_path} — run `careeros dedupe` first.", err=True)
        raise typer.Exit(1)
    with open(unique_path) as f:
        jobs = [Job.from_dict(d) for d in json.load(f)]

    profile = _load_profile(cfg)
    start = time.time()
    eligible: list[dict] = []
    rejected: list[dict] = []
    for job in jobs:
        result = evaluate_constraints(job, profile, cfg.fx_rates)
        if result.passed:
            eligible.append(job.to_dict())
        else:
            rejected.append({**job.to_dict(), "_reject_reasons": result.reasons})
    elapsed = time.time() - start

    stage_dir = runmeta.stage_dir(cfg.runs_dir, date, "constraints")
    with open(stage_dir / "eligible.json", "w") as f:
        f.write(dumps(eligible))
    with open(stage_dir / "rejected.json", "w") as f:
        f.write(dumps(rejected))

    typer.echo(f"[constraints] {len(jobs)} in -> {len(eligible)} eligible, "
               f"{len(rejected)} hard-rejected ({elapsed:.2f}s)")
    runmeta.record_stage(cfg.runs_dir, date, "constraints",
                          count_in=len(jobs), count_out=len(eligible), seconds=elapsed)


# ── threshold ─────────────────────────────────────────────────────────────

@app.command(hidden=True)
def threshold(
    date: str = typer.Option(None, help="Run date, default today"),
    min_score: Optional[float] = typer.Option(None, help="Override config.threshold (APPLY tier)"),
    consider_min: Optional[float] = typer.Option(None, help="Override config.consider_threshold (CONSIDER tier)"),
):
    """[dev] Two-tier threshold. APPLY: score >= threshold, recommendation
    "apply", passing hard constraints -> full pipeline. CONSIDER:
    consider_threshold <= score < threshold (constraints pass) -> Sheet row
    only, no artifacts/Drive. Below consider_threshold -> omitted (still
    written to 07_select/omitted.json — v2.0; previously discarded entirely,
    see partition_evals's third return value). See
    careeros/pipeline/threshold.py:partition_evals.

    v2.0: this is also where `.careeros/processed.jsonl` gets written —
    unconditionally, for every job that reached a terminal decision this
    run (constraints-rejected, gate-dropped, evaluate-omitted, consider,
    apply) — because `threshold` is the run's last deterministic stage and
    the only place that can see every earlier stage's output at once. See
    careeros/pipeline/dedupe.py's module docstring for why this replaced
    the old sheets_append-only `seen.jsonl` write."""
    cfg = _config()
    date = date or _today()
    min_score = min_score if min_score is not None else cfg.threshold
    consider_min = consider_min if consider_min is not None else cfg.consider_threshold
    start = time.time()

    eval_dir = runmeta.stage_dir(cfg.runs_dir, date, "evaluate")
    evals = []
    for path in eval_dir.glob("*.json"):
        if path.name.startswith("_"):
            continue
        with open(path) as f:
            evals.append(Eval.from_dict(json.load(f)))

    # Every evaluated job already passed `constraints`, but re-checking here
    # (via partition_evals) is the deterministic backstop against the AI
    # mislabeling a hard-reject as "apply" — see careeros/pipeline/threshold.py.
    jobs_path = runmeta.stage_dir(cfg.runs_dir, date, "normalize") / "jobs.json"
    with open(jobs_path) as f:
        jobs_by_id = {j["id"]: Job.from_dict(j) for j in json.load(f)}

    profile = _load_profile(cfg)
    apply_, consider_, omit_ = partition_evals(
        evals, min_score, consider_min, jobs_by_id, profile, cfg.fx_rates)

    stage_dir = runmeta.stage_dir(cfg.runs_dir, date, "select")
    with open(stage_dir / "selected.json", "w") as f:
        f.write(dumps([e.to_dict() for e in apply_]))
    with open(stage_dir / "consider.json", "w") as f:
        f.write(dumps([e.to_dict() for e in consider_]))
    with open(stage_dir / "omitted.json", "w") as f:
        f.write(dumps([e.to_dict() for e in omit_]))

    # v2.0: WHY each job landed where it did, derived purely from its own
    # rubric (weighted contribution shortfall — see careeros/pipeline/
    # outcomes.py for why raw argmin over the rubric gets this wrong) plus
    # the discovery tier(s) that surfaced it. Zero AI, fully regenerable —
    # this is the input the learning ledger aggregates, never a source of
    # truth on its own.
    tiers_by_id = {jid: job.tiers for jid, job in jobs_by_id.items()}
    outcomes = build_outcomes(apply_, consider_, omit_, tiers_by_id=tiers_by_id)
    with open(stage_dir / "outcomes.json", "w") as f:
        f.write(dumps([o.to_dict() for o in outcomes]))

    typer.echo(
        f"[threshold] {len(evals)} evaluated -> {len(apply_)} APPLY (>= {min_score}), "
        f"{len(consider_)} CONSIDER ([{consider_min}, {min_score})), "
        f"{len(omit_)} omitted "
        f"(top: {apply_[0].score if apply_ else 'n/a'})"
    )
    runmeta.record_stage(cfg.runs_dir, date, "select",
                          count_in=len(evals), count_out=len(apply_),
                          seconds=time.time() - start)

    _write_processed(cfg, date, jobs_by_id, apply_, consider_, omit_)


def _write_processed(
    cfg, date: str, jobs_by_id: dict[str, Job],
    apply_: list[Eval], consider_: list[Eval], omit_: list[Eval],
) -> None:
    """Unconditional history write, covering every job that reached a
    terminal decision anywhere this run — not just Apply/Consider. Reads
    constraints/rejected.json and gate/gated.json (both already on disk by
    the time `threshold` runs) in addition to this stage's own three
    partitions, so a job dropped at ANY stage stops re-entering the
    pipeline tomorrow."""
    records: list[dict] = []

    rejected_path = runmeta.stage_dir(cfg.runs_dir, date, "constraints") / "rejected.json"
    if rejected_path.exists():
        with open(rejected_path) as f:
            for j in json.load(f):
                reasons = j.get("_reject_reasons") or []
                records.append({
                    "id": j["id"], "date": date, "terminal_stage": "constraints",
                    "reason": "; ".join(reasons) if reasons else None, "score": None,
                })

    gated_path = runmeta.stage_dir(cfg.runs_dir, date, "gate") / "gated.json"
    if gated_path.exists():
        with open(gated_path) as f:
            for r in json.load(f):
                if r.get("keep"):
                    continue
                records.append({
                    "id": r["id"], "date": date, "terminal_stage": "gate",
                    "reason": r.get("reason"), "score": None,
                })

    for tier_name, evals in (("omit", omit_), ("consider", consider_), ("apply", apply_)):
        for e in evals:
            records.append({
                "id": e.id, "date": date, "terminal_stage": "select",
                "reason": tier_name, "score": e.score,
            })

    processed_path = cfg.careeros_dir / "processed.jsonl"
    append_processed(processed_path, records)
    typer.echo(f"[threshold] recorded {len(records)} processed id(s) to {processed_path}")
