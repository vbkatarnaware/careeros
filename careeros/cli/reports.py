"""Deterministic report rendering: the Level-1 per-job report and the
day-level executive summary — pure templates, zero AI."""

from __future__ import annotations

import json
import os
from typing import Optional

import typer

from careeros import budget, runmeta
from careeros.cli import app
from careeros.cli._shared import _config, _today
from careeros.config import Config
from careeros.models import Eval, Job
from careeros.report import render_daily_report, render_summary


# ── report render (deterministic) ────────────────────────────────────────

@app.command("render-report", hidden=True)
def render_report(job_id: str, date: str = typer.Option(None)):
    """[dev] Render the Level-1 daily report for one job — pure template, zero AI."""
    cfg = _config()
    date = date or _today()

    eval_path = runmeta.stage_dir(cfg.runs_dir, date, "evaluate") / f"{job_id}.json"
    jobs_path = runmeta.stage_dir(cfg.runs_dir, date, "normalize") / "jobs.json"
    with open(eval_path) as f:
        evaluation = Eval.from_dict(json.load(f))
    with open(jobs_path) as f:
        job_dict = next(j for j in json.load(f) if j["id"] == job_id)
    job = Job.from_dict(job_dict)

    artifacts = runmeta.artifacts_dir(cfg.runs_dir, date, job_id)
    resume_path = str(artifacts / "resume.pdf")
    cover_path = str(artifacts / "cover.md")

    report_md = render_daily_report(job, evaluation, resume_path, cover_path)
    report_path = artifacts / "daily_report.md"
    with open(report_path, "w") as f:
        f.write(report_md)

    typer.echo(f"[render-report] wrote {report_path}")


def _build_discovery_stats(cfg: Config, date: str) -> Optional[dict]:
    """P2.9 Discovery KPI join, extended for v1.2's multi-provider raw.json
    shape (`{"providers": [...], "items": {name: [...]}, "meta": {name:
    {...}}}` — see `discover`). Read-only over files `discover` already
    wrote plus the rolling-week budget state; fetches nothing, mutates
    nothing.

    The ATS-vs-job-board split and top-platforms list are Fantastic-Jobs-
    specific concepts (`source_type`/`source` are only meaningful on ITS
    items — providers/fantastic_jobs.py), so they're computed over ONLY that
    provider's own item slice, when it ran — never over other providers'
    items, which don't carry those fields.

    `stats["providers"]` is the NEW per-provider discovery-summary table
    (v1.2 revision #6): one entry per provider `discover` recorded (ran,
    skipped, or errored), straight from each `ProviderResult`'s persisted
    metadata — this is what `summary.md` renders as the discovery table."""
    raw_path = runmeta.stage_dir(cfg.runs_dir, date, "discover") / "raw.json"
    if not raw_path.exists():
        return None
    with open(raw_path) as f:
        raw = json.load(f)

    provider_names: list[str] = raw.get("providers", [])
    items_by_provider: dict = raw.get("items", {})
    meta_by_provider: dict = raw.get("meta", {})

    fj_items = items_by_provider.get("fantastic-jobs", [])
    ats_count = sum(1 for it in fj_items if it.get("source_type") == "ats")
    jb_count = len(fj_items) - ats_count if fj_items else 0

    platform_counts: dict[str, int] = {}
    for it in fj_items:
        src = it.get("source")
        if src:
            platform_counts[src] = platform_counts.get(src, 0) + 1
    top_platforms = sorted(platform_counts.items(), key=lambda kv: -kv[1])[:5]

    stats: dict = {"ats_count": ats_count, "jb_count": jb_count, "top_platforms": top_platforms}

    if "fantastic-jobs" in provider_names:
        fj_meta = meta_by_provider.get("fantastic-jobs", {})
        state = budget.load_state(cfg.careeros_dir, date)
        stats["requests_this_run"] = fj_meta.get("requests", 0)
        stats["requests_this_week"] = state.get("requests", 0)
        stats["records_this_run"] = fj_meta.get("records", len(fj_items))
        stats["records_this_week"] = state.get("records", 0)
        stats["records_quota"] = budget.weekly_quota(cfg.api)

    stats["providers"] = [
        {
            "provider": name,
            "records": len(items_by_provider.get(name, [])),
            "requests": meta_by_provider.get(name, {}).get("requests", 0),
            "cost_usd": meta_by_provider.get(name, {}).get("cost_usd", 0.0),
            "seconds": meta_by_provider.get(name, {}).get("seconds", 0.0),
            "skipped": meta_by_provider.get(name, {}).get("skipped", False),
            "skip_reason": meta_by_provider.get(name, {}).get("skip_reason"),
            # LIVE quota as reported by the provider's own API response on
            # this run (e.g. Fantastic Jobs' x-ratelimit-* headers) — never
            # a locally calculated estimate. None for providers that don't
            # report one. See AGENT_GUIDE.md / ProviderResult.live_quota.
            "live_quota": meta_by_provider.get(name, {}).get("live_quota"),
        }
        for name in provider_names
    ]
    stats["merged_total"] = sum(p["records"] for p in stats["providers"])

    return stats


@app.command("summary", hidden=True)
def summary(date: str = typer.Option(None)):
    """[dev] Render the day-level executive summary.md — pure template, zero
    AI. Funnel counts, the Apply (≥threshold) list, the Consider (near-miss)
    list, and cost-per-selected-job — the P2.6 KPI made visible every run.

    Reads `07_select/selected.json`/`consider.json` (the SAME partition
    `threshold` already computed via partition_evals) rather than re-deriving
    apply/consider from raw evals — the summary must never disagree with
    what actually got artifacts/Sheet rows.

    Written to TWO places: the internal `runs/<date>/summary.md` (unchanged,
    stage-pipeline convention) AND the stable `.careeros/results/<date>/`
    digest (v1.6.0, local-first mode) with a `latest` pointer — the one
    place a local-only candidate (no Sheets/Drive) is told to look, with
    relative links straight to each Apply job's rendered resume/cover PDF."""
    cfg = _config()
    date = date or _today()

    manifest = runmeta.load_manifest(cfg.runs_dir, date)

    select_dir = runmeta.stage_dir(cfg.runs_dir, date, "select")

    def _load_evals(filename: str) -> list[Eval]:
        path = select_dir / filename
        if not path.exists():
            return []
        with open(path) as f:
            return [Eval.from_dict(d) for d in json.load(f)]

    apply_evals = _load_evals("selected.json")
    consider_evals = _load_evals("consider.json")

    jobs_path = runmeta.stage_dir(cfg.runs_dir, date, "normalize") / "jobs.json"
    jobs_by_id = {}
    if jobs_path.exists():
        with open(jobs_path) as f:
            jobs_by_id = {j["id"]: Job.from_dict(j) for j in json.load(f)}

    discovery_stats = _build_discovery_stats(cfg, date)

    results_dir = runmeta.results_dir(cfg.careeros_dir, date)
    artifact_links: dict[str, dict[str, str]] = {}
    for e in apply_evals:
        artifacts_path = runmeta.artifacts_dir(cfg.runs_dir, date, e.id)
        links = {}
        for name, filename in (("resume", "resume.pdf"), ("cover", "cover.pdf")):
            if (artifacts_path / filename).exists():
                links[name] = os.path.relpath(artifacts_path / filename, results_dir)
        if links:
            artifact_links[e.id] = links

    summary_md = render_summary(date, manifest, apply_evals, consider_evals, jobs_by_id,
                                threshold=cfg.threshold, consider_threshold=cfg.consider_threshold,
                                discovery_stats=discovery_stats, artifact_links=artifact_links)

    summary_path = runmeta.run_dir(cfg.runs_dir, date) / "summary.md"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    with open(summary_path, "w") as f:
        f.write(summary_md)

    results_path = results_dir / "summary.md"
    with open(results_path, "w") as f:
        f.write(summary_md)
    runmeta.write_results_latest_pointer(cfg.careeros_dir, date)

    typer.echo(f"[summary] wrote {summary_path}")
    typer.echo(f"[summary] wrote {results_path} (also: .careeros/results/latest/)")
