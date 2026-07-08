"""Stage: query planning for `discover`. Deterministic. Zero AI, zero tokens.

The discovery benchmark (2026-07-08) found that a single broad query yields
roughly one apply-worthy job per 40 fetched, and that segmenting by work-mode
surfaces near-disjoint pools from the SAME provider (global-remote vs
India-remote vs onsite barely overlapped). Title segmentation wasn't the
lever — the actor's `titleSearch` is an OR-array, so every target role fits in
one query already — work-mode/location was.

So: one query per `profile.work_mode_priority` tier, each searching all of
`profile.role_priorities` at once. For a 4-tier profile that's 4 Apify calls,
not a cartesian product — cost- and complexity-bounded by construction.

Each returned spec is a plain dict using the exact keys
`providers/fantastic_jobs.py`'s `_build_run_input()` already understands
(title_search, location_search, work_arrangement, ...), so wiring this in
required zero new field-mapping logic.
"""

from __future__ import annotations

from typing import Any

from careeros.models import Profile

# work_mode_priority tier -> (locationSearch, aiWorkArrangementFilter).
# Only the profile's own onsite_ok cities are ever used for the onsite tiers
# (see _onsite_query), so a profile that only lists "Mumbai" never gets a
# spurious "Navi Mumbai" query.
_REMOTE_ARRANGEMENT = ["Remote OK", "Remote Solely"]
_ONSITE_ARRANGEMENT = ["On-site", "Hybrid"]


def _global_remote_query() -> dict[str, Any]:
    return {"location_search": [], "work_arrangement": _REMOTE_ARRANGEMENT}


def _india_remote_query() -> dict[str, Any]:
    return {"location_search": ["India"], "work_arrangement": _REMOTE_ARRANGEMENT}


def _onsite_query(city: str) -> dict[str, Any]:
    return {"location_search": [city], "work_arrangement": _ONSITE_ARRANGEMENT}


_TIER_BUILDERS = {
    "global_remote": _global_remote_query,
    "india_remote": _india_remote_query,
    # onsite tiers are handled specially in build_query_plan since they need
    # the profile's own city name, not a hardcoded one.
}

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
    for tier in work_modes:
        builder = _TIER_BUILDERS.get(tier)
        if builder is not None:
            tier_filters = builder()
        elif tier.endswith("_onsite"):
            # e.g. "navi_mumbai_onsite" -> "Navi Mumbai" — only if that city
            # is actually one the candidate accepts onsite (profile-driven,
            # never a guessed city name).
            city_guess = tier[: -len("_onsite")].replace("_", " ").title()
            match = next((c for c in onsite_ok if c.lower() == city_guess.lower()), None)
            if match is None:
                continue
            tier_filters = _onsite_query(match)
        else:
            continue  # unrecognized tier — skip rather than guess

        query = _base_query(apify_cfg, role_priorities)
        query.update(tier_filters)
        query["_work_mode"] = tier  # debug/logging only, not an actor field

        dedup_key = (tuple(query["location_search"]), tuple(query["work_arrangement"]))
        if dedup_key in seen_specs:
            continue
        seen_specs.add(dedup_key)
        plan.append(query)

    return plan or [_base_query(apify_cfg, role_priorities) | {"location_search": []}]
