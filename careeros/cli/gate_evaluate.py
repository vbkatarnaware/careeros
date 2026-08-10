"""AI stages: gate (cheap triage) and evaluate (real scoring), each with a
--prepare/--finalize host-CLI execution boundary."""

from __future__ import annotations

import json
import time
from pathlib import Path

import typer

from careeros import calibration, runmeta
from careeros.cache import Cache, eval_cache_key
from careeros.cli import app
from careeros.cli._shared import _config, _load_profile, _today
from careeros.companies import normalize_name
from careeros.config import Config
from careeros.models import Job, Profile, dumps

_ROTATION_STATE_FILENAME = "gate_rotation.json"


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


def _gate_input_job(job: dict, max_chars: int) -> dict:
    """A copy of one eligible.json job dict with `description` trimmed to
    `max_chars` for the Gate's eyes only — this is what the Gate actually
    reads (see cfg.gate_description_max_chars's docstring: 86% of gate
    tokens are this one field, measured across this project's own past
    runs, for a stage that only needs "could this plausibly be a fit").
    `eligible.json` itself is never touched, and `evaluate` reads it
    independently at its own `description_max_chars`, so evaluation depth
    is unaffected. Same truncate-with-ellipsis convention as
    pipeline/normalize.py, applied a second time on top of it."""
    description = job.get("description")
    if description and len(description) > max_chars:
        job = {**job, "description": description[:max_chars].rstrip() + "…"}
    return job


def _load_rotation_state(cfg: Config) -> dict[str, str]:
    path = cfg.careeros_dir / _ROTATION_STATE_FILENAME
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_rotation_state(cfg: Config, state: dict[str, str]) -> None:
    path = cfg.careeros_dir / _ROTATION_STATE_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        f.write(dumps(state))


def _company_cap_select(
    jobs: list[dict], *, max_per_company: int | None, rotation: dict[str, str],
) -> tuple[list[dict], list[dict]]:
    """Per-company fairness cap for the AI Gate's INPUT only — never applied
    to `eligible.json` itself, so nothing discovered is ever deleted. A job
    dropped here just isn't sent to the Gate this run; since it's never
    gated/evaluated it's never written to `.careeros/processed.jsonl`, so it
    remains a normal candidate on every future run.

    Rotation-aware, not a fixed top-N: within a company, jobs never before
    sent to the Gate (absent from `rotation`) sort first, then the
    longest-unshown, so a flooding company's unseen jobs get priority over
    ones a prior run already showed the Gate — a company is never
    permanently capped at the same N jobs."""
    if not max_per_company:
        return jobs, []
    by_company: dict[str, list[dict]] = {}
    for job in jobs:
        by_company.setdefault(normalize_name(job.get("company") or ""), []).append(job)

    kept: list[dict] = []
    dropped: list[dict] = []
    for group in by_company.values():
        # Stable sort applied twice (recency, then rotation) is simplest
        # correct way to express a two-key sort with mixed direction:
        # most-recent-first among ties, but rotation state as the primary key.
        group.sort(key=lambda j: j.get("posted_at") or "", reverse=True)
        group.sort(key=lambda j: rotation.get(j["id"], ""))
        kept.extend(group[:max_per_company])
        dropped.extend(group[max_per_company:])
    return kept, dropped


def _gate_rank_key(job: dict, profile: Profile) -> tuple[int, int]:
    """Plain, auditable ranking — tier priority then role-title priority,
    both straight from profile.yaml, no ML/embeddings. Lower sorts first."""
    work_modes = list(getattr(profile, "work_mode_priority", []) or [])
    tiers = job.get("tiers") or []
    tier_rank = min((work_modes.index(t) for t in tiers if t in work_modes), default=len(work_modes))

    role_priorities = list(getattr(profile, "role_priorities", []) or [])
    title = (job.get("title") or "").lower()
    role_rank = next(
        (i for i, r in enumerate(role_priorities) if r.lower() in title),
        len(role_priorities),
    )
    return (tier_rank, role_rank)


def _overall_cap_select(
    jobs: list[dict], *, gate_max_jobs: int | None, profile: Profile,
) -> tuple[list[dict], list[dict]]:
    """Cost ceiling applied AFTER the per-company cap. Same
    discovery-is-never-lost guarantee as `_company_cap_select` — this only
    decides what reaches the Gate today."""
    if not gate_max_jobs or len(jobs) <= gate_max_jobs:
        return jobs, []
    ranked = sorted(jobs, key=lambda j: j.get("posted_at") or "", reverse=True)
    ranked.sort(key=lambda j: _gate_rank_key(j, profile))
    return ranked[:gate_max_jobs], ranked[gate_max_jobs:]


def _gate_prepare(cfg: Config, date: str) -> None:
    eligible_path = runmeta.stage_dir(cfg.runs_dir, date, "constraints") / "eligible.json"
    if not eligible_path.exists():
        typer.echo(f"No {eligible_path} — run `careeros constraints` first.", err=True)
        raise typer.Exit(1)
    with open(eligible_path) as f:
        eligible_jobs = json.load(f)

    rotation = _load_rotation_state(cfg)
    selected, company_capped = _company_cap_select(
        eligible_jobs, max_per_company=cfg.max_jobs_per_company_per_run, rotation=rotation,
    )

    volume_capped: list[dict] = []
    if cfg.gate_max_jobs:
        try:
            profile = _load_profile(cfg)
        except (OSError, TypeError, KeyError) as e:
            typer.echo(
                f"[gate:prepare] gate_max_jobs is set but profile.yaml couldn't be "
                f"read ({e}) — skipping the volume cap this run.", err=True,
            )
        else:
            selected, volume_capped = _overall_cap_select(
                selected, gate_max_jobs=cfg.gate_max_jobs, profile=profile,
            )

    for job in selected:
        rotation[job["id"]] = date
    if selected:
        _save_rotation_state(cfg, rotation)

    stage_dir = runmeta.stage_dir(cfg.runs_dir, date, "gate")
    if company_capped or volume_capped:
        selection_meta = {
            "eligible": len(eligible_jobs),
            "sent_to_gate": len(selected),
            "dropped_by_company_cap": len(company_capped),
            "dropped_by_gate_max_jobs": len(volume_capped),
            "dropped_ids": {
                "company_cap": [j["id"] for j in company_capped],
                "gate_max_jobs": [j["id"] for j in volume_capped],
            },
        }
        with open(stage_dir / "_selection_meta.json", "w") as f:
            f.write(dumps(selection_meta))

    jobs = [_gate_input_job(j, cfg.gate_description_max_chars) for j in selected]

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
    cap_note = ""
    if company_capped or volume_capped:
        cap_note = (
            f" ({len(eligible_jobs)} eligible, {len(company_capped)} held back by the"
            f" per-company cap, {len(volume_capped)} by gate_max_jobs — see"
            f" 05_gate/_selection_meta.json; none are lost, they're candidates again"
            f" on a future run)"
        )
    typer.echo(
        f"[gate:prepare] {len(jobs)} jobs -> {len(batches)} batch(es) of up to {batch_size}.{cap_note}\n\n"
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

    # v2.0: today's-relevant-ids, so a same-date re-run's stale leftovers get
    # ignored rather than silently folded back in. Found live 2026-07-31: a
    # second `discover`+`normalize` call for the same date overwrote
    # 02_normalize/jobs.json, but 06_evaluate/ still held eval files from the
    # first attempt whose ids no longer existed anywhere in the current run
    # -- the old blanket `glob("*.json")` treated those as legitimate cache
    # hits, so `record_stage` reported 39 evaluations against 31 files on
    # disk and the count drifted every time --finalize re-ran. "Relevant"
    # means: today's gate kept it. Anything else in the stage dir is a
    # leftover from an earlier attempt at this same date and must be
    # ignored entirely -- not counted, not re-cached, not judged by
    # calibration.
    gate_path = runmeta.stage_dir(cfg.runs_dir, date, "gate") / "gated.json"
    relevant_ids = set(expected_ids)
    if gate_path.exists():
        with open(gate_path) as f:
            relevant_ids |= {r["id"] for r in json.load(f) if r.get("keep")}

    # `fresh_records`: written THIS run, from this run's own _input.json.
    # This is the population calibration judges -- cache hits are already-
    # accepted prior work and must never be re-judged (re-judging a cache
    # hit means a batch's calibration verdict depends on what ELSE happened
    # to be cached that day, which is not reproducible).
    fresh_records: list[dict] = []
    fresh_paths: dict[int, Path] = {}
    missing = []
    for job_id in expected_ids:
        out_path = stage_dir / f"{job_id}.json"
        if not out_path.exists():
            missing.append(job_id)
            continue
        with open(out_path) as f:
            fresh_records.append(json.load(f))
        fresh_paths[len(fresh_records) - 1] = out_path

    if missing:
        typer.echo(f"[evaluate:finalize] Missing output for: {', '.join(missing)}", err=True)
        typer.echo("Agent: write the missing files, then re-run --finalize.")
        raise typer.Exit(1)

    # Legitimate cache hits: written during --prepare for a job this run's
    # gate kept, but not in _input.json because the cache already had it.
    cache_hit_records: list[dict] = []
    cache_hit_paths: dict[int, Path] = {}
    for path in sorted(stage_dir.glob("*.json")):
        if path.name.startswith("_"):
            continue
        job_id = path.stem
        if job_id in expected_ids or job_id not in relevant_ids:
            continue
        with open(path) as f:
            cache_hit_records.append(json.load(f))
        cache_hit_paths[len(cache_hit_records) - 1] = path

    all_records = fresh_records + cache_hit_records
    record_paths: dict[int, Path] = dict(fresh_paths)
    for idx, path in cache_hit_paths.items():
        record_paths[len(fresh_records) + idx] = path

    errors = runmeta.validate_stage("eval", all_records)
    if errors:
        typer.echo("[evaluate:finalize] Schema validation FAILED:\n" + "\n".join(errors), err=True)
        raise typer.Exit(1)

    jobs_by_id: dict[str, dict] = {}
    eligible_path = runmeta.stage_dir(cfg.runs_dir, date, "constraints") / "eligible.json"
    if eligible_path.exists():
        with open(eligible_path) as f:
            jobs_by_id = {j["id"]: j for j in json.load(f)}

    calibration_enabled = cfg.calibration.get("enabled", True)
    calib_cfg = cfg.calibration

    # ── v2.0 calibration, pass 1: per-record BLOCK checks, PRE-clamp ──
    # Must run before the clamp below, which deliberately breaks the
    # arithmetic identity for "skip" records -- checking post-clamp would
    # make the anti-inflation check fire on the very backstop that exists to
    # protect the scoring contract.
    if calibration_enabled and fresh_records:
        pre_clamp = [
            calibration.check_arithmetic(
                fresh_records,
                tol=calib_cfg.get("arithmetic_tolerance", calibration.DEFAULT_ARITHMETIC_TOLERANCE),
            ),
            calibration.check_prose_contradiction(
                fresh_records,
                markers=tuple(calib_cfg.get("seniority_markers", calibration.DEFAULT_SENIORITY_MARKERS)),
                seniority_fit_max=calib_cfg.get("seniority_fit_max_with_marker", 3.5),
            ),
        ]
        blocked = calibration.blocking_findings(pre_clamp)
        if blocked:
            _fail_calibration(blocked, date)

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

    # ── v2.0 calibration, pass 2: batch checks, POST-clamp, PRE-cache ──
    # A blocked eval must never be cached — see _fail_calibration, which
    # exits before cache.put runs below.
    if calibration_enabled and fresh_records:
        batch_findings = [
            calibration.check_rubric_vector_clones(
                fresh_records, jobs_by_id,
                min_records=calib_cfg.get("clone_min_records", 4),
                min_companies=calib_cfg.get("clone_min_companies", 3),
            ),
            calibration.check_uniformity(
                fresh_records, cfg.threshold,
                min_n=calib_cfg.get("min_batch_n", 10),
                max_stdev=calib_cfg.get("uniformity_max_stdev", 0.25),
                min_pass_rate=calib_cfg.get("uniformity_min_pass_rate", 0.90),
            ),
            calibration.check_dimension_repetition(
                fresh_records,
                min_n=calib_cfg.get("min_batch_n", 10),
                max_mode_share=calib_cfg.get("dimension_mode_share_warn", 0.60),
                exempt=tuple(calib_cfg.get("dimension_exempt", ("logistics",))),
            ),
        ]
        blocked = calibration.blocking_findings(batch_findings)
        if blocked:
            _fail_calibration(blocked, date)

        all_findings = pre_clamp + batch_findings
        with open(stage_dir / "_calibration.json", "w") as f:
            f.write(dumps(calibration.findings_to_dicts(all_findings)))
        warn_headlines = [
            f"calibration:{f.code}: {f.headline}"
            for f in all_findings if f.fired and f.severity == "warn"
        ]
        for h in warn_headlines:
            typer.echo(f"[evaluate:finalize] WARN — {h}")
    else:
        warn_headlines = []

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
                          estimated_tokens=meta.get("estimated_tokens", 0),
                          errors=warn_headlines)


def _fail_calibration(blocked: list[calibration.Finding], date: str) -> None:
    """Shared block-and-exit path for both calibration passes. Never called
    after cache.put runs, so a blocked eval can never survive re-reasoning
    as a stale cache hit."""
    lines = []
    for finding in blocked:
        lines.append(f"{finding.headline}:")
        lines.extend(f"  - {d}" for d in finding.detail)
    typer.echo("[evaluate:finalize] Calibration FAILED:\n" + "\n".join(lines), err=True)
    typer.echo(
        "\nAgent: re-reason ONLY the listed job(s) against prompts/eval_v2.md. "
        "Do not adjust a number just to satisfy this check -- write the score "
        "the rubric you already wrote actually implies, or rewrite the rubric "
        "if the number was wrong. Then re-run "
        f"`careeros evaluate --finalize --date {date}`."
    )
    raise typer.Exit(1)
