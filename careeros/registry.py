"""The (optional) reference company registry — a local cache of the public
ats-scrapers company/tenant directory, used only to answer "do we already
know this company, and which ATS is it on?" before hand-adding it to
`.careeros/watchlist.yaml` (Layer 2A, see providers/ats_watchlist.py).

Deliberately NOT a hard dependency of the discovery engine: nothing in
`careeros discover` reads this module or its cache. It exists purely as a
`careeros registry sync` / `careeros registry find <name>` convenience —
an OSS user who never runs `sync` loses nothing but that lookup.

Format decision (measured, not assumed): the source is a 79,906-row,
4-column (`ats, name, slug, url`) CSV, ~6.8MB. Benchmarked 2026-08-10 —
stdlib `csv` parses it in ~0.19s with zero extra dependencies; Parquet
(via pandas+pyarrow) decodes in ~0.04s but needs both installed. A 0.15s
difference on a command a user runs occasionally is not a "meaningful
benefit" for a small lookup table, so this stays plain CSV — no pandas,
no pyarrow, portable, human-diffable. (Parquet remains the right call for
the multi-gigabyte JOBS dataset, which ats_dataset.py already reads via
the ats-scrapers library's own client — this module is unrelated to that.)

Honest limitation, also measured: the source CSV has no country or domain
column, so it cannot be filtered to "companies in India" or similar — only
exact/substring name lookups are possible. Don't build filtering logic
that pretends otherwise.
"""

from __future__ import annotations

import csv
import hashlib
import json
import time
from pathlib import Path
from typing import Any

from careeros.companies import normalize_name
from careeros.config import Config

_COMPANIES_FILENAME = "companies.csv"
_META_FILENAME = "reference_meta.json"


def _registry_dir(cfg: Config) -> Path:
    return cfg.careeros_dir / "registry"


def sync_reference(cfg: Config, *, manifest_url: str | None = None) -> dict[str, Any]:
    """Download the current companies.csv and cache it locally with
    provenance. Network call — never invoked automatically by discovery,
    only by the explicit `careeros registry sync` command (or a `daily`
    maintenance step that calls this same function).

    Always fetches the small manifest.json first (a few KB) to check
    `generated_at` against what's already cached — if unchanged, the 6.8MB
    CSV download is skipped and only `last_synced_at` is refreshed (we DID
    just confirm the cache is current, we just didn't re-download it).
    This is a REFERENCE-REGISTRY-ONLY skip — it has no bearing on whether
    `careeros discover` (the actual job pipeline) runs; that's a
    completely separate freshness domain, see this module's docstring."""
    from ats_scrapers.manifest import DEFAULT_MANIFEST_URL, Manifest

    manifest = Manifest.fetch(manifest_url or DEFAULT_MANIFEST_URL)
    csv_url = manifest.url_for_companies(prefer_parquet=False)
    upstream_generated_at = manifest.generated_at.isoformat()

    registry_dir = _registry_dir(cfg)
    existing = load_meta(cfg)
    if existing and existing.get("manifest_generated_at") == upstream_generated_at:
        existing = dict(existing)
        existing["last_synced_at"] = _now_iso()
        existing["skipped"] = True
        registry_dir.mkdir(parents=True, exist_ok=True)
        with open(registry_dir / _META_FILENAME, "w") as f:
            json.dump(existing, f, indent=2, sort_keys=True)
        return existing

    import httpx
    response = httpx.get(csv_url, timeout=120.0, follow_redirects=True)
    response.raise_for_status()
    content = response.content

    registry_dir.mkdir(parents=True, exist_ok=True)
    companies_path = registry_dir / _COMPANIES_FILENAME
    companies_path.write_bytes(content)

    row_count = sum(1 for _ in csv.reader(content.decode("utf-8").splitlines())) - 1  # minus header

    meta = {
        "source": csv_url,
        "manifest_generated_at": upstream_generated_at,
        "sha256": hashlib.sha256(content).hexdigest(),
        "row_count": row_count,
        "imported_at": meta_existing_imported_at(cfg) or _now_iso(),
        "last_synced_at": _now_iso(),
        "skipped": False,
    }
    with open(registry_dir / _META_FILENAME, "w") as f:
        json.dump(meta, f, indent=2, sort_keys=True)
    return meta


def meta_existing_imported_at(cfg: Config) -> str | None:
    meta_path = _registry_dir(cfg) / _META_FILENAME
    if not meta_path.exists():
        return None
    try:
        with open(meta_path) as f:
            return json.load(f).get("imported_at")
    except (OSError, json.JSONDecodeError):
        return None


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def load_meta(cfg: Config) -> dict[str, Any] | None:
    meta_path = _registry_dir(cfg) / _META_FILENAME
    if not meta_path.exists():
        return None
    with open(meta_path) as f:
        return json.load(f)


def find_company(cfg: Config, name: str, *, limit: int = 10) -> list[dict[str, str]]:
    """Case-insensitive, normalized substring match against the local
    cache. Returns [] (not an error) if `sync_reference` was never run —
    this is a convenience lookup, not a required step."""
    companies_path = _registry_dir(cfg) / _COMPANIES_FILENAME
    if not companies_path.exists():
        return []

    needle = normalize_name(name)
    if not needle:
        return []

    matches: list[dict[str, str]] = []
    with open(companies_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            row_key = normalize_name(row.get("name") or "")
            slug_key = (row.get("slug") or "").strip().lower()
            if needle in row_key or needle in slug_key:
                matches.append(row)

    def _rank(row: dict[str, str]) -> tuple[int, int]:
        row_key = normalize_name(row.get("name") or "")
        slug_key = (row.get("slug") or "").strip().lower()
        return (0 if slug_key == needle else 1, 0 if row_key == needle else 1)

    matches.sort(key=_rank)
    return matches[:limit]
