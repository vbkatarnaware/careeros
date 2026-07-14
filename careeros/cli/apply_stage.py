"""Application Answers (AI stage: prepare / finalize, Apply-tier only —
P2.10), plus the `apply` command's on-demand single-job entry point.

Automatic Application Answers for every Apply-tier (score >= threshold) job,
run as part of `daily` right after resume/cover. `careeros/apply/browser.py`
fetches each job's application-form text in the BACKGROUND (HTTP-first,
optional headless-Playwright fallback — never the user's own browser, never
a visible window). A form that isn't automatically readable is marked with
one of the specific `STATUS_*` codes below (e.g. a login-gated flow, a
closed posting, the optional Playwright extra not being installed) rather
than one generic "needs manual review" bucket — the candidate can always
run the on-demand `careeros apply <job-id>` (below) using their own real,
logged-in browser for that one job, or for any below-threshold job they
want to pursue anyway.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

import typer

from careeros import runmeta
from careeros.apply import browser as apply_browser
from careeros.cli import app
from careeros.cli._shared import REPO_ROOT, _config, _today
from careeros.config import Config
from careeros.lint import lint_file
from careeros.models import Eval, Job, dumps

# The full status taxonomy for an Apply-tier job's Application Answers,
# stored per-job in apply_status.json and shown per-job in the Sheet's
# "Application Answers (Drive)" cell (see `_STATUS_LABELS` below). Replaces
# the single generic "manual_required" this stage used to collapse every
# non-generated outcome into — each of these is a specific, mechanically
# distinguishable reason, so the candidate immediately knows what (if
# anything) they can do about it instead of having to open the job and
# investigate from scratch.
STATUS_GENERATED = "generated"
STATUS_LOGIN_REQUIRED = "login_required"
STATUS_PLAYWRIGHT_MISSING = "playwright_missing"
STATUS_CLOSED = "closed"
STATUS_NO_ESSAY_QUESTIONS = "no_essay_questions"
STATUS_NETWORK_ERROR = "network_error"
STATUS_BOT_CHECK = "bot_check"
# Preserved as the fallback for any outcome that doesn't match one of the
# specific reasons above (e.g. a fetch that failed for some other reason
# `browser.py` doesn't yet classify) — never removed, so status files from
# before this taxonomy existed still parse and display sensibly.
STATUS_MANUAL_REQUIRED = "manual_required"

# Sheet-cell / CLI-summary display label for each status code — a literal,
# human-readable string, not a URL, so the candidate immediately knows what
# happened and, where relevant, what to do about it, rather than expecting a
# broken/missing link.
_STATUS_LABELS = {
    STATUS_GENERATED: "✅ Generated",
    STATUS_LOGIN_REQUIRED: "🔒 Login Required",
    STATUS_PLAYWRIGHT_MISSING: "⚙️ Playwright Missing — pip install 'careeros[apply]' && playwright install chromium",
    STATUS_CLOSED: "❌ Closed",
    STATUS_NO_ESSAY_QUESTIONS: "📄 No Essay Questions",
    STATUS_NETWORK_ERROR: "🌐 Network Error",
    STATUS_BOT_CHECK: "🛡️ Bot-Blocked",
    STATUS_MANUAL_REQUIRED: "Manual review required",
}

# Maps a `careeros.apply.browser.REASON_*` fetch-failure reason to the
# specific status code above — the one place that translation happens, so
# `_apply_prepare` itself stays a plain lookup rather than a chain of ifs.
_REASON_TO_STATUS = {
    apply_browser.REASON_LOGIN_WALL: STATUS_LOGIN_REQUIRED,
    apply_browser.REASON_CLOSED_POSTING: STATUS_CLOSED,
    apply_browser.REASON_PLAYWRIGHT_MISSING: STATUS_PLAYWRIGHT_MISSING,
    apply_browser.REASON_NETWORK_ERROR: STATUS_NETWORK_ERROR,
    apply_browser.REASON_BOT_CHECK: STATUS_BOT_CHECK,
}


def _resolve_answers_cell(status_code: Optional[str], links: dict) -> str:
    """The single place that decides what goes in a job's Application
    Answers (Drive) cell: a specific status label for anything not
    generated, otherwise the actual Drive link (if uploaded) or blank.
    Shared by `sheets_append` (new rows) and `sheets_sync_status` (patching
    existing rows after a re-run of `apply --prepare/--finalize`) so the two
    can never drift out of sync with each other."""
    if status_code and status_code != STATUS_GENERATED:
        return _STATUS_LABELS.get(status_code, _STATUS_LABELS[STATUS_MANUAL_REQUIRED])
    return links.get("answers", "")


# Statuses `_apply_prepare` can assign BEFORE the agent ever sees a job —
# each one means the form fetch itself already produced a final answer, so
# `_apply_finalize` must treat these as already-resolved rather than
# expecting an answers.md for them. STATUS_NO_ESSAY_QUESTIONS is
# deliberately excluded: it can only be known AFTER the agent reads a
# genuinely-fetched real form and finds no real questions in it, so it's
# only ever assigned inside `_apply_finalize` itself.
_PREPARE_TERMINAL_STATUSES = frozenset({
    STATUS_GENERATED, STATUS_LOGIN_REQUIRED, STATUS_PLAYWRIGHT_MISSING,
    STATUS_CLOSED, STATUS_NETWORK_ERROR, STATUS_BOT_CHECK, STATUS_MANUAL_REQUIRED,
})


def _apply_status_path(cfg: Config, date: str) -> Path:
    return runmeta.run_dir(cfg.runs_dir, date) / "apply_status.json"


def _load_apply_status(cfg: Config, date: str) -> dict:
    path = _apply_status_path(cfg, date)
    if not path.exists():
        return {}
    with open(path) as f:
        return json.load(f)


def _save_apply_status(cfg: Config, date: str, status: dict) -> None:
    with open(_apply_status_path(cfg, date), "w") as f:
        f.write(dumps(status))


def _apply_prepare(cfg: Config, date: str) -> None:
    selected_path = runmeta.stage_dir(cfg.runs_dir, date, "select") / "selected.json"
    jobs_path = runmeta.stage_dir(cfg.runs_dir, date, "normalize") / "jobs.json"
    if not selected_path.exists() or not jobs_path.exists():
        typer.echo("Missing select/normalize output — run those stages first.", err=True)
        raise typer.Exit(1)

    with open(selected_path) as f:
        evals = [Eval.from_dict(d) for d in json.load(f)]
    with open(jobs_path) as f:
        jobs_by_id = {j["id"]: Job.from_dict(j) for j in json.load(f)}

    status: dict[str, str] = {}
    to_generate: list[dict] = []
    fetch_methods: dict[str, str] = {}

    for e in evals:
        job = jobs_by_id.get(e.id)
        if job is None:
            continue
        artifacts_path = runmeta.artifacts_dir(cfg.runs_dir, date, e.id)
        answers_path = artifacts_path / "answers.md"
        if answers_path.exists():
            status[e.id] = STATUS_GENERATED  # already drafted (e.g. a resumed run) — never re-fetch/redraft
            continue

        form_text, method, reason = apply_browser.fetch_visible_text(job.apply_url)
        fetch_methods[e.id] = method
        # `reason` must be checked BEFORE `form_text` truthiness: a login
        # wall, closed posting, or bot-check page can come back with
        # substantial, real, non-empty text (see browser.py's
        # fetch_visible_text docstring) -- it's just the wrong page, not an
        # empty fetch. Checking `not form_text` first would silently send
        # that boilerplate to the agent as if it were the real form.
        if reason is not None:
            status[e.id] = _REASON_TO_STATUS.get(reason, STATUS_MANUAL_REQUIRED)
            continue
        if not form_text:
            status[e.id] = STATUS_MANUAL_REQUIRED
            continue

        context_path = artifacts_path / "_context.json"
        input_payload = {
            "id": e.id, "company": job.company, "title": job.title,
            "apply_url": job.apply_url, "ats": job.ats,
            "fetch_method": method, "form_text": form_text,
            "eval_path": str(runmeta.stage_dir(cfg.runs_dir, date, "evaluate") / f"{e.id}.json"),
            "context_path": str(context_path) if context_path.exists() else None,
            "artifacts_path": str(artifacts_path),
        }
        with open(artifacts_path / "_apply_input.json", "w") as f:
            f.write(dumps(input_payload))
        to_generate.append(input_payload)

    _save_apply_status(cfg, date, status)
    manual_count = sum(1 for v in status.values() if v not in (STATUS_GENERATED,))
    already_count = sum(1 for v in status.values() if v == STATUS_GENERATED)
    status_counts = {s: sum(1 for v in status.values() if v == s) for s in _STATUS_LABELS}
    runmeta.write_stage_meta(cfg.runs_dir, date, "apply", {
        "prepared_at": time.time(), "fetch_methods": fetch_methods,
        "manual_required": manual_count, "already_generated": already_count,
        "status_counts": {s: c for s, c in status_counts.items() if c},
    })

    typer.echo(
        f"[apply:prepare] {len(evals)} Apply-tier job(s): {len(to_generate)} form(s) readable "
        f"(need drafting), {manual_count} need manual review (form not automatically readable), "
        f"{already_count} already generated.\n"
    )
    if to_generate:
        typer.echo(
            "AGENT INSTRUCTIONS:\n"
            f"Read {cfg.prompt_path('apply')} and .careeros/profile.yaml.\n"
            "For each job below, `form_text` is the application form's rendered page text\n"
            "(fetched automatically in the background — no candidate paste needed for this\n"
            "batch). Identify the real application questions from it, then draft\n"
            "artifacts/<id>/answers.md per the prompt (every answer must trace to\n"
            "profile.yaml / the eval / cached context). If `form_text` doesn't actually contain\n"
            "identifiable application questions (e.g. a login/error page the fetch still\n"
            "partially rendered, or a genuinely real form with no free-text essay questions),\n"
            "do NOT invent questions — leave that job's answers.md unwritten; it will be marked\n"
            "'No Essay Questions' and the candidate can run `careeros apply <job-id>` themselves\n"
            "if they still want to double-check by hand.\n"
            "Then run:\n"
            f"  careeros apply --finalize --date {date}\n\n"
            + dumps(to_generate)
        )
    else:
        typer.echo(f"Nothing to draft — run `careeros apply --finalize --date {date}` to finalize.")


def _apply_finalize(cfg: Config, date: str) -> None:
    selected_path = runmeta.stage_dir(cfg.runs_dir, date, "select") / "selected.json"
    with open(selected_path) as f:
        evals = [Eval.from_dict(d) for d in json.load(f)]

    status = _load_apply_status(cfg, date)
    errors: list[str] = []
    newly_generated = 0

    for e in evals:
        if status.get(e.id) in _PREPARE_TERMINAL_STATUSES:
            continue  # prepare already resolved this one (cache hit / unreadable form)
        artifacts_path = runmeta.artifacts_dir(cfg.runs_dir, date, e.id)
        answers_path = artifacts_path / "answers.md"
        if not answers_path.exists():
            # The agent legitimately chose to skip this job: prepare DID fetch
            # a real, usable form (otherwise it would already be one of the
            # _PREPARE_TERMINAL_STATUSES above) — the agent just didn't find
            # any free-text essay questions in it.
            status[e.id] = STATUS_NO_ESSAY_QUESTIONS
            continue
        voice_issues = lint_file(str(answers_path))
        if voice_issues:
            for issue in voice_issues:
                errors.append(f"{e.id}: answers.md voice-dna: {issue.kind} at line {issue.line}")
            continue
        status[e.id] = "generated"
        newly_generated += 1

    if errors:
        typer.echo("[apply:finalize] Issues found (unresolved until fixed):\n" + "\n".join(errors), err=True)
        typer.echo(f"\nAgent: fix the listed files, then re-run `careeros apply --finalize --date {date}`.")
        raise typer.Exit(1)

    _save_apply_status(cfg, date, status)

    meta = runmeta.read_stage_meta(cfg.runs_dir, date, "apply")
    elapsed = time.time() - meta["prepared_at"] if "prepared_at" in meta else 0.0
    manual_count = sum(1 for v in status.values() if v != STATUS_GENERATED)
    generated_count = sum(1 for v in status.values() if v == STATUS_GENERATED)

    typer.echo(f"[apply:finalize] {len(evals)} Apply-tier job(s): {generated_count} answers generated, "
               f"{manual_count} need manual review, {newly_generated} newly drafted this pass.")
    runmeta.record_stage(cfg.runs_dir, date, "apply",
                          count_in=len(evals), count_out=generated_count, seconds=elapsed)


@app.command(rich_help_panel="Per-job")
def apply(
    job_id: str = typer.Argument(
        None, help="On-demand: draft answers for one job via the host-CLI skill (any score)."),
    prepare: bool = typer.Option(
        False, "--prepare", help="Batch: fetch + write apply input for every Apply-tier job."),
    finalize: bool = typer.Option(
        False, "--finalize", help="Batch: validate the agent-written answers.md files."),
    date: str = typer.Option(None, help="Run date for --prepare/--finalize, default today"),
):
    """Application Answers. Two entry points: the automatic Apply-tier batch
    (--prepare/--finalize, run as part of `daily`, background form-reading —
    see careeros/apply/browser.py) or on-demand for one job at a time (any
    score, host-CLI skill, the candidate's own real logged-in browser)."""
    if prepare or finalize:
        cfg = _config()
        d = date or _today()
        if prepare:
            _apply_prepare(cfg, d)
        else:
            _apply_finalize(cfg, d)
        return
    if not job_id:
        typer.echo("Pass a job-id for on-demand apply, or --prepare/--finalize "
                   "for the automatic Apply-tier batch stage.", err=True)
        raise typer.Exit(1)
    typer.echo(
        "`careeros apply <job-id>` is a host-CLI skill, not a single blocking "
        "Python call — drafting application answers needs the agent's "
        "reasoning over the job's real form and your profile.\n\n"
        f"Run it as `/careeros apply {job_id}` in your host CLI.\n"
        f"The skill playbook is at {REPO_ROOT / 'skills' / 'apply.md'}."
    )
