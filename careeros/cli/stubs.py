"""End-user stubs — real orchestration lives in skills/*.md, run by the host
coding agent. These commands exist so `careeros <cmd>` is discoverable and
prints the right entry point. (`apply` is a hybrid stub + batch command and
lives in careeros/cli/apply_stage.py instead; `publish` is a real, fully
Python-implemented per-job command and lives in careeros/cli/perjob.py.)"""

from __future__ import annotations

import typer

from careeros.cli import app
from careeros.cli._shared import REPO_ROOT


def _daily_stub():
    typer.echo(
        "`careeros daily` is a host-CLI skill, not a single blocking Python call — "
        "AI stages (gate, evaluate, resume, cover) need the agent's reasoning.\n\n"
        "Run it as `/careeros daily` in Claude Code / Codex / Gemini CLI / etc.\n"
        f"The skill playbook is at {REPO_ROOT / 'skills' / 'daily.md'}, and it "
        "orchestrates exactly the dev-stage commands above, in order."
    )


@app.command(rich_help_panel="Daily")
def daily():
    """Run the full daily pipeline. Entry point for the host-CLI skill."""
    _daily_stub()


@app.command(rich_help_panel="Daily")
def scan():
    """Alias for `daily` — CareerOS's job is scanning the market for you."""
    _daily_stub()


@app.command(rich_help_panel="Setup")
def start():
    """Guided onboarding -> .careeros/profile.yaml + discovery goal/plan."""
    typer.echo(
        "`careeros start` is a host-CLI skill, not a single blocking Python call — "
        "an interactive onboarding needs the agent's reasoning to extract facts "
        "from your CV and ask good follow-ups.\n\n"
        "Run it as `/careeros start` in Claude Code / Codex / Gemini CLI / etc.\n"
        f"The skill playbook is at {REPO_ROOT / 'skills' / 'start.md'}.\n\n"
        "It opens by asking you to paste your CV (optional — type `skip` to "
        "build your profile by answering questions instead), then captures "
        "your interviews/week goal, Fantastic Jobs plan, and whether you want "
        "Google Sheets/Drive or a local-only results folder.\n\n"
        "For now, you can also hand-edit .careeros/profile.yaml directly "
        "(seeded from templates/profile.example.yaml by `careeros init`)."
    )


@app.command(rich_help_panel="Per-job")
def prep(job_id: str):
    """Generate the Level-2 deep interview-prep report for one job."""
    typer.echo(
        "`careeros prep` is a host-CLI skill, not a single blocking Python call — "
        "interview-prep synthesis needs the agent's reasoning over the job "
        "description and your profile.\n\n"
        f"Run it as `/careeros prep {job_id}` in your host CLI.\n"
        f"The skill playbook is at {REPO_ROOT / 'skills' / 'prep.md'}."
    )
