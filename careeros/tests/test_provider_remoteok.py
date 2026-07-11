"""Tests for careeros/providers/remoteok.py — RemoteOK's free public API
provider. `to_job_dict` field-mapping assertions use a trimmed but realistic
fixture of the REAL verified response shape (see the provider's module
docstring). `fetch()` mocks `requests.get` via unittest.mock — no real
network calls, matching this repo's existing pattern (see
test_provider_fantastic_jobs_errors.py)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from careeros.providers.base import ProviderError
from careeros.providers.remoteok import PROVIDER, RemoteOKProvider

# The real response shape: item[0] is a legal-notice blob (no "id" key).
_LEGAL_NOTICE = {
    "legal": "https://remoteok.com/legal",
    "last_updated": "2026-07-09T00:00:00+00:00",
}

_REAL_JOB = {
    "id": "1234567",
    "slug": "senior-backend-engineer-acme-1234567",
    "epoch": 1783785855,
    "date": "2026-07-09T15:04:15+00:00",
    "company": "Acme Corp",
    "company_logo": "https://remoteok.com/logo.png",
    "position": "Senior Backend Engineer",
    "tags": ["python", "backend", "remote"],
    "description": "<p>Build cool stuff.</p>",
    "location": "Kalgoorlie, ",
    "apply_url": "https://remoteOK.com/remote-jobs/senior-backend-engineer-acme-1234567",
    "url": "https://remoteOK.com/remote-jobs/senior-backend-engineer-acme-1234567",
    "salary_min": 90000,
    "salary_max": 130000,
    "logo": "https://remoteok.com/logo.png",
}


def _resp(json_body, status_code: int = 200, text: str = "") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.json.return_value = json_body
    return resp


# ── to_job_dict: field mapping ───────────────────────────────────────────

def test_to_job_dict_maps_realistic_fixture():
    job = PROVIDER.to_job_dict(_REAL_JOB)
    assert job == {
        "title": "Senior Backend Engineer",
        "company": "Acme Corp",
        "apply_url": "https://remoteOK.com/remote-jobs/senior-backend-engineer-acme-1234567",
        "description": "<p>Build cool stuff.</p>",
        "location": "Kalgoorlie,",
        "remote": True,
        "employment_type": None,
        "seniority": None,
        "posted_at": "2026-07-09T15:04:15+00:00",
        "salary": {"min": 90000, "max": 130000, "currency": "USD", "unit": "year"},
        "contact": None,
        "company_linkedin": None,
    }


def test_to_job_dict_none_when_title_missing():
    raw = dict(_REAL_JOB)
    del raw["position"]
    assert PROVIDER.to_job_dict(raw) is None


def test_to_job_dict_none_when_apply_url_missing():
    raw = dict(_REAL_JOB)
    del raw["apply_url"]
    del raw["url"]
    assert PROVIDER.to_job_dict(raw) is None


def test_to_job_dict_none_when_url_not_http():
    raw = dict(_REAL_JOB)
    raw["apply_url"] = "not-a-url"
    raw["url"] = "not-a-url"
    assert PROVIDER.to_job_dict(raw) is None


def test_to_job_dict_defaults_location_to_remote_when_empty():
    raw = dict(_REAL_JOB)
    raw["location"] = ""
    assert PROVIDER.to_job_dict(raw)["location"] == "Remote"


def test_to_job_dict_defaults_location_to_remote_when_whitespace():
    raw = dict(_REAL_JOB)
    raw["location"] = "   "
    assert PROVIDER.to_job_dict(raw)["location"] == "Remote"


def test_to_job_dict_defaults_location_to_remote_when_missing_key():
    raw = dict(_REAL_JOB)
    del raw["location"]
    assert PROVIDER.to_job_dict(raw)["location"] == "Remote"


def test_to_job_dict_salary_none_when_both_zero():
    raw = dict(_REAL_JOB)
    raw["salary_min"] = 0
    raw["salary_max"] = 0
    assert PROVIDER.to_job_dict(raw)["salary"] is None


def test_to_job_dict_salary_none_when_both_missing():
    raw = dict(_REAL_JOB)
    del raw["salary_min"]
    del raw["salary_max"]
    assert PROVIDER.to_job_dict(raw)["salary"] is None


def test_to_job_dict_salary_dict_when_nonzero():
    raw = dict(_REAL_JOB)
    raw["salary_min"] = 80000
    raw["salary_max"] = 120000
    salary = PROVIDER.to_job_dict(raw)["salary"]
    assert salary == {"min": 80000, "max": 120000, "currency": "USD", "unit": "year"}


def test_to_job_dict_remote_is_always_true():
    assert PROVIDER.to_job_dict(_REAL_JOB)["remote"] is True


def test_to_job_dict_company_falls_back_to_unknown():
    raw = dict(_REAL_JOB)
    del raw["company"]
    assert PROVIDER.to_job_dict(raw)["company"] == "Unknown"


# ── fetch(): mocked HTTP, no real network calls ──────────────────────────

def test_fetch_skips_legal_notice_item_and_returns_result():
    body = [_LEGAL_NOTICE, _REAL_JOB, dict(_REAL_JOB, id="7654321")]
    with patch("requests.get", return_value=_resp(body)):
        result = RemoteOKProvider().fetch(config=None)
    assert result.provider == "remoteok"
    assert result.items == [_REAL_JOB, dict(_REAL_JOB, id="7654321")]
    assert result.records == 2
    assert result.cost_usd == 0.0
    assert result.requests == 1


def test_fetch_respects_limit_as_client_side_slice():
    body = [_LEGAL_NOTICE] + [dict(_REAL_JOB, id=str(i)) for i in range(10)]
    with patch("requests.get", return_value=_resp(body)):
        result = RemoteOKProvider().fetch(config=None, limit=3)
    assert len(result.items) == 3
    assert result.records == 3


def test_fetch_sends_user_agent_header():
    body = [_LEGAL_NOTICE, _REAL_JOB]
    with patch("requests.get", return_value=_resp(body)) as mock_get:
        RemoteOKProvider().fetch(config=None)
    _, kwargs = mock_get.call_args
    assert "User-Agent" in kwargs["headers"]


def test_fetch_ignores_search_and_query_kwargs_without_crashing():
    body = [_LEGAL_NOTICE, _REAL_JOB]
    with patch("requests.get", return_value=_resp(body)):
        result = RemoteOKProvider().fetch(
            config=None, search="engineer", query={"location_search": ["India"]},
        )
    assert result.records == 1


def test_fetch_raises_provider_error_on_non_200():
    with patch("requests.get", return_value=_resp(None, status_code=500, text="oops")):
        with pytest.raises(ProviderError, match="HTTP 500"):
            RemoteOKProvider().fetch(config=None)


def test_fetch_raises_provider_error_on_timeout():
    with patch("requests.get", side_effect=requests.Timeout("timed out")):
        with pytest.raises(ProviderError, match="network or service outage"):
            RemoteOKProvider().fetch(config=None)


def test_fetch_raises_provider_error_on_connection_error():
    with patch("requests.get", side_effect=requests.ConnectionError("no route")):
        with pytest.raises(ProviderError, match="network or service outage"):
            RemoteOKProvider().fetch(config=None)


def test_fetch_raises_provider_error_on_non_list_response():
    with patch("requests.get", return_value=_resp({"not": "a list"})):
        with pytest.raises(ProviderError, match="unexpected response shape"):
            RemoteOKProvider().fetch(config=None)


# ── validate(): no credentials needed ─────────────────────────────────────

def test_validate_returns_empty_list():
    assert PROVIDER.validate(config=None) == []
