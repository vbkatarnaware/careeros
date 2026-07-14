"""Google Drive backup: the daily upload (optional, config-gated, fail-soft)
and the one-time backfill for Apply-tier rows that predate Drive automation."""

from __future__ import annotations

import json
import time
import types
from pathlib import Path

import typer

from careeros import runmeta
from careeros import sheets as sheets_mod
from careeros.cli import app
from careeros.cli._shared import _config, _today
from careeros.models import Eval, Job, dumps


def _job_upload_results_to_dict(results: dict) -> dict:
    """JobUploadResult dataclasses aren't directly JSON-serializable — flatten
    to plain dicts for drive_links.json (also the shape sheets_append reads
    back). No "folder" key (P2.10 dropped the Drive Folder Sheet column —
    there's only ever one project folder, so a per-row link to it was
    redundant); every other key is a direct, per-file link."""
    return {
        job_id: {
            "resume": r.resume_link, "cover": r.cover_link,
            "eval": r.eval_link, "deep_report": r.deep_report_link,
            "answers": r.answers_link, "warnings": r.warnings,
        }
        for job_id, r in results.items()
    }


@app.command("drive", hidden=True)
def drive_upload(date: str = typer.Option(None, help="Run date, default today")):
    """[dev] Upload the day's Apply-tier artifacts to Google Drive as an
    additive backup (flat layout, PDF resume/cover) — off by default
    (drive.enabled: false). Local Markdown is never replaced or moved. ANY
    failure here (missing deps, auth, network, quota) is caught and reported
    as a warning; the rest of the pipeline is never blocked by a Drive
    failure — that's a hard requirement, not a nicety."""
    cfg = _config()
    date = date or _today()

    if not cfg.drive.get("enabled", False):
        typer.echo("[drive] disabled (set drive.enabled: true in .careeros/config.yaml to use).")
        return

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
        results = upload_run(cfg, date, run_dir / "run.json", run_dir / "summary.md", selected_jobs)
    except Exception as e:  # deliberately broad — fail-soft is a hard requirement, see docstring
        typer.echo(f"[drive] WARNING: upload failed, continuing without Drive — {e}", err=True)
        return

    with open(runmeta.run_dir(cfg.runs_dir, date) / "drive_links.json", "w") as f:
        f.write(dumps(_job_upload_results_to_dict(results)))

    for job_id, r in results.items():
        for w in r.warnings:
            typer.echo(f"[drive] {job_id}: {w}", err=True)

    typer.echo(f"[drive] uploaded {len(results)}/{len(selected_jobs)} job(s) to Drive "
               f"({time.time() - start:.1f}s).")
    runmeta.record_stage(cfg.runs_dir, date, "drive",
                          count_in=len(selected_jobs), count_out=len(results),
                          seconds=time.time() - start)


# ── backfill-drive (Phase 3, v1.1) ───────────────────────────────────────

def _cell_is_blank(value: str) -> bool:
    """True for a Sheet cell with no real content — both the historical
    empty string and the "-" sentinel `sheets.py` now fills blanks with."""
    return value in ("", "-")


@app.command("backfill-drive", hidden=True)
def backfill_drive(
    dry_run: bool = typer.Option(
        True, "--dry-run/--no-dry-run",
        help="Preview only (default): no Drive uploads, no Sheet writes. Pass --no-dry-run to apply."),
):
    """Add Drive artifacts + clickable Sheet links (Resume (Drive), Cover
    Letter (Drive), Evaluation (Drive)) to Apply-tier rows that predate Drive
    automation. Safe to re-run: rows that already have all three links are
    skipped (idempotent). Never fabricates — a row missing any of those
    links whose corresponding local file (resume.json/resume.md, cover.md,
    daily_report.md) no longer exists on disk is listed as needing regeneration, not silently
    invented. Defaults to --dry-run so the very first run against your real
    Sheet only shows you what WOULD happen."""
    cfg = _config()

    if not cfg.drive.get("enabled", False) or not cfg.drive.get("root_folder_id"):
        typer.echo("[backfill-drive] Drive isn't configured (drive.enabled + "
                   "drive.root_folder_id in .careeros/config.yaml) — nothing to backfill.", err=True)
        raise typer.Exit(1)

    rows = sheets_mod.read_all_rows_with_job_id(cfg)
    # A blank/missing Tier means the row predates the Tier column (Phase 3) —
    # every row written before Tier existed was, by construction, an Apply-
    # tier row (the Consider tier did not exist yet, so nothing else could
    # have been appended). Only a row EXPLICITLY marked "Consider" is excluded.
    apply_rows = [r for r in rows if r.get("Tier", "") in ("Apply", "")]
    typer.echo(f"[backfill-drive] {len(apply_rows)} Apply-tier row(s) found in the Sheet "
               f"({len(rows)} total rows).")

    to_process: list[tuple[str, str, str, str, Path]] = []
    needs_regen: list[tuple[str, str, str, str]] = []
    already_done = 0

    for row in apply_rows:
        resume_missing = _cell_is_blank(row.get("Resume (Drive)", ""))
        cover_missing = _cell_is_blank(row.get("Cover Letter (Drive)", ""))
        eval_missing = _cell_is_blank(row.get("Evaluation (Drive)", ""))
        if not (resume_missing or cover_missing or eval_missing):
            already_done += 1
            continue
        date, job_id = row.get("Date", ""), row.get("Job ID", "")
        company, role = row.get("Company", ""), row.get("Role", "")
        if not date or not job_id:
            continue  # malformed row (predates Job ID being tracked) — nothing we can key on
        artifacts_dir = runmeta.artifacts_dir(cfg.runs_dir, date, job_id)
        missing_locally = []
        # resume.json (v1.4.0+) or the legacy resume.md — either is a valid
        # local source to backfill from.
        if resume_missing and not (
            (artifacts_dir / "resume.json").exists() or (artifacts_dir / "resume.md").exists()
        ):
            missing_locally.append("resume.json/resume.md")
        if cover_missing and not (artifacts_dir / "cover.md").exists():
            missing_locally.append("cover.md")
        if eval_missing and not (artifacts_dir / "daily_report.md").exists():
            missing_locally.append("daily_report.md")
        if missing_locally:
            needs_regen.append((date, company, role, job_id))
            continue
        to_process.append((date, company, role, job_id, artifacts_dir))

    typer.echo(f"[backfill-drive] {already_done} row(s) already backfilled (idempotent skip).")
    if needs_regen:
        typer.echo(f"[backfill-drive] {len(needs_regen)} row(s) NEED REGENERATION "
                   f"(local artifacts no longer on disk — NOT fabricated):")
        for date, company, role, job_id in needs_regen:
            typer.echo(f"    {date} | {company} - {role} ({job_id})")

    if not to_process:
        typer.echo("[backfill-drive] Nothing left to upload.")
        return

    typer.echo(f"[backfill-drive] {len(to_process)} row(s) to backfill:")
    for date, company, role, job_id, _ in to_process:
        typer.echo(f"    {date} | {company} - {role} ({job_id})")

    if dry_run:
        typer.echo("\n[backfill-drive] DRY RUN — no Drive uploads, no Sheet writes made. "
                   "Re-run with --no-dry-run to apply.")
        return

    from careeros.drive import upload_jobs, verify_uploads

    jobs_batch = [
        (date, types.SimpleNamespace(id=job_id, company=company, title=role), artifacts_dir)
        for date, company, role, job_id, artifacts_dir in to_process
    ]
    try:
        results = upload_jobs(cfg, jobs_batch)
    except Exception as e:  # only a whole-batch failure (auth/config) raises this high —
        typer.echo(f"[backfill-drive] WARNING: upload failed, nothing written — {e}", err=True)
        raise typer.Exit(1)

    # Every requested job should appear in `results` UNLESS it had no local
    # artifacts at all (already excluded above, so this shouldn't happen) —
    # track it anyway so a silent gap is visible rather than assumed fine.
    upload_failed: list[tuple[str, str]] = []   # (job_id, error)
    upload_succeeded: dict[str, object] = {}     # job_id -> JobUploadResult
    for job_id, r in results.items():
        for w in r.warnings:
            typer.echo(f"[backfill-drive] {job_id}: {w}", err=True)
        if r.error:
            upload_failed.append((job_id, r.error))
            typer.echo(f"[backfill-drive] UPLOAD FAILED for {job_id}: {r.error}", err=True)
        else:
            upload_succeeded[job_id] = r

    sheet_update_failed: list[tuple[str, str]] = []   # (job_id, reason)
    sheet_update_succeeded: list[str] = []
    for job_id, r in upload_succeeded.items():
        # Only include links this upload actually produced -- a row missing
        # just "Evaluation (Drive)" may not have re-uploaded resume/cover
        # (their source files may not have existed to reprocess), and an
        # empty string here would wipe an already-good link on that column.
        updates = {}
        if r.resume_link:
            updates["Resume (Drive)"] = r.resume_link
        if r.cover_link:
            updates["Cover Letter (Drive)"] = r.cover_link
        if r.eval_link:
            updates["Evaluation (Drive)"] = r.eval_link
        try:
            found = sheets_mod.update_row_by_job_id(cfg, job_id, updates) if updates else True
        except Exception as e:  # one row's Sheet-write failure must not stop the rest
            sheet_update_failed.append((job_id, str(e)))
            typer.echo(f"[backfill-drive] SHEET UPDATE FAILED for {job_id}: {e}", err=True)
            continue
        if found:
            sheet_update_succeeded.append(job_id)
        else:
            sheet_update_failed.append((job_id, "row not found on re-lookup (was it deleted?)"))
            typer.echo(f"[backfill-drive] SHEET UPDATE FAILED for {job_id}: "
                       f"row not found on re-lookup", err=True)

    # ── Verification pass: re-fetch from Drive + re-read the Sheet fresh —
    # never trust the upload/update calls' own success signal alone. ──
    drive_verification = verify_uploads(cfg, upload_succeeded) if upload_succeeded else {}
    drive_verified = sum(
        1 for v in drive_verification.values() if v["resume_ok"] and v["cover_ok"] and not v["errors"]
    )
    drive_verify_failed = [
        job_id for job_id, v in drive_verification.items()
        if not (v["resume_ok"] and v["cover_ok"] and not v["errors"])
    ]

    sheet_verified = 0
    sheet_verify_failed: list[str] = []
    if sheet_update_succeeded:
        fresh_rows = {r.get("Job ID"): r for r in sheets_mod.read_all_rows_with_job_id(cfg)}
        for job_id in sheet_update_succeeded:
            r = upload_succeeded[job_id]
            fresh = fresh_rows.get(job_id, {})
            # Only verify the links this row's upload actually produced --
            # a link this run didn't touch was never written, so comparing
            # it would fail regardless of the write's real success.
            ok = (
                (not r.resume_link or fresh.get("Resume (Drive)") == r.resume_link)
                and (not r.cover_link or fresh.get("Cover Letter (Drive)") == r.cover_link)
                and (not r.eval_link or fresh.get("Evaluation (Drive)") == r.eval_link)
            )
            if ok:
                sheet_verified += 1
            else:
                sheet_verify_failed.append(job_id)

    all_failed = upload_failed + sheet_update_failed
    fully_verified = (
        not all_failed
        and drive_verified == len(upload_succeeded)
        and sheet_verified == len(sheet_update_succeeded)
    )

    typer.echo("\n[backfill-drive] ── Reconciliation report ──────────────────────")
    typer.echo(f"  Apply rows found:            {len(apply_rows)}")
    typer.echo(f"  Skipped (already backfilled): {already_done}")
    typer.echo(f"  Skipped (needs regeneration): {len(needs_regen)}")
    typer.echo(f"  Uploaded to Drive:            {len(upload_succeeded)}/{len(to_process)}")
    typer.echo(f"  Updated in Sheets:            {len(sheet_update_succeeded)}/{len(upload_succeeded)}")
    typer.echo(f"  Drive links verified:         {drive_verified}/{len(upload_succeeded)}")
    typer.echo(f"  Sheet links verified:         {sheet_verified}/{len(sheet_update_succeeded)}")
    if all_failed:
        typer.echo(f"  FAILED ({len(all_failed)}):")
        for job_id, reason in all_failed:
            typer.echo(f"    - {job_id}: {reason}")
    if drive_verify_failed:
        typer.echo(f"  Drive verification FAILED for: {', '.join(drive_verify_failed)}")
    if sheet_verify_failed:
        typer.echo(f"  Sheet verification FAILED for: {', '.join(sheet_verify_failed)}")

    if fully_verified:
        typer.echo("\n[backfill-drive] MIGRATION COMPLETE — all uploads and Sheet updates verified.")
    else:
        typer.echo("\n[backfill-drive] MIGRATION INCOMPLETE — see failures/verification gaps above. "
                   "Safe to re-run: already-backfilled rows are skipped.", err=True)
        raise typer.Exit(1)
