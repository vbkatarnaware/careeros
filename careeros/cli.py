"""careeros CLI.

Thin dispatch only — every command here calls into careeros/{config,models,
cache,runmeta,lint,report,sheets}.py or careeros/{providers,pipeline}/. No
business logic lives in this file.

Two tiers of commands:
  - End-user:  init, start, daily, prep, apply, publish, config, providers
  - Developer: discover, normalize, dedupe, constraints, gate, evaluate,
               threshold, artifacts, apply --prepare/--finalize, sheets,
               lint, verify-resume — each stage runnable standalone against
               a run directory, for debugging without re-running the whole
               pipeline.

AI stages (gate, evaluate, artifacts, apply --prepare/--finalize) follow the
host-CLI execution boundary: a `--prepare` half (Python writes the stage's
input + an instruction for the agent) and a `--finalize` half (Python
validates whatever the agent wrote). See skills/daily.md for the full
instruction sequence. `apply` additionally has an on-demand, single-job form
(`careeros apply <job-id>`, no --prepare/--finalize) for any job at any
score, run manually via its own host-CLI skill.

`constraints` is deterministic: it hard-rejects jobs violating an objective
profile deal-breaker (location, salary floor) BEFORE any AI is spent, and
`threshold` re-checks the same constraints as a backstop so a hard-rejected
job can never slip through as "apply" even if the AI mislabels it.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Optional

import typer
import yaml

from careeros import budget
from careeros.apply import browser as apply_browser
from careeros.cache import Cache, artifact_cache_key, eval_cache_key
from careeros.config import (
    Config, LEGACY_PROVIDER_DEPRECATION_NOTICE, enabled_providers, load_config, provider_config_block,
)
from careeros.lint import format_issues, lint_file, verify_resume_bullets
from careeros.models import Eval, Job, Profile, dumps
from careeros.pipeline.dedupe import (
    append_seen_ids, dedupe_against_history, dedupe_against_sheet_ids,
    dedupe_cross_location, dedupe_in_run,
)
from careeros.pipeline.constraints import evaluate_constraints
from careeros.pipeline.normalize import normalize_all
from careeros.pipeline.queryplan import build_query_plan, resolve_tier_limit
from careeros.pipeline.threshold import partition_evals
from careeros.providers.base import ProviderError, ProviderResult
from careeros.providers.registry import get as get_provider
from careeros.providers.registry import list_providers
from careeros.report import render_daily_report, render_summary
from careeros import runmeta
from careeros import sheets as sheets_mod

app = typer.Typer(add_completion=False, no_args_is_help=True,
                   help="CareerOS — an AI-powered, deterministic job discovery and recommendation engine.")

REPO_ROOT = Path(__file__).resolve().parent.parent


def _version_callback(show_version: bool) -> None:
    if show_version:
        from careeros import __version__
        typer.echo(f"careeros {__version__}")
        raise typer.Exit()


@app.callback()
def _main(
    version: Optional[bool] = typer.Option(
        None, "--version", callback=_version_callback, is_eager=True,
        help="Show the installed careeros version and exit.",
    ),
) -> None:
    pass


def _provider_query_cfg(cfg: Config, provider_name: str) -> dict:
    """Thin cli.py-local alias for `config.provider_config_block` (v1.2) —
    kept under this name since it's used throughout this file. See that
    function's docstring for the exact per-provider resolution and why
    `cfg.apify` is deliberately NOT merged in generically (it would leak
    Apify-budget guard capability into the free providers)."""
    return provider_config_block(cfg, provider_name)


def _today() -> str:
    """Run date. Callers may override via --date for reproducible/resumed
    runs; this is the only place "today" is computed so tests can pass a
    fixed date instead."""
    import datetime
    return datetime.date.today().isoformat()


def _config() -> Config:
    return load_config()


# ── init ──────────────────────────────────────────────────────────────────

@app.command()
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
        "(or `skip`), set your interviews/week goal and plan, and set up Sheets "
        "credentials (see docs/google-setup.md).\n"
        "  3. Run `careeros doctor` to confirm everything's ready.\n"
        "  4. Run `/careeros daily`."
    )


# ── providers / config ───────────────────────────────────────────────────

@app.command()
def providers():
    """List registered discovery providers."""
    for name in list_providers():
        typer.echo(name)


@app.command()
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


@app.command("migrate-config")
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


# ── doctor ────────────────────────────────────────────────────────────────

class _CheckStatus:
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


def _check_result(status: str, label: str, detail: str = "") -> tuple[str, str, str]:
    return (status, label, detail)


def _run_doctor_checks(cfg: Config) -> list[tuple[str, str, str]]:
    """Pure(ish) — reads env vars, config, and the filesystem; makes no
    network calls and changes nothing. Each check is independent so one
    failure never hides the rest."""
    import os
    import sys as _sys

    results: list[tuple[str, str, str]] = []

    # Python version
    if _sys.version_info >= (3, 11):
        results.append(_check_result(_CheckStatus.PASS, "Python version",
                                     f"{_sys.version_info.major}.{_sys.version_info.minor} (>= 3.11 required)"))
    else:
        results.append(_check_result(_CheckStatus.FAIL, "Python version",
                                     f"{_sys.version_info.major}.{_sys.version_info.minor} — CareerOS needs Python 3.11+"))

    # .careeros/ scaffolding
    if not cfg.careeros_dir.exists():
        results.append(_check_result(_CheckStatus.FAIL, ".careeros/ scaffolding",
                                     "not found — run `careeros init` first"))
        return results  # nothing else is checkable yet
    results.append(_check_result(_CheckStatus.PASS, ".careeros/ scaffolding", str(cfg.careeros_dir)))

    # Profile
    if not cfg.profile_path.exists():
        results.append(_check_result(_CheckStatus.FAIL, "Profile (.careeros/profile.yaml)",
                                     "not found — run `/careeros start` or hand-edit the template"))
    else:
        try:
            _load_profile(cfg)
            results.append(_check_result(_CheckStatus.PASS, "Profile (.careeros/profile.yaml)", "present and valid"))
        except Exception as e:
            results.append(_check_result(_CheckStatus.FAIL, "Profile (.careeros/profile.yaml)",
                                         f"invalid — {type(e).__name__}: {e}"))

    # Discovery provider credentials — v1.2: looped over every ENABLED
    # provider (config.providers), not a single cfg.provider check, since
    # several can run at once. Fantastic Jobs and the legacy actor keep
    # their existing rich, battle-tested diagnostics (transport/endpoint/
    # last-error/discovery-limit for the former, token check for the
    # latter) unchanged; every v1.2 provider gets a uniform check via its
    # own `validate()` — the same method `discover` calls before `fetch()`
    # — plus its Apify budget-vs-spend if its capability is "monthly" (see
    # budget.guard_for). No provider is special-cased beyond preserving the
    # two pre-existing diagnostics blocks as-is.
    active = enabled_providers(cfg)

    if "fantastic-jobs" in active:
        transport = cfg.api.get("transport")
        if transport == "direct":
            key_env = cfg.api.get("api_key_env", "FANTASTIC_API_KEY")
            if os.environ.get(key_env):
                results.append(_check_result(_CheckStatus.PASS, "Discovery credentials",
                                             f"transport=direct, {key_env} is set"))
            else:
                results.append(_check_result(_CheckStatus.FAIL, "Discovery credentials",
                                             f"transport=direct but {key_env} is not set"))
        elif transport == "rapidapi":
            key_env = cfg.api.get("rapidapi_key_env", "RAPIDAPI_KEY")
            if os.environ.get(key_env):
                results.append(_check_result(_CheckStatus.PASS, "Discovery credentials",
                                             f"transport=rapidapi, {key_env} is set"))
            else:
                results.append(_check_result(_CheckStatus.FAIL, "Discovery credentials",
                                             f"transport=rapidapi but {key_env} is not set"))
        else:
            results.append(_check_result(_CheckStatus.FAIL, "Discovery credentials",
                                         'api.transport not set — choose "direct" or "rapidapi" in config.yaml'))
        endpoint = cfg.api.get("endpoint", "both")
        results.append(_check_result(_CheckStatus.PASS, "Discovery endpoint", f"endpoint={endpoint}"))

        # Last discovery failure (P2.9) — LOCAL STATE ONLY: read from the file
        # `discover` wrote on its last failed attempt. Never a live API call,
        # so `doctor` never spends quota just by being run.
        last_error = budget.load_last_error(cfg.careeros_dir)
        if last_error:
            results.append(_check_result(_CheckStatus.WARN, "Last discovery run",
                                         f"failed on {last_error.get('date')}: {last_error.get('message')}"))
        else:
            results.append(_check_result(_CheckStatus.PASS, "Last discovery run", "no recorded failures"))

        # Recommended vs configured discovery limit (P2.9) — same formula
        # `careeros config`/`start` already print, surfaced here too so
        # `doctor` is a one-stop diagnostic. Display only; never mutates.
        if cfg.profile_path.exists():
            try:
                num_queries = len(build_query_plan(_load_profile(cfg), cfg.api)) or 1
            except Exception:
                num_queries = 1
            rec = budget.recommend(cfg.api, cfg.goals, num_queries)
            if rec.quota and rec.recommended_per_request is not None:
                plan_note = f"{rec.plan} — assumed default, set api.plan to silence" if rec.plan_is_assumed else rec.plan
                if rec.configured_limit > rec.recommended_per_request:
                    results.append(_check_result(
                        _CheckStatus.WARN, "Discovery limit",
                        f"current={rec.configured_limit}, recommended={rec.recommended_per_request} "
                        f"(plan {plan_note}: {rec.quota} records/wk ÷ {rec.active_days} active days ÷ "
                        f"{num_queries} query tier(s)) — edit api.limit in .careeros/config.yaml, or "
                        "re-run `careeros start`."
                    ))
                else:
                    results.append(_check_result(
                        _CheckStatus.PASS, "Discovery limit",
                        f"current={rec.configured_limit}, recommended={rec.recommended_per_request} — within quota"
                    ))
    if "fantastic-jobs-actor" in active:
        token_env = cfg.apify.get("token_env", "APIFY_TOKEN")
        tokens_env = cfg.apify.get("tokens_env", "APIFY_TOKENS")
        if os.environ.get(tokens_env) or os.environ.get(token_env):
            results.append(_check_result(_CheckStatus.PASS, "Discovery credentials (legacy actor)",
                                         f"{tokens_env} or {token_env} is set"))
        else:
            results.append(_check_result(_CheckStatus.FAIL, "Discovery credentials (legacy actor)",
                                         f"neither {tokens_env} nor {token_env} is set"))

    # Every other v1.2 provider: uniform validate()-based check, no special
    # cases — plus its Apify budget-vs-spend when its capability is
    # "monthly" (never a name check, see budget.guard_for).
    for name in active:
        if name in ("fantastic-jobs", "fantastic-jobs-actor"):
            continue
        provider_cfg = provider_config_block(cfg, name)
        problems = get_provider(name).validate(cfg)
        if problems:
            results.append(_check_result(_CheckStatus.FAIL, f"Discovery credentials ({name})",
                                         "; ".join(problems)))
        else:
            results.append(_check_result(_CheckStatus.PASS, f"Discovery credentials ({name})", "configured"))
        if budget.guard_for(provider_cfg) == "monthly":
            max_budget = provider_cfg.get("max_monthly_budget_usd") or cfg.apify.get("max_monthly_budget_usd")
            state = budget.load_apify_state(cfg.careeros_dir, _today())
            spent = state.get("spend_usd", 0.0)
            results.append(_check_result(_CheckStatus.PASS, f"Apify budget ({name})",
                                         f"${spent:.4f}/${max_budget or 0:.2f} used this month (estimated)"))

    # Sheets
    spreadsheet_id = cfg.sheets.get("spreadsheet_id")
    creds_path = cfg.sheets.get("credentials_path")
    if not spreadsheet_id or not creds_path:
        results.append(_check_result(_CheckStatus.FAIL, "Google Sheets",
                                     "sheets.spreadsheet_id and/or sheets.credentials_path not set in config.yaml "
                                     "— see docs/google-setup.md"))
    elif not Path(creds_path).exists():
        results.append(_check_result(_CheckStatus.FAIL, "Google Sheets",
                                     f"sheets.credentials_path does not exist: {creds_path}"))
    else:
        results.append(_check_result(_CheckStatus.PASS, "Google Sheets",
                                     f"spreadsheet_id set, credentials file found"))

    # Drive (optional — only checked if enabled)
    if cfg.drive.get("enabled"):
        client_secret_path = cfg.drive.get("client_secret_path")
        root_folder_id = cfg.drive.get("root_folder_id")
        if not client_secret_path or not Path(client_secret_path).exists():
            results.append(_check_result(_CheckStatus.FAIL, "Google Drive (enabled)",
                                         f"client_secret_path missing or not found: {client_secret_path}"))
        elif not root_folder_id:
            results.append(_check_result(_CheckStatus.FAIL, "Google Drive (enabled)",
                                         "drive.root_folder_id not set in config.yaml"))
        else:
            try:
                import google_auth_oauthlib  # noqa: F401
                results.append(_check_result(_CheckStatus.PASS, "Google Drive (enabled)",
                                             "credentials configured, [drive] extra installed"))
            except ImportError:
                results.append(_check_result(_CheckStatus.FAIL, "Google Drive (enabled)",
                                             'credentials configured but [drive] extra not installed — '
                                             'run: pip install -e ".[drive]"'))
    else:
        results.append(_check_result(_CheckStatus.WARN, "Google Drive", "disabled (drive.enabled: false) — optional"))

    # Playwright (optional — the `apply` stage's fallback tier for JS-gated
    # forms; the primary HTTP tier works without it). Two independent things
    # can be missing: the `careeros[apply]` extra's Python package, and the
    # `chromium` browser BINARY it still needs (`playwright install
    # chromium`) — pip alone does not install the binary. Distinguishing
    # them here is exactly what makes "⚙️ Playwright Missing" in the apply
    # stage's status column actionable rather than a dead end.
    try:
        import playwright.sync_api  # noqa: F401
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                browser.close()
            results.append(_check_result(_CheckStatus.PASS, "Playwright (apply fallback)",
                                         "[apply] extra installed, chromium browser available"))
        except Exception:
            results.append(_check_result(
                _CheckStatus.WARN, "Playwright (apply fallback)",
                "[apply] extra installed but the chromium browser binary is missing — "
                "run: playwright install chromium"
            ))
    except ImportError:
        results.append(_check_result(
            _CheckStatus.WARN, "Playwright (apply fallback)",
            'not installed — optional, only needed for JS-gated application forms; '
            'run: pip install -e ".[apply]" && playwright install chromium'
        ))

    return results


@app.command()
def doctor():
    """First-run checklist: Python version, profile, discovery credentials,
    Sheets, and (if enabled) Drive. Checks only — never modifies anything.
    Exits non-zero if any check FAILs, so it's safe to gate a first `daily`
    run on `careeros doctor && careeros daily`-style scripting."""
    cfg = _config()
    results = _run_doctor_checks(cfg)

    icon = {_CheckStatus.PASS: "✓", _CheckStatus.WARN: "!", _CheckStatus.FAIL: "✗"}
    for status, label, detail in results:
        typer.echo(f"[{icon[status]}] {label:32} {detail}")

    fails = [r for r in results if r[0] == _CheckStatus.FAIL]
    typer.echo("")
    if fails:
        typer.echo(f"{len(fails)} check(s) failed — fix the items marked [✗] above before running `daily`.")
        raise typer.Exit(1)
    typer.echo("All checks passed. You're ready to run `/careeros daily`.")


# ── discover ──────────────────────────────────────────────────────────────

def _discover_one_provider(
    cfg: Config, name: str, *, date: str, limit: Optional[int], search: str, ignore_budget: bool,
) -> ProviderResult:
    """Run exactly one provider's fetch, enforcing whichever budget/quota
    CAPABILITY its own config declares (see `budget.guard_for` — never a
    branch on `name`). Returns a `ProviderResult`; a provider that's enabled
    but can't run this call (failed `validate()`, or its guard says stop)
    comes back with `skipped=True` rather than raising, so `discover`'s loop
    can record it and move on to the rest.

    Only the "weekly" capability (Fantastic Jobs' own `api:` block) gets the
    full segmented per-work-mode query plan (`pipeline/queryplan.py`) — that
    plan is shaped around that provider's rich title/location/work-arrangement
    filters. Every other provider gets exactly ONE fetch call: issuing N
    near-identical calls to a provider that doesn't understand the segmented
    spec would just waste real money for zero benefit (most of the v1.2
    additions are paid Apify actors)."""
    p = get_provider(name)
    provider_cfg = _provider_query_cfg(cfg, name)

    problems = p.validate(cfg)
    if problems:
        msg = "; ".join(problems)
        typer.echo(f"[discover] {name}: skipped — {msg}")
        return ProviderResult.skip(name, msg)

    capability = budget.guard_for(provider_cfg)

    if capability == "weekly":
        if search or not cfg.profile_path.exists():
            queries: list[Optional[dict]] = [None]
        else:
            queries = build_query_plan(_load_profile(cfg), provider_cfg) or [None]

        explicit_limit = provider_cfg.get("limit")
        has_explicit_limit = limit is not None or (isinstance(explicit_limit, int) and explicit_limit > 0)
        base_limit = limit if limit is not None else (explicit_limit or 100)

        # "both" SPLITS base_limit across the 2 endpoints, so records/tier
        # stays = base_limit regardless of endpoint count — reason the record
        # budget in query TIERS; the HTTP call count (tiers x endpoints) is
        # tracked separately for the informational request counter.
        num_endpoints = 2 if provider_cfg.get("endpoint", "both") == "both" else 1
        rec = budget.recommend(provider_cfg, cfg.goals, len(queries), cli_default_limit=base_limit)
        if not has_explicit_limit and rec.recommended_per_request is not None:
            base_limit = rec.recommended_per_request
            rec = budget.recommend(provider_cfg, cfg.goals, len(queries), cli_default_limit=base_limit)
        http_requests = len(queries) * num_endpoints
        for line in rec.lines():
            typer.echo(f"[discover] {name}: {line}")
        quota = budget.weekly_quota(provider_cfg)
        weekly_state = budget.load_state(cfg.careeros_dir, date)
        ok, msg = budget.check_before_run(weekly_state, quota)
        if msg:
            typer.echo(f"[discover] {name}: {msg}")
        if not ok and not ignore_budget:
            typer.echo(f"[discover] {name}: skipped to protect your weekly quota. Re-run with --ignore-budget to override.")
            return ProviderResult.skip(name, "weekly record quota exhausted")

        items: list = []
        cost = 0.0
        start = time.time()
        for i, query in enumerate(queries):
            work_mode = (query or {}).get("_work_mode", "single")
            effective_limit = resolve_tier_limit(work_mode, provider_cfg, base_limit)
            result = p.fetch(cfg, limit=effective_limit, search=search, query=query)
            cost += result.cost_usd
            typer.echo(
                f"  [discover] {name} query {i + 1}/{len(queries)} ({work_mode}, "
                f"limit={effective_limit}): {len(result.items)} items (${result.cost_usd:.4f})"
            )
            items.extend(result.items)
        # The API was consumed regardless of --dry-run, so record it against
        # the rolling weekly budget before anything else can early-return.
        budget.record_consumption(weekly_state, records=len(items), requests=http_requests)
        budget.save_state(cfg.careeros_dir, weekly_state)
        return ProviderResult(
            provider=name, items=items, cost_usd=cost,
            requests=http_requests, records=len(items), seconds=time.time() - start,
        )

    if capability == "monthly":
        max_budget = provider_cfg.get("max_monthly_budget_usd") or cfg.apify.get("max_monthly_budget_usd")
        apify_state = budget.load_apify_state(cfg.careeros_dir, date)
        ok, msg = budget.check_apify_budget(apify_state, max_budget)
        if msg:
            typer.echo(f"[discover] {name}: {msg}")
        if not ok and not ignore_budget:
            return ProviderResult.skip(name, "monthly Apify budget exhausted")

        effective_limit = limit if limit is not None else (provider_cfg.get("limit") or 100)
        try:
            result = p.fetch(cfg, limit=effective_limit, search=search, query=None)
        except ProviderError as e:
            # A HARD failure from the actor/account itself (e.g. every
            # rotated Apify token exhausted or out of balance) — distinct
            # from our own soft max_monthly_budget_usd guard above. Tell the
            # user clearly and skip just THIS provider, mirroring the
            # weekly-quota guard's "tell and move on" behavior, rather than
            # aborting the whole multi-provider discover run.
            typer.echo(f"[discover] {name}: skipped — {e}")
            return ProviderResult.skip(name, f"Apify usage/quota exhausted: {e}")
        typer.echo(
            f"[discover] {name}: {len(result.items)} items (${result.cost_usd:.4f}, {result.seconds:.1f}s)"
        )
        budget.record_apify_spend(apify_state, result.cost_usd)
        budget.save_apify_state(cfg.careeros_dir, apify_state)
        return result

    # capability == "none": unmetered, no guard (RemoteOK, We Work Remotely).
    effective_limit = limit if limit is not None else (provider_cfg.get("limit") or 100)
    result = p.fetch(cfg, limit=effective_limit, search=search, query=None)
    typer.echo(f"[discover] {name}: {len(result.items)} items ({result.seconds:.1f}s)")
    return result


@app.command()
def discover(
    provider: Optional[str] = typer.Option(
        None, help="Force exactly ONE provider id, ignoring config.providers — the manual "
                    "dry-run/trial workflow (providers/README.md) before enabling a source for real"),
    date: str = typer.Option(None, help="Run date, default today"),
    limit: Optional[int] = typer.Option(
        None, help="Per-query max records; default from each provider's own configured limit, "
                    "else 100. Overridden per-tier by tier_limits (Fantastic Jobs only)"),
    search: str = typer.Option(
        "", help="Manual single-query override — bypasses profile-driven segmentation"),
    dry_run: bool = typer.Option(False, help="Fetch and print, don't write raw.json"),
    ignore_budget: bool = typer.Option(
        False, "--ignore-budget", help="Bypass every provider's budget/quota guard for this run"),
):
    """[dev] Discover: run every enabled provider (config.providers, IN
    CONFIG ORDER — dedupe keeps the FIRST occurrence of a duplicate role, so
    list your primary/most-trusted source first), write 01_discover/raw.json.

    v1.2: multiple providers run side by side in one call — see
    config.providers. `--provider NAME` forces exactly one, ignoring
    config.providers entirely (the manual trial workflow).

    Budget/quota enforcement is CAPABILITY-driven, never by provider identity
    (see `budget.guard_for` / `_discover_one_provider`): a provider whose own
    config declares a weekly record quota (Fantastic Jobs) gets that guard
    (unchanged — the segmented per-work-mode query plan, P2.8's quota-aware
    default limit, everything); one declaring a monthly USD budget (every
    Apify-actor provider) gets the rolling-month soft guard; an unmetered
    free provider (RemoteOK, We Work Remotely) gets none. A provider that's
    ENABLED but can't run this call (failed `validate()`, or its guard says
    stop) is recorded as `skipped` with a reason — never silently dropped —
    and the run continues with whatever else is enabled."""
    cfg = _config()
    date = date or _today()

    if provider:
        provider_names = [provider]
    else:
        provider_names = enabled_providers(cfg)
        if not provider_names:
            typer.echo(
                "[discover] No providers enabled in config.providers — nothing to do. "
                "Set at least one `enabled: true` in .careeros/config.yaml.", err=True
            )
            raise typer.Exit(1)

    results: list[ProviderResult] = []
    start_all = time.time()
    try:
        for name in provider_names:
            results.append(_discover_one_provider(
                cfg, name, date=date, limit=limit, search=search, ignore_budget=ignore_budget,
            ))
    except ProviderError as e:
        # P2.9: persist the classified failure so `careeros doctor` can show
        # it later without a live API call (see budget.record_last_error).
        budget.record_last_error(cfg.careeros_dir, date, str(e))
        typer.echo(f"[discover] {e}", err=True)
        raise typer.Exit(1)
    budget.clear_last_error(cfg.careeros_dir)
    elapsed_all = time.time() - start_all

    total_items = sum(len(r.items) for r in results)
    total_cost = sum(r.cost_usd for r in results)
    typer.echo(
        f"[discover] {len(provider_names)} provider(s), {total_items} raw items total "
        f"(${total_cost:.4f}, {elapsed_all:.1f}s)"
    )

    if dry_run:
        typer.echo(dumps({r.provider: r.items[:3] for r in results}))
        return

    stage_path = runmeta.stage_dir(cfg.runs_dir, date, "discover")
    with open(stage_path / "raw.json", "w") as f:
        f.write(dumps({
            "providers": provider_names,
            "items": {r.provider: r.items for r in results},
            "meta": {
                r.provider: {
                    "cost_usd": r.cost_usd, "requests": r.requests, "records": r.records,
                    "seconds": round(r.seconds, 2), "warnings": r.warnings, "errors": r.errors,
                    "skipped": r.skipped, "skip_reason": r.skip_reason,
                }
                for r in results
            },
        }))

    runmeta.record_stage(cfg.runs_dir, date, "discover",
                          count_in=0, count_out=total_items, seconds=elapsed_all,
                          apify_cost_usd=total_cost)


# ── normalize ─────────────────────────────────────────────────────────────

@app.command()
def normalize(date: str = typer.Option(None, help="Run date, default today")):
    """[dev] Normalize: 01_discover/raw.json -> 02_normalize/jobs.json.

    v1.2: raw.json holds one item-list PER provider that ran (see
    `discover`) — this maps each provider's items with ITS OWN
    `to_job_dict`, then concatenates every provider's jobs into ONE flat
    list, in the same order `discover` ran them. Every stage from here on
    (dedupe onward) reads that flat list and has no idea how many providers
    contributed to it — that's what keeps the rest of the pipeline
    completely provider-agnostic."""
    cfg = _config()
    date = date or _today()

    raw_path = runmeta.stage_dir(cfg.runs_dir, date, "discover") / "raw.json"
    if not raw_path.exists():
        typer.echo(f"No {raw_path} — run `careeros discover` first.", err=True)
        raise typer.Exit(1)

    import json
    with open(raw_path) as f:
        raw = json.load(f)

    start = time.time()
    jobs: list[Job] = []
    total_raw = 0
    for provider_name in raw.get("providers", []):
        raw_items = raw.get("items", {}).get(provider_name, [])
        total_raw += len(raw_items)
        if not raw_items:
            continue
        p = get_provider(provider_name)
        jobs.extend(normalize_all(raw_items, p, source=provider_name,
                                   description_max_chars=cfg.description_max_chars))
    elapsed = time.time() - start

    out_path = runmeta.stage_dir(cfg.runs_dir, date, "normalize") / "jobs.json"
    with open(out_path, "w") as f:
        f.write(dumps([j.to_dict() for j in jobs]))

    typer.echo(f"[normalize] {total_raw} raw -> {len(jobs)} jobs ({elapsed:.1f}s)")
    runmeta.record_stage(cfg.runs_dir, date, "normalize",
                          count_in=total_raw, count_out=len(jobs), seconds=elapsed)


# ── dedupe ────────────────────────────────────────────────────────────────

@app.command()
def dedupe(
    date: str = typer.Option(None, help="Run date, default today"),
    against_sheet: bool = typer.Option(True, help="Also dedupe against the Sheet's existing Job IDs"),
):
    """[dev] Dedupe: in-run + cross-location + vs history (+ vs Sheet) ->
    03_dedupe/{unique,dropped}.json."""
    cfg = _config()
    date = date or _today()

    jobs_path = runmeta.stage_dir(cfg.runs_dir, date, "normalize") / "jobs.json"
    if not jobs_path.exists():
        typer.echo(f"No {jobs_path} — run `careeros normalize` first.", err=True)
        raise typer.Exit(1)

    import json
    with open(jobs_path) as f:
        jobs = [Job.from_dict(d) for d in json.load(f)]

    start = time.time()
    unique, dropped_in_run = dedupe_in_run(jobs)
    unique, dropped_cross_location = dedupe_cross_location(unique)

    seen_path = cfg.careeros_dir / "seen.jsonl"
    unique, dropped_history = dedupe_against_history(unique, seen_path)

    dropped_sheet: list[Job] = []
    if against_sheet:
        try:
            sheet_ids = sheets_mod.read_existing_job_ids(cfg)
            unique, dropped_sheet = dedupe_against_sheet_ids(unique, sheet_ids)
        except RuntimeError as e:
            typer.echo(f"[dedupe] Sheets dedupe skipped: {e}")

    elapsed = time.time() - start
    all_dropped = dropped_in_run + dropped_cross_location + dropped_history + dropped_sheet

    stage_path = runmeta.stage_dir(cfg.runs_dir, date, "dedupe")
    with open(stage_path / "unique.json", "w") as f:
        f.write(dumps([j.to_dict() for j in unique]))
    with open(stage_path / "dropped.json", "w") as f:
        f.write(dumps([j.to_dict() for j in all_dropped]))

    typer.echo(f"[dedupe] {len(jobs)} in -> {len(unique)} unique, {len(all_dropped)} dropped "
               f"(in-run: {len(dropped_in_run)}, cross-location: {len(dropped_cross_location)}, "
               f"history: {len(dropped_history)}, sheet: {len(dropped_sheet)}) ({elapsed:.1f}s)")
    runmeta.record_stage(cfg.runs_dir, date, "dedupe",
                          count_in=len(jobs), count_out=len(unique), seconds=elapsed)


# ── constraints (deterministic hard deal-breakers) ───────────────────────

@app.command()
def constraints(date: str = typer.Option(None, help="Run date, default today")):
    """[dev] Constraints: apply hard deal-breakers (location, salary) to
    03_dedupe/unique.json -> 04_constraints/{eligible,rejected}.json.
    Rejected jobs never reach the AI gate, so no tokens are spent on them."""
    cfg = _config()
    date = date or _today()

    import json
    unique_path = runmeta.stage_dir(cfg.runs_dir, date, "dedupe") / "unique.json"
    if not unique_path.exists():
        typer.echo(f"No {unique_path} — run `careeros dedupe` first.", err=True)
        raise typer.Exit(1)
    with open(unique_path) as f:
        jobs = [Job.from_dict(d) for d in json.load(f)]

    profile = _load_profile(cfg)
    start = time.time()
    eligible: list[dict] = []
    rejected: list[dict] = []
    for job in jobs:
        result = evaluate_constraints(job, profile, cfg.fx_rates)
        if result.passed:
            eligible.append(job.to_dict())
        else:
            rejected.append({**job.to_dict(), "_reject_reasons": result.reasons})
    elapsed = time.time() - start

    stage_dir = runmeta.stage_dir(cfg.runs_dir, date, "constraints")
    with open(stage_dir / "eligible.json", "w") as f:
        f.write(dumps(eligible))
    with open(stage_dir / "rejected.json", "w") as f:
        f.write(dumps(rejected))

    typer.echo(f"[constraints] {len(jobs)} in -> {len(eligible)} eligible, "
               f"{len(rejected)} hard-rejected ({elapsed:.2f}s)")
    runmeta.record_stage(cfg.runs_dir, date, "constraints",
                          count_in=len(jobs), count_out=len(eligible), seconds=elapsed)


# ── gate (AI stage: prepare / finalize) ──────────────────────────────────

@app.command()
def gate(
    date: str = typer.Option(None, help="Run date, default today"),
    prepare: bool = typer.Option(False, "--prepare", help="Write gate input + print agent instructions"),
    finalize: bool = typer.Option(False, "--finalize", help="Validate agent-written gated.json"),
):
    """[dev] AI Gate: cheap batched keep/drop triage. See prompts/gate_v1.md."""
    cfg = _config()
    date = date or _today()

    if prepare:
        _gate_prepare(cfg, date)
    elif finalize:
        _gate_finalize(cfg, date)
    else:
        typer.echo("Pass --prepare or --finalize.", err=True)
        raise typer.Exit(1)


def _gate_prepare(cfg: Config, date: str) -> None:
    import json
    eligible_path = runmeta.stage_dir(cfg.runs_dir, date, "constraints") / "eligible.json"
    if not eligible_path.exists():
        typer.echo(f"No {eligible_path} — run `careeros constraints` first.", err=True)
        raise typer.Exit(1)
    with open(eligible_path) as f:
        jobs = json.load(f)

    stage_dir = runmeta.stage_dir(cfg.runs_dir, date, "gate")
    batch_size = cfg.gate_batch_size
    batches = [jobs[i:i + batch_size] for i in range(0, len(jobs), batch_size)]
    input_paths = []
    for i, batch in enumerate(batches):
        input_path = stage_dir / f"_input_{i}.json"
        with open(input_path, "w") as f:
            f.write(dumps(batch))
        input_paths.append(input_path)

    estimated_tokens = runmeta.estimate_tokens(*input_paths)
    runmeta.write_stage_meta(cfg.runs_dir, date, "gate", {
        "prepared_at": time.time(), "estimated_tokens": estimated_tokens,
    })

    prompt_path = cfg.prompt_path("gate")
    typer.echo(
        f"[gate:prepare] {len(jobs)} jobs -> {len(batches)} batch(es) of up to {batch_size}.\n\n"
        f"AGENT INSTRUCTIONS:\n"
        f"Read {prompt_path} and .careeros/profile.yaml.\n"
        f"For each 05_gate/_input_N.json batch, write 05_gate/_output_N.json:\n"
        f'  {{"results": [{{"id","keep","reason","confidence"}}, ...]}}\n'
        f"One result per job in that batch. Then run:\n"
        f"  careeros gate --finalize --date {date}"
    )


def _gate_finalize(cfg: Config, date: str) -> None:
    import json
    stage_dir = runmeta.stage_dir(cfg.runs_dir, date, "gate")
    output_files = sorted(stage_dir.glob("_output_*.json"))
    if not output_files:
        typer.echo(f"No _output_*.json found in {stage_dir} — agent hasn't written gate results yet.", err=True)
        raise typer.Exit(1)

    all_results = []
    for path in output_files:
        with open(path) as f:
            data = json.load(f)
        all_results.extend(data.get("results", []))

    errors = []
    for r in all_results:
        for field in ("id", "keep", "reason", "confidence"):
            if field not in r:
                errors.append(f"{r.get('id', '?')}: missing field '{field}'")

    if errors:
        typer.echo("[gate:finalize] Validation FAILED:\n" + "\n".join(errors), err=True)
        typer.echo(f"\nAgent: fix the listed items in their _output_N.json file, "
                    f"then re-run `careeros gate --finalize --date {date}`.")
        raise typer.Exit(1)

    input_files = sorted(stage_dir.glob("_input_*.json"))
    total_in = sum(len(json.load(open(p))) for p in input_files)
    kept = [r for r in all_results if r["keep"]]

    with open(stage_dir / "gated.json", "w") as f:
        f.write(dumps(all_results))

    meta = runmeta.read_stage_meta(cfg.runs_dir, date, "gate")
    elapsed = time.time() - meta["prepared_at"] if "prepared_at" in meta else 0.0

    typer.echo(f"[gate:finalize] {total_in} in -> {len(kept)} kept, {total_in - len(kept)} dropped.")
    runmeta.record_stage(cfg.runs_dir, date, "gate", count_in=total_in, count_out=len(kept),
                          seconds=elapsed, prompt_version=cfg.prompts.get("gate"),
                          estimated_tokens=meta.get("estimated_tokens", 0))


# ── evaluate (AI stage: prepare / finalize, cache-checked) ──────────────

@app.command()
def evaluate(
    date: str = typer.Option(None, help="Run date, default today"),
    prepare: bool = typer.Option(False, "--prepare"),
    finalize: bool = typer.Option(False, "--finalize"),
):
    """[dev] Final Evaluation: score against the profile, cache-checked.
    Writes 06_evaluate/<job-id>.json — the source of truth every later
    artifact reads. See prompts/eval_v2.md."""
    cfg = _config()
    date = date or _today()

    if prepare:
        _evaluate_prepare(cfg, date)
    elif finalize:
        _evaluate_finalize(cfg, date)
    else:
        typer.echo("Pass --prepare or --finalize.", err=True)
        raise typer.Exit(1)


def _load_profile(cfg: Config) -> Profile:
    with open(cfg.profile_path) as f:
        return Profile.from_dict(yaml.safe_load(f))


def _evaluate_prepare(cfg: Config, date: str) -> None:
    import json
    gate_path = runmeta.stage_dir(cfg.runs_dir, date, "gate") / "gated.json"
    eligible_path = runmeta.stage_dir(cfg.runs_dir, date, "constraints") / "eligible.json"
    if not gate_path.exists() or not eligible_path.exists():
        typer.echo("Missing gate/constraints output — run those stages first.", err=True)
        raise typer.Exit(1)

    with open(gate_path) as f:
        gated = {r["id"]: r for r in json.load(f)}
    with open(eligible_path) as f:
        jobs_by_id = {j["id"]: j for j in json.load(f)}

    kept_ids = [jid for jid, r in gated.items() if r["keep"]]
    profile = _load_profile(cfg)
    prompt_version = cfg.prompts.get("eval", "v1")
    cache = Cache(cfg.cache_dir)

    to_evaluate = []
    cache_hits = 0
    stage_dir = runmeta.stage_dir(cfg.runs_dir, date, "evaluate")
    for job_id in kept_ids:
        job = jobs_by_id[job_id]
        job_hash = Job.from_dict(job).content_hash()
        key = eval_cache_key(job_hash, profile.version, prompt_version)
        cached = cache.get("evaluate", key)
        if cached:
            # The cache key is content-based (job_hash excludes `source`), so a
            # cache hit can carry a STALE `id` from whenever this content was
            # first evaluated under a different Job.id (e.g. before the P2.7
            # actor->REST provider migration, since `source` feeds Job.id but
            # not content_hash). Eval.id must be today's actual job_id or every
            # downstream stage (threshold/artifacts/drive/sheets/summary) fails
            # to find the matching Job — found live 2026-07-10 on a real cache
            # hit (Motive PM) that silently displaced today's own evaluation.
            with open(stage_dir / f"{job_id}.json", "w") as f:
                f.write(dumps({**cached, "id": job_id}))
            cache_hits += 1
        else:
            to_evaluate.append({"job": job, "job_hash": job_hash})

    input_path = stage_dir / "_input.json"
    if to_evaluate:
        with open(input_path, "w") as f:
            f.write(dumps(to_evaluate))

    # eval_v2.md reads the FULL profile.yaml (unlike gate's headline-only
    # subset), so it's counted once per prepare call alongside the job batch.
    estimated_tokens = (
        runmeta.estimate_tokens(input_path, cfg.profile_path) if to_evaluate else 0
    )
    runmeta.write_stage_meta(cfg.runs_dir, date, "evaluate", {
        "prepared_at": time.time(), "cache_hits": cache_hits, "cache_misses": len(to_evaluate),
        "estimated_tokens": estimated_tokens,
    })

    prompt_path = cfg.prompt_path("eval")
    typer.echo(
        f"[evaluate:prepare] {len(kept_ids)} gated jobs: {cache_hits} cache hits (written directly), "
        f"{len(to_evaluate)} need evaluation.\n\n"
        + (
            f"AGENT INSTRUCTIONS:\n"
            f"Read {prompt_path} and .careeros/profile.yaml.\n"
            f"For each entry in 06_evaluate/_input.json, write 06_evaluate/<id>.json\n"
            f"matching schemas/eval.schema.json (set job_hash from the input entry,\n"
            f"profile_version={profile.version}, prompt_version=\"{prompt_version}\").\n"
            f"Then run:\n  careeros evaluate --finalize --date {date}"
            if to_evaluate else "Nothing to do — run `careeros evaluate --finalize` to finalize."
        )
    )


def _evaluate_finalize(cfg: Config, date: str) -> None:
    import json
    stage_dir = runmeta.stage_dir(cfg.runs_dir, date, "evaluate")
    input_path = stage_dir / "_input.json"
    expected_ids = set()
    if input_path.exists():
        with open(input_path) as f:
            expected_ids = {e["job"]["id"] for e in json.load(f)}

    all_records = []
    missing = []
    for job_id in expected_ids:
        out_path = stage_dir / f"{job_id}.json"
        if not out_path.exists():
            missing.append(job_id)
            continue
        with open(out_path) as f:
            all_records.append(json.load(f))

    # Also fold in cache-hit files already written during --prepare, so the
    # finalize summary reflects the FULL evaluated set for this run, not just
    # the freshly-generated ones.
    for path in stage_dir.glob("*.json"):
        if path.name in ("_input.json",):
            continue
        job_id = path.stem
        if job_id not in expected_ids:
            with open(path) as f:
                all_records.append(json.load(f))

    if missing:
        typer.echo(f"[evaluate:finalize] Missing output for: {', '.join(missing)}", err=True)
        typer.echo("Agent: write the missing files, then re-run --finalize.")
        raise typer.Exit(1)

    errors = runmeta.validate_stage("eval", all_records)
    if errors:
        typer.echo("[evaluate:finalize] Schema validation FAILED:\n" + "\n".join(errors), err=True)
        raise typer.Exit(1)

    profile = _load_profile(cfg)
    prompt_version = cfg.prompts.get("eval", "v1")
    cache = Cache(cfg.cache_dir)
    for record in all_records:
        key = eval_cache_key(record["job_hash"], profile.version, prompt_version)
        cache.put("evaluate", key, record)

    meta = runmeta.read_stage_meta(cfg.runs_dir, date, "evaluate")
    elapsed = time.time() - meta["prepared_at"] if "prepared_at" in meta else 0.0

    typer.echo(f"[evaluate:finalize] {len(all_records)} evaluations valid and cached.")
    runmeta.record_stage(cfg.runs_dir, date, "evaluate",
                          count_in=len(expected_ids), count_out=len(all_records),
                          seconds=elapsed, prompt_version=prompt_version,
                          cache_hits=meta.get("cache_hits", 0), cache_misses=meta.get("cache_misses", 0),
                          estimated_tokens=meta.get("estimated_tokens", 0))


# ── threshold ─────────────────────────────────────────────────────────────

@app.command()
def threshold(
    date: str = typer.Option(None, help="Run date, default today"),
    min_score: Optional[float] = typer.Option(None, help="Override config.threshold (APPLY tier)"),
    consider_min: Optional[float] = typer.Option(None, help="Override config.consider_threshold (CONSIDER tier)"),
):
    """[dev] Two-tier threshold. APPLY: score >= threshold, recommendation
    "apply", passing hard constraints -> full pipeline. CONSIDER:
    consider_threshold <= score < threshold (constraints pass) -> Sheet row
    only, no artifacts/Drive. Below consider_threshold -> omitted. See
    careeros/pipeline/threshold.py:partition_evals."""
    cfg = _config()
    date = date or _today()
    min_score = min_score if min_score is not None else cfg.threshold
    consider_min = consider_min if consider_min is not None else cfg.consider_threshold
    start = time.time()

    import json
    eval_dir = runmeta.stage_dir(cfg.runs_dir, date, "evaluate")
    evals = []
    for path in eval_dir.glob("*.json"):
        if path.name.startswith("_"):
            continue
        with open(path) as f:
            evals.append(Eval.from_dict(json.load(f)))

    # Every evaluated job already passed `constraints`, but re-checking here
    # (via partition_evals) is the deterministic backstop against the AI
    # mislabeling a hard-reject as "apply" — see careeros/pipeline/threshold.py.
    jobs_path = runmeta.stage_dir(cfg.runs_dir, date, "normalize") / "jobs.json"
    with open(jobs_path) as f:
        jobs_by_id = {j["id"]: Job.from_dict(j) for j in json.load(f)}

    profile = _load_profile(cfg)
    apply_, consider_, _omit = partition_evals(
        evals, min_score, consider_min, jobs_by_id, profile, cfg.fx_rates)

    stage_dir = runmeta.stage_dir(cfg.runs_dir, date, "select")
    with open(stage_dir / "selected.json", "w") as f:
        f.write(dumps([e.to_dict() for e in apply_]))
    with open(stage_dir / "consider.json", "w") as f:
        f.write(dumps([e.to_dict() for e in consider_]))

    typer.echo(
        f"[threshold] {len(evals)} evaluated -> {len(apply_)} APPLY (>= {min_score}), "
        f"{len(consider_)} CONSIDER ([{consider_min}, {min_score})) "
        f"(top: {apply_[0].score if apply_ else 'n/a'})"
    )
    runmeta.record_stage(cfg.runs_dir, date, "select",
                          count_in=len(evals), count_out=len(apply_),
                          seconds=time.time() - start)


# ── artifacts (AI stage: prepare / finalize, cache-checked) ──────────────

@app.command()
def artifacts(
    date: str = typer.Option(None, help="Run date, default today"),
    prepare: bool = typer.Option(False, "--prepare"),
    finalize: bool = typer.Option(False, "--finalize"),
):
    """[dev] Resume + cover letter generation for selected jobs, cache-checked
    via artifact_cache_key. `--finalize` blocks caching on a lint or
    verify-resume failure — see careeros/lint.py."""
    cfg = _config()
    date = date or _today()

    if prepare:
        _artifacts_prepare(cfg, date)
    elif finalize:
        _artifacts_finalize(cfg, date)
    else:
        typer.echo("Pass --prepare or --finalize.", err=True)
        raise typer.Exit(1)


def _artifacts_prepare(cfg: Config, date: str) -> None:
    import json
    selected_path = runmeta.stage_dir(cfg.runs_dir, date, "select") / "selected.json"
    jobs_path = runmeta.stage_dir(cfg.runs_dir, date, "normalize") / "jobs.json"
    if not selected_path.exists() or not jobs_path.exists():
        typer.echo("Missing select/normalize output — run those stages first.", err=True)
        raise typer.Exit(1)

    with open(selected_path) as f:
        evals = [Eval.from_dict(d) for d in json.load(f)]
    with open(jobs_path) as f:
        jobs_by_id = {j["id"]: Job.from_dict(j) for j in json.load(f)}

    profile = _load_profile(cfg)
    resume_prompt_version = cfg.prompts.get("resume", "v1")
    cover_prompt_version = cfg.prompts.get("cover", "v1")
    cache = Cache(cfg.cache_dir)

    to_generate: list[dict] = []
    cache_hits = 0
    for e in evals:
        job = jobs_by_id[e.id]
        job_hash = job.content_hash()
        artifacts_path = runmeta.artifacts_dir(cfg.runs_dir, date, e.id)

        needs_resume = True
        needs_cover = True

        resume_key = artifact_cache_key(job_hash, profile.version, e.score, resume_prompt_version)
        cached_resume = cache.get("resume", resume_key)
        if cached_resume:
            with open(artifacts_path / "resume.md", "w") as f:
                f.write(cached_resume["content"])
            needs_resume = False
            cache_hits += 1

        cover_key = artifact_cache_key(job_hash, profile.version, e.score, cover_prompt_version)
        cached_cover = cache.get("cover", cover_key)
        if cached_cover:
            with open(artifacts_path / "cover.md", "w") as f:
                f.write(cached_cover["content"])
            needs_cover = False
            cache_hits += 1

        if needs_resume or needs_cover:
            to_generate.append({
                "id": e.id, "company": job.company, "title": job.title,
                "needs_resume": needs_resume, "needs_cover": needs_cover,
                "artifacts_path": str(artifacts_path),
            })

    # Each resume/cover generation independently reads the full profile.yaml
    # (per prompts/resume_v1.md, prompts/cover_v1.md) plus the job's own
    # description — so the estimate multiplies profile size by the number of
    # generation tasks, not just by job count.
    profile_bytes = cfg.profile_path.stat().st_size if cfg.profile_path.exists() else 0
    generation_tasks = sum(
        int(item["needs_resume"]) + int(item["needs_cover"]) for item in to_generate
    )
    job_desc_bytes = sum(
        len((jobs_by_id[item["id"]].description or "").encode("utf-8")) for item in to_generate
    )
    estimated_tokens = (profile_bytes * generation_tasks + job_desc_bytes) // 4

    runmeta.write_stage_meta(cfg.runs_dir, date, "artifacts", {
        "prepared_at": time.time(),
        "cache_hits": cache_hits,
        "cache_misses": len(to_generate),
        "estimated_tokens": estimated_tokens,
    })

    typer.echo(
        f"[artifacts:prepare] {len(evals)} selected: {cache_hits} cache hits (written directly), "
        f"{len(to_generate)} job(s) need generation.\n"
    )
    if to_generate:
        typer.echo(
            "AGENT INSTRUCTIONS:\n"
            f"Read {cfg.prompt_path('resume')} and {cfg.prompt_path('cover')} plus .careeros/profile.yaml.\n"
            "For each job below needing resume/cover, write the file(s) to its artifacts_path,\n"
            "following the selector-not-writer rule. Run `careeros verify-resume` + `careeros lint`\n"
            "on each resume, and `careeros lint` on each cover, before moving to the next job.\n"
            "Then run:\n"
            f"  careeros artifacts --finalize --date {date}\n\n"
            + dumps(to_generate)
        )
    else:
        typer.echo(f"Nothing to generate — run `careeros artifacts --finalize --date {date}` to finalize.")


def _artifacts_finalize(cfg: Config, date: str) -> None:
    import json
    selected_path = runmeta.stage_dir(cfg.runs_dir, date, "select") / "selected.json"
    jobs_path = runmeta.stage_dir(cfg.runs_dir, date, "normalize") / "jobs.json"
    with open(selected_path) as f:
        evals = [Eval.from_dict(d) for d in json.load(f)]
    with open(jobs_path) as f:
        jobs_by_id = {j["id"]: Job.from_dict(j) for j in json.load(f)}

    profile = _load_profile(cfg)
    resume_prompt_version = cfg.prompts.get("resume", "v1")
    cover_prompt_version = cfg.prompts.get("cover", "v1")
    cache = Cache(cfg.cache_dir)

    errors: list[str] = []
    newly_cached = 0
    artifact_count = 0

    for e in evals:
        job = jobs_by_id[e.id]
        job_hash = job.content_hash()
        artifacts_path = runmeta.artifacts_dir(cfg.runs_dir, date, e.id)
        resume_path = artifacts_path / "resume.md"
        cover_path = artifacts_path / "cover.md"

        resume_key = artifact_cache_key(job_hash, profile.version, e.score, resume_prompt_version)
        cover_key = artifact_cache_key(job_hash, profile.version, e.score, cover_prompt_version)

        if not resume_path.exists():
            errors.append(f"{e.id}: missing resume.md")
        else:
            artifact_count += 1
            if cache.get("resume", resume_key) is None:
                resume_text = resume_path.read_text(encoding="utf-8")
                voice_issues = lint_file(str(resume_path))
                truth_issues = verify_resume_bullets(resume_text, profile)
                if voice_issues or truth_issues:
                    for issue in voice_issues:
                        errors.append(f"{e.id}: resume.md voice-dna: {issue.kind} at line {issue.line}")
                    for issue in truth_issues:
                        errors.append(f"{e.id}: resume.md truthfulness: {issue}")
                else:
                    cache.put("resume", resume_key, {"content": resume_text})
                    newly_cached += 1

        if not cover_path.exists():
            errors.append(f"{e.id}: missing cover.md")
        else:
            artifact_count += 1
            if cache.get("cover", cover_key) is None:
                cover_text = cover_path.read_text(encoding="utf-8")
                voice_issues = lint_file(str(cover_path))
                if voice_issues:
                    for issue in voice_issues:
                        errors.append(f"{e.id}: cover.md voice-dna: {issue.kind} at line {issue.line}")
                else:
                    cache.put("cover", cover_key, {"content": cover_text})
                    newly_cached += 1

    if errors:
        typer.echo("[artifacts:finalize] Issues found (uncached until fixed):\n" + "\n".join(errors), err=True)
        typer.echo(f"\nAgent: fix the listed files, then re-run `careeros artifacts --finalize --date {date}`.")
        raise typer.Exit(1)

    meta = runmeta.read_stage_meta(cfg.runs_dir, date, "artifacts")
    elapsed = time.time() - meta["prepared_at"] if "prepared_at" in meta else 0.0

    typer.echo(f"[artifacts:finalize] {len(evals)} job(s), {artifact_count} artifact(s) verified, "
               f"{newly_cached} newly cached.")
    runmeta.record_stage(cfg.runs_dir, date, "artifacts",
                          count_in=len(evals), count_out=artifact_count, seconds=elapsed,
                          cache_hits=meta.get("cache_hits", 0), cache_misses=meta.get("cache_misses", 0),
                          estimated_tokens=meta.get("estimated_tokens", 0))


# ── apply (AI stage: prepare / finalize, Apply-tier only — P2.10) ────────
#
# Automatic Application Answers for every Apply-tier (score >= threshold) job,
# run as part of `daily` right after resume/cover. `careeros/apply/browser.py`
# fetches each job's application-form text in the BACKGROUND (HTTP-first,
# optional headless-Playwright fallback — never the user's own browser, never
# a visible window). A form that isn't automatically readable is marked with
# one of the specific `STATUS_*` codes below (e.g. a login-gated flow, a
# closed posting, the optional Playwright extra not being installed) rather
# than one generic "needs manual review" bucket — the candidate can always
# run the on-demand `careeros apply <job-id>` (below) using their own real,
# logged-in browser for that one job, or for any below-threshold job they
# want to pursue anyway.

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
    import json
    with open(path) as f:
        return json.load(f)


def _save_apply_status(cfg: Config, date: str, status: dict) -> None:
    with open(_apply_status_path(cfg, date), "w") as f:
        f.write(dumps(status))


def _apply_prepare(cfg: Config, date: str) -> None:
    import json
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
    import json
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


# ── report render (deterministic) ────────────────────────────────────────

@app.command("render-report")
def render_report(job_id: str, date: str = typer.Option(None)):
    """[dev] Render the Level-1 daily report for one job — pure template, zero AI."""
    cfg = _config()
    date = date or _today()

    import json
    eval_path = runmeta.stage_dir(cfg.runs_dir, date, "evaluate") / f"{job_id}.json"
    jobs_path = runmeta.stage_dir(cfg.runs_dir, date, "normalize") / "jobs.json"
    with open(eval_path) as f:
        evaluation = Eval.from_dict(json.load(f))
    with open(jobs_path) as f:
        job_dict = next(j for j in json.load(f) if j["id"] == job_id)
    job = Job.from_dict(job_dict)

    artifacts = runmeta.artifacts_dir(cfg.runs_dir, date, job_id)
    resume_path = str(artifacts / "resume.md")
    cover_path = str(artifacts / "cover.md")

    report_md = render_daily_report(job, evaluation, resume_path, cover_path)
    report_path = artifacts / "daily_report.md"
    with open(report_path, "w") as f:
        f.write(report_md)

    typer.echo(f"[render-report] wrote {report_path}")


def _build_discovery_stats(cfg: Config, date: str) -> Optional[dict]:
    """P2.9 Discovery KPI join, extended for v1.2's multi-provider raw.json
    shape (`{"providers": [...], "items": {name: [...]}, "meta": {name:
    {...}}}` — see `discover`). Read-only over files `discover` already
    wrote plus the rolling-week budget state; fetches nothing, mutates
    nothing.

    The ATS-vs-job-board split and top-platforms list are Fantastic-Jobs-
    specific concepts (`source_type`/`source` are only meaningful on ITS
    items — providers/fantastic_jobs.py), so they're computed over ONLY that
    provider's own item slice, when it ran — never over other providers'
    items, which don't carry those fields.

    `stats["providers"]` is the NEW per-provider discovery-summary table
    (v1.2 revision #6): one entry per provider `discover` recorded (ran,
    skipped, or errored), straight from each `ProviderResult`'s persisted
    metadata — this is what `summary.md` renders as the discovery table."""
    import json

    raw_path = runmeta.stage_dir(cfg.runs_dir, date, "discover") / "raw.json"
    if not raw_path.exists():
        return None
    with open(raw_path) as f:
        raw = json.load(f)

    provider_names: list[str] = raw.get("providers", [])
    items_by_provider: dict = raw.get("items", {})
    meta_by_provider: dict = raw.get("meta", {})

    fj_items = items_by_provider.get("fantastic-jobs", [])
    ats_count = sum(1 for it in fj_items if it.get("source_type") == "ats")
    jb_count = len(fj_items) - ats_count if fj_items else 0

    platform_counts: dict[str, int] = {}
    for it in fj_items:
        src = it.get("source")
        if src:
            platform_counts[src] = platform_counts.get(src, 0) + 1
    top_platforms = sorted(platform_counts.items(), key=lambda kv: -kv[1])[:5]

    stats: dict = {"ats_count": ats_count, "jb_count": jb_count, "top_platforms": top_platforms}

    if "fantastic-jobs" in provider_names:
        fj_meta = meta_by_provider.get("fantastic-jobs", {})
        state = budget.load_state(cfg.careeros_dir, date)
        stats["requests_this_run"] = fj_meta.get("requests", 0)
        stats["requests_this_week"] = state.get("requests", 0)
        stats["records_this_run"] = fj_meta.get("records", len(fj_items))
        stats["records_this_week"] = state.get("records", 0)
        stats["records_quota"] = budget.weekly_quota(cfg.api)

    stats["providers"] = [
        {
            "provider": name,
            "records": len(items_by_provider.get(name, [])),
            "requests": meta_by_provider.get(name, {}).get("requests", 0),
            "cost_usd": meta_by_provider.get(name, {}).get("cost_usd", 0.0),
            "seconds": meta_by_provider.get(name, {}).get("seconds", 0.0),
            "skipped": meta_by_provider.get(name, {}).get("skipped", False),
            "skip_reason": meta_by_provider.get(name, {}).get("skip_reason"),
        }
        for name in provider_names
    ]
    stats["merged_total"] = sum(p["records"] for p in stats["providers"])

    return stats


@app.command("summary")
def summary(date: str = typer.Option(None)):
    """[dev] Render the day-level executive summary.md — pure template, zero
    AI. Funnel counts, the Apply (≥threshold) list, the Consider (near-miss)
    list, and cost-per-selected-job — the P2.6 KPI made visible every run.

    Reads `07_select/selected.json`/`consider.json` (the SAME partition
    `threshold` already computed via partition_evals) rather than re-deriving
    apply/consider from raw evals — the summary must never disagree with
    what actually got artifacts/Sheet rows."""
    cfg = _config()
    date = date or _today()

    import json
    manifest = runmeta.load_manifest(cfg.runs_dir, date)

    select_dir = runmeta.stage_dir(cfg.runs_dir, date, "select")

    def _load_evals(filename: str) -> list[Eval]:
        path = select_dir / filename
        if not path.exists():
            return []
        with open(path) as f:
            return [Eval.from_dict(d) for d in json.load(f)]

    apply_evals = _load_evals("selected.json")
    consider_evals = _load_evals("consider.json")

    jobs_path = runmeta.stage_dir(cfg.runs_dir, date, "normalize") / "jobs.json"
    jobs_by_id = {}
    if jobs_path.exists():
        with open(jobs_path) as f:
            jobs_by_id = {j["id"]: Job.from_dict(j) for j in json.load(f)}

    discovery_stats = _build_discovery_stats(cfg, date)

    summary_md = render_summary(date, manifest, apply_evals, consider_evals, jobs_by_id,
                                threshold=cfg.threshold, consider_threshold=cfg.consider_threshold,
                                discovery_stats=discovery_stats)
    summary_path = runmeta.run_dir(cfg.runs_dir, date) / "summary.md"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    with open(summary_path, "w") as f:
        f.write(summary_md)

    typer.echo(f"[summary] wrote {summary_path}")


# ── drive (optional, config-gated, fail-soft) ────────────────────────────

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


@app.command("drive")
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

    import json
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


# ── sheets ────────────────────────────────────────────────────────────────

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
# form wasn't automatically readable now comes from `_STATUS_LABELS` (see
# above, near `_apply_prepare`) — one specific, human-readable status per
# job rather than a single generic label, so the candidate immediately knows
# WHY (a login wall, a closed posting, Playwright not installed, ...) and,
# where relevant, what to do about it, instead of expecting a broken/missing
# link.


def _cell_is_blank(value: str) -> bool:
    """True for a Sheet cell with no real content — both the historical
    empty string and the "-" sentinel `sheets.py` now fills blanks with."""
    return value in ("", "-")


sheets_app = typer.Typer(help="Google Sheets operations")
app.add_typer(sheets_app, name="sheets")


@sheets_app.command("append")
def sheets_append(date: str = typer.Option(None, help="Run date, default today")):
    """[dev] Append selected jobs' rows to the configured Google Sheet."""
    cfg = _config()
    date = date or _today()
    start = time.time()

    import json
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


# ── backfill-drive (Phase 3, v1.1) ───────────────────────────────────────

@app.command("backfill-drive")
def backfill_drive(
    dry_run: bool = typer.Option(
        True, "--dry-run/--no-dry-run",
        help="Preview only (default): no Drive uploads, no Sheet writes. Pass --no-dry-run to apply."),
):
    """Add Drive artifacts + clickable Sheet links (Resume (Drive), Cover
    Letter (Drive), Evaluation (Drive)) to Apply-tier rows that predate Drive
    automation. Safe to re-run: rows that already have all three links are
    skipped (idempotent). Never fabricates — a row missing any of those
    links whose corresponding local file (resume.md/cover.md/daily_report.md)
    no longer exists on disk is listed as needing regeneration, not silently
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
        if resume_missing and not (artifacts_dir / "resume.md").exists():
            missing_locally.append("resume.md")
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

    import types
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


# ── lint ──────────────────────────────────────────────────────────────────

@app.command()
def lint(file: str):
    """[dev] Check a generated artifact against the deterministic voice-dna
    rules (em-dashes, banned AI vocabulary, negative-parallelism tell)."""
    issues = lint_file(file)
    typer.echo(format_issues(issues))
    if issues:
        raise typer.Exit(1)


@app.command("verify-resume")
def verify_resume(file: str):
    """[dev] Deterministic truthfulness check: every bullet/summary in a
    generated resume must verbatim-match a profile.yaml fact. CareerOS's
    analog of Career Ops' plan-lint.mjs verbatim check — enforces "selector,
    not writer" mechanically, not just via prompt instruction."""
    cfg = _config()
    profile = _load_profile(cfg)
    with open(file, encoding="utf-8") as f:
        resume_md = f.read()
    issues = verify_resume_bullets(resume_md, profile)
    if not issues:
        typer.echo("OK — every bullet/summary verbatim-matches profile.yaml.")
        return
    typer.echo(f"{len(issues)} truthfulness issue(s) found:")
    for issue in issues:
        typer.echo(f"  - {issue}")
    raise typer.Exit(1)


# ── end-user stubs (real orchestration lives in skills/*.md, run by the
#    host coding agent — these commands exist so `careeros <cmd>` is
#    discoverable and prints the right entry point) ──────────────────────

def _daily_stub():
    typer.echo(
        "`careeros daily` is a host-CLI skill, not a single blocking Python call — "
        "AI stages (gate, evaluate, resume, cover) need the agent's reasoning.\n\n"
        "Run it as `/careeros daily` in Claude Code / Codex / Gemini CLI / etc.\n"
        f"The skill playbook is at {REPO_ROOT / 'skills' / 'daily.md'}, and it "
        "orchestrates exactly the dev-stage commands above, in order."
    )


@app.command()
def daily():
    """Run the full daily pipeline. Entry point for the host-CLI skill."""
    _daily_stub()


@app.command()
def scan():
    """Alias for `daily` — CareerOS's job is scanning the market for you."""
    _daily_stub()


@app.command()
def start():
    """Guided onboarding -> .careeros/profile.yaml + discovery goal/plan."""
    typer.echo(
        "`careeros start` is a host-CLI skill (an interactive onboarding "
        "needs the agent's reasoning to extract facts from your CV and ask "
        "good follow-ups).\n\n"
        "Run it as `/careeros start`. Playbook: "
        f"{REPO_ROOT / 'skills' / 'start.md'}\n\n"
        "It opens by asking you to paste your CV (optional — type `skip` to "
        "build your profile by answering questions instead), then captures "
        "your interviews/week goal and Fantastic Jobs plan to recommend a "
        "daily discovery limit.\n\n"
        "For now, you can also hand-edit .careeros/profile.yaml directly "
        "(seeded from templates/profile.example.yaml by `careeros init`)."
    )


@app.command()
def prep(job_id: str):
    """Generate the Level-2 deep interview-prep report for one job."""
    typer.echo(f"Run `/careeros prep {job_id}` in your host CLI. "
               f"Playbook: {REPO_ROOT / 'skills' / 'prep.md'}")


@app.command()
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
    typer.echo(f"Run `/careeros apply {job_id}` in your host CLI. "
               f"Playbook: {REPO_ROOT / 'skills' / 'apply.md'}")


@app.command()
def publish(job_id: str, date: str = typer.Option(None, help="Run date the job was discovered in, default today")):
    """Upload one job's current artifacts (whichever exist on disk — resume,
    cover, evaluation, deep report, application answers) to Drive and patch
    just that Sheet row's Drive-link cells. Use this after `careeros prep
    <job-id>` or an on-demand `careeros apply <job-id>` so the result shows
    up in Drive + the Sheet without waiting for the next full `daily` run."""
    cfg = _config()
    date = date or _today()

    if not cfg.drive.get("enabled", False):
        typer.echo("[publish] Drive is disabled (set drive.enabled: true in "
                   ".careeros/config.yaml) — nothing to publish.", err=True)
        raise typer.Exit(1)

    import json
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

    found = sheets_mod.update_row_by_job_id(cfg, job_id, updates)
    if not found:
        typer.echo(f"[publish] Uploaded to Drive, but {job_id} isn't in the Sheet yet "
                   "(its row hasn't been appended by `sheets append`) — nothing to update.", err=True)
        raise typer.Exit(1)

    typer.echo(f"[publish] Updated Sheet row for {job_id}: {', '.join(updates.keys())}")


if __name__ == "__main__":
    app()
