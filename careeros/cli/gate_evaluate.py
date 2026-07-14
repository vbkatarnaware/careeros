"""AI stages: gate (cheap triage) and evaluate (real scoring), each with a
--prepare/--finalize host-CLI execution boundary."""

from __future__ import annotations

import json
import time
from pathlib import Path

import typer

from careeros import runmeta
from careeros.cache import Cache, eval_cache_key
from careeros.cli import app
from careeros.cli._shared import _config, _load_profile, _today
from careeros.config import Config
from careeros.models import Job, dumps


# ── gate (AI stage: prepare / finalize) ──────────────────────────────────

@app.command(hidden=True)
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

@app.command(hidden=True)
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


def _evaluate_prepare(cfg: Config, date: str) -> None:
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
            # The cache key is content-based (job_hash excludes `source`), so a
            # cache hit can carry a STALE `id` from whenever this content was
            # first evaluated under a different Job.id (e.g. before the P2.7
            # actor->REST provider migration, since `source` feeds Job.id but
            # not content_hash). Eval.id must be today's actual job_id or every
            # downstream stage (threshold/artifacts/drive/sheets/summary) fails
            # to find the matching Job — found live 2026-07-10 on a real cache
            # hit (Motive PM) that silently displaced today's own evaluation.
            with open(stage_dir / f"{job_id}.json", "w") as f:
                f.write(dumps({**cached, "id": job_id}))
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
    stage_dir = runmeta.stage_dir(cfg.runs_dir, date, "evaluate")
    input_path = stage_dir / "_input.json"
    expected_ids = set()
    if input_path.exists():
        with open(input_path) as f:
            expected_ids = {e["job"]["id"] for e in json.load(f)}

    all_records = []
    record_paths: dict[int, Path] = {}
    missing = []
    for job_id in expected_ids:
        out_path = stage_dir / f"{job_id}.json"
        if not out_path.exists():
            missing.append(job_id)
            continue
        with open(out_path) as f:
            all_records.append(json.load(f))
        record_paths[len(all_records) - 1] = out_path

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
            record_paths[len(all_records) - 1] = path

    if missing:
        typer.echo(f"[evaluate:finalize] Missing output for: {', '.join(missing)}", err=True)
        typer.echo("Agent: write the missing files, then re-run --finalize.")
        raise typer.Exit(1)

    errors = runmeta.validate_stage("eval", all_records)
    if errors:
        typer.echo("[evaluate:finalize] Schema validation FAILED:\n" + "\n".join(errors), err=True)
        raise typer.Exit(1)

    # Deterministic scoring-contract clamp (AGENT_GUIDE.md: "green means
    # apply-able"). A "skip" recommendation (deal-breaker or a stated
    # preference violation) must never leave an apply-tier score sitting on
    # disk — the agent scores the 5 rubric dimensions honestly, but the
    # dimensions alone (logistics is only 10% weight) can't reliably pull an
    # otherwise-strong fit below threshold. This backstop guarantees
    # score/recommendation never disagree, regardless of which agent/model
    # wrote the eval, without asking anyone to fudge a rubric dimension.
    clamp_ceiling = round(cfg.threshold - 0.1, 1)
    clamped = 0
    for idx, record in enumerate(all_records):
        if record.get("recommendation") == "skip" and record.get("score", 0) >= cfg.threshold:
            record["score"] = min(record["score"], clamp_ceiling)
            clamped += 1
            path = record_paths.get(idx)
            if path is not None:
                with open(path, "w") as f:
                    json.dump(record, f, indent=2, sort_keys=True)
    if clamped:
        typer.echo(
            f"[evaluate:finalize] Clamped {clamped} skip-recommendation eval(s) "
            f"to score <= {clamp_ceiling} (a deal-breaker/preference violation "
            f"must never leave an apply-tier score)."
        )

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
