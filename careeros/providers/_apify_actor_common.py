"""Shared Apify-actor run mechanics for the v1.2 actor-based providers
(naukri, foundit, indeed, glassdoor, ziprecruiter) — token-pool rotation,
the per-call `max_total_charge_usd` cap, and cost read-back.

Extracted from the pattern already proven in
`providers/legacy/fantastic_jobs_actor.py`. That file is deliberately left
untouched (beyond its own mechanical ProviderResult update) — this is a NEW
shared helper for the new v1.2 providers only, not a refactor of the
existing working legacy path, so there is zero risk to it.
"""

from __future__ import annotations

import os
import time
from decimal import Decimal
from typing import Any

from apify_client import ApifyClient
from apify_client.errors import ApifyApiError

from careeros.providers.base import ProviderError, ProviderResult


def iter_tokens(apify_cfg: dict[str, Any]) -> list[str]:
    """Token rotation pool: `tokens_env` (comma-separated, one per account)
    first, falling back to the single `token_env` var. Same convention and
    env var names as the legacy actor provider — one Apify account, shared
    across every Apify-actor-based provider."""
    tokens_env = apify_cfg.get("tokens_env", "APIFY_TOKENS")
    raw = os.environ.get(tokens_env, "")
    tokens = [t.strip() for t in raw.split(",") if t.strip()]
    if tokens:
        return tokens
    token_env = apify_cfg.get("token_env", "APIFY_TOKEN")
    single = os.environ.get(token_env)
    return [single] if single else []


def validate_apify_token(apify_cfg: dict[str, Any]) -> list[str]:
    """Shared `validate()` body for every Apify-actor-based provider — pure,
    no network call."""
    if iter_tokens(apify_cfg):
        return []
    return [
        f"No Apify token configured — set {apify_cfg.get('tokens_env', 'APIFY_TOKENS')} "
        f"(comma-separated, for rotation) or {apify_cfg.get('token_env', 'APIFY_TOKEN')} "
        "(single token). See providers/README.md."
    ]


def _extract_usage_usd(run: Any) -> float:
    """Best-effort USD cost of one finished actor run — see `run_actor`'s
    docstring for the same undercounting caveat already documented on the
    legacy actor provider."""
    raw = run.get("usageTotalUsd") if isinstance(run, dict) else getattr(run, "usage_total_usd", None)
    return float(raw) if raw is not None else 0.0


def run_actor(
    provider_id: str,
    apify_cfg: dict[str, Any],
    actor_id: str,
    run_input: dict[str, Any],
    *,
    max_cost_usd: float | None = None,
) -> ProviderResult:
    """Run one Apify actor with token-pool rotation + an optional hard
    per-call `max_total_charge_usd` cap (Apify enforces this server-side —
    the RELIABLE half of the v1.2 monthly-budget guard; see
    `budget.py`'s honesty caveat on the soft rolling-month counter, the
    other half, which `discover` checks BEFORE calling this).

    Raises ProviderError if every configured token fails (exhausted/invalid)
    or none is configured — `discover` catches this per-provider via each
    provider's own `fetch()` call and records it against that provider only;
    it never crashes the whole multi-provider run.

    `cost_usd` on the returned ProviderResult is a best-effort LOWER BOUND,
    not the settled final spend — Apify's per-result dataset-item charges
    can settle asynchronously after `.call()` returns (found live on the
    legacy actor provider, 2026-07-08). Directionally useful; check the
    Apify console for the authoritative total.
    """
    start = time.time()
    tokens = iter_tokens(apify_cfg)
    if not tokens:
        raise ProviderError(
            f"{provider_id}: no Apify token configured — set "
            f"{apify_cfg.get('tokens_env', 'APIFY_TOKENS')} or "
            f"{apify_cfg.get('token_env', 'APIFY_TOKEN')}. See providers/README.md."
        )

    max_total_charge_usd = Decimal(str(max_cost_usd)) if max_cost_usd is not None else None

    last_error: Exception | None = None
    for index, token in enumerate(tokens):
        client = ApifyClient(token)
        try:
            run = client.actor(actor_id).call(
                run_input=run_input, max_total_charge_usd=max_total_charge_usd
            )
        except ApifyApiError as e:
            # Budget/consent errors (exhausted monthly usage, or the
            # "Maximum charged results must be greater than zero" state seen
            # live when usage is already at the cap) — try the next token in
            # the pool rather than failing the whole provider immediately.
            last_error = e
            print(f"  [{provider_id}] token index {index} failed ({e}); trying next token…")
            continue

        dataset_id = (
            run.get("defaultDatasetId") if isinstance(run, dict)
            else getattr(run, "default_dataset_id", None)
        )
        if not dataset_id:
            raise ProviderError(f"{provider_id}: actor run returned no dataset id")
        items = list(client.dataset(dataset_id).iterate_items())
        return ProviderResult(
            provider=provider_id, items=items, cost_usd=_extract_usage_usd(run),
            requests=1, records=len(items), seconds=time.time() - start,
        )

    raise ProviderError(
        f"{provider_id}: all {len(tokens)} configured Apify token(s) failed (exhausted budget "
        f"or invalid) — last error: {last_error}. Add a fresh token to "
        f"{apify_cfg.get('tokens_env', 'APIFY_TOKENS')} or wait for the monthly reset."
    )
