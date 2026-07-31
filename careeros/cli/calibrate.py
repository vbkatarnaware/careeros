"""v2.0: golden-set drift detection. See careeros/pipeline/golden.py and
schemas/golden.schema.json.

Three sub-commands, one execution boundary:
  --check   free, every run, zero tokens: did any golden job reappear in
            today's own eval output? Compare directly.
  --probe   the real probe: sample N golden jobs (labels stripped), write
            them for the host-CLI agent to evaluate exactly as it would any
            other job, via prompts/eval_v2.md.
  --score   after the agent has evaluated the probe, compare its output
            against the golden `expected` values and report drift.

`.careeros/golden/golden_set.json` (the SEALED set) is what these commands
read. `.careeros/golden/golden_set.draft.json` is a candidate set pending
human review — see `careeros calibrate --approve`, which is the ONLY command
that may write the sealed file, and only after confirming `sealed_by`. No
command here ever derives `expected` values itself; that is a human step by
design (AGENT_GUIDE.md's "seeding is yours, not an agent's" — copying an
agent's own output produces a mirror of one run, not a ruler)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import typer

from careeros import runmeta
from careeros.cli import app
from careeros.cli._shared import _config, _today
from careeros.config import Config
from careeros.models import dumps
from careeros.pipeline import golden


def _golden_dir(cfg: Config) -> Path:
    return cfg.careeros_dir / "golden"


def _load_golden_set(cfg: Config) -> tuple[dict, list[dict]]:
    path = _golden_dir(cfg) / "golden_set.json"
    if not path.exists():
        typer.echo(
            f"No {path} — nothing sealed yet. A draft may exist at "
            f"{_golden_dir(cfg) / 'golden_set.draft.json'}; review it and run "
            "`careeros calibrate --approve` to seal it.",
            err=True,
        )
        raise typer.Exit(1)
    with open(path) as f:
        gs = json.load(f)
    with open(_golden_dir(cfg) / "jobs.json") as f:
        jobs = json.load(f)
    return gs, jobs


@app.command(hidden=True)
def calibrate(
    date: str = typer.Option(None, help="Run date, default today"),
    check: bool = typer.Option(False, "--check", help="Free: compare golden jobs that reappeared in today's own eval output"),
    probe: bool = typer.Option(False, "--probe", help="Sample N golden jobs (unlabelled) for the agent to evaluate fresh"),
    n: int = typer.Option(6, "--n", help="Sample size for --probe"),
    score: bool = typer.Option(False, "--score", help="Compare the agent's fresh --probe output against golden `expected`"),
    approve: bool = typer.Option(False, "--approve", help="Promote a reviewed golden_set.draft.json to the sealed golden_set.json"),
):
    """[dev] v2.0 golden-set drift check. See careeros/pipeline/golden.py."""
    cfg = _config()
    date = date or _today()

    flags = [check, probe, score, approve]
    if sum(bool(f) for f in flags) != 1:
        typer.echo("Pass exactly one of --check, --probe, --score, --approve.", err=True)
        raise typer.Exit(1)

    if approve:
        _calibrate_approve(cfg)
    elif check:
        _calibrate_check(cfg, date)
    elif probe:
        _calibrate_probe(cfg, date, n)
    else:
        _calibrate_score(cfg, date)


def _calibrate_approve(cfg: Config) -> None:
    draft_path = _golden_dir(cfg) / "golden_set.draft.json"
    sealed_path = _golden_dir(cfg) / "golden_set.json"
    if not draft_path.exists():
        typer.echo(f"No {draft_path} to approve.", err=True)
        raise typer.Exit(1)
    with open(draft_path) as f:
        draft = json.load(f)
    if draft.get("sealed_by") != "human":
        typer.echo(
            "Refusing to seal: sealed_by must be 'human'. This file is only ever "
            "sealed after YOU review each `expected` score/rubric by hand — "
            "see AGENT_GUIDE.md.",
            err=True,
        )
        raise typer.Exit(1)
    errors = runmeta.validate_stage("golden", [draft])
    if errors:
        typer.echo("[calibrate:approve] Schema validation FAILED:\n" + "\n".join(errors), err=True)
        raise typer.Exit(1)
    with open(sealed_path, "w") as f:
        f.write(dumps(draft))
    typer.echo(
        f"[calibrate:approve] Sealed {len(draft['entries'])} entries to {sealed_path}. "
        "This is now the set --check/--probe/--score read."
    )


def _calibrate_check(cfg: Config, date: str) -> None:
    gs, _jobs = _load_golden_set(cfg)
    by_hash = {e["job_hash"]: e for e in gs["entries"]}

    stage_dir = runmeta.stage_dir(cfg.runs_dir, date, "evaluate")
    actual_by_hash = {}
    for path in stage_dir.glob("*.json"):
        if path.name.startswith("_"):
            continue
        with open(path) as f:
            record = json.load(f)
        job_hash = record.get("job_hash")
        if job_hash in by_hash:
            actual_by_hash[job_hash] = record

    if not actual_by_hash:
        typer.echo(f"[calibrate:check] No golden-set jobs reappeared in {date}'s eval output. Nothing to compare.")
        return

    report = golden.score_probe(
        by_hash, actual_by_hash, threshold=cfg.threshold, consider_threshold=cfg.consider_threshold,
    )
    _print_drift_report(report, source=f"today's run ({date})")


def _calibrate_probe(cfg: Config, date: str, n: int) -> None:
    gs, jobs = _load_golden_set(cfg)
    jobs_by_hash = {}
    for i, j in enumerate(jobs):
        from careeros.models import Job
        jobs_by_hash[Job.from_dict(j).content_hash()] = (i, j)

    sample = golden.deterministic_sample(gs["entries"], n=n, seed=date)
    probe_input = []
    for entry in sample:
        job_hash = entry["job_hash"]
        _, job = jobs_by_hash[job_hash]
        probe_input.append({"job": job, "job_hash": job_hash})

    golden_dir = _golden_dir(cfg)
    input_path = golden_dir / "_probe_input.json"
    with open(input_path, "w") as f:
        f.write(dumps(probe_input))
    runmeta.write_stage_meta(cfg.runs_dir, date, "calibrate_probe", {
        "prepared_at": time.time(), "n": len(probe_input),
    })

    typer.echo(
        f"[calibrate:probe] Sampled {len(probe_input)} golden job(s), labels stripped, "
        f"seeded on {date}.\n\n"
        f"AGENT INSTRUCTIONS:\n"
        f"Read {cfg.prompt_path('eval')} and .careeros/profile.yaml — evaluate each entry in\n"
        f"{input_path} EXACTLY as you would any other job (do not treat this specially\n"
        f"or try to guess the expected answer). For each entry, write\n"
        f".careeros/golden/_probe_output/<job_hash>.json matching schemas/eval.schema.json\n"
        f"(job_hash = the input entry's own job_hash field). Then run:\n"
        f"  careeros calibrate --score --date {date}"
    )


def _calibrate_score(cfg: Config, date: str) -> None:
    gs, _jobs = _load_golden_set(cfg)
    output_dir = _golden_dir(cfg) / "_probe_output"
    if not output_dir.exists() or not any(output_dir.glob("*.json")):
        typer.echo(f"No {output_dir} — run `careeros calibrate --probe` first, have the agent write it, then retry.", err=True)
        raise typer.Exit(1)

    actual_by_hash = {}
    for path in output_dir.glob("*.json"):
        with open(path) as f:
            record = json.load(f)
        job_hash = record.get("job_hash", path.stem)
        actual_by_hash[job_hash] = record

    errors = runmeta.validate_stage("eval", list(actual_by_hash.values()))
    if errors:
        typer.echo("[calibrate:score] Schema validation FAILED:\n" + "\n".join(errors), err=True)
        raise typer.Exit(1)

    by_hash = {e["job_hash"]: e for e in gs["entries"]}
    report = golden.score_probe(
        by_hash, actual_by_hash, threshold=cfg.threshold, consider_threshold=cfg.consider_threshold,
    )
    _print_drift_report(report, source="probe")

    if report.severity == "block":
        typer.echo(
            "\n[calibrate:score] BLOCK: drift exceeds tolerance. Do not trust today's "
            "eval batch without investigating (a prompt/profile change, a model "
            "swap, or a genuine reasoning regression). See AGENT_GUIDE.md.",
            err=True,
        )
        raise typer.Exit(1)


def _print_drift_report(report: golden.DriftReport, *, source: str) -> None:
    typer.echo(f"[calibrate] Drift report from {source}, n={report.n}, severity={report.severity.upper()}")
    typer.echo(f"  Per-dimension max deviation: {report.per_dimension_max_deviation}")
    typer.echo(f"  Mean signed score error: {report.mean_signed_error:+.3f} "
               f"(positive = inflation signature, negative = scorer turned harsh)")
    typer.echo(f"  Band agreement: {report.band_agreement}/{report.band_total}")
    if report.mismatches:
        typer.echo("  Band mismatches:")
        for m in report.mismatches:
            typer.echo(
                f"    {m['label']}: expected {m['expected_band']} ({m['expected_score']}), "
                f"got {m['actual_band']} ({m['actual_score']})"
            )
