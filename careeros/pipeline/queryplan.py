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
calls total, not a cartesian product — cost- and complexity-bounded by
construction.

v2.2 (2026-08-05): `role_priorities` beyond 3 terms must now be CHUNKED, one
query per chunk of `_MAX_TITLE_OR_TERMS` — see that constant's comment for
the live-verified bug this works around. A 6-role profile (the real
.careeros/profile.yaml) now costs 2x the queries above (e.g. 4 tiers -> 8),
not 3-4 flat; the record budget still divides evenly across however many
queries actually get built, so this costs extra `api_requests` (a looser,
separately-metered quota) but not extra records/week.

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
`providers/fantastic_jobs.py`'s `_build_params()` already understands
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

# Live-verified 2026-08-05 against Fantastic.jobs' bare `title` param (the
# documented syntax we use: "A OR B OR C -D -E" — see their Advanced
# Searching Guide). Their docs state no hard term-count limit exists, but
# direct testing found the exact break point: with <=3 OR-terms, an
# `-exclusion` clause in the same param applies cleanly every time (0 leaks
# across repeated tests); at 4+ OR-terms, the exclusion clause is silently
# ignored — same leaked jobs whether 1 exclusion or 6 is appended, regardless
# of term order or total string length (isolated by testing both). This is
# why title_exclusion_search (Intern/Trainee/Marketing/Assistant/...) never
# actually filtered anything live once role_priorities grew past 3 entries:
# the query string was syntactically fine, just silently over budget for
# whatever internal limit the provider's query parser enforces.
_MAX_TITLE_OR_TERMS = 3


def _title_chunks(role_priorities: list[str], apify_cfg: dict[str, Any]) -> list[list[str]]:
    """Splits role_priorities into groups of at most `_MAX_TITLE_OR_TERMS` so
    every resulting query's title param stays inside the verified range where
    `-exclusion` terms actually apply. A profile with <=3 roles (nearly every
    test fixture, and plenty of real profiles) yields exactly one chunk equal
    to the full list — this is a no-op for anyone not already past the
    threshold.

    Chunking is SKIPPED entirely in the default `title_mode: advanced`, where
    `providers/fantastic_jobs.py` expresses the same roles + exclusions as one
    boolean `title_advanced` expression that has no term ceiling. Chunking
    remains the fallback for `title_mode: basic` — it is the only thing that
    makes exclusions work on the bare `title` param."""
    if not role_priorities:
        return [[]]
    if apify_cfg.get("title_mode", "advanced") == "advanced":
        return [list(role_priorities)]
    return [
        role_priorities[i : i + _MAX_TITLE_OR_TERMS]
        for i in range(0, len(role_priorities), _MAX_TITLE_OR_TERMS)
    ]


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

    # v2.1: years-of-experience bands to fetch (see fantastic_jobs.py's
    # `_build_params` for why this must be one query PER band). Empty/absent
    # keeps the pre-v2.1 behavior of one unfiltered query per tier.
    experience_levels = list(apify_cfg.get("experience_levels", []) or [])

    def _add(work_mode: str, location_search: list[str], work_arrangement: list[str]) -> None:
        dedup_key = (tuple(location_search), tuple(work_arrangement))
        if dedup_key in seen_specs:
            return
        seen_specs.add(dedup_key)

        def _emit(experience_level: str | None) -> None:
            # v2.2: one query per title chunk (see _MAX_TITLE_OR_TERMS) so
            # title_exclusion_search stays effective regardless of how many
            # role_priorities the profile declares.
            for title_chunk in _title_chunks(role_priorities, apify_cfg):
                query = _base_query(apify_cfg, title_chunk)
                query.update({"location_search": location_search, "work_arrangement": work_arrangement})
                if experience_level:
                    query["experience_level"] = experience_level
                # `_work_mode` deliberately stays the TIER name even when a
                # tier fans out across experience bands OR title chunks: the
                # learning ledger (pipeline/ledger.py) aggregates by it, and
                # splitting one tier into several would shrink each one's
                # sample size and push every tier further from the arming
                # thresholds for no analytical gain.
                query["_work_mode"] = work_mode  # debug/logging only, not an actor field
                plan.append(query)

        if experience_levels:
            for level in experience_levels:
                _emit(level)
        else:
            _emit(None)

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
