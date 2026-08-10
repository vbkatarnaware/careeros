"""Tests for careeros/providers/ats_watchlist.py — Layer 2A, the targeted
company watchlist provider. Offline by default: `_scrape_entry` is the one
function that touches `ats_scrapers` for a live fetch (same seam pattern as
ats_dataset.py's `_load_slice`), so every test here patches it directly
rather than needing the real package or network.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from careeros.config import Config
from careeros.models import Profile
from careeros.providers.ats_watchlist import (
    PROVIDER,
    WatchlistEntry,
    entry_key,
    load_watchlist,
)
from careeros.tests.conftest import requires_ats_scrapers


def _cfg(**overrides) -> Config:
    base = dict(threshold=4.0, consider_threshold=3.5, gate_batch_size=50, description_max_chars=4000)
    base.update(overrides)
    return Config(**base)


def _profile(**overrides) -> Profile:
    base = dict(
        version=1, candidate={}, headline="", targets=[], experience=[],
        role_priorities=["Product Manager"],
        work_mode_priority=["india_remote"],
        location={"onsite_ok": []},
        deal_breakers={"min_years_ok": 3},
    )
    base.update(overrides)
    return Profile(**base)


def _write_profile(tmp_path: Path, profile: Profile) -> None:
    careeros_dir = tmp_path / ".careeros"
    careeros_dir.mkdir(parents=True, exist_ok=True)
    with open(careeros_dir / "profile.yaml", "w") as f:
        yaml.safe_dump({k: v for k, v in profile.__dict__.items() if k != "experience"} | {"experience": []}, f)


def _write_watchlist(tmp_path: Path, companies: list[dict]) -> Path:
    careeros_dir = tmp_path / ".careeros"
    careeros_dir.mkdir(parents=True, exist_ok=True)
    path = careeros_dir / "watchlist.yaml"
    with open(path, "w") as f:
        yaml.safe_dump({"companies": companies}, f)
    return path


def _row(**overrides) -> dict:
    base = dict(
        title="Product Manager", company="Acme India",
        location="Bengaluru, India", country_iso="IN", region="APAC",
        is_remote=True, posted_at=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
        description="Own the roadmap. 3+ years of experience preferred.",
        apply_url="https://acme.example.com/jobs/1", url="https://acme.example.com/jobs/1",
        salary_min=None, salary_max=None, salary_currency=None, salary_period=None,
        employment_type="FullTime",
    )
    base.update(overrides)
    return base


# ── load_watchlist ───────────────────────────────────────────────────────

def test_load_watchlist_missing_file_returns_empty(tmp_path):
    assert load_watchlist(tmp_path / "watchlist.yaml") == []


def test_load_watchlist_parses_careers_url_entry(tmp_path):
    path = _write_watchlist(tmp_path, [{"name": "Acme", "careers_url": "https://jobs.lever.co/acme"}])
    entries = load_watchlist(path)
    assert entries == [WatchlistEntry(name="Acme", careers_url="https://jobs.lever.co/acme")]


def test_load_watchlist_parses_ats_slug_entry(tmp_path):
    path = _write_watchlist(tmp_path, [{"name": "Acme", "ats": "greenhouse", "slug": "acmeinc"}])
    entries = load_watchlist(path)
    assert entries[0].ats == "greenhouse"
    assert entries[0].slug == "acmeinc"


# ── validate ─────────────────────────────────────────────────────────────

@requires_ats_scrapers
def test_validate_ok_when_ats_scrapers_installed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert PROVIDER.validate(_cfg()) == []


@requires_ats_scrapers
def test_validate_missing_watchlist_file_is_not_a_problem(tmp_path, monkeypatch):
    # No watchlist.yaml at all — opt-in provider, zero entries is normal.
    monkeypatch.chdir(tmp_path)
    assert PROVIDER.validate(_cfg()) == []


# ── fetch: skip cases ────────────────────────────────────────────────────

@requires_ats_scrapers
def test_fetch_skips_when_watchlist_empty(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = PROVIDER.fetch(_cfg())
    assert result.skipped
    assert "no entries" in result.skip_reason


@requires_ats_scrapers
def test_fetch_skips_when_profile_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_watchlist(tmp_path, [{"name": "Acme", "ats": "greenhouse", "slug": "acme"}])
    result = PROVIDER.fetch(_cfg())
    assert result.skipped
    assert "profile.yaml" in result.skip_reason


# ── fetch: successful scrape + filter chain wiring ──────────────────────

@requires_ats_scrapers
def test_fetch_returns_filtered_rows(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_profile(tmp_path, _profile())
    _write_watchlist(tmp_path, [{"name": "Acme", "ats": "greenhouse", "slug": "acme"}])

    with patch(
        "careeros.providers.ats_watchlist._scrape_entry",
        return_value=("greenhouse", [_row()]),
    ):
        result = PROVIDER.fetch(_cfg())

    assert not result.skipped
    assert len(result.items) == 1
    assert result.items[0]["company"] == "Acme India"


@requires_ats_scrapers
def test_fetch_geo_filter_excludes_non_matching_row(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_profile(tmp_path, _profile(work_mode_priority=["india_remote"], location={"onsite_ok": []}))
    _write_watchlist(tmp_path, [{"name": "Acme", "ats": "greenhouse", "slug": "acme"}])

    us_row = _row(country_iso="US", location="Remote - US only")
    with patch("careeros.providers.ats_watchlist._scrape_entry", return_value=("greenhouse", [us_row])):
        result = PROVIDER.fetch(_cfg())

    assert result.items == []


@requires_ats_scrapers
def test_fetch_records_success_in_state_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_profile(tmp_path, _profile())
    _write_watchlist(tmp_path, [{"name": "Acme", "ats": "greenhouse", "slug": "acme"}])

    with patch("careeros.providers.ats_watchlist._scrape_entry", return_value=("greenhouse", [_row()])):
        PROVIDER.fetch(_cfg())

    key = entry_key(WatchlistEntry(name="Acme", ats="greenhouse", slug="acme"))
    with open(tmp_path / ".careeros" / "watchlist_state.json") as f:
        state = json.load(f)
    assert state[key]["verification_status"] == "live"
    assert state[key]["consecutive_failures"] == 0
    assert state[key]["ats"] == "greenhouse"


@requires_ats_scrapers
def test_fetch_warns_and_truncates_when_over_limit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_profile(tmp_path, _profile())
    _write_watchlist(tmp_path, [{"name": "Acme", "ats": "greenhouse", "slug": "acme"}])

    rows = [_row(posted_at=(datetime.now(timezone.utc) - timedelta(hours=i)).isoformat()) for i in range(5)]
    with patch("careeros.providers.ats_watchlist._scrape_entry", return_value=("greenhouse", rows)):
        result = PROVIDER.fetch(_cfg(), limit=2)

    assert len(result.items) == 2
    assert any("kept only the 2 most recent" in w for w in result.warnings)


# ── fetch: CompanyNotFoundError / ScraperError handling ─────────────────

@requires_ats_scrapers
def test_fetch_company_not_found_increments_consecutive_failures(tmp_path, monkeypatch):
    from ats_scrapers.exceptions import CompanyNotFoundError

    monkeypatch.chdir(tmp_path)
    _write_profile(tmp_path, _profile())
    _write_watchlist(tmp_path, [{"name": "Ghost Co", "ats": "greenhouse", "slug": "ghost"}])

    with patch("careeros.providers.ats_watchlist._scrape_entry", side_effect=CompanyNotFoundError("not found")):
        result = PROVIDER.fetch(_cfg())

    assert result.items == []
    key = entry_key(WatchlistEntry(name="Ghost Co", ats="greenhouse", slug="ghost"))
    with open(tmp_path / ".careeros" / "watchlist_state.json") as f:
        state = json.load(f)
    assert state[key]["consecutive_failures"] == 1
    assert "verification_status" not in state[key] or state[key].get("verification_status") != "stale"


@requires_ats_scrapers
def test_fetch_marks_stale_after_three_consecutive_not_found(tmp_path, monkeypatch):
    from ats_scrapers.exceptions import CompanyNotFoundError

    monkeypatch.chdir(tmp_path)
    _write_profile(tmp_path, _profile())
    _write_watchlist(tmp_path, [{"name": "Ghost Co", "ats": "greenhouse", "slug": "ghost"}])

    with patch("careeros.providers.ats_watchlist._scrape_entry", side_effect=CompanyNotFoundError("not found")):
        for _ in range(3):
            result = PROVIDER.fetch(_cfg())

    key = entry_key(WatchlistEntry(name="Ghost Co", ats="greenhouse", slug="ghost"))
    with open(tmp_path / ".careeros" / "watchlist_state.json") as f:
        state = json.load(f)
    assert state[key]["consecutive_failures"] == 3
    assert state[key]["verification_status"] == "stale"
    assert any("marked stale" in w for w in result.warnings)


@requires_ats_scrapers
def test_fetch_generic_scraper_error_produces_warning_not_crash(tmp_path, monkeypatch):
    from ats_scrapers.exceptions import ScraperError

    monkeypatch.chdir(tmp_path)
    _write_profile(tmp_path, _profile())
    _write_watchlist(tmp_path, [{"name": "Flaky Co", "ats": "greenhouse", "slug": "flaky"}])

    with patch("careeros.providers.ats_watchlist._scrape_entry", side_effect=ScraperError("500 error")):
        result = PROVIDER.fetch(_cfg())  # must not raise

    assert result.items == []
    assert any("Flaky Co" in w for w in result.warnings)
    key = entry_key(WatchlistEntry(name="Flaky Co", ats="greenhouse", slug="flaky"))
    with open(tmp_path / ".careeros" / "watchlist_state.json") as f:
        state = json.load(f)
    assert state[key]["verification_status"] == "temporary_error"
    assert state[key]["last_checked_at"]
    assert state[key]["consecutive_failures"] == 0  # transient, never counts toward "stale"


@requires_ats_scrapers
def test_fetch_classifies_429_as_rate_limited(tmp_path, monkeypatch):
    from ats_scrapers.exceptions import ScraperError

    monkeypatch.chdir(tmp_path)
    _write_profile(tmp_path, _profile())
    _write_watchlist(tmp_path, [{"name": "Busy Co", "ats": "greenhouse", "slug": "busy"}])

    with patch("careeros.providers.ats_watchlist._scrape_entry", side_effect=ScraperError("GreenhouseScraper: url returned 429")):
        PROVIDER.fetch(_cfg())

    key = entry_key(WatchlistEntry(name="Busy Co", ats="greenhouse", slug="busy"))
    with open(tmp_path / ".careeros" / "watchlist_state.json") as f:
        state = json.load(f)
    assert state[key]["verification_status"] == "rate_limited"


@requires_ats_scrapers
def test_fetch_no_scraper_for_ats_is_validation_failed_not_a_crash(tmp_path, monkeypatch):
    """keka has no scraper in ats-scrapers (unlike darwinbox, which has a
    bespoke replacement — see test_provider_darwinbox.py and
    test_scrape_entry_routes_darwinbox_to_bespoke_fetcher below) —
    `_scrape_entry` itself raises `WatchlistConfigError` for this case (a
    config problem, not a transient one); confirm the provider classifies
    it as validation_failed and surfaces a warning rather than propagating."""
    monkeypatch.chdir(tmp_path)
    _write_profile(tmp_path, _profile())
    _write_watchlist(tmp_path, [{"name": "Keka Co", "ats": "keka", "slug": "kekaco"}])

    result = PROVIDER.fetch(_cfg())  # real _scrape_entry, no mock — keka genuinely has no scraper

    assert result.items == []
    assert any("no scraper" in w for w in result.warnings)
    key = entry_key(WatchlistEntry(name="Keka Co", ats="keka", slug="kekaco"))
    with open(tmp_path / ".careeros" / "watchlist_state.json") as f:
        state = json.load(f)
    assert state[key]["verification_status"] == "validation_failed"
    assert state[key]["consecutive_failures"] == 0  # not the CompanyNotFoundError counter


@requires_ats_scrapers
def test_scrape_entry_routes_darwinbox_to_bespoke_fetcher(monkeypatch):
    """darwinbox has no ats-scrapers adapter but DOES have a bespoke plain-
    httpx replacement (careeros/providers/darwinbox.py) — `_scrape_entry`
    must route to it instead of raising WatchlistConfigError."""
    from careeros.providers.ats_watchlist import WatchlistEntry, _scrape_entry

    with patch(
        "careeros.providers.darwinbox.fetch_darwinbox_jobs",
        return_value=[{"title": "APM", "company": "acme"}],
    ) as mock_fetch:
        resolved_ats, rows = _scrape_entry(WatchlistEntry(name="Acme", ats="darwinbox", slug="acme"))

    mock_fetch.assert_called_once_with("acme", company_name="Acme")
    assert resolved_ats == "darwinbox"
    assert rows == [{"title": "APM", "company": "acme"}]


@requires_ats_scrapers
def test_fetch_malformed_entry_is_validation_failed(tmp_path, monkeypatch):
    """An entry with neither careers_url nor ats+slug can never resolve —
    real `_scrape_entry`, no mock."""
    monkeypatch.chdir(tmp_path)
    _write_profile(tmp_path, _profile())
    _write_watchlist(tmp_path, [{"name": "Broken Entry"}])

    result = PROVIDER.fetch(_cfg())

    assert result.items == []
    key = entry_key(WatchlistEntry(name="Broken Entry"))
    with open(tmp_path / ".careeros" / "watchlist_state.json") as f:
        state = json.load(f)
    assert state[key]["verification_status"] == "validation_failed"


# ── fetch: ATS migration detection ──────────────────────────────────────

@requires_ats_scrapers
def test_fetch_detects_ats_migration_and_appends_history(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_profile(tmp_path, _profile())
    _write_watchlist(tmp_path, [{"name": "Acme", "ats": "ashby", "slug": "acme"}])

    with patch("careeros.providers.ats_watchlist._scrape_entry", return_value=("ashby", [_row()])):
        PROVIDER.fetch(_cfg())  # first run: records ats=ashby

    with patch("careeros.providers.ats_watchlist._scrape_entry", return_value=("workday", [_row()])):
        result = PROVIDER.fetch(_cfg())  # second run: ats changed

    assert any("migration detected ashby -> workday" in w for w in result.warnings)
    key = entry_key(WatchlistEntry(name="Acme", ats="ashby", slug="acme"))
    with open(tmp_path / ".careeros" / "watchlist_state.json") as f:
        state = json.load(f)
    assert state[key]["ats"] == "workday"  # current value updated
    assert state[key]["history"] == [
        {"ats": "ashby", "detected_at": state[key]["last_checked_at"], "evidence": "ats changed on re-verify"}
    ]


@requires_ats_scrapers
def test_fetch_first_run_does_not_report_migration(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_profile(tmp_path, _profile())
    _write_watchlist(tmp_path, [{"name": "Acme", "ats": "ashby", "slug": "acme"}])

    with patch("careeros.providers.ats_watchlist._scrape_entry", return_value=("ashby", [_row()])):
        result = PROVIDER.fetch(_cfg())

    assert not any("migration" in w for w in result.warnings)


# ── multi-board: the core reason entry_key exists ────────────────────────

@requires_ats_scrapers
def test_fetch_multiple_boards_same_company_get_independent_state(tmp_path, monkeypatch):
    """Two boards for one company (e.g. a Workday tenant AND an Eightfold
    tenant) must not share one state slot, flip `ats` back and forth, or
    produce a false migration record."""
    monkeypatch.chdir(tmp_path)
    _write_profile(tmp_path, _profile())
    _write_watchlist(tmp_path, [
        {"name": "Acme", "ats": "workday", "slug": "acme-workday-tenant"},
        {"name": "Acme", "ats": "eightfold", "slug": "acme-eightfold-tenant"},
    ])

    with patch("careeros.providers.ats_watchlist._scrape_entry", side_effect=lambda entry: (entry.ats, [_row()])):
        result = PROVIDER.fetch(_cfg())

    assert not any("migration" in w for w in result.warnings)
    with open(tmp_path / ".careeros" / "watchlist_state.json") as f:
        state = json.load(f)
    assert len(state) == 2  # two independent slots, not one shared "Acme"
    workday_key = entry_key(WatchlistEntry(name="Acme", ats="workday", slug="acme-workday-tenant"))
    eightfold_key = entry_key(WatchlistEntry(name="Acme", ats="eightfold", slug="acme-eightfold-tenant"))
    assert state[workday_key]["ats"] == "workday"
    assert state[eightfold_key]["ats"] == "eightfold"


@requires_ats_scrapers
def test_fetch_multiple_boards_repeated_runs_do_not_flap(tmp_path, monkeypatch):
    """Re-running with the same two boards must never report a migration —
    each board's resolved ats is compared against its OWN prior state, not
    a state slot shared with the other board."""
    monkeypatch.chdir(tmp_path)
    _write_profile(tmp_path, _profile())
    _write_watchlist(tmp_path, [
        {"name": "Acme", "ats": "workday", "slug": "acme-workday-tenant"},
        {"name": "Acme", "ats": "eightfold", "slug": "acme-eightfold-tenant"},
    ])

    with patch("careeros.providers.ats_watchlist._scrape_entry", side_effect=lambda entry: (entry.ats, [_row()])):
        PROVIDER.fetch(_cfg())
        result = PROVIDER.fetch(_cfg())

    assert not any("migration" in w for w in result.warnings)


# ── to_job_dict ──────────────────────────────────────────────────────────

def test_to_job_dict_delegates_to_ats_dataset():
    from careeros.providers.ats_dataset import to_job_dict as dataset_to_job_dict

    raw = _row()
    assert PROVIDER.to_job_dict(raw) == dataset_to_job_dict(raw)
