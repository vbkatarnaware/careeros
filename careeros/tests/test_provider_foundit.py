"""Tests for careeros/providers/foundit.py — the v1.2 Apify-actor-based
Foundit provider (`shahidirfan/Foundit-Jobs-Scraper`). No real network/Apify
calls: `_build_run_input` is a pure function tested directly, and `fetch()`
is tested with `careeros.providers.foundit.run_actor` mocked out entirely.

The `to_job_dict` fixtures below are trimmed real dataset items captured
live via `careeros discover --provider foundit --dry-run` on 2026-07-10
($0.0005 total for 1 job). Most fields already resolve through the shared
candidate-key pattern; `salary` (a compound string like "INR 500000-
1000000") and `employment_type` (a flat key that wasn't being read at all)
were the real gaps fixed here."""

from __future__ import annotations

from unittest.mock import patch

from careeros.providers.base import ProviderResult
from careeros.providers.foundit import PROVIDER, _build_run_input


# ── _build_run_input: pure function, no mocking needed ──────────────────

def test_build_run_input_defaults_when_nothing_configured():
    run_input = _build_run_input({}, limit=25, search="")
    assert run_input["keyword"] == "Product Manager"
    assert run_input["location"] == "India"
    assert run_input["results_wanted"] == 25


def test_build_run_input_uses_configured_keyword_and_location():
    cfg = {"keyword": "Data Analyst", "location": "Bangalore"}
    run_input = _build_run_input(cfg, limit=10, search="")
    assert run_input["keyword"] == "Data Analyst"
    assert run_input["location"] == "Bangalore"


def test_build_run_input_search_kwarg_overrides_config_keyword():
    cfg = {"keyword": "Data Analyst"}
    run_input = _build_run_input(cfg, limit=10, search="Growth PM")
    assert run_input["keyword"] == "Growth PM"


def test_build_run_input_limit_maps_to_results_wanted():
    run_input = _build_run_input({}, limit=50, search="")
    assert run_input["results_wanted"] == 50


def test_build_run_input_results_wanted_floors_at_one():
    run_input = _build_run_input({}, limit=0, search="")
    assert run_input["results_wanted"] == 1


def test_build_run_input_omits_max_pages_when_unset():
    run_input = _build_run_input({}, limit=10, search="")
    assert "max_pages" not in run_input


def test_build_run_input_includes_max_pages_when_configured():
    run_input = _build_run_input({"max_pages": 3}, limit=10, search="")
    assert run_input["max_pages"] == 3


# ── to_job_dict: real live-captured Foundit shape (2026-07-10) ───────────

def _real_foundit_item(**overrides):
    item = {
        "title": "Territory Sales Manager - Genset Sales - Pan India",
        "url": "https://www.foundit.in/job/territory-sales-manager-genset-sales-pan-india-nova-human-resources-outsourcing-private-limited-other-india-53246273",
        "apply_url": None,
        "company": "Nova Human Resources Outsourcing Private Limited",
        "location": "Other India, India",
        "salary": "INR 500000-1000000",
        "employment_type": "Full time",
        "date_posted": "2026-05-20T09:24:30.000Z",
        "description_text": "Drive sales of core gensets across the assigned state.",
    }
    item.update(overrides)
    return item


def test_to_job_dict_maps_real_foundit_shape():
    job = PROVIDER.to_job_dict(_real_foundit_item())
    assert job == {
        "title": "Territory Sales Manager - Genset Sales - Pan India",
        "company": "Nova Human Resources Outsourcing Private Limited",
        "apply_url": "https://www.foundit.in/job/territory-sales-manager-genset-sales-pan-india-nova-human-resources-outsourcing-private-limited-other-india-53246273",
        "description": "Drive sales of core gensets across the assigned state.",
        "location": "Other India, India",
        "remote": None,
        "employment_type": "Full time",
        "seniority": None,
        "posted_at": "2026-05-20T09:24:30.000Z",
        "salary": {"min": 500000.0, "max": 1000000.0, "currency": "INR", "unit": "year"},
        "contact": None,
        "company_linkedin": None,
    }


def test_to_job_dict_salary_none_when_string_unparseable():
    raw = _real_foundit_item(salary="Not disclosed")
    assert PROVIDER.to_job_dict(raw)["salary"] is None


def test_to_job_dict_salary_none_when_missing():
    raw = _real_foundit_item(salary=None)
    assert PROVIDER.to_job_dict(raw)["salary"] is None


def test_to_job_dict_salary_handles_comma_separated_thousands():
    raw = _real_foundit_item(salary="USD 50,000-80,000")
    assert PROVIDER.to_job_dict(raw)["salary"] == {"min": 50000.0, "max": 80000.0, "currency": "USD", "unit": "year"}


def test_to_job_dict_returns_none_when_title_missing():
    raw = _real_foundit_item(title="")
    assert PROVIDER.to_job_dict(raw) is None


def test_to_job_dict_returns_none_when_url_missing():
    raw = _real_foundit_item(url="")
    assert PROVIDER.to_job_dict(raw) is None


def test_to_job_dict_returns_none_when_url_not_http():
    raw = _real_foundit_item(url="foundit.in/job/123")
    assert PROVIDER.to_job_dict(raw) is None


def test_to_job_dict_defaults_company_to_unknown():
    raw = _real_foundit_item(company="")
    job = PROVIDER.to_job_dict(raw)
    assert job["company"] == "Unknown"


# ── validate(): delegates to validate_apify_token ────────────────────────

def test_validate_empty_when_token_configured(monkeypatch):
    monkeypatch.setenv("APIFY_TOKEN", "tok")
    monkeypatch.delenv("APIFY_TOKENS", raising=False)

    class FakeConfig:
        apify: dict = {}

    assert PROVIDER.validate(FakeConfig()) == []


def test_validate_non_empty_when_no_token_configured(monkeypatch):
    monkeypatch.delenv("APIFY_TOKEN", raising=False)
    monkeypatch.delenv("APIFY_TOKENS", raising=False)

    class FakeConfig:
        apify: dict = {}

    errors = PROVIDER.validate(FakeConfig())
    assert len(errors) == 1
    assert "No Apify token configured" in errors[0]


# ── fetch(): run_actor mocked, no network ────────────────────────────────

def test_fetch_passes_actor_id_and_run_input_to_run_actor():
    fake_result = ProviderResult(provider="foundit", items=[{"title": "x"}])

    class FakeConfig:
        apify = {"tokens_env": "APIFY_TOKENS"}
        providers = {"foundit": {"keyword": "Growth PM", "location": "Delhi"}}

    with patch("careeros.providers.foundit.run_actor", return_value=fake_result) as mock_run:
        result = PROVIDER.fetch(FakeConfig(), limit=20, search="")

    assert result is fake_result
    mock_run.assert_called_once()
    args, kwargs = mock_run.call_args
    assert args[0] == "foundit"
    assert args[1] == {"tokens_env": "APIFY_TOKENS"}
    assert args[2] == "shahidirfan/Foundit-Jobs-Scraper"
    run_input = args[3]
    assert run_input["keyword"] == "Growth PM"
    assert run_input["location"] == "Delhi"
    assert run_input["results_wanted"] == 20
    assert kwargs["max_cost_usd"] is None


def test_fetch_uses_configured_actor_override():
    fake_result = ProviderResult(provider="foundit")

    class FakeConfig:
        apify = {}
        providers = {"foundit": {"actor": "someone-else/foundit-fork"}}

    with patch("careeros.providers.foundit.run_actor", return_value=fake_result) as mock_run:
        PROVIDER.fetch(FakeConfig(), limit=10)

    args, _ = mock_run.call_args
    assert args[2] == "someone-else/foundit-fork"


def test_fetch_passes_max_cost_usd_from_provider_cfg():
    fake_result = ProviderResult(provider="foundit")

    class FakeConfig:
        apify = {}
        providers = {"foundit": {"max_cost_usd": 2.5}}

    with patch("careeros.providers.foundit.run_actor", return_value=fake_result) as mock_run:
        PROVIDER.fetch(FakeConfig(), limit=10)

    _, kwargs = mock_run.call_args
    assert kwargs["max_cost_usd"] == 2.5


def test_fetch_handles_missing_provider_config_block():
    fake_result = ProviderResult(provider="foundit")

    class FakeConfig:
        apify = {}
        providers: dict = {}

    with patch("careeros.providers.foundit.run_actor", return_value=fake_result) as mock_run:
        result = PROVIDER.fetch(FakeConfig(), limit=10)

    assert result is fake_result
    args, _ = mock_run.call_args
    assert args[3]["keyword"] == "Product Manager"
    assert args[3]["location"] == "India"


def test_fetch_accepts_query_kwarg_as_noop():
    """query (segmented-discovery spec) is accepted but ignored — this
    actor's input schema doesn't offer enough verified fields to justify an
    overlay pattern that hasn't been confirmed against real actor behavior."""
    fake_result = ProviderResult(provider="foundit")

    class FakeConfig:
        apify = {}
        providers: dict = {}

    with patch("careeros.providers.foundit.run_actor", return_value=fake_result):
        result = PROVIDER.fetch(FakeConfig(), limit=10, query={"location_search": ["India"]})

    assert result is fake_result
