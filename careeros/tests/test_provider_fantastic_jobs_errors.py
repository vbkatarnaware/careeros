"""Tests for _fetch_one_endpoint's typed error classification (P2.9) — every
Fantastic Jobs failure mode maps to a distinct, actionable ProviderError
message instead of a generic "request failed"/"HTTP 500". No real network
calls: `requests.get` is mocked via unittest.mock, matching this repo's
existing pattern (see test_drive.py)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from careeros.providers.base import ProviderError
from careeros.providers.fantastic_jobs import _fetch_one_endpoint


def _resp(status_code: int, headers: dict | None = None, json_body=None, text: str = "") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = headers or {}
    resp.text = text
    resp.json.return_value = json_body if json_body is not None else []
    return resp


def _fetch():
    return _fetch_one_endpoint("https://data.fantastic.jobs", {}, "active-ats", {})


def test_200_with_list_body_returns_items():
    with patch("requests.get", return_value=_resp(200, json_body=[{"id": "1"}])):
        items, live_quota = _fetch()
        assert items == [{"id": "1"}]
        assert live_quota is None  # no x-ratelimit-* headers on this mock response


def test_200_surfaces_live_ratelimit_headers():
    """The whole point of the fix: live quota headers must be read on the
    SUCCESS path too, not just 429 — this is the real, provider-verified
    remaining quota `doctor`/the discovery summary should show, never a
    locally calculated guess (see AGENT_GUIDE.md)."""
    resp = _resp(200, headers={
        "x-ratelimit-requests-remaining": "42",
        "x-ratelimit-jobs-remaining": "1000",
    }, json_body=[{"id": "1"}])
    with patch("requests.get", return_value=resp):
        items, live_quota = _fetch()
        assert items == [{"id": "1"}]
        assert live_quota == {"requests_remaining": "42", "jobs_remaining": "1000"}


def test_401_is_classified_as_invalid_api_key():
    with patch("requests.get", return_value=_resp(401)):
        with pytest.raises(ProviderError, match="API key rejected"):
            _fetch()


def test_403_is_classified_as_invalid_api_key():
    with patch("requests.get", return_value=_resp(403)):
        with pytest.raises(ProviderError, match="API key rejected"):
            _fetch()


def test_429_with_requests_remaining_zero_is_request_quota_exhausted():
    resp = _resp(429, headers={"x-ratelimit-requests-remaining": "0"})
    with patch("requests.get", return_value=resp):
        with pytest.raises(ProviderError, match="request quota exhausted"):
            _fetch()


def test_429_with_jobs_remaining_zero_is_job_quota_exhausted():
    resp = _resp(429, headers={"x-ratelimit-jobs-remaining": "0", "x-ratelimit-requests-remaining": "10"})
    with patch("requests.get", return_value=resp):
        with pytest.raises(ProviderError, match="job/record quota exhausted"):
            _fetch()


def test_429_without_zero_remaining_headers_is_transient_rate_limit():
    resp = _resp(429, headers={"x-ratelimit-requests-remaining": "10", "x-ratelimit-jobs-remaining": "500"})
    with patch("requests.get", return_value=resp):
        with pytest.raises(ProviderError, match="rate limited"):
            _fetch()


def test_429_with_no_headers_is_transient_rate_limit():
    with patch("requests.get", return_value=_resp(429)):
        with pytest.raises(ProviderError, match="rate limited"):
            _fetch()


@pytest.mark.parametrize("code", [500, 502, 503])
def test_5xx_is_classified_as_service_outage(code):
    with patch("requests.get", return_value=_resp(code)):
        with pytest.raises(ProviderError, match="service outage"):
            _fetch()


def test_timeout_is_classified_as_network_outage():
    with patch("requests.get", side_effect=requests.Timeout("timed out")):
        with pytest.raises(ProviderError, match="network or service outage"):
            _fetch()


def test_connection_error_is_classified_as_network_outage():
    with patch("requests.get", side_effect=requests.ConnectionError("no route")):
        with pytest.raises(ProviderError, match="network or service outage"):
            _fetch()


def test_other_request_exception_falls_back_to_generic_message():
    with patch("requests.get", side_effect=requests.RequestException("weird")):
        with pytest.raises(ProviderError, match="request failed"):
            _fetch()


def test_unexpected_status_falls_back_to_raw_http_code():
    with patch("requests.get", return_value=_resp(418, text="I'm a teapot")):
        with pytest.raises(ProviderError, match="HTTP 418"):
            _fetch()


def test_non_list_response_shape_raises():
    with patch("requests.get", return_value=_resp(200, json_body={"not": "a list"})):
        with pytest.raises(ProviderError, match="unexpected response shape"):
            _fetch()
