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

import datetime
import os
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from apify_client import ApifyClient
from apify_client.errors import ApifyApiError

from careeros import budget
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
    careeros_dir: Path | None = None,
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

    `careeros_dir` (optional — omitted only by tests that don't care about
    cross-call token memory) enables the rolling-month exhaustion cache
    (`budget.apify_tokens.json`): a token that already failed with a
    budget/consent error THIS billing cycle is skipped up front instead of
    being retried and re-earning the same rejection on every provider call.
    Rotation itself is otherwise silent — no "token index N failed" noise on
    a normal, recoverable rotation; that's expected multi-key behavior, not
    something worth alarming the user about.
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

    today_iso = datetime.date.today().isoformat()
    tokens_state = (
        budget.load_apify_tokens_state(careeros_dir, today_iso) if careeros_dir is not None else None
    )

    last_error: Exception | None = None
    tried_any = False
    for token in tokens:
        # `is_token_exhausted` only skips a token pre-emptively if it was
        # ALREADY verified-dead earlier THIS SAME day — any other day
        # (including a same-token top-up mid-month) gets one fresh live
        # retry below before being trusted as exhausted again. See
        # budget.is_token_exhausted's docstring / AGENT_GUIDE.md.
        if tokens_state is not None and budget.is_token_exhausted(tokens_state, token, today_iso):
            continue
        tried_any = True
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
            # Silent by design: this is expected, recoverable behavior for
            # anyone running more than one Apify token, not a failure worth
            # surfacing to the user.
            last_error = e
            if tokens_state is not None:
                budget.mark_token_exhausted(tokens_state, token, today_iso)
                budget.save_apify_tokens_state(careeros_dir, tokens_state)
            continue

        dataset_id = (
            run.get("defaultDatasetId") if isinstance(run, dict)
            else getattr(run, "default_dataset_id", None)
        )
        if not dataset_id:
            raise ProviderError(f"{provider_id}: actor run returned no dataset id")
        items = list(client.dataset(dataset_id).iterate_items())
        if tokens_state is not None:
            # A live success clears any older exhausted-mark for this token
            # (e.g. a mid-month top-up) — keeps doctor's token-health display
            # accurate instead of showing a stale "exhausted" from days ago
            # that `is_token_exhausted` would no longer even honor.
            fp = budget.token_fingerprint(token)
            if tokens_state.get("exhausted", {}).pop(fp, None) is not None:
                budget.save_apify_tokens_state(careeros_dir, tokens_state)
        return ProviderResult(
            provider=provider_id, items=items, cost_usd=_extract_usage_usd(run),
            requests=1, records=len(items), seconds=time.time() - start,
        )

    if not tried_any:
        # Every configured token was already known-exhausted this billing
        # cycle before we even tried one — same terminal state as trying
        # and failing all of them, just without the wasted API calls.
        raise ProviderError(
            f"{provider_id}: all {len(tokens)} configured Apify token(s) are already known "
            f"exhausted this billing cycle. Add a fresh key to "
            f"{apify_cfg.get('tokens_env', 'APIFY_TOKENS')}, raise your Apify plan's limit, "
            "or wait for next month's reset."
        )
    raise ProviderError(
        f"{provider_id}: all {len(tokens)} configured Apify token(s) exhausted this billing "
        f"cycle (last error: {last_error}). Add a fresh key to "
        f"{apify_cfg.get('tokens_env', 'APIFY_TOKENS')}, raise your Apify plan's limit, "
        "or wait for next month's reset."
    )
