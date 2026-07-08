"""Tests for careeros/providers/fantastic_jobs.py's pure functions: REST
param construction and transport/auth resolution (P2.7's default provider).
No network calls — `requests` itself is never touched here, only the
config -> query-param mapping, and the transport/header selection.
See test_provider_fantastic_jobs_parity.py for `to_job_dict` parity against
the legacy actor provider."""

from __future__ import annotations

import pytest

from careeros.providers.base import ProviderError
from careeros.providers.fantastic_jobs import (
    _base_url_and_headers, _build_params, _merge_query, _or_exclude_param,
)


# ── _or_exclude_param: shared title/location OR + "-exclusion" builder ──────

def test_or_exclude_param_none_when_both_empty():
    assert _or_exclude_param([], []) is None


def test_or_exclude_param_joins_terms_with_or():
    assert _or_exclude_param(["Product Manager", "Growth PM"], []) == "Product Manager OR Growth PM"


def test_or_exclude_param_appends_exclusions():
    assert _or_exclude_param(["Product Manager"], ["Intern"]) == "Product Manager -Intern"


def test_or_exclude_param_exclusions_only():
    assert _or_exclude_param([], ["Intern", "Contract"]) == "-Intern -Contract"


# ── _merge_query: segmented-discovery spec overlay (mirrors the actor's) ────

def test_merge_query_returns_base_config_unchanged_when_no_query():
    api_cfg = {"title_search": ["Product Manager"]}
    assert _merge_query(api_cfg, None) == api_cfg


def test_merge_query_overrides_matching_keys():
    api_cfg = {"title_search": ["Product Manager"], "location_search": ["India"]}
    query = {"location_search": [], "work_arrangement": ["Remote OK"]}
    merged = _merge_query(api_cfg, query)
    assert merged["title_search"] == ["Product Manager"]
    assert merged["location_search"] == []
    assert merged["work_arrangement"] == ["Remote OK"]


def test_merge_query_drops_underscore_prefixed_debug_keys():
    api_cfg = {"title_search": ["Product Manager"]}
    query = {"location_search": ["India"], "_work_mode": "india_remote"}
    merged = _merge_query(api_cfg, query)
    assert "_work_mode" not in merged


def test_merge_query_does_not_mutate_base_config():
    api_cfg = {"location_search": ["India"]}
    _merge_query(api_cfg, {"location_search": []})
    assert api_cfg["location_search"] == ["India"]


# ── _build_params: config -> REST query params ───────────────────────────

def test_build_params_always_includes_limit_and_time_frame():
    params = _build_params({}, limit=25, search="")
    assert params["limit"] == 25
    assert params["time_frame"] == "7d"


def test_build_params_no_actor_style_minimum_limit():
    """The official API has no documented minimum limit (unlike the actor's
    MIN_LIMIT=10 quirk) — only clamped to at least 1."""
    params = _build_params({}, limit=1, search="")
    assert params["limit"] == 1


def test_build_params_search_overrides_config_title_search():
    cfg = {"title_search": ["Product Manager"]}
    params = _build_params(cfg, limit=10, search="Growth PM")
    assert params["title"] == "Growth PM"


def test_build_params_wires_work_arrangement_filter():
    cfg = {"work_arrangement": ["Remote OK", "Remote Solely"]}
    params = _build_params(cfg, limit=10, search="")
    assert params["ai_work_arrangement"] == ["Remote OK", "Remote Solely"]


def test_build_params_omits_work_arrangement_when_empty():
    params = _build_params({"work_arrangement": []}, limit=10, search="")
    assert "ai_work_arrangement" not in params


def test_build_params_remove_agency_defaults_true_maps_to_exclude():
    params = _build_params({}, limit=10, search="")
    assert params["organization_agency"] == "exclude"


def test_build_params_remove_agency_false_omits_param():
    params = _build_params({"remove_agency": False}, limit=10, search="")
    assert "organization_agency" not in params


def test_build_params_has_salary_omitted_when_none():
    params = _build_params({"has_salary": None}, limit=10, search="")
    assert "has_salary" not in params


def test_build_params_has_salary_included_when_set():
    params = _build_params({"has_salary": True}, limit=10, search="")
    assert params["has_salary"] is True


def test_build_params_wires_exclusion_searches_into_title_and_location():
    cfg = {
        "title_search": ["Product Manager"],
        "title_exclusion_search": ["Intern"],
        "location_search": ["India"],
        "location_exclusion_search": ["United States"],
    }
    params = _build_params(cfg, limit=10, search="")
    assert params["title"] == "Product Manager -Intern"
    assert params["location"] == "India -United States"


# ── _base_url_and_headers: transport selection (config, not hardcoded) ──────

def test_base_url_and_headers_requires_transport_to_be_set():
    with pytest.raises(ProviderError, match="transport is not set"):
        _base_url_and_headers({})


def test_base_url_and_headers_direct_requires_api_key(monkeypatch):
    monkeypatch.delenv("FANTASTIC_API_KEY", raising=False)
    with pytest.raises(ProviderError, match="No direct Fantastic.jobs API key"):
        _base_url_and_headers({"transport": "direct"})


def test_base_url_and_headers_direct_uses_bearer_auth(monkeypatch):
    monkeypatch.setenv("FANTASTIC_API_KEY", "secret123")
    base_url, headers = _base_url_and_headers({"transport": "direct"})
    assert base_url == "https://data.fantastic.jobs"
    assert headers == {"Authorization": "Bearer secret123"}


def test_base_url_and_headers_rapidapi_requires_key(monkeypatch):
    monkeypatch.delenv("RAPIDAPI_KEY", raising=False)
    with pytest.raises(ProviderError, match="No RapidAPI key"):
        _base_url_and_headers({"transport": "rapidapi"})


def test_base_url_and_headers_rapidapi_uses_rapidapi_headers(monkeypatch):
    monkeypatch.setenv("RAPIDAPI_KEY", "rapid123")
    base_url, headers = _base_url_and_headers({"transport": "rapidapi"})
    assert base_url == "https://active-jobs-db.p.rapidapi.com"
    assert headers == {"X-RapidAPI-Key": "rapid123", "X-RapidAPI-Host": "active-jobs-db.p.rapidapi.com"}


def test_base_url_and_headers_rapidapi_respects_custom_host(monkeypatch):
    monkeypatch.setenv("RAPIDAPI_KEY", "rapid123")
    base_url, headers = _base_url_and_headers(
        {"transport": "rapidapi", "rapidapi_host": "custom.p.rapidapi.com"}
    )
    assert base_url == "https://custom.p.rapidapi.com"
    assert headers["X-RapidAPI-Host"] == "custom.p.rapidapi.com"
