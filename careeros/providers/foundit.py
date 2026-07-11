"""Provider: Foundit / Apify actor (`shahidirfan/Foundit-Jobs-Scraper`) —
v1.2 actor-based source.

Foundit is the rebranded name for Monster India (same company, same job
board — renamed in 2021), so it's an India-focused source that matches this
project's target market.

INPUT SCHEMA (verified live via Apify's public actor API this session):
`url` (string), `keyword` (string), `location` (string), `results_wanted`
(integer — the record limit), `max_pages` (integer), `proxyConfiguration`
(object — left unset/default here; no custom proxy configured).

OUTPUT SHAPE — VERIFIED LIVE 2026-07-10 (`careeros discover --provider
foundit --dry-run`, 1 real job returned for a 3-item request, $0.0005 total).
Most fields (`title`, `company`, `description`, `location`, posted-date)
already resolve correctly through the shared `pick_field`/candidate-key
pattern — Foundit's real output is mostly flat. Two real gaps found:
`salary` is a plain compound string (e.g. `"INR 500000 - 1000000"`), not a
structured dict, so it needs `_foundit_salary`'s regex parse; and
`employment_type` is a flat `employment_type` key that just wasn't being
read at all.

CAVEAT — data relevance is QUERY-INDEPENDENT, confirmed 2026-07-11 by
varying the search term (unlike Indeed, this rules out a query-specific
explanation): live results for "Product Manager" returned roles unrelated
to product management (e.g. a Territory Sales Manager posting), and a
second, independently run "Software Engineer" search also returned poor
results (Fresher/generic listings) — both times the raw item's own
`keyword` field correctly echoed the sent query. Poor relevance for two
unrelated search terms rules out a CareerOS query-construction bug; this is
a genuine recall/relevance weakness in the actor or underlying site.
Classified Not Recommended — leave disabled; the India-focused low-overlap
appeal doesn't offset consistently low-value results.

All actor-run mechanics (token rotation, the `max_total_charge_usd` cap,
dataset fetch, cost read-back) are shared via `_apify_actor_common.py` —
this module only builds the run input and maps the output.
"""

from __future__ import annotations

import re
from typing import Any

from careeros.config import Config
from careeros.providers._apify_actor_common import run_actor, validate_apify_token
from careeros.providers._apify_common import (
    COMPANY_KEYS, DESCRIPTION_KEYS, TITLE_KEYS, URL_KEYS, pick_field,
)
from careeros.providers.base import ProviderResult

_SALARY_RE = re.compile(r"^([A-Za-z]{3})\s+([\d,]+)\s*-\s*([\d,]+)$")


def _foundit_salary(raw: dict[str, Any]) -> dict[str, Any] | None:
    text = raw.get("salary")
    if not isinstance(text, str) or not text.strip():
        return None
    match = _SALARY_RE.match(text.strip())
    if not match:
        return None
    currency, lo, hi = match.groups()
    return {"min": float(lo.replace(",", "")), "max": float(hi.replace(",", "")), "currency": currency, "unit": "year"}

ACTOR_ID = "shahidirfan/Foundit-Jobs-Scraper"
DEFAULT_KEYWORD = "Product Manager"
DEFAULT_LOCATION = "India"


def _build_run_input(provider_cfg: dict[str, Any], *, limit: int, search: str) -> dict[str, Any]:
    """Build the actor's run input. `search` (the CLI --search string, or a
    segmented-discovery query's search term) overrides the configured
    `keyword` when given; `limit` always wins over any configured
    `results_wanted`-equivalent for the actual `results_wanted` sent.
    `max_pages` is included only when explicitly configured — no invented
    default."""
    run_input: dict[str, Any] = {
        "keyword": search or (provider_cfg.get("keyword") or DEFAULT_KEYWORD),
        "location": provider_cfg.get("location") or DEFAULT_LOCATION,
        "results_wanted": max(int(limit), 1),
    }
    if provider_cfg.get("max_pages"):
        run_input["max_pages"] = provider_cfg["max_pages"]
    return run_input


class FounditProvider:
    id = "foundit"

    def validate(self, config: Config) -> list[str]:
        """Config/credential problems only — no network call. Foundit shares
        the same Apify token pool as every other actor-based provider."""
        return validate_apify_token(config.apify)

    def fetch(
        self, config: Config, *, limit: int = 100, search: str = "",
        query: dict[str, Any] | None = None,
    ) -> ProviderResult:
        """`query` (a segmented-discovery spec from pipeline/queryplan.py) is
        accepted but currently ignored — this actor's input schema doesn't
        offer enough independently verified fields (beyond keyword/location,
        already covered by `search`/config) to justify inventing an overlay
        pattern that hasn't been verified against real actor behavior. Favor
        simplicity here over unverified multi-field query support.
        """
        del query
        provider_cfg = config.providers.get(self.id, {}) or {}
        actor_id = provider_cfg.get("actor", ACTOR_ID)
        run_input = _build_run_input(provider_cfg, limit=limit, search=search)
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
            "company": pick_field(raw, COMPANY_KEYS, fallback="Unknown"),
            "apply_url": url,
            "description": pick_field(raw, DESCRIPTION_KEYS) or None,
            "location": pick_field(raw, ["location", "job_location", "city"]) or None,
            "remote": None,  # no reliable remote/onsite signal in the verified live shape
            "employment_type": raw.get("employment_type") or None,
            "seniority": None,
            "posted_at": pick_field(raw, ["date_posted", "posted_date", "postedDate", "date"]) or None,
            "salary": _foundit_salary(raw),
            "contact": None,
            "company_linkedin": None,
        }


PROVIDER = FounditProvider()
