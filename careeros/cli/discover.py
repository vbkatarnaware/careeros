"""`careeros discover` — run every enabled provider, write 01_discover/raw.json."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Optional

import typer

from careeros import budget, runmeta
from careeros.cli import app
from careeros.cli._shared import _config, _load_profile, _provider_query_cfg, _today
from careeros.config import Config, enabled_providers
from careeros.models import dumps
from careeros.pipeline.queryplan import build_query_plan, resolve_tier_limit
from careeros.providers.base import ProviderError, ProviderResult
from careeros.providers.registry import get as get_provider


@dataclass
class _ProviderPlan:
    """Everything one provider's actual fetch(es) need, decided during the
    serial PREFLIGHT phase (`_preflight_provider`) — validate() plus the
    budget/quota guard CHECK (never the recording of consumption). Built so
    the fetch itself (`_fetch_provider`, pure network/Apify I/O) can safely
    run inside a worker thread: it touches no shared file-backed state, only
    this plan and the provider's own `fetch()` call."""

    name: str
    capability: str  # "weekly" | "monthly" | "none"
    queries: list[Optional[dict]] = field(default_factory=lambda: [None])
    base_limit: int = 100
    http_requests: int = 1
    effective_limit: int = 100


def _preflight_provider(
    cfg: Config, name: str, *, date: str, limit: Optional[int], search: str, ignore_budget: bool,
) -> "ProviderResult | _ProviderPlan":
    """validate() + the budget/quota guard CHECK for one provider — no
    network call, no shared-state mutation. Returns a skipped `ProviderResult`
    if this provider can't run this call at all (bad config, guard says
    stop), or a `_ProviderPlan` describing exactly what its (possibly
    concurrent) fetch phase should do.

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
        # LIVE quota is authoritative, never the local counter alone (a real
        # incident: a rotated-in fresh API key still got reported "exhausted"
        # because the local .careeros/discovery_budget.json counter is a
        # Monday-reset calculation entirely independent of WHICH key is
        # configured). `check_before_run` still runs here so its message is
        # printed as an early-warning estimate, but its verdict no longer
        # hard-skips the run — the fetch phase's real HTTP call is what
        # verifies quota against the live API (see `_fetch_one_endpoint`'s
        # 429/x-ratelimit-* handling), and a genuinely exhausted key is
        # skipped there, from the live response, not a local guess.
        quota = budget.weekly_quota(provider_cfg)
        weekly_state = budget.load_state(cfg.careeros_dir, date)
        ok, msg = budget.check_before_run(weekly_state, quota)
        if msg:
            typer.echo(f"[discover] {name}: {msg}")
        if not ok:
            typer.echo(
                f"[discover] {name}: local estimate says quota may be exhausted — "
                "verifying against the live API instead of skipping on that estimate alone."
            )

        return _ProviderPlan(
            name=name, capability="weekly", queries=queries,
            base_limit=base_limit, http_requests=http_requests,
        )

    if capability == "monthly":
        # HONEST LIMITATION (deliberate trade-off, not a bug): this check
        # only sees spend already RECORDED before this discover call, not
        # what a sibling "monthly" provider fetching CONCURRENTLY in this
        # same call is about to spend — recording happens serially, only
        # after every concurrent fetch finishes (see `discover`). Multiple
        # monthly-capability providers enabled together can therefore
        # collectively land slightly over `max_monthly_budget_usd` within a
        # single run before the NEXT run's check catches up. This mirrors
        # `check_apify_budget`'s own documented best-effort/soft-guard
        # philosophy; the real backstop is the per-call `max_total_charge_usd`
        # hard cap Apify enforces server-side on every individual actor run.
        max_budget = provider_cfg.get("max_monthly_budget_usd") or cfg.apify.get("max_monthly_budget_usd")
        apify_state = budget.load_apify_state(cfg.careeros_dir, date)
        ok, msg = budget.check_apify_budget(apify_state, max_budget)
        if msg:
            typer.echo(f"[discover] {name}: {msg}")
        if not ok and not ignore_budget:
            return ProviderResult.skip(name, "monthly Apify budget exhausted")

        effective_limit = limit if limit is not None else (provider_cfg.get("limit") or 100)
        return _ProviderPlan(name=name, capability="monthly", effective_limit=effective_limit)

    # capability == "none": unmetered, no guard (RemoteOK, We Work Remotely).
    effective_limit = limit if limit is not None else (provider_cfg.get("limit") or 100)
    return _ProviderPlan(name=name, capability="none", effective_limit=effective_limit)


def _fetch_provider(cfg: Config, plan: _ProviderPlan, *, search: str) -> ProviderResult:
    """Run one provider's actual fetch(es) — the network-I/O part. Safe to
    run concurrently across providers (see `discover`'s ThreadPoolExecutor):
    it reads only `cfg` and `plan`, and touches no shared file-backed budget
    state — that's recorded serially afterward, once every concurrent fetch
    has finished (`_record_provider_consumption`).

    A hard `ProviderError` (invalid/exhausted credentials, an account-level
    failure) is caught here for EVERY capability and converted to a skip —
    uniformly, so one provider's failure never takes down the others and
    never aborts the rest of a multi-provider `discover` run."""
    p = get_provider(plan.name)
    name = plan.name

    if plan.capability == "weekly":
        items: list = []
        cost = 0.0
        start = time.time()
        for i, query in enumerate(plan.queries):
            work_mode = (query or {}).get("_work_mode", "single")
            effective_limit = resolve_tier_limit(work_mode, _provider_query_cfg(cfg, name), plan.base_limit)
            try:
                result = p.fetch(cfg, limit=effective_limit, search=search, query=query)
            except ProviderError as e:
                # A HARD failure from the API/account itself (e.g. an
                # invalid/exhausted key) — skip just this provider for this
                # run rather than aborting every other enabled provider. Any
                # queries already completed in this segmented plan are
                # discarded — a partial result from an account-level failure
                # isn't safe to treat as "done."
                typer.echo(f"[discover] {name}: skipped — {e}")
                return ProviderResult.skip(name, str(e))
            cost += result.cost_usd
            typer.echo(
                f"  [discover] {name} query {i + 1}/{len(plan.queries)} ({work_mode}, "
                f"limit={effective_limit}): {len(result.items)} items (${result.cost_usd:.4f})"
            )
            items.extend(result.items)
        return ProviderResult(
            provider=name, items=items, cost_usd=cost,
            requests=plan.http_requests, records=len(items), seconds=time.time() - start,
        )

    if plan.capability == "monthly":
        try:
            result = p.fetch(cfg, limit=plan.effective_limit, search=search, query=None)
        except ProviderError as e:
            # A HARD failure from the actor/account itself (e.g. every
            # rotated Apify token exhausted or out of balance) — distinct
            # from the soft max_monthly_budget_usd guard checked in
            # preflight. Tell the user clearly and skip just THIS provider.
            typer.echo(f"[discover] {name}: skipped — {e}")
            return ProviderResult.skip(name, f"Apify usage/quota exhausted: {e}")
        typer.echo(
            f"[discover] {name}: {len(result.items)} items (${result.cost_usd:.4f}, {result.seconds:.1f}s)"
        )
        return result

    # "none": unmetered (RemoteOK, We Work Remotely). Previously had no
    # try/except at all — a network/timeout ProviderError from one of these
    # would silently abort the WHOLE multi-provider run, the exact same bug
    # class fixed for the weekly/monthly branches earlier. Now consistent.
    try:
        result = p.fetch(cfg, limit=plan.effective_limit, search=search, query=None)
    except ProviderError as e:
        typer.echo(f"[discover] {name}: skipped — {e}")
        return ProviderResult.skip(name, str(e))
    typer.echo(f"[discover] {name}: {len(result.items)} items ({result.seconds:.1f}s)")
    return result


def _record_provider_consumption(cfg: Config, plan: _ProviderPlan, result: ProviderResult, date: str) -> None:
    """Serial bookkeeping, run AFTER every concurrent fetch in this
    `discover` call has finished, IN CONFIG ORDER — mutates and saves the
    shared weekly/monthly budget state files. Must never run inside a
    worker thread: these are shared, file-backed counters, and running this
    serially (one provider fully recorded before the next provider's write)
    is what keeps concurrent fetching race-free. A skipped result records
    nothing, since nothing was actually consumed."""
    if result.skipped:
        return
    if plan.capability == "weekly":
        weekly_state = budget.load_state(cfg.careeros_dir, date)
        budget.record_consumption(weekly_state, records=len(result.items), requests=plan.http_requests)
        budget.save_state(cfg.careeros_dir, weekly_state)
    elif plan.capability == "monthly":
        apify_state = budget.load_apify_state(cfg.careeros_dir, date)
        budget.record_apify_spend(apify_state, result.cost_usd)
        budget.save_apify_state(cfg.careeros_dir, apify_state)


@app.command(hidden=True)
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

    v1.3: each provider's actual fetch() runs CONCURRENTLY (a thread pool,
    capped by `discovery_max_workers` — default 4, set to 1 to force serial)
    since every fetch is blocking network/Apify I/O. Budget/quota
    CHECKING and the merged result order always stay serial and in CONFIG
    ORDER regardless of which provider's network call finishes first — see
    `_preflight_provider` (validate + guard check, serial), `_fetch_provider`
    (the actual network call, concurrent), `_record_provider_consumption`
    (budget bookkeeping, serial, after every fetch joins).

    Budget/quota enforcement is CAPABILITY-driven, never by provider identity
    (see `budget.guard_for`): a provider whose own config declares a weekly
    record quota (Fantastic Jobs) gets that guard (unchanged — the segmented
    per-work-mode query plan, P2.8's quota-aware default limit, everything);
    one declaring a monthly USD budget (every Apify-actor provider) gets the
    rolling-month soft guard; an unmetered free provider (RemoteOK, We Work
    Remotely) gets none. A provider that's ENABLED but can't run this call
    (failed `validate()`, its guard says stop, or a hard fetch error) is
    recorded as `skipped` with a reason — never silently dropped, and never
    aborts the rest of the run — and the run continues with whatever else is
    enabled."""
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

    start_all = time.time()
    try:
        # Phase 1 — preflight, serial, in config order: validate() + the
        # budget/quota guard CHECK for every provider. Fast, no network.
        results_by_name: dict[str, ProviderResult] = {}
        plans: list[_ProviderPlan] = []
        for name in provider_names:
            outcome = _preflight_provider(
                cfg, name, date=date, limit=limit, search=search, ignore_budget=ignore_budget,
            )
            if isinstance(outcome, ProviderResult):
                results_by_name[name] = outcome
            else:
                plans.append(outcome)

        # Phase 2 — fetch, concurrent: the actual network/Apify calls. Each
        # provider's own hard-error handling (ProviderError -> skip) already
        # happens inside _fetch_provider, so a worker never needs to raise.
        if plans:
            max_workers = max(1, min(len(plans), cfg.discovery_max_workers))
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_name = {
                    executor.submit(_fetch_provider, cfg, plan, search=search): plan.name
                    for plan in plans
                }
                for future in as_completed(future_to_name):
                    results_by_name[future_to_name[future]] = future.result()

        # Phase 3 — bookkeeping + merge, serial, in CONFIG ORDER (never
        # completion order — dedupe's "keep first" contract depends on it).
        results: list[ProviderResult] = []
        plans_by_name = {plan.name: plan for plan in plans}
        for name in provider_names:
            result = results_by_name[name]
            plan = plans_by_name.get(name)
            if plan is not None:
                _record_provider_consumption(cfg, plan, result, date)
            results.append(result)
    except ProviderError as e:
        # Defensive backstop only — every capability branch in
        # _fetch_provider already catches ProviderError itself, so this
        # should rarely if ever fire. Persist the classified failure so
        # `careeros doctor` can show it later without a live API call.
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

    if total_items == 0 and results and all(r.skipped for r in results):
        # Every enabled provider was skipped — not an error (a single
        # skipped provider, e.g. budget exhausted, is expected/non-fatal by
        # design, same exit code either way), but plain-English enough that
        # nothing looks like it silently succeeded with real jobs.
        typer.echo(
            "[discover] Every enabled provider was skipped this run — no "
            "jobs were fetched. Reasons:"
        )
        for r in results:
            typer.echo(f"  - {r.provider}: {r.skip_reason or 'unknown reason'}")
        typer.echo(
            "[discover] Fix at least one provider's credentials/budget above "
            "(see providers/README.md), or check .careeros/config.yaml, then "
            "re-run discover."
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
                    "live_quota": r.live_quota,
                }
                for r in results
            },
        }))

    runmeta.record_stage(cfg.runs_dir, date, "discover",
                          count_in=0, count_out=total_items, seconds=elapsed_all,
                          apify_cost_usd=total_cost)
