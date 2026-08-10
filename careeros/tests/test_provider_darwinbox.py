"""Tests for careeros/providers/darwinbox.py — the bespoke plain-httpx
Darwinbox client (ats-scrapers has no released scraper for this platform,
see that module's docstring and docs/ats-registry.md). No real network
calls: httpx.Client is patched to route through httpx.MockTransport."""

from __future__ import annotations

import functools
import json

import pytest

httpx = pytest.importorskip("httpx")
from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError  # noqa: E402

from careeros.providers.darwinbox import _resolve_tenant, fetch_darwinbox_jobs  # noqa: E402

_JOB = {
    "id": "123",
    "designation_display_name": "Associate Product Manager",
    "country": "India",
    "is_remote": False,
    "tool_tip_locations": ["Bengaluru, Karnataka, India"],
    "department_name": "Product",
    "jd": "<p>Own the <b>roadmap</b>.</p>",
    "posted_on": 1752690600,
}


def _patch_client(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "careeros.providers.darwinbox.httpx.Client",
        functools.partial(httpx.Client, transport=transport),
    )


def test_fetch_maps_fields_and_stops_on_short_page(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "acme.darwinbox.in"
        assert request.method == "POST"
        body = json.loads(request.content)
        assert body == {"companyId": "main", "page": 1, "sort_option": "new", "limit": 10}
        return httpx.Response(200, json={"status": "success", "data": [_JOB], "job_counts": 1})

    _patch_client(monkeypatch, handler)
    rows = fetch_darwinbox_jobs("acme")

    assert len(rows) == 1
    row = rows[0]
    assert row["title"] == "Associate Product Manager"
    assert row["company"] == "acme"
    assert row["ats_type"] == "darwinbox"
    assert row["ats_id"] == "123"
    assert row["country_iso"] == "IN"
    assert row["is_remote"] is False
    assert row["location"] == "Bengaluru, Karnataka, India"
    assert row["description"] == "Own the roadmap ."
    assert row["url"].endswith("/ms/candidate/careers/123")
    assert row["apply_url"] == row["url"]
    assert row["posted_at"] is not None


def test_fetch_paginates_until_job_counts_reached(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        page = body["page"]
        calls.append(page)
        # Full-size pages (10 jobs) so the "short page" stop condition
        # doesn't trigger before job_counts does — a real Darwinbox
        # tenant always returns exactly PAGE_SIZE until the final page.
        page_jobs = [{**_JOB, "id": f"{page}-{i}"} for i in range(10)]
        return httpx.Response(200, json={"status": "success", "data": page_jobs, "job_counts": 15})

    _patch_client(monkeypatch, handler)
    rows = fetch_darwinbox_jobs("acme")

    assert calls == [1, 2]
    assert len(rows) == 20  # stops once len(jobs) >= job_counts, doesn't trim to exactly 15


def test_fetch_raises_company_not_found_on_404(monkeypatch):
    _patch_client(monkeypatch, lambda request: httpx.Response(404))
    with pytest.raises(CompanyNotFoundError):
        fetch_darwinbox_jobs("ghost")


def test_fetch_raises_scraper_error_on_api_failure_status(monkeypatch):
    _patch_client(monkeypatch, lambda request: httpx.Response(200, json={"status": "error"}))
    with pytest.raises(ScraperError):
        fetch_darwinbox_jobs("acme")


def test_fetch_raises_scraper_error_on_non_json(monkeypatch):
    _patch_client(monkeypatch, lambda request: httpx.Response(200, text="not json"))
    with pytest.raises(ScraperError):
        fetch_darwinbox_jobs("acme")


def test_fetch_skips_jobs_missing_id_or_title(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        data = [{**_JOB, "id": ""}, {**_JOB, "designation_display_name": None, "title": None}, _JOB]
        return httpx.Response(200, json={"status": "success", "data": data, "job_counts": 1})

    _patch_client(monkeypatch, handler)
    rows = fetch_darwinbox_jobs("acme")
    assert len(rows) == 1
    assert rows[0]["ats_id"] == "123"


@pytest.mark.parametrize("raw,tenant,tld", [
    ("acme", "acme", "in"),
    ("acme.in", "acme", "in"),
    ("acme.com", "acme", "com"),
    ("https://acme.darwinbox.com/ms/candidate/careers", "acme", "com"),
])
def test_resolve_tenant_accepts_bare_slug_suffix_and_url(raw, tenant, tld):
    assert _resolve_tenant(raw) == (tenant, tld)


def test_resolve_tenant_rejects_empty_and_malformed():
    with pytest.raises(ScraperError):
        _resolve_tenant("")
    with pytest.raises(ScraperError):
        _resolve_tenant("https://not-darwinbox.example.com/careers")
