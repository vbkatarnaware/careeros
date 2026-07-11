"""Tests for careeros/providers/indeed.py: run-input construction (pure,
no network) and `to_job_dict`'s defensive field mapping. `fetch()` is tested
with `careeros.providers.indeed.run_actor` mocked — no real Apify/network
call, same pattern as test_provider_fantastic_jobs_actor.py but against the
shared `_apify_actor_common.run_actor` helper this provider delegates to."""

from __future__ import annotations

from unittest.mock import patch

from careeros.providers.base import ProviderResult
from careeros.providers.indeed import ACTOR_ID, PROVIDER, _build_run_input


# ── _build_run_input: pure, no mocking ──────────────────────────────────

def test_build_run_input_defaults_when_nothing_configured():
    run_input = _build_run_input({}, limit=50, search="")
    assert run_input["title"] == "Product Manager"
    assert run_input["location"] == "India"
    assert run_input["country"] == "in"
    assert run_input["limit"] == 50


def test_build_run_input_uses_configured_title_location_country():
    cfg = {"title": "Data Analyst", "location": "Bengaluru", "country": "us"}
    run_input = _build_run_input(cfg, limit=10, search="")
    assert run_input["title"] == "Data Analyst"
    assert run_input["location"] == "Bengaluru"
    assert run_input["country"] == "us"


def test_build_run_input_search_overrides_configured_title():
    cfg = {"title": "Data Analyst"}
    run_input = _build_run_input(cfg, limit=10, search="Growth PM")
    assert run_input["title"] == "Growth PM"


def test_build_run_input_limit_maps_to_actor_limit():
    run_input = _build_run_input({}, limit=25, search="")
    assert run_input["limit"] == 25


def test_build_run_input_limit_floored_at_one():
    run_input = _build_run_input({}, limit=0, search="")
    assert run_input["limit"] == 1


def test_build_run_input_date_posted_omitted_when_not_configured():
    run_input = _build_run_input({}, limit=10, search="")
    assert "datePosted" not in run_input


def test_build_run_input_date_posted_included_when_configured():
    run_input = _build_run_input({"date_posted": "last 7 days"}, limit=10, search="")
    assert run_input["datePosted"] == "last 7 days"


# ── to_job_dict: real live-captured Indeed shape (2026-07-10) ────────────
# Indeed's real output is deeply nested — company under employer.name,
# description a {html, text} dict, location a dict of admin codes, salary
# under baseSalary, employment type inside a jobTypes dict keyed by code.
# Live results for "Product Manager" were, notably, all irrelevant roles
# (Sales Manager / E-Commerce GM / Performance Marketing Manager) — a data
# quality caveat documented in the module docstring, not a mapping bug.

def _real_indeed_item(**overrides):
    item = {
        "title": "E Commerce General Manager - Night shift",
        "url": "https://in.indeed.com/viewjob?jk=ae88e8d5f8c5eaab",
        "employer": {"name": "ICT Tubes Private Limited"},
        "description": {
            "html": "<p>Own e-commerce ops.</p>",
            "text": "Own e-commerce ops.",
        },
        "location": {"city": "", "admin1Code": "GJ", "countryName": "India"},
        "baseSalary": {"currencyCode": "INR", "min": 12641.38, "max": 55000, "unitOfWork": "MONTH"},
        "jobTypes": {"5QWDV": "Permanent", "CF3CP": "Full-time"},
        "datePublished": "2026-07-10T17:26:48.704Z",
        "dateOnIndeed": "2026-07-10T17:26:48.704Z",
    }
    item.update(overrides)
    return item


def test_to_job_dict_maps_real_indeed_shape():
    job = PROVIDER.to_job_dict(_real_indeed_item())
    assert job == {
        "title": "E Commerce General Manager - Night shift",
        "company": "ICT Tubes Private Limited",
        "apply_url": "https://in.indeed.com/viewjob?jk=ae88e8d5f8c5eaab",
        "description": "Own e-commerce ops.",
        "location": "GJ, India",
        "remote": None,
        "employment_type": "Permanent",
        "seniority": None,
        "posted_at": "2026-07-10T17:26:48.704Z",
        "salary": {"min": 12641.38, "max": 55000, "currency": "INR", "unit": "month"},
        "contact": None,
        "company_linkedin": None,
    }


def test_to_job_dict_falls_back_to_unknown_company_when_employer_missing():
    raw = _real_indeed_item(employer={})
    assert PROVIDER.to_job_dict(raw)["company"] == "Unknown"


def test_to_job_dict_description_falls_back_to_html_when_text_missing():
    raw = _real_indeed_item(description={"html": "<p>HTML only</p>"})
    assert PROVIDER.to_job_dict(raw)["description"] == "<p>HTML only</p>"


def test_to_job_dict_location_skips_empty_city():
    raw = _real_indeed_item(location={"city": "", "admin1Code": "MH", "countryName": "India"})
    assert PROVIDER.to_job_dict(raw)["location"] == "MH, India"


def test_to_job_dict_location_none_when_missing():
    raw = _real_indeed_item(location=None)
    assert PROVIDER.to_job_dict(raw)["location"] is None


def test_to_job_dict_salary_none_when_min_and_max_both_null():
    raw = _real_indeed_item(baseSalary={"currencyCode": "INR", "min": None, "max": None, "unitOfWork": "MONTH"})
    assert PROVIDER.to_job_dict(raw)["salary"] is None


def test_to_job_dict_employment_type_none_when_job_types_empty():
    raw = _real_indeed_item(jobTypes={})
    assert PROVIDER.to_job_dict(raw)["employment_type"] is None


def test_to_job_dict_posted_at_falls_back_to_date_on_indeed():
    raw = _real_indeed_item(datePublished=None, dateOnIndeed="2026-07-09T00:00:00.000Z")
    assert PROVIDER.to_job_dict(raw)["posted_at"] == "2026-07-09T00:00:00.000Z"


def test_to_job_dict_none_when_title_missing():
    raw = _real_indeed_item(title="")
    assert PROVIDER.to_job_dict(raw) is None


def test_to_job_dict_none_when_url_missing():
    raw = _real_indeed_item(url="")
    assert PROVIDER.to_job_dict(raw) is None


def test_to_job_dict_none_when_url_not_http():
    raw = _real_indeed_item(url="javascript:void(0)")
    assert PROVIDER.to_job_dict(raw) is None


# ── validate(): delegates to shared helper ───────────────────────────────

def test_validate_delegates_to_validate_apify_token(monkeypatch):
    monkeypatch.setenv("APIFY_TOKEN", "tok")
    monkeypatch.delenv("APIFY_TOKENS", raising=False)

    class FakeConfig:
        apify = {"token_env": "APIFY_TOKEN", "tokens_env": "APIFY_TOKENS"}

    assert PROVIDER.validate(FakeConfig()) == []


def test_validate_reports_missing_token(monkeypatch):
    monkeypatch.delenv("APIFY_TOKEN", raising=False)
    monkeypatch.delenv("APIFY_TOKENS", raising=False)

    class FakeConfig:
        apify = {"token_env": "APIFY_TOKEN", "tokens_env": "APIFY_TOKENS"}

    errors = PROVIDER.validate(FakeConfig())
    assert len(errors) == 1
    assert "No Apify token configured" in errors[0]


# ── fetch(): run_actor mocked, no network ────────────────────────────────

class FakeConfig:
    apify = {"tokens_env": "APIFY_TOKENS"}
    providers: dict = {}


def test_fetch_passes_actor_id_and_run_input_through():
    config = FakeConfig()
    config.providers = {
        "indeed": {"title": "Data Analyst", "location": "Mumbai", "country": "in"},
    }
    fake_result = ProviderResult(provider="indeed")
    with patch("careeros.providers.indeed.run_actor", return_value=fake_result) as mock_run_actor:
        result = PROVIDER.fetch(config, limit=15, search="")

    assert result is fake_result
    mock_run_actor.assert_called_once()
    args, kwargs = mock_run_actor.call_args
    assert args[0] == "indeed"
    assert args[1] is config.apify
    assert args[2] == ACTOR_ID
    assert args[3] == {
        "title": "Data Analyst", "location": "Mumbai", "country": "in", "limit": 15,
    }


def test_fetch_uses_configured_actor_override_and_max_cost():
    config = FakeConfig()
    config.providers = {
        "indeed": {"actor": "someone/custom-indeed-actor", "max_cost_usd": 2.5},
    }
    fake_result = ProviderResult(provider="indeed")
    with patch("careeros.providers.indeed.run_actor", return_value=fake_result) as mock_run_actor:
        PROVIDER.fetch(config, limit=10, search="")

    args, kwargs = mock_run_actor.call_args
    assert args[2] == "someone/custom-indeed-actor"
    assert kwargs["max_cost_usd"] == 2.5


def test_fetch_search_overrides_configured_title():
    config = FakeConfig()
    config.providers = {"indeed": {"title": "Data Analyst"}}
    fake_result = ProviderResult(provider="indeed")
    with patch("careeros.providers.indeed.run_actor", return_value=fake_result) as mock_run_actor:
        PROVIDER.fetch(config, limit=10, search="Growth PM")

    args, kwargs = mock_run_actor.call_args
    assert args[3]["title"] == "Growth PM"
