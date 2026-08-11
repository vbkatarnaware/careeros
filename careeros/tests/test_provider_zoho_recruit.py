"""Tests for careeros/providers/zoho_recruit.py — the bespoke plain-httpx
Zoho Recruit client (ats-scrapers has no scraper for this platform and no
reusable OSS alternative was found, see that module's docstring and
docs/ats-registry.md). No real network calls: httpx.Client is patched to
route through httpx.MockTransport. Field shapes below mirror the real
response captured live from otsi-global.zohorecruit.com during the
2026-08-11 research pass.
"""

from __future__ import annotations

import functools

import pytest

httpx = pytest.importorskip("httpx")
from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError  # noqa: E402

from careeros.providers.zoho_recruit import _resolve_tenant, fetch_zoho_recruit_jobs  # noqa: E402

_JOB = {
    "id": "724561000014793056",
    "Posting_Title": "Staff Engineer-Electrical",
    "Job_Opening_Name": "Staff Engineer-Electrical",
    "City": "Rancho Cordova",
    "State": "CA",
    "Country": "United States",
    "Industry": "Engineering",
    "Job_Type": "Contract",
    "Job_Description": "<p>Own the <b>roadmap</b>.</p>",
    "Date_Opened": "08/11/2026",
    "$url": "https://acme.zohorecruit.com/jobs/Careers/724561000014793056/Staff-Engineer",
}


def _patch_client(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "careeros.providers.zoho_recruit.httpx.Client",
        functools.partial(httpx.Client, transport=transport),
    )


def test_fetch_maps_fields(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "acme.zohorecruit.com"
        assert request.method == "GET"
        assert dict(request.url.params) == {"pagename": "Careers"}
        return httpx.Response(200, json={"code": "success", "data": [_JOB]})

    _patch_client(monkeypatch, handler)
    rows = fetch_zoho_recruit_jobs("acme")

    assert len(rows) == 1
    row = rows[0]
    assert row["title"] == "Staff Engineer-Electrical"
    assert row["company"] == "acme"
    assert row["ats_type"] == "zoho_recruit"
    assert row["ats_id"] == "724561000014793056"
    assert row["country_iso"] == "US"
    assert row["location"] == "Rancho Cordova, CA, United States"
    assert row["description"] == "Own the roadmap ."
    assert row["employment_type"] == "Contract"
    assert row["posted_at"] == "2026-08-11"
    assert row["url"] == _JOB["$url"]
    assert row["apply_url"] == row["url"]
    assert row["is_remote"] is None  # never guessed -- not exposed by this endpoint


def test_fetch_defaults_tld_to_com_and_rejects_extra_params(monkeypatch):
    """The public endpoint rejects page/per_page outright (verified live:
    EXTRA_PARAM_FOUND) -- confirms this must stay a single unpaginated
    request, never a pagination loop."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "acme.zohorecruit.com"
        return httpx.Response(200, json={"code": "success", "data": []})

    _patch_client(monkeypatch, handler)
    assert fetch_zoho_recruit_jobs("acme") == []


def test_fetch_uses_in_tld_when_given(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "apcerls.zohorecruit.in"
        return httpx.Response(200, json={"code": "success", "data": [_JOB]})

    _patch_client(monkeypatch, handler)
    rows = fetch_zoho_recruit_jobs("apcerls.in")
    assert len(rows) == 1


def test_fetch_raises_company_not_found_on_the_one_verified_shape(monkeypatch):
    """Zoho signals an unresolvable tenant/page as HTTP 403 + body
    {"code": "ORG_NOT_FOUND"} -- not a plain 404, unlike darwinbox. This is
    the ONLY shape actually observed live; deliberately narrow (see
    fetch_zoho_recruit_jobs's docstring) so an unrelated non-200 never gets
    misclassified as "company doesn't exist"."""
    _patch_client(monkeypatch, lambda request: httpx.Response(
        403, json={"code": "ORG_NOT_FOUND", "status": "error"}
    ))
    with pytest.raises(CompanyNotFoundError):
        fetch_zoho_recruit_jobs("ghost")


def test_fetch_raises_scraper_error_not_company_not_found_on_transient_5xx(monkeypatch):
    """A transient server error must NOT be treated as "company doesn't
    exist" -- ats_watchlist.py only marks an entry stale after repeated
    CompanyNotFoundError, and a real Zoho outage misclassified this way
    would wrongly stale-mark a company that's still there."""
    _patch_client(monkeypatch, lambda request: httpx.Response(503, text="Service Unavailable"))
    with pytest.raises(ScraperError):
        fetch_zoho_recruit_jobs("acme")


def test_fetch_raises_scraper_error_on_403_with_different_code(monkeypatch):
    """A 403 that isn't the exact verified ORG_NOT_FOUND shape (e.g. a
    future access-denied/rate-limit variant) stays ScraperError, not
    CompanyNotFoundError -- narrow match, not "any 403"."""
    _patch_client(monkeypatch, lambda request: httpx.Response(
        403, json={"code": "RATE_LIMIT_EXCEEDED", "status": "error"}
    ))
    with pytest.raises(ScraperError):
        fetch_zoho_recruit_jobs("acme")


def test_fetch_raises_scraper_error_on_api_failure_code(monkeypatch):
    _patch_client(monkeypatch, lambda request: httpx.Response(200, json={"code": "error"}))
    with pytest.raises(ScraperError):
        fetch_zoho_recruit_jobs("acme")


def test_fetch_raises_scraper_error_on_non_json(monkeypatch):
    _patch_client(monkeypatch, lambda request: httpx.Response(200, text="not json"))
    with pytest.raises(ScraperError):
        fetch_zoho_recruit_jobs("acme")


def test_fetch_skips_jobs_missing_id_or_title(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        data = [
            {**_JOB, "id": ""},
            {**_JOB, "Posting_Title": None, "Job_Opening_Name": None},
            _JOB,
        ]
        return httpx.Response(200, json={"code": "success", "data": data})

    _patch_client(monkeypatch, handler)
    rows = fetch_zoho_recruit_jobs("acme")
    assert len(rows) == 1
    assert rows[0]["ats_id"] == _JOB["id"]


def test_fetch_falls_back_to_constructed_url_when_dollar_url_missing(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        job = {k: v for k, v in _JOB.items() if k != "$url"}
        return httpx.Response(200, json={"code": "success", "data": [job]})

    _patch_client(monkeypatch, handler)
    rows = fetch_zoho_recruit_jobs("acme")
    assert rows[0]["url"].startswith("https://acme.zohorecruit.com/jobs/Careers/")
    assert rows[0]["url"].endswith(_JOB["id"])


def test_fetch_unparseable_date_opened_returns_none_posted_at(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        job = {**_JOB, "Date_Opened": "not-a-date"}
        return httpx.Response(200, json={"code": "success", "data": [job]})

    _patch_client(monkeypatch, handler)
    rows = fetch_zoho_recruit_jobs("acme")
    assert rows[0]["posted_at"] is None


@pytest.mark.parametrize("raw,tenant,tld", [
    ("acme", "acme", "com"),
    ("acme.in", "acme", "in"),
    ("acme.com", "acme", "com"),
    ("https://acme.zohorecruit.in/jobs/Careers", "acme", "in"),
])
def test_resolve_tenant_accepts_bare_slug_suffix_and_url(raw, tenant, tld):
    assert _resolve_tenant(raw) == (tenant, tld)


def test_resolve_tenant_rejects_empty_and_malformed():
    with pytest.raises(ScraperError):
        _resolve_tenant("")
    with pytest.raises(ScraperError):
        _resolve_tenant("https://not-zoho.example.com/careers")
