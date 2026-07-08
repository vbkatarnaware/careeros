"""Tests for careeros/providers/legacy/fantastic_jobs_actor.py's pure
functions: run-input construction (source-side filtering) and token-pool
parsing (the rotation mechanism). No network calls — ApifyClient itself is
never touched here, only the config -> run_input mapping and env-var
parsing. P2.7: this provider is now registered as `fantastic-jobs-actor`
(legacy/reference), superseded as the default by the REST provider — see
test_provider_fantastic_jobs.py and test_provider_fantastic_jobs_parity.py."""

from __future__ import annotations

from careeros.providers.legacy.fantastic_jobs_actor import (
    _build_run_input, _company_linkedin_from_slug, _contact_from_ai_fields, _extract_usage_usd,
    _iter_tokens, _merge_query, _remote_from_arrangement, _salary_from_ai_fields,
)


# ── _merge_query: segmented-discovery spec overlay ──────────────────────

def test_merge_query_returns_base_config_unchanged_when_no_query():
    apify_cfg = {"title_search": ["Product Manager"]}
    assert _merge_query(apify_cfg, None) == apify_cfg


def test_merge_query_overrides_matching_keys():
    apify_cfg = {"title_search": ["Product Manager"], "location_search": ["India"]}
    query = {"location_search": [], "work_arrangement": ["Remote OK"]}
    merged = _merge_query(apify_cfg, query)
    assert merged["title_search"] == ["Product Manager"]  # untouched
    assert merged["location_search"] == []                # overridden
    assert merged["work_arrangement"] == ["Remote OK"]     # added


def test_merge_query_drops_underscore_prefixed_debug_keys():
    apify_cfg = {"title_search": ["Product Manager"]}
    query = {"location_search": ["India"], "_work_mode": "india_remote"}
    merged = _merge_query(apify_cfg, query)
    assert "_work_mode" not in merged
    assert merged["location_search"] == ["India"]


def test_merge_query_does_not_mutate_base_config():
    apify_cfg = {"location_search": ["India"]}
    _merge_query(apify_cfg, {"location_search": []})
    assert apify_cfg["location_search"] == ["India"]


# ── _build_run_input: source-side filtering ─────────────────────────────

def test_build_run_input_always_includes_limit_and_time_range():
    run_input = _build_run_input({}, limit=25, search="")
    assert run_input["limit"] == 25
    assert run_input["timeRange"] == "7d"


def test_build_run_input_enforces_actor_min_limit():
    run_input = _build_run_input({}, limit=1, search="")
    assert run_input["limit"] == 10  # MIN_LIMIT


def test_build_run_input_search_overrides_config_title_search():
    cfg = {"title_search": ["Product Manager"]}
    run_input = _build_run_input(cfg, limit=10, search="Growth PM")
    assert run_input["titleSearch"] == ["Growth PM"]


def test_build_run_input_wires_work_arrangement_filter():
    cfg = {"work_arrangement": ["Remote OK", "Remote Solely"]}
    run_input = _build_run_input(cfg, limit=10, search="")
    assert run_input["aiWorkArrangementFilter"] == ["Remote OK", "Remote Solely"]


def test_build_run_input_omits_work_arrangement_when_empty():
    run_input = _build_run_input({"work_arrangement": []}, limit=10, search="")
    assert "aiWorkArrangementFilter" not in run_input


def test_build_run_input_remove_agency_defaults_true():
    run_input = _build_run_input({}, limit=10, search="")
    assert run_input["removeAgency"] is True


def test_build_run_input_remove_agency_respects_config_false():
    run_input = _build_run_input({"remove_agency": False}, limit=10, search="")
    assert run_input["removeAgency"] is False


def test_build_run_input_has_salary_omitted_when_none():
    run_input = _build_run_input({"has_salary": None}, limit=10, search="")
    assert "hasSalary" not in run_input


def test_build_run_input_has_salary_included_when_set():
    run_input = _build_run_input({"has_salary": True}, limit=10, search="")
    assert run_input["hasSalary"] is True


def test_build_run_input_wires_exclusion_searches():
    cfg = {
        "title_exclusion_search": ["Intern"],
        "location_exclusion_search": ["United States"],
    }
    run_input = _build_run_input(cfg, limit=10, search="")
    assert run_input["titleExclusionSearch"] == ["Intern"]
    assert run_input["locationExclusionSearch"] == ["United States"]


# ── _iter_tokens: rotation pool parsing ──────────────────────────────────

def test_iter_tokens_prefers_comma_separated_pool(monkeypatch):
    monkeypatch.setenv("APIFY_TOKENS", "tok1, tok2 ,tok3")
    monkeypatch.setenv("APIFY_TOKEN", "single-tok")
    tokens = _iter_tokens({"tokens_env": "APIFY_TOKENS", "token_env": "APIFY_TOKEN"})
    assert tokens == ["tok1", "tok2", "tok3"]


def test_iter_tokens_falls_back_to_single_token(monkeypatch):
    monkeypatch.delenv("APIFY_TOKENS", raising=False)
    monkeypatch.setenv("APIFY_TOKEN", "single-tok")
    tokens = _iter_tokens({"tokens_env": "APIFY_TOKENS", "token_env": "APIFY_TOKEN"})
    assert tokens == ["single-tok"]


def test_iter_tokens_empty_when_nothing_configured(monkeypatch):
    monkeypatch.delenv("APIFY_TOKENS", raising=False)
    monkeypatch.delenv("APIFY_TOKEN", raising=False)
    tokens = _iter_tokens({"tokens_env": "APIFY_TOKENS", "token_env": "APIFY_TOKEN"})
    assert tokens == []


# ── field mappers ─────────────────────────────────────────────────────────

def test_remote_from_arrangement_remote_ok_is_remote():
    assert _remote_from_arrangement("Remote OK") is True


def test_remote_from_arrangement_hybrid_is_not_remote():
    assert _remote_from_arrangement("Hybrid") is False


def test_remote_from_arrangement_onsite_is_not_remote():
    assert _remote_from_arrangement("On-site") is False


def test_remote_from_arrangement_unknown_is_none():
    assert _remote_from_arrangement(None) is None
    assert _remote_from_arrangement("") is None


def test_salary_from_ai_fields_none_when_all_missing():
    assert _salary_from_ai_fields({}) is None


def test_salary_from_ai_fields_maps_min_max_currency_unit():
    raw = {
        "ai_salary_min_value": 1_500_000, "ai_salary_max_value": 2_000_000,
        "ai_salary_currency": "INR", "ai_salary_unit_text": "Annually",
    }
    salary = _salary_from_ai_fields(raw)
    assert salary == {"min": 1_500_000, "max": 2_000_000, "currency": "INR", "unit": "year"}


def test_salary_from_ai_fields_falls_back_to_point_value():
    raw = {"ai_salary_value": 1_800_000, "ai_salary_currency": "INR"}
    salary = _salary_from_ai_fields(raw)
    assert salary["min"] == 1_800_000
    assert salary["max"] == 1_800_000


def test_contact_from_ai_fields_none_when_both_missing():
    assert _contact_from_ai_fields({}) is None


def test_contact_from_ai_fields_maps_name_and_email():
    raw = {"ai_hiring_manager_name": "Jane Doe", "ai_hiring_manager_email_address": "jane@acme.com"}
    contact = _contact_from_ai_fields(raw)
    assert contact == {"name": "Jane Doe", "linkedin": None, "email": "jane@acme.com"}


def test_company_linkedin_none_when_slug_missing():
    assert _company_linkedin_from_slug({}) is None


def test_company_linkedin_builds_url_from_bare_slug():
    """org_linkedin_slug is a bare slug like 'nearmap-com', not a URL —
    verified live during the P2.6 benchmark (present on ~100% of postings)."""
    assert (_company_linkedin_from_slug({"org_linkedin_slug": "nearmap-com"})
            == "https://www.linkedin.com/company/nearmap-com")


def test_company_linkedin_none_for_blank_slug():
    assert _company_linkedin_from_slug({"org_linkedin_slug": "   "}) is None


# ── _extract_usage_usd: real per-run $ cost (P2.6) ───────────────────────

def test_extract_usage_usd_from_dict_run():
    assert _extract_usage_usd({"usageTotalUsd": 0.142}) == 0.142


def test_extract_usage_usd_from_object_run():
    class FakeRun:
        usage_total_usd = 0.01
    assert _extract_usage_usd(FakeRun()) == 0.01


def test_extract_usage_usd_defaults_to_zero_when_missing():
    assert _extract_usage_usd({}) == 0.0
