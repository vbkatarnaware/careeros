"""Provider: ats-watchlist — Layer 2A, a small user-supplied list of
specific companies scraped directly via `ats-scrapers`' per-platform
adapters (https://github.com/kalil0321/ats-scrapers, MIT), as a companion
to the Layer 1 hosted snapshot (ats_dataset.py).

Layer 2A is TARGETED, not automated discovery. It only ever scrapes
companies the user explicitly named in `.careeros/watchlist.yaml`. This is
a deliberate scope line: Layer 2B (search-driven, "find me companies I
don't know about") is NOT built here — the project plan measured
`resolve_careers_url()` at 1/12 and static HTML fingerprinting at 1/6
against real missing-company careers pages (most are JS SPAs), which is
too low-yield to justify the added surface without stronger evidence. If
that evidence shows up later, 2B is a separate, explicitly-gated addition,
not something this module silently grows into.

What this buys over Layer 1: reach for companies NOT in the hosted
snapshot at all. It does NOT meaningfully improve freshness for companies
Layer 1 already carries — measured ~1% of a scraped company's postings
were newer than the snapshot for companies covered by both, so a watchlist
entry for an already-covered company is rarely worth adding.

Adapter contract (verified across greenhouse/lever/ashby/workday/taleo/
icims/bamboohr/recruitee/avature/personio/teamtailor/pinpoint/
smartrecruiters/gem/rippling): `company_slug` semantics are NOT uniform —
bare token, tenant subdomain, or full URL depending on platform. This
provider does not assume uniformity there: a watchlist entry supplies
EITHER `careers_url` (resolved via `get_scraper_for_url`, which derives
the correct slug shape for whichever platform matches) OR an explicit
`ats` + `slug` pair for custom-domain sites `resolve_careers_url` can't
recognize — in which case `slug` must already be in the shape that
platform's own scraper module expects. What IS uniform, and what this
provider actually relies on: every adapter funnels through the library's
shared `Fetcher`, which raises `CompanyNotFoundError` on a 404 and
`ScraperError` otherwise, regardless of platform.

Some ATS platforms this project's Layer 1 config enables have NO scraper
in this library — verified 2026-08-10: keka. (darwinbox WAS in this
category until the same date — see providers/darwinbox.py: the real
scraper exists only on ats-scrapers' unreleased `main` branch and hardcodes
a `fetch_engine` this project doesn't have installed, so this project ships
a small bespoke replica of the verified API contract instead, over plain
httpx.) An entry naming keka (or any other genuinely unsupported ats) is
rejected with a clear message at fetch time, never silently dropped.

AI policy: AI never proposes or writes an (ats, slug) mapping, and never
performs the resolution above. Every entry is either hand-written by the
user or derived deterministically from a URL the user supplied — see
AGENT_GUIDE.md's deterministic/reasoning boundary. ATS-migration detection
(a re-verify returning a different `ats` than last recorded) is likewise
purely deterministic: it appends to `history[]` rather than silently
overwriting, and never triggers an automatic rediscovery.
"""

from __future__ import annotations

import importlib.util
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from careeros.config import Config
from careeros.models import dumps
from careeros.providers.ats_dataset import (
    _DEFAULT_TITLE_EXCLUSIONS,
    _load_profile,
    _title_excluded,
    build_geo_spec,
    clean_row,
    row_is_fresh,
    row_matches_geo,
    seniority_excluded,
    years_exceed,
)
from careeros.providers.base import ProviderResult

_WATCHLIST_FILENAME = "watchlist.yaml"
STATE_FILENAME = "watchlist_state.json"
_DEAD_AFTER_CONSECUTIVE_FAILURES = 3
_DEFAULT_MAX_AGE_DAYS = 30


@dataclass
class WatchlistEntry:
    name: str
    ats: str | None = None
    slug: str | None = None
    careers_url: str | None = None
    source: str = "manual"
    added_at: str | None = None


def entry_key(entry: WatchlistEntry) -> str:
    """Composite state key so two boards for the same company (e.g. a
    Workday tenant AND an Eightfold tenant, or two Workday tenants) get
    INDEPENDENT state instead of sharing one `entry.name` slot — sharing
    one slot made every run flip `ats` back and forth between the two
    boards and append a false ATS-migration record each time. Built from
    the entry's own static YAML fields, so it's stable across runs
    regardless of what `_scrape_entry` resolves it to."""
    identity = entry.careers_url or f"{entry.ats}:{entry.slug}"
    return f"{entry.name}::{identity}"


def load_watchlist(path: Path) -> list[WatchlistEntry]:
    if not path.exists():
        return []
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    entries = []
    for raw in data.get("companies", []) or []:
        entries.append(WatchlistEntry(
            name=raw["name"], ats=raw.get("ats"), slug=raw.get("slug"),
            careers_url=raw.get("careers_url"), source=raw.get("source", "manual"),
            added_at=raw.get("added_at"),
        ))
    return entries


def append_watchlist_entry(path: Path, entry: dict[str, Any]) -> None:
    """Read-modify-write append, used only by `careeros watchlist add`
    (careeros/cli/registry_cmd.py) after live validation has already
    confirmed the entry resolves to real job data — this function itself
    does no validation, it just persists. Plain PyYAML round-trip:
    comments in a hand-edited `watchlist.yaml` are not preserved across an
    `add` call. Not fixed here — would need a new dependency
    (`ruamel.yaml`) for a file that's usually either fully hand-written or
    fully machine-appended-to, rarely both."""
    if path.exists():
        with open(path) as f:
            data = yaml.safe_load(f) or {}
    else:
        data = {}
    companies = data.get("companies") or []
    companies.append(entry)
    data["companies"] = companies
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)


def load_state(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(path: Path, state: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        f.write(dumps(state))


class WatchlistConfigError(Exception):
    """The watchlist ENTRY itself is unusable — missing `careers_url`/
    `ats`+`slug`, or the named `ats` has no scraper in this ats-scrapers
    version. Distinct from a network/upstream failure (`ScraperError`):
    retrying on a later run won't help until the entry is fixed by hand,
    so this classifies as `validation_failed`, never `temporary_error`."""


def _scrape_entry(entry: WatchlistEntry) -> tuple[str | None, list[dict[str, Any]]]:
    """The ONLY function that touches `ats_scrapers` for a live fetch —
    tests patch this, same seam pattern as ats_dataset.py's `_load_slice`.
    Deterministic resolution only; raises `WatchlistConfigError` (this
    entry can never resolve as configured), `CompanyNotFoundError` (a
    company not present on the named ATS), `MalformedJSONError` (the
    response wasn't shaped like real job data), or `ScraperError`
    (anything else, incl. network/HTTP failures) — callers classify all
    four into `verification_status`, never a crash. Returns the ATS
    actually resolved (for migration detection) and the jobs as plain
    dicts in the same row shape ats_dataset.py's filter chain and
    `to_job_dict` already operate on — the two schemas are deliberately
    identical, so this is a pydantic->JSON-safe-dict conversion, not a
    field remap."""
    from ats_scrapers import get_scraper_for_url
    from ats_scrapers.exceptions import ScraperError
    from ats_scrapers.scrapers import get_scraper
    from ats_scrapers.scrapers.base import ScraperRegistry

    # darwinbox has no scraper in the installed ats-scrapers version (only
    # on that project's unreleased main branch — see providers/darwinbox.py
    # and this module's docstring) — bespoke path, ats+slug only, no
    # careers_url resolution (get_scraper_for_url can't recognize a
    # platform it has no registered scraper for).
    if entry.ats == "darwinbox" and entry.slug:
        from careeros.providers.darwinbox import fetch_darwinbox_jobs
        rows = [clean_row(r) for r in fetch_darwinbox_jobs(entry.slug, company_name=entry.name)]
        return "darwinbox", rows

    if entry.careers_url:
        try:
            scraper = get_scraper_for_url(entry.careers_url)
        except ScraperError as e:
            # An unrecognized URL shape is a config problem, not a network
            # one — resolve_careers_url() raises this for any custom-domain
            # site it can't recognize, before any HTTP request is made.
            raise WatchlistConfigError(str(e)) from e
    elif entry.ats and entry.slug:
        if not ScraperRegistry.has_scraper(entry.ats):
            raise WatchlistConfigError(
                f"no scraper available for ats={entry.ats!r} in this version of "
                f"ats-scrapers (e.g. keka has none)"
            )
        scraper = get_scraper(entry.ats, entry.slug)
    else:
        raise WatchlistConfigError("watchlist entry needs either careers_url or (ats + slug)")

    jobs = scraper.fetch()
    resolved_ats = getattr(scraper, "ats", None)
    resolved_ats_value = resolved_ats.value if resolved_ats is not None else entry.ats
    rows = [clean_row(j.model_dump(mode="json")) for j in jobs]
    return resolved_ats_value, rows


class AtsWatchlistProvider:
    id = "ats-watchlist"

    def validate(self, config: Config) -> list[str]:
        # A missing/empty watchlist.yaml is NOT a problem — this provider is
        # opt-in per company; zero entries is a normal, common state.
        if importlib.util.find_spec("ats_scrapers") is None:
            return ['ats-scrapers not installed — run: pip install -e ".[ats-dataset]"']
        return []

    def fetch(
        self, config: Config, *, limit: int = 100, search: str = "",
        query: dict[str, Any] | None = None,
    ) -> ProviderResult:
        if importlib.util.find_spec("ats_scrapers") is None:
            return ProviderResult.skip(self.id, 'ats-scrapers not installed — run: pip install -e ".[ats-dataset]"')

        block = config.providers.get(self.id, {}) or {}
        watchlist_path = config.careeros_dir / block.get("watchlist_path", _WATCHLIST_FILENAME)
        state_path = config.careeros_dir / STATE_FILENAME
        max_age_days = block.get("max_age_days", _DEFAULT_MAX_AGE_DAYS)
        title_exclusions = block.get("title_exclusions", _DEFAULT_TITLE_EXCLUSIONS)

        entries = load_watchlist(watchlist_path)
        if not entries:
            return ProviderResult.skip(self.id, f"no entries in {watchlist_path} — targeted, opt-in source")

        try:
            profile = _load_profile(config)
        except (OSError, TypeError, KeyError) as e:
            return ProviderResult.skip(self.id, f"could not read .careeros/profile.yaml: {e}")

        includes = [r.lower() for r in (getattr(profile, "role_priorities", []) or [])]
        min_years_ok = (getattr(profile, "deal_breakers", {}) or {}).get("min_years_ok")
        geo = build_geo_spec(profile)
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)

        state = load_state(state_path)
        start = time.time()
        warnings: list[str] = []
        matched: list[dict[str, Any]] = []
        today = datetime.now(timezone.utc).date().isoformat()

        for entry in entries:
            from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
            from ats_scrapers.fetch import MalformedJSONError

            key = entry_key(entry)
            entry_state = state.setdefault(key, {"consecutive_failures": 0})
            entry_state["name"] = entry.name  # for display — key alone isn't human-friendly
            try:
                resolved_ats, rows = _scrape_entry(entry)
            except CompanyNotFoundError as e:
                # The only failure that can mark an entry "stale" — a
                # persistent 404 genuinely means "this mapping is wrong,"
                # unlike every other failure below.
                entry_state["consecutive_failures"] = entry_state.get("consecutive_failures", 0) + 1
                entry_state["last_checked_at"] = today
                if entry_state["consecutive_failures"] >= _DEAD_AFTER_CONSECUTIVE_FAILURES:
                    entry_state["verification_status"] = "stale"
                    warnings.append(
                        f"{entry.name}: not found {entry_state['consecutive_failures']}x in a "
                        f"row — marked stale in {state_path}, check the mapping"
                    )
                else:
                    warnings.append(f"{entry.name}: not found ({e})")
                continue
            except WatchlistConfigError as e:
                # Never transient — retrying won't help until the entry
                # itself is fixed by hand. Does NOT touch
                # consecutive_failures (that counter is CompanyNotFoundError-
                # specific): a bad config is a different failure mode than
                # "this company doesn't exist on this ATS."
                entry_state["verification_status"] = "validation_failed"
                entry_state["last_checked_at"] = today
                warnings.append(f"{entry.name}: invalid watchlist entry ({e})")
                continue
            except MalformedJSONError as e:
                entry_state["verification_status"] = "validation_failed"
                entry_state["last_checked_at"] = today
                warnings.append(f"{entry.name}: malformed response ({e})")
                continue
            except ScraperError as e:
                # Network/HTTP failure that survived the library's own
                # retry logic (fetch.py retries 403/429/5xx/timeouts before
                # raising). Per the "don't interpret temporary failure as
                # no jobs exist" rule: never touches consecutive_failures,
                # never marks stale — just records what happened and when,
                # so a persistently-erroring entry is visible in
                # `watchlist list` rather than silently "never checked."
                status = "rate_limited" if "429" in str(e) else "temporary_error"
                entry_state["verification_status"] = status
                entry_state["last_checked_at"] = today
                warnings.append(f"{entry.name}: {status} ({e})")
                continue

            entry_state["consecutive_failures"] = 0
            entry_state["verification_status"] = "live"
            entry_state["last_checked_at"] = today
            entry_state["last_ok_at"] = today
            entry_state["job_count"] = len(rows)
            previous_ats = entry_state.get("ats")
            if previous_ats and resolved_ats and previous_ats != resolved_ats:
                # ATS MIGRATION — never overwrite silently, append instead.
                history = entry_state.setdefault("history", [])
                history.append({"ats": previous_ats, "detected_at": today, "evidence": "ats changed on re-verify"})
                warnings.append(f"{entry.name}: ATS migration detected {previous_ats} -> {resolved_ats}")
            entry_state["ats"] = resolved_ats

            for row in rows:
                if not row_is_fresh(row, cutoff):
                    continue
                if _title_excluded(row.get("title"), title_exclusions):
                    continue
                if seniority_excluded(row.get("title")):
                    continue
                if min_years_ok is not None and years_exceed(row.get("description"), min_years_ok):
                    continue
                if includes and not any(r in (row.get("title") or "").lower() for r in includes):
                    continue
                if not row_matches_geo(row, geo):
                    continue
                matched.append(row)

        save_state(state_path, state)
        matched.sort(key=lambda r: r.get("posted_at") or "", reverse=True)
        items = matched[:limit]
        if len(matched) > limit:
            warnings.append(f"matched {len(matched)} jobs, kept only the {limit} most recent (limit={limit})")

        return ProviderResult(
            provider=self.id, items=items, cost_usd=0.0,
            requests=len(entries), records=len(items),
            seconds=time.time() - start, warnings=warnings,
        )

    def to_job_dict(self, raw: dict[str, Any]) -> dict[str, Any] | None:
        from careeros.providers.ats_dataset import to_job_dict as _dataset_to_job_dict
        return _dataset_to_job_dict(raw)


PROVIDER = AtsWatchlistProvider()
