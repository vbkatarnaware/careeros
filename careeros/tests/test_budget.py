"""Tests for careeros/budget.py — the discovery quota guard. Pure logic +
rolling-week state; no network. The guard's contract is recommend / explain /
warn / prevent, but NEVER silently override the user's api.limit."""

from __future__ import annotations

from careeros import budget


# ── quota resolution ────────────────────────────────────────────────────────

def test_weekly_quota_explicit_wins():
    assert budget.weekly_quota({"plan": "free", "weekly_record_quota": 2000}) == 2000


def test_weekly_quota_from_free_plan():
    assert budget.weekly_quota({"plan": "free"}) == 500


def test_weekly_quota_unknown_for_paid_without_explicit():
    # paid/enterprise/rapidapi have no hardcoded (unverified) number
    assert budget.weekly_quota({"plan": "paid"}) is None


def test_weekly_quota_none_when_no_plan():
    assert budget.weekly_quota({}) is None


def test_effective_limit_prefers_api_limit():
    assert budget.effective_limit({"limit": 40}) == 40


def test_effective_limit_falls_back_to_default():
    assert budget.effective_limit({}) == budget.DEFAULT_LIMIT
    assert budget.effective_limit({"limit": None}, cli_default=55) == 55


# ── recommendation ──────────────────────────────────────────────────────────

def test_recommend_spreads_quota_over_active_days():
    rec = budget.recommend(
        {"plan": "free", "active_days_per_week": 7}, {"interviews_per_week": 5},
        requests_per_run=1,
    )
    # 500 / 7 active days ≈ 71 records/day
    assert rec.quota == 500
    assert rec.recommended_records_per_day == 71
    assert rec.recommended_per_request == 71  # 1 request/run
    assert rec.goal_interviews_per_week == 5


def test_recommend_divides_by_requests_per_run():
    rec = budget.recommend({"plan": "free"}, {}, requests_per_run=2)
    # 500/7 ≈ 71 records/day, over 2 requests -> ~35/request
    assert rec.recommended_records_per_day == 71
    assert rec.recommended_per_request == 35


def test_recommend_flags_over_quota_and_never_mutates():
    api_cfg = {"plan": "free", "limit": 100, "active_days_per_week": 7}
    rec = budget.recommend(api_cfg, {}, requests_per_run=2)
    # 100 * 2 * 7 = 1400 records/week >> 500 quota
    assert rec.configured_weekly_records == 1400
    assert rec.over_quota is True
    assert any("exceed your weekly quota" in ln for ln in rec.lines())
    assert api_cfg["limit"] == 100  # guard did not touch the config


def test_recommend_informational_when_quota_unknown():
    rec = budget.recommend({"plan": "paid"}, {}, requests_per_run=1)
    assert rec.quota is None
    assert rec.recommended_per_request is None
    assert any("No weekly quota known" in ln for ln in rec.lines())


# ── rolling-week state ──────────────────────────────────────────────────────

def test_week_start_is_monday():
    # 2026-07-09 is a Thursday -> Monday is 2026-07-06
    assert budget.week_start("2026-07-09") == "2026-07-06"
    assert budget.week_start("2026-07-06") == "2026-07-06"  # Monday itself


def test_week_start_never_crashes_on_a_non_iso_run_label():
    """Regression: every OTHER pipeline command (normalize, dedupe, ...)
    treats --date as an opaque run-folder label, not a strict calendar date
    (this repo's own QA runs are labeled "qa-p27-actor", "qa-hardening-01").
    The guard must degrade gracefully (fall back to real today), never crash
    `discover` over a non-ISO label."""
    import datetime as _dt
    real_monday = budget.week_start(_dt.date.today().isoformat())
    assert budget.week_start("qa-p27-actor") == real_monday
    assert budget.week_start("2026-07-09-e2e") == real_monday
    assert budget.week_start("") == real_monday
    assert budget.week_start(None) == real_monday


def test_state_resets_on_new_week(tmp_path):
    budget.save_state(tmp_path, {"week_start": "2026-06-29", "records": 480, "requests": 10})
    # a date in the NEXT week -> fresh counter
    state = budget.load_state(tmp_path, "2026-07-09")
    assert state == {"week_start": "2026-07-06", "records": 0, "requests": 0}


def test_state_persists_within_week(tmp_path):
    budget.save_state(tmp_path, {"week_start": "2026-07-06", "records": 200, "requests": 4})
    state = budget.load_state(tmp_path, "2026-07-09")  # same week
    assert state["records"] == 200 and state["requests"] == 4


def test_record_consumption_accumulates():
    state = {"week_start": "2026-07-06", "records": 100, "requests": 2}
    budget.record_consumption(state, records=45, requests=1)
    assert state["records"] == 145 and state["requests"] == 3


def test_load_state_tolerates_corrupt_file(tmp_path):
    (tmp_path / budget.BUDGET_FILENAME).write_text("{not json")
    state = budget.load_state(tmp_path, "2026-07-09")
    assert state == {"week_start": "2026-07-06", "records": 0, "requests": 0}


# ── last-error diagnostics (P2.9) ────────────────────────────────────────────

def test_load_last_error_none_when_no_file(tmp_path):
    assert budget.load_last_error(tmp_path) is None


def test_record_and_load_last_error_roundtrip(tmp_path):
    budget.record_last_error(tmp_path, "2026-07-10", "API key rejected (HTTP 401)")
    err = budget.load_last_error(tmp_path)
    assert err == {"date": "2026-07-10", "message": "API key rejected (HTTP 401)"}


def test_record_last_error_creates_careeros_dir(tmp_path):
    nested = tmp_path / "not-yet-created"
    budget.record_last_error(nested, "2026-07-10", "boom")
    assert budget.load_last_error(nested) is not None


def test_clear_last_error_removes_the_file(tmp_path):
    budget.record_last_error(tmp_path, "2026-07-10", "boom")
    budget.clear_last_error(tmp_path)
    assert budget.load_last_error(tmp_path) is None


def test_clear_last_error_is_a_noop_when_nothing_recorded(tmp_path):
    budget.clear_last_error(tmp_path)  # must not raise
    assert budget.load_last_error(tmp_path) is None


def test_record_last_error_overwrites_previous(tmp_path):
    budget.record_last_error(tmp_path, "2026-07-09", "first failure")
    budget.record_last_error(tmp_path, "2026-07-10", "second failure")
    err = budget.load_last_error(tmp_path)
    assert err == {"date": "2026-07-10", "message": "second failure"}


def test_load_last_error_tolerates_corrupt_file(tmp_path):
    (tmp_path / budget.LAST_ERROR_FILENAME).write_text("{not json")
    assert budget.load_last_error(tmp_path) is None


# ── prevent (check_before_run) ──────────────────────────────────────────────

def test_check_no_quota_never_prevents():
    ok, msg = budget.check_before_run({"records": 10_000}, None)
    assert ok is True and msg is None


def test_check_prevents_when_exhausted():
    ok, msg = budget.check_before_run({"week_start": "2026-07-06", "records": 500}, 500)
    assert ok is False
    assert "quota reached" in msg


def test_check_allows_with_remaining_message():
    ok, msg = budget.check_before_run({"week_start": "2026-07-06", "records": 300}, 500)
    assert ok is True
    assert "200 remaining" in msg
