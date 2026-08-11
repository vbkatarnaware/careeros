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

Deliberately NOT a browser and NOT a search engine — plain `httpx` (already a
dependency), a handful of GET requests, and a regex over the HTML for
recognizable ATS URLs, reusing `ats_scrapers.resolve.resolve_careers_url`'s
own host table wholesale. No new host table, no Playwright, no JS execution —
a site that only reveals its ATS via client-side rendering is out of scope,
same boundary `resolve_careers_url` itself already draws. Never guesses: a
non-match returns `None`, exactly as `resolve_careers_url` does, so a caller
can never accidentally treat an unresolved company as resolved.
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
_DARWINBOX_RE = re.compile(r"https?://([a-z0-9-]+)\.darwinbox\.(in|com)", re.IGNORECASE)

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
        try:
            resolved = resolve_careers_url(match.group(0))
        except ValueError:
            # Real HTML regularly contains near-URL debris (JS template
            # literals, escaped JSON) the regex can't fully filter out —
            # e.g. a bracket sequence urlparse misreads as an IPv6 host.
            # Not a match, not a crash — same "unresolved" outcome as any
            # other non-ATS URL on the page.
            continue
        if resolved is not None:
            return resolved.ats.value, resolved.slug
    dbx = _DARWINBOX_RE.search(html)
    if dbx:
        tenant, tld = dbx.group(1).lower(), dbx.group(2).lower()
        return "darwinbox", f"{tenant}.{tld}"
    return None


def resolve_company_ats(website: str, *, timeout: float = 10.0) -> Optional[ResolvedAts]:
    """Resolve a company's ATS from its own website or careers URL.

    1. If `website` is already a recognizable ATS URL (a `--url` a caller
       already had), resolve it directly — zero HTTP requests.
    2. Otherwise fetch up to `_MAX_REQUESTS` candidate careers paths off the
       company's own domain and look for an embedded ATS link.

    Returns `None` on anything short of a genuine match — a fetch failure,
    a non-200, or a page with no recognizable ATS link. Never raises for a
    network problem; that's a "couldn't resolve," not an error, to every
    caller (discovery records it as unresolved, same as an unrecognized
    URL shape today)."""
    website = website.strip()
    if not website:
        return None
    if "://" not in website:
        website = f"https://{website}"

    direct = resolve_careers_url(website)
    if direct is not None:
        return ResolvedAts(direct.ats.value, direct.slug, website)

    base = website.rstrip("/")
    requests_made = 0
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
                found = _resolve_from_html(resp.text)
                if found is not None:
                    ats, slug = found
                    return ResolvedAts(ats, slug, url)
    except httpx.HTTPError:
        return None
    return None
