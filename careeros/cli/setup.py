"""Setup/onboarding commands: init, providers, config, migrate-config."""

from __future__ import annotations

import shutil
from pathlib import Path

import typer
import yaml

from careeros import budget
from careeros.cli import app
from careeros.cli._shared import REPO_ROOT, _config, _load_profile, _provider_query_cfg, _today
from careeros.config import LEGACY_PROVIDER_DEPRECATION_NOTICE, enabled_providers
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
        "  1. In .careeros/config.yaml, set api.transport to \"direct\" or \"rapidapi\" "
        "and the matching key env var (FANTASTIC_API_KEY / RAPIDAPI_KEY). (Prefer the "
        "legacy Apify actor instead? Set provider: fantastic-jobs-actor and APIFY_TOKEN "
        "— see providers/README.md.)\n"
        "  2. Run `/careeros start` inside your host coding CLI — paste your CV "
        "(or `skip`), set your interviews/week goal and plan, and choose Google "
        "Sheets/Drive or local-only results (Sheets/Drive is optional — see "
        "docs/google-setup.md; local mode needs nothing extra).\n"
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
    if cfg.provider_migrated:
        typer.echo(f"NOTE: {LEGACY_PROVIDER_DEPRECATION_NOTICE.format(name=cfg.provider)}\n")
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
        capability = budget.guard_for(provider_cfg)
        if capability == "weekly":
            try:
                reqs = len(build_query_plan(_load_profile(cfg), provider_cfg)) if cfg.profile_path.exists() else 1
            except Exception:
                reqs = 1
            rec = budget.recommend(provider_cfg, cfg.goals, reqs)
            typer.echo(f"[{name}]")
            typer.echo("\n".join(rec.lines()))
        elif capability == "monthly":
            max_budget = provider_cfg.get("max_monthly_budget_usd") or cfg.apify.get("max_monthly_budget_usd")
            state = budget.load_apify_state(cfg.careeros_dir, _today())
            spent = state.get("spend_usd", 0.0)
            typer.echo(f"[{name}] Apify budget (estimated): ${spent:.4f}/${max_budget or 0:.2f} used this month")


@app.command("migrate-config", hidden=True)
def migrate_config():
    """Rewrite .careeros/config.yaml's deprecated single `provider:` key to
    the v1.2 `providers:` model, permanently, on disk. Same shape as
    `careeros sheets migrate`: explicit, on-demand, idempotent — a no-op if
    the file is already on the new model or doesn't set `provider:` at all.

    This does NOT change what runs — it upgrades exactly ONE provider (the
    one `provider:` already named) to `providers: {<that provider>:
    {enabled: true}}`, nothing new is enabled. `load_config` already does
    this same upgrade automatically, in memory, on every run (with a
    one-time deprecation notice) — this command is what makes it permanent
    so that notice stops appearing. See config.py's `_migrate_legacy_provider`
    for the exact rule this mirrors."""
    config_path = Path(".careeros/config.yaml")
    if not config_path.exists():
        typer.echo(f"[migrate-config] {config_path} not found — nothing to migrate.", err=True)
        raise typer.Exit(1)

    with open(config_path) as f:
        raw_cfg = yaml.safe_load(f) or {}

    if "providers" in raw_cfg:
        typer.echo("[migrate-config] Already on the providers: model — nothing to do.")
        return
    if "provider" not in raw_cfg:
        typer.echo("[migrate-config] No `provider:` key set — nothing to migrate.")
        return

    legacy_name = raw_cfg.pop("provider")
    raw_cfg["providers"] = {legacy_name: {"enabled": True}}
    with open(config_path, "w") as f:
        yaml.dump(raw_cfg, f, sort_keys=False)

    typer.echo(
        f"[migrate-config] Rewrote {config_path}: provider: {legacy_name} -> "
        f"providers: {{{legacy_name}: {{enabled: true}}}}. Same single source, "
        "nothing new enabled — add more providers by hand when you're ready "
        "(see providers/README.md)."
    )
