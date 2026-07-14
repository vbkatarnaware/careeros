"""Resume + cover letter generation (AI stage: prepare / finalize,
cache-checked via artifact_cache_key)."""

from __future__ import annotations

import json
import time

import jsonschema
import typer

from careeros import runmeta
from careeros.cache import Cache, artifact_cache_key
from careeros.cli import app
from careeros.cli._shared import _config, _load_profile, _today
from careeros.config import Config
from careeros.lint import lint_file, lint_resume_json_text, verify_resume_facts
from careeros.models import Eval, Job, dumps
from careeros.pdf import render_markdown_to_pdf
from careeros.typst_render import (
    build_render_data, pdf_page_count, render_cover_pdf, render_data_to_markdown, render_resume_pdf,
)


@app.command(hidden=True)
def artifacts(
    date: str = typer.Option(None, help="Run date, default today"),
    prepare: bool = typer.Option(False, "--prepare"),
    finalize: bool = typer.Option(False, "--finalize"),
    job_id: str = typer.Option(
        None, "--job-id",
        help="Generate for exactly this one job (any tier, must already have an "
             "06_evaluate/<job-id>.json for --date) instead of the date's full "
             "selected.json batch — the `job <job-id>` skill's enabler."),
):
    """[dev] Resume + cover letter generation for selected jobs, cache-checked
    via artifact_cache_key. `--finalize` blocks caching on a lint or
    verify-resume failure — see careeros/lint.py."""
    cfg = _config()
    date = date or _today()

    if prepare:
        _artifacts_prepare(cfg, date, job_id=job_id)
    elif finalize:
        _artifacts_finalize(cfg, date, job_id=job_id)
    else:
        typer.echo("Pass --prepare or --finalize.", err=True)
        raise typer.Exit(1)


def _load_single_job_eval(cfg: Config, date: str, job_id: str) -> tuple[list[Eval], dict[str, Job]]:
    """The `--job-id` path (v1.6.0): reads ONE job straight from
    06_evaluate/<job-id>.json — any tier, not just selected.json's Apply-tier
    batch — so `job <job-id>` can generate artifacts for a Consider-tier or
    below-threshold job the candidate wants to pursue anyway. Requires the
    job to already have an eval on record for this date (from a prior
    `daily`/`evaluate --finalize` run) — this command never runs gate/
    evaluate itself."""
    jobs_path = runmeta.stage_dir(cfg.runs_dir, date, "normalize") / "jobs.json"
    eval_path = runmeta.stage_dir(cfg.runs_dir, date, "evaluate") / f"{job_id}.json"
    if not jobs_path.exists() or not eval_path.exists():
        typer.echo(
            f"No evaluation on record for {job_id} on --date {date} — run `/careeros daily` "
            "(or evaluate --prepare/--finalize) for that date first, then retry.", err=True,
        )
        raise typer.Exit(1)
    with open(eval_path) as f:
        evals = [Eval.from_dict(json.load(f))]
    with open(jobs_path) as f:
        jobs_by_id = {j["id"]: Job.from_dict(j) for j in json.load(f) if j["id"] == job_id}
    if job_id not in jobs_by_id:
        typer.echo(f"{job_id} not found in {jobs_path}.", err=True)
        raise typer.Exit(1)
    return evals, jobs_by_id


def _artifacts_prepare(cfg: Config, date: str, job_id: str = None) -> None:
    if job_id:
        evals, jobs_by_id = _load_single_job_eval(cfg, date, job_id)
    else:
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
    resume_prompt_version = cfg.prompts.get("resume", "v2")
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
            with open(artifacts_path / "resume.json", "w") as f:
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
    # (per prompts/resume_v2.md, prompts/cover_v1.md) plus the job's own
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

    finalize_cmd = f"careeros artifacts --finalize --date {date}" + (f" --job-id {job_id}" if job_id else "")
    typer.echo(
        f"[artifacts:prepare] {len(evals)} selected: {cache_hits} cache hits (written directly), "
        f"{len(to_generate)} job(s) need generation.\n"
    )
    if to_generate:
        typer.echo(
            "AGENT INSTRUCTIONS:\n"
            f"Read {cfg.prompt_path('resume')} and {cfg.prompt_path('cover')} plus .careeros/profile.yaml.\n"
            "For each job below needing resume/cover, write the file(s) to its artifacts_path:\n"
            "  - resume.json (tailoring zones only — see resume_v2.md; canonical facts are\n"
            "    merged in from profile.yaml at render time, never write them here)\n"
            "  - cover.md (unchanged from v1 — freely written, grounded prose)\n"
            "Run `careeros verify-resume <path>/resume.json --company \"<company>\"` + `careeros lint`\n"
            "on each resume, and `careeros lint` on each cover, before moving to the next job.\n"
            "Then run:\n"
            f"  {finalize_cmd}\n\n"
            + dumps(to_generate)
        )
    else:
        typer.echo(f"Nothing to generate — run `{finalize_cmd}` to finalize.")


def _artifacts_finalize(cfg: Config, date: str, job_id: str = None) -> None:
    if job_id:
        evals, jobs_by_id = _load_single_job_eval(cfg, date, job_id)
    else:
        selected_path = runmeta.stage_dir(cfg.runs_dir, date, "select") / "selected.json"
        jobs_path = runmeta.stage_dir(cfg.runs_dir, date, "normalize") / "jobs.json"
        with open(selected_path) as f:
            evals = [Eval.from_dict(d) for d in json.load(f)]
        with open(jobs_path) as f:
            jobs_by_id = {j["id"]: Job.from_dict(j) for j in json.load(f)}

    profile = _load_profile(cfg)
    resume_prompt_version = cfg.prompts.get("resume", "v2")
    cover_prompt_version = cfg.prompts.get("cover", "v1")
    cache = Cache(cfg.cache_dir)
    resume_schema = json.loads((runmeta.SCHEMAS_DIR / "resume.schema.json").read_text())

    errors: list[str] = []
    newly_cached = 0
    artifact_count = 0

    for e in evals:
        job = jobs_by_id[e.id]
        job_hash = job.content_hash()
        artifacts_path = runmeta.artifacts_dir(cfg.runs_dir, date, e.id)
        resume_path = artifacts_path / "resume.json"
        cover_path = artifacts_path / "cover.md"

        resume_key = artifact_cache_key(job_hash, profile.version, e.score, resume_prompt_version)
        cover_key = artifact_cache_key(job_hash, profile.version, e.score, cover_prompt_version)

        # ── Resume: schema -> (cache-checked) voice/fact verify -> render ──
        resume_json: dict | None = None
        if not resume_path.exists():
            errors.append(f"{e.id}: missing resume.json")
        else:
            artifact_count += 1
            try:
                resume_json = json.loads(resume_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as ex:
                errors.append(f"{e.id}: resume.json is not valid JSON: {ex}")

            if resume_json is not None:
                schema_errors = [
                    f"{'/'.join(str(p) for p in err.path) or '(root)'}: {err.message}"
                    for err in jsonschema.Draft7Validator(resume_schema).iter_errors(resume_json)
                ]
                if schema_errors:
                    errors.extend(f"{e.id}: resume.json schema: {msg}" for msg in schema_errors)
                    resume_json = None

        if resume_json is not None:
            # Always re-checked, cache or not: voice-dna/verify-resume are
            # pure regex (zero-token, microseconds) — gating them on the
            # cache would let a hand-edited resume.json (same job_hash/score/
            # prompt_version, so same cache key) skip re-verification while
            # the PDF still re-renders below, shipping an unchecked edit.
            # `cache.put` here is just the "already proven clean once" marker
            # for `newly_cached`'s reporting, not a gate on running the check.
            voice_issues = lint_resume_json_text(resume_json)
            truth_issues = verify_resume_facts(resume_json, profile, target_company=job.company)
            if voice_issues or truth_issues:
                for issue in voice_issues:
                    errors.append(f"{e.id}: resume.json voice-dna: {issue.kind} at line {issue.line}")
                for issue in truth_issues:
                    errors.append(f"{e.id}: resume.json truthfulness: {issue}")
                resume_json = None
            elif cache.get("resume", resume_key) is None:
                cache.put("resume", resume_key, {"content": dumps(resume_json)})
                newly_cached += 1

        if resume_json is not None:
            # Rendered unconditionally (cache hit or not) — the PDF itself
            # isn't cached, it's a cheap deterministic derive from a
            # validated resume.json + profile.yaml, and every run should
            # leave a real resume.pdf on disk, not just on a cache miss.
            pdf_bytes = render_resume_pdf(profile, resume_json)
            if pdf_bytes is None:
                # typst missing or a genuine render bug (doctor FAILs on the
                # former) — fall back to the legacy fpdf2 renderer so a
                # resume is never left with literally no PDF.
                data = build_render_data(profile, resume_json)
                pdf_bytes = render_markdown_to_pdf(render_data_to_markdown(data))
            if pdf_bytes is None:
                errors.append(
                    f"{e.id}: resume.pdf failed to render — install the [resume] extra: "
                    'pip install -e ".[resume]"'
                )
            else:
                pages = pdf_page_count(pdf_bytes)
                if pages > 1:
                    errors.append(
                        f"{e.id}: resume.pdf renders to {pages} pages (must be exactly 1) — "
                        "trim bullets/skills in resume.json and re-run finalize."
                    )
                else:
                    (artifacts_path / "resume.pdf").write_bytes(pdf_bytes)

        # ── Cover: unchanged content model (freely written, grounded prose) ─
        if not cover_path.exists():
            errors.append(f"{e.id}: missing cover.md")
        else:
            artifact_count += 1
            cover_text = cover_path.read_text(encoding="utf-8")
            # Same rationale as the resume block above: always re-lint, cache
            # or not, so a hand-edited cover.md can't skip verification.
            voice_issues = lint_file(str(cover_path))
            if voice_issues:
                for issue in voice_issues:
                    errors.append(f"{e.id}: cover.md voice-dna: {issue.kind} at line {issue.line}")
                cover_ok = False
            else:
                cover_ok = True
                if cache.get("cover", cover_key) is None:
                    cache.put("cover", cover_key, {"content": cover_text})
                    newly_cached += 1

            if cover_ok:
                cover_pdf_bytes = render_cover_pdf(profile, cover_text)
                if cover_pdf_bytes is not None:
                    (artifacts_path / "cover.pdf").write_bytes(cover_pdf_bytes)
                # No hard failure if the cover PDF specifically can't render
                # (e.g. typst missing) — drive.py's own fpdf2 fallback covers
                # cover.md same as always; the resume.pdf check above is the
                # one that's a hard block.

    if errors:
        retry_cmd = f"careeros artifacts --finalize --date {date}" + (f" --job-id {job_id}" if job_id else "")
        typer.echo("[artifacts:finalize] Issues found (uncached until fixed):\n" + "\n".join(errors), err=True)
        typer.echo(f"\nAgent: fix the listed files, then re-run `{retry_cmd}`.")
        raise typer.Exit(1)

    meta = runmeta.read_stage_meta(cfg.runs_dir, date, "artifacts")
    elapsed = time.time() - meta["prepared_at"] if "prepared_at" in meta else 0.0

    typer.echo(f"[artifacts:finalize] {len(evals)} job(s), {artifact_count} artifact(s) verified, "
               f"{newly_cached} newly cached.")
    runmeta.record_stage(cfg.runs_dir, date, "artifacts",
                          count_in=len(evals), count_out=artifact_count, seconds=elapsed,
                          cache_hits=meta.get("cache_hits", 0), cache_misses=meta.get("cache_misses", 0),
                          estimated_tokens=meta.get("estimated_tokens", 0))
