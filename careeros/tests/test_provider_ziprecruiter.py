"""Tests for careeros/providers/ziprecruiter.py — the run-input builder and
field mapper are pure functions tested directly (no mocking, no network).
`fetch()` mocks `careeros.providers.ziprecruiter.run_actor` so no real Apify
call is ever made. This actor has a known ~63% run-success rate (see the
module docstring) — the `fetch()` tests include a `ProviderError` propagation
case specifically because that failure mode is expected to happen often in
practice and must surface cleanly, not be swallowed."""

from __future__ import annotations

from unittest.mock import patch

from careeros.providers.base import ProviderError, ProviderResult
from careeros.providers.ziprecruiter import PROVIDER, _build_run_input


# ── _build_run_input ──────────────────────────────────────────────────────

def test_build_run_input_defaults_when_nothing_configured():
    run_input = _build_run_input({}, limit=50, search="")
    assert run_input["search"] == "Product Manager"
    assert run_input["location"] == "United States"
    assert run_input["maxItems"] == 50


def test_build_run_input_search_kwarg_overrides_config():
    run_input = _build_run_input({"search": "Data Analyst"}, limit=10, search="Growth PM")
    assert run_input["search"] == "Growth PM"


def test_build_run_input_uses_config_search_when_no_kwarg():
    run_input = _build_run_input({"search": "Data Analyst"}, limit=10, search="")
    assert run_input["search"] == "Data Analyst"


def test_build_run_input_uses_config_location():
    run_input = _build_run_input({"location": "Remote"}, limit=10, search="")
    assert run_input["location"] == "Remote"


def test_build_run_input_limit_maps_to_max_items():
    run_input = _build_run_input({}, limit=25, search="")
    assert run_input["maxItems"] == 25


def test_build_run_input_limit_floored_at_one():
    run_input = _build_run_input({}, limit=0, search="")
    assert run_input["maxItems"] == 1


def test_build_run_input_remote_only_omitted_by_default():
    run_input = _build_run_input({}, limit=10, search="")
    assert "remoteOnly" not in run_input


def test_build_run_input_remote_only_included_when_configured():
    run_input = _build_run_input({"remote_only": True}, limit=10, search="")
    assert run_input["remoteOnly"] is True


def test_build_run_input_days_posted_omitted_by_default():
    run_input = _build_run_input({}, limit=10, search="")
    assert "daysPosted" not in run_input


def test_build_run_input_days_posted_included_when_configured():
    run_input = _build_run_input({"days_posted": 7}, limit=10, search="")
    assert run_input["daysPosted"] == 7


# ── to_job_dict: real live-captured ZipRecruiter shape (2026-07-10) ──────
# title/url/company/location/description all resolve via the shared
# candidate-key pattern in the real live sample — only salary needed a
# dedicated mapper (present in just 1 of 3 real samples, already numeric).

def _real_zip_item(**overrides):
    item = {
        "title": "Senior Product Manager, Intelligent Sales Platform",
        "url": "https://www.ziprecruiter.com/c/Vanguard/Job/Senior-Product-Manager,-Intelligent-Sales-Platform/-in-Malvern,PA?jid=8fa816e3288b5286",
        "company": "Vanguard",
        "location": "Malvern, PA",
        "salaryMin": 124000,
        "salaryMax": 163000,
        "salaryPeriod": "year",
    }
    item.update(overrides)
    return item


def test_to_job_dict_maps_real_ziprecruiter_shape():
    job = PROVIDER.to_job_dict(_real_zip_item())
    assert job == {
        "title": "Senior Product Manager, Intelligent Sales Platform",
        "company": "Vanguard",
        "apply_url": "https://www.ziprecruiter.com/c/Vanguard/Job/Senior-Product-Manager,-Intelligent-Sales-Platform/-in-Malvern,PA?jid=8fa816e3288b5286",
        "description": None,
        "location": "Malvern, PA",
        "remote": None,
        "employment_type": None,
        "seniority": None,
        "posted_at": None,
        "salary": {"min": 124000, "max": 163000, "currency": "USD", "unit": "year"},
        "contact": None,
        "company_linkedin": None,
    }


def test_to_job_dict_salary_none_when_min_and_max_both_missing():
    """Live sample: salary is absent on 2 of 3 real jobs."""
    raw = _real_zip_item(salaryMin=None, salaryMax=None, salaryPeriod=None)
    assert PROVIDER.to_job_dict(raw)["salary"] is None


def test_to_job_dict_salary_assumes_usd_currency():
    raw = _real_zip_item(salaryMin=50000, salaryMax=70000, salaryPeriod="year")
    assert PROVIDER.to_job_dict(raw)["salary"]["currency"] == "USD"


def test_to_job_dict_company_falls_back_to_unknown():
    raw = _real_zip_item(company="")
    job = PROVIDER.to_job_dict(raw)
    assert job["company"] == "Unknown"


def test_to_job_dict_none_when_title_missing():
    raw = _real_zip_item(title="")
    assert PROVIDER.to_job_dict(raw) is None


def test_to_job_dict_none_when_url_missing():
    raw = _real_zip_item(url="")
    assert PROVIDER.to_job_dict(raw) is None


def test_to_job_dict_none_when_url_not_http():
    raw = _real_zip_item(url="not-a-url")
    assert PROVIDER.to_job_dict(raw) is None


# ── validate() ─────────────────────────────────────────────────────────────

def test_validate_delegates_to_validate_apify_token():
    with patch("careeros.providers.ziprecruiter.validate_apify_token") as mock_validate:
        mock_validate.return_value = ["some problem"]
        config = _FakeConfig(apify={"tokens_env": "APIFY_TOKENS"})
        result = PROVIDER.validate(config)
        mock_validate.assert_called_once_with(config.apify)
        assert result == ["some problem"]


# ── fetch() (run_actor mocked — no network) ────────────────────────────────

def test_fetch_passes_actor_id_and_run_input_through():
    config = _FakeConfig(
        apify={"tokens_env": "APIFY_TOKENS"},
        providers={"ziprecruiter": {"location": "Remote", "max_cost_usd": 2.5}},
    )
    with patch("careeros.providers.ziprecruiter.run_actor") as mock_run_actor:
        mock_run_actor.return_value = ProviderResult(provider="ziprecruiter", items=[{"title": "x"}])
        result = PROVIDER.fetch(config, limit=15, search="Growth PM")

    mock_run_actor.assert_called_once()
    args, kwargs = mock_run_actor.call_args
    assert args[0] == "ziprecruiter"
    assert args[1] == config.apify
    assert args[2] == "crawlerbros/ziprecruiter-scraper-pro"
    run_input = args[3]
    assert run_input["search"] == "Growth PM"
    assert run_input["location"] == "Remote"
    assert run_input["maxItems"] == 15
    assert kwargs["max_cost_usd"] == 2.5
    assert result.items == [{"title": "x"}]


def test_fetch_uses_default_actor_id_when_not_configured():
    config = _FakeConfig(apify={}, providers={})
    with patch("careeros.providers.ziprecruiter.run_actor") as mock_run_actor:
        mock_run_actor.return_value = ProviderResult(provider="ziprecruiter")
        PROVIDER.fetch(config, limit=10)
    args, _ = mock_run_actor.call_args
    assert args[2] == "crawlerbros/ziprecruiter-scraper-pro"


def test_fetch_uses_custom_actor_id_when_configured():
    config = _FakeConfig(apify={}, providers={"ziprecruiter": {"actor": "someone/custom-actor"}})
    with patch("careeros.providers.ziprecruiter.run_actor") as mock_run_actor:
        mock_run_actor.return_value = ProviderResult(provider="ziprecruiter")
        PROVIDER.fetch(config, limit=10)
    args, _ = mock_run_actor.call_args
    assert args[2] == "someone/custom-actor"


def test_fetch_propagates_provider_error_from_run_actor():
    """This actor's known ~63% run-success rate means a ProviderError here is
    expected to happen more often than for other providers — it must
    propagate cleanly to the caller (`discover`), not be caught/swallowed."""
    config = _FakeConfig(apify={}, providers={})
    with patch("careeros.providers.ziprecruiter.run_actor") as mock_run_actor:
        mock_run_actor.side_effect = ProviderError("ziprecruiter: all tokens failed")
        try:
            PROVIDER.fetch(config, limit=10)
            assert False, "expected ProviderError to propagate"
        except ProviderError as e:
            assert "ziprecruiter" in str(e)


class _FakeConfig:
    """Minimal stand-in for careeros.config.Config — only the attributes
    ziprecruiter.py actually reads (`apify`, `providers`)."""

    def __init__(self, apify=None, providers=None):
        self.apify = apify or {}
        self.providers = providers or {}
