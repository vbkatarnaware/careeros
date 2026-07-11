"""Provider: ZipRecruiter / Apify actor (crawlerbros/ziprecruiter-scraper-pro).

https://apify.com/crawlerbros/ziprecruiter-scraper-pro

RELIABILITY CAVEAT (audited this session, not something to "fix" here): this
actor has a good review history (21 reviews, 5.0*) but only a ~63% run-success
rate over its last 1,153 runs — roughly 1 in 3 runs fails or returns nothing
usable. This is why the provider config keeps it `enabled: false` by default
and expects a manual trial before real use. `fetch()` here is written so a
failed/empty run surfaces a clear result (a `ProviderError` propagated from
`run_actor`, or a `ProviderResult` with `records=0`) rather than crashing
oddly or being silently swallowed — expect this provider to error out or
return 0 items more often than the other providers.

Input schema verified live against Apify's API this session: `startUrls`
(array), `search` (string), `location` (string), `jobType` (string),
`daysPosted` (integer), `radiusMiles` (integer), `remoteOnly` (boolean),
`maxItems` (integer — the limit), `proxyConfiguration` (object, left at
actor default here).

OUTPUT SHAPE — VERIFIED LIVE 2026-07-10 (`careeros discover --provider
ziprecruiter --dry-run`, 3 real, relevant Product Manager jobs). `title`,
`url`, `company`, `location`, `description` all already resolve correctly
through the shared candidate-key pattern. Only real gap: `salary` — present
in just 1 of 3 live samples, but with a clean, already-numeric shape
(`salaryMin`/`salaryMax`/`salaryPeriod`); `_zip_salary` below maps it,
assuming `currency: "USD"` since this actor is US-only (same convention as
`remoteok.py` asserting a platform-implicit currency). No employment-type
or absolute posted-date field was found in the live sample — both left
`None` rather than guessed.

COST — CORRECTED after a larger live sample (important: the first
verification below was misleading). The initial 3-item trial cost $0.1585
for 3 items (~$0.053/job) and took ~179s — which looked like ~26x the
originally-audited ~$0.002/job. That comparison was an artifact of the
sample size, not this actor's real economics: this actor bills for real
browser/compute time PER RUN (headful rendering), which is a large,
near-fixed cost that a 3-item run barely amortizes. A 2026-07-11 combined
multi-provider validation run at `limit: 30` cost $0.1252 for 30 items
(~$0.0042/job, 244.5s) — squarely comparable to Glassdoor, not a cost
outlier. **Judge this actor's cost from a `limit >= 20-30` run, never a
`--limit 3` trial.** Relevance across every live sample this session (n=5,
5, 15, 30) was consistently high — the best of the five Apify providers
tested.

RELIABILITY (the caveat that actually holds): the documented ~63% actor
run-success rate reproduced live once during this session's validation — a
`limit: 30` run returned 0 items for a small charge ($0.0275), then an
immediate retry succeeded (30/30 relevant items, $0.1252). Budget for an
occasional wasted small charge and a retry, not for a fundamentally
expensive or irrelevant source.
"""

from __future__ import annotations

from typing import Any

from careeros.config import Config
from careeros.providers._apify_actor_common import run_actor, validate_apify_token
from careeros.providers._apify_common import (
    COMPANY_KEYS, DESCRIPTION_KEYS, TITLE_KEYS, URL_KEYS, pick_field,
)
from careeros.providers.base import ProviderResult

ACTOR_ID = "crawlerbros/ziprecruiter-scraper-pro"


def _zip_salary(raw: dict[str, Any]) -> dict[str, Any] | None:
    lo, hi = raw.get("salaryMin"), raw.get("salaryMax")
    if not lo and not hi:
        return None
    return {"min": lo, "max": hi, "currency": "USD", "unit": raw.get("salaryPeriod") or None}


def _build_run_input(provider_cfg: dict[str, Any], *, limit: int, search: str) -> dict[str, Any]:
    """Build the actor's run input from config-driven defaults + the `search`
    kwarg override. `search` (the CLI --search string or a segmented-discovery
    query's search term) overrides `provider_cfg.get("search")`, which itself
    falls back to a fixed default — ZipRecruiter is US-focused, so `location`
    defaults to "United States" rather than being left unset.
    """
    run_input: dict[str, Any] = {
        "search": search or (provider_cfg.get("search") or "Product Manager"),
        "location": provider_cfg.get("location") or "United States",
        "maxItems": max(int(limit), 1),
    }
    if provider_cfg.get("remote_only"):
        run_input["remoteOnly"] = True
    if provider_cfg.get("days_posted"):
        run_input["daysPosted"] = provider_cfg["days_posted"]
    return run_input


class ZipRecruiterProvider:
    id = "ziprecruiter"

    def validate(self, config: Config) -> list[str]:
        """Config/credential problems only — delegates entirely to the
        shared Apify-actor-provider check (no ZipRecruiter-specific
        credential of its own)."""
        return validate_apify_token(config.apify)

    def fetch(
        self, config: Config, *, limit: int = 100, search: str = "",
        query: dict[str, Any] | None = None,
    ) -> ProviderResult:
        """`query` (a segmented-discovery spec, see pipeline/queryplan.py) is
        accepted for interface parity with other providers but not merged
        here — this actor's input schema (search/location/maxItems) is too
        narrow to benefit from per-work-mode segmentation the way the
        Fantastic Jobs actor's titleSearch/locationSearch arrays do, so it is
        deliberately ignored rather than force-fit.

        Raises `careeros.providers.base.ProviderError` (propagated from
        `run_actor`, unmodified) if every configured Apify token fails or
        none is configured — including the elevated odds of that happening
        on this specific actor, per the reliability caveat in this module's
        docstring.
        """
        provider_cfg = config.providers.get(self.id, {}) or {}
        run_input = _build_run_input(provider_cfg, limit=limit, search=search)
        actor_id = provider_cfg.get("actor", ACTOR_ID)
        return run_actor(
            self.id, config.apify, actor_id, run_input,
            max_cost_usd=provider_cfg.get("max_cost_usd"),
        )

    def to_job_dict(self, raw: dict[str, Any]) -> dict[str, Any] | None:
        title = pick_field(raw, TITLE_KEYS)
        url = pick_field(raw, URL_KEYS)
        if not title or not url.startswith("http"):
            return None

        return {
            "title": title,
            "company": pick_field(raw, COMPANY_KEYS, fallback="Unknown"),
            "apply_url": url,
            "description": pick_field(raw, DESCRIPTION_KEYS) or None,
            "location": pick_field(raw, ["location", "job_location", "city"]) or None,
            "remote": None,  # no reliable remote/onsite signal in the verified live shape
            "employment_type": None,  # no employment-type field found in the verified live sample
            "seniority": None,
            "posted_at": pick_field(raw, ["date_posted", "posted_date", "postedDate", "date"]) or None,
            "salary": _zip_salary(raw),
            "contact": None,
            "company_linkedin": None,
        }


PROVIDER = ZipRecruiterProvider()
