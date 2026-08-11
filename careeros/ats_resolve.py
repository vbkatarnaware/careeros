"""Deterministic company → ATS resolution — the one mechanism reused by every
company source (reference registry hit, automatic discovery, manual
`watchlist add`, and ATS-change recovery on a stale watchlist entry). See
`docs/ats-registry.md` for the measurement behind this: `resolve_careers_url`
alone (URL-*shape* matching only) resolves ~1/12 real missing-company careers
pages, because most companies front their ATS with a custom domain
(`careers.acme.com`) rather than exposing the ATS's own host directly. This
module adds the one technique that measurably raises that: fetch the
company's own careers page and look for an *embedded* ATS link — Fi Money,
Sarvam AI, Clevertap, and Perfios all resolved this way in the 2026-08-10
session that motivated this module (~5/9 attempts).

Deliberately NOT a browser and NOT a search engine, for the primary path —
plain `httpx` (already a dependency), a handful of GET requests, and a regex
over the HTML for recognizable ATS URLs, reusing
`ats_scrapers.resolve.resolve_careers_url`'s own host table wholesale. No new
host table, no new JS-parsing logic. Never guesses: a non-match returns
`None`, exactly as `resolve_careers_url` does, so a caller can never
accidentally treat an unresolved company as resolved.

v2.3: one strictly-bounded, opt-in LAST-RESORT tier below the HTTP one —
render the single careers-page URL the HTTP loop already tried (and found
nothing on) through a real headless browser, then feed the exact same
`_resolve_from_html` the rendered HTML instead of the raw one. Motivated by a
real, measured finding (2026-08-11 research pass, docs/ats-registry.md):
Gong's careers page returns HTTP 200 with a real Greenhouse board link
(`job-boards.greenhouse.io/gongio`), but the link is injected by client-side
JS at runtime and is not present as a literal string anywhere in the raw
HTTP response — no amount of regex tuning finds it without executing the
page's JavaScript.

This is the SAME optional `careeros[apply]` Playwright dependency and the
same lazy-import/graceful-degradation pattern `careeros/apply/browser.py`
already uses for a different purpose (reading application-form text) — see
`_fetch_via_browser`'s docstring for why the two aren't merged into a shared
module. Zero behavior change for anyone without `[apply]` installed: this
tier only ever fires when Playwright's Python package is importable, and
never runs at all before the HTTP tier has already exhausted every
`_CAREERS_PATHS` candidate and found nothing — see `_resolve_company_ats_impl`.

Two more boundaries, both from the pre-commit audit that reviewed this
tier before it shipped:

- `allow_browser_fallback` (default `False` on both public functions) is a
  SEPARATE gate from "is Playwright installed" — a real browser launch is
  materially more expensive than the HTTP tier, and neither of this
  module's two current callers (ATS-change recovery, `watchlist discover`)
  opts in, so this tier does not fire in normal operation today. It exists
  so a future single-company-at-a-time caller can opt in explicitly
  without every bulk/automated caller silently paying the cost the moment
  `[apply]` happens to be installed for the unrelated apply-stage feature.
- `_fetch_via_browser` rejects the render outright if `page.url` ends up on
  a genuinely different HOST than the one it was asked to fetch (a
  client-side JS redirect, or a `<meta refresh>`, executing during the
  settle window) — see `_same_host_ignoring_www` and that function's
  SECURITY BOUNDARY docstring. The FIRST version of this guard compared
  full URLs byte-for-byte and, caught live during this feature's own
  pre-commit audit, rejected Gong itself — `gong.io` ordinarily redirects
  to `www.gong.io`, the exact case this whole tier exists to recover.
  Narrowed to hostname-only, with exactly one normalization (a leading
  "www." stripped from each side before comparing) — deliberately no
  broader than that: a different subdomain, a different domain, or a
  redirect to an unrelated origin is still always rejected.
"""

from __future__ import annotations

import re
from typing import NamedTuple, Optional

import httpx
from ats_scrapers.resolve import resolve_careers_url

# Order matters only for the request cap below — try the bare domain first
# (some companies point it straight at the ATS via a redirect), then the two
# conventional careers paths.
_CAREERS_PATHS = ("", "/careers", "/jobs")
_MAX_REQUESTS = 3

_URL_RE = re.compile(r"""https?://[^\s"'<>)]+""", re.IGNORECASE)

# darwinbox has no member in the installed ats_scrapers' ATSType enum (see
# providers/darwinbox.py's docstring — the real adapter only exists on that
# project's unreleased branch), so resolve_careers_url can never recognize a
# darwinbox link no matter how directly it's linked. Same tenant-hostname
# shape providers/darwinbox.py's own _resolve_tenant already parses.
#
# Captures an optional trailing path too (group 3) — darwinbox.com's own
# marketing site links its OWN non-tenant subdomains (explore., blog.,
# academy.) before any real tenant board in document order, so a bare
# host-only match picks the wrong one (measured live, 2026-08-11: darwinbox's
# own /careers page resolved to tenant "explore" and 403'd). Every real
# tenant board path observed (LeadSquared, Darwinbox's own dbx tenant)
# contains `/ms/candidate`; a marketing subdomain never does — see
# _resolve_from_html's use of this group as the discriminator.
_DARWINBOX_RE = re.compile(
    r"https?://([a-z0-9-]+)\.darwinbox\.(in|com)(/[^\s\"'<>)]*)?", re.IGNORECASE
)
_DARWINBOX_TENANT_PATH_HINT = "/ms/candidate"

# Greenhouse's own embed-widget script (`.../embed/job_board/js?for=<slug>`)
# is a real, working link to the tenant's board, but resolve_careers_url's
# upstream first-path-segment logic reads "embed" as the tenant, not the
# `for=` query param — measured live, 2026-08-11: CloudSEK and Observe.AI
# both link only this embed form, and boards.greenhouse.io/embed both
# resolve to a nonexistent "embed" tenant while boards.greenhouse.io/cloudsek
# and .../observeai are real, live boards. Recovered as a targeted
# correction (see _resolve_from_html), not a rewrite of the upstream
# library's own path-segment logic.
_GREENHOUSE_EMBED_FOR_RE = re.compile(r"[?&]for=([a-z0-9][a-z0-9._-]*)", re.IGNORECASE)

# zoho_recruit: same situation as darwinbox — no scraper in the installed
# ats_scrapers package and no reusable OSS alternative (research pass,
# 2026-08-11, see docs/ats-registry.md), so this project's own bespoke
# fetcher (providers/zoho_recruit.py) is the only path. Same tenant-hostname
# shape that module's own _resolve_tenant already parses.
_ZOHO_RECRUIT_RE = re.compile(r"https?://([a-z0-9-]+)\.zohorecruit\.(in|com)", re.IGNORECASE)

# Zoho Recruit career sites can run on a CUSTOM DOMAIN instead of the
# canonical *.zohorecruit.{in,com} tenant subdomain (a real, documented
# Zoho Recruit feature — verified live 2026-08-11: Zoho's own careers.
# zohocorp.com is exactly this case, and its public API returns
# ORG_NOT_FOUND when queried via the canonical careers.zohorecruit.com
# tenant path found embedded in the page's own RSS link, but succeeds when
# queried against the custom domain directly). This asset path is Zoho's
# OWN CDN, present on every real Zoho Recruit career site regardless of
# domain — verified on both a canonical tenant (bruntwork.zohorecruit.com)
# and the zohocorp.com custom domain — so it's a generic signature, not
# specific to any one company. See _resolve_company_ats_impl's use of this:
# it needs the fetched URL's own host, not a link found within the page,
# so it can't live in _resolve_from_html below.
#
# NOT sufficient on its own, though — Zoho Recruit has a SECOND, different
# deployment shape: a JS WIDGET embedded into a company's EXISTING page
# (Zoho's own "Embed jobs on your website" feature), which also loads this
# same CDN asset but does NOT mean the embedding page's own host is a
# queryable career site — verified live 2026-08-11: yellow.ai/careers
# embeds this exact widget, but yellow.ai itself has no public Job_Openings
# endpoint; the widget's own init call names the REAL site
# (careers.yellow.ai, which does). _ZOHO_RECRUIT_EMBED_SITE_RE below
# extracts that real target and MUST be checked first — the full
# page-builder site (zohocorp.com) never contains this embed-widget init
# call at all, so the two shapes are cleanly distinguishable.
_ZOHO_RECRUIT_CDN_SIGNATURE_RE = re.compile(r"static\.zohocdn\.com/recruit/", re.IGNORECASE)
_ZOHO_RECRUIT_EMBED_SITE_RE = re.compile(
    r"rec_embed_js\.load\(\{.*?\bsite\s*:\s*[\"']([^\"']+)[\"']", re.IGNORECASE | re.DOTALL
)

# Greenhouse's own API-only board host (distinct from the boards.*/job-
# boards.* hosts resolve_careers_url already recognizes) — verified live
# 2026-08-11: Noora Health's careers page links
# boards-api.greenhouse.io/v1/boards/noorahealth/jobs, a real (if
# currently empty) Greenhouse board resolve_careers_url's host table
# doesn't cover. Routed through the SAME greenhouse adapter, no new
# scraper — this only widens which URL shapes are recognized as greenhouse.
_GREENHOUSE_BOARDS_API_RE = re.compile(
    r"https?://boards-api\.greenhouse\.io/v1/boards/([a-z0-9][a-z0-9._-]*)/jobs", re.IGNORECASE
)

# Keka and Freshteam: recognized so a real, working link to either isn't
# silently discarded as "no detectable ATS" — verified live 2026-08-11,
# Jupiter (jupiter.keka.com) and Peoplebox (peoplebox.keka.com) both link
# real Keka tenants; Haptik (haptik.freshteam.com) links a real Freshteam
# tenant. Neither platform has a scraper in the installed ats_scrapers
# package (`ScraperRegistry.has_scraper` is False for both) — this does
# NOT add scraping support for either. It only lets `watchlist discover`
# correctly classify these as `pending_unsupported_ats` (evidence
# preserved, auto-promotable the moment a real adapter exists) instead of
# losing the company entirely as an indistinguishable "nothing found."
_KEKA_RE = re.compile(r"https?://([a-z0-9-]+)\.keka\.com", re.IGNORECASE)
_FRESHTEAM_RE = re.compile(r"https?://([a-z0-9-]+)\.freshteam\.com", re.IGNORECASE)

_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
    ),
}


class ResolvedAts(NamedTuple):
    ats: str
    slug: str
    matched_url: str  # the exact URL the ats/slug was read off — audit trail


def _resolve_from_html(html: str) -> Optional[tuple[str, str]]:
    """Every absolute URL in the page, fed through the same resolver a
    hand-supplied `--url` would use — first recognizable one wins. Order
    within the page is the only ranking signal; there's no reason to prefer
    one real match over another."""
    for match in _URL_RE.finditer(html):
        url = match.group(0)
        try:
            resolved = resolve_careers_url(url)
        except ValueError:
            # Real HTML regularly contains near-URL debris (JS template
            # literals, escaped JSON) the regex can't fully filter out —
            # e.g. a bracket sequence urlparse misreads as an IPv6 host.
            # Not a match, not a crash — same "unresolved" outcome as any
            # other non-ATS URL on the page.
            continue
        if resolved is None:
            boards_api = _GREENHOUSE_BOARDS_API_RE.match(url)
            if boards_api:
                return "greenhouse", boards_api.group(1).lower()
            continue
        ats, slug = resolved.ats.value, resolved.slug
        if ats == "greenhouse" and slug == "embed":
            # The upstream resolver read "embed" off /embed/job_board/js —
            # not a real tenant. The actual slug is in `?for=`; if it's not
            # there this specific URL isn't recoverable, so keep scanning
            # the rest of the page instead of returning the bogus slug.
            for_match = _GREENHOUSE_EMBED_FOR_RE.search(url)
            if for_match is None:
                continue
            slug = for_match.group(1).lower()
        return ats, slug
    for dbx in _DARWINBOX_RE.finditer(html):
        tenant, tld, path = dbx.group(1).lower(), dbx.group(2).lower(), dbx.group(3) or ""
        if _DARWINBOX_TENANT_PATH_HINT in path.lower():
            return "darwinbox", f"{tenant}.{tld}"
    zoho = _ZOHO_RECRUIT_RE.search(html)
    if zoho:
        tenant, tld = zoho.group(1).lower(), zoho.group(2).lower()
        return "zoho_recruit", f"{tenant}.{tld}"
    keka = _KEKA_RE.search(html)
    if keka:
        return "keka", keka.group(1).lower()
    freshteam = _FRESHTEAM_RE.search(html)
    if freshteam:
        return "freshteam", freshteam.group(1).lower()
    return None


def _same_host_ignoring_www(url_a: str, url_b: str) -> bool:
    """True if `url_a` and `url_b` have the same hostname once a single
    LEADING "www." is stripped from each -- e.g. "gong.io" and
    "www.gong.io" compare equal (an ordinary same-company redirect,
    verified live 2026-08-11: gong.io itself 301s to www.gong.io), but
    "gong.io" and "evil.example" do not, and neither does "gong.io" vs
    "app.gong.io" -- deliberately narrow, only the one specific "www."
    prefix is normalized, no other subdomain is treated as equivalent.
    Case-insensitive (hostnames are)."""
    def _bare_host(u: str) -> str:
        host = (httpx.URL(u).host or "").lower()
        return host[4:] if host.startswith("www.") else host

    return _bare_host(url_a) == _bare_host(url_b)


def _browser_available() -> bool:
    """Whether the optional `careeros[apply]` extra's Playwright Python
    package is importable. Does NOT confirm the `chromium` browser BINARY
    (`playwright install chromium`) is present -- a missing binary still
    surfaces as an ordinary launch failure via `_fetch_via_browser`'s
    `except Exception`. Same split `careeros/apply/browser.py`'s own
    `_playwright_installed` makes, duplicated rather than imported: two
    lines, not worth a shared module for."""
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return True


def _fetch_via_browser(url: str, timeout: float) -> Optional[str]:
    """Last-resort, browser-rendered fetch of `url` -- returns the fully
    rendered `page.content()` (HTML, for `_resolve_from_html` to parse),
    never raises, returns `None` on any failure (Playwright not installed,
    launch failure, navigation timeout, or a genuine crash).

    Mirrors `careeros/apply/browser.py`'s `_fetch_via_playwright` almost
    exactly -- same lazy import, same isolated single-use headless context,
    same bounded `wait_until="load"` + fixed settle buffer (see that
    module's docstring for why NOT `networkidle`). Deliberately NOT
    extracted into a shared module: the two call sites want different
    output (`page.content()`'s raw HTML here, to feed the existing
    `_resolve_from_html`; `page.inner_text("body")` there, for a human to
    read as form text) and the shared part is a handful of lines -- forcing
    a shared abstraction over that would cost more than it saves.

    SECURITY BOUNDARY (see module docstring): this function performs
    exactly ONE navigation, to exactly the `url` the caller passed in --
    never a second `page.goto`, never `page.click`, never inspects or acts
    on any link/URL discovered inside the rendered page. `wait_until="load"`
    may follow ordinary HTTP redirects for THAT one navigation (the same
    single-hop redirect behavior the HTTP tier's own
    `follow_redirects=True` already accepts) -- it never chains to a second,
    distinct page. The browser context is the default minimal one (no
    extra permissions, no downloads, no geolocation) and is closed
    immediately after this one read, bounding both navigation time
    (`timeout`) and total browser lifetime (opened and closed within this
    single call)."""
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
                if not _same_host_ignoring_www(page.url, url):
                    # SECURITY: the page ended up on a genuinely DIFFERENT
                    # host than the one CareerOS asked for -- a JS redirect
                    # (window.location, meta-refresh) executing during the
                    # settle window above, or a server-side redirect to an
                    # unrelated origin. Discard rather than parse content
                    # CareerOS never chose to fetch from -- see this
                    # function's SECURITY BOUNDARY docstring.
                    #
                    # Same-host-modulo-"www." is intentionally the only
                    # normalization: an ordinary same-company redirect
                    # (gong.io -> www.gong.io) is accepted, but nothing
                    # broader -- a different subdomain, a different
                    # domain, a different scheme+host entirely, is always
                    # rejected.
                    return None
                html = page.content()
            finally:
                browser.close()
    except Exception:
        return None

    return html or None


def _resolve_company_ats_impl(
    website: str, timeout: float, allow_browser_fallback: bool
) -> tuple[Optional[ResolvedAts], bool]:
    """Shared implementation behind `resolve_company_ats` and
    `resolve_company_ats_or_fetch_failure` below. Second element is True
    once at least one candidate path returned a real HTTP 200 (regardless
    of whether it contained a recognizable ATS link) — False only when
    EVERY attempt failed at the network level (timeout, DNS, connection
    error, or a non-200). This is the signal `watchlist discover` uses to
    tell "we looked and found nothing" (a real, 30-day-TTL-worthy
    unresolved) apart from "we couldn't even reach the site this run"
    (transient — see docs/ats-registry.md's Perfios finding: the same
    company resolved cleanly on one attempt and read-timed-out minutes
    later; a network hiccup must never poison a candidate for a month)."""
    website = website.strip()
    if not website:
        return None, False
    if "://" not in website:
        website = f"https://{website}"

    direct = resolve_careers_url(website)
    if direct is not None:
        return ResolvedAts(direct.ats.value, direct.slug, website), True

    base = website.rstrip("/")
    requests_made = 0
    fetched_ok = False
    # The sole, deterministic target for the browser tier below if every
    # HTTP attempt comes up empty. Prefers a real careers-page PATH
    # (/careers, /jobs) over the bare domain -- the bare domain redirecting
    # to a normal marketing homepage is a common false-positive-200 (e.g.
    # gong.io -> www.gong.io/ -- verified live 2026-08-11: rendering that
    # homepage finds nothing, since only /careers's own client-side fetch
    # ever calls the real ATS; the homepage was never going to have it,
    # rendered or not). Once a path-based candidate is set, it's never
    # overwritten by a later one -- `_CAREERS_PATHS`'s own ordering already
    # encodes which path is more likely the real careers page.
    browser_candidate_url: Optional[str] = None
    browser_candidate_is_specific = False
    try:
        with httpx.Client(timeout=timeout, headers=_DEFAULT_HEADERS, follow_redirects=True) as client:
            for path in _CAREERS_PATHS:
                if requests_made >= _MAX_REQUESTS:
                    break
                url = f"{base}{path}"
                requests_made += 1
                try:
                    resp = client.get(url)
                except httpx.HTTPError:
                    continue
                if resp.status_code != 200:
                    continue
                fetched_ok = True
                embed_site = _ZOHO_RECRUIT_EMBED_SITE_RE.search(resp.text)
                if embed_site:
                    # Checked FIRST: an embed-widget's own `site:` config
                    # names the real career site explicitly — the most
                    # direct signal available, and the only correct one
                    # when the current page is merely EMBEDDING a widget
                    # rather than being the career site itself (see
                    # _ZOHO_RECRUIT_EMBED_SITE_RE's docstring).
                    embed_host = httpx.URL(embed_site.group(1)).host
                    if embed_host:
                        return ResolvedAts("zoho_recruit", embed_host, url), True
                elif _ZOHO_RECRUIT_CDN_SIGNATURE_RE.search(resp.text):
                    # No embed-widget init call, but Zoho's own CDN asset
                    # is present -- the fetched page IS ITSELF the career
                    # site (page-builder rendered), so its own host is the
                    # correct target. Checked before the generic
                    # embedded-link scan below, deliberately: trusting an
                    # embedded *.zohorecruit.{in,com} link found WITHIN
                    # such a page can be wrong (Zoho's own careers.
                    # zohocorp.com embeds a canonical-domain RSS link,
                    # careers.zohorecruit.com, whose tenant does NOT
                    # resolve via the public API — ORG_NOT_FOUND — while
                    # the custom domain itself does). A page that isn't
                    # itself Zoho-Recruit-rendered at all (ordinary
                    # marketing copy merely linking OUT to an externally
                    # hosted board) never matches this signature, so the
                    # embedded-link scan below is unaffected for that, far
                    # more common, case.
                    host = httpx.URL(url).host
                    if host:
                        return ResolvedAts("zoho_recruit", host, url), True
                found = _resolve_from_html(resp.text)
                if found is not None:
                    ats, slug = found
                    return ResolvedAts(ats, slug, url), True
                if browser_candidate_url is None or (path and not browser_candidate_is_specific):
                    browser_candidate_url = url
                    browser_candidate_is_specific = bool(path)
    except httpx.HTTPError:
        return None, fetched_ok

    # LAST RESORT, strictly bounded: every deterministic HTTP path was
    # tried and none had a recognizable ATS link. If Playwright is
    # available, render the SAME url exactly once (never a URL discovered
    # during rendering, never a second navigation -- see
    # `_fetch_via_browser`'s docstring) and feed the result into the exact
    # same `_resolve_from_html` used above -- no second ATS parser. A
    # network-level exception above already returned before reaching here,
    # so this tier only ever runs after a genuine "fetched fine, found
    # nothing" outcome, never as a retry for a fetch failure.
    #
    # `allow_browser_fallback` is a SEPARATE gate from `_browser_available`,
    # off by default -- a real headless-browser launch is materially more
    # expensive than the HTTP tier, and a bulk `watchlist discover` run
    # over dozens of never-resolved candidates would silently get much
    # slower merely from having the optional `[apply]` extra installed for
    # an unrelated feature. Callers doing bulk/automated resolution should
    # leave this False (the default); a caller resolving ONE company a
    # human is actively waiting on can opt in explicitly.
    if allow_browser_fallback and browser_candidate_url is not None and _browser_available():
        rendered_html = _fetch_via_browser(browser_candidate_url, timeout)
        if rendered_html:
            found = _resolve_from_html(rendered_html)
            if found is not None:
                ats, slug = found
                return ResolvedAts(ats, slug, browser_candidate_url), True

    return None, fetched_ok


def resolve_company_ats(
    website: str, *, timeout: float = 10.0, allow_browser_fallback: bool = False
) -> Optional[ResolvedAts]:
    """Resolve a company's ATS from its own website or careers URL.

    1. If `website` is already a recognizable ATS URL (a `--url` a caller
       already had), resolve it directly — zero HTTP requests.
    2. Otherwise fetch up to `_MAX_REQUESTS` candidate careers paths off the
       company's own domain and look for an embedded ATS link.
    3. If every path above came back 200 with no recognizable link, AND
       `allow_browser_fallback=True`, AND the optional `careeros[apply]`
       Playwright extra is installed, render the one careers-page URL that
       fetched cleanly and look again — see `_fetch_via_browser`. Silently
       skipped (identical to today's behavior) when Playwright isn't
       installed OR `allow_browser_fallback` is left at its default False.

    `allow_browser_fallback` defaults to False deliberately: a real
    headless-browser launch is materially more expensive than the HTTP
    tier above, and callers doing bulk/automated resolution over many
    candidates (e.g. `watchlist discover`) should not silently pay that
    cost for every one that fails HTTP resolution just because `[apply]`
    happens to be installed for an unrelated feature. Pass `True` only for
    a single-company resolution a human is actively waiting on.

    Returns `None` on anything short of a genuine match — a fetch failure,
    a non-200, or a page with no recognizable ATS link. Never raises for a
    network problem; that's a "couldn't resolve," not an error, to every
    caller (discovery records it as unresolved, same as an unrecognized
    URL shape today). ATS-change recovery (`providers/ats_watchlist.py`) is
    this function's one current caller — it doesn't need the fetch-vs-no-
    match distinction `resolve_company_ats_or_fetch_failure` below
    provides, and does not currently opt into `allow_browser_fallback`."""
    return _resolve_company_ats_impl(website, timeout, allow_browser_fallback)[0]


def resolve_company_ats_or_fetch_failure(
    website: str, *, timeout: float = 10.0, allow_browser_fallback: bool = False
) -> tuple[Optional[ResolvedAts], bool]:
    """Same resolution as `resolve_company_ats` (see its docstring for the
    `allow_browser_fallback` default-False rationale), plus a `fetched_ok`
    flag: True once at least one candidate page was genuinely fetched (a
    real ATS-less page counts), False only when every attempt failed at
    the network level. `watchlist discover` -- this function's one current
    caller, and a bulk/automated one -- uses `fetched_ok` to give a
    transient network failure a short retry TTL instead of quietly reusing
    the normal 30-day "genuinely unresolved" one, and does NOT currently
    opt into `allow_browser_fallback`."""
    return _resolve_company_ats_impl(website, timeout, allow_browser_fallback)
