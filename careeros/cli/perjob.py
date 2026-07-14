"""Per-job commands: `job` (the full apply-tier treatment for one job, any
score — a host-CLI skill stub, see skills/job.md) and `publish` (upload one
job's current artifacts to Drive/Sheet)."""

from __future__ import annotations

import json

import typer

from careeros import runmeta
from careeros import sheets as sheets_mod
from careeros.cli import app
from careeros.cli._shared import REPO_ROOT, _config, _today
from careeros.models import Job


@app.command(rich_help_panel="Per-job")
def job(job_id: str):
    """Give one job the full Apply-tier treatment — resume, cover, report,
    application answers, auto-published — regardless of its score. Entry
    point for the host-CLI skill (see skills/job.md)."""
    typer.echo(
        "`careeros job <job-id>` is a host-CLI skill, not a single blocking "
        "Python call — resume/cover tailoring and application answers need "
        "the agent's reasoning.\n\n"
        f"Run it as `/careeros job {job_id}` in your host CLI.\n"
        f"The skill playbook is at {REPO_ROOT / 'skills' / 'job.md'}."
    )


@app.command(rich_help_panel="Per-job")
def publish(job_id: str, date: str = typer.Option(None, help="Run date the job was discovered in, default today")):
    """Upload one job's current artifacts (whichever exist on disk — resume,
    cover, evaluation, deep report, application answers) to Drive and patch
    just that Sheet row's Drive-link cells. Use this after `careeros prep
    <job-id>` or an on-demand `careeros apply <job-id>` so the result shows
    up in Drive + the Sheet without waiting for the next full `daily` run.
    In local mode (both drive.enabled and sheets.enabled false) there is
    nothing to upload — artifacts are already on disk under
    `.careeros/runs/<date>/artifacts/<job-id>/`; re-run `careeros summary
    --date <date>` to refresh the local digest's links instead."""
    cfg = _config()
    date = date or _today()

    if not cfg.drive.get("enabled", False):
        typer.echo("[publish] Drive is disabled (set drive.enabled: true in .careeros/config.yaml) "
                   "— nothing to upload. In local mode, artifacts already live under "
                   f".careeros/runs/{date}/artifacts/{job_id}/; run `careeros summary --date {date}` "
                   "to refresh the local digest's links.")
        return

    jobs_path = runmeta.stage_dir(cfg.runs_dir, date, "normalize") / "jobs.json"
    if not jobs_path.exists():
        typer.echo(f"[publish] No normalize output for --date {date} — is that the right run date?", err=True)
        raise typer.Exit(1)
    with open(jobs_path) as f:
        matches = [j for j in json.load(f) if j["id"] == job_id]
    if not matches:
        typer.echo(f"[publish] Job {job_id} not found in {date}'s normalize output.", err=True)
        raise typer.Exit(1)
    job = Job.from_dict(matches[0])
    artifacts_path = runmeta.artifacts_dir(cfg.runs_dir, date, job_id)

    from careeros.drive import DriveError, upload_jobs
    try:
        results = upload_jobs(cfg, [(date, job, artifacts_path)])
    except DriveError as e:
        typer.echo(f"[publish] Drive upload failed — {e}", err=True)
        raise typer.Exit(1)

    result = results.get(job_id)
    if result is None or result.error:
        reason = result.error if result else "no artifact files found on disk to upload"
        typer.echo(f"[publish] Nothing published for {job_id} — {reason}", err=True)
        raise typer.Exit(1)

    for w in result.warnings:
        typer.echo(f"[publish] {job_id}: {w}", err=True)

    updates = {}
    if result.eval_link:
        updates["Evaluation (Drive)"] = result.eval_link
    if result.deep_report_link:
        updates["Deep Report (Drive)"] = result.deep_report_link
    if result.answers_link:
        updates["Application Answers (Drive)"] = result.answers_link
    if result.resume_link:
        updates["Resume (Drive)"] = result.resume_link
    if result.cover_link:
        updates["Cover Letter (Drive)"] = result.cover_link

    if not updates:
        typer.echo(f"[publish] Uploaded, but nothing new to link for {job_id}.")
        return

    if not cfg.sheets.get("enabled", False):
        typer.echo(f"[publish] Uploaded to Drive for {job_id}. Sheets is disabled "
                   "(sheets.enabled: false) — no row to patch.")
        return

    found = sheets_mod.update_row_by_job_id(cfg, job_id, updates)
    if not found:
        typer.echo(f"[publish] Uploaded to Drive, but {job_id} isn't in the Sheet yet "
                   "(its row hasn't been appended by `sheets append`) — nothing to update.", err=True)
        raise typer.Exit(1)

    typer.echo(f"[publish] Updated Sheet row for {job_id}: {', '.join(updates.keys())}")
