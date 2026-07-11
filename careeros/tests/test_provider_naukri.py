"""Tests for careeros/providers/naukri.py — the v1.2 Apify-actor-based
Naukri provider (`memo23/naukri-scraper`). No real network/Apify calls:
`_build_run_input` is a pure function tested directly, and `fetch()` is
tested with `careeros.providers.naukri.run_actor` mocked out entirely.

The `to_job_dict` fixtures below are trimmed real dataset items captured
live via `careeros discover --provider naukri --dry-run` on 2026-07-10
(3 real jobs, $0.0050 total) — not invented shapes. Naukri's real output is
deeply nested (company under `companyDetail.name`, location as a `locations`
array, salary under `salaryDetail`), unlike the flat candidate-key pattern
most other providers can rely on."""

from __future__ import annotations

from unittest.mock import patch

from careeros.providers.base import ProviderResult
from careeros.providers.naukri import PROVIDER, _build_run_input


# ── _build_run_input: pure function, no mocking needed ──────────────────

def test_build_run_input_defaults_when_nothing_configured():
    run_input = _build_run_input({}, limit=25, search="")
    assert run_input["searchQuery"] == "Product Manager"
    assert run_input["location"] == "India"
    assert run_input["maximumJobs"] == 25
    assert run_input["cleanHtml"] is True


def test_build_run_input_uses_configured_search_query_and_location():
    cfg = {"search_query": "Growth PM", "location": "Bangalore"}
    run_input = _build_run_input(cfg, limit=10, search="")
    assert run_input["searchQuery"] == "Growth PM"
    assert run_input["location"] == "Bangalore"


def test_build_run_input_search_kwarg_overrides_config():
    cfg = {"search_query": "Growth PM"}
    run_input = _build_run_input(cfg, limit=10, search="Data Analyst")
    assert run_input["searchQuery"] == "Data Analyst"


def test_build_run_input_limit_maps_to_maximum_jobs():
    run_input = _build_run_input({}, limit=50, search="")
    assert run_input["maximumJobs"] == 50


def test_build_run_input_limit_floors_at_one():
    run_input = _build_run_input({}, limit=0, search="")
    assert run_input["maximumJobs"] == 1


def test_build_run_input_omits_optional_filters_when_unset():
    run_input = _build_run_input({}, limit=10, search="")
    for key in (
        "workMode", "timeFilter", "experienceLevel", "jobType",
        "industry", "roleCategory", "companyType", "minSalary",
    ):
        assert key not in run_input


def test_build_run_input_includes_optional_filters_when_configured():
    cfg = {
        "work_mode": "Remote",
        "time_filter": "7",
        "experience_level": 3,
        "job_type": "Full Time",
        "industry": "IT Services",
        "role_category": "Product Management",
        "company_type": "Startup",
        "min_salary": 1500000,
    }
    run_input = _build_run_input(cfg, limit=10, search="")
    assert run_input["workMode"] == "Remote"
    assert run_input["timeFilter"] == "7"
    assert run_input["experienceLevel"] == 3
    assert run_input["jobType"] == "Full Time"
    assert run_input["industry"] == "IT Services"
    assert run_input["roleCategory"] == "Product Management"
    assert run_input["companyType"] == "Startup"
    assert run_input["minSalary"] == 1500000


# ── to_job_dict: real live-captured Naukri shape (2026-07-10) ────────────

def _real_naukri_item(**overrides):
    item = {
        "title": "Product Manager",
        "url": "https://www.naukri.com/job-listings-product-manager-tata-consultancy-services-pune-mumbai-all-areas-2-to-4-years-090726024721",
        "shortDescription": "Own the roadmap for our flagship product.",
        "basicInfo": {"companyName": "Tata Consultancy Services"},
        "companyDetail": {"name": "Tata Consultancy Services"},
        "locations": [{"label": "Pune"}, {"label": "Mumbai (All Areas)"}],
        "wfhLabel": "Work from office",
        "employmentType": "Full Time, Permanent",
        "createdDate": "2026-07-09 16:31:59",
        "salaryDetail": {"currency": "INR", "hideSalary": True, "maximumSalary": 0, "minimumSalary": 0},
    }
    item.update(overrides)
    return item


def test_to_job_dict_maps_real_naukri_shape():
    job = PROVIDER.to_job_dict(_real_naukri_item())
    assert job == {
        "title": "Product Manager",
        "company": "Tata Consultancy Services",
        "apply_url": "https://www.naukri.com/job-listings-product-manager-tata-consultancy-services-pune-mumbai-all-areas-2-to-4-years-090726024721",
        "description": "Own the roadmap for our flagship product.",
        "location": "Pune, Mumbai (All Areas)",
        "remote": False,
        "employment_type": "Full Time, Permanent",
        "seniority": None,
        "posted_at": "2026-07-09 16:31:59",
        "salary": None,  # hideSalary: True in the real sample -> no salary dict
        "contact": None,
        "company_linkedin": None,
    }


def test_to_job_dict_maps_company_from_company_detail_name():
    raw = _real_naukri_item(companyDetail={"name": "Cybage"}, basicInfo={})
    assert PROVIDER.to_job_dict(raw)["company"] == "Cybage"


def test_to_job_dict_falls_back_to_basic_info_company_name():
    raw = _real_naukri_item(companyDetail={}, basicInfo={"companyName": "Cybage"})
    assert PROVIDER.to_job_dict(raw)["company"] == "Cybage"


def test_to_job_dict_defaults_company_to_unknown_when_neither_present():
    raw = _real_naukri_item(companyDetail={}, basicInfo={})
    assert PROVIDER.to_job_dict(raw)["company"] == "Unknown"


def test_to_job_dict_joins_multiple_location_labels():
    raw = _real_naukri_item(locations=[{"label": "Hyderabad"}, {"label": "Bengaluru"}])
    assert PROVIDER.to_job_dict(raw)["location"] == "Hyderabad, Bengaluru"


def test_to_job_dict_location_none_when_locations_missing():
    raw = _real_naukri_item(locations=None)
    assert PROVIDER.to_job_dict(raw)["location"] is None


def test_to_job_dict_remote_true_when_wfh_label_mentions_home():
    raw = _real_naukri_item(wfhLabel="Work from home")
    assert PROVIDER.to_job_dict(raw)["remote"] is True


def test_to_job_dict_remote_none_for_ambiguous_hybrid_label():
    raw = _real_naukri_item(wfhLabel="Hybrid")
    assert PROVIDER.to_job_dict(raw)["remote"] is None


def test_to_job_dict_salary_none_when_hide_salary_true():
    raw = _real_naukri_item(salaryDetail={"currency": "INR", "hideSalary": True, "minimumSalary": 0, "maximumSalary": 0})
    assert PROVIDER.to_job_dict(raw)["salary"] is None


def test_to_job_dict_salary_mapped_when_visible_and_nonzero():
    raw = _real_naukri_item(
        salaryDetail={"currency": "INR", "hideSalary": False, "minimumSalary": 800000, "maximumSalary": 1200000}
    )
    assert PROVIDER.to_job_dict(raw)["salary"] == {"min": 800000, "max": 1200000, "currency": "INR", "unit": "year"}


def test_to_job_dict_returns_none_when_title_missing():
    raw = _real_naukri_item(title="")
    assert PROVIDER.to_job_dict(raw) is None


def test_to_job_dict_returns_none_when_url_missing():
    raw = _real_naukri_item(url="")
    assert PROVIDER.to_job_dict(raw) is None


def test_to_job_dict_returns_none_when_url_not_http():
    raw = _real_naukri_item(url="naukri.com/job/123")
    assert PROVIDER.to_job_dict(raw) is None


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
    fake_result = ProviderResult(provider="naukri", items=[{"title": "x"}])

    class FakeConfig:
        apify = {"tokens_env": "APIFY_TOKENS"}
        providers = {"naukri": {"search_query": "Growth PM", "location": "Delhi"}}

    with patch("careeros.providers.naukri.run_actor", return_value=fake_result) as mock_run:
        result = PROVIDER.fetch(FakeConfig(), limit=20, search="")

    assert result is fake_result
    mock_run.assert_called_once()
    args, kwargs = mock_run.call_args
    assert args[0] == "naukri"
    assert args[1] == {"tokens_env": "APIFY_TOKENS"}
    assert args[2] == "memo23/naukri-scraper"
    run_input = args[3]
    assert run_input["searchQuery"] == "Growth PM"
    assert run_input["location"] == "Delhi"
    assert run_input["maximumJobs"] == 20
    assert kwargs["max_cost_usd"] is None


def test_fetch_uses_configured_actor_override():
    fake_result = ProviderResult(provider="naukri")

    class FakeConfig:
        apify = {}
        providers = {"naukri": {"actor": "someone-else/naukri-fork"}}

    with patch("careeros.providers.naukri.run_actor", return_value=fake_result) as mock_run:
        PROVIDER.fetch(FakeConfig(), limit=10)

    args, _ = mock_run.call_args
    assert args[2] == "someone-else/naukri-fork"


def test_fetch_passes_max_cost_usd_from_provider_cfg():
    fake_result = ProviderResult(provider="naukri")

    class FakeConfig:
        apify = {}
        providers = {"naukri": {"max_cost_usd": 2.5}}

    with patch("careeros.providers.naukri.run_actor", return_value=fake_result) as mock_run:
        PROVIDER.fetch(FakeConfig(), limit=10)

    _, kwargs = mock_run.call_args
    assert kwargs["max_cost_usd"] == 2.5


def test_fetch_handles_missing_provider_config_block():
    fake_result = ProviderResult(provider="naukri")

    class FakeConfig:
        apify = {}
        providers: dict = {}

    with patch("careeros.providers.naukri.run_actor", return_value=fake_result) as mock_run:
        result = PROVIDER.fetch(FakeConfig(), limit=10)

    assert result is fake_result
    args, _ = mock_run.call_args
    assert args[3]["searchQuery"] == "Product Manager"
    assert args[3]["location"] == "India"


def test_fetch_accepts_query_kwarg_as_noop():
    """query (segmented-discovery spec) is accepted but ignored — this actor
    has no documented multi-field query mapping."""
    fake_result = ProviderResult(provider="naukri")

    class FakeConfig:
        apify = {}
        providers: dict = {}

    with patch("careeros.providers.naukri.run_actor", return_value=fake_result):
        result = PROVIDER.fetch(FakeConfig(), limit=10, query={"location_search": ["India"]})

    assert result is fake_result
