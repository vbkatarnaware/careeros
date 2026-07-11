"""Provider: Indeed / Apify actor (`valig/indeed-jobs-scraper`) — v1.2
addition.

https://apify.com/valig/indeed-jobs-scraper

Indeed is a broad general job-board aggregator — like Fantastic Jobs (this
project's default discovery source), so expect meaningful listing overlap
and a real dedupe burden between the two rather than mostly-fresh coverage.

INPUT SCHEMA verified live against the actor's Apify API listing: `country`
(string), `title` (string, job title search), `location` (string), `limit`
(integer), `datePosted` (string, optional recency filter — accepted values
not verified, treated as an opaque passthrough).

OUTPUT SHAPE — VERIFIED LIVE 2026-07-10 (`careeros discover --provider indeed
--dry-run`, 3 real jobs, $0.0010 total, ~$0.0003/job). `title`/`url` resolve
through the shared candidate-key pattern, but everything else is nested and
needed dedicated mapping: `company` only lives at `employer.name`;
`description` is a `{html, text}` dict, not a string; `location` is a dict
(`city`/`admin1Code`/`countryName`, no single pre-joined string);
`baseSalary` is a dict (`currencyCode`/`min`/`max`/`unitOfWork`); employment
type lives in a `jobTypes` dict keyed by code (`{"code": "label"}` — we take
the first label); posted date is `datePublished`/`dateOnIndeed` at the top
level. `_indeed_company`/`_indeed_description`/`_indeed_location`/
`_indeed_salary`/`_indeed_employment_type`/`_indeed_posted` below handle
this.

CAVEAT — data relevance is QUERY-DEPENDENT, root-caused 2026-07-11 by
varying the search term (not a CareerOS query bug, and not a broken
integration): a live "Software Engineer" search returned genuinely on-target
results (Azure/AWS Cloud Engineer, Sr. Developer, etc.), proving the actor
does honor `title` server-side. But the earlier live finding — an initial
"Product Manager" search returning unrelated roles (Sales Manager,
E-Commerce GM, Performance Marketing Manager) — reproduced at a larger
n=20 sample too (~10% relevant), consistent with the actor broad-matching
on the word "Manager" across unrelated domains rather than honoring the
full phrase. CareerOS's own default `title_search` is "Product Manager",
so this is a real practical limitation for THIS project even though the
actor works correctly for less ambiguous, single-concept titles. Classified
Experimental (not Not Recommended): enable only if you retarget
`search_keyword` away from "Product Manager", or re-verify after an actor
update.
"""

from __future__ import annotations

from typing import Any

from careeros.config import Config
from careeros.providers._apify_actor_common import run_actor, validate_apify_token
from careeros.providers._apify_common import TITLE_KEYS, URL_KEYS, pick_field
from careeros.providers.base import ProviderResult

ACTOR_ID = "valig/indeed-jobs-scraper"


def _indeed_company(raw: dict[str, Any]) -> str:
    name = (raw.get("employer") or {}).get("name")
    return name if isinstance(name, str) and name.strip() else "Unknown"


def _indeed_description(raw: dict[str, Any]) -> str | None:
    desc = raw.get("description")
    if isinstance(desc, dict):
        text = desc.get("text") or desc.get("html")
        return text.strip() if isinstance(text, str) and text.strip() else None
    if isinstance(desc, str) and desc.strip():
        return desc.strip()
    return None


def _indeed_location(raw: dict[str, Any]) -> str | None:
    loc = raw.get("location")
    if not isinstance(loc, dict):
        return None
    parts = [loc.get("city"), loc.get("admin1Code"), loc.get("countryName")]
    parts = [p for p in parts if isinstance(p, str) and p.strip()]
    return ", ".join(parts) if parts else None


def _indeed_salary(raw: dict[str, Any]) -> dict[str, Any] | None:
    base = raw.get("baseSalary")
    if not isinstance(base, dict):
        return None
    lo, hi = base.get("min"), base.get("max")
    if not lo and not hi:
        return None
    unit = base.get("unitOfWork")
    return {"min": lo, "max": hi, "currency": base.get("currencyCode"), "unit": unit.lower() if isinstance(unit, str) else None}


def _indeed_employment_type(raw: dict[str, Any]) -> str | None:
    job_types = raw.get("jobTypes")
    if isinstance(job_types, dict) and job_types:
        first = next(iter(job_types.values()), None)
        return first if isinstance(first, str) and first.strip() else None
    return None


def _indeed_posted(raw: dict[str, Any]) -> str | None:
    for key in ("datePublished", "dateOnIndeed"):
        v = raw.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _build_run_input(provider_cfg: dict[str, Any], *, limit: int, search: str) -> dict[str, Any]:
    """Build the actor's run input from this provider's config block +
    call-time overrides. `search` (the CLI --search string) overrides
    provider_cfg's configured title when given; `limit` (the caller's
    resolved record cap) always wins over any configured limit."""
    run_input: dict[str, Any] = {
        "title": search or (provider_cfg.get("title") or "Product Manager"),
        "location": provider_cfg.get("location") or "India",
        "country": provider_cfg.get("country") or "in",
        "limit": max(int(limit), 1),
    }
    date_posted = provider_cfg.get("date_posted")
    if date_posted:
        run_input["datePosted"] = date_posted
    return run_input


class IndeedProvider:
    id = "indeed"

    def validate(self, config: Config) -> list[str]:
        """Config/credential problems only — delegates entirely to the
        shared Apify-actor-provider helper (no network call)."""
        return validate_apify_token(config.apify)

    def fetch(
        self, config: Config, *, limit: int = 100, search: str = "",
        query: dict[str, Any] | None = None,
    ) -> ProviderResult:
        """`query` (a segmented-discovery spec from pipeline/queryplan.py) is
        accepted but not merged in — this actor's input surface (title/
        location/country/limit/datePosted) doesn't map onto the segmented
        per-work-mode query shape the way Fantastic Jobs' filter-heavy input
        does, so a straightforward merge isn't natural here; kept simple and
        ignored rather than force-fit."""
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
            "company": _indeed_company(raw),
            "apply_url": url,
            "description": _indeed_description(raw),
            "location": _indeed_location(raw),
            "remote": None,  # no reliable remote/onsite signal in the verified live shape
            "employment_type": _indeed_employment_type(raw),
            "seniority": None,
            "posted_at": _indeed_posted(raw),
            "salary": _indeed_salary(raw),
            "contact": None,
            "company_linkedin": None,
        }


PROVIDER = IndeedProvider()
