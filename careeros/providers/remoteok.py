"""Provider: RemoteOK — free public API (v1.2 multi-provider addition).

https://remoteok.com/api

FIELD NAMES + RESPONSE SHAPE VERIFIED LIVE this session against the real
`GET https://remoteok.com/api` endpoint. No auth, no API key — but RemoteOK
returns HTTP 403 without a `User-Agent` header, so `fetch()` always sends one.

Response shape (verified live): a bare JSON array. `items[0]` is NOT a job —
it's a legal-notice object (has a `"legal"` key and a `"last_updated"` key,
but no `"id"`/`"position"`). Every real job dict has an `"id"` key; the
legal-notice blob doesn't, so `fetch()` skips any item without one rather
than special-casing index 0 (more robust if RemoteOK ever reorders the
array).

Real job fields (verified live, exact keys): `id`, `slug`, `epoch`, `date`
(ISO 8601 posted-at timestamp), `company`, `company_logo`, `position` (job
title), `tags`, `description` (HTML string), `location` (often messy, e.g.
`"Kalgoorlie, "` with a trailing comma/space, or blank — RemoteOK is a
100%-remote board, so a missing/blank location maps to `"Remote"`),
`apply_url` / `url` (same value — RemoteOK's own job page), `salary_min` /
`salary_max` (`0` means "no salary data", NOT a real zero salary), `logo`.

Server-side search: RemoteOK's public API does NOT support a query/keyword
or location filter param — there is no server-side way to narrow the
result set. `fetch()` therefore ignores both the `search` and `query`
kwargs entirely (accepted only so a generic caller doesn't crash) and
always returns the full board; `pipeline/constraints.py` handles relevance
filtering downstream. This is a deliberate scope decision, not an
oversight — see the module contract in `careeros/providers/base.py`.

Cost model: free source. `cost_usd` is always 0.0; one HTTP call per
`fetch()`, so `requests=1`.
"""

from __future__ import annotations

from typing import Any

import requests

from careeros.config import Config
from careeros.providers._apify_common import (
    COMPANY_KEYS, DESCRIPTION_KEYS, TITLE_KEYS, URL_KEYS, pick_field,
)
from careeros.providers.base import ProviderError, ProviderResult

_API_URL = "https://remoteok.com/api"
_TIMEOUT_S = 30.0
_HEADERS = {"User-Agent": "Mozilla/5.0"}


def _fetch_raw() -> list[dict[str, Any]]:
    """One unauthenticated GET against RemoteOK's public API. Network/
    timeout/non-200 failures are surfaced as a clean `ProviderError`
    ("service outage, not a config problem"), mirroring the style of
    `fantastic_jobs.py`'s `_fetch_one_endpoint` — this provider needs no
    credentials, so there's no auth-error branch to classify."""
    try:
        resp = requests.get(_API_URL, headers=_HEADERS, timeout=_TIMEOUT_S)
    except requests.Timeout as e:
        raise ProviderError(
            "remoteok: timed out reaching RemoteOK — this looks like a network or "
            "service outage, not a configuration problem. Retry in a few minutes."
        ) from e
    except requests.ConnectionError as e:
        raise ProviderError(
            "remoteok: couldn't connect to RemoteOK — this looks like a network or "
            "service outage, not a configuration problem. Check your connection and "
            "retry later."
        ) from e
    except requests.RequestException as e:
        raise ProviderError(f"remoteok: request failed — {e}") from e

    if resp.status_code != 200:
        raise ProviderError(f"remoteok: HTTP {resp.status_code} — {resp.text[:300]}")

    items = resp.json()
    if not isinstance(items, list):
        raise ProviderError("remoteok: unexpected response shape (expected a JSON array)")
    return items


class RemoteOKProvider:
    id = "remoteok"

    def validate(self, config: Config) -> list[str]:
        """No credentials needed — free public API. Always OK."""
        return []

    def fetch(
        self, config: Config, *, limit: int = 100, search: str = "",
        query: dict[str, Any] | None = None,
    ) -> ProviderResult:
        """`search` and `query` are accepted but ignored — RemoteOK's public
        API has no server-side keyword/title/location filtering (see module
        docstring); this is a client-filtered source. `limit` is applied as
        a plain client-side slice after dropping the legal-notice item,
        since the API itself has no pagination or limit param."""
        import time as _time
        start = _time.time()

        raw_items = _fetch_raw()
        # items[0] is a legal-notice blob, not a job — every real job has an
        # "id" key, the legal notice doesn't, so filter on that rather than
        # assuming a fixed index.
        jobs = [item for item in raw_items if item.get("id")]
        jobs = jobs[: max(int(limit), 0)]

        return ProviderResult(
            provider=self.id, items=jobs, cost_usd=0.0,
            requests=1, records=len(jobs), seconds=_time.time() - start,
        )

    def to_job_dict(self, raw: dict[str, Any]) -> dict[str, Any] | None:
        title = pick_field(raw, TITLE_KEYS)
        url = pick_field(raw, URL_KEYS)
        if not title or not url.startswith("http"):
            return None

        return {
            "title": title,
            "company": pick_field(raw, COMPANY_KEYS, fallback="Unknown"),
            "apply_url": url,
            "description": pick_field(raw, DESCRIPTION_KEYS) or None,
            "location": _location_or_remote(raw.get("location")),
            "remote": True,  # RemoteOK is a 100%-remote job board by definition
            "employment_type": None,  # no clean employment-type enum exposed
            "seniority": None,
            "posted_at": _pick_posted(raw),
            "salary": _salary_from_fields(raw),
            "contact": None,  # not exposed
            "company_linkedin": None,  # not exposed
        }


def _location_or_remote(location: Any) -> str:
    if isinstance(location, str) and location.strip():
        return location.strip()
    return "Remote"


def _pick_posted(raw: dict[str, Any]) -> str | None:
    date = raw.get("date")
    if isinstance(date, str) and date.strip():
        return date.strip()
    return None


def _salary_from_fields(raw: dict[str, Any]) -> dict[str, Any] | None:
    """`salary_min`/`salary_max` are `0` when RemoteOK has no salary data for
    a posting — NOT a real zero salary — so both falsy/0 means "no data"
    (returns None, matching every other provider's "don't construct an
    all-empty salary dict" convention). RemoteOK salaries are USD/year by
    platform convention."""
    salary_min = raw.get("salary_min") or None
    salary_max = raw.get("salary_max") or None
    if not salary_min and not salary_max:
        return None
    return {"min": salary_min, "max": salary_max, "currency": "USD", "unit": "year"}


PROVIDER = RemoteOKProvider()
