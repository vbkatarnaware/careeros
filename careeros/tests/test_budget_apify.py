"""Tests for careeros/budget.py's v1.2 additions: `guard_for` (capability
detection — weekly vs monthly vs none, purely from which KEYS are present in
a provider's own resolved config, never its name) and the rolling-month
Apify spend counter (`month_start`/`load_apify_state`/`save_apify_state`/
`record_apify_spend`/`check_apify_budget`) — the monthly-budget half of the
same "recommend, warn, prevent, never silently override" contract the
existing rolling-week guard already follows.

Explicitly covers the HONEST caveat the design commits to: this is a
best-effort estimate, only ever a hard block once the recorded (possibly
undercounted) spend has reached the configured ceiling."""

from __future__ import annotations

from careeros import budget


# ── guard_for: capability detection is key-presence, not name-based ──────

def test_guard_for_weekly_when_plan_key_present():
    assert budget.guard_for({"plan": "free", "endpoint": "both"}) == "weekly"


def test_guard_for_weekly_even_when_plan_value_is_none():
    """Presence of the KEY matters, not its value — an unset api.plan is
    still Fantastic Jobs' own config shape."""
    assert budget.guard_for({"plan": None}) == "weekly"


def test_guard_for_monthly_when_max_monthly_budget_key_present():
    assert budget.guard_for({"enabled": True, "max_monthly_budget_usd": 10}) == "monthly"


def test_guard_for_monthly_even_when_budget_value_is_none():
    """A provider's own max_monthly_budget_usd: null means "use the shared
    apify.max_monthly_budget_usd default" — the KEY being present is what
    declares the capability; the null value is resolved by the caller."""
    assert budget.guard_for({"enabled": True, "max_monthly_budget_usd": None}) == "monthly"


def test_guard_for_none_when_neither_key_present():
    """A free provider (RemoteOK, We Work Remotely) has neither key in its
    own resolved config -> no guard at all."""
    assert budget.guard_for({"enabled": True}) == "none"


def test_guard_for_weekly_takes_priority_if_both_keys_somehow_present():
    assert budget.guard_for({"plan": "free", "max_monthly_budget_usd": 10}) == "weekly"


# ── month_start: rolls over on the 1st, same fallback as week_start ──────

def test_month_start_is_the_first_of_the_month():
    assert budget.month_start("2026-07-15") == "2026-07-01"


def test_month_start_falls_back_to_real_today_for_unparseable_label():
    import datetime
    expected = datetime.date.today().replace(day=1).isoformat()
    assert budget.month_start("qa-p27-actor") == expected


# ── rolling-month state: load/save/record ─────────────────────────────────

def test_load_apify_state_defaults_to_zero_spend_when_no_file(tmp_path):
    state = budget.load_apify_state(tmp_path, "2026-07-15")
    assert state == {"month_start": "2026-07-01", "spend_usd": 0.0}


def test_save_and_reload_apify_state_roundtrips(tmp_path):
    state = {"month_start": "2026-07-01", "spend_usd": 1.23}
    budget.save_apify_state(tmp_path, state)
    reloaded = budget.load_apify_state(tmp_path, "2026-07-20")
    assert reloaded == state


def test_load_apify_state_rolls_over_into_a_new_month(tmp_path):
    budget.save_apify_state(tmp_path, {"month_start": "2026-06-01", "spend_usd": 9.5})
    reloaded = budget.load_apify_state(tmp_path, "2026-07-01")
    assert reloaded == {"month_start": "2026-07-01", "spend_usd": 0.0}


def test_record_apify_spend_accumulates():
    state = {"month_start": "2026-07-01", "spend_usd": 1.0}
    budget.record_apify_spend(state, 0.5)
    budget.record_apify_spend(state, 0.25)
    assert state["spend_usd"] == 1.75


def test_record_apify_spend_never_goes_negative():
    state = {"month_start": "2026-07-01", "spend_usd": 1.0}
    budget.record_apify_spend(state, -5.0)
    assert state["spend_usd"] == 1.0


# ── check_apify_budget: recommend/warn/prevent, best-effort ──────────────

def test_check_apify_budget_ok_and_no_message_when_no_budget_configured():
    ok, msg = budget.check_apify_budget({"spend_usd": 100.0}, None)
    assert ok is True
    assert msg is None


def test_check_apify_budget_warns_with_remaining_when_under_budget():
    ok, msg = budget.check_apify_budget({"spend_usd": 3.0, "month_start": "2026-07-01"}, 10.0)
    assert ok is True
    assert "3.0000" in msg
    assert "10.00" in msg
    assert "7.0000" in msg  # remaining


def test_check_apify_budget_prevents_once_spend_reaches_budget():
    ok, msg = budget.check_apify_budget({"spend_usd": 10.0, "month_start": "2026-07-01"}, 10.0)
    assert ok is False
    assert "Monthly Apify budget reached" in msg


def test_check_apify_budget_prevents_when_spend_exceeds_budget():
    ok, msg = budget.check_apify_budget({"spend_usd": 15.0}, 10.0)
    assert ok is False


def test_check_apify_budget_message_flags_the_best_effort_caveat():
    """The honest limitation must be visible in the actual message shown to
    the user, not just documented in a docstring — Apify's own usage
    reporting can undercount real settled cost."""
    ok, msg = budget.check_apify_budget({"spend_usd": 10.0}, 10.0)
    assert "estimate" in msg.lower()
