"""careeros CLI.

Thin dispatch only — every command here calls into careeros/{config,models,
cache,runmeta,lint,report,sheets}.py or careeros/{providers,pipeline}/. No
business logic lives in this file.

Two tiers of commands:
  - End-user:  init, start, daily, prep, apply, config, providers
  - Developer: discover, normalize, dedupe, constraints, gate, evaluate,
               threshold, artifacts, sheets, lint, verify-resume — each
               stage runnable standalone against a run directory, for
               debugging without re-running the whole pipeline.

AI stages (gate, evaluate, artifacts) follow the host-CLI execution
boundary: a `--prepare` half (Python writes the stage's input + an
instruction for the agent) and a `--finalize` half (Python validates
whatever the agent wrote). See skills/daily.md for the full instruction
sequence.

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
from careeros.cache import Cache, artifact_cache_key, eval_cache_key
from careeros.config import Config, load_config
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
from careeros.providers.base import ProviderError
from careeros.providers.registry import get as get_provider
from careeros.providers.registry import list_providers
from careeros.report import render_daily_report, render_summary
from careeros import runmeta
from careeros import sheets as sheets_mod

app = typer.Typer(add_completion=False, no_args_is_help=True,
                   help="CareerOS — an AI-powered, deterministic job discovery and recommendation engine.")

REPO_ROOT = Path(__file__).resolve().parent.parent


def _provider_query_cfg(cfg: Config, provider_name: str) -> dict:
    """Which provider-config block pipeline/queryplan.py's neutral
    title_search/location_search/work_arrangement keys should overlay onto
    for `discover`'s segmented-query plan (P2.7) — provider-keyed since
    query-plan config is provider-specific, not a single generic Config
    field. `config.api` and `config.apify` share the same key names by
    design (see config.py), so queryplan.py itself never has to know which
    provider is active."""
    if provider_name == "fantastic-jobs-actor":
        return cfg.apify
    return cfg.api


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
    typer.echo(yaml.dump({
        "provider": cfg.provider,
        "endpoint": cfg.api.get("endpoint", "both"),
        "threshold_apply": cfg.threshold,
        "threshold_consider": cfg.consider_threshold,
        "gate_batch_size": cfg.gate_batch_size, "prompts": cfg.prompts,
        "sheets": cfg.sheets,
    }, sort_keys=False))

    # Discovery quota guard preview (REST provider only). Advisory — shows the
    # recommendation you'd see at `discover` time so you can tune api.limit up
    # front. Never changes anything.
    if cfg.provider != "fantastic-jobs-actor":
        try:
            reqs = len(build_query_plan(_load_profile(cfg), cfg.api)) if cfg.profile_path.exists() else 1
        except Exception:
            reqs = 1
        # "both" splits the per-tier allocation across endpoints, so records
        # reason in tiers (reqs), not ×endpoints.
        rec = budget.recommend(cfg.api, cfg.goals, reqs)
        typer.echo("\n".join(rec.lines()))


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

    # Discovery provider credentials
    if cfg.provider == "fantastic-jobs":
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
    elif cfg.provider == "fantastic-jobs-actor":
        token_env = cfg.apify.get("token_env", "APIFY_TOKEN")
        tokens_env = cfg.apify.get("tokens_env", "APIFY_TOKENS")
        if os.environ.get(tokens_env) or os.environ.get(token_env):
            results.append(_check_result(_CheckStatus.PASS, "Discovery credentials (legacy actor)",
                                         f"{tokens_env} or {token_env} is set"))
        else:
            results.append(_check_result(_CheckStatus.FAIL, "Discovery credentials (legacy actor)",
                                         f"neither {tokens_env} nor {token_env} is set"))

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

@app.command()
def discover(
    provider: Optional[str] = typer.Option(None, help="Provider id (default: config.provider)"),
    date: str = typer.Option(None, help="Run date, default today"),
    limit: Optional[int] = typer.Option(
        None, help="Per-query max records; default from config.api.limit, else 100. Overridden per-tier by tier_limits"),
    search: str = typer.Option(
        "", help="Manual single-query override — bypasses profile-driven segmentation"),
    dry_run: bool = typer.Option(False, help="Fetch and print, don't write raw.json"),
    ignore_budget: bool = typer.Option(
        False, "--ignore-budget", help="Bypass the weekly quota guard for this run"),
):
    """[dev] Discover: call a provider, write 01_discover/raw.json.

    By default (discovery_mode: "profile") this runs one segmented query per
    profile.work_mode_priority tier — see pipeline/queryplan.py; the
    discovery benchmark found a single broad query yields far fewer
    apply-worthy jobs than targeted per-work-mode ones. `discovery_mode:
    "single"`, `--search`, or a missing profile.yaml all fall back to
    today's one-query behavior."""
    cfg = _config()
    date = date or _today()
    provider_name = provider or cfg.provider
    p = get_provider(provider_name)
    provider_cfg = _provider_query_cfg(cfg, provider_name)

    if search or not cfg.profile_path.exists():
        queries: list[Optional[dict]] = [None]
    else:
        queries = build_query_plan(_load_profile(cfg), provider_cfg) or [None]

    # api.limit is the user's default per-query record cap; an explicit
    # --limit still wins, and tier_limits still override per work-mode tier.
    explicit_limit = provider_cfg.get("limit")
    has_explicit_limit = limit is not None or (isinstance(explicit_limit, int) and explicit_limit > 0)
    base_limit = limit if limit is not None else (explicit_limit or 100)

    # ── Quota guard (P2.8). REST provider only — the legacy actor has its own
    # per-call USD cost model. Recommend + explain + warn + prevent, but never
    # rewrite api.limit; the user owns the final number (see careeros/budget.py).
    guard_on = provider_cfg is cfg.api
    budget_state: dict = {}
    if guard_on:
        # "both" SPLITS base_limit across the 2 endpoints, so records/tier stays
        # = base_limit regardless of endpoint count — reason the record budget in
        # query TIERS. The HTTP call count (tiers × endpoints) is tracked
        # separately for the informational request counter.
        num_endpoints = 2 if provider_cfg.get("endpoint", "both") == "both" else 1
        rec = budget.recommend(cfg.api, cfg.goals, len(queries), cli_default_limit=base_limit)
        # P2.9: with no explicit limit (CLI --limit or api.limit) and a known
        # weekly quota (e.g. plan: free), USE the computed recommendation as
        # the actual per-query fetch limit instead of the hardcoded 100 —
        # closing the gap between what `careeros config`/`start` already
        # recommend and what `discover` actually fetches. Recompute `rec` so
        # the printed lines describe what's ACTUALLY about to run, not a
        # hypothetical 100-record default. An explicit limit is NEVER touched.
        if not has_explicit_limit and rec.recommended_per_request is not None:
            base_limit = rec.recommended_per_request
            rec = budget.recommend(cfg.api, cfg.goals, len(queries), cli_default_limit=base_limit)
        http_requests = len(queries) * num_endpoints
        for line in rec.lines():
            typer.echo(f"[discover] {line}")
        quota = budget.weekly_quota(cfg.api)
        budget_state = budget.load_state(cfg.careeros_dir, date)
        ok, msg = budget.check_before_run(budget_state, quota)
        if msg:
            typer.echo(f"[discover] {msg}")
        if not ok and not ignore_budget:
            typer.echo("[discover] Skipped to protect your weekly quota. Re-run with --ignore-budget to override.")
            raise typer.Exit(0)

    raw_items: list = []
    total_cost_usd = 0.0
    start = time.time()
    try:
        for i, query in enumerate(queries):
            work_mode = (query or {}).get("_work_mode", "single")
            effective_limit = resolve_tier_limit(work_mode, provider_cfg, base_limit)
            items, query_cost = p.fetch(cfg, limit=effective_limit, search=search, query=query)
            total_cost_usd += query_cost
            typer.echo(
                f"  [discover] query {i + 1}/{len(queries)} ({work_mode}, "
                f"limit={effective_limit}): {len(items)} items (${query_cost:.4f})"
            )
            raw_items.extend(items)
    except ProviderError as e:
        # P2.9: persist the classified failure so `careeros doctor` can show
        # it later without a live API call (see budget.record_last_error).
        budget.record_last_error(cfg.careeros_dir, date, str(e))
        typer.echo(f"[discover] {e}", err=True)
        raise typer.Exit(1)
    budget.clear_last_error(cfg.careeros_dir)
    elapsed = time.time() - start

    # The API was consumed regardless of --dry-run, so record it against the
    # rolling weekly budget before anything else can early-return.
    if guard_on:
        budget.record_consumption(budget_state, records=len(raw_items), requests=http_requests)
        budget.save_state(cfg.careeros_dir, budget_state)

    typer.echo(
        f"[discover] {provider_name}: {len(raw_items)} raw items across "
        f"{len(queries)} quer{'y' if len(queries) == 1 else 'ies'} "
        f"(${total_cost_usd:.4f}, {elapsed:.1f}s)"
    )

    if dry_run:
        typer.echo(dumps(raw_items[:3]))
        return

    stage_path = runmeta.stage_dir(cfg.runs_dir, date, "discover")
    with open(stage_path / "raw.json", "w") as f:
        f.write(dumps({
            "provider": provider_name,
            "queries": [(q or {}).get("_work_mode", "single") for q in queries],
            "items": raw_items,
        }))

    runmeta.record_stage(cfg.runs_dir, date, "discover",
                          count_in=0, count_out=len(raw_items), seconds=elapsed,
                          apify_cost_usd=total_cost_usd)


# ── normalize ─────────────────────────────────────────────────────────────

@app.command()
def normalize(date: str = typer.Option(None, help="Run date, default today")):
    """[dev] Normalize: 01_discover/raw.json -> 02_normalize/jobs.json."""
    cfg = _config()
    date = date or _today()

    raw_path = runmeta.stage_dir(cfg.runs_dir, date, "discover") / "raw.json"
    if not raw_path.exists():
        typer.echo(f"No {raw_path} — run `careeros discover` first.", err=True)
        raise typer.Exit(1)

    import json
    with open(raw_path) as f:
        raw = json.load(f)
    provider_name = raw["provider"]
    p = get_provider(provider_name)

    start = time.time()
    jobs = normalize_all(raw["items"], p, source=provider_name,
                          description_max_chars=cfg.description_max_chars)
    elapsed = time.time() - start

    out_path = runmeta.stage_dir(cfg.runs_dir, date, "normalize") / "jobs.json"
    with open(out_path, "w") as f:
        f.write(dumps([j.to_dict() for j in jobs]))

    typer.echo(f"[normalize] {len(raw['items'])} raw -> {len(jobs)} jobs ({elapsed:.1f}s)")
    runmeta.record_stage(cfg.runs_dir, date, "normalize",
                          count_in=len(raw["items"]), count_out=len(jobs), seconds=elapsed)


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
    """P2.9 Discovery KPI join — read-only over files `discover` already
    wrote plus the rolling-week budget state. Fetches nothing, mutates
    nothing; the join key is raw item `source_type`/`source` (present on
    every Fantastic Jobs item — see providers/fantastic_jobs.py). Requests
    this run are recomputed deterministically from raw.json's `queries` list
    length × endpoint count (both already persisted at discover time), so no
    new per-run request count needs to be stored."""
    import json

    raw_path = runmeta.stage_dir(cfg.runs_dir, date, "discover") / "raw.json"
    if not raw_path.exists():
        return None
    with open(raw_path) as f:
        raw = json.load(f)
    items = raw.get("items", [])

    ats_count = sum(1 for it in items if it.get("source_type") == "ats")
    jb_count = len(items) - ats_count

    platform_counts: dict[str, int] = {}
    for it in items:
        src = it.get("source")
        if src:
            platform_counts[src] = platform_counts.get(src, 0) + 1
    top_platforms = sorted(platform_counts.items(), key=lambda kv: -kv[1])[:5]

    stats: dict = {"ats_count": ats_count, "jb_count": jb_count, "top_platforms": top_platforms}

    if raw.get("provider") == "fantastic-jobs":
        num_queries = len(raw.get("queries", []))
        num_endpoints = 2 if cfg.api.get("endpoint", "both") == "both" else 1
        state = budget.load_state(cfg.careeros_dir, date)
        stats["requests_this_run"] = num_queries * num_endpoints
        stats["requests_this_week"] = state.get("requests", 0)
        stats["records_this_run"] = len(items)
        stats["records_this_week"] = state.get("records", 0)
        stats["records_quota"] = budget.weekly_quota(cfg.api)

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
    back)."""
    return {
        job_id: {
            "folder": r.folder_link, "resume": r.resume_link,
            "cover": r.cover_link, "warnings": r.warnings,
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
    # {"job_id": {"folder": url, "resume": url, "cover": url, "warnings": [...]}}
    drive_links_path = runmeta.run_dir(cfg.runs_dir, date) / "drive_links.json"
    drive_links: dict = {}
    if drive_links_path.exists():
        with open(drive_links_path) as f:
            drive_links = json.load(f)

    rows = []
    # APPLY tier: full row with artifact paths + any Drive links.
    for e in apply_evals:
        job = jobs_by_id[e.id]
        artifacts = runmeta.artifacts_dir(cfg.runs_dir, date, e.id)
        links = drive_links.get(e.id, {})
        rows.append(sheets_mod.job_to_row(
            date, job, e,
            resume_path=str(artifacts / "resume.md"),
            cover_path=str(artifacts / "cover.md"),
            report_path=str(artifacts / "daily_report.md"),
            drive_folder_link=links.get("folder", ""),
            resume_drive_link=links.get("resume", ""),
            cover_drive_link=links.get("cover", ""),
            tier="Apply",
        ))
    # CONSIDER tier: near-misses — NO artifacts, NO Drive; just score + a
    # concise reason it fell short of the apply threshold (from the eval).
    for e in consider_evals:
        job = jobs_by_id[e.id]
        rows.append(sheets_mod.job_to_row(
            date, job, e,
            resume_path="", cover_path="", report_path="",
            drive_folder_link="",
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


# ── backfill-drive (Phase 3, v1.1) ───────────────────────────────────────

@app.command("backfill-drive")
def backfill_drive(
    dry_run: bool = typer.Option(
        True, "--dry-run/--no-dry-run",
        help="Preview only (default): no Drive uploads, no Sheet writes. Pass --no-dry-run to apply."),
):
    """Add Drive artifacts + clickable Sheet links (Drive Folder, Resume
    (Drive), Cover Letter (Drive)) to Apply-tier rows that predate Drive
    automation. Safe to re-run: rows that already have both links are
    skipped (idempotent). Never fabricates — a row whose local
    resume.md/cover.md no longer exist on disk is listed as needing
    regeneration, not silently invented. Defaults to --dry-run so the very
    first run against your real Sheet only shows you what WOULD happen."""
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
        if row.get("Resume (Drive)") and row.get("Cover Letter (Drive)"):
            already_done += 1
            continue
        date, job_id = row.get("Date", ""), row.get("Job ID", "")
        company, role = row.get("Company", ""), row.get("Role", "")
        if not date or not job_id:
            continue  # malformed row (predates Job ID being tracked) — nothing we can key on
        artifacts_dir = runmeta.artifacts_dir(cfg.runs_dir, date, job_id)
        if not (artifacts_dir / "resume.md").exists() or not (artifacts_dir / "cover.md").exists():
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
        try:
            found = sheets_mod.update_row_by_job_id(cfg, job_id, {
                "Drive Folder": r.folder_link,
                "Resume (Drive)": r.resume_link,
                "Cover Letter (Drive)": r.cover_link,
            })
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
            ok = (fresh.get("Drive Folder") == r.folder_link
                  and fresh.get("Resume (Drive)") == r.resume_link
                  and fresh.get("Cover Letter (Drive)") == r.cover_link)
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
def apply(job_id: str):
    """Detect ATS and generate application answers for pasted questions."""
    typer.echo(f"Run `/careeros apply {job_id}` in your host CLI. "
               f"Playbook: {REPO_ROOT / 'skills' / 'apply.md'}")


if __name__ == "__main__":
    app()
