"""Tests for careeros/pipeline/queryplan.py — the P2.1 discovery-benchmark fix:
one segmented query per work-mode tier instead of a single broad fetch."""

from __future__ import annotations

from careeros.pipeline.queryplan import build_query_plan
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


def test_full_profile_produces_one_query_per_work_mode_tier():
    profile = _real_shaped_profile()
    plan = build_query_plan(profile, {})
    assert len(plan) == 4
    assert {q["_work_mode"] for q in plan} == {
        "global_remote", "india_remote", "navi_mumbai_onsite", "mumbai_onsite",
    }


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


def test_onsite_tiers_use_onsite_arrangement_and_matching_city():
    profile = _real_shaped_profile()
    by_mode = _by_work_mode(build_query_plan(profile, {}))
    assert by_mode["mumbai_onsite"]["location_search"] == ["Mumbai"]
    assert by_mode["navi_mumbai_onsite"]["location_search"] == ["Navi Mumbai"]
    for tier in ("mumbai_onsite", "navi_mumbai_onsite"):
        assert by_mode[tier]["work_arrangement"] == ["On-site", "Hybrid"]


def test_onsite_tier_skipped_when_city_not_in_onsite_ok():
    """A profile that only accepts Mumbai onsite must not silently query for
    a Navi Mumbai-onsite job — the city must come FROM the profile, never be
    guessed from the tier name alone."""
    profile = _real_shaped_profile(location={"remote": "preferred", "onsite_ok": ["Mumbai"]})
    by_mode = _by_work_mode(build_query_plan(profile, {}))
    assert "mumbai_onsite" in by_mode
    assert "navi_mumbai_onsite" not in by_mode


def test_unrecognized_work_mode_tier_is_skipped_not_guessed():
    profile = _real_shaped_profile(work_mode_priority=["global_remote", "some_future_tier"])
    plan = build_query_plan(profile, {})
    assert len(plan) == 1
    assert plan[0]["_work_mode"] == "global_remote"


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
