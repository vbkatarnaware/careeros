"""Provider: Glassdoor / Apify actor (`memo23/glassdoor-scraper-ppr`) — v1.2
addition.

https://apify.com/memo23/glassdoor-scraper-ppr

NOT the same actor as the one flagged in this project's own audit
(`orgupdate/glassdoor-jobs-scraper`, ~$0.20/job — "avoid entirely, 40-500x
pricier than alternatives"). This provider uses a different, cheap actor
instead — do not "fix" `ACTOR_ID` below to the rejected one.

COST — this actor's cost is dominated by a large, near-fixed per-RUN
overhead (browser/proxy startup), not a per-item rate. A `--limit 3` trial
run is a misleading basis for $/job: verified live 2026-07-10 at n=3
($0.0050 total, ~$0.0017/job "looks cheap"), but small batches (n=3-10)
were separately observed running as high as ~$0.04-0.15/job once retries
factor in, and a combined multi-provider validation run on 2026-07-11 at
n=30 cost $0.4325 total (~$0.0144/job) — converging toward the original
fixed-actor audit estimate (~$0.005/job, seen at n=50) only once the run
amortizes over a real batch. Use `limit >= 20-30` for representative
economics; do not extrapolate $/job from a 3-item trial.

Uses the shared Apify-actor mechanics in `_apify_actor_common.py`
(`validate_apify_token`, `run_actor`) rather than reimplementing token
rotation / dataset fetch / cost read-back — see that module for the
token-pool convention (`APIFY_TOKENS` comma-separated, or `APIFY_TOKEN`
single) and the `max_total_charge_usd` budget cap.

INPUT SCHEMA (verified live via Apify's public API this session — these are
the actor's real accepted input parameters): startUrls, command,
searchJobsByKeyword, searchKeyword, searchLocation, sortBy, maxDaysOld,
remoteWorkType, applicationType, plus a set of review/interview-only fields
(includes, includeCompanyReviewStats, includeAllReviews,
monitoringModeForReviews, sortReviewsBy, reviewsStartDate) that are
irrelevant to job search and never set here.

This is a MULTI-PURPOSE actor — it also scrapes company reviews/interviews.
`searchJobsByKeyword` MUST be `True` on every call, or the actor runs in
review/interview mode instead of job search — see `_build_run_input` below,
which hardcodes it unconditionally, and the tests, which assert on it
directly.

No per-call result-count field (`limit`/`maxItems`) was found on this
actor's verified input schema. Rather than guess at an unverified field
name, `fetch()` below applies `limit` client-side by slicing `run_actor`'s
returned items after the call — see `_apply_limit`.

OUTPUT SHAPE — VERIFIED LIVE 2026-07-10 (`careeros discover --provider
glassdoor --dry-run`, 3 real, relevant Product Manager jobs) and again at
n=30 on 2026-07-11 (combined multi-provider validation run). `title`
("jobTitle"), `company` ("employer"), `location`, and `description` all
resolve correctly through the shared candidate-key pattern — no fix needed
for those. `salary` is a nested dict (`{min, max, currency, period}`) that
the flat candidate-key pattern can't reach; `_glassdoor_salary` below maps
it, translating `period` (`"ANNUAL"`/`"MONTHLY"`/etc.) to this project's
`unit` convention. No absolute posted-date field exists on this actor's
output (only a relative `ageInDays`) — `posted_at` is deliberately left
`None` rather than computing/inventing an absolute date from a relative one.

**Real bug found and fixed 2026-07-11**: at n=30, every item's `jobLink`/
`applyUrl` was a partner-tracking path RELATIVE to Glassdoor's own domain
(`/partner/jobListing.htm?...`), not an absolute URL — the earlier 3-item
sample happened to get absolute URLs, masking this. `to_job_dict` silently
dropped every item on the `url.startswith("http")` check (30 items in, 0
jobs out through `normalize`) before the fix below, which resolves a
relative URL against `https://www.glassdoor.com` via `urljoin` rather than
discarding the job.

Relevance for "Product Manager" was genuinely high at both n=3 and n=30
(Product Manager, Associate Product Manager, Startup Operator & Product
Execution Manager, etc.) — unlike Foundit/Indeed, this actor's search
quality is validated at production scale, not just a small sample.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from urllib.parse import urljoin

from careeros.config import Config
from careeros.providers._apify_actor_common import run_actor, validate_apify_token
from careeros.providers._apify_common import (
    COMPANY_KEYS, DESCRIPTION_KEYS, TITLE_KEYS, URL_KEYS, pick_field,
)
from careeros.providers.base import ProviderResult

ACTOR_ID = "memo23/glassdoor-scraper-ppr"

_BASE_URL = "https://www.glassdoor.com"

_PERIOD_UNIT = {"ANNUAL": "year", "MONTHLY": "month", "WEEKLY": "week", "DAILY": "day", "HOURLY": "hour"}


def _glassdoor_salary(raw: dict[str, Any]) -> dict[str, Any] | None:
    salary = raw.get("salary")
    if not isinstance(salary, dict):
        return None
    lo, hi = salary.get("min"), salary.get("max")
    if not lo and not hi:
        return None
    period = salary.get("period")
    unit = _PERIOD_UNIT.get(period, period.lower() if isinstance(period, str) else None)
    return {"min": lo, "max": hi, "currency": salary.get("currency"), "unit": unit}


def _build_run_input(provider_cfg: dict[str, Any], *, limit: int, search: str) -> dict[str, Any]:
    """Build the actor's run input. `search` (the CLI --search string)
    overrides `provider_cfg["search_keyword"]` when given.

    `searchJobsByKeyword` is unconditionally `True` — CRITICAL: this actor
    also does review/interview scraping, and this flag is the only thing
    that puts it into job-search mode. `limit` is intentionally NOT wired
    into run_input (no matching field was found on the verified schema) —
    see `_apply_limit`, applied client-side in `fetch()` instead.
    """
    run_input: dict[str, Any] = {
        "searchJobsByKeyword": True,  # CRITICAL: job-search mode, not reviews/interviews
        "searchKeyword": search or (provider_cfg.get("search_keyword") or "Product Manager"),
        "searchLocation": provider_cfg.get("search_location") or "India",
        "maxDaysOld": str(provider_cfg.get("max_days_old") or "7"),
    }
    if provider_cfg.get("remote_work_type"):
        run_input["remoteWorkType"] = provider_cfg["remote_work_type"]
    if provider_cfg.get("sort_by"):
        run_input["sortBy"] = provider_cfg["sort_by"]
    if provider_cfg.get("application_type"):
        run_input["applicationType"] = provider_cfg["application_type"]
    return run_input


def _apply_limit(result: ProviderResult, limit: int) -> ProviderResult:
    """Client-side cap on returned items — this actor's verified input
    schema has no per-call result-count field, so `limit` can't be pushed
    into run_input the way `maximumJobs`/`limit` work on other actors.
    Slices `items` and updates `records` to match; every other field
    (cost_usd, requests, seconds, warnings, errors, skipped, skip_reason)
    is left exactly as `run_actor` returned it — the actor still ran (and
    was billed) for its own full result set, this only trims what
    downstream normalize.py sees."""
    if limit is None or len(result.items) <= limit:
        return result
    trimmed = result.items[:limit]
    return replace(result, items=trimmed, records=len(trimmed))


class GlassdoorProvider:
    id = "glassdoor"

    def validate(self, config: Config) -> list[str]:
        return validate_apify_token(config.apify)

    def fetch(
        self, config: Config, *, limit: int = 100, search: str = "",
        query: dict[str, Any] | None = None,
    ) -> ProviderResult:
        """`query` (the segmented-discovery spec kwarg from
        pipeline/queryplan.py) is accepted but intentionally ignored — this
        actor doesn't have a documented multi-field query mapping, and
        inventing one on an unverified output shape isn't worth the added
        complexity. Callers may still pass it; it's a no-op here."""
        provider_cfg = config.providers.get(self.id, {}) or {}
        run_input = _build_run_input(provider_cfg, limit=limit, search=search)
        actor_id = provider_cfg.get("actor", ACTOR_ID)
        result = run_actor(
            self.id, config.apify, actor_id, run_input,
            max_cost_usd=provider_cfg.get("max_cost_usd"),
        )
        return _apply_limit(result, limit)

    def to_job_dict(self, raw: dict[str, Any]) -> dict[str, Any] | None:
        title = pick_field(raw, TITLE_KEYS)
        url = pick_field(raw, URL_KEYS)
        if url.startswith("/"):
            # Verified live 2026-07-11 (30-item batch): jobLink/applyUrl on
            # this actor are partner-tracking paths relative to Glassdoor's
            # own domain ("/partner/jobListing.htm?..."), not absolute URLs
            # — a small earlier 3-item sample happened to get absolute ones.
            # Resolve against the site root rather than dropping the job.
            url = urljoin(_BASE_URL, url)
        if not title or not url.startswith("http"):
            return None

        return {
            "title": title,
            "company": pick_field(raw, COMPANY_KEYS, fallback="Unknown"),
            "apply_url": url,
            "description": pick_field(raw, DESCRIPTION_KEYS) or None,
            "location": pick_field(raw, ["location", "job_location", "city"]) or None,
            "remote": None,  # no reliable remote/onsite signal in the verified live shape
            "employment_type": None,
            "seniority": None,
            "posted_at": None,  # only a relative ageInDays exists — no absolute date to map
            "salary": _glassdoor_salary(raw),
            "contact": None,
            "company_linkedin": None,
        }


PROVIDER = GlassdoorProvider()
