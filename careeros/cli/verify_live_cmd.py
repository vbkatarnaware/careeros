"""`careeros verify-live` (v2.1) — cross-check Apply-tier jobs against their
REAL postings before any resume/cover letter is generated for them.

Exists because of two same-day misses (2026-07-31) where the provider's
metadata was simply wrong and the pipeline scored honestly against it:
MDOTM was labelled remote but is Milan-hybrid; AU Small Finance Bank was
labelled "2-5 years" but asks for 7. Neither is fixable with a better filter
or a better prompt — both would read the same wrong field. See
careeros/pipeline/verify_live.py's module docstring.

FLAGS, never auto-rejects: a fetch can fail, a page can be a login wall, and
a regex can misread prose. Findings go to the candidate to decide on, per
AGENT_GUIDE.md's Failure Handling Principle.
"""

from __future__ import annotations

import json
import time

import typer

from careeros import runmeta
from careeros.apply import browser
from careeros.cli import app
from careeros.cli._shared import _config, _load_profile, _today
from careeros.config import Config
from careeros.models import Eval, Job, dumps
from careeros.pipeline.verify_live import check_job


@app.command(hidden=True, name="verify-live")
def verify_live(
    date: str = typer.Option(None, help="Run date, default today"),
    timeout: float = typer.Option(10.0, help="Per-posting fetch timeout, seconds"),
):
    """[dev] Fetch each Apply-tier job's live posting and check the two
    things the provider gets wrong most often — years-of-experience and
    location/work-arrangement — against the candidate's own profile.

    Cheap by construction: only Apply-tier jobs reach here (1-3 on a typical
    day). Run AFTER `threshold` and BEFORE `artifacts --prepare`, so a job
    that turns out to be impossible never costs a generated resume."""
    cfg = _config()
    date = date or _today()
    start = time.time()

    select_dir = runmeta.stage_dir(cfg.runs_dir, date, "select")
    selected_path = select_dir / "selected.json"
    if not selected_path.exists():
        typer.echo(f"No {selected_path} — run `careeros threshold` first.", err=True)
        raise typer.Exit(1)
    with open(selected_path) as f:
        apply_evals = [Eval.from_dict(d) for d in json.load(f)]

    if not apply_evals:
        typer.echo("[verify-live] No Apply-tier jobs today — nothing to verify.")
        return

    jobs_path = runmeta.stage_dir(cfg.runs_dir, date, "normalize") / "jobs.json"
    with open(jobs_path) as f:
        jobs_by_id = {j["id"]: Job.from_dict(j) for j in json.load(f)}

    profile = _load_profile(cfg)
    max_years_ok = (getattr(profile, "deal_breakers", {}) or {}).get("min_years_ok", 3)
    onsite_ok = list((getattr(profile, "location", {}) or {}).get("onsite_ok", []) or [])

    checks = []
    for e in apply_evals:
        job = jobs_by_id.get(e.id)
        if job is None:
            continue  # stale eval with no matching job this run; sheets/artifacts skip it too
        page_text, method, reason = browser.fetch_visible_text(job.apply_url, timeout=timeout)
        check = check_job(
            job_id=job.id, title=job.title, company=job.company,
            page_text=page_text, fetch_reason=reason,
            max_years_ok=max_years_ok, provider_said_remote=job.remote,
            onsite_ok_cities=onsite_ok, job_location=job.location,
        )
        checks.append(check)
        typer.echo(f"  [verify-live] {job.company} — {job.title}: "
                   f"{'OK' if not check.concerns else str(len(check.concerns)) + ' concern(s)'}"
                   f" (fetched via {method})")

    out_path = select_dir / "live_checks.json"
    with open(out_path, "w") as f:
        f.write(dumps([c.to_dict() for c in checks]))

    flagged = [c for c in checks if c.concerns]
    typer.echo(f"[verify-live] {len(checks)} Apply-tier job(s) checked, "
               f"{len(flagged)} with concerns -> {out_path}")

    if flagged:
        typer.echo("")
        for c in flagged:
            typer.echo(f"  ⚠ {c.company} — {c.title}")
            for concern in c.concerns:
                typer.echo(f"      {concern}")
        typer.echo(
            "\nAgent: surface these to the candidate in plain language BEFORE running "
            "`careeros artifacts --prepare` — a job whose live posting contradicts what we "
            "scored should not silently get a resume generated for it. Per the Failure "
            "Handling Principle, state what was found and let them decide whether to keep, "
            "drop, or re-score it."
        )

    runmeta.record_stage(cfg.runs_dir, date, "verify_live",
                          count_in=len(apply_evals), count_out=len(checks),
                          seconds=time.time() - start,
                          errors=[f"{c.company} — {c.title}: {c.concerns[0]}" for c in flagged])
