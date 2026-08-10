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
from careeros.providers.legacy.fantastic_jobs import _fetch_one_endpoint


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


# ── 403: exhausted meter vs genuinely bad key (2026-08-05) ───────────────
# Fantastic Jobs returns 403 for BOTH, distinguished only by the RFC-7807
# `detail` field. Treating every 403 as "rotate your key" sent this project
# chasing a key rotation twice in one session while the key was fine. The two
# need opposite actions, so they must produce different messages.

# Verbatim body from the real 2026-08-05 failure.
_REAL_METER_BODY = {
    "type": "https://httpproblems.com/http-status/403",
    "title": "Forbidden",
    "status": 403,
    "detail": 'API Key has exceeded the allowed limit for "api_requests" meter.',
    "instance": "/v1/active-ats",
}


def test_403_with_exhausted_meter_is_not_reported_as_an_invalid_key():
    """The regression that matters: this must NOT tell the user to rotate."""
    with patch("requests.get", return_value=_resp(403, json_body=_REAL_METER_BODY)):
        with pytest.raises(ProviderError) as exc:
            _fetch()
    assert "usage quota exhausted" in str(exc.value)
    assert "API key rejected" not in str(exc.value)


def test_403_meter_message_quotes_the_providers_own_detail():
    """Never paraphrase the provider's diagnosis — the meter NAME is the
    single most useful token for the user (records vs api_requests)."""
    with patch("requests.get", return_value=_resp(403, json_body=_REAL_METER_BODY)):
        with pytest.raises(ProviderError, match="api_requests"):
            _fetch()


def test_403_meter_message_says_a_fresh_key_may_not_help():
    with patch("requests.get", return_value=_resp(403, json_body=_REAL_METER_BODY)):
        with pytest.raises(ProviderError, match="NOT invalid"):
            _fetch()


def test_403_with_genuine_auth_detail_still_reports_a_bad_key():
    body = {"status": 403, "detail": "API Key is invalid or has been revoked."}
    with patch("requests.get", return_value=_resp(403, json_body=body)):
        with pytest.raises(ProviderError) as exc:
            _fetch()
    assert "API key rejected" in str(exc.value)
    assert "usage quota exhausted" not in str(exc.value)


def test_403_with_genuine_auth_detail_still_surfaces_that_detail():
    body = {"status": 403, "detail": "API Key is invalid or has been revoked."}
    with patch("requests.get", return_value=_resp(403, json_body=body)):
        with pytest.raises(ProviderError, match="revoked"):
            _fetch()


def test_403_with_unparseable_body_falls_back_to_the_bad_key_message():
    """An error path must never raise a second error while explaining the
    first — a non-JSON body just loses the detail, it doesn't blow up."""
    resp = _resp(403, text="<html>502 Bad Gateway</html>")
    resp.json.side_effect = ValueError("not json")
    with patch("requests.get", return_value=resp):
        with pytest.raises(ProviderError, match="API key rejected"):
            _fetch()


def test_403_with_json_list_body_falls_back_to_the_bad_key_message():
    with patch("requests.get", return_value=_resp(403, json_body=[])):
        with pytest.raises(ProviderError, match="API key rejected"):
            _fetch()


def test_401_with_exhausted_meter_is_also_classified_as_quota():
    """Same classification logic on 401, so behaviour can't diverge by code."""
    body = {"status": 401, "detail": 'exceeded the allowed limit for "records" meter.'}
    with patch("requests.get", return_value=_resp(401, json_body=body)):
        with pytest.raises(ProviderError, match="usage quota exhausted"):
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


# ── title_advanced expression builder (v2.2) ─────────────────────────────
# Replaces the bare `title` param's live-verified 3-OR-term ceiling, past
# which its `-exclusion` clause is silently dropped. The boolean grammar has
# no such limit, so all roles + exclusions fit one query.

from careeros.providers.legacy.fantastic_jobs import _advanced_term, build_title_advanced


def test_single_word_term_is_left_bare():
    assert _advanced_term("robotics") == "robotics"


def test_multi_word_term_is_single_quoted():
    assert _advanced_term("Product Manager") == "'Product Manager'"


def test_apostrophe_term_avoids_quoting_entirely():
    """`'Founder's Office'` would terminate the quoted string at the
    apostrophe. Docs give no escaping rule, so use the documented adjacency
    (`<->`) and prefix (`:*`) operators instead of guessing at escaping."""
    out = _advanced_term("Founder's Office")
    assert "'" not in out
    assert out == "Founder:* <-> Office"


def test_curly_apostrophe_is_handled_the_same_way():
    """Real postings use U+2019 as often as ASCII "'"."""
    assert _advanced_term("Founder’s Office") == "Founder:* <-> Office"


def test_builds_or_group_with_negated_group():
    expr = build_title_advanced(["Product Manager", "robotics"], ["intern", "marketing"])
    assert expr == "('Product Manager' | robotics) & !(intern | marketing)"


def test_single_include_is_not_parenthesised():
    assert build_title_advanced(["robotics"], []) == "robotics"


def test_single_exclusion_is_not_parenthesised():
    assert build_title_advanced(["robotics"], ["intern"]) == "robotics & !intern"


def test_all_six_real_roles_fit_one_expression():
    """The actual profile — the case that broke the bare `title` param."""
    roles = [
        "Product Manager", "AI Product Manager", "Founder's Office",
        "Growth Product Manager", "Product Operations", "Associate Product Manager",
    ]
    expr = build_title_advanced(roles, ["Intern", "Marketing", "Assistant"])
    assert expr.count(" | ") == 5 + 2, "6 includes + 3 excludes -> 5 + 2 separators"
    assert expr.startswith("(") and " & !(" in expr
    assert "Founder:* <-> Office" in expr


def test_empty_inputs_return_none_rather_than_a_broken_expression():
    assert build_title_advanced([], []) is None
    assert build_title_advanced(["", "  "], []) is None


def test_exclusions_alone_still_produce_a_valid_expression():
    assert build_title_advanced([], ["intern"]) == "!intern"
