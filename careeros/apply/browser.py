"""Reads an application form's visible page text, for the `apply` stage's
automatic Application Answers generation (P2.10; Apply-tier, score >= 4.0,
jobs only — see cli.py `_apply_prepare`).

Two-tier by design, HTTP-first:

1. `_fetch_via_http` — the already-core `requests` dependency plus a stdlib
   `html.parser` text extractor. Zero new dependencies. Covers most ATS
   application pages (Greenhouse, Lever, Ashby, and similar are largely
   server-rendered HTML), since most forms are publicly VIEWABLE even though
   submitting them requires an account.
2. `_fetch_via_playwright` — a background, HEADLESS browser, reached only
   when the HTTP tier's result looks genuinely unusable as a form — either a
   thin/SPA shell (`_looks_like_js_shell`) or a page that server-rendered
   plenty of text but never got past an unclicked "Apply now"-style call to
   action (`_looks_like_unresolved_apply_page` — see its docstring for why
   char-count alone doesn't catch this). This is the optional
   `careeros[apply]` extra (`pip install 'careeros[apply]'`, THEN
   `playwright install chromium` for the actual browser binary — see
   README.md's Application Answers section); imported lazily so the rest of
   CareerOS never depends on it. It launches its OWN isolated browser
   context — never the user's real browser, never a visible window, and
   never touches whatever the user is doing elsewhere on their machine.

Neither tier has any per-ATS selector logic: both just return the page's
rendered visible text, and the calling agent identifies the actual questions
semantically. That is deliberate — a selector keyed to one ATS's current DOM
is exactly the kind of code that silently rots; returning raw text and
letting the reasoning step do the interpretation has nothing to rot.

Every failure mode in this module — no network, a login redirect, a
timeout, Playwright not installed, a genuine crash inside Playwright — is
swallowed, never raised. What used to collapse into a single generic
"nothing readable" outcome now comes back as one of a small set of specific,
mechanically-detected REASON_* strings (see `fetch_visible_text`) so the
`apply` stage can record — and the Sheet can show — exactly WHY a job needs
manual review (a real login wall, a closed posting, the optional Playwright
extra not being installed, or a genuine fetch failure) instead of one opaque
`manual_required` bucket that conflated all four."""

from __future__ import annotations

from html.parser import HTMLParser
from typing import Optional

import requests

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Below this many characters of extracted text, a page is almost certainly
# either an error/login page or an unrendered SPA shell — not a real
# application form. Not a precise science; a cheap, honest floor.
_MIN_CONTENT_CHARS = 200

# Known SPA framework mount points. Their PRESENCE alone doesn't prove a page
# needs JavaScript (plenty of server-rendered pages still use React for
# interactivity) — only combined with thin extracted text does it raise the
# bar further (see `_looks_like_js_shell`).
_SPA_ROOT_MARKERS = ('id="root"', "id='root'", 'id="app"', "id='app'", 'id="__next"')

# A page can clear _MIN_CONTENT_CHARS by a wide margin and still not be the
# form — e.g. a company's own careers site server-renders a full page of
# marketing copy, nav, and footer around an unclicked "Apply now" button,
# with the real form loading via a separate route the HTTP tier never
# reaches. Char-count alone doesn't catch this; only combining "the text
# ends around an apply call-to-action" with "none of the field labels a
# real form would have are anywhere in it" does. Generic phrase matching,
# not a per-ATS selector — same category of heuristic as _SPA_ROOT_MARKERS.
_UNRESOLVED_CTA_MARKERS = ("apply now", "apply for this job", "apply here")
_FORM_FIELD_MARKERS = (
    "first name", "last name", "resume", "cover letter", "cv",
    "attach", "select...", "required field", "submit application",
    "phone number", "email address", "linkedin profile",
)

# Phrases that show up on a page gated behind a login wall even though the
# HTTP fetch technically "succeeded" (it got real, non-thin text — just not
# the form). Mostly LinkedIn's own boilerplate, since many `custom`-ATS
# apply_urls resolve to a LinkedIn job-view page rather than the employer's
# own site; a few generic phrases catch non-LinkedIn login gates too.
_LOGIN_WALL_MARKERS = (
    "sign in to see who you already know",
    "new to linkedin?",
    "agree & join linkedin",
    "log in to apply",
    "sign in to apply",
    "please log in to continue",
    "you must be signed in to apply",
)

# Phrases indicating the posting itself is no longer accepting applications
# — a real, correctly-classifiable outcome, distinct from "couldn't read the
# form". Checked against whatever text was actually fetched, regardless of
# which tier produced it.
_CLOSED_POSTING_MARKERS = (
    "no longer accepting applications",
    "position has been filled",
    "this job is no longer available",
    "posting has closed",
    "job has expired",
    "this position is no longer open",
    "applications are now closed",
)

# A page genuinely fetched (by either tier) can still be a bot-detection
# challenge (Cloudflare and similar) rather than the real form — e.g.
# Coinbase's careers pages block plain headless-browser fingerprints even
# though the same page is reachable over the zero-JS HTTP tier. This is
# never something to try to bypass (see this repo's operating rules on
# CAPTCHA/bot-detection) — only to name accurately instead of collapsing
# into the same generic bucket as an unrelated fetch failure.
_BOT_CHECK_MARKERS = (
    "performing security verification",
    "checking your browser before accessing",
    "please stand by, while we are checking your browser",
    "verify you are human",
    "needs to review the security of your connection",
    "attention required! | cloudflare",
    "ray id:",
)


class _VisibleTextExtractor(HTMLParser):
    """Strips `<script>`/`<style>`/`<noscript>`/`<template>` content and
    collects everything else — a rough but adequate approximation of "what a
    person looking at this page would read", without a full CSS/layout
    engine. HTMLParser is lenient by construction, so malformed real-world
    HTML doesn't raise here."""

    _SKIP_TAGS = {"script", "style", "noscript", "template"}

    def __init__(self):
        super().__init__()
        self._skip_depth = 0
        self._chunks: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0:
            text = data.strip()
            if text:
                self._chunks.append(text)

    def get_text(self) -> str:
        return "\n".join(self._chunks)


def _extract_visible_text(html_text: str) -> str:
    parser = _VisibleTextExtractor()
    try:
        parser.feed(html_text)
    except Exception:
        pass
    return parser.get_text()


def _fetch_via_http(url: str, timeout: float) -> tuple[Optional[str], Optional[str]]:
    """Returns (raw_html, extracted_text), or (None, None) on any failure
    (network error, non-2xx status, timeout) — never raises."""
    try:
        resp = requests.get(url, timeout=timeout, headers={"User-Agent": _USER_AGENT})
        resp.raise_for_status()
    except requests.RequestException:
        return None, None
    return resp.text, _extract_visible_text(resp.text)


def _looks_like_js_shell(raw_html: Optional[str], extracted_text: str) -> bool:
    """A cheap, honest heuristic, not a certainty. A page that rendered
    almost no visible text server-side needs client-side JavaScript to show
    its real content — the HTTP-only fetch got the shell, not the form. A
    known SPA mount point in the markup raises the bar further, since some
    frameworks server-render a little boilerplate around an otherwise-empty
    root div."""
    if len(extracted_text) < _MIN_CONTENT_CHARS:
        return True
    if raw_html and any(marker in raw_html for marker in _SPA_ROOT_MARKERS):
        return len(extracted_text) < _MIN_CONTENT_CHARS * 2
    return False


def _looks_like_unresolved_apply_page(extracted_text: str) -> bool:
    """Catches the Coinbase-style case `_looks_like_js_shell` can't: a page
    that server-renders a healthy amount of real text — marketing copy, nav,
    footer, well past `_MIN_CONTENT_CHARS` — but never gets past an
    unclicked "Apply now" button, because the real form lives behind a
    client-side route the HTTP tier never follows. Two conditions, both
    required, to keep this from misfiring on genuine job-description pages
    that happen to mention applying:

    1. None of `_FORM_FIELD_MARKERS`' phrases appear at least twice — a real
       form has multiple distinct field labels (name, resume, phone, ...);
       one incidental hit (e.g. "resume" mentioned once in an unrelated
       sentence about how applications get reviewed) isn't enough to call a
       page a real form.
    2. An "apply now"-style call to action appears a SMALL number of times
       (1-3 — a real page has one apply button, maybe echoed in a nav/footer
       link). A large, evenly-repeated count is a sign of templated or
       padded text, not a single unresolved button, and is deliberately
       NOT flagged here."""
    lowered = extracted_text.lower()
    field_hits = sum(1 for marker in _FORM_FIELD_MARKERS if marker in lowered)
    if field_hits >= 2:
        return False
    cta_occurrences = sum(lowered.count(marker) for marker in _UNRESOLVED_CTA_MARKERS)
    return 1 <= cta_occurrences <= 3


def _looks_like_login_wall(extracted_text: str) -> bool:
    """Real, substantial text — passes `_looks_like_js_shell` cleanly — but
    it's the login page's boilerplate, not the form. Mostly LinkedIn, since
    many `custom`-ATS apply_urls resolve to a LinkedIn job-view page."""
    lowered = extracted_text.lower()
    return any(marker in lowered for marker in _LOGIN_WALL_MARKERS)


def _looks_like_closed_posting(extracted_text: str) -> bool:
    """The posting itself says it's no longer accepting applications — a
    real, correctly-classifiable outcome, not a fetch problem."""
    lowered = extracted_text.lower()
    return any(marker in lowered for marker in _CLOSED_POSTING_MARKERS)


def _looks_like_bot_check(extracted_text: str) -> bool:
    """A Cloudflare-style bot-detection challenge page, not the real form —
    see _BOT_CHECK_MARKERS' comment. Never bypassed, only named."""
    lowered = extracted_text.lower()
    return any(marker in lowered for marker in _BOT_CHECK_MARKERS)


def _fetch_via_playwright(url: str, timeout: float) -> Optional[str]:
    """Background, headless-only fetch in its own isolated browser context —
    see module docstring for the isolation guarantee. Returns None (never
    raises) if Playwright isn't installed, the browser fails to launch, the
    page times out, or a login redirect leaves nothing meaningful to read.

    Waits for `wait_until="load"` (fires once the page's initial resources
    finish loading), NOT `"networkidle"` — networkidle waits for ALL network
    activity to go quiet, which many real pages never do (analytics
    beacons, chat widgets, a bot-check's own verification polling), so it
    can hard-timeout with ZERO text captured even though the real content
    rendered within a second or two. A short fixed buffer after `load` lets
    client-side JS finish painting before `inner_text` reads the page."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.goto(url, timeout=timeout * 1000, wait_until="load")
                page.wait_for_timeout(1500)
                text = page.inner_text("body")
            finally:
                browser.close()
    except Exception:
        return None

    text = text.strip()
    return text or None


def _playwright_installed() -> bool:
    """Whether the optional `careeros[apply]` extra's Python package is
    importable. Deliberately separate from `_fetch_via_playwright`'s own
    lazy import (which folds "not installed" and "installed but the launch
    failed" into the same None return, by design — see its docstring) so
    `fetch_visible_text` can tell those two cases apart and report
    REASON_PLAYWRIGHT_MISSING only for the former. This does NOT confirm the
    `chromium` browser binary (`playwright install chromium`) is present —
    only the Python package; a missing binary still surfaces as a normal
    launch failure via `_fetch_via_playwright`'s `except Exception`."""
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return True


# Reasons `fetch_visible_text` can attach to a `(None-or-real-but-wrong, ...)`
# result, letting the `apply` stage assign one of several specific statuses
# instead of one generic `manual_required` (see cli.py's `_apply_prepare`).
# `None` (no reason) means either the fetch succeeded with a usable form, or
# it failed in some other unclassified way -- still folded into the original
# generic manual-review bucket rather than guessing.
REASON_LOGIN_WALL = "login_wall"
REASON_CLOSED_POSTING = "closed_posting"
REASON_PLAYWRIGHT_MISSING = "playwright_missing"
REASON_NETWORK_ERROR = "network_error"
REASON_BOT_CHECK = "bot_check"


def fetch_visible_text(
    url: str, timeout: float = 10.0
) -> tuple[Optional[str], str, Optional[str]]:
    """The `apply` stage's only entry point into form-fetching. Tries the
    zero-dependency HTTP tier first; escalates to the optional headless-
    Playwright tier only when the HTTP result looks genuinely unusable as a
    form -- either JavaScript-gated (`_looks_like_js_shell`) or a
    server-rendered page that never gets past an unclicked Apply button
    (`_looks_like_unresolved_apply_page`) -- most ATS application pages
    resolve on the first tier.

    Whatever text is ultimately fetched (by either tier) is checked against
    the login-wall and closed-posting phrase lists BEFORE being accepted as
    a usable form, since both can produce substantial, real, non-thin text
    that still isn't the form -- checking closed-posting first, since it is
    the more specific and actionable of the two when a page happens to
    match both (a LinkedIn login-gated page that also states the posting is
    closed should be reported as closed, not merely login-gated).

    Returns (text, method, reason):
    - `method` is "http", "playwright", or "none".
    - `text` is the extracted text if a usable form was found, else None.
    - `reason` is None on success, else one of the `REASON_*` constants
      above (login wall, closed posting, Playwright's Python package not
      installed, or a network-level fetch failure) -- or None even on
      failure, if the cause doesn't match any of those specific cases (the
      caller falls back to the original generic manual-review handling).

    Never raises."""
    raw_html, http_text = _fetch_via_http(url, timeout)
    network_error = http_text is None

    if http_text:
        if _looks_like_closed_posting(http_text):
            return http_text, "http", REASON_CLOSED_POSTING
        if _looks_like_login_wall(http_text):
            return http_text, "http", REASON_LOGIN_WALL
        if _looks_like_bot_check(http_text):
            return http_text, "http", REASON_BOT_CHECK
        if not _looks_like_js_shell(raw_html, http_text) and not _looks_like_unresolved_apply_page(
            http_text
        ):
            return http_text, "http", None

    playwright_available = _playwright_installed()
    playwright_text = _fetch_via_playwright(url, timeout) if playwright_available else None

    if playwright_text:
        if _looks_like_closed_posting(playwright_text):
            return playwright_text, "playwright", REASON_CLOSED_POSTING
        if _looks_like_login_wall(playwright_text):
            return playwright_text, "playwright", REASON_LOGIN_WALL
        if _looks_like_bot_check(playwright_text):
            return playwright_text, "playwright", REASON_BOT_CHECK
        return playwright_text, "playwright", None

    if not playwright_available:
        return None, "none", REASON_PLAYWRIGHT_MISSING
    if network_error:
        return None, "none", REASON_NETWORK_ERROR
    return None, "none", None
