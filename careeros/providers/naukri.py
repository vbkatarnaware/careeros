"""Provider: Naukri / Apify actor (`memo23/naukri-scraper`) — v1.2 addition.

https://apify.com/memo23/naukri-scraper

Uses the shared Apify-actor mechanics in `_apify_actor_common.py`
(`validate_apify_token`, `run_actor`) rather than reimplementing token
rotation / dataset fetch / cost read-back — see that module for the
token-pool convention (`APIFY_TOKENS` comma-separated, or `APIFY_TOKEN`
single) and the `max_total_charge_usd` budget cap.

INPUT SCHEMA (verified live via Apify's public API this session — these are
the actor's real accepted input parameters): startUrls, platform,
searchQuery, location, maximumJobs, experienceLevel, jobType, workMode,
industry, roleCategory, companyType, timeFilter, minSalary,
includeDescription, cleanHtml.

OUTPUT SHAPE — VERIFIED LIVE 2026-07-10 (`careeros discover --provider naukri
--dry-run`, 3 real jobs, $0.0050 total, ~$0.0017/job — matches the audited
estimate). Naukri's real output is a DEEPLY NESTED shape, unlike the flat
candidate-key pattern most providers use: `title`/`url` are flat (handled by
the shared `pick_field`), but `company` only lives inside `companyDetail.name`
/ `basicInfo.companyName` (no flat "company"-style key), `location` is an
array of `{label, url}` dicts under `locations`, and `salaryDetail` is a
nested dict (`{currency, hideSalary, minimumSalary, maximumSalary}` — both
live samples had `hideSalary: true`, so the visible-salary shape itself is
inferred from the verified dict KEYS, not a live example with real numbers).
`_naukri_company`/`_naukri_location`/`_naukri_salary`/`_naukri_remote` below
handle this nesting explicitly rather than forcing it through the flat
candidate-key helpers.

KNOWN ACTOR QUIRK: run logs print a `[NK-VALIG]` prefix (shared codebase
branding with the `valig` Indeed actor, apparently) — cosmetic, not a bug.

RELEVANCE AND COST CONFIRMED AT SCALE (2026-07-11): a larger live sample
(n=10) scored 10/10 on-target Product Manager results, and cost across
n=1-20 stayed a flat ~$0.0005-0.005/run regardless of item count (this is
one of the lightweight HTTP-style actors, not a browser-driven one — see
`glassdoor.py`'s docstring for the contrast). Classified Optional: the
strongest single recommendation of the five Apify providers this project
ships.
"""

from __future__ import annotations

from typing import Any

from careeros.config import Config
from careeros.providers._apify_actor_common import run_actor, validate_apify_token
from careeros.providers._apify_common import TITLE_KEYS, URL_KEYS, pick_field
from careeros.providers.base import ProviderResult

ACTOR_ID = "memo23/naukri-scraper"

# provider_cfg key -> actor run_input key, for the optional pass-through
# filters. Only included in run_input when the user's config actually sets
# it — we don't send actor defaults we don't have an opinion on.
_OPTIONAL_FILTER_KEYS = {
    "work_mode": "workMode",
    "time_filter": "timeFilter",
    "experience_level": "experienceLevel",
    "job_type": "jobType",
    "industry": "industry",
    "role_category": "roleCategory",
    "company_type": "companyType",
    "min_salary": "minSalary",
}


def _naukri_company(raw: dict[str, Any]) -> str:
    company = (raw.get("companyDetail") or {}).get("name") or (raw.get("basicInfo") or {}).get("companyName")
    return company if isinstance(company, str) and company.strip() else "Unknown"


def _naukri_location(raw: dict[str, Any]) -> str | None:
    locs = raw.get("locations")
    if not isinstance(locs, list):
        return None
    labels = [loc.get("label") for loc in locs if isinstance(loc, dict) and isinstance(loc.get("label"), str) and loc["label"].strip()]
    return ", ".join(labels) if labels else None


def _naukri_remote(raw: dict[str, Any]) -> bool | None:
    label = raw.get("wfhLabel")
    if not isinstance(label, str) or not label.strip():
        return None
    lowered = label.lower()
    if "home" in lowered:
        return True
    if "office" in lowered:
        return False
    return None  # e.g. "Hybrid" — ambiguous, don't force a boolean


def _naukri_salary(raw: dict[str, Any]) -> dict[str, Any] | None:
    detail = raw.get("salaryDetail")
    if not isinstance(detail, dict) or detail.get("hideSalary"):
        return None
    lo, hi = detail.get("minimumSalary"), detail.get("maximumSalary")
    if not lo and not hi:
        return None
    return {"min": lo, "max": hi, "currency": detail.get("currency") or "INR", "unit": "year"}


def _build_run_input(provider_cfg: dict[str, Any], *, limit: int, search: str) -> dict[str, Any]:
    """Build the actor's run input. `search` (the CLI --search string)
    overrides `provider_cfg["search_query"]` when given; `limit` always wins
    over any configured value for `maximumJobs` (explicit call-time override
    convention, same as fantastic_jobs.py's `_build_params`)."""
    run_input: dict[str, Any] = {
        "searchQuery": search or (provider_cfg.get("search_query") or "Product Manager"),
        "location": provider_cfg.get("location") or "India",
        "maximumJobs": max(int(limit), 1),
        "cleanHtml": True,
    }
    for cfg_key, actor_key in _OPTIONAL_FILTER_KEYS.items():
        if cfg_key in provider_cfg and provider_cfg[cfg_key] is not None:
            run_input[actor_key] = provider_cfg[cfg_key]
    return run_input


class NaukriProvider:
    id = "naukri"

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
        return run_actor(
            self.id, config.apify, actor_id, run_input,
            max_cost_usd=provider_cfg.get("max_cost_usd"), careeros_dir=config.careeros_dir,
        )

    def to_job_dict(self, raw: dict[str, Any]) -> dict[str, Any] | None:
        title = pick_field(raw, TITLE_KEYS)
        url = pick_field(raw, URL_KEYS)
        if not title or not url.startswith("http"):
            return None

        return {
            "title": title,
            "company": _naukri_company(raw),
            "apply_url": url,
            "description": raw.get("shortDescription") or None,
            "location": _naukri_location(raw),
            "remote": _naukri_remote(raw),
            "employment_type": raw.get("employmentType") or None,
            "seniority": None,
            "posted_at": raw.get("createdDate") or None,
            "salary": _naukri_salary(raw),
            "contact": None,
            "company_linkedin": None,
        }


PROVIDER = NaukriProvider()
