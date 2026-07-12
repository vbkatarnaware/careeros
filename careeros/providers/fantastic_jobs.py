"""Provider: Fantastic Jobs — official REST API (P2.7 migration, P2.8-frozen).

https://developer.fantastic.jobs/documentation/how-fantastic-jobs-api-works
https://developer.fantastic.jobs/api/new-jobs

The default, maintained Fantastic Jobs provider. Same underlying dataset and
the SAME field names as the legacy Apify actor
(careeros/providers/legacy/fantastic_jobs_actor.py, registered as
`fantastic-jobs-actor`) — confirmed during the P2.6/P2.7 architecture review:
`.careeros/qa/sample_raw.json` is already API-shaped (id, source,
source_type, title, organization, url, locations_derived,
ai_work_arrangement, ai_salary_*, org_linkedin_slug, ...). `to_job_dict()`
below is therefore copied verbatim from the actor provider — this migration
only changes fetch(); normalize/queryplan/gate/evaluate/threshold/artifacts
never know the difference.

Two commercial transports, one architecture (config.api.transport):
  - "direct"   — https://data.fantastic.jobs (developer.fantastic.jobs)
  - "rapidapi" — RapidAPI's "Active Jobs DB" listing (same vendor, proxied)
Both return the identical Fantastic Jobs dataset; they differ only in base
URL and auth header (see `_base_url_and_headers()`). Which is cheaper/has a
usable free tier is a config/commercial decision, deliberately NOT hardcoded
here — see the architecture review. `transport` has no default: an unset
value fails fast with a clear message rather than silently preferring either
vendor.

Endpoints & the P2.8-frozen default (config.api.endpoint):
  - "both" (DEFAULT) — queries active-ats (career sites/ATS) AND active-jb
    (+ LinkedIn/YC/Wellfound) every run, splitting the per-tier record
    allocation 50/50 so it costs the SAME quota as one endpoint. The P2.8
    Final Discovery Acceptance Audit (full 107-job population,
    `.careeros/qa/acceptance_audit_report.md`) found the two sources score a
    statistically identical ~8% >=4.0 rate but are 92% disjoint, so "both"
    roughly doubles interview-worthy jobs found at the same cost.
  - "active-ats" / "active-jb" — a single source (halves the sources, same
    per-tier record count). Selectable but no longer the recommended default.
Discovery is frozen on "both" (P2.8); see the roadmap. Each fetch is
single-page (up to the endpoint's split of `limit`); cursor pagination and
incremental `date_created_gte` sync remain out of scope (roadmap).

LIVE-VERIFIED (P2.8): fetch() was exercised against a real direct-transport
key — active-ats, active-jb, and "both" all return a bare JSON array of job
objects that `to_job_dict()` maps cleanly (see the acceptance audit). Two
transports, one architecture; "rapidapi" shares the same response shape.
"""

from __future__ import annotations

import os
from typing import Any

import requests

from careeros.config import Config
from careeros.providers._apify_common import (
    COMPANY_KEYS, DESCRIPTION_KEYS, TITLE_KEYS, URL_KEYS, pick_field,
)
from careeros.providers.base import ProviderError, ProviderResult

_DIRECT_BASE_URL = "https://data.fantastic.jobs"
_RAPIDAPI_HOST_DEFAULT = "active-jobs-db.p.rapidapi.com"
_TIMEOUT_S = 60.0  # matches the API's own documented request timeout

# ai_employment_type enum -> Job.employment_type enum (identical to the actor)
_EMPLOYMENT_MAP = {
    "FULL_TIME": "full_time",
    "PART_TIME": "part_time",
    "CONTRACT": "contract",
    "CONTRACTOR": "contract",
    "TEMPORARY": "contract",
    "INTERN": "internship",
    "INTERNSHIP": "internship",
}


def _or_exclude_param(terms: list[str], exclusions: list[str]) -> str | None:
    """Both `title` and `location` are documented as supporting `OR` between
    terms and a `-term` exclusion syntax IN THE SAME PARAM (no separate
    exclusion param exists server-side) — so this builds one string for
    either field, not a `_advanced` boolean expression (kept minimal; no
    unverified boolean-syntax assumptions)."""
    included = [t for t in terms if t]
    excluded = [f"-{t}" for t in exclusions if t]
    if not included and not excluded:
        return None
    value = " OR ".join(included) if included else ""
    if excluded:
        value = f"{value} {' '.join(excluded)}".strip()
    return value


def _merge_query(api_cfg: dict[str, Any], query: dict[str, Any] | None) -> dict[str, Any]:
    """Same overlay pattern as the legacy actor's `_merge_query`: layers a
    segmented-discovery spec (pipeline/queryplan.py) onto the base api
    config for one `fetch()` call. `_work_mode` is debug-only, dropped here."""
    if not query:
        return api_cfg
    return {**api_cfg, **{k: v for k, v in query.items() if not k.startswith("_")}}


def _base_url_and_headers(api_cfg: dict[str, Any]) -> tuple[str, dict[str, str]]:
    transport = api_cfg.get("transport")
    if transport == "direct":
        key_env = api_cfg.get("api_key_env", "FANTASTIC_API_KEY")
        key = os.environ.get(key_env)
        if not key:
            raise ProviderError(
                f"No direct Fantastic.jobs API key configured — set {key_env}. "
                "See providers/README.md."
            )
        base_url = api_cfg.get("base_url") or _DIRECT_BASE_URL
        return base_url, {"Authorization": f"Bearer {key}"}
    if transport == "rapidapi":
        key_env = api_cfg.get("rapidapi_key_env", "RAPIDAPI_KEY")
        key = os.environ.get(key_env)
        if not key:
            raise ProviderError(
                f"No RapidAPI key configured — set {key_env}. See providers/README.md."
            )
        host = api_cfg.get("rapidapi_host") or _RAPIDAPI_HOST_DEFAULT
        base_url = api_cfg.get("rapidapi_base_url") or f"https://{host}"
        return base_url, {"X-RapidAPI-Key": key, "X-RapidAPI-Host": host}
    raise ProviderError(
        'config.api.transport is not set — choose "direct" (developer.fantastic.jobs) '
        'or "rapidapi" (RapidAPI marketplace). This is a config/commercial choice, not '
        "an architectural one; see providers/README.md."
    )


def _build_params(api_cfg: dict[str, Any], *, limit: int, search: str) -> dict[str, Any]:
    """Mirrors the legacy actor's `_build_run_input`, mapped onto the
    official API's own param names (per developer.fantastic.jobs's
    /api/new-jobs reference)."""
    title_search = [search] if search else list(api_cfg.get("title_search", []) or [])
    location_search = list(api_cfg.get("location_search", []) or [])
    title_exclusion = list(api_cfg.get("title_exclusion_search", []) or [])
    location_exclusion = list(api_cfg.get("location_exclusion_search", []) or [])
    work_arrangement = list(api_cfg.get("work_arrangement", []) or [])
    has_salary = api_cfg.get("has_salary")
    remove_agency = api_cfg.get("remove_agency", True)

    params: dict[str, Any] = {
        "limit": max(int(limit), 1),
        "time_frame": api_cfg.get("time_range", "7d"),
        "description_format": "text",
    }
    title = _or_exclude_param(title_search, title_exclusion)
    if title:
        params["title"] = title
    location = _or_exclude_param(location_search, location_exclusion)
    if location:
        params["location"] = location
    if work_arrangement:
        params["ai_work_arrangement"] = work_arrangement
    if has_salary is not None:
        params["has_salary"] = bool(has_salary)
    if remove_agency:
        # Actor parity: removeAgency=True -> exclude. False means "don't
        # filter" (no agency-only mode is exercised here), so the param is
        # simply omitted rather than sent as "only".
        params["organization_agency"] = "exclude"
    return params


_BOTH_ENDPOINTS = ("active-ats", "active-jb")


def _endpoint_limits(effective_cfg: dict[str, Any], endpoints: tuple[str, ...], total_limit: int) -> dict[str, int]:
    """Split the per-tier record allocation across the active endpoints. The
    P2.8-frozen default is an EQUAL split (50/50 for "both"), so "both" costs
    the SAME total records as a single endpoint — the two sources share the
    weekly quota rather than doubling it. Users on a paid plan can override the
    split via `api.endpoint_allocation` (e.g. {"active-ats": 0.3, "active-jb":
    0.7}); weights are normalized. Deterministic — no auto-rebalancing (that's
    P3)."""
    if len(endpoints) == 1:
        return {endpoints[0]: max(1, total_limit)}
    alloc = effective_cfg.get("endpoint_allocation") or {}
    weights = {ep: float(alloc.get(ep, 1.0)) for ep in endpoints}
    wsum = sum(weights.values()) or float(len(endpoints))
    return {ep: max(1, int(round(total_limit * weights[ep] / wsum))) for ep in endpoints}


def _fetch_one_endpoint(
    base_url: str, headers: dict[str, str], endpoint: str, params: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, str] | None]:
    """Every failure mode is classified into a plain-English cause + next
    action (P2.9) — never a generic "request failed"/"HTTP 500" dead end.
    Weekly RECORD quota exhaustion is caught earlier, pre-call, by
    `budget.check_before_run` (that's OUR guard); the cases here are what the
    API itself reports mid-call: invalid/expired key, its own request/job
    quota, transient rate-limiting, and network/service outages — each
    distinct because the user's next action differs (rotate a key vs. wait
    for a reset vs. just retry)."""
    try:
        resp = requests.get(
            f"{base_url}/v1/{endpoint}", headers=headers, params=params, timeout=_TIMEOUT_S,
        )
    except requests.Timeout as e:
        raise ProviderError(
            f"fantastic-jobs ({endpoint}): timed out reaching Fantastic Jobs — this looks like a "
            "network or service outage, not a configuration problem. Retry in a few minutes."
        ) from e
    except requests.ConnectionError as e:
        raise ProviderError(
            f"fantastic-jobs ({endpoint}): couldn't connect to Fantastic Jobs — this looks like a "
            "network or service outage, not a configuration problem. Check your connection and "
            "retry later."
        ) from e
    except requests.RequestException as e:
        raise ProviderError(f"fantastic-jobs ({endpoint}): request failed — {e}") from e

    if resp.status_code in (401, 403):
        raise ProviderError(
            f"fantastic-jobs ({endpoint}): API key rejected (HTTP {resp.status_code}) — your "
            "FANTASTIC_API_KEY (or RAPIDAPI_KEY) is invalid, expired, or lacks access. Check/rotate "
            "it — see providers/README.md."
        )
    if resp.status_code == 429:
        # x-ratelimit-*-remaining headers (per Fantastic Jobs' own docs)
        # distinguish a hard billing-period quota from ordinary transient
        # rate-limiting — the next action differs (wait for reset/upgrade vs.
        # just retry), so don't collapse them into one message.
        remaining_requests = resp.headers.get("x-ratelimit-requests-remaining")
        remaining_jobs = resp.headers.get("x-ratelimit-jobs-remaining")
        if remaining_requests == "0":
            raise ProviderError(
                f"fantastic-jobs ({endpoint}): request quota exhausted for this billing period "
                "(HTTP 429, x-ratelimit-requests-remaining: 0) — wait for it to reset, or upgrade "
                "your plan."
            )
        if remaining_jobs == "0":
            raise ProviderError(
                f"fantastic-jobs ({endpoint}): job/record quota exhausted for this billing period "
                "(HTTP 429, x-ratelimit-jobs-remaining: 0) — wait for it to reset, or upgrade your "
                "plan. (CareerOS's own weekly record guard should normally catch this before it "
                "happens — see `careeros doctor`.)"
            )
        raise ProviderError(
            f"fantastic-jobs ({endpoint}): rate limited (HTTP 429) — usually transient. "
            "Wait a moment and retry."
        )
    if 500 <= resp.status_code < 600:
        raise ProviderError(
            f"fantastic-jobs ({endpoint}): Fantastic Jobs returned a server error "
            f"(HTTP {resp.status_code}) — this looks like a service outage on their end, not a "
            "configuration problem. Retry later."
        )
    if resp.status_code != 200:
        raise ProviderError(f"fantastic-jobs ({endpoint}): HTTP {resp.status_code} — {resp.text[:300]}")

    items = resp.json()
    if not isinstance(items, list):
        raise ProviderError(f"fantastic-jobs ({endpoint}): unexpected response shape (expected a JSON array)")

    # Read the LIVE quota headers on the success path too, not just 429 —
    # this is the real, provider-verified remaining quota (never a locally
    # calculated guess), surfaced via ProviderResult so `doctor`/the
    # discovery summary can show it instead of only the local weekly
    # counter (see AGENT_GUIDE.md: verify against the live source).
    live_quota = None
    remaining_requests = resp.headers.get("x-ratelimit-requests-remaining")
    remaining_jobs = resp.headers.get("x-ratelimit-jobs-remaining")
    if remaining_requests is not None or remaining_jobs is not None:
        live_quota = {
            "requests_remaining": remaining_requests,
            "jobs_remaining": remaining_jobs,
        }
    return items, live_quota


class FantasticJobsProvider:
    id = "fantastic-jobs"

    def validate(self, config: Config) -> list[str]:
        """Config/credential problems only — no network call (doctor/discover
        both need this to be free and instant). Reuses `_base_url_and_headers`
        purely for its validation logic by calling it and catching the
        `ProviderError` it already raises for an unset transport or a missing
        key, rather than duplicating that logic here."""
        try:
            _base_url_and_headers(config.api)
        except ProviderError as e:
            return [str(e)]
        return []

    def fetch(
        self, config: Config, *, limit: int = 100, search: str = "",
        query: dict[str, Any] | None = None,
    ) -> ProviderResult:
        """Single-page REST fetch — see the module docstring for the
        deliberate parity scope (no pagination/incremental sync in P2.7).

        `config.api.endpoint` selects the source: "active-ats" (career
        sites/ATS), "active-jb" (+ LinkedIn/YC/Wellfound), or **"both"**
        (P2.8 production default — see the Final Discovery Acceptance Audit,
        `.careeros/qa/acceptance_audit_report.md`: on a full 107-job
        population, ats and jb scored a statistically identical ~8% >=4.0
        rate but are 92% disjoint, so querying both roughly DOUBLES the
        interview-worthy jobs found per run at the same per-job quality).

        For "both", the per-tier record allocation `limit` is SPLIT across the
        two endpoints (50/50 by default; see `_endpoint_limits`), so "both"
        consumes the same total records as a single endpoint — the sources
        share the weekly quota, they don't double it. Each endpoint is one
        HTTP call; the raw union is returned, and downstream normalize+dedupe
        (job-id-keyed) collapses the small real overlap (no extra dedup here).

        `cost_usd` on the returned `ProviderResult` is always 0.0: unlike the
        Apify actor's pay-per-result billing, both REST transports are
        subscription/credit-metered, not priced per call, so there is no
        real per-call USD figure to report (0.0 is the documented contract
        for a non-metered-per-call source — see providers/base.py)."""
        import time as _time
        start = _time.time()
        api_cfg = config.api
        effective_cfg = _merge_query(api_cfg, query)
        base_url, headers = _base_url_and_headers(effective_cfg)
        endpoint = effective_cfg.get("endpoint", "both")

        endpoints = _BOTH_ENDPOINTS if endpoint == "both" else (endpoint,)
        ep_limits = _endpoint_limits(effective_cfg, endpoints, limit)
        items: list[dict[str, Any]] = []
        live_quota: dict[str, str] | None = None
        for ep in endpoints:
            ep_params = _build_params(effective_cfg, limit=ep_limits[ep], search=search)
            ep_items, ep_live_quota = _fetch_one_endpoint(base_url, headers, ep, ep_params)
            items.extend(ep_items)
            if ep_live_quota is not None:
                live_quota = ep_live_quota  # last endpoint's reading wins (freshest)
        return ProviderResult(
            provider=self.id, items=items, cost_usd=0.0,
            requests=len(endpoints), records=len(items),
            seconds=_time.time() - start,
            live_quota=live_quota,
        )

    def to_job_dict(self, raw: dict[str, Any]) -> dict[str, Any] | None:
        # Copied verbatim from the legacy actor provider — same dataset,
        # same field names (see module docstring).
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
            # API's own `seniority`/`ai_experience_level` fields are a future
            # enrichment, not in P2.7's parity scope — matches the actor.
            "seniority": None,
            "posted_at": _pick_posted(raw),
            "salary": _salary_from_ai_fields(raw),
            "contact": _contact_from_ai_fields(raw),
            "company_linkedin": _company_linkedin_from_slug(raw),
        }


# ── field mappers — copied verbatim from providers/legacy/fantastic_jobs_actor.py ──

def _company_linkedin_from_slug(raw: dict[str, Any]) -> str | None:
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


_SALARY_UNIT_MAP = {
    "year": "year", "years": "year", "yearly": "year", "annual": "year", "annually": "year", "per year": "year",
    "month": "month", "months": "month", "monthly": "month", "per month": "month",
    "week": "week", "weeks": "week", "weekly": "week", "per week": "week",
    "day": "day", "days": "day", "daily": "day", "per day": "day",
    "hour": "hour", "hours": "hour", "hourly": "hour", "per hour": "hour",
}


def _salary_from_ai_fields(raw: dict[str, Any]) -> dict[str, Any] | None:
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
    name = raw.get("ai_hiring_manager_name")
    email = raw.get("ai_hiring_manager_email_address")
    if not name and not email:
        return None
    return {"name": name or None, "linkedin": None, "email": email or None}


PROVIDER = FantasticJobsProvider()
