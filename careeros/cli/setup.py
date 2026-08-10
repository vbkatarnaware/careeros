"""Setup/onboarding commands: init, providers, config."""

from __future__ import annotations

import shutil
from pathlib import Path

import typer
import yaml

from careeros import budget
from careeros.cli import app
from careeros.cli._shared import REPO_ROOT, _config, _load_profile, _provider_query_cfg
from careeros.config import enabled_providers
from careeros.pipeline.queryplan import build_query_plan
from careeros.providers.registry import list_providers


# ── init ──────────────────────────────────────────────────────────────────

@app.command(rich_help_panel="Setup")
def init():
    """Scaffold .careeros/ (config, profile template, cache/runs dirs)."""
    careeros_dir = Path(".careeros")
    careeros_dir.mkdir(exist_ok=True)
    (careeros_dir / "cache").mkdir(exist_ok=True)
    (careeros_dir / "runs").mkdir(exist_ok=True)

    config_path = careeros_dir / "config.yaml"
    if not config_path.exists():
        shutil.copy(REPO_ROOT / "templates" / "config.example.yaml", config_path)
        typer.echo(f"Wrote {config_path}")
    else:
        typer.echo(f"{config_path} already exists — left untouched")

    profile_path = careeros_dir / "profile.yaml"
    if not profile_path.exists():
        shutil.copy(REPO_ROOT / "templates" / "profile.example.yaml", profile_path)
        typer.echo(f"Wrote {profile_path} (seeded template — edit with your own facts,"
                    " or run `/careeros start` for the guided onboarding)")
    else:
        typer.echo(f"{profile_path} already exists — left untouched")

    typer.echo(
        "\nNext:\n"
        "  1. Install the free discovery source: pip install -e \".[ats-dataset]\" "
        "(no signup, no API key — see careeros/providers/ats_dataset.py). Only if "
        "you want the optional paid Fantastic Jobs source instead/as well, set "
        "providers.fantastic-jobs.enabled: true in .careeros/config.yaml and its "
        "api.transport + key env var (FANTASTIC_API_KEY / RAPIDAPI_KEY).\n"
        "  2. Run `/careeros start` inside your host coding CLI — paste your CV "
        "(or `skip`), set your interviews/week goal, plan, and daily job limit, "
        "and choose Google Sheets/Drive or local-only results (Sheets/Drive is "
        "optional — see docs/google-setup.md; local mode needs nothing extra).\n"
        "  3. Run `careeros doctor` to confirm everything's ready.\n"
        "  4. Run `/careeros daily`."
    )


# ── providers / config ───────────────────────────────────────────────────

@app.command(rich_help_panel="Advanced")
def providers():
    """List registered discovery providers."""
    for name in list_providers():
        typer.echo(name)


@app.command(rich_help_panel="Setup")
def config():
    """Print the resolved config."""
    cfg = _config()
    typer.echo(yaml.dump({
        "providers": cfg.providers,
        "threshold_apply": cfg.threshold,
        "threshold_consider": cfg.consider_threshold,
        "gate_batch_size": cfg.gate_batch_size, "prompts": cfg.prompts,
        "sheets": cfg.sheets,
    }, sort_keys=False))

    # Per-enabled-provider budget/quota preview — CAPABILITY-driven (see
    # budget.guard_for), never a name check. Advisory only, shows what
    # `discover` would print/enforce; never changes anything.
    for name in enabled_providers(cfg):
        provider_cfg = _provider_query_cfg(cfg, name)
        if budget.guard_for(provider_cfg) != "weekly":
            continue
        try:
            reqs = len(build_query_plan(_load_profile(cfg), provider_cfg)) if cfg.profile_path.exists() else 1
        except Exception:
            reqs = 1
        rec = budget.recommend(provider_cfg, cfg.goals, reqs)
        typer.echo(f"[{name}]")
        typer.echo("\n".join(rec.lines()))
