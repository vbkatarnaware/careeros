"""Stage: query planning for `discover`. Deterministic. Zero AI, zero tokens.

The discovery benchmark (2026-07-08) found that a single broad query yields
roughly one apply-worthy job per 40 fetched, and that segmenting by work-mode
surfaces near-disjoint pools from the SAME provider (global-remote vs
India-remote vs onsite barely overlapped). Title segmentation wasn't the
lever — the actor's `titleSearch` is an OR-array, so every target role fits in
one query already — work-mode/location was.

So: one query per REMOTE work-mode tier, plus ONE consolidated query covering
every onsite city the profile accepts, each searching all of
`profile.role_priorities` at once. For a typical profile that's still 3-4
Apify calls total, not a cartesian product — cost- and complexity-bounded by
construction.

The P2.6 benchmark (2026-07-08) added two refinements, both evidence-backed:
- **Onsite cities are merged into ONE query** instead of one call per city —
  confirmed live that `locationSearch` with multiple cities returns their
  union in a single call, so N onsite tiers no longer cost N actor calls.
- **Remote geography is fully generic**, not hardcoded to India. Any tier
  named `"<place>_remote"` derives `place` from the tier name itself
  (`"india_remote"` -> "India", `"united_kingdom_remote"` -> "United Kingdom").
  This is what makes CareerOS role/geo-agnostic: a Software Engineer profile
  based in Germany just writes `germany_remote` in `work_mode_priority` and
  gets a correctly-scoped query with zero code changes. Known limitation:
  ALL-CAPS country codes don't title-case cleanly (`"us_remote"` -> "Us", not
  "US") — spell country names out (`"united_states_remote"`) for reliable
  matching; `location_search` also isn't strictly exact-match server-side (the
  benchmark saw some out-of-scope leakage even for named cities), which is
  exactly why the deterministic `constraints` stage re-checks location
  regardless of what discovery returns.

Each returned spec is a plain dict using the exact keys
`providers/fantastic_jobs.py`'s `_build_run_input()` already understands
(title_search, location_search, work_arrangement, ...), so wiring this in
required zero new field-mapping logic.
"""

from __future__ import annotations

from typing import Any

from careeros.models import Profile

_REMOTE_ARRANGEMENT = ["Remote OK", "Remote Solely"]
_ONSITE_ARRANGEMENT = ["On-site", "Hybrid"]

_CARRY_THROUGH_KEYS = (
    "remove_agency", "has_salary", "title_exclusion_search", "location_exclusion_search",
)


def _base_query(apify_cfg: dict[str, Any], role_priorities: list[str]) -> dict[str, Any]:
    query: dict[str, Any] = {"title_search": list(role_priorities)}
    for key in _CARRY_THROUGH_KEYS:
        if key in apify_cfg:
            query[key] = apify_cfg[key]
    return query


def build_query_plan(profile: Profile, apify_cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Returns a list of run-input-shaped dicts, one per `discover` call.

    Falls back to a single legacy query (today's title_search/location_search
    from config, unchanged) when `discovery_mode` is explicitly "single", or
    when the profile doesn't declare enough to derive segmented queries from
    (no work_mode_priority or no role_priorities) — segmentation needs both.
    """
    mode = apify_cfg.get("discovery_mode", "profile")
    role_priorities = list(getattr(profile, "role_priorities", []) or [])
    work_modes = list(getattr(profile, "work_mode_priority", []) or [])

    if mode == "single" or not role_priorities or not work_modes:
        return [_base_query(apify_cfg, apify_cfg.get("title_search", []) or role_priorities)
                | {"location_search": apify_cfg.get("location_search", [])}]

    location = getattr(profile, "location", {}) or {}
    onsite_ok = list(location.get("onsite_ok", []) or [])

    plan: list[dict[str, Any]] = []
    seen_specs: set[tuple] = set()
    has_onsite_tier = False

    def _add(work_mode: str, location_search: list[str], work_arrangement: list[str]) -> None:
        dedup_key = (tuple(location_search), tuple(work_arrangement))
        if dedup_key in seen_specs:
            return
        seen_specs.add(dedup_key)
        query = _base_query(apify_cfg, role_priorities)
        query.update({"location_search": location_search, "work_arrangement": work_arrangement})
        query["_work_mode"] = work_mode  # debug/logging only, not an actor field
        plan.append(query)

    for tier in work_modes:
        if tier == "global_remote":
            _add(tier, [], _REMOTE_ARRANGEMENT)
        elif tier.endswith("_remote"):
            # Generic geography, profile-driven — e.g. "india_remote" -> "India",
            # "united_kingdom_remote" -> "United Kingdom". No place is hardcoded.
            place = tier[: -len("_remote")].replace("_", " ").title()
            if place:
                _add(tier, [place], _REMOTE_ARRANGEMENT)
        elif tier.endswith("_onsite"):
            has_onsite_tier = True  # consolidated once below, not per tier
        # unrecognized tier shape: skip rather than guess

    if has_onsite_tier and onsite_ok:
        _add("onsite", list(onsite_ok), _ONSITE_ARRANGEMENT)

    return plan or [_base_query(apify_cfg, role_priorities) | {"location_search": []}]


def resolve_tier_limit(work_mode: str, apify_cfg: dict[str, Any], default_limit: int) -> int:
    """Per-tier `limit` override, keyed by the same `_work_mode` tag
    `build_query_plan` puts on each spec — falls back to `default_limit` (the
    CLI's --limit) for any tier not explicitly listed in
    `apify_cfg["tier_limits"]`. Deliberately NOT pre-tuned with opinionated
    per-tier defaults in `config.py` (see its comment there) — this function
    just resolves whatever the user has configured for themselves."""
    tier_limits = apify_cfg.get("tier_limits", {}) or {}
    return tier_limits.get(work_mode, default_limit)
