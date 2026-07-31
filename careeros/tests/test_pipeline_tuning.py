"""Tests for careeros/pipeline/tuning.py — proposal guardrails and the
diff-in-differences auto-revert rule."""

from __future__ import annotations

from careeros.pipeline.ledger import TierLedgerEntry
from careeros.pipeline.tuning import (
    Proposal, apply_proposal_to_overlay, check_revert, record_floor,
    sunset_expired_exclusions, validate_proposal,
)

ARMED = {"onsite": {"armed": True, "reason": ""}, "global_remote": {"armed": False, "reason": "only 100 records (need 400)"}}


def _entry(date, tier, *, records=100, constraints_rejected=5, gate_dropped=20,
           evaluated=30, apply=2, consider=3):
    return TierLedgerEntry(
        date=date, tier=tier,
        decision={"records": records, "constraints_rejected": constraints_rejected,
                  "gate_dropped": gate_dropped, "gate_keep_rate": evaluated / max(1, gate_dropped + evaluated),
                  "constraints_rejection_rate": constraints_rejected / max(1, records),
                  "reject_reason_histogram": {}},
        descriptive={"evaluated": evaluated, "apply": apply, "consider": consider, "omit": evaluated - apply - consider,
                    "mean_score": 3.0, "mean_shortfall": None},
    )


# ── record_floor ──────────────────────────────────────────────────────────

def test_record_floor_is_half_equal_share_with_15_minimum():
    assert record_floor(num_tiers=3, daily_total=100) == 17  # 0.5 * 33.33 = 16.67 -> round 17
    assert record_floor(num_tiers=10, daily_total=50) == 15  # 0.5*5=2.5, floored up to the 15 minimum


# ── validate_proposal ─────────────────────────────────────────────────────

def _base_kwargs(**overrides):
    kwargs = dict(
        arming=ARMED, control_tier="india_remote", current_overlay_api={},
        changes_this_cycle=0, num_tiers=3, daily_total=100,
    )
    kwargs.update(overrides)
    return kwargs


def test_validate_proposal_clean_exclusion_addition_passes():
    p = Proposal(tier="onsite", field="title_exclusion_search", value="Brand",
                evidence="11 records over 4 weeks gate-dropped role-mismatch with 'Brand' in the title",
                sunset="2026-11-01")
    assert validate_proposal(p, **_base_kwargs()) == []


def test_validate_proposal_rejects_unarmed_tier():
    p = Proposal(tier="global_remote", field="title_exclusion_search", value="Brand",
                evidence="some evidence", sunset="2026-11-01")
    violations = validate_proposal(p, **_base_kwargs())
    assert any("not armed" in v for v in violations)


def test_validate_proposal_rejects_control_tier():
    p = Proposal(tier="india_remote", field="title_exclusion_search", value="Brand",
                evidence="some evidence", sunset="2026-11-01")
    violations = validate_proposal(p, **_base_kwargs(arming={"india_remote": {"armed": True, "reason": ""}}))
    assert any("control tier" in v for v in violations)


def test_validate_proposal_rejects_second_change_in_same_cycle():
    p = Proposal(tier="onsite", field="title_exclusion_search", value="Brand",
                evidence="some evidence", sunset="2026-11-01")
    violations = validate_proposal(p, **_base_kwargs(changes_this_cycle=1))
    assert any("one change per cycle" in v for v in violations)


def test_validate_proposal_rejects_missing_evidence():
    p = Proposal(tier="onsite", field="title_exclusion_search", value="Brand",
                evidence="   ", sunset="2026-11-01")
    violations = validate_proposal(p, **_base_kwargs())
    assert any("no evidence" in v for v in violations)


def test_validate_proposal_rejects_exclusion_without_sunset():
    p = Proposal(tier="onsite", field="title_exclusion_search", value="Brand", evidence="e", sunset=None)
    violations = validate_proposal(p, **_base_kwargs())
    assert any("sunset" in v for v in violations)


def test_validate_proposal_rejects_exclusion_list_at_cap():
    p = Proposal(tier="onsite", field="title_exclusion_search", value="NewTerm", evidence="e", sunset="2026-11-01")
    current = {"title_exclusion_search": [f"term{i}" for i in range(8)]}
    violations = validate_proposal(p, **_base_kwargs(current_overlay_api=current, max_exclusion_entries=8))
    assert any("cap" in v for v in violations)


def test_validate_proposal_rejects_tier_limit_change_over_25_percent():
    p = Proposal(tier="onsite", field="tier_limits", value=50, evidence="e")
    current = {"tier_limits": {"onsite": 33}}  # (50-33)/33 = 51%
    violations = validate_proposal(p, **_base_kwargs(current_overlay_api=current))
    assert any("exceeding" in v for v in violations)


def test_validate_proposal_rejects_tier_limit_below_floor():
    p = Proposal(tier="onsite", field="tier_limits", value=5, evidence="e")
    violations = validate_proposal(p, **_base_kwargs(num_tiers=3, daily_total=100))
    assert any("record floor" in v for v in violations)


def test_validate_proposal_accepts_tier_limit_change_within_bounds():
    p = Proposal(tier="onsite", field="tier_limits", value=40, evidence="e")
    current = {"tier_limits": {"onsite": 33}}  # (40-33)/33 = 21%, under 25% cap, above floor
    assert validate_proposal(p, **_base_kwargs(current_overlay_api=current)) == []


def test_validate_proposal_unknown_field_rejected():
    p = Proposal(tier="onsite", field="work_arrangement", value="Remote", evidence="e")
    violations = validate_proposal(p, **_base_kwargs())
    assert any("not a tunable field" in v for v in violations)


# ── apply_proposal_to_overlay ─────────────────────────────────────────────

def test_apply_proposal_adds_exclusion_entry():
    out = apply_proposal_to_overlay({}, Proposal(tier="onsite", field="title_exclusion_search",
                                                 value="Brand", evidence="e", sunset="2026-11-01"))
    assert out["title_exclusion_search"] == ["Brand"]


def test_apply_proposal_is_pure_does_not_mutate_input():
    original = {"title_exclusion_search": ["Existing"]}
    apply_proposal_to_overlay(original, Proposal(tier="onsite", field="title_exclusion_search",
                                                 value="New", evidence="e", sunset="2026-11-01"))
    assert original["title_exclusion_search"] == ["Existing"]


def test_apply_proposal_sets_tier_limit():
    out = apply_proposal_to_overlay({"tier_limits": {"onsite": 20}},
                                     Proposal(tier="global_remote", field="tier_limits", value=30, evidence="e"))
    assert out["tier_limits"] == {"onsite": 20, "global_remote": 30}


# ── sunset_expired_exclusions ─────────────────────────────────────────────

def test_sunset_drops_expired_entries():
    overlay = {"title_exclusion_search": ["Old", "Fresh"]}
    sunsets = {"Old": "2026-01-01", "Fresh": "2027-01-01"}
    out = sunset_expired_exclusions(overlay, as_of="2026-08-01", sunsets=sunsets)
    assert out["title_exclusion_search"] == ["Fresh"]


def test_sunset_keeps_entries_with_no_recorded_sunset():
    overlay = {"title_exclusion_search": ["Mystery"]}
    out = sunset_expired_exclusions(overlay, as_of="2026-08-01", sunsets={})
    assert out["title_exclusion_search"] == ["Mystery"]


# ── check_revert ──────────────────────────────────────────────────────────

def test_check_revert_r1_volume_floor():
    window = [_entry("2026-08-01", "onsite", records=10, gate_dropped=2, evaluated=3)]
    baseline = [_entry("2026-07-01", "onsite", records=100, gate_dropped=20, evaluated=30)]
    decision = check_revert(window, baseline, [], [])
    assert decision.should_revert
    assert "R1" in decision.reason


def test_check_revert_r3_killed_the_best():
    window = [_entry(f"2026-08-0{i}", "onsite", records=100, gate_dropped=20, evaluated=30, apply=0, consider=0) for i in range(1, 6)]
    baseline = [_entry(f"2026-07-0{i}", "onsite", records=100, gate_dropped=20, evaluated=30, apply=1, consider=1) for i in range(1, 6)]
    decision = check_revert(window, baseline, [], [])
    assert decision.should_revert
    assert "R3" in decision.reason


def test_check_revert_r2_precision_drop_vs_stable_control():
    changed_window = [_entry("2026-08-01", "onsite", gate_dropped=80, evaluated=20)]  # keep rate 0.2
    changed_baseline = [_entry("2026-07-01", "onsite", gate_dropped=20, evaluated=80)]  # keep rate 0.8
    control_window = [_entry("2026-08-01", "india_remote", gate_dropped=20, evaluated=80)]  # 0.8
    control_baseline = [_entry("2026-07-01", "india_remote", gate_dropped=20, evaluated=80)]  # 0.8, unchanged
    decision = check_revert(changed_window, changed_baseline, control_window, control_baseline)
    assert decision.should_revert
    assert "R2" in decision.reason


def test_check_revert_confounded_when_control_moves_similarly():
    """The direct defense against a market-wide swing being blamed on the
    one tier that happened to get changed."""
    changed_window = [_entry("2026-08-01", "onsite", gate_dropped=80, evaluated=20)]  # 0.2
    changed_baseline = [_entry("2026-07-01", "onsite", gate_dropped=20, evaluated=80)]  # 0.8
    control_window = [_entry("2026-08-01", "india_remote", gate_dropped=80, evaluated=20)]  # 0.2 -- also dropped
    control_baseline = [_entry("2026-07-01", "india_remote", gate_dropped=20, evaluated=80)]  # 0.8
    decision = check_revert(changed_window, changed_baseline, control_window, control_baseline)
    assert not decision.should_revert
    assert decision.confounded


def test_check_revert_no_trigger_on_neutral_change():
    window = [_entry("2026-08-01", "onsite", gate_dropped=20, evaluated=31)]
    baseline = [_entry("2026-07-01", "onsite", gate_dropped=20, evaluated=30)]
    control_window = [_entry("2026-08-01", "india_remote", gate_dropped=20, evaluated=30)]
    control_baseline = [_entry("2026-07-01", "india_remote", gate_dropped=20, evaluated=30)]
    decision = check_revert(window, baseline, control_window, control_baseline)
    assert not decision.should_revert


def test_check_revert_no_data_in_window():
    decision = check_revert([], [], [], [])
    assert not decision.should_revert


# ── is_check_due ──────────────────────────────────────────────────────────

from careeros.pipeline.tuning import find_tuning_flags, is_check_due  # noqa: E402


def test_is_check_due_true_when_never_checked():
    assert is_check_due(None, "2026-08-01") is True


def test_is_check_due_false_within_cadence():
    assert is_check_due("2026-07-28", "2026-08-01", cadence_days=7) is False


def test_is_check_due_true_at_or_past_cadence():
    assert is_check_due("2026-07-25", "2026-08-01", cadence_days=7) is True  # exactly 7 days
    assert is_check_due("2026-07-01", "2026-08-01", cadence_days=7) is True


def test_is_check_due_true_on_unparseable_date():
    assert is_check_due("not-a-date", "2026-08-01") is True


# ── find_tuning_flags ─────────────────────────────────────────────────────

def test_find_tuning_flags_surfaces_armed_tier_with_common_reason():
    summary = {
        "onsite": {"dates": ["2026-07-01", "2026-07-28"], "total_records": 500,
                  "apply_or_consider_events": 10, "top_reject_reasons": [("role-mismatch", 23)]},
    }
    arming = {"onsite": {"armed": True, "reason": ""}}
    flags = find_tuning_flags(summary, arming, control_tier="india_remote")
    assert len(flags) == 1
    assert flags[0].tier == "onsite"
    assert flags[0].reason_count == 23
    assert "23" in flags[0].label and "role-mismatch" in flags[0].label


def test_find_tuning_flags_skips_unarmed_tier():
    summary = {"onsite": {"dates": ["2026-07-28"], "total_records": 50,
                          "apply_or_consider_events": 1, "top_reject_reasons": [("role-mismatch", 20)]}}
    arming = {"onsite": {"armed": False, "reason": "not enough data"}}
    assert find_tuning_flags(summary, arming, control_tier="india_remote") == []


def test_find_tuning_flags_never_flags_the_control_tier():
    summary = {"india_remote": {"dates": ["2026-07-01", "2026-07-28"], "total_records": 500,
                                "apply_or_consider_events": 10, "top_reject_reasons": [("role-mismatch", 50)]}}
    arming = {"india_remote": {"armed": True, "reason": ""}}
    assert find_tuning_flags(summary, arming, control_tier="india_remote") == []


def test_find_tuning_flags_skips_thin_reason_counts():
    summary = {"onsite": {"dates": ["2026-07-01", "2026-07-28"], "total_records": 500,
                          "apply_or_consider_events": 10, "top_reject_reasons": [("role-mismatch", 3)]}}
    arming = {"onsite": {"armed": True, "reason": ""}}
    assert find_tuning_flags(summary, arming, control_tier="india_remote", min_reason_count=8) == []
