"""Tests for careeros/providers/glassdoor.py — the v1.2 Apify-actor-based
Glassdoor provider (`memo23/glassdoor-scraper-ppr`, NOT the rejected
`orgupdate/glassdoor-jobs-scraper` flagged elsewhere in this project's audit
at ~$0.20/job). No real network/Apify calls: `_build_run_input` and
`_apply_limit` are pure functions tested directly, and `fetch()` is tested
with `careeros.providers.glassdoor.run_actor` mocked out entirely.

The `to_job_dict` fixtures below are trimmed real dataset items captured
live via `careeros discover --provider glassdoor --dry-run` on 2026-07-10
(3 real, relevant Product Manager jobs, $0.0050 total). Most fields already
resolve through the shared candidate-key pattern (title via "jobTitle", url
via "jobLink"/"applyUrl", company via "employer", location, description) —
`salary` (a nested dict with a `period` field) was the one real gap."""

from __future__ import annotations

from pathlib import Path

from unittest.mock import patch

from careeros.providers.base import ProviderResult
from careeros.providers.glassdoor import ACTOR_ID, PROVIDER, _apply_limit, _build_run_input

_FAKE_CAREEROS_DIR = Path("/tmp/fake-careeros-dir")


# ── _build_run_input: pure function, no mocking needed ──────────────────

def test_build_run_input_always_sets_search_jobs_by_keyword_true():
    """CRITICAL correctness point: without this flag the actor scrapes
    reviews/interviews instead of jobs."""
    run_input = _build_run_input({}, limit=25, search="")
    assert run_input["searchJobsByKeyword"] is True


def test_build_run_input_search_jobs_by_keyword_true_regardless_of_config():
    """Even a mischievous/mistaken config value can't turn this off."""
    cfg = {"searchJobsByKeyword": False, "search_jobs_by_keyword": False}
    run_input = _build_run_input(cfg, limit=25, search="")
    assert run_input["searchJobsByKeyword"] is True


def test_build_run_input_defaults_when_nothing_configured():
    run_input = _build_run_input({}, limit=25, search="")
    assert run_input["searchKeyword"] == "Product Manager"
    assert run_input["searchLocation"] == "India"
    assert run_input["maxDaysOld"] == "7"


def test_build_run_input_uses_configured_search_keyword_and_location():
    cfg = {"search_keyword": "Growth PM", "search_location": "Bangalore"}
    run_input = _build_run_input(cfg, limit=10, search="")
    assert run_input["searchKeyword"] == "Growth PM"
    assert run_input["searchLocation"] == "Bangalore"


def test_build_run_input_search_kwarg_overrides_config():
    cfg = {"search_keyword": "Growth PM"}
    run_input = _build_run_input(cfg, limit=10, search="Data Analyst")
    assert run_input["searchKeyword"] == "Data Analyst"


def test_build_run_input_max_days_old_configured():
    run_input = _build_run_input({"max_days_old": 30}, limit=10, search="")
    assert run_input["maxDaysOld"] == "30"


def test_build_run_input_omits_remote_work_type_when_unset():
    run_input = _build_run_input({}, limit=10, search="")
    assert "remoteWorkType" not in run_input


def test_build_run_input_includes_remote_work_type_when_configured():
    run_input = _build_run_input({"remote_work_type": "Remote"}, limit=10, search="")
    assert run_input["remoteWorkType"] == "Remote"


def test_build_run_input_omits_application_type_when_unset():
    run_input = _build_run_input({}, limit=10, search="")
    assert "applicationType" not in run_input


def test_build_run_input_includes_application_type_when_configured():
    run_input = _build_run_input({"application_type": "Easy Apply only"}, limit=10, search="")
    assert run_input["applicationType"] == "Easy Apply only"


def test_build_run_input_has_no_limit_or_max_items_field():
    """No per-call result-count field was found on the verified schema —
    `limit` must not leak into run_input under any guessed key name."""
    run_input = _build_run_input({}, limit=25, search="")
    for key in ("limit", "maxItems", "maximumJobs"):
        assert key not in run_input


# ── _apply_limit: client-side slicing (no run_input limit field exists) ──

def test_apply_limit_slices_items_and_updates_records():
    result = ProviderResult(
        provider="glassdoor",
        items=[{"title": f"Job {i}"} for i in range(10)],
        cost_usd=0.05, requests=1, records=10, seconds=1.2,
    )
    trimmed = _apply_limit(result, 3)
    assert len(trimmed.items) == 3
    assert trimmed.records == 3
    # metadata other than items/records is untouched
    assert trimmed.cost_usd == 0.05
    assert trimmed.requests == 1
    assert trimmed.seconds == 1.2


def test_apply_limit_no_op_when_under_limit():
    result = ProviderResult(provider="glassdoor", items=[{"title": "Job 1"}], records=1)
    trimmed = _apply_limit(result, 100)
    assert trimmed is result


# ── to_job_dict: real live-captured Glassdoor shape (2026-07-10) ─────────

def _real_glassdoor_item(**overrides):
    item = {
        "jobTitle": "Manager, Product Development",
        "jobLink": "https://www.glassdoor.com/job-listing/j?jl=1010194248479",
        "applyUrl": "https://www.glassdoor.com/job-listing/j?jl=1010194248479",
        "employer": "Cardinal Health, Inc.",
        "location": "Indianapolis, IN",
        "description": "Lead a team of Product Development scientists.",
        "salary": {"currency": "USD", "max": 150100, "median": 127600, "min": 105100, "period": "ANNUAL"},
    }
    item.update(overrides)
    return item


def test_to_job_dict_maps_real_glassdoor_shape():
    job = PROVIDER.to_job_dict(_real_glassdoor_item())
    assert job == {
        "title": "Manager, Product Development",
        "company": "Cardinal Health, Inc.",
        "apply_url": "https://www.glassdoor.com/job-listing/j?jl=1010194248479",
        "description": "Lead a team of Product Development scientists.",
        "location": "Indianapolis, IN",
        "remote": None,
        "employment_type": None,
        "seniority": None,
        "posted_at": None,
        "salary": {"min": 105100, "max": 150100, "currency": "USD", "unit": "year"},
        "contact": None,
        "company_linkedin": None,
    }


def test_to_job_dict_salary_none_when_missing():
    raw = _real_glassdoor_item(salary=None)
    assert PROVIDER.to_job_dict(raw)["salary"] is None


def test_to_job_dict_salary_translates_monthly_period():
    raw = _real_glassdoor_item(salary={"currency": "USD", "min": 5000, "max": 8000, "period": "MONTHLY"})
    assert PROVIDER.to_job_dict(raw)["salary"]["unit"] == "month"


def test_to_job_dict_salary_none_when_min_and_max_both_falsy():
    raw = _real_glassdoor_item(salary={"currency": "USD", "min": None, "max": None, "period": "ANNUAL"})
    assert PROVIDER.to_job_dict(raw)["salary"] is None


def test_to_job_dict_posted_at_always_none_no_absolute_date_field():
    """Only a relative ageInDays exists on this actor's output — never
    computed into a fabricated absolute date."""
    raw = _real_glassdoor_item()
    raw["ageInDays"] = 1
    assert PROVIDER.to_job_dict(raw)["posted_at"] is None


def test_to_job_dict_returns_none_when_title_missing():
    raw = _real_glassdoor_item(jobTitle="")
    assert PROVIDER.to_job_dict(raw) is None


def test_to_job_dict_returns_none_when_url_missing():
    raw = _real_glassdoor_item(jobLink="", applyUrl="")
    assert PROVIDER.to_job_dict(raw) is None


def test_to_job_dict_returns_none_when_url_not_http():
    raw = _real_glassdoor_item(jobLink="glassdoor.com/job/123", applyUrl="")
    assert PROVIDER.to_job_dict(raw) is None


def test_to_job_dict_resolves_relative_partner_url_against_site_root():
    """Real regression, verified live 2026-07-11 at n=30: this actor's
    jobLink/applyUrl come back as partner-tracking paths relative to
    Glassdoor's own domain, not absolute URLs — every item in that batch
    was silently dropped before this fix."""
    raw = _real_glassdoor_item(
        jobLink=(
            "/partner/jobListing.htm?pos=105&ao=1136043&s=230"
            "&jobListingId=1010189928176"
        ),
        applyUrl=(
            "/partner/jobListing.htm?pos=105&ao=1136043&tgt=APPLY_START"
            "&jobListingId=1010189928176"
        ),
    )
    job = PROVIDER.to_job_dict(raw)
    assert job is not None
    assert job["apply_url"] == (
        "https://www.glassdoor.com/partner/jobListing.htm?pos=105&ao=1136043"
        "&s=230&jobListingId=1010189928176"
    )


def test_to_job_dict_defaults_company_to_unknown():
    raw = _real_glassdoor_item(employer="")
    job = PROVIDER.to_job_dict(raw)
    assert job["company"] == "Unknown"


# ── validate(): delegates to validate_apify_token ────────────────────────

def test_validate_empty_when_token_configured(monkeypatch):
    monkeypatch.setenv("APIFY_TOKEN", "tok")
    monkeypatch.delenv("APIFY_TOKENS", raising=False)

    class FakeConfig:
        careeros_dir = _FAKE_CAREEROS_DIR
        apify: dict = {}

    assert PROVIDER.validate(FakeConfig()) == []


def test_validate_non_empty_when_no_token_configured(monkeypatch):
    monkeypatch.delenv("APIFY_TOKEN", raising=False)
    monkeypatch.delenv("APIFY_TOKENS", raising=False)

    class FakeConfig:
        careeros_dir = _FAKE_CAREEROS_DIR
        apify: dict = {}

    errors = PROVIDER.validate(FakeConfig())
    assert len(errors) == 1
    assert "No Apify token configured" in errors[0]


# ── fetch(): run_actor mocked, no network ────────────────────────────────

def test_fetch_passes_correct_actor_id_and_run_input_to_run_actor():
    """Assert the actor id is EXACTLY the cheap, verified one — not the
    rejected ~$0.20/job `orgupdate/glassdoor-jobs-scraper`."""
    fake_result = ProviderResult(provider="glassdoor", items=[{"title": "x"}], records=1)

    class FakeConfig:
        careeros_dir = _FAKE_CAREEROS_DIR
        apify = {"tokens_env": "APIFY_TOKENS"}
        providers = {"glassdoor": {"search_keyword": "Growth PM", "search_location": "Delhi"}}

    with patch("careeros.providers.glassdoor.run_actor", return_value=fake_result) as mock_run:
        result = PROVIDER.fetch(FakeConfig(), limit=20, search="")

    assert result.items == fake_result.items
    mock_run.assert_called_once()
    args, kwargs = mock_run.call_args
    assert args[0] == "glassdoor"
    assert args[1] == {"tokens_env": "APIFY_TOKENS"}
    assert args[2] == "memo23/glassdoor-scraper-ppr"
    assert args[2] == ACTOR_ID
    assert args[2] != "orgupdate/glassdoor-jobs-scraper"
    run_input = args[3]
    assert run_input["searchJobsByKeyword"] is True
    assert run_input["searchKeyword"] == "Growth PM"
    assert run_input["searchLocation"] == "Delhi"
    assert kwargs["max_cost_usd"] is None


def test_fetch_uses_configured_actor_override():
    fake_result = ProviderResult(provider="glassdoor")

    class FakeConfig:
        careeros_dir = _FAKE_CAREEROS_DIR
        apify = {}
        providers = {"glassdoor": {"actor": "someone-else/glassdoor-fork"}}

    with patch("careeros.providers.glassdoor.run_actor", return_value=fake_result) as mock_run:
        PROVIDER.fetch(FakeConfig(), limit=10)

    args, _ = mock_run.call_args
    assert args[2] == "someone-else/glassdoor-fork"


def test_fetch_passes_max_cost_usd_from_provider_cfg():
    fake_result = ProviderResult(provider="glassdoor")

    class FakeConfig:
        careeros_dir = _FAKE_CAREEROS_DIR
        apify = {}
        providers = {"glassdoor": {"max_cost_usd": 2.5}}

    with patch("careeros.providers.glassdoor.run_actor", return_value=fake_result) as mock_run:
        PROVIDER.fetch(FakeConfig(), limit=10)

    _, kwargs = mock_run.call_args
    assert kwargs["max_cost_usd"] == 2.5


def test_fetch_handles_missing_provider_config_block():
    fake_result = ProviderResult(provider="glassdoor")

    class FakeConfig:
        careeros_dir = _FAKE_CAREEROS_DIR
        apify = {}
        providers: dict = {}

    with patch("careeros.providers.glassdoor.run_actor", return_value=fake_result) as mock_run:
        result = PROVIDER.fetch(FakeConfig(), limit=10)

    assert result is fake_result
    args, _ = mock_run.call_args
    assert args[3]["searchKeyword"] == "Product Manager"
    assert args[3]["searchLocation"] == "India"
    assert args[3]["searchJobsByKeyword"] is True


def test_fetch_applies_client_side_limit_to_returned_items():
    fake_result = ProviderResult(
        provider="glassdoor",
        items=[{"title": f"Job {i}"} for i in range(10)],
        records=10,
    )

    class FakeConfig:
        careeros_dir = _FAKE_CAREEROS_DIR
        apify = {}
        providers: dict = {}

    with patch("careeros.providers.glassdoor.run_actor", return_value=fake_result):
        result = PROVIDER.fetch(FakeConfig(), limit=4)

    assert len(result.items) == 4
    assert result.records == 4


def test_fetch_accepts_query_kwarg_as_noop():
    """query (segmented-discovery spec) is accepted but ignored — this actor
    has no documented multi-field query mapping."""
    fake_result = ProviderResult(provider="glassdoor")

    class FakeConfig:
        careeros_dir = _FAKE_CAREEROS_DIR
        apify = {}
        providers: dict = {}

    with patch("careeros.providers.glassdoor.run_actor", return_value=fake_result):
        result = PROVIDER.fetch(FakeConfig(), limit=10, query={"location_search": ["India"]})

    assert result is fake_result
