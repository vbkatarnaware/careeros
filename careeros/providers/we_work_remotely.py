"""Provider: We Work Remotely — free public RSS feed.

https://weworkremotely.com/remote-jobs.rss

A 100%-remote job board that publishes its full listing as a public RSS
feed — no API key, no auth, no rate-limit tier to configure. `validate()`
therefore always returns `[]` (nothing to misconfigure).

VERIFIED LIVE THIS SESSION (2026-07-10): `GET
https://weworkremotely.com/remote-jobs.rss` returns `<rss><channel>` with
~100 `<item>` elements. Each item's real, exact child tags: `title`
(format `"Company: Job Title"`), `region`, `country`, `state`, `skills`,
`category`, `type` (employment type as free text), `description` (real
rendered HTML — ElementTree already unescapes entities into a string
containing literal `<tag>` markup), `pubDate`/`expires_at` (RFC 822 date
strings), `guid`, `link` (WWR's own permalink page — same "board page as
the accepted apply_url" convention this codebase already uses for other
sources, e.g. LinkedIn/Wellfound slugs). There's also one namespaced
`<media:content>` per item (a logo image, `{http://search.yahoo.com/mrss}
content`) — not needed for job mapping, ignored.

Deliberately stdlib-only: parsed with `xml.etree.ElementTree`, NOT
`feedparser`/`beautifulsoup4` — this provider must not add a new
dependency to the project.

`title`'s `"Company: Job Title"` convention is split on the FIRST `": "`
occurrence — see `_split_title`. No server-side search/filtering exists on
this feed (verified: it's a single flat unparameterized RSS URL), so
`fetch()`'s `search`/`query` kwargs are accepted (so calls from the generic
`discover` loop don't crash) but silently ignored; `limit` is applied as a
client-side slice after parsing the full feed.
"""

from __future__ import annotations

import time as _time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import Any

import requests

from careeros.config import Config
from careeros.providers.base import ProviderError, ProviderResult

FEED_URL = "https://weworkremotely.com/remote-jobs.rss"
_TIMEOUT_S = 30.0
_USER_AGENT = "Mozilla/5.0 (compatible; CareerOS/1.2; +https://github.com/)"

# WWR's free-text `type` -> Job.employment_type enum. Case-insensitive.
_EMPLOYMENT_MAP = {
    "full-time": "full_time",
    "full time": "full_time",
    "part-time": "part_time",
    "part time": "part_time",
    "contract": "contract",
    "contractor": "contract",
    "freelance": "contract",
    "internship": "internship",
    "intern": "internship",
}

# The RSS item child tags we read into the raw record dict — a plain flat
# shape, one key per tag, `.text` value (empty string if the tag is present
# but empty, absent from the dict entirely if the tag itself is missing).
_ITEM_TAGS = (
    "title", "region", "country", "state", "skills", "category", "type",
    "description", "pubDate", "expires_at", "guid", "link",
)


def _parse_item(item: ET.Element) -> dict[str, Any]:
    """One `<item>` element -> a plain raw-record dict. Only the plain
    (non-namespaced) child tags listed in `_ITEM_TAGS` are read; the
    namespaced `<media:content>` logo element is ignored (not needed for
    job mapping)."""
    raw: dict[str, Any] = {}
    for tag in _ITEM_TAGS:
        child = item.find(tag)
        if child is not None:
            raw[tag] = child.text if child.text is not None else ""
    return raw


def _parse_feed(xml_text: str) -> list[dict[str, Any]]:
    """Parse the RSS feed body into a list of raw record dicts, one per
    `<item>`. stdlib `ElementTree` only — no new dependency."""
    root = ET.fromstring(xml_text)
    channel = root.find("channel")
    items = channel.findall("item") if channel is not None else root.findall(".//item")
    return [_parse_item(item) for item in items]


def _fetch_feed(url: str) -> str:
    """GET the feed body, raising a clean `ProviderError` on any network
    failure or non-200 response — mirrors (simplified) the pattern in
    fantastic_jobs.py's `_fetch_one_endpoint`: timeout/connection-error/
    other-request-exception/non-200 each get a clear, actionable message
    rather than a bare traceback."""
    try:
        resp = requests.get(url, headers={"User-Agent": _USER_AGENT}, timeout=_TIMEOUT_S)
    except requests.Timeout as e:
        raise ProviderError(
            "we-work-remotely: timed out reaching weworkremotely.com — this looks like a network "
            "or service outage, not a configuration problem. Retry in a few minutes."
        ) from e
    except requests.ConnectionError as e:
        raise ProviderError(
            "we-work-remotely: couldn't connect to weworkremotely.com — check your connection and "
            "retry later."
        ) from e
    except requests.RequestException as e:
        raise ProviderError(f"we-work-remotely: request failed — {e}") from e

    if resp.status_code != 200:
        raise ProviderError(
            f"we-work-remotely: HTTP {resp.status_code} fetching the RSS feed — {resp.text[:300]}"
        )
    return resp.text


def _split_title(title: str) -> tuple[str, str]:
    """"Company: Job Title" -> (company, title), split on the FIRST ": ".
    No ": " found -> (\"Unknown\", whole string)."""
    if ": " in title:
        company, _, rest = title.partition(": ")
        return company.strip() or "Unknown", rest.strip()
    return "Unknown", title.strip()


def _map_employment_type(raw_type: str | None) -> str | None:
    if not raw_type:
        return None
    return _EMPLOYMENT_MAP.get(raw_type.strip().lower())


def _posted_at(pub_date: str | None) -> str | None:
    """RFC 822 `pubDate` -> ISO string. Falls back to the raw string as-is
    if parsing fails, rather than raising; returns None if missing."""
    if not pub_date:
        return None
    try:
        return parsedate_to_datetime(pub_date).isoformat()
    except (TypeError, ValueError):
        return pub_date


class WeWorkRemotelyProvider:
    id = "we-work-remotely"

    def validate(self, config: Config) -> list[str]:
        """Free public feed, no credentials — nothing to misconfigure."""
        return []

    def fetch(
        self, config: Config, *, limit: int = 100, search: str = "",
        query: dict[str, Any] | None = None,
    ) -> ProviderResult:
        """Fetch and parse the full public RSS feed, then apply `limit` as a
        client-side slice (the feed has no server-side limit/search param).
        `search`/`query` are accepted so the generic `discover` loop can call
        every provider uniformly, but are not usable here — WWR's RSS
        doesn't support server-side filtering — and are silently ignored."""
        start = _time.time()
        xml_text = _fetch_feed(FEED_URL)
        items = _parse_feed(xml_text)
        if limit is not None and limit >= 0:
            items = items[:limit]
        return ProviderResult(
            provider=self.id, items=items, cost_usd=0.0,
            requests=1, records=len(items), seconds=_time.time() - start,
        )

    def to_job_dict(self, raw: dict[str, Any]) -> dict[str, Any] | None:
        raw_title = raw.get("title") or ""
        url = raw.get("link") or ""
        if not raw_title.strip() or not url.startswith("http"):
            return None

        company, title = _split_title(raw_title)
        region = (raw.get("region") or "").strip()
        description = raw.get("description")

        return {
            "title": title,
            "company": company,
            "apply_url": url,
            "description": description or None,
            "location": region or "Remote",
            "remote": True,  # WWR only lists remote jobs
            "employment_type": _map_employment_type(raw.get("type")),
            "seniority": None,
            "posted_at": _posted_at(raw.get("pubDate")),
            "salary": None,  # not exposed in the RSS feed
            "contact": None,  # not exposed
            "company_linkedin": None,  # not exposed
        }


PROVIDER = WeWorkRemotelyProvider()
