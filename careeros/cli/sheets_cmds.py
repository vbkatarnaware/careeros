"""Google Sheets operations: append, migrate, sync-status."""

from __future__ import annotations

import json
import time

import typer

from careeros import runmeta
from careeros import sheets as sheets_mod
from careeros.cli import sheets_app
from careeros.cli._shared import _config, _today
from careeros.cli.apply_stage import STATUS_GENERATED, _load_apply_status, _resolve_answers_cell
from careeros.models import Eval, Job
from careeros.pipeline.dedupe import append_seen_ids


def _consider_note(e: Eval, apply_threshold: float) -> str:
    """A concise, human-readable reason a CONSIDER-tier job fell short of the
    apply threshold — drawn from the eval's own weaknesses so a near-miss is
    self-explanatory in the Sheet without opening the eval JSON. No AI call."""
    reasons = "; ".join(w.strip() for w in (e.weaknesses or [])[:2] if w and w.strip())
    if not reasons:
        reasons = (e.fit_paragraph or e.company_summary or "").strip()[:200]
    prefix = f"Consider (scored {e.score:g}, below {apply_threshold:g})"
    return f"{prefix}: {reasons}" if reasons else prefix


# The Application Answers (Drive) cell's value for an Apply-tier job whose
# form wasn't automatically readable now comes from `_STATUS_LABELS`
# (careeros/cli/apply_stage.py, near `_apply_prepare`) — one specific,
# human-readable status per job rather than a single generic label, so the
# candidate immediately knows WHY (a login wall, a closed posting, Playwright
# not installed, ...) and, where relevant, what to do about it, instead of
# expecting a broken/missing link.


@sheets_app.command("append")
def sheets_append(date: str = typer.Option(None, help="Run date, default today")):
    """[dev] Append selected jobs' rows to the configured Google Sheet — off
    by default (sheets.enabled: false), same optional/config-gated pattern
    as `drive`. In local mode, `daily`'s digest is the record instead."""
    cfg = _config()
    date = date or _today()

    if not cfg.sheets.get("enabled", False):
        typer.echo("[sheets:append] disabled (set sheets.enabled: true in .careeros/config.yaml to use) "
                   "— results stay local under .careeros/results/.")
        return

    start = time.time()

    select_dir = runmeta.stage_dir(cfg.runs_dir, date, "select")
    jobs_path = runmeta.stage_dir(cfg.runs_dir, date, "normalize") / "jobs.json"
    with open(select_dir / "selected.json") as f:
        apply_evals = [Eval.from_dict(d) for d in json.load(f)]
    consider_path = select_dir / "consider.json"  # absent on older runs
    consider_evals = ([Eval.from_dict(d) for d in json.load(open(consider_path))]
                      if consider_path.exists() else [])
    with open(jobs_path) as f:
        jobs_by_id = {j["id"]: Job.from_dict(j) for j in json.load(f)}

    # Optional hand-off from `careeros drive` (Phase 3) — sheets.py has no
    # import dependency on drive.py; if the file isn't there (Drive disabled,
    # not yet run, or it failed), every row's Drive cells are just blank.
    # {"job_id": {"resume": url, "cover": url, "eval": url, "deep_report": url,
    #             "answers": url, "warnings": [...]}}
    drive_links_path = runmeta.run_dir(cfg.runs_dir, date) / "drive_links.json"
    drive_links: dict = {}
    if drive_links_path.exists():
        with open(drive_links_path) as f:
            drive_links = json.load(f)

    # Optional hand-off from `careeros apply --finalize` (P2.10) —
    # {"job_id": <one of the STATUS_* codes above>}. Absent entirely (apply
    # stage never ran, e.g. an older run predating this feature) -> every
    # Apply row's Application Answers cell is just blank, same as any other
    # optional artifact that hasn't been generated yet.
    apply_status = _load_apply_status(cfg, date)

    rows = []
    # APPLY tier: full row with any Drive links.
    for e in apply_evals:
        job = jobs_by_id[e.id]
        links = drive_links.get(e.id, {})
        answers_cell = _resolve_answers_cell(apply_status.get(e.id), links)
        rows.append(sheets_mod.job_to_row(
            date, job, e,
            resume_drive_link=links.get("resume", ""),
            cover_drive_link=links.get("cover", ""),
            eval_drive_link=links.get("eval", ""),
            deep_report_drive_link=links.get("deep_report", ""),
            answers_drive_link=answers_cell,
            tier="Apply",
        ))
    # CONSIDER tier: near-misses — NO artifacts, NO Drive; just score + a
    # concise reason it fell short of the apply threshold (from the eval).
    for e in consider_evals:
        job = jobs_by_id[e.id]
        rows.append(sheets_mod.job_to_row(
            date, job, e,
            tier="Consider",
            notes=_consider_note(e, cfg.threshold),
        ))

    sheets_mod.append_rows(cfg, rows)
    typer.echo(f"[sheets:append] wrote {len(rows)} row(s) "
               f"({len(apply_evals)} Apply, {len(consider_evals)} Consider).")

    # Mark both tiers seen so neither re-surfaces next run (both appear in the Sheet).
    seen_path = cfg.careeros_dir / "seen.jsonl"
    append_seen_ids(seen_path, [jobs_by_id[e.id] for e in apply_evals + consider_evals], date)

    runmeta.record_stage(cfg.runs_dir, date, "sheets",
                          count_in=len(apply_evals) + len(consider_evals), count_out=len(rows),
                          seconds=time.time() - start)


@sheets_app.command("migrate")
def sheets_migrate():
    """Clean up the live Sheet right now: physically remove the deprecated
    Resume/Cover Letter/Report Path and Drive Folder columns, add the new
    Drive-link + Status columns, apply header/Score/Status formatting, and
    sort existing rows by Date descending (newest on top — a one-time fix
    for a Sheet built before P2.11's rows insert at the top automatically).
    This is the exact same pass `sheets append` already runs automatically
    on every write (see `sheets.py:append_rows`), minus the date sort (that
    part only needs to run once) — this command just exposes it standalone
    so an existing Sheet doesn't have to wait for the next `daily` run to
    clean up. Safe to re-run: idempotent, and a no-op once already current."""
    cfg = _config()
    result = sheets_mod.migrate(cfg)
    if result["removed"]:
        typer.echo(f"[sheets:migrate] Removed: {', '.join(result['removed'])}")
    if result["added"]:
        typer.echo(f"[sheets:migrate] Added: {', '.join(result['added'])}")
    if result.get("reordered"):
        typer.echo("[sheets:migrate] Columns reordered to match the canonical layout.")
    if result.get("blanks_filled"):
        typer.echo("[sheets:migrate] Blank cells filled with \"-\".")
    if result.get("date_sorted"):
        typer.echo("[sheets:migrate] Rows sorted by Date descending (newest on top).")
    if not any(result.get(k) for k in ("removed", "added", "reordered", "blanks_filled", "date_sorted")):
        typer.echo("[sheets:migrate] Schema already up to date — formatting refreshed.")
    else:
        typer.echo("[sheets:migrate] Done.")


@sheets_app.command("sync-status")
def sheets_sync_status(date: str = typer.Option(None, help="Run date, default today")):
    """Patch the Application Answers (Drive) cell of EXISTING Sheet rows for
    a date's NON-generated Apply-tier jobs (login_required, closed,
    no_essay_questions, playwright_missing, network_error, bot_check,
    manual_required), from apply_status.json — without appending new rows
    or touching any other cell. `sheets append` only ever ADDS rows; it
    never revisits a row already in the Sheet. Use this after re-running
    `careeros apply --prepare/--finalize` for a date whose rows are already
    there (e.g. reclassifying old jobs that were marked with the old
    generic manual_required into the newer, more specific status taxonomy)
    so the Sheet catches up without a duplicate row or a full re-append.

    Deliberately SKIPS `generated` jobs: `drive_links.json` (this stage's
    only local record of a job's Drive links) is only ever refreshed by the
    full `careeros drive` batch command, NOT by `careeros publish` (which
    patches the Sheet directly without also rewriting that file) — so
    re-deriving a `generated` job's cell from it here can read a STALE or
    missing "answers" link and overwrite a correct one `publish` just set
    moments earlier. For a `generated` job, `publish <job-id>` is the only
    source of truth for that cell; this command leaves it alone."""
    cfg = _config()
    date = date or _today()

    apply_status = _load_apply_status(cfg, date)

    if not apply_status:
        typer.echo(f"[sheets:sync-status] No apply_status.json for --date {date} — nothing to sync.")
        return

    updated, skipped_generated, not_found = 0, 0, []
    for job_id, status_code in apply_status.items():
        if status_code == STATUS_GENERATED:
            skipped_generated += 1
            continue
        cell = _resolve_answers_cell(status_code, {})
        if sheets_mod.update_row_by_job_id(cfg, job_id, {"Application Answers (Drive)": cell}):
            updated += 1
        else:
            not_found.append(job_id)

    typer.echo(f"[sheets:sync-status] {updated} row(s) updated.")
    if skipped_generated:
        typer.echo(
            f"[sheets:sync-status] {skipped_generated} 'generated' job(s) skipped "
            "— run `careeros publish <job-id>` for those instead."
        )
    if not_found:
        typer.echo(
            f"[sheets:sync-status] {len(not_found)} job(s) not found in the Sheet "
            f"(never appended, or already removed by hand): {', '.join(not_found)}"
        )
