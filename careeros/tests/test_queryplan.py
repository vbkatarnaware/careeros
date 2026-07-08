"""Tests for careeros/pipeline/queryplan.py — the P2.1 discovery-benchmark fix
(one segmented query per work-mode tier instead of a single broad fetch), plus
the P2.6 refinements: onsite cities consolidated into ONE query (benchmark-
confirmed the actor returns their union in a single call), and fully generic
"<place>_remote" geography (no country hardcoded — role/geo-agnostic by
construction, so any profile's own place names just work)."""

from __future__ import annotations

from careeros.pipeline.queryplan import build_query_plan, resolve_tier_limit
from careeros.tests.conftest import make_profile


def _real_shaped_profile(**overrides):
    """Mirrors the real .careeros/profile.yaml's discovery-relevant shape."""
    defaults = dict(
        role_priorities=["Product Manager", "AI Product Manager", "Founder's Office"],
        work_mode_priority=["global_remote", "india_remote", "navi_mumbai_onsite", "mumbai_onsite"],
        location={"remote": "preferred", "onsite_ok": ["Mumbai", "Navi Mumbai"]},
    )
    defaults.update(overrides)
    return make_profile(**defaults)


def _by_work_mode(plan):
    return {q["_work_mode"]: q for q in plan}


def test_full_profile_produces_one_query_per_remote_tier_plus_one_consolidated_onsite():
    """4 work-mode tiers (global_remote, india_remote, 2 onsite cities) collapse
    to 3 actor calls: the two onsite tiers merge into a single 'onsite' query."""
    profile = _real_shaped_profile()
    plan = build_query_plan(profile, {})
    assert len(plan) == 3
    assert {q["_work_mode"] for q in plan} == {"global_remote", "india_remote", "onsite"}


def test_every_query_searches_all_role_priorities():
    profile = _real_shaped_profile()
    plan = build_query_plan(profile, {})
    for q in plan:
        assert q["title_search"] == profile.role_priorities


def test_global_remote_query_has_no_location_and_remote_arrangement():
    profile = _real_shaped_profile()
    q = _by_work_mode(build_query_plan(profile, {}))["global_remote"]
    assert q["location_search"] == []
    assert q["work_arrangement"] == ["Remote OK", "Remote Solely"]


def test_india_remote_query_scopes_location_to_india():
    profile = _real_shaped_profile()
    q = _by_work_mode(build_query_plan(profile, {}))["india_remote"]
    assert q["location_search"] == ["India"]
    assert q["work_arrangement"] == ["Remote OK", "Remote Solely"]


def test_remote_tier_geography_is_generic_not_hardcoded_to_india():
    """The whole point of P2.6's de-hardcoding: any '<place>_remote' tier name
    works from profile data alone — a German Software Engineer profile gets a
    correct query with zero code changes, same as the India PM profile."""
    profile = _real_shaped_profile(
        role_priorities=["Software Engineer"],
        work_mode_priority=["global_remote", "germany_remote"],
    )
    q = _by_work_mode(build_query_plan(profile, {}))["germany_remote"]
    assert q["location_search"] == ["Germany"]
    assert q["work_arrangement"] == ["Remote OK", "Remote Solely"]


def test_remote_tier_geography_handles_multi_word_place_names():
    profile = _real_shaped_profile(work_mode_priority=["united_kingdom_remote"])
    q = _by_work_mode(build_query_plan(profile, {}))["united_kingdom_remote"]
    assert q["location_search"] == ["United Kingdom"]


def test_onsite_tiers_consolidate_into_one_query_covering_all_onsite_ok_cities():
    """Benchmark-confirmed (2026-07-08): locationSearch returns the union
    across cities in a single call, so N onsite tiers cost 1 actor call, not N."""
    profile = _real_shaped_profile()
    q = _by_work_mode(build_query_plan(profile, {}))["onsite"]
    assert set(q["location_search"]) == {"Mumbai", "Navi Mumbai"}
    assert q["work_arrangement"] == ["On-site", "Hybrid"]


def test_onsite_query_covers_full_onsite_ok_list_even_if_a_city_has_no_own_tier():
    """Consolidating onsite queries also fixes a latent bug: if the profile's
    onsite_ok list has a city with no matching work_mode_priority tier entry,
    the old per-tier-city matching would silently never search it. The new
    consolidated query searches the profile's FULL onsite_ok list whenever any
    onsite tier is present, so this can't happen."""
    profile = _real_shaped_profile(
        work_mode_priority=["global_remote", "mumbai_onsite"],  # no explicit Pune tier
        location={"remote": "preferred", "onsite_ok": ["Mumbai", "Navi Mumbai", "Pune"]},
    )
    q = _by_work_mode(build_query_plan(profile, {}))["onsite"]
    assert set(q["location_search"]) == {"Mumbai", "Navi Mumbai", "Pune"}


def test_no_onsite_query_when_onsite_ok_is_empty():
    profile = _real_shaped_profile(
        work_mode_priority=["global_remote", "mumbai_onsite"],
        location={"remote": "preferred", "onsite_ok": []},
    )
    by_mode = _by_work_mode(build_query_plan(profile, {}))
    assert "onsite" not in by_mode


def test_unrecognized_work_mode_tier_is_skipped_not_guessed():
    profile = _real_shaped_profile(work_mode_priority=["global_remote", "some_future_tier"])
    plan = build_query_plan(profile, {})
    assert len(plan) == 1
    assert plan[0]["_work_mode"] == "global_remote"


def test_duplicate_remote_tiers_deduped_to_one_query():
    """Two tiers that resolve to the same location+arrangement (e.g. a typo'd
    duplicate) must not double-charge an actor call."""
    profile = _real_shaped_profile(work_mode_priority=["india_remote", "india_remote"])
    plan = build_query_plan(profile, {})
    assert len(plan) == 1


def test_carries_through_remove_agency_has_salary_and_exclusions():
    profile = _real_shaped_profile()
    apify_cfg = {
        "remove_agency": False, "has_salary": True,
        "title_exclusion_search": ["Intern"], "location_exclusion_search": ["China"],
    }
    for q in build_query_plan(profile, apify_cfg):
        assert q["remove_agency"] is False
        assert q["has_salary"] is True
        assert q["title_exclusion_search"] == ["Intern"]
        assert q["location_exclusion_search"] == ["China"]


# ── fallback to a single legacy query ───────────────────────────────────

def test_discovery_mode_single_forces_one_legacy_query():
    profile = _real_shaped_profile()
    apify_cfg = {"discovery_mode": "single", "title_search": ["Growth PM"], "location_search": ["India"]}
    plan = build_query_plan(profile, apify_cfg)
    assert len(plan) == 1
    assert plan[0]["title_search"] == ["Growth PM"]
    assert plan[0]["location_search"] == ["India"]


def test_profile_without_role_priorities_falls_back_to_single_query():
    profile = _real_shaped_profile(role_priorities=[])
    plan = build_query_plan(profile, {"title_search": ["Product Manager"]})
    assert len(plan) == 1
    assert "_work_mode" not in plan[0]


def test_profile_without_work_mode_priority_falls_back_to_single_query():
    profile = _real_shaped_profile(work_mode_priority=[])
    plan = build_query_plan(profile, {"location_search": ["India"]})
    assert len(plan) == 1


def test_single_fallback_uses_role_priorities_as_title_search_when_config_empty():
    """If config has no title_search but the profile has role_priorities and
    we're in a fallback (e.g. discovery_mode=single with no config override),
    still search the profile's own roles rather than nothing."""
    profile = _real_shaped_profile()
    plan = build_query_plan(profile, {"discovery_mode": "single"})
    assert plan[0]["title_search"] == profile.role_priorities


# ── resolve_tier_limit: per-tier limit override (P2.6) ──────────────────

def test_resolve_tier_limit_falls_back_to_default_when_unset():
    assert resolve_tier_limit("global_remote", {}, 100) == 100


def test_resolve_tier_limit_uses_configured_override():
    apify_cfg = {"tier_limits": {"india_remote": 25}}
    assert resolve_tier_limit("india_remote", apify_cfg, 100) == 25


def test_resolve_tier_limit_only_overrides_the_named_tier():
    apify_cfg = {"tier_limits": {"india_remote": 25}}
    assert resolve_tier_limit("global_remote", apify_cfg, 100) == 100


def test_resolve_tier_limit_handles_missing_tier_limits_key():
    """A user config with no tier_limits at all (not even an empty dict)
    must not raise — falls back to default_limit cleanly."""
    assert resolve_tier_limit("global_remote", {"discovery_mode": "profile"}, 50) == 50
