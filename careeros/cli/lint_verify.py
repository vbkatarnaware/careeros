"""Deterministic checks a candidate can run by hand on any generated
artifact: voice-dna lint and resume truthfulness verification."""

from __future__ import annotations

import json

import typer

from careeros.cli import app
from careeros.cli._shared import _config, _load_profile
from careeros.lint import format_issues, lint_file, verify_resume_bullets, verify_resume_facts


@app.command(hidden=True)
def lint(file: str):
    """[dev] Check a generated artifact against the deterministic voice-dna
    rules (em-dashes, banned AI vocabulary, negative-parallelism tell)."""
    issues = lint_file(file)
    typer.echo(format_issues(issues))
    if issues:
        raise typer.Exit(1)


@app.command("verify-resume", hidden=True)
def verify_resume(
    file: str,
    company: str = typer.Option(
        None, "--company",
        help="The target job's company name, to check the transferable-language "
             "(never-name-the-target-company) rule. Omit to skip that check.",
    ),
):
    """[dev] Deterministic truthfulness check for resume.json (v2): every
    reworded experience bullet must preserve every number/metric from its
    source profile.yaml bullet (no invented or dropped fact), and no field
    may name the target company. CareerOS's analog of Career Ops'
    plan-lint.mjs verbatim check — enforces resume_v2.md's rules
    mechanically, not just via prompt instruction.

    A bare .md file (v1 legacy) is also accepted for backward compatibility
    with any not-yet-backfilled historical resume — verbatim-matched against
    profile.yaml the old way."""
    cfg = _config()
    profile = _load_profile(cfg)
    if file.endswith(".md"):
        with open(file, encoding="utf-8") as f:
            resume_md = f.read()
        issues = verify_resume_bullets(resume_md, profile)
        if not issues:
            typer.echo("OK — every bullet/summary verbatim-matches profile.yaml.")
            return
    else:
        with open(file, encoding="utf-8") as f:
            resume_json = json.load(f)
        issues = verify_resume_facts(resume_json, profile, target_company=company)
        if not issues:
            typer.echo(
                "OK — every reworded bullet preserves its source facts"
                + (" and no company-name leak." if company else " (pass --company to also check for a name leak).")
            )
            return
    typer.echo(f"{len(issues)} truthfulness issue(s) found:")
    for issue in issues:
        typer.echo(f"  - {issue}")
    raise typer.Exit(1)
