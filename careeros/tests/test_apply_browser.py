"""Tests for careeros/apply/browser.py — the HTTP-first, optional headless-
Playwright-fallback form-text fetcher used by the `apply` batch stage
(P2.10). The HTTP tier is exercised against mocked `requests.get` responses
(no real network). Playwright itself is only reached via a real smoke test
guarded by `pytest.importorskip` — this repo's default `[dev]`-only test
env doesn't have the optional `[apply]` extra installed, so that one test
is expected to skip locally/in CI unless it is."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from careeros.apply.browser import (
    REASON_BOT_CHECK,
    REASON_CLOSED_POSTING,
    REASON_LOGIN_WALL,
    REASON_NETWORK_ERROR,
    REASON_PLAYWRIGHT_MISSING,
    _extract_visible_text,
    _fetch_via_http,
    _fetch_via_playwright,
    _looks_like_bot_check,
    _looks_like_closed_posting,
    _looks_like_js_shell,
    _looks_like_login_wall,
    _looks_like_unresolved_apply_page,
    _playwright_installed,
    fetch_visible_text,
)

# ── HTML text extraction ─────────────────────────────────────────────────


def test_extract_visible_text_strips_script_and_style():
    html = (
        "<html><head><style>.x{color:red}</style></head>"
        "<body><script>var x=1;</script><p>Hello world</p></body></html>"
    )
    text = _extract_visible_text(html)
    assert "Hello world" in text
    assert "color:red" not in text
    assert "var x=1" not in text


def test_extract_visible_text_tolerates_malformed_html():
    """HTMLParser is lenient by construction -- must not raise on real-world
    unclosed/broken markup."""
    text = _extract_visible_text("<div><p>Unclosed paragraph<div>Next</div>")
    assert "Unclosed paragraph" in text
    assert "Next" in text


# ── SPA-shell heuristic ──────────────────────────────────────────────────


def test_looks_like_js_shell_true_for_thin_text():
    assert _looks_like_js_shell(None, "short") is True


def test_looks_like_js_shell_false_for_real_content():
    """Real, substantial content that mentions applying but ALSO reads like
    a real job description (not just a bare, repeated CTA) must not be
    flagged -- distinguishing this from the Coinbase-style unresolved-CTA
    case is `_looks_like_unresolved_apply_page`'s job, not this heuristic's
    (see its own tests below)."""
    long_text = (
        "We are looking for a candidate with 5+ years of experience. "
        "Responsibilities include leading cross-functional projects. "
    ) * 10 + "Apply now to join our team."
    assert _looks_like_js_shell("<html><body>...</body></html>", long_text) is False


def test_looks_like_js_shell_true_for_spa_root_with_thin_boilerplate():
    """A known SPA mount point present AND text still thin even past the
    base floor -- the higher bar for a suspected client-rendered shell."""
    raw_html = '<div id="root"></div><footer>cookie notice</footer>'
    extracted = "cookie notice " * 15  # ~225 chars: over base floor, under 2x
    assert len(extracted) > 200
    assert _looks_like_js_shell(raw_html, extracted) is True


def test_looks_like_js_shell_false_when_spa_marker_present_but_content_is_ample():
    raw_html = '<div id="root">' + ("Real server-rendered content. " * 30) + "</div>"
    extracted = "Real server-rendered content. " * 30
    assert _looks_like_js_shell(raw_html, extracted) is False


# ── unresolved-CTA heuristic (Coinbase-style pages) ──────────────────────


def test_looks_like_unresolved_apply_page_true_for_cta_without_form_fields():
    """Ample marketing/nav text, ends at an unclicked Apply button, no real
    form-field labels anywhere -- the case _looks_like_js_shell misses."""
    text = (
        "About the team. We build the future of finance. Our culture "
        "values are here. Learn more about benefits and perks below. "
    ) * 8 + "Apply now"
    assert _looks_like_unresolved_apply_page(text) is True


def test_looks_like_unresolved_apply_page_false_when_form_fields_present():
    """A real Greenhouse/Lever-style form has multiple distinct field
    labels -- must not be flagged even though it also says 'Apply now'."""
    text = (
        "Apply now\nFirst Name\nLast Name\nEmail address\nPhone number\n"
        "Resume\nAttach\nCover Letter\nSubmit Application"
    )
    assert _looks_like_unresolved_apply_page(text) is False


def test_looks_like_unresolved_apply_page_false_for_single_incidental_field_mention():
    """A single incidental field-marker mention (e.g. 'resume' inside an
    unrelated sentence about how applications get reviewed) alone isn't
    enough to call this a real form -- but it should also not be misread as
    an unresolved CTA if there's no CTA phrase in range. This checks the
    field-count-of-one case in isolation: with zero CTA occurrences, it's
    simply not flagged either way."""
    text = "We use software to help review resumes as part of our process. " * 10
    assert _looks_like_unresolved_apply_page(text) is False


def test_looks_like_unresolved_apply_page_false_when_cta_repeated_many_times():
    """A large, evenly-repeated CTA count reads as templated/padded text,
    not a single unresolved button -- deliberately not flagged."""
    text = "Apply now. " * 50
    assert _looks_like_unresolved_apply_page(text) is False


# ── login-wall heuristic ──────────────────────────────────────────────────


def test_looks_like_login_wall_true_for_linkedin_boilerplate():
    text = "Sign in to see who you already know at this company. New to LinkedIn? Join now."
    assert _looks_like_login_wall(text) is True


def test_looks_like_login_wall_false_for_real_form_text():
    text = "First Name\nLast Name\nResume\nSubmit Application"
    assert _looks_like_login_wall(text) is False


# ── closed-posting heuristic ─────────────────────────────────────────────


def test_looks_like_closed_posting_true_for_closed_phrase():
    text = "Thanks for your interest -- this job is no longer accepting applications."
    assert _looks_like_closed_posting(text) is True


def test_looks_like_closed_posting_false_for_open_posting():
    text = "First Name\nLast Name\nResume\nSubmit Application"
    assert _looks_like_closed_posting(text) is False


# ── bot-check heuristic ───────────────────────────────────────────────────


def test_looks_like_bot_check_true_for_cloudflare_challenge():
    """Real observed text from a Cloudflare bot-detection challenge page
    (e.g. Coinbase's careers site blocking a headless-browser fingerprint) --
    never bypassed, only named specifically instead of a generic failure."""
    text = (
        "www.coinbase.com\nPerforming security verification\n\nThis website "
        "uses a security service to protect against malicious bots. This "
        "page is displayed while the website verifies you are not a bot.\n"
        "Ray ID: a18f1c9dcde0ff7b\nPerformance and Security by Cloudflare"
    )
    assert _looks_like_bot_check(text) is True


def test_looks_like_bot_check_false_for_real_form():
    text = "First Name\nLast Name\nResume\nSubmit Application"
    assert _looks_like_bot_check(text) is False


# ── HTTP tier ─────────────────────────────────────────────────────────────


def _mock_response(text: str, status: int = 200):
    resp = MagicMock()
    resp.text = text
    if status >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(f"{status}")
    else:
        resp.raise_for_status.return_value = None
    return resp


def test_fetch_via_http_returns_none_on_network_error():
    with patch("careeros.apply.browser.requests.get", side_effect=requests.ConnectionError("boom")):
        raw, text = _fetch_via_http("https://example.com/apply", timeout=5)
    assert raw is None and text is None


def test_fetch_via_http_returns_none_on_http_error_status():
    with patch("careeros.apply.browser.requests.get", return_value=_mock_response("x", status=404)):
        raw, text = _fetch_via_http("https://example.com/apply", timeout=5)
    assert raw is None and text is None


def test_fetch_via_http_extracts_text_on_success():
    html = "<html><body><p>" + ("Application question one. " * 20) + "</p></body></html>"
    with patch("careeros.apply.browser.requests.get", return_value=_mock_response(html)):
        raw, text = _fetch_via_http("https://example.com/apply", timeout=5)
    assert raw == html
    assert "Application question one." in text


# ── Playwright tier ───────────────────────────────────────────────────────


def test_fetch_via_playwright_returns_none_when_not_installed():
    """Regression: the previous version of this test relied on a REAL absent
    import and skipped itself whenever playwright WAS installed -- and CI
    (ci.yml) always installs the [apply] extra, so this always skipped
    there. The "Playwright Missing" degradation path -- exactly what a fresh
    OSS clone without [apply] hits -- was verified nowhere in CI.

    Fixed by forcing the ImportError deterministically: setting a
    sys.modules entry to None makes Python's import system raise ImportError
    for that name regardless of whether the real package is actually
    installed, so this now runs (and means something) in every environment."""
    with patch.dict("sys.modules", {"playwright.sync_api": None}):
        assert _fetch_via_playwright("https://example.com/apply", timeout=1) is None


def test_playwright_installed_returns_false_when_import_fails():
    """Same deterministic-ImportError technique as the test above, applied to
    the sibling function `fetch_visible_text` actually calls to decide
    REASON_PLAYWRIGHT_MISSING -- also environment-independent, also
    previously unverified by any direct test."""
    with patch.dict("sys.modules", {"playwright.sync_api": None}):
        assert _playwright_installed() is False


def test_playwright_installed_returns_true_when_import_succeeds():
    with patch.dict("sys.modules", {"playwright.sync_api": MagicMock()}):
        assert _playwright_installed() is True


def test_fetch_via_playwright_smoke_real_browser():
    """Real headless-browser round trip against a local fixture page --
    only runs when the optional [apply] extra is actually installed."""
    pytest.importorskip("playwright", reason="requires the optional [apply] extra")
    from pathlib import Path

    fixture = Path(__file__).parent / "fixtures" / "apply_form.html"
    if not fixture.exists():
        pytest.skip("fixture apply_form.html not present")
    text = _fetch_via_playwright(fixture.resolve().as_uri(), timeout=10)
    assert text is not None
    assert "Why do you want to work here" in text


def test_fetch_via_playwright_returns_none_on_any_failure():
    """Never raises -- a launch failure, timeout, or navigation error all
    collapse to None (the caller treats it as 'not readable', not a crash)."""
    fake_sync_playwright = MagicMock()
    fake_sync_playwright.return_value.__enter__.side_effect = RuntimeError("boom")
    with patch.dict("sys.modules", {"playwright.sync_api": MagicMock(sync_playwright=fake_sync_playwright)}):
        assert _fetch_via_playwright("https://example.com/apply", timeout=1) is None


def _fake_playwright_page(inner_text_results, wait_for_load_state_effect=None):
    """A mocked Playwright chain (sync_playwright() -> chromium.launch() ->
    new_page()) whose `page.inner_text` returns `inner_text_results` in
    order on successive calls. Mocked rather than a real browser + real JS
    timing, which would race against CI's own timing variance -- these
    tests are about the RETRY LOGIC itself, not actual browser hydration."""
    fake_page = MagicMock()
    fake_page.inner_text.side_effect = inner_text_results
    if wait_for_load_state_effect is not None:
        fake_page.wait_for_load_state.side_effect = wait_for_load_state_effect

    fake_browser = MagicMock()
    fake_browser.new_page.return_value = fake_page

    fake_context = MagicMock()
    fake_context.chromium.launch.return_value = fake_browser

    fake_sync_playwright = MagicMock()
    fake_sync_playwright.return_value.__enter__.return_value = fake_context
    return fake_page, fake_sync_playwright


def test_fetch_via_playwright_retries_when_first_extract_is_thin():
    """Regression: a real Workday-hosted posting (DBS Bank) returned only
    132 characters ("Loading" plus nav chrome) from the fixed 1.5s buffer
    alone, because the SPA hadn't finished hydrating -- and that got
    misclassified downstream as "no essay questions" instead of "never saw
    the real form." When the first extract is under _MIN_CONTENT_CHARS, a
    second (post-networkidle) read that returns MORE text must win."""
    thin_text = "Loading"
    full_text = "Real Application Form\n" + ("Field label. " * 40).strip()
    fake_page, fake_sync_playwright = _fake_playwright_page([thin_text, full_text])

    with patch.dict("sys.modules", {"playwright.sync_api": MagicMock(sync_playwright=fake_sync_playwright)}):
        text = _fetch_via_playwright("https://example.com/apply", timeout=10)

    assert text == full_text
    fake_page.wait_for_load_state.assert_called_once_with("networkidle", timeout=5000)
    assert fake_page.inner_text.call_count == 2


def test_fetch_via_playwright_does_not_retry_when_first_extract_is_substantial():
    """The fast path -- most pages -- must not pay for a second read at all."""
    real_text = "Real Application Form\n" + ("Field label. " * 40).strip()
    fake_page, fake_sync_playwright = _fake_playwright_page([real_text])

    with patch.dict("sys.modules", {"playwright.sync_api": MagicMock(sync_playwright=fake_sync_playwright)}):
        text = _fetch_via_playwright("https://example.com/apply", timeout=10)

    assert text == real_text
    fake_page.wait_for_load_state.assert_not_called()
    assert fake_page.inner_text.call_count == 1


def test_fetch_via_playwright_keeps_first_extract_if_retry_is_not_longer():
    """A retry that comes back equal-or-shorter (the page genuinely never
    gained content) must not silently replace a perfectly fine first read --
    "longer wins" only, never "second read always wins"."""
    thin_text = "Loading a bit more"
    even_thinner = "Load"
    fake_page, fake_sync_playwright = _fake_playwright_page([thin_text, even_thinner])

    with patch.dict("sys.modules", {"playwright.sync_api": MagicMock(sync_playwright=fake_sync_playwright)}):
        text = _fetch_via_playwright("https://example.com/apply", timeout=10)

    assert text == thin_text


def test_fetch_via_playwright_retry_survives_networkidle_never_firing():
    """`wait_for_load_state("networkidle", ...)` is a bounded FALLBACK-ONLY
    wait -- see the function's own docstring on why networkidle is never the
    PRIMARY wait. A page that never goes network-idle within the 5s cap must
    not abort the retry; the second `inner_text` read still happens."""
    full_text = "Real Application Form\n" + ("Field label. " * 40).strip()
    fake_page, fake_sync_playwright = _fake_playwright_page(
        ["Loading", full_text],
        wait_for_load_state_effect=TimeoutError("never idle"),
    )

    with patch.dict("sys.modules", {"playwright.sync_api": MagicMock(sync_playwright=fake_sync_playwright)}):
        text = _fetch_via_playwright("https://example.com/apply", timeout=10)

    assert text == full_text


def test_fetch_via_playwright_retry_survives_second_read_failing():
    """If the SECOND `inner_text` call itself raises (page navigated away,
    closed, whatever), the already-captured first read must still be
    returned -- a failed retry attempt must never discard a good result."""
    thin_text = "Loading"
    fake_page = MagicMock()
    fake_page.inner_text.side_effect = [thin_text, RuntimeError("page closed")]
    fake_browser = MagicMock()
    fake_browser.new_page.return_value = fake_page
    fake_context = MagicMock()
    fake_context.chromium.launch.return_value = fake_browser
    fake_sync_playwright = MagicMock()
    fake_sync_playwright.return_value.__enter__.return_value = fake_context

    with patch.dict("sys.modules", {"playwright.sync_api": MagicMock(sync_playwright=fake_sync_playwright)}):
        text = _fetch_via_playwright("https://example.com/apply", timeout=10)

    assert text == thin_text


# ── orchestration: fetch_visible_text ────────────────────────────────────


def test_fetch_visible_text_prefers_http_when_content_is_real():
    long_text = "Real application question content. " * 20
    with patch("careeros.apply.browser._fetch_via_http", return_value=("<html></html>", long_text)), \
         patch("careeros.apply.browser._fetch_via_playwright") as mock_pw:
        text, method, reason = fetch_visible_text("https://example.com/apply")
    assert text == long_text and method == "http" and reason is None
    mock_pw.assert_not_called()


def test_fetch_visible_text_falls_back_to_playwright_when_http_is_js_shell():
    with patch("careeros.apply.browser._fetch_via_http", return_value=('<div id="root"></div>', "thin")), \
         patch("careeros.apply.browser._playwright_installed", return_value=True), \
         patch("careeros.apply.browser._fetch_via_playwright",
               return_value="Real rendered question text here, at real length."):
        text, method, reason = fetch_visible_text("https://example.com/apply")
    assert text == "Real rendered question text here, at real length."
    assert method == "playwright" and reason is None


def test_fetch_visible_text_returns_none_when_both_tiers_fail():
    """Both tiers empty, Playwright IS installed (so it's not a missing-
    extra case) and the HTTP tier had a real result (so it's not a network
    error either) -- the generic, unclassified failure case."""
    with patch("careeros.apply.browser._fetch_via_http", return_value=("<html></html>", "short")), \
         patch("careeros.apply.browser._playwright_installed", return_value=True), \
         patch("careeros.apply.browser._fetch_via_playwright", return_value=None):
        text, method, reason = fetch_visible_text("https://example.com/apply")
    assert text is None and method == "none" and reason is None


def test_fetch_visible_text_reports_login_wall():
    text_with_wall = "Sign in to see who you already know. " * 10
    with patch("careeros.apply.browser._fetch_via_http", return_value=("<html></html>", text_with_wall)):
        text, method, reason = fetch_visible_text("https://example.com/apply")
    assert text == text_with_wall and method == "http" and reason == REASON_LOGIN_WALL


def test_fetch_visible_text_reports_closed_posting():
    text_closed = "Thanks for your interest. This posting has closed. " * 5
    with patch("careeros.apply.browser._fetch_via_http", return_value=("<html></html>", text_closed)):
        text, method, reason = fetch_visible_text("https://example.com/apply")
    assert text == text_closed and method == "http" and reason == REASON_CLOSED_POSTING


def test_fetch_visible_text_closed_posting_takes_priority_over_login_wall():
    """A page that matches both (e.g. a LinkedIn login-gated page that also
    states the posting is closed) reports the more specific, actionable
    outcome: closed."""
    text_both = "Sign in to see who you already know. This job is no longer accepting applications. " * 5
    with patch("careeros.apply.browser._fetch_via_http", return_value=("<html></html>", text_both)):
        text, method, reason = fetch_visible_text("https://example.com/apply")
    assert reason == REASON_CLOSED_POSTING


def test_fetch_visible_text_reports_bot_check_from_playwright_tier():
    """The real-world Coinbase case: the HTTP tier gets Coinbase's own real
    (but unresolved-CTA) marketing page, escalates to Playwright for the
    real form, and Playwright instead hits a Cloudflare bot-detection
    challenge -- a distinct, specific outcome, not a generic failure."""
    unresolved_apply_text = (
        "About the team. We build the future of finance. Our culture "
        "values are here. Learn more about benefits and perks below. "
    ) * 8 + "Apply now"
    bot_check_text = "Performing security verification. Ray ID: abc123."
    with patch("careeros.apply.browser._fetch_via_http",
               return_value=("<html></html>", unresolved_apply_text)), \
         patch("careeros.apply.browser._playwright_installed", return_value=True), \
         patch("careeros.apply.browser._fetch_via_playwright", return_value=bot_check_text):
        text, method, reason = fetch_visible_text("https://example.com/apply")
    assert text == bot_check_text and method == "playwright" and reason == REASON_BOT_CHECK


def test_fetch_visible_text_reports_playwright_missing():
    """HTTP tier came back as a JS shell needing escalation, and Playwright's
    Python package isn't importable -- distinct from a launch/timeout
    failure with the package installed."""
    with patch("careeros.apply.browser._fetch_via_http", return_value=('<div id="root"></div>', "thin")), \
         patch("careeros.apply.browser._playwright_installed", return_value=False):
        text, method, reason = fetch_visible_text("https://example.com/apply")
    assert text is None and method == "none" and reason == REASON_PLAYWRIGHT_MISSING


def test_fetch_visible_text_reports_network_error():
    """HTTP tier failed outright (no text at all, not just thin), and
    Playwright also came back empty -- reported as a network error rather
    than the generic unclassified failure."""
    with patch("careeros.apply.browser._fetch_via_http", return_value=(None, None)), \
         patch("careeros.apply.browser._playwright_installed", return_value=True), \
         patch("careeros.apply.browser._fetch_via_playwright", return_value=None):
        text, method, reason = fetch_visible_text("https://example.com/apply")
    assert text is None and method == "none" and reason == REASON_NETWORK_ERROR

