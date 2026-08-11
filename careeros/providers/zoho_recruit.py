"""A small deterministic Zoho Recruit career-site client — Zoho Recruit has
no scraper in the installed `ats_scrapers` package, and no OSS project was
found (research pass, 2026-08-11) that reliably scrapes it: the one
candidate targeting it (github.com/Himaanshuuuu04/Job_Scraper) uses
browser automation against generic/unverified CSS selectors, and Zoho's own
career sites render entirely client-side — the initial HTML carries zero
job data, so a static-HTML scraper returns nothing against a real tenant.

Replicates the mechanism Zoho's OWN career-site JavaScript uses instead,
found by inspecting that bundle directly and verified live with plain
`httpx` (already a dependency) against 6 independent real tenants across
both regional domains — `.zohorecruit.com` (Hannah Solar, BruntWork, OTSI
Global, WorkBetterNow, Talenture — US/Philippines/Nigeria/Costa Rica) and
`.zohorecruit.in` (APCER Life Sciences, India) — see docs/ats-registry.md
for the full investigation:

    GET https://{tenant}.zohorecruit.{in,com}/recruit/v2/public/Job_Openings?pagename=Careers

No auth, no cookies, no session — the same request an anonymous visitor's
browser makes to render the page. Same evidentiary standard as
`providers/darwinbox.py`'s precedent: undocumented by the vendor as a
public API, but genuinely what unauthenticated visitors are served. If
that ever stops being true for some tenant, this raises `ScraperError`
like any other adapter failure, never silently returns partial data.

MEASURED LIMITATION, not a bug: `Job_Description` and `Date_Opened` are
per-tenant-configurable career-site display fields, not always public —
verified live across the 6 tenants above, only 2 (OTSI Global,
WorkBetterNow) expose both; the other 4 expose neither. A tenant that
doesn't expose `Date_Opened` gets `posted_at: None` on every job (never
guessed from `fetched_at` or anything else), which `row_is_fresh()`
correctly drops — same "no description, no posted_at, so row_is_fresh()
drops it" outcome docs/ats-registry.md already recorded for Darwinbox's
own worse case. Title/company/location/apply-url/employment_type are
reliable regardless; whether a given tenant clears the freshness filter
is real, per-tenant variance, not something this module can control.

Scope note: only the default career-site page name ("Careers", what a
freshly-set-up Zoho Recruit career site uses) is tried. A tenant that
renamed its page is out of scope for now — same "fails the same way"
posture Darwinbox's module takes for its own unhandled edge cases, not a
silent guess.
"""

from __future__ import annotations

import html
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import httpx
from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError

_DEFAULT_TLD = "com"
_DEFAULT_PAGE_NAME = "Careers"
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
_TENANT_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")

_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}

# Small, local table (same pattern as darwinbox.py's own copy, kept local
# for the same reason: this module has no other dependency on ats_dataset.py's
# larger table) — covers the countries seen across the 6 real tenants this
# was verified against plus common near neighbors, not an exhaustive list.
_COUNTRY_ISO = {
    "india": "IN", "united states": "US", "united states of america": "US",
    "united kingdom": "GB", "canada": "CA", "australia": "AU",
    "philippines": "PH", "nigeria": "NG", "costa rica": "CR",
    "singapore": "SG", "united arab emirates": "AE",
    "germany": "DE", "france": "FR", "ireland": "IE",
}


def _resolve_tenant(slug: str) -> tuple[str, str]:
    """Same acceptance rules as darwinbox.py's own resolver: a bare slug, a
    slug+TLD suffix, or a full zohorecruit.{in,com} URL."""
    raw = slug.strip()
    if not raw:
        raise ScraperError("zoho_recruit slug must not be empty")
    if "://" in raw:
        host = (urlparse(raw).hostname or "").lower()
        match = re.fullmatch(rf"({_TENANT_RE.pattern})\.zohorecruit\.(in|com)", host)
        if not match:
            raise ScraperError(f"zoho_recruit slug must be a zohorecruit.{{in,com}} URL, got {raw!r}")
        return match.group(1), match.group(2)
    if raw.endswith(".com"):
        tenant, tld = raw[:-4], "com"
    elif raw.endswith(".in"):
        tenant, tld = raw[:-3], "in"
    else:
        tenant, tld = raw, _DEFAULT_TLD
    if not _TENANT_RE.fullmatch(tenant):
        raise ScraperError(f"zoho_recruit slug {raw!r} looks malformed; expected a DNS-safe subdomain")
    return tenant, tld


def _clean_text(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    text = _HTML_TAG_RE.sub(" ", html.unescape(value))
    return _WHITESPACE_RE.sub(" ", text).strip()[:25_000] or None


def _parse_posted_at(date_opened: Any) -> str | None:
    if not isinstance(date_opened, str) or not date_opened:
        return None
    try:
        return datetime.strptime(date_opened.strip(), "%m/%d/%Y").date().isoformat()
    except ValueError:
        return None  # unparseable -- never guess


def _format_location(item: dict[str, Any]) -> str | None:
    parts = [item.get("City"), item.get("State"), item.get("Country")]
    joined = ", ".join(p.strip() for p in parts if isinstance(p, str) and p.strip())
    return joined or None


def _parse_listing(item: dict[str, Any], tenant: str, base_url: str, company_name: str | None) -> dict[str, Any] | None:
    ats_id = str(item.get("id") or "").strip()
    title = item.get("Posting_Title") or item.get("Job_Opening_Name")
    title = title.strip() if isinstance(title, str) and title.strip() else None
    if not ats_id or not title:
        return None

    country = item.get("Country")
    country_iso = _COUNTRY_ISO.get(country.strip().casefold()) if isinstance(country, str) and country.strip() else None
    job_url = item.get("$url") or f"{base_url}/jobs/{_DEFAULT_PAGE_NAME}/{ats_id}"

    return {
        "url": job_url,
        "apply_url": job_url,
        "title": title,
        "company": company_name or tenant,
        "ats_type": "zoho_recruit",
        "ats_id": ats_id,
        "location": _format_location(item),
        "country_iso": country_iso,
        "is_remote": None,  # not exposed by this endpoint -- never guessed
        "department": item.get("Industry"),
        "description": _clean_text(item.get("Job_Description")),
        "employment_type": item.get("Job_Type"),
        "posted_at": _parse_posted_at(item.get("Date_Opened")),
        "fetched_at": datetime.now().isoformat(),
        "language": "en",
    }


_NOT_FOUND_STATUS = 403
_NOT_FOUND_CODE = "ORG_NOT_FOUND"


def fetch_zoho_recruit_jobs(slug: str, *, company_name: str | None = None, timeout: float = 30.0) -> list[dict[str, Any]]:
    """Fetch every published job for one Zoho Recruit tenant's default
    career-site page. Raises `CompanyNotFoundError` only for the ONE
    not-found shape actually verified live (HTTP 403 + JSON body
    `{"code": "ORG_NOT_FOUND"}` — seen from a tenant whose career-site page
    name wasn't the default "Careers"; Zoho does not use a plain 404 here).
    Every OTHER non-200 (a transient 5xx, a future 429, a 403 with some
    other code, or a non-JSON error body) raises `ScraperError` instead —
    deliberately narrow, not a generic "any 4xx is not-found" heuristic:
    misclassifying a transient failure as CompanyNotFoundError would count
    toward `ats_watchlist.py`'s consecutive-failures counter and could
    wrongly mark a real, still-existing company `stale`, exactly what
    `ScraperError`'s separate handling in that module exists to prevent.
    Same exception contract `ats_watchlist._scrape_entry`'s other callers
    already handle, so no new except-branch is needed there. A single
    unpaginated request: the public endpoint rejects `page`/`per_page`
    query params outright (`EXTRA_PARAM_FOUND`, verified live), so it
    always returns the tenant's complete published job list in one call."""
    tenant, tld = _resolve_tenant(slug)
    base_url = f"https://{tenant}.zohorecruit.{tld}"
    api_url = f"{base_url}/recruit/v2/public/Job_Openings"

    try:
        with httpx.Client(timeout=timeout, headers=_DEFAULT_HEADERS) as client:
            resp = client.get(api_url, params={"pagename": _DEFAULT_PAGE_NAME})
    except httpx.HTTPError as e:
        raise ScraperError(f"zoho_recruit request failed for tenant={tenant}: {e}") from e

    if resp.status_code != 200:
        error_code = None
        try:
            error_body = resp.json()
        except ValueError:
            error_body = None
        if isinstance(error_body, dict):
            error_code = error_body.get("code")
        if resp.status_code == _NOT_FOUND_STATUS and error_code == _NOT_FOUND_CODE:
            raise CompanyNotFoundError(f"zoho_recruit tenant/page not found: {tenant}")
        raise ScraperError(f"zoho_recruit HTTP {resp.status_code} for tenant={tenant}: {error_body!r}")
    try:
        payload = resp.json()
    except ValueError as e:
        raise ScraperError(f"zoho_recruit returned non-JSON for tenant={tenant}: {e}") from e
    if not isinstance(payload, dict) or payload.get("code") != "success":
        raise ScraperError(f"zoho_recruit API failure for tenant={tenant}: {payload!r}")

    jobs: list[dict[str, Any]] = []
    for item in payload.get("data") or []:
        if not isinstance(item, dict):
            continue
        row = _parse_listing(item, tenant, base_url, company_name)
        if row is not None:
            jobs.append(row)
    return jobs
