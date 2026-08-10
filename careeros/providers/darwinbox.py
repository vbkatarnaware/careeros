"""A small deterministic Darwinbox career-API client, standing in for
ats-scrapers' own DarwinboxScraper — verified 2026-08-10 to exist only on
that project's unreleased `main` branch (not in any published release,
including the version this project depends on), and to hardcode
`fetch_engine="cloak"` with no httpx fallback, which would require adding
`httpcloak` as a new dependency just to get past `import`.

Replicates the real, verified contract instead, with plain `httpx` (already
a dependency) and zero new packages:

    POST https://{tenant}.darwinbox.{in,com}/ms/candidateapi/job/alljobs
    body: {"companyId": "main", "page": N, "sort_option": "new", "limit": 10}

Live-tested 2026-08-10 against 10 real Darwinbox tenants (curefit, rapido,
purplle, mpl, pinelabs, juspay, classplus, stanzaliving, bewakoof, rupeek):
every one returned HTTP 200 over plain httpx, no Cloudflare block
encountered — see docs/ats-registry.md for the full audit. If that ever
stops being true for some tenant, this raises `ScraperError` like any other
adapter failure; it does not silently return partial data.

Field mapping mirrors the upstream (unreleased) `darwinbox.py` as closely as
plain-dict output allows, since `ats_watchlist.py`'s filter chain and
`ats_dataset.py`'s `to_job_dict` already operate on that exact row shape
(title/company/url/apply_url/location/country_iso/region/is_remote/
description/posted_at) — see those modules for why.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx
from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError

PAGE_SIZE = 10
MAX_PAGES = 500
_DEFAULT_TLD = "in"
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
_TENANT_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")

_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en",
    "Content-Type": "application/json",
    "X-Requested-With": "XMLHttpRequest",
}

# Same table shape as ats_dataset.py's _COUNTRY_ISO — kept local since this
# module has no other dependency on that one and the upstream scraper's own
# list is India-platform-specific (includes SA/QA/KW/OM Gulf markets
# ats_dataset.py's smaller table doesn't).
_COUNTRY_ISO = {
    "india": "IN", "united states": "US", "united states of america": "US",
    "united kingdom": "GB", "canada": "CA", "australia": "AU",
    "singapore": "SG", "united arab emirates": "AE", "saudi arabia": "SA",
    "qatar": "QA", "kuwait": "KW", "oman": "OM", "bahrain": "BH",
    "malaysia": "MY", "philippines": "PH", "indonesia": "ID", "nepal": "NP",
    "bangladesh": "BD", "sri lanka": "LK", "vietnam": "VN", "thailand": "TH",
    "hong kong": "HK", "china": "CN", "japan": "JP", "south korea": "KR",
    "germany": "DE", "france": "FR", "netherlands": "NL", "ireland": "IE",
    "spain": "ES", "italy": "IT", "poland": "PL", "sweden": "SE",
}


def _resolve_tenant(slug: str) -> tuple[str, str]:
    """Same acceptance rules as the upstream scraper: a bare slug, a
    slug+TLD suffix, or a full darwinbox.{in,com} URL."""
    raw = slug.strip()
    if not raw:
        raise ScraperError("darwinbox slug must not be empty")
    if "://" in raw:
        host = (urlparse(raw).hostname or "").lower()
        match = re.fullmatch(rf"({_TENANT_RE.pattern})\.darwinbox\.(in|com)", host)
        if not match:
            raise ScraperError(f"darwinbox slug must be a darwinbox.{{in,com}} URL, got {raw!r}")
        return match.group(1), match.group(2)
    if raw.endswith(".com"):
        tenant, tld = raw[:-4], "com"
    elif raw.endswith(".in"):
        tenant, tld = raw[:-3], "in"
    else:
        tenant, tld = raw, _DEFAULT_TLD
    if not _TENANT_RE.fullmatch(tenant):
        raise ScraperError(f"darwinbox slug {raw!r} looks malformed; expected a DNS-safe subdomain")
    return tenant, tld


def _html_to_text(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    text = _HTML_TAG_RE.sub(" ", html.unescape(value))
    return _WHITESPACE_RE.sub(" ", text).strip()[:25_000] or None


def _parse_posted_at(posted_on: Any, created_on: Any) -> str | None:
    try:
        ts = int(float(posted_on))
        if ts > 0:
            return datetime.fromtimestamp(ts, tz=UTC).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        pass
    if isinstance(created_on, str) and created_on:
        try:
            parsed = datetime.fromisoformat(created_on.replace("Z", "+00:00"))
        except ValueError:
            return None
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).isoformat()
    return None


def _format_location(item: dict[str, Any]) -> str | None:
    for key in ("tool_tip_locations", "officelocation_show_arr_list"):
        value = item.get(key)
        if isinstance(value, list):
            unique = list(dict.fromkeys(
                e.strip().rstrip(",") for e in value if isinstance(e, str) and e.strip()
            ))
            if unique:
                return "; ".join(unique)
    for key in ("locations", "officelocation_show_arr"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _parse_listing(item: dict[str, Any], tenant: str, tld: str, base_url: str, company_name: str | None) -> dict[str, Any] | None:
    ats_id = str(item.get("id") or "").strip()
    title = item.get("designation_display_name") or item.get("title") or item.get("designation_name")
    title = title.strip() if isinstance(title, str) and title.strip() else None
    if not ats_id or not title:
        return None

    country = item.get("country")
    country = country.strip() if isinstance(country, str) and country.strip() else None
    country_iso = _COUNTRY_ISO.get(country.casefold()) if country else None
    is_remote_raw = item.get("is_remote")
    job_url = f"{base_url}/ms/candidate/careers/{ats_id}"

    return {
        "url": job_url,
        "apply_url": job_url,
        "title": title,
        "company": company_name or tenant,
        "ats_type": "darwinbox",
        "ats_id": ats_id,
        "location": _format_location(item),
        "country_iso": country_iso,
        "is_remote": bool(is_remote_raw) if isinstance(is_remote_raw, (bool, int)) else None,
        "department": item.get("department_name") or item.get("department_name_only") or item.get("department"),
        "description": _html_to_text(item.get("jd")),
        "posted_at": _parse_posted_at(item.get("posted_on"), item.get("created_on")),
        "fetched_at": datetime.now(UTC).isoformat(),
        "language": "en",
    }


def fetch_darwinbox_jobs(slug: str, *, company_name: str | None = None, timeout: float = 30.0) -> list[dict[str, Any]]:
    """Fetch every open job for one Darwinbox tenant. Raises
    `CompanyNotFoundError` if the tenant subdomain itself doesn't resolve,
    `ScraperError` for any other request/response-shape failure — same
    exception contract `ats_watchlist._scrape_entry`'s other callers
    already handle, so no new except-branch is needed there."""
    tenant, tld = _resolve_tenant(slug)
    base_url = f"https://{tenant}.darwinbox.{tld}"
    api_url = f"{base_url}/ms/candidateapi/job/alljobs"
    headers = {
        **_DEFAULT_HEADERS,
        "Origin": base_url,
        "Referer": f"{base_url}/ms/candidatev2/main/careers/allJobs",
    }

    jobs: list[dict[str, Any]] = []
    seen: set[str] = set()
    with httpx.Client(timeout=timeout, headers=headers) as client:
        for page in range(1, MAX_PAGES + 1):
            try:
                resp = client.post(api_url, params={"companyId": "main"}, json={
                    "companyId": "main", "page": page, "sort_option": "new", "limit": PAGE_SIZE,
                })
            except httpx.HTTPError as e:
                raise ScraperError(f"darwinbox request failed for tenant={tenant}: {e}") from e

            if resp.status_code == 404:
                raise CompanyNotFoundError(f"darwinbox tenant not found: {tenant}")
            if resp.status_code != 200:
                raise ScraperError(f"darwinbox HTTP {resp.status_code} for tenant={tenant} page={page}")
            try:
                payload = resp.json()
            except ValueError as e:
                raise ScraperError(f"darwinbox returned non-JSON for tenant={tenant}: {e}") from e
            if not isinstance(payload, dict) or payload.get("status") != "success":
                raise ScraperError(f"darwinbox API failure for tenant={tenant} page={page}: {payload!r}")

            page_jobs = payload.get("data")
            if not isinstance(page_jobs, list) or not page_jobs:
                break
            for item in page_jobs:
                if not isinstance(item, dict):
                    continue
                row = _parse_listing(item, tenant, tld, base_url, company_name)
                if row is None or row["ats_id"] in seen:
                    continue
                seen.add(row["ats_id"])
                jobs.append(row)

            total = payload.get("job_counts")
            try:
                total = int(total) if total is not None else None
            except (TypeError, ValueError):
                total = None
            if total is not None and len(jobs) >= total:
                break
            if len(page_jobs) < PAGE_SIZE:
                break
        else:
            raise ScraperError(f"darwinbox pagination cap reached for tenant={tenant} ({MAX_PAGES} pages)")

    return jobs
