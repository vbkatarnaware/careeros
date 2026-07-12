"""Tests for `careeros doctor --live` (careeros/cli.py's
`_run_doctor_live_checks`) — the 2026-07-12 fix for a real incident: quota
was only ever a locally stored/calculated guess (a Monday-reset counter
independent of which API key was configured), so replacing an exhausted
Fantastic Jobs key still reported "quota exhausted" with no live
verification. `--live` actually reaches each enabled provider's real API.
No real network calls in these tests: `requests.get` / `ApifyClient` are
mocked, matching this repo's existing test patterns."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from careeros.cli import _CheckStatus, _run_doctor_live_checks
from careeros.config import Config


def _cfg(**overrides) -> Config:
    provider_name = overrides.get("provider", "fantastic-jobs")
    defaults = dict(
        provider="fantastic-jobs",
        threshold=4.0, consider_threshold=3.5,
        gate_batch_size=50, description_max_chars=4000,
        goals={}, prompts={},
        sheets={}, apify={}, api={"transport": "direct"}, fx_rates={},
        drive={"enabled": False},
        providers={provider_name: {"enabled": True}},
    )
    defaults.update(overrides)
    return Config(**defaults)


def _status_for(results, label_substr):
    for status, label, detail in results:
        if label_substr in label:
            return status, detail
    return None, None


def _fj_resp(status_code=200, headers=None, json_body=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = headers or {}
    resp.json.return_value = json_body if json_body is not None else []
    return resp


def test_live_fj_check_reports_real_remaining_quota(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FANTASTIC_API_KEY", "fake-key")
    cfg = _cfg()

    resp = _fj_resp(headers={"x-ratelimit-requests-remaining": "99", "x-ratelimit-jobs-remaining": "480"})
    with patch("requests.get", return_value=resp):
        results = _run_doctor_live_checks(cfg)

    status, detail = _status_for(results, "Fantastic Jobs (LIVE)")
    assert status == _CheckStatus.PASS
    assert "requests_remaining=99" in detail
    assert "jobs_remaining=480" in detail


def test_live_fj_check_fails_on_rejected_key(tmp_path, monkeypatch):
    """This is the exact incident: a live check must actually SURFACE a real
    rejection instead of silently trusting stale local state."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FANTASTIC_API_KEY", "fake-key")
    cfg = _cfg()

    resp = _fj_resp(status_code=401)
    with patch("requests.get", return_value=resp):
        results = _run_doctor_live_checks(cfg)

    status, detail = _status_for(results, "Fantastic Jobs (LIVE)")
    assert status == _CheckStatus.FAIL
    assert "API key rejected" in detail


def test_live_apify_check_reports_real_spend(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APIFY_TOKENS", "tok-abc")
    cfg = _cfg(provider="naukri", providers={"naukri": {"enabled": True, "max_monthly_budget_usd": 10}})

    fake_usage = MagicMock()
    fake_usage.total_usage_credits_usd_after_volume_discount = 3.21
    fake_client = MagicMock()
    fake_client.user.return_value.monthly_usage.return_value = fake_usage

    with patch("apify_client.ApifyClient", return_value=fake_client):
        results = _run_doctor_live_checks(cfg)

    status, detail = _status_for(results, "Apify token 1/1 (LIVE")
    assert status == _CheckStatus.PASS
    assert "$3.2100 used this billing cycle" in detail


def test_live_apify_check_fails_on_rejected_token(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APIFY_TOKENS", "tok-dead")
    cfg = _cfg(provider="naukri", providers={"naukri": {"enabled": True, "max_monthly_budget_usd": 10}})

    from apify_client.errors import ApifyApiError

    fake_client = MagicMock()
    fake_client.user.return_value.monthly_usage.side_effect = ApifyApiError(
        MagicMock(status_code=401, json=lambda: {"error": {"message": "token rejected"}}), 0
    )

    with patch("apify_client.ApifyClient", return_value=fake_client):
        results = _run_doctor_live_checks(cfg)

    status, detail = _status_for(results, "Apify token 1/1 (LIVE")
    assert status == _CheckStatus.FAIL
    assert "rejected/exhausted" in detail
