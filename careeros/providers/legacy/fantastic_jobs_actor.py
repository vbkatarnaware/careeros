"""Provider: Fantastic Jobs / Apify actor (career-site-job-listing-api) — LEGACY.

https://apify.com/fantastic-jobs/career-site-job-listing-api

P2.7: superseded as the default by the REST provider at
`careeros/providers/fantastic_jobs.py` (registered as `fantastic-jobs`),
which hits the same underlying Fantastic Jobs dataset directly. This module
is kept registered as `fantastic-jobs-actor` — a reference/no-code backend
for Zapier/n8n/MCP-style setups — but is NOT the actively maintained path;
new discovery features land in the REST provider only. See the P2.6/P2.7
architecture review for the full reasoning (no-code is this actor's only
real advantage; a code-first CLI gets no benefit from the extra Apify
platform dependency, actor cold-start latency, or its pay-per-result cost
model).

Runs the actor and returns raw dataset items untouched; this file's
`to_job_dict` maps those into the common pre-Job shape that
`pipeline/normalize.py` consumes.

FIELD NAMES VERIFIED LIVE (2026-07-07, build 0.0.64, apify-client 3.0.4).
The actor's real output is NOT the generic flat shape — it uses:
  title, organization (company), url (apply link), description_text,
  locations_derived (LIST of "City, Region, Country" strings),
  ai_work_arrangement ("Remote"/"Hybrid"/"On-site"), ai_employment_type
  (LIST of enum strings), date_posted (ISO). Location and work-arrangement
  are the two that a naive flat mapper drops, which is why this provider uses
  an explicit mapper rather than only the generic candidate-key lists.

Input contract (also verified): the actor takes titleSearch/locationSearch
(arrays), timeRange (enum: 1h|24h|7d|6m), and limit (integer, MIN 10), plus
the source-side filters wired below (aiWorkArrangementFilter, removeAgency,
hasSalary, titleExclusionSearch, locationExclusionSearch) — verified live
during the 2026-07-08 discovery benchmark. The run-input keys are nothing
like a generic {"search"} — see fetch().

Cost model: this is a pay-per-result Apify actor on a free-tier account with
a small monthly budget. QA discovered live that an unhandled budget-exhausted
call raises a raw `ApifyApiError` ("Maximum charged results must be greater
than zero" / "you will exceed your remaining usage of $X"). fetch() now:
(1) rotates through a comma-separated token pool on that specific error,
(2) caps each call's own spend via `max_total_charge_usd`, and (3) raises a
clean `ProviderError` — never a bare traceback — once every token is
exhausted or none are configured.
"""

from __future__ import annotations

import os
from decimal import Decimal
from typing import Any

from apify_client import ApifyClient
from apify_client.errors import ApifyApiError

from careeros.config import Config
from careeros.providers._apify_common import (
    COMPANY_KEYS, DESCRIPTION_KEYS, TITLE_KEYS, URL_KEYS, pick_field,
)
from careeros.providers.base import ProviderError, ProviderResult

ACTOR_ID = "fantastic-jobs/career-site-job-listing-api"
MIN_LIMIT = 10  # actor rejects limit < 10


def _iter_tokens(apify_cfg: dict[str, Any]) -> list[str]:
    """Token rotation pool: `tokens_env` (comma-separated, one per account)
    first, falling back to the single `token_env` var. Never logs values —
    callers only ever see/record an index, per the security requirement that
    secrets are never printed."""
    tokens_env = apify_cfg.get("tokens_env", "APIFY_TOKENS")
    raw = os.environ.get(tokens_env, "")
    tokens = [t.strip() for t in raw.split(",") if t.strip()]
    if tokens:
        return tokens
    token_env = apify_cfg.get("token_env", "APIFY_TOKEN")
    single = os.environ.get(token_env)
    return [single] if single else []


# ai_employment_type enum -> Job.employment_type enum
_EMPLOYMENT_MAP = {
    "FULL_TIME": "full_time",
    "PART_TIME": "part_time",
    "CONTRACT": "contract",
    "CONTRACTOR": "contract",
    "TEMPORARY": "contract",
    "INTERN": "internship",
    "INTERNSHIP": "internship",
}


def _build_run_input(apify_cfg: dict[str, Any], *, limit: int, search: str) -> dict[str, Any]:
    """Build the actor's run input from config-driven search + source-side
    filter params. `search` (the CLI --search string) overrides
    config.apify.title_search when given.

    The filter fields (aiWorkArrangementFilter/removeAgency/hasSalary/
    titleExclusionSearch/locationExclusionSearch) are the "source-side
    filtering" fix from the 2026-07-08 discovery benchmark: fetching fewer,
    more-targeted jobs server-side is strictly better than fetching broadly
    and discarding most of it in the deterministic `constraints` stage.
    """
    title_search = [search] if search else list(apify_cfg.get("title_search", []) or [])
    location_search = list(apify_cfg.get("location_search", []) or [])
    title_exclusion = list(apify_cfg.get("title_exclusion_search", []) or [])
    location_exclusion = list(apify_cfg.get("location_exclusion_search", []) or [])
    work_arrangement = list(apify_cfg.get("work_arrangement", []) or [])
    has_salary = apify_cfg.get("has_salary")

    run_input: dict[str, Any] = {
        "limit": max(int(limit), MIN_LIMIT),
        "timeRange": apify_cfg.get("time_range", "7d"),
        "descriptionType": "text",
        "includeCompanyDetails": False,
        "removeAgency": bool(apify_cfg.get("remove_agency", True)),
    }
    if title_search:
        run_input["titleSearch"] = title_search
    if location_search:
        run_input["locationSearch"] = location_search
    if title_exclusion:
        run_input["titleExclusionSearch"] = title_exclusion
    if location_exclusion:
        run_input["locationExclusionSearch"] = location_exclusion
    if work_arrangement:
        run_input["aiWorkArrangementFilter"] = work_arrangement
    if has_salary is not None:
        run_input["hasSalary"] = bool(has_salary)
    return run_input


def _extract_usage_usd(run: Any) -> float:
    """Best-effort USD cost of one finished actor run, off its own metadata
    — dict-or-object defensive, same pattern as the dataset-id lookup above
    (apify-client 3.x objects, earlier versions dicts). NOT guaranteed final:
    found live that this can undercount the true settled cost (see fetch()'s
    docstring) — treat it as a lower bound, not authoritative spend."""
    raw = run.get("usageTotalUsd") if isinstance(run, dict) else getattr(run, "usage_total_usd", None)
    return float(raw) if raw is not None else 0.0


def _merge_query(apify_cfg: dict[str, Any], query: dict[str, Any] | None) -> dict[str, Any]:
    """Overlay a segmented-discovery query spec (pipeline/queryplan.py) onto
    the base apify config for one `fetch()` call. `_work_mode` is a
    debug/logging-only key on the spec, never an actor field — dropped here."""
    if not query:
        return apify_cfg
    return {**apify_cfg, **{k: v for k, v in query.items() if not k.startswith("_")}}


class FantasticJobsActorProvider:
    id = "fantastic-jobs-actor"

    def validate(self, config: Config) -> list[str]:
        """Config/credential problems only — no network call. Reuses
        `_iter_tokens` purely to check whether ANY token is configured."""
        if not _iter_tokens(config.apify):
            apify_cfg = config.apify
            return [
                f"No Apify token configured — set {apify_cfg.get('tokens_env', 'APIFY_TOKENS')} "
                f"(comma-separated, for rotation) or {apify_cfg.get('token_env', 'APIFY_TOKEN')} "
                "(single token). See providers/README.md."
            ]
        return []

    def fetch(
        self, config: Config, *, limit: int = 100, search: str = "",
        query: dict[str, Any] | None = None,
    ) -> ProviderResult:
        """`query`, when given (see pipeline/queryplan.py), overrides the
        matching config.apify keys for this one call only — e.g. a segmented
        discovery plan's per-work-mode location_search/work_arrangement. No
        new field-mapping logic: it's merged straight into the same dict
        `_build_run_input` already reads, so every existing filter keeps
        working unchanged for both the segmented and legacy single-query paths.

        `cost_usd` on the returned `ProviderResult` is the finished run's own
        `usageTotalUsd` field, read with zero extra API call. IMPORTANT
        (found live, 2026-07-08): this figure can materially UNDERCOUNT the
        true final cost — a verification run reported $0.02 across 3 queries
        here, while the account's actual monthly-usage balance rose by
        ~$0.50 for the same run. Apify's per-result dataset-item charges
        appear to settle asynchronously, after `.call()` already returns. So
        treat `cost_usd` (and anything derived from it — `summary.md`'s
        "Apify spend today", `run.json`'s `apify_cost_usd_total`) as a
        best-effort LOWER BOUND / directional signal, not the authoritative
        final spend — check your Apify console for the real settled total.
        """
        import time as _time
        start = _time.time()
        apify_cfg = config.apify
        tokens = _iter_tokens(apify_cfg)
        if not tokens:
            raise ProviderError(
                f"No Apify token configured — set {apify_cfg.get('tokens_env', 'APIFY_TOKENS')} "
                f"(comma-separated, for rotation) or {apify_cfg.get('token_env', 'APIFY_TOKEN')} "
                "(single token). See providers/README.md."
            )

        effective_cfg = _merge_query(apify_cfg, query)

        actor_id = apify_cfg.get("actor", ACTOR_ID)
        run_input = _build_run_input(effective_cfg, limit=limit, search=search)
        max_cost = apify_cfg.get("max_cost_usd")
        max_total_charge_usd = Decimal(str(max_cost)) if max_cost is not None else None

        last_error: Exception | None = None
        for index, token in enumerate(tokens):
            client = ApifyClient(token)
            try:
                run = client.actor(actor_id).call(
                    run_input=run_input, max_total_charge_usd=max_total_charge_usd
                )
            except ApifyApiError as e:
                # Budget/consent errors (exhausted monthly usage, or the
                # "Maximum charged results must be greater than zero" state
                # observed live when usage is already at the cap) — try the
                # next token in the pool rather than crashing.
                last_error = e
                print(f"  [fantastic-jobs-actor] token index {index} failed ({e}); trying next token…")
                continue

            dataset_id = (
                run.get("defaultDatasetId") if isinstance(run, dict)
                else getattr(run, "default_dataset_id", None)
            )
            if not dataset_id:
                raise ProviderError("fantastic-jobs-actor: actor run returned no dataset id")
            items = list(client.dataset(dataset_id).iterate_items())
            return ProviderResult(
                provider=self.id, items=items, cost_usd=_extract_usage_usd(run),
                requests=1, records=len(items), seconds=_time.time() - start,
            )

        raise ProviderError(
            f"All {len(tokens)} configured Apify token(s) failed (exhausted budget or invalid) — "
            f"last error: {last_error}. Add a fresh token to "
            f"{apify_cfg.get('tokens_env', 'APIFY_TOKENS')} or wait for the monthly reset."
        )

    def to_job_dict(self, raw: dict[str, Any]) -> dict[str, Any] | None:
        # Flat fields still map cleanly via the shared candidate lists.
        title = pick_field(raw, TITLE_KEYS)
        url = pick_field(raw, URL_KEYS)
        if not title or not url.startswith("http"):
            return None

        return {
            "title": title,
            "company": pick_field(raw, COMPANY_KEYS, fallback="Unknown"),
            "apply_url": url,
            "description": pick_field(raw, DESCRIPTION_KEYS) or None,
            "location": _first_location(raw),
            "remote": _remote_from_arrangement(raw.get("ai_work_arrangement")),
            "employment_type": _first_employment_type(raw.get("ai_employment_type")),
            "seniority": None,  # actor gives a years-range (e.g. "2-5"), not a level enum
            "posted_at": _pick_posted(raw),
            "salary": _salary_from_ai_fields(raw),
            "contact": _contact_from_ai_fields(raw),
            "company_linkedin": _company_linkedin_from_slug(raw),
        }


def _company_linkedin_from_slug(raw: dict[str, Any]) -> str | None:
    """`org_linkedin_slug` (verified live: present on ~100% of postings,
    doesn't require `includeCompanyDetails`) is a bare slug like
    'nearmap-com', not a URL — build the actual company-page link. P2.6:
    this field was being fetched already and silently discarded."""
    slug = raw.get("org_linkedin_slug")
    if not isinstance(slug, str) or not slug.strip():
        return None
    return f"https://www.linkedin.com/company/{slug.strip()}"


def _first_location(raw: dict[str, Any]) -> str | None:
    for key in ("locations_derived", "cities_derived"):
        val = raw.get(key)
        if isinstance(val, list) and val:
            first = val[0]
            if isinstance(first, str) and first.strip():
                return first.strip()
    return None


def _remote_from_arrangement(arrangement: Any) -> bool | None:
    if not isinstance(arrangement, str) or not arrangement.strip():
        return None
    a = arrangement.strip().lower()
    if "remote" in a and "hybrid" not in a:
        return True
    if a in ("hybrid", "on-site", "onsite", "in-person", "in office"):
        return False
    return None


def _first_employment_type(val: Any) -> str | None:
    if isinstance(val, list) and val:
        first = str(val[0]).upper()
        return _EMPLOYMENT_MAP.get(first)
    return None


def _pick_posted(raw: dict[str, Any]) -> str | None:
    for key in ("date_posted", "date_created"):
        v = raw.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


# ai_salary_unit_text observed values not exhaustively verified live (the QA
# sample had all-null salary fields); normalized case-insensitively with a
# generous alias list. An unrecognized unit maps to None, which
# constraints.annual_inr() already treats as "don't reject on this salary" —
# safe by construction, never a false hard-reject.
_SALARY_UNIT_MAP = {
    "year": "year", "years": "year", "yearly": "year", "annual": "year", "annually": "year", "per year": "year",
    "month": "month", "months": "month", "monthly": "month", "per month": "month",
    "week": "week", "weeks": "week", "weekly": "week", "per week": "week",
    "day": "day", "days": "day", "daily": "day", "per day": "day",
    "hour": "hour", "hours": "hour", "hourly": "hour", "per hour": "hour",
}


def _salary_from_ai_fields(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Fantastic Jobs exposes ai_salary_min_value/ai_salary_max_value/
    ai_salary_value (single point)/ai_salary_currency/ai_salary_unit_text.
    Returns None (not an all-None dict) when nothing usable is present, so
    Job.salary stays None and constraints.annual_inr() skips it cleanly."""
    min_v = raw.get("ai_salary_min_value")
    max_v = raw.get("ai_salary_max_value")
    point_v = raw.get("ai_salary_value")
    if min_v is None and max_v is None and point_v is None:
        return None
    unit_raw = raw.get("ai_salary_unit_text")
    unit = _SALARY_UNIT_MAP.get(str(unit_raw).strip().lower()) if unit_raw else None
    return {
        "min": min_v if min_v is not None else point_v,
        "max": max_v if max_v is not None else point_v,
        "currency": raw.get("ai_salary_currency") or None,
        "unit": unit,
    }


def _contact_from_ai_fields(raw: dict[str, Any]) -> dict[str, Any] | None:
    """ai_hiring_manager_name/ai_hiring_manager_email_address are the only
    per-job contact fields this actor exposes (verified live: both were
    present, if null, in the QA sample). `org_linkedin_slug` is the
    COMPANY's LinkedIn, not a personal contact, so it is deliberately not
    mapped to contact.linkedin — no field for that was observed."""
    name = raw.get("ai_hiring_manager_name")
    email = raw.get("ai_hiring_manager_email_address")
    if not name and not email:
        return None
    return {"name": name or None, "linkedin": None, "email": email or None}


PROVIDER = FantasticJobsActorProvider()
