"""careeros CLI.

Thin dispatch only — every command here calls into careeros/{config,models,
cache,runmeta,lint,report,sheets}.py or careeros/{providers,pipeline}/. No
business logic lives in this file.

Two tiers of commands:
  - End-user:  init, start, daily, prep, apply, config, providers
  - Developer: discover, normalize, dedupe, constraints, gate, evaluate,
               threshold, artifacts, sheets, lint, verify-resume — each
               stage runnable standalone against a run directory, for
               debugging without re-running the whole pipeline.

AI stages (gate, evaluate, artifacts) follow the host-CLI execution
boundary: a `--prepare` half (Python writes the stage's input + an
instruction for the agent) and a `--finalize` half (Python validates
whatever the agent wrote). See skills/daily.md for the full instruction
sequence.

`constraints` is deterministic: it hard-rejects jobs violating an objective
profile deal-breaker (location, salary floor) BEFORE any AI is spent, and
`threshold` re-checks the same constraints as a backstop so a hard-rejected
job can never slip through as "apply" even if the AI mislabels it.
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path
from typing import Optional

import typer
import yaml

from careeros.cache import Cache, artifact_cache_key, eval_cache_key
from careeros.config import Config, load_config
from careeros.lint import format_issues, lint_file, verify_resume_bullets
from careeros.models import Eval, Job, Profile, dumps
from careeros.pipeline.dedupe import (
    append_seen_ids, dedupe_against_history, dedupe_against_sheet_ids,
    dedupe_cross_location, dedupe_in_run,
)
from careeros.pipeline.constraints import evaluate_constraints
from careeros.pipeline.normalize import normalize_all
from careeros.pipeline.queryplan import build_query_plan, resolve_tier_limit
from careeros.pipeline.threshold import select_final
from careeros.providers.base import ProviderError
from careeros.providers.registry import get as get_provider
from careeros.providers.registry import list_providers
from careeros.report import render_daily_report, render_summary
from careeros import runmeta
from careeros import sheets as sheets_mod

app = typer.Typer(add_completion=False, no_args_is_help=True,
                   help="CareerOS — a supreme, high-quality job discovery and recommendation engine.")

REPO_ROOT = Path(__file__).resolve().parent.parent


def _provider_query_cfg(cfg: Config, provider_name: str) -> dict:
    """Which provider-config block pipeline/queryplan.py's neutral
    title_search/location_search/work_arrangement keys should overlay onto
    for `discover`'s segmented-query plan (P2.7) — provider-keyed since
    query-plan config is provider-specific, not a single generic Config
    field. `config.api` and `config.apify` share the same key names by
    design (see config.py), so queryplan.py itself never has to know which
    provider is active."""
    if provider_name == "fantastic-jobs-actor":
        return cfg.apify
    return cfg.api


def _today() -> str:
    """Run date. Callers may override via --date for reproducible/resumed
    runs; this is the only place "today" is computed so tests can pass a
    fixed date instead."""
    import datetime
    return datetime.date.today().isoformat()


def _config() -> Config:
    return load_config()


# ── init ──────────────────────────────────────────────────────────────────

@app.command()
def init():
    """Scaffold .careeros/ (config, profile template, cache/runs dirs)."""
    careeros_dir = Path(".careeros")
    careeros_dir.mkdir(exist_ok=True)
    (careeros_dir / "cache").mkdir(exist_ok=True)
    (careeros_dir / "runs").mkdir(exist_ok=True)

    config_path = careeros_dir / "config.yaml"
    if not config_path.exists():
        shutil.copy(REPO_ROOT / "templates" / "config.example.yaml", config_path)
        typer.echo(f"Wrote {config_path}")
    else:
        typer.echo(f"{config_path} already exists — left untouched")

    profile_path = careeros_dir / "profile.yaml"
    if not profile_path.exists():
        shutil.copy(REPO_ROOT / "templates" / "profile.example.yaml", profile_path)
        typer.echo(f"Wrote {profile_path} (seeded template — edit with your own facts,"
                    " or run `careeros start` for the guided interview)")
    else:
        typer.echo(f"{profile_path} already exists — left untouched")

    typer.echo(
        "\nNext: in .careeros/config.yaml, set api.transport to \"direct\" or \"rapidapi\" "
        "and the matching key env var (FANTASTIC_API_KEY / RAPIDAPI_KEY), set up Sheets "
        "credentials, then run `careeros daily`. (Prefer the legacy Apify actor instead? "
        "Set provider: fantastic-jobs-actor and APIFY_TOKEN — see providers/README.md.)"
    )


# ── providers / config ───────────────────────────────────────────────────

@app.command()
def providers():
    """List registered discovery providers."""
    for name in list_providers():
        typer.echo(name)


@app.command()
def config():
    """Print the resolved config."""
    cfg = _config()
    typer.echo(yaml.dump({
        "provider": cfg.provider, "threshold": cfg.threshold,
        "gate_batch_size": cfg.gate_batch_size, "prompts": cfg.prompts,
        "sheets": cfg.sheets,
    }, sort_keys=False))


# ── discover ──────────────────────────────────────────────────────────────

@app.command()
def discover(
    provider: Optional[str] = typer.Option(None, help="Provider id (default: config.provider)"),
    date: str = typer.Option(None, help="Run date, default today"),
    limit: int = typer.Option(
        100, help="Default max jobs to fetch per query; overridden per-tier by config.apify.tier_limits"),
    search: str = typer.Option(
        "", help="Manual single-query override — bypasses profile-driven segmentation"),
    dry_run: bool = typer.Option(False, help="Fetch and print, don't write raw.json"),
):
    """[dev] Discover: call a provider, write 01_discover/raw.json.

    By default (discovery_mode: "profile") this runs one segmented query per
    profile.work_mode_priority tier — see pipeline/queryplan.py; the
    discovery benchmark found a single broad query yields far fewer
    apply-worthy jobs than targeted per-work-mode ones. `discovery_mode:
    "single"`, `--search`, or a missing profile.yaml all fall back to
    today's one-query behavior."""
    cfg = _config()
    date = date or _today()
    provider_name = provider or cfg.provider
    p = get_provider(provider_name)
    provider_cfg = _provider_query_cfg(cfg, provider_name)

    if search or not cfg.profile_path.exists():
        queries: list[Optional[dict]] = [None]
    else:
        queries = build_query_plan(_load_profile(cfg), provider_cfg) or [None]

    raw_items: list = []
    total_cost_usd = 0.0
    start = time.time()
    try:
        for i, query in enumerate(queries):
            work_mode = (query or {}).get("_work_mode", "single")
            effective_limit = resolve_tier_limit(work_mode, provider_cfg, limit)
            items, query_cost = p.fetch(cfg, limit=effective_limit, search=search, query=query)
            total_cost_usd += query_cost
            typer.echo(
                f"  [discover] query {i + 1}/{len(queries)} ({work_mode}, "
                f"limit={effective_limit}): {len(items)} items (${query_cost:.4f})"
            )
            raw_items.extend(items)
    except ProviderError as e:
        typer.echo(f"[discover] {e}", err=True)
        raise typer.Exit(1)
    elapsed = time.time() - start

    typer.echo(
        f"[discover] {provider_name}: {len(raw_items)} raw items across "
        f"{len(queries)} quer{'y' if len(queries) == 1 else 'ies'} "
        f"(${total_cost_usd:.4f}, {elapsed:.1f}s)"
    )

    if dry_run:
        typer.echo(dumps(raw_items[:3]))
        return

    stage_path = runmeta.stage_dir(cfg.runs_dir, date, "discover")
    with open(stage_path / "raw.json", "w") as f:
        f.write(dumps({
            "provider": provider_name,
            "queries": [(q or {}).get("_work_mode", "single") for q in queries],
            "items": raw_items,
        }))

    runmeta.record_stage(cfg.runs_dir, date, "discover",
                          count_in=0, count_out=len(raw_items), seconds=elapsed,
                          apify_cost_usd=total_cost_usd)


# ── normalize ─────────────────────────────────────────────────────────────

@app.command()
def normalize(date: str = typer.Option(None, help="Run date, default today")):
    """[dev] Normalize: 01_discover/raw.json -> 02_normalize/jobs.json."""
    cfg = _config()
    date = date or _today()

    raw_path = runmeta.stage_dir(cfg.runs_dir, date, "discover") / "raw.json"
    if not raw_path.exists():
        typer.echo(f"No {raw_path} — run `careeros discover` first.", err=True)
        raise typer.Exit(1)

    import json
    with open(raw_path) as f:
        raw = json.load(f)
    provider_name = raw["provider"]
    p = get_provider(provider_name)

    start = time.time()
    jobs = normalize_all(raw["items"], p, source=provider_name,
                          description_max_chars=cfg.description_max_chars)
    elapsed = time.time() - start

    out_path = runmeta.stage_dir(cfg.runs_dir, date, "normalize") / "jobs.json"
    with open(out_path, "w") as f:
        f.write(dumps([j.to_dict() for j in jobs]))

    typer.echo(f"[normalize] {len(raw['items'])} raw -> {len(jobs)} jobs ({elapsed:.1f}s)")
    runmeta.record_stage(cfg.runs_dir, date, "normalize",
                          count_in=len(raw["items"]), count_out=len(jobs), seconds=elapsed)


# ── dedupe ────────────────────────────────────────────────────────────────

@app.command()
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

    import json
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

@app.command()
def constraints(date: str = typer.Option(None, help="Run date, default today")):
    """[dev] Constraints: apply hard deal-breakers (location, salary) to
    03_dedupe/unique.json -> 04_constraints/{eligible,rejected}.json.
    Rejected jobs never reach the AI gate, so no tokens are spent on them."""
    cfg = _config()
    date = date or _today()

    import json
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


# ── gate (AI stage: prepare / finalize) ──────────────────────────────────

@app.command()
def gate(
    date: str = typer.Option(None, help="Run date, default today"),
    prepare: bool = typer.Option(False, "--prepare", help="Write gate input + print agent instructions"),
    finalize: bool = typer.Option(False, "--finalize", help="Validate agent-written gated.json"),
):
    """[dev] AI Gate: cheap batched keep/drop triage. See prompts/gate_v1.md."""
    cfg = _config()
    date = date or _today()

    if prepare:
        _gate_prepare(cfg, date)
    elif finalize:
        _gate_finalize(cfg, date)
    else:
        typer.echo("Pass --prepare or --finalize.", err=True)
        raise typer.Exit(1)


def _gate_prepare(cfg: Config, date: str) -> None:
    import json
    eligible_path = runmeta.stage_dir(cfg.runs_dir, date, "constraints") / "eligible.json"
    if not eligible_path.exists():
        typer.echo(f"No {eligible_path} — run `careeros constraints` first.", err=True)
        raise typer.Exit(1)
    with open(eligible_path) as f:
        jobs = json.load(f)

    stage_dir = runmeta.stage_dir(cfg.runs_dir, date, "gate")
    batch_size = cfg.gate_batch_size
    batches = [jobs[i:i + batch_size] for i in range(0, len(jobs), batch_size)]
    input_paths = []
    for i, batch in enumerate(batches):
        input_path = stage_dir / f"_input_{i}.json"
        with open(input_path, "w") as f:
            f.write(dumps(batch))
        input_paths.append(input_path)

    estimated_tokens = runmeta.estimate_tokens(*input_paths)
    runmeta.write_stage_meta(cfg.runs_dir, date, "gate", {
        "prepared_at": time.time(), "estimated_tokens": estimated_tokens,
    })

    prompt_path = cfg.prompt_path("gate")
    typer.echo(
        f"[gate:prepare] {len(jobs)} jobs -> {len(batches)} batch(es) of up to {batch_size}.\n\n"
        f"AGENT INSTRUCTIONS:\n"
        f"Read {prompt_path} and .careeros/profile.yaml.\n"
        f"For each 05_gate/_input_N.json batch, write 05_gate/_output_N.json:\n"
        f'  {{"results": [{{"id","keep","reason","confidence"}}, ...]}}\n'
        f"One result per job in that batch. Then run:\n"
        f"  careeros gate --finalize --date {date}"
    )


def _gate_finalize(cfg: Config, date: str) -> None:
    import json
    stage_dir = runmeta.stage_dir(cfg.runs_dir, date, "gate")
    output_files = sorted(stage_dir.glob("_output_*.json"))
    if not output_files:
        typer.echo(f"No _output_*.json found in {stage_dir} — agent hasn't written gate results yet.", err=True)
        raise typer.Exit(1)

    all_results = []
    for path in output_files:
        with open(path) as f:
            data = json.load(f)
        all_results.extend(data.get("results", []))

    errors = []
    for r in all_results:
        for field in ("id", "keep", "reason", "confidence"):
            if field not in r:
                errors.append(f"{r.get('id', '?')}: missing field '{field}'")

    if errors:
        typer.echo("[gate:finalize] Validation FAILED:\n" + "\n".join(errors), err=True)
        typer.echo(f"\nAgent: fix the listed items in their _output_N.json file, "
                    f"then re-run `careeros gate --finalize --date {date}`.")
        raise typer.Exit(1)

    input_files = sorted(stage_dir.glob("_input_*.json"))
    total_in = sum(len(json.load(open(p))) for p in input_files)
    kept = [r for r in all_results if r["keep"]]

    with open(stage_dir / "gated.json", "w") as f:
        f.write(dumps(all_results))

    meta = runmeta.read_stage_meta(cfg.runs_dir, date, "gate")
    elapsed = time.time() - meta["prepared_at"] if "prepared_at" in meta else 0.0

    typer.echo(f"[gate:finalize] {total_in} in -> {len(kept)} kept, {total_in - len(kept)} dropped.")
    runmeta.record_stage(cfg.runs_dir, date, "gate", count_in=total_in, count_out=len(kept),
                          seconds=elapsed, prompt_version=cfg.prompts.get("gate"),
                          estimated_tokens=meta.get("estimated_tokens", 0))


# ── evaluate (AI stage: prepare / finalize, cache-checked) ──────────────

@app.command()
def evaluate(
    date: str = typer.Option(None, help="Run date, default today"),
    prepare: bool = typer.Option(False, "--prepare"),
    finalize: bool = typer.Option(False, "--finalize"),
):
    """[dev] Final Evaluation: score against the profile, cache-checked.
    Writes 06_evaluate/<job-id>.json — the source of truth every later
    artifact reads. See prompts/eval_v2.md."""
    cfg = _config()
    date = date or _today()

    if prepare:
        _evaluate_prepare(cfg, date)
    elif finalize:
        _evaluate_finalize(cfg, date)
    else:
        typer.echo("Pass --prepare or --finalize.", err=True)
        raise typer.Exit(1)


def _load_profile(cfg: Config) -> Profile:
    with open(cfg.profile_path) as f:
        return Profile.from_dict(yaml.safe_load(f))


def _evaluate_prepare(cfg: Config, date: str) -> None:
    import json
    gate_path = runmeta.stage_dir(cfg.runs_dir, date, "gate") / "gated.json"
    eligible_path = runmeta.stage_dir(cfg.runs_dir, date, "constraints") / "eligible.json"
    if not gate_path.exists() or not eligible_path.exists():
        typer.echo("Missing gate/constraints output — run those stages first.", err=True)
        raise typer.Exit(1)

    with open(gate_path) as f:
        gated = {r["id"]: r for r in json.load(f)}
    with open(eligible_path) as f:
        jobs_by_id = {j["id"]: j for j in json.load(f)}

    kept_ids = [jid for jid, r in gated.items() if r["keep"]]
    profile = _load_profile(cfg)
    prompt_version = cfg.prompts.get("eval", "v1")
    cache = Cache(cfg.cache_dir)

    to_evaluate = []
    cache_hits = 0
    stage_dir = runmeta.stage_dir(cfg.runs_dir, date, "evaluate")
    for job_id in kept_ids:
        job = jobs_by_id[job_id]
        job_hash = Job.from_dict(job).content_hash()
        key = eval_cache_key(job_hash, profile.version, prompt_version)
        cached = cache.get("evaluate", key)
        if cached:
            with open(stage_dir / f"{job_id}.json", "w") as f:
                f.write(dumps(cached))
            cache_hits += 1
        else:
            to_evaluate.append({"job": job, "job_hash": job_hash})

    input_path = stage_dir / "_input.json"
    if to_evaluate:
        with open(input_path, "w") as f:
            f.write(dumps(to_evaluate))

    # eval_v2.md reads the FULL profile.yaml (unlike gate's headline-only
    # subset), so it's counted once per prepare call alongside the job batch.
    estimated_tokens = (
        runmeta.estimate_tokens(input_path, cfg.profile_path) if to_evaluate else 0
    )
    runmeta.write_stage_meta(cfg.runs_dir, date, "evaluate", {
        "prepared_at": time.time(), "cache_hits": cache_hits, "cache_misses": len(to_evaluate),
        "estimated_tokens": estimated_tokens,
    })

    prompt_path = cfg.prompt_path("eval")
    typer.echo(
        f"[evaluate:prepare] {len(kept_ids)} gated jobs: {cache_hits} cache hits (written directly), "
        f"{len(to_evaluate)} need evaluation.\n\n"
        + (
            f"AGENT INSTRUCTIONS:\n"
            f"Read {prompt_path} and .careeros/profile.yaml.\n"
            f"For each entry in 06_evaluate/_input.json, write 06_evaluate/<id>.json\n"
            f"matching schemas/eval.schema.json (set job_hash from the input entry,\n"
            f"profile_version={profile.version}, prompt_version=\"{prompt_version}\").\n"
            f"Then run:\n  careeros evaluate --finalize --date {date}"
            if to_evaluate else "Nothing to do — run `careeros evaluate --finalize` to finalize."
        )
    )


def _evaluate_finalize(cfg: Config, date: str) -> None:
    import json
    stage_dir = runmeta.stage_dir(cfg.runs_dir, date, "evaluate")
    input_path = stage_dir / "_input.json"
    expected_ids = set()
    if input_path.exists():
        with open(input_path) as f:
            expected_ids = {e["job"]["id"] for e in json.load(f)}

    all_records = []
    missing = []
    for job_id in expected_ids:
        out_path = stage_dir / f"{job_id}.json"
        if not out_path.exists():
            missing.append(job_id)
            continue
        with open(out_path) as f:
            all_records.append(json.load(f))

    # Also fold in cache-hit files already written during --prepare, so the
    # finalize summary reflects the FULL evaluated set for this run, not just
    # the freshly-generated ones.
    for path in stage_dir.glob("*.json"):
        if path.name in ("_input.json",):
            continue
        job_id = path.stem
        if job_id not in expected_ids:
            with open(path) as f:
                all_records.append(json.load(f))

    if missing:
        typer.echo(f"[evaluate:finalize] Missing output for: {', '.join(missing)}", err=True)
        typer.echo("Agent: write the missing files, then re-run --finalize.")
        raise typer.Exit(1)

    errors = runmeta.validate_stage("eval", all_records)
    if errors:
        typer.echo("[evaluate:finalize] Schema validation FAILED:\n" + "\n".join(errors), err=True)
        raise typer.Exit(1)

    profile = _load_profile(cfg)
    prompt_version = cfg.prompts.get("eval", "v1")
    cache = Cache(cfg.cache_dir)
    for record in all_records:
        key = eval_cache_key(record["job_hash"], profile.version, prompt_version)
        cache.put("evaluate", key, record)

    meta = runmeta.read_stage_meta(cfg.runs_dir, date, "evaluate")
    elapsed = time.time() - meta["prepared_at"] if "prepared_at" in meta else 0.0

    typer.echo(f"[evaluate:finalize] {len(all_records)} evaluations valid and cached.")
    runmeta.record_stage(cfg.runs_dir, date, "evaluate",
                          count_in=len(expected_ids), count_out=len(all_records),
                          seconds=elapsed, prompt_version=prompt_version,
                          cache_hits=meta.get("cache_hits", 0), cache_misses=meta.get("cache_misses", 0),
                          estimated_tokens=meta.get("estimated_tokens", 0))


# ── threshold ─────────────────────────────────────────────────────────────

@app.command()
def threshold(
    date: str = typer.Option(None, help="Run date, default today"),
    min_score: Optional[float] = typer.Option(None, help="Override config.threshold"),
):
    """[dev] Threshold: select evaluated jobs scoring >= threshold, with
    recommendation=="apply" and passing deterministic hard constraints
    (see careeros/pipeline/constraints.py)."""
    cfg = _config()
    date = date or _today()
    min_score = min_score if min_score is not None else cfg.threshold
    start = time.time()

    import json
    eval_dir = runmeta.stage_dir(cfg.runs_dir, date, "evaluate")
    evals = []
    for path in eval_dir.glob("*.json"):
        if path.name.startswith("_"):
            continue
        with open(path) as f:
            evals.append(Eval.from_dict(json.load(f)))

    # Every evaluated job already passed `constraints`, but re-checking here
    # (via select_final) is the deterministic backstop against the AI
    # mislabeling a hard-reject as "apply" — see careeros/pipeline/threshold.py.
    jobs_path = runmeta.stage_dir(cfg.runs_dir, date, "normalize") / "jobs.json"
    with open(jobs_path) as f:
        jobs_by_id = {j["id"]: Job.from_dict(j) for j in json.load(f)}

    profile = _load_profile(cfg)
    selected, held_back = select_final(evals, min_score, jobs_by_id, profile, cfg.fx_rates)

    stage_dir = runmeta.stage_dir(cfg.runs_dir, date, "select")
    with open(stage_dir / "selected.json", "w") as f:
        f.write(dumps([e.to_dict() for e in selected]))

    typer.echo(f"[threshold] {len(evals)} evaluated -> {len(selected)} >= {min_score} "
               f"(top: {selected[0].score if selected else 'n/a'})")
    runmeta.record_stage(cfg.runs_dir, date, "select",
                          count_in=len(evals), count_out=len(selected),
                          seconds=time.time() - start)


# ── artifacts (AI stage: prepare / finalize, cache-checked) ──────────────

@app.command()
def artifacts(
    date: str = typer.Option(None, help="Run date, default today"),
    prepare: bool = typer.Option(False, "--prepare"),
    finalize: bool = typer.Option(False, "--finalize"),
):
    """[dev] Resume + cover letter generation for selected jobs, cache-checked
    via artifact_cache_key. `--finalize` blocks caching on a lint or
    verify-resume failure — see careeros/lint.py."""
    cfg = _config()
    date = date or _today()

    if prepare:
        _artifacts_prepare(cfg, date)
    elif finalize:
        _artifacts_finalize(cfg, date)
    else:
        typer.echo("Pass --prepare or --finalize.", err=True)
        raise typer.Exit(1)


def _artifacts_prepare(cfg: Config, date: str) -> None:
    import json
    selected_path = runmeta.stage_dir(cfg.runs_dir, date, "select") / "selected.json"
    jobs_path = runmeta.stage_dir(cfg.runs_dir, date, "normalize") / "jobs.json"
    if not selected_path.exists() or not jobs_path.exists():
        typer.echo("Missing select/normalize output — run those stages first.", err=True)
        raise typer.Exit(1)

    with open(selected_path) as f:
        evals = [Eval.from_dict(d) for d in json.load(f)]
    with open(jobs_path) as f:
        jobs_by_id = {j["id"]: Job.from_dict(j) for j in json.load(f)}

    profile = _load_profile(cfg)
    resume_prompt_version = cfg.prompts.get("resume", "v1")
    cover_prompt_version = cfg.prompts.get("cover", "v1")
    cache = Cache(cfg.cache_dir)

    to_generate: list[dict] = []
    cache_hits = 0
    for e in evals:
        job = jobs_by_id[e.id]
        job_hash = job.content_hash()
        artifacts_path = runmeta.artifacts_dir(cfg.runs_dir, date, e.id)

        needs_resume = True
        needs_cover = True

        resume_key = artifact_cache_key(job_hash, profile.version, e.score, resume_prompt_version)
        cached_resume = cache.get("resume", resume_key)
        if cached_resume:
            with open(artifacts_path / "resume.md", "w") as f:
                f.write(cached_resume["content"])
            needs_resume = False
            cache_hits += 1

        cover_key = artifact_cache_key(job_hash, profile.version, e.score, cover_prompt_version)
        cached_cover = cache.get("cover", cover_key)
        if cached_cover:
            with open(artifacts_path / "cover.md", "w") as f:
                f.write(cached_cover["content"])
            needs_cover = False
            cache_hits += 1

        if needs_resume or needs_cover:
            to_generate.append({
                "id": e.id, "company": job.company, "title": job.title,
                "needs_resume": needs_resume, "needs_cover": needs_cover,
                "artifacts_path": str(artifacts_path),
            })

    # Each resume/cover generation independently reads the full profile.yaml
    # (per prompts/resume_v1.md, prompts/cover_v1.md) plus the job's own
    # description — so the estimate multiplies profile size by the number of
    # generation tasks, not just by job count.
    profile_bytes = cfg.profile_path.stat().st_size if cfg.profile_path.exists() else 0
    generation_tasks = sum(
        int(item["needs_resume"]) + int(item["needs_cover"]) for item in to_generate
    )
    job_desc_bytes = sum(
        len((jobs_by_id[item["id"]].description or "").encode("utf-8")) for item in to_generate
    )
    estimated_tokens = (profile_bytes * generation_tasks + job_desc_bytes) // 4

    runmeta.write_stage_meta(cfg.runs_dir, date, "artifacts", {
        "prepared_at": time.time(),
        "cache_hits": cache_hits,
        "cache_misses": len(to_generate),
        "estimated_tokens": estimated_tokens,
    })

    typer.echo(
        f"[artifacts:prepare] {len(evals)} selected: {cache_hits} cache hits (written directly), "
        f"{len(to_generate)} job(s) need generation.\n"
    )
    if to_generate:
        typer.echo(
            "AGENT INSTRUCTIONS:\n"
            f"Read {cfg.prompt_path('resume')} and {cfg.prompt_path('cover')} plus .careeros/profile.yaml.\n"
            "For each job below needing resume/cover, write the file(s) to its artifacts_path,\n"
            "following the selector-not-writer rule. Run `careeros verify-resume` + `careeros lint`\n"
            "on each resume, and `careeros lint` on each cover, before moving to the next job.\n"
            "Then run:\n"
            f"  careeros artifacts --finalize --date {date}\n\n"
            + dumps(to_generate)
        )
    else:
        typer.echo(f"Nothing to generate — run `careeros artifacts --finalize --date {date}` to finalize.")


def _artifacts_finalize(cfg: Config, date: str) -> None:
    import json
    selected_path = runmeta.stage_dir(cfg.runs_dir, date, "select") / "selected.json"
    jobs_path = runmeta.stage_dir(cfg.runs_dir, date, "normalize") / "jobs.json"
    with open(selected_path) as f:
        evals = [Eval.from_dict(d) for d in json.load(f)]
    with open(jobs_path) as f:
        jobs_by_id = {j["id"]: Job.from_dict(j) for j in json.load(f)}

    profile = _load_profile(cfg)
    resume_prompt_version = cfg.prompts.get("resume", "v1")
    cover_prompt_version = cfg.prompts.get("cover", "v1")
    cache = Cache(cfg.cache_dir)

    errors: list[str] = []
    newly_cached = 0
    artifact_count = 0

    for e in evals:
        job = jobs_by_id[e.id]
        job_hash = job.content_hash()
        artifacts_path = runmeta.artifacts_dir(cfg.runs_dir, date, e.id)
        resume_path = artifacts_path / "resume.md"
        cover_path = artifacts_path / "cover.md"

        resume_key = artifact_cache_key(job_hash, profile.version, e.score, resume_prompt_version)
        cover_key = artifact_cache_key(job_hash, profile.version, e.score, cover_prompt_version)

        if not resume_path.exists():
            errors.append(f"{e.id}: missing resume.md")
        else:
            artifact_count += 1
            if cache.get("resume", resume_key) is None:
                resume_text = resume_path.read_text(encoding="utf-8")
                voice_issues = lint_file(str(resume_path))
                truth_issues = verify_resume_bullets(resume_text, profile)
                if voice_issues or truth_issues:
                    for issue in voice_issues:
                        errors.append(f"{e.id}: resume.md voice-dna: {issue.kind} at line {issue.line}")
                    for issue in truth_issues:
                        errors.append(f"{e.id}: resume.md truthfulness: {issue}")
                else:
                    cache.put("resume", resume_key, {"content": resume_text})
                    newly_cached += 1

        if not cover_path.exists():
            errors.append(f"{e.id}: missing cover.md")
        else:
            artifact_count += 1
            if cache.get("cover", cover_key) is None:
                cover_text = cover_path.read_text(encoding="utf-8")
                voice_issues = lint_file(str(cover_path))
                if voice_issues:
                    for issue in voice_issues:
                        errors.append(f"{e.id}: cover.md voice-dna: {issue.kind} at line {issue.line}")
                else:
                    cache.put("cover", cover_key, {"content": cover_text})
                    newly_cached += 1

    if errors:
        typer.echo("[artifacts:finalize] Issues found (uncached until fixed):\n" + "\n".join(errors), err=True)
        typer.echo(f"\nAgent: fix the listed files, then re-run `careeros artifacts --finalize --date {date}`.")
        raise typer.Exit(1)

    meta = runmeta.read_stage_meta(cfg.runs_dir, date, "artifacts")
    elapsed = time.time() - meta["prepared_at"] if "prepared_at" in meta else 0.0

    typer.echo(f"[artifacts:finalize] {len(evals)} job(s), {artifact_count} artifact(s) verified, "
               f"{newly_cached} newly cached.")
    runmeta.record_stage(cfg.runs_dir, date, "artifacts",
                          count_in=len(evals), count_out=artifact_count, seconds=elapsed,
                          cache_hits=meta.get("cache_hits", 0), cache_misses=meta.get("cache_misses", 0),
                          estimated_tokens=meta.get("estimated_tokens", 0))


# ── report render (deterministic) ────────────────────────────────────────

@app.command("render-report")
def render_report(job_id: str, date: str = typer.Option(None)):
    """[dev] Render the Level-1 daily report for one job — pure template, zero AI."""
    cfg = _config()
    date = date or _today()

    import json
    eval_path = runmeta.stage_dir(cfg.runs_dir, date, "evaluate") / f"{job_id}.json"
    jobs_path = runmeta.stage_dir(cfg.runs_dir, date, "normalize") / "jobs.json"
    with open(eval_path) as f:
        evaluation = Eval.from_dict(json.load(f))
    with open(jobs_path) as f:
        job_dict = next(j for j in json.load(f) if j["id"] == job_id)
    job = Job.from_dict(job_dict)

    artifacts = runmeta.artifacts_dir(cfg.runs_dir, date, job_id)
    resume_path = str(artifacts / "resume.md")
    cover_path = str(artifacts / "cover.md")

    report_md = render_daily_report(job, evaluation, resume_path, cover_path)
    report_path = artifacts / "daily_report.md"
    with open(report_path, "w") as f:
        f.write(report_md)

    typer.echo(f"[render-report] wrote {report_path}")


@app.command("summary")
def summary(date: str = typer.Option(None)):
    """[dev] Render the day-level executive summary.md — pure template, zero
    AI. Funnel counts, the Apply (≥threshold) list, the Review (near-miss)
    list, and cost-per-selected-job — the P2.6 KPI made visible every run."""
    cfg = _config()
    date = date or _today()

    import json
    manifest = runmeta.load_manifest(cfg.runs_dir, date)

    eval_dir = runmeta.stage_dir(cfg.runs_dir, date, "evaluate")
    evals = []
    for path in eval_dir.glob("*.json"):
        if path.name.startswith("_"):
            continue
        with open(path) as f:
            evals.append(Eval.from_dict(json.load(f)))

    jobs_path = runmeta.stage_dir(cfg.runs_dir, date, "normalize") / "jobs.json"
    jobs_by_id = {}
    if jobs_path.exists():
        with open(jobs_path) as f:
            jobs_by_id = {j["id"]: Job.from_dict(j) for j in json.load(f)}

    summary_md = render_summary(date, manifest, evals, jobs_by_id, threshold=cfg.threshold)
    summary_path = runmeta.run_dir(cfg.runs_dir, date) / "summary.md"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    with open(summary_path, "w") as f:
        f.write(summary_md)

    typer.echo(f"[summary] wrote {summary_path}")


# ── drive (optional, config-gated, fail-soft) ────────────────────────────

@app.command("drive")
def drive_upload(date: str = typer.Option(None, help="Run date, default today")):
    """[dev] Upload the day's shortlisted (selected) artifacts to Google
    Drive as an additive backup — off by default (drive.enabled: false).
    Local Markdown is never replaced or moved. ANY failure here (missing
    deps, auth, network, quota) is caught and reported as a warning; the
    rest of the pipeline is never blocked by a Drive failure — that's a hard
    requirement, not a nicety."""
    cfg = _config()
    date = date or _today()

    if not cfg.drive.get("enabled", False):
        typer.echo("[drive] disabled (set drive.enabled: true in .careeros/config.yaml to use).")
        return

    import json
    from careeros.drive import upload_run

    selected_path = runmeta.stage_dir(cfg.runs_dir, date, "select") / "selected.json"
    jobs_path = runmeta.stage_dir(cfg.runs_dir, date, "normalize") / "jobs.json"
    if not selected_path.exists() or not jobs_path.exists():
        typer.echo("[drive] Missing select/normalize output — skipping.", err=True)
        return

    start = time.time()
    try:
        with open(selected_path) as f:
            evals = [Eval.from_dict(d) for d in json.load(f)]
        with open(jobs_path) as f:
            jobs_by_id = {j["id"]: Job.from_dict(j) for j in json.load(f)}

        selected_jobs = [
            (jobs_by_id[e.id], runmeta.artifacts_dir(cfg.runs_dir, date, e.id))
            for e in evals if e.id in jobs_by_id
        ]
        run_dir = runmeta.run_dir(cfg.runs_dir, date)
        links = upload_run(cfg, date, run_dir / "run.json", run_dir / "summary.md", selected_jobs)
    except Exception as e:  # deliberately broad — fail-soft is a hard requirement, see docstring
        typer.echo(f"[drive] WARNING: upload failed, continuing without Drive — {e}", err=True)
        return

    with open(runmeta.run_dir(cfg.runs_dir, date) / "drive_links.json", "w") as f:
        f.write(dumps(links))

    typer.echo(f"[drive] uploaded {len(links)}/{len(selected_jobs)} job folder(s) to Drive "
               f"({time.time() - start:.1f}s).")
    runmeta.record_stage(cfg.runs_dir, date, "drive",
                          count_in=len(selected_jobs), count_out=len(links),
                          seconds=time.time() - start)


# ── sheets ────────────────────────────────────────────────────────────────

sheets_app = typer.Typer(help="Google Sheets operations")
app.add_typer(sheets_app, name="sheets")


@sheets_app.command("append")
def sheets_append(date: str = typer.Option(None, help="Run date, default today")):
    """[dev] Append selected jobs' rows to the configured Google Sheet."""
    cfg = _config()
    date = date or _today()
    start = time.time()

    import json
    selected_path = runmeta.stage_dir(cfg.runs_dir, date, "select") / "selected.json"
    jobs_path = runmeta.stage_dir(cfg.runs_dir, date, "normalize") / "jobs.json"
    with open(selected_path) as f:
        evals = [Eval.from_dict(d) for d in json.load(f)]
    with open(jobs_path) as f:
        jobs_by_id = {j["id"]: Job.from_dict(j) for j in json.load(f)}

    # Optional hand-off from `careeros drive` (P2.6) — sheets.py has no
    # import dependency on drive.py; if the file isn't there (Drive disabled,
    # not yet run, or it failed), every row's Drive Folder cell is just blank.
    drive_links_path = runmeta.run_dir(cfg.runs_dir, date) / "drive_links.json"
    drive_links: dict = {}
    if drive_links_path.exists():
        with open(drive_links_path) as f:
            drive_links = json.load(f)

    rows = []
    for e in evals:
        job = jobs_by_id[e.id]
        artifacts = runmeta.artifacts_dir(cfg.runs_dir, date, e.id)
        rows.append(sheets_mod.job_to_row(
            date, job, e,
            resume_path=str(artifacts / "resume.md"),
            cover_path=str(artifacts / "cover.md"),
            report_path=str(artifacts / "daily_report.md"),
            drive_folder_link=drive_links.get(e.id, ""),
        ))

    sheets_mod.append_rows(cfg, rows)
    typer.echo(f"[sheets:append] wrote {len(rows)} row(s).")

    seen_path = cfg.careeros_dir / "seen.jsonl"
    append_seen_ids(seen_path, [jobs_by_id[e.id] for e in evals], date)

    runmeta.record_stage(cfg.runs_dir, date, "sheets",
                          count_in=len(evals), count_out=len(rows),
                          seconds=time.time() - start)


# ── lint ──────────────────────────────────────────────────────────────────

@app.command()
def lint(file: str):
    """[dev] Check a generated artifact against the deterministic voice-dna
    rules (em-dashes, banned AI vocabulary, negative-parallelism tell)."""
    issues = lint_file(file)
    typer.echo(format_issues(issues))
    if issues:
        raise typer.Exit(1)


@app.command("verify-resume")
def verify_resume(file: str):
    """[dev] Deterministic truthfulness check: every bullet/summary in a
    generated resume must verbatim-match a profile.yaml fact. CareerOS's
    analog of Career Ops' plan-lint.mjs verbatim check — enforces "selector,
    not writer" mechanically, not just via prompt instruction."""
    cfg = _config()
    profile = _load_profile(cfg)
    with open(file, encoding="utf-8") as f:
        resume_md = f.read()
    issues = verify_resume_bullets(resume_md, profile)
    if not issues:
        typer.echo("OK — every bullet/summary verbatim-matches profile.yaml.")
        return
    typer.echo(f"{len(issues)} truthfulness issue(s) found:")
    for issue in issues:
        typer.echo(f"  - {issue}")
    raise typer.Exit(1)


# ── end-user stubs (real orchestration lives in skills/*.md, run by the
#    host coding agent — these commands exist so `careeros <cmd>` is
#    discoverable and prints the right entry point) ──────────────────────

def _daily_stub():
    typer.echo(
        "`careeros daily` is a host-CLI skill, not a single blocking Python call — "
        "AI stages (gate, evaluate, resume, cover) need the agent's reasoning.\n\n"
        "Run it as `/careeros daily` in Claude Code / Codex / Gemini CLI / etc.\n"
        f"The skill playbook is at {REPO_ROOT / 'skills' / 'daily.md'}, and it "
        "orchestrates exactly the dev-stage commands above, in order."
    )


@app.command()
def daily():
    """Run the full daily pipeline. Entry point for the host-CLI skill."""
    _daily_stub()


@app.command()
def scan():
    """Alias for `daily` — CareerOS's job is scanning the market for you."""
    _daily_stub()


@app.command()
def start():
    """Guided profile interview -> .careeros/profile.yaml."""
    typer.echo(
        "`careeros start` is a host-CLI skill (an interactive interview needs "
        "the agent's reasoning to ask good follow-ups).\n\n"
        "Run it as `/careeros start`. Playbook: "
        f"{REPO_ROOT / 'skills' / 'start.md'}\n\n"
        "For now, you can also hand-edit .careeros/profile.yaml directly "
        "(seeded from templates/profile.example.yaml by `careeros init`)."
    )


@app.command()
def prep(job_id: str):
    """Generate the Level-2 deep interview-prep report for one job."""
    typer.echo(f"Run `/careeros prep {job_id}` in your host CLI. "
               f"Playbook: {REPO_ROOT / 'skills' / 'prep.md'}")


@app.command()
def apply(job_id: str):
    """Detect ATS and generate application answers for pasted questions."""
    typer.echo(f"Run `/careeros apply {job_id}` in your host CLI. "
               f"Playbook: {REPO_ROOT / 'skills' / 'apply.md'}")


if __name__ == "__main__":
    app()
