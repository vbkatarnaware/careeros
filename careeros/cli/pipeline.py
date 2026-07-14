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
    dedupe_against_history, dedupe_against_sheet_ids, dedupe_cross_location, dedupe_in_run,
)
from careeros.pipeline.normalize import normalize_all
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
    for provider_name in raw.get("providers", []):
        raw_items = raw.get("items", {}).get(provider_name, [])
        total_raw += len(raw_items)
        if not raw_items:
            continue
        p = get_provider(provider_name)
        jobs.extend(normalize_all(raw_items, p, source=provider_name,
                                   description_max_chars=cfg.description_max_chars))
    elapsed = time.time() - start

    out_path = runmeta.stage_dir(cfg.runs_dir, date, "normalize") / "jobs.json"
    with open(out_path, "w") as f:
        f.write(dumps([j.to_dict() for j in jobs]))

    typer.echo(f"[normalize] {total_raw} raw -> {len(jobs)} jobs ({elapsed:.1f}s)")
    runmeta.record_stage(cfg.runs_dir, date, "normalize",
                          count_in=total_raw, count_out=len(jobs), seconds=elapsed)


# ── dedupe ────────────────────────────────────────────────────────────────

@app.command(hidden=True)
def dedupe(
    date: str = typer.Option(None, help="Run date, default today"),
    against_sheet: bool = typer.Option(True, help="Also dedupe against the Sheet's existing Job IDs"),
):
    """[dev] Dedupe: in-run + cross-location + vs history (+ vs Sheet) ->
    03_dedupe/{unique,dropped}.json."""
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
    unique, dropped_cross_location = dedupe_cross_location(unique)

    seen_path = cfg.careeros_dir / "seen.jsonl"
    unique, dropped_history = dedupe_against_history(unique, seen_path)

    dropped_sheet: list[Job] = []
    if against_sheet:
        try:
            sheet_ids = sheets_mod.read_existing_job_ids(cfg)
            unique, dropped_sheet = dedupe_against_sheet_ids(unique, sheet_ids)
        except RuntimeError as e:
            typer.echo(f"[dedupe] Sheets dedupe skipped: {e}")

    elapsed = time.time() - start
    all_dropped = dropped_in_run + dropped_cross_location + dropped_history + dropped_sheet

    stage_path = runmeta.stage_dir(cfg.runs_dir, date, "dedupe")
    with open(stage_path / "unique.json", "w") as f:
        f.write(dumps([j.to_dict() for j in unique]))
    with open(stage_path / "dropped.json", "w") as f:
        f.write(dumps([j.to_dict() for j in all_dropped]))

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
    only, no artifacts/Drive. Below consider_threshold -> omitted. See
    careeros/pipeline/threshold.py:partition_evals."""
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
    apply_, consider_, _omit = partition_evals(
        evals, min_score, consider_min, jobs_by_id, profile, cfg.fx_rates)

    stage_dir = runmeta.stage_dir(cfg.runs_dir, date, "select")
    with open(stage_dir / "selected.json", "w") as f:
        f.write(dumps([e.to_dict() for e in apply_]))
    with open(stage_dir / "consider.json", "w") as f:
        f.write(dumps([e.to_dict() for e in consider_]))

    typer.echo(
        f"[threshold] {len(evals)} evaluated -> {len(apply_)} APPLY (>= {min_score}), "
        f"{len(consider_)} CONSIDER ([{consider_min}, {min_score})) "
        f"(top: {apply_[0].score if apply_ else 'n/a'})"
    )
    runmeta.record_stage(cfg.runs_dir, date, "select",
                          count_in=len(evals), count_out=len(apply_),
                          seconds=time.time() - start)
