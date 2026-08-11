"""Tests for careeros/ats_resolve.py — the one deterministic company->ATS
resolver reused by discovery, `watchlist add`, and ATS-change recovery. No
real network calls: httpx.Client is patched to route through
httpx.MockTransport, same pattern as test_provider_darwinbox.py."""

from __future__ import annotations

import functools
from unittest.mock import MagicMock, patch

import pytest

httpx = pytest.importorskip("httpx")
pytest.importorskip("ats_scrapers")

from careeros.ats_resolve import (  # noqa: E402
    ResolvedAts,
    _fetch_via_browser,
    resolve_company_ats,
    resolve_company_ats_or_fetch_failure,
)


def _patch_client(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "careeros.ats_resolve.httpx.Client",
        functools.partial(httpx.Client, transport=transport),
    )


@pytest.fixture(autouse=True)
def _browser_tier_off_by_default(monkeypatch):
    """The browser last-resort tier must never activate in this file unless
    a test explicitly opts in -- otherwise whether these "unresolved" tests
    stay fast/offline would depend on whether the real `playwright` package
    happens to be installed in whatever venv runs the suite (it is, in this
    project's own dev venv, via the `[apply]` extra), which is exactly the
    un-testable environment-dependency this fixture removes. The dedicated
    browser-tier tests below re-enable it with their own `monkeypatch.setattr`
    on the same target, which wins because it runs after this fixture's
    setup call, inside the test body."""
    monkeypatch.setattr("careeros.ats_resolve._browser_available", lambda: False)


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


def test_resolve_company_ats_finds_zoho_recruit_supplement(monkeypatch):
    """zoho_recruit has no member in the installed ats_scrapers' ATSType
    enum either (same situation as darwinbox) -- this is the other pattern
    ats_resolve.py adds on top, verified live against 6 real tenants
    (see docs/ats-registry.md)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, html='<a href="https://acme.zohorecruit.in/jobs/Careers">Careers</a>')

    _patch_client(monkeypatch, handler)
    result = resolve_company_ats("https://acme.example")
    assert result == ResolvedAts("zoho_recruit", "acme.in", "https://acme.example")


def test_resolve_company_ats_prefers_custom_domain_over_embedded_canonical_link(monkeypatch):
    """Regression (live finding, Zoho's own careers.zohocorp.com): when the
    fetched page is ITSELF served by Zoho Recruit's page-builder (its own
    CDN asset signature is present), the page's own host must win over an
    embedded *.zohorecruit.{in,com} link found within it -- verified live
    that the embedded canonical-domain link (a page-builder RSS artifact)
    does not resolve via the public API for this real case, while the
    custom domain itself does."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            html=(
                '<link rel="stylesheet" href="https://static.zohocdn.com/recruit/css/career-website-common.css">'
                '<a href="https://acme.zohorecruit.com/jobs/Careers/rss">RSS</a>'
            ),
        )

    _patch_client(monkeypatch, handler)
    result = resolve_company_ats("https://careers.acme.example")
    assert result == ResolvedAts("zoho_recruit", "careers.acme.example", "https://careers.acme.example")


def test_resolve_company_ats_zoho_recruit_embed_widget_uses_its_own_site_config(monkeypatch):
    """Regression (live finding, Yellow.ai): a company can embed Zoho
    Recruit's JS WIDGET into an EXISTING page rather than hosting a full
    page-builder career site -- the embedding page (yellow.ai) has no
    public Job_Openings endpoint of its own, but the widget's init call
    names the real site (careers.yellow.ai) explicitly, which does. Using
    the embedding page's own host here (the CDN-signature fallback) would
    be wrong -- this must take priority over that."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            html=(
                '<link rel="stylesheet" href="https://static.zohocdn.com/recruit/embed_careers_site/css/v1.1/embed_jobs.css">'
                '<script>rec_embed_js.load({\n'
                'widget_id:"rec_job_listing_div",\n'
                'page_name:"Careers",\n'
                'source:"CareerSite",\n'
                'site:"https://careers.yellow.ai",\n'
                'brand_color:"#000"});</script>'
            ),
        )

    _patch_client(monkeypatch, handler)
    result = resolve_company_ats("https://yellow.ai")
    assert result == ResolvedAts("zoho_recruit", "careers.yellow.ai", "https://yellow.ai")


def test_resolve_company_ats_zoho_recruit_embedded_link_still_used_when_page_not_zoho_rendered(monkeypatch):
    """The custom-domain priority must not swallow the ordinary case: a
    ordinary marketing page that merely LINKS OUT to an externally hosted
    Zoho Recruit board (no CDN signature on the linking page itself) still
    resolves via the embedded link, exactly as before."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, html='<a href="https://acme.zohorecruit.in/jobs/Careers">Careers</a>')

    _patch_client(monkeypatch, handler)
    result = resolve_company_ats("https://acme.example")
    assert result == ResolvedAts("zoho_recruit", "acme.in", "https://acme.example")


def test_resolve_company_ats_recognizes_greenhouse_boards_api_host(monkeypatch):
    """Regression (live finding, Noora Health): boards-api.greenhouse.io is
    a real Greenhouse board host resolve_careers_url's table doesn't cover
    (only boards./job-boards. variants). Routed through the same
    greenhouse adapter -- no new scraper."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            html='<a href="https://boards-api.greenhouse.io/v1/boards/noorahealth/jobs?content=true">Jobs</a>',
        )

    _patch_client(monkeypatch, handler)
    result = resolve_company_ats("https://acme.example")
    assert result == ResolvedAts("greenhouse", "noorahealth", "https://acme.example")


def test_resolve_company_ats_recognizes_keka_link_as_pending_unsupported_evidence(monkeypatch):
    """Regression (live findings, Jupiter and Peoplebox): a real Keka link
    must be recognized as ats=keka so `watchlist discover` can park it as
    pending_unsupported_ats (Keka has no scraper in ats_scrapers) instead
    of losing it entirely as an indistinguishable 'no detectable ATS'."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, html='<a href="https://jupiter.keka.com/careers">Careers</a>')

    _patch_client(monkeypatch, handler)
    result = resolve_company_ats("https://acme.example")
    assert result == ResolvedAts("keka", "jupiter", "https://acme.example")


def test_resolve_company_ats_recognizes_freshteam_link_as_pending_unsupported_evidence(monkeypatch):
    """Regression (live finding, Haptik): same shape as the Keka case
    above, for Freshteam (also no scraper in ats_scrapers)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, html='<a href="https://haptik.freshteam.com/jobs">Jobs</a>')

    _patch_client(monkeypatch, handler)
    result = resolve_company_ats("https://acme.example")
    assert result == ResolvedAts("freshteam", "haptik", "https://acme.example")


def test_resolve_company_ats_recovers_real_slug_from_greenhouse_embed_link(monkeypatch):
    """Regression (2026-08-11 live finding, CloudSEK/Observe.AI): the
    Greenhouse embed-widget script's real tenant is in `?for=`, not the
    first path segment resolve_careers_url reads ('embed'). Both cloudsek
    and observeai are real, live boards -- must not be discarded."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            html='<script src="https://boards.greenhouse.io/embed/job_board/js?for=cloudsek"></script>',
        )

    _patch_client(monkeypatch, handler)
    result = resolve_company_ats("https://acme.example")
    assert result == ResolvedAts("greenhouse", "cloudsek", "https://acme.example")


def test_resolve_company_ats_skips_unrecoverable_embed_and_keeps_scanning(monkeypatch):
    """A greenhouse embed link with no `for=` param at all can't be
    corrected -- must be treated as no match on THIS url and keep scanning
    the rest of the page, never return the bogus 'embed' slug."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            html=(
                '<script src="https://boards.greenhouse.io/embed/job_board/js"></script>'
                '<a href="https://jobs.lever.co/acme">Careers</a>'
            ),
        )

    _patch_client(monkeypatch, handler)
    result = resolve_company_ats("https://acme.example")
    assert result == ResolvedAts("lever", "acme", "https://acme.example")


def test_resolve_company_ats_skips_darwinbox_marketing_subdomain(monkeypatch):
    """Regression (2026-08-11 live finding, darwinbox.com's own /careers
    page): a vendor's own non-tenant subdomains (explore., blog., academy.)
    must never be mistaken for a real tenant board -- they never carry
    /ms/candidate in the path."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            html='<a href="https://explore.darwinbox.com/darwinbox-partner-with-us">Partner</a>',
        )

    _patch_client(monkeypatch, handler)
    assert resolve_company_ats("https://acme.example") is None


def test_resolve_company_ats_picks_real_darwinbox_tenant_over_earlier_marketing_link(monkeypatch):
    """The real tenant board can appear AFTER a marketing subdomain in
    document order (darwinbox.com's own /careers page does exactly this) --
    must not stop at the first darwinbox.* host it sees."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            html=(
                '<a href="https://explore.darwinbox.com/lp/events">Events</a>'
                '<a href="https://dbx.darwinbox.in/ms/candidatev2/main/careers/allJobs">Careers</a>'
            ),
        )

    _patch_client(monkeypatch, handler)
    result = resolve_company_ats("https://acme.example")
    assert result == ResolvedAts("darwinbox", "dbx.in", "https://acme.example")


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


def _patch_browser(monkeypatch, rendered_html, *, calls=None):
    """Mocks the browser last-resort tier so no real Playwright/Chromium is
    ever launched -- patches `_browser_available` to True and
    `_fetch_via_browser` to a fake that records the URL(s) it was asked to
    render (into the caller-supplied `calls` list, if given) and returns
    `rendered_html` unconditionally, mirroring the real function's
    (url, timeout) -> Optional[str] signature."""
    monkeypatch.setattr("careeros.ats_resolve._browser_available", lambda: True)

    def fake_fetch(url, timeout):
        if calls is not None:
            calls.append(url)
        return rendered_html

    monkeypatch.setattr("careeros.ats_resolve._fetch_via_browser", fake_fetch)


def test_resolve_company_ats_http_resolution_wins_without_touching_browser(monkeypatch):
    """1. HTTP resolution remains first -- the browser tier is available
    (mocked True) and explicitly enabled, but must never be reached when
    the HTTP tier already found a match; a browser call here would be an
    assertion failure."""
    def boom_browser(url, timeout):
        raise AssertionError("browser tier must not run when HTTP already resolved")

    monkeypatch.setattr("careeros.ats_resolve._browser_available", lambda: True)
    monkeypatch.setattr("careeros.ats_resolve._fetch_via_browser", boom_browser)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, html='<a href="https://jobs.lever.co/acme">Careers</a>')

    _patch_client(monkeypatch, handler)
    result = resolve_company_ats("https://acme.example", allow_browser_fallback=True)
    assert result == ResolvedAts("lever", "acme", "https://acme.example")


def test_resolve_company_ats_browser_tier_off_by_default_even_when_available(monkeypatch):
    """Bulk/automated callers (e.g. `watchlist discover`, via
    `resolve_company_ats_or_fetch_failure`) must not silently pay the
    browser tier's cost merely because the optional `[apply]` extra
    happens to be installed for the unrelated apply-stage feature --
    `allow_browser_fallback` defaults to False on both public functions,
    and this tier must stay off even when Playwright IS available and a
    valid browser-candidate URL exists, unless a caller explicitly opts
    in. Covers both `resolve_company_ats` and
    `resolve_company_ats_or_fetch_failure` (discover's own entry point)."""
    def boom_browser(url, timeout):
        raise AssertionError("browser tier must not run without allow_browser_fallback=True")

    monkeypatch.setattr("careeros.ats_resolve._browser_available", lambda: True)
    monkeypatch.setattr("careeros.ats_resolve._fetch_via_browser", boom_browser)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, html="<html>loading...</html>")

    _patch_client(monkeypatch, handler)
    assert resolve_company_ats("https://acme.example") is None
    result, fetched_ok = resolve_company_ats_or_fetch_failure("https://acme.example")
    assert result is None
    assert fetched_ok is True  # the HTTP tier still genuinely fetched something


def test_resolve_company_ats_falls_back_to_browser_only_after_http_finds_nothing(monkeypatch):
    """2. Browser fallback is invoked only after every deterministic HTTP
    candidate path has been tried and found nothing -- proven here by a
    handler that returns a real 200 with no ATS link for every one of the
    three `_CAREERS_PATHS`, and asserting the browser tier only fires once,
    after all three HTTP calls."""
    http_calls = []
    browser_calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        http_calls.append(str(request.url))
        return httpx.Response(200, html="<html>loading...</html>")

    _patch_client(monkeypatch, handler)
    _patch_browser(
        monkeypatch,
        '<a href="https://job-boards.greenhouse.io/gongio">Apply</a>',
        calls=browser_calls,
    )
    result = resolve_company_ats("https://acme.example", allow_browser_fallback=True)
    assert len(http_calls) == 3  # all three _CAREERS_PATHS tried first
    # exactly one render, on the first PATH-based candidate (not the bare
    # domain -- see test_..._prefers_careers_path_over_bare_domain below)
    assert browser_calls == ["https://acme.example/careers"]
    assert result == ResolvedAts("greenhouse", "gongio", "https://acme.example/careers")


def test_resolve_company_ats_missing_playwright_behaves_exactly_as_before(monkeypatch):
    """3. Missing Playwright gracefully falls back to the current unresolved
    behavior -- `_browser_available` False (the real return value when the
    package isn't importable) means the exact same `None` this resolver has
    always returned for a real page with no ATS link, unchanged. Explicitly
    opted in (`allow_browser_fallback=True`) to prove this degrades
    gracefully even for a caller that DID ask for the tier."""
    monkeypatch.setattr("careeros.ats_resolve._browser_available", lambda: False)

    def boom_browser(url, timeout):
        raise AssertionError("must never call the browser tier when unavailable")

    monkeypatch.setattr("careeros.ats_resolve._fetch_via_browser", boom_browser)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, html="<html>loading...</html>")

    _patch_client(monkeypatch, handler)
    assert resolve_company_ats("https://acme.example", allow_browser_fallback=True) is None


def test_resolve_company_ats_feeds_rendered_html_to_existing_resolve_from_html(monkeypatch):
    """4. Rendered page.content() is passed to the existing
    `_resolve_from_html` -- proven by a Keka link (a supplement this module
    already has its own regex for, distinct from the greenhouse case used
    elsewhere in this file) appearing ONLY in the mocked "rendered" HTML,
    never in what the HTTP tier fetched."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, html="<html>loading...</html>")

    _patch_client(monkeypatch, handler)
    _patch_browser(monkeypatch, '<a href="https://jupiter.keka.com/careers">Careers</a>')
    result = resolve_company_ats("https://acme.example", allow_browser_fallback=True)
    assert result == ResolvedAts("keka", "jupiter", "https://acme.example/careers")


def test_resolve_company_ats_browser_tier_navigates_exactly_one_url_never_a_discovered_one(monkeypatch):
    """5. Browser fallback cannot navigate to an arbitrary URL discovered in
    the page -- `_fetch_via_browser` is called with exactly the ONE
    deterministic candidate URL the resolver already constructed (never a
    URL that would only be knowable from inspecting rendered content), and
    exactly once, even though the mocked render itself contains further
    links an attacker-controlled page could plant."""
    browser_calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/careers"):
            return httpx.Response(200, html="<html>loading...</html>")
        return httpx.Response(404)

    _patch_client(monkeypatch, handler)
    # The rendered page itself contains OTHER links (a real page would too)
    # -- proving the resolver only ever acts on the single url it already
    # passed to _fetch_via_browser, never anything discovered inside.
    _patch_browser(
        monkeypatch,
        (
            '<a href="https://evil.example/steal">not a real ATS, must be ignored</a>'
            '<a href="https://job-boards.greenhouse.io/gongio">Apply</a>'
        ),
        calls=browser_calls,
    )
    result = resolve_company_ats("https://acme.example", allow_browser_fallback=True)
    assert browser_calls == ["https://acme.example/careers"]
    assert len(browser_calls) == 1
    assert result == ResolvedAts("greenhouse", "gongio", "https://acme.example/careers")


def test_resolve_company_ats_normal_http_companies_unaffected_by_browser_tier(monkeypatch):
    """6. Existing resolver behavior remains unchanged for normal
    HTTP-resolvable companies, even with the browser tier available -- same
    scenario as test_resolve_company_ats_tries_careers_path_after_bare_
    domain_misses, replayed with the browser tier mocked "on" to prove it's
    never reached."""
    calls = []

    def boom_browser(url, timeout):
        raise AssertionError("browser tier must not run for an HTTP-resolvable company")

    monkeypatch.setattr("careeros.ats_resolve._browser_available", lambda: True)
    monkeypatch.setattr("careeros.ats_resolve._fetch_via_browser", boom_browser)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if str(request.url).endswith("/careers"):
            return httpx.Response(200, html='<a href="https://jobs.ashbyhq.com/acme">Jobs</a>')
        return httpx.Response(200, html="<html>no ats link here</html>")

    _patch_client(monkeypatch, handler)
    result = resolve_company_ats("https://acme.example", allow_browser_fallback=True)
    assert result == ResolvedAts("ashby", "acme", "https://acme.example/careers")
    assert calls == ["https://acme.example", "https://acme.example/careers"]


def test_resolve_company_ats_browser_tier_prefers_careers_path_over_bare_domain(monkeypatch):
    """Regression (live finding, 2026-08-11): the bare domain redirecting to
    a normal marketing homepage is a common false-positive-200 (e.g.
    gong.io -> www.gong.io/) -- tried FIRST in `_CAREERS_PATHS`, so a naive
    "first successful URL" browser-candidate choice picks the homepage,
    which was never going to have an ATS link even rendered. The real
    `/careers` page (tried second) must win instead."""
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/careers"):
            return httpx.Response(200, html="<html>loading...</html>")
        return httpx.Response(200, html="<html>ordinary marketing homepage</html>")

    _patch_client(monkeypatch, handler)
    browser_calls = []
    _patch_browser(
        monkeypatch,
        '<a href="https://job-boards.greenhouse.io/gongio">Apply</a>',
        calls=browser_calls,
    )
    result = resolve_company_ats("https://acme.example", allow_browser_fallback=True)
    assert browser_calls == ["https://acme.example/careers"]
    assert result == ResolvedAts("greenhouse", "gongio", "https://acme.example/careers")


def test_resolve_company_ats_browser_tier_returns_none_still_unresolved(monkeypatch):
    """The browser tier itself finding nothing (rendered page genuinely has
    no ATS link either) falls through to the same None every other
    unresolved case returns -- no special-cased outcome for this tier."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, html="<html>loading...</html>")

    _patch_client(monkeypatch, handler)
    _patch_browser(monkeypatch, "<html>still nothing after rendering</html>")
    assert resolve_company_ats("https://acme.example", allow_browser_fallback=True) is None


def _fake_playwright_chain(page_url, content):
    """Minimal mocked Playwright chain (sync_playwright() ->
    chromium.launch() -> new_page()) for testing `_fetch_via_browser`
    directly, no real browser involved. `page.url` and `page.content()`
    are fixed to exactly what's given; `goto`/`wait_for_timeout`/`close`
    are no-op MagicMocks."""
    fake_page = MagicMock()
    fake_page.url = page_url
    fake_page.content.return_value = content
    fake_browser = MagicMock()
    fake_browser.new_page.return_value = fake_page
    fake_launch_ctx = MagicMock()
    fake_launch_ctx.chromium.launch.return_value = fake_browser
    fake_sync_playwright = MagicMock()
    fake_sync_playwright.return_value.__enter__.return_value = fake_launch_ctx
    return fake_sync_playwright


def test_fetch_via_browser_rejects_cross_host_redirect():
    """JS/meta redirects to a genuinely DIFFERENT host are rejected: the
    render is discarded outright -- never parsed, never returned,
    regardless of what ATS link the redirected-to content happens to
    contain."""
    fake_sync_playwright = _fake_playwright_chain(
        page_url="https://evil.example/somewhere-else",
        content='<a href="https://job-boards.greenhouse.io/gongio">Apply</a>',
    )
    with patch.dict("sys.modules", {"playwright.sync_api": MagicMock(sync_playwright=fake_sync_playwright)}):
        result = _fetch_via_browser("https://acme.example/careers", timeout=10)
    assert result is None


def test_fetch_via_browser_accepts_same_host_www_redirect():
    """Regression (live finding during this feature's own pre-commit
    audit): gong.io itself ordinarily redirects to www.gong.io -- an
    ordinary same-company redirect, not a security concern, and the exact
    case this tier exists to recover. A byte-exact page.url check would
    reject this; the narrower same-host-modulo-"www." comparison must
    accept it."""
    fake_sync_playwright = _fake_playwright_chain(
        page_url="https://www.gong.io/careers",
        content='<a href="https://job-boards.greenhouse.io/gongio">Apply</a>',
    )
    with patch.dict("sys.modules", {"playwright.sync_api": MagicMock(sync_playwright=fake_sync_playwright)}):
        result = _fetch_via_browser("https://gong.io/careers", timeout=10)
    assert result == '<a href="https://job-boards.greenhouse.io/gongio">Apply</a>'


def test_fetch_via_browser_rejects_different_subdomain_not_just_www():
    """The www-normalization is deliberately narrow: a DIFFERENT subdomain
    (not "www.") is still a rejected host mismatch, even though it might
    look superficially related to a human reading it."""
    fake_sync_playwright = _fake_playwright_chain(
        page_url="https://app.acme.example/careers",
        content='<a href="https://job-boards.greenhouse.io/gongio">Apply</a>',
    )
    with patch.dict("sys.modules", {"playwright.sync_api": MagicMock(sync_playwright=fake_sync_playwright)}):
        result = _fetch_via_browser("https://acme.example/careers", timeout=10)
    assert result is None


def test_fetch_via_browser_accepts_matching_page_url():
    """The redirect guard isn't over-broad: a render that lands on exactly
    the requested URL (the normal, non-redirected case) still returns the
    rendered content."""
    fake_sync_playwright = _fake_playwright_chain(
        page_url="https://acme.example/careers",
        content='<a href="https://job-boards.greenhouse.io/gongio">Apply</a>',
    )
    with patch.dict("sys.modules", {"playwright.sync_api": MagicMock(sync_playwright=fake_sync_playwright)}):
        result = _fetch_via_browser("https://acme.example/careers", timeout=10)
    assert result == '<a href="https://job-boards.greenhouse.io/gongio">Apply</a>'


def test_fetch_via_browser_smoke_real_browser():
    """One Playwright-gated live/integration test for the Gong-motivating
    case -- only runs when the optional [apply] extra is actually
    installed, matching test_apply_browser.py's own
    test_fetch_via_playwright_smoke_real_browser pattern (a local static
    fixture via a file:// URI, never a live network call, so this stays
    deterministic and offline).

    The fixture's script builds the target URL from string PARTS at
    runtime rather than writing it as one literal string -- so the
    substring "greenhouse.io" never appears anywhere in the raw file on
    disk. This faithfully mirrors the real Gong finding this tier exists
    for: fetching the raw HTML (or, here, reading the raw file bytes) finds
    nothing; only actually EXECUTING the page's JavaScript produces the
    real link. If this test could pass by regex-scanning the fixture file
    directly, it would not be testing the browser tier at all."""
    pytest.importorskip("playwright", reason="requires the optional [apply] extra")
    from pathlib import Path

    from careeros.ats_resolve import _fetch_via_browser, _resolve_from_html

    fixture = Path(__file__).parent / "fixtures" / "js_rendered_ats_link.html"
    if not fixture.exists():
        pytest.skip("fixture js_rendered_ats_link.html not present")

    raw_bytes = fixture.read_bytes()
    assert b"greenhouse" not in raw_bytes  # proves the link isn't a literal string on disk

    html = _fetch_via_browser(fixture.resolve().as_uri(), timeout=10)
    assert html is not None
    found = _resolve_from_html(html)
    assert found == ("greenhouse", "gongio")


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


# ── resolve_company_ats_or_fetch_failure ────────────────────────────────

def test_fetch_failure_flag_true_on_genuine_no_match(monkeypatch):
    """A real 200 with no ATS link is a genuine 'looked and found nothing'
    -- fetched_ok must be True so the caller applies the normal 30-day TTL,
    not the short transient-failure one."""
    _patch_client(monkeypatch, lambda request: httpx.Response(200, html="no ats link here"))
    result, fetched_ok = resolve_company_ats_or_fetch_failure("https://acme.example")
    assert result is None
    assert fetched_ok is True


def test_fetch_failure_flag_false_when_every_attempt_times_out(monkeypatch):
    """Regression (2026-08-11 live finding, Perfios): a company that
    resolves cleanly on one attempt can read-time-out minutes later. When
    EVERY candidate path fails at the network level, fetched_ok must be
    False so the caller can retry soon instead of recording a month-long
    false 'unresolved'."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated timeout", request=request)

    _patch_client(monkeypatch, handler)
    result, fetched_ok = resolve_company_ats_or_fetch_failure("https://acme.example")
    assert result is None
    assert fetched_ok is False


def test_fetch_failure_flag_true_when_at_least_one_path_succeeds(monkeypatch):
    """Only the bare domain fails at the network level; /careers returns a
    real (ATS-less) 200 -- fetched_ok reflects that SOMETHING was actually
    reachable, not just the first attempt."""
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/careers"):
            return httpx.Response(200, html="no ats link")
        raise httpx.ConnectError("simulated connection error", request=request)

    _patch_client(monkeypatch, handler)
    result, fetched_ok = resolve_company_ats_or_fetch_failure("https://acme.example")
    assert result is None
    assert fetched_ok is True


def test_fetch_failure_flag_true_on_direct_ats_url_with_zero_requests(monkeypatch):
    def _boom(request):
        raise AssertionError("should not make any HTTP request for a direct ATS URL")

    _patch_client(monkeypatch, _boom)
    result, fetched_ok = resolve_company_ats_or_fetch_failure("https://jobs.lever.co/epifi")
    assert result == ResolvedAts("lever", "epifi", "https://jobs.lever.co/epifi")
    assert fetched_ok is True


def test_fetch_failure_flag_true_on_successful_resolution(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, html='<a href="https://jobs.lever.co/acme">Careers</a>')

    _patch_client(monkeypatch, handler)
    result, fetched_ok = resolve_company_ats_or_fetch_failure("https://acme.example")
    assert result == ResolvedAts("lever", "acme", "https://acme.example")
    assert fetched_ok is True
