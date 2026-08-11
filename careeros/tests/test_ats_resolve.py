"""Tests for careeros/ats_resolve.py — the one deterministic company->ATS
resolver reused by discovery, `watchlist add`, and ATS-change recovery. No
real network calls: httpx.Client is patched to route through
httpx.MockTransport, same pattern as test_provider_darwinbox.py."""

from __future__ import annotations

import functools

import pytest

httpx = pytest.importorskip("httpx")
pytest.importorskip("ats_scrapers")

from careeros.ats_resolve import ResolvedAts, resolve_company_ats  # noqa: E402


def _patch_client(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "careeros.ats_resolve.httpx.Client",
        functools.partial(httpx.Client, transport=transport),
    )


def test_resolve_company_ats_recognizes_direct_ats_url_with_zero_requests(monkeypatch):
    """Already-an-ATS-URL is resolved offline — a MockTransport that raises
    on any call proves no HTTP request happens for this path."""
    def _boom(request):
        raise AssertionError("should not make any HTTP request for a direct ATS URL")

    _patch_client(monkeypatch, _boom)
    result = resolve_company_ats("https://jobs.lever.co/epifi")
    assert result == ResolvedAts("lever", "epifi", "https://jobs.lever.co/epifi")


def test_resolve_company_ats_finds_embedded_lever_link_on_careers_page(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://acme.example"
        return httpx.Response(200, html='<html><a href="https://jobs.lever.co/acme">Careers</a></html>')

    _patch_client(monkeypatch, handler)
    result = resolve_company_ats("https://acme.example")
    assert result == ResolvedAts("lever", "acme", "https://acme.example")


def test_resolve_company_ats_tries_careers_path_after_bare_domain_misses(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if str(request.url).endswith("/careers"):
            return httpx.Response(200, html='<a href="https://jobs.ashbyhq.com/acme">Jobs</a>')
        return httpx.Response(200, html="<html>no ats link here</html>")

    _patch_client(monkeypatch, handler)
    result = resolve_company_ats("https://acme.example")
    assert result == ResolvedAts("ashby", "acme", "https://acme.example/careers")
    assert calls == ["https://acme.example", "https://acme.example/careers"]


def test_resolve_company_ats_finds_darwinbox_supplement(monkeypatch):
    """darwinbox has no member in the installed ats_scrapers' ATSType enum,
    so resolve_careers_url alone can never recognize it -- this is the one
    pattern ats_resolve.py adds on top."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, html='<a href="https://mpl.darwinbox.in/ms/candidate/careers">Careers</a>')

    _patch_client(monkeypatch, handler)
    result = resolve_company_ats("https://mpl.example")
    assert result == ResolvedAts("darwinbox", "mpl.in", "https://mpl.example")


def test_resolve_company_ats_returns_none_when_unresolvable(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, html="<html>just a marketing site, no ats link anywhere</html>")

    _patch_client(monkeypatch, handler)
    assert resolve_company_ats("https://acme.example") is None


def test_resolve_company_ats_handles_non_200_and_keeps_trying(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/jobs"):
            return httpx.Response(200, html='<a href="https://boards.greenhouse.io/acme">Jobs</a>')
        return httpx.Response(404)

    _patch_client(monkeypatch, handler)
    result = resolve_company_ats("https://acme.example")
    assert result == ResolvedAts("greenhouse", "acme", "https://acme.example/jobs")


def test_resolve_company_ats_respects_request_cap(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(404)

    _patch_client(monkeypatch, handler)
    assert resolve_company_ats("https://acme.example") is None
    assert len(calls) <= 3


def test_resolve_company_ats_survives_url_regex_debris(monkeypatch):
    """Regression: real HTML can contain near-URL text (JS/JSON debris) that
    makes urlparse raise ValueError (e.g. a bracket sequence misread as an
    IPv6 host) -- must be treated as 'not a match', never a crash."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, html='<script>var x = "https://[not-a-real-host/broken";</script>')

    _patch_client(monkeypatch, handler)
    assert resolve_company_ats("https://acme.example") is None


def test_resolve_company_ats_empty_input_returns_none():
    assert resolve_company_ats("") is None
    assert resolve_company_ats("   ") is None


def test_resolve_company_ats_adds_https_scheme_when_missing(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith("https://")
        return httpx.Response(200, html="no ats link")

    _patch_client(monkeypatch, handler)
    resolve_company_ats("acme.example")  # no scheme given
