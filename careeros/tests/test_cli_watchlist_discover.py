"""Tests for `careeros watchlist discover` (careeros/cli/registry_cmd.py) —
the automatic, profile-driven counterpart to `watchlist add` (see
test_cli_watchlist_add.py for that command's tests; both share
`_resolve_via_registry`/`_is_duplicate`/`_append_entry`). Same offline
pattern: `_scrape_entry` and `resolve_company_ats` are patched so nothing
here makes a real network call.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

pytest.importorskip("ats_scrapers", reason=(
    "every test here invokes the real `watchlist discover` CLI command, "
    "whose body unconditionally imports ats_scrapers.exceptions/fetch "
    "regardless of whether _scrape_entry itself is mocked — see registry_cmd.py"
))

from typer.testing import CliRunner  # noqa: E402

from careeros.ats_resolve import ResolvedAts  # noqa: E402
from careeros.cli import app  # noqa: E402
from careeros.providers.ats_watchlist import WatchlistConfigError  # noqa: E402

runner = CliRunner()


def _write_profile(tmp_path: Path, role_priorities: tuple[str, ...] = ("Product Manager",)) -> None:
    careeros_dir = tmp_path / ".careeros"
    careeros_dir.mkdir(parents=True, exist_ok=True)
    profile = {
        "version": 1, "candidate": {}, "headline": "", "targets": [], "experience": [],
        "role_priorities": list(role_priorities),
    }
    with open(careeros_dir / "profile.yaml", "w") as f:
        yaml.safe_dump(profile, f)


def _write_config(tmp_path: Path, providers: dict) -> None:
    careeros_dir = tmp_path / ".careeros"
    careeros_dir.mkdir(parents=True, exist_ok=True)
    with open(careeros_dir / "config.yaml", "w") as f:
        yaml.safe_dump({"providers": providers}, f)


def _write_watchlist(tmp_path: Path, companies: list[dict]) -> None:
    careeros_dir = tmp_path / ".careeros"
    careeros_dir.mkdir(parents=True, exist_ok=True)
    with open(careeros_dir / "watchlist.yaml", "w") as f:
        yaml.safe_dump({"companies": companies}, f)


def _write_discovery_state(tmp_path: Path, state: dict) -> None:
    careeros_dir = tmp_path / ".careeros"
    careeros_dir.mkdir(parents=True, exist_ok=True)
    with open(careeros_dir / "discovery_candidates.json", "w") as f:
        json.dump(state, f)


def _read_watchlist(tmp_path: Path) -> list[dict]:
    path = tmp_path / ".careeros" / "watchlist.yaml"
    if not path.exists():
        return []
    with open(path) as f:
        return (yaml.safe_load(f) or {}).get("companies", [])


def _read_discovery_state(tmp_path: Path) -> dict:
    path = tmp_path / ".careeros" / "discovery_candidates.json"
    if not path.exists():
        return {}
    with open(path) as f:
        return json.load(f)


def _fresh_row(title: str = "Senior Product Manager", days_ago: int = 5) -> dict:
    return {
        "title": title,
        "posted_at": (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(),
    }


def _run(tmp_path, *args):
    return runner.invoke(app, ["watchlist", "discover", *args])


# ── deterministic skip gates ─────────────────────────────────────────────

def test_discover_registry_hit_is_advisory_not_a_skip(tmp_path, monkeypatch):
    """Regression: registry presence != Layer 1 job coverage (docs/
    ats-registry.md documents Swiggy as exactly this case — registered,
    but confirmed absent from its ATS's dataset slice). A registry hit
    must be reported, never silently skip live validation."""
    monkeypatch.chdir(tmp_path)
    _write_profile(tmp_path)
    registry_match = [{"name": "Swiggy", "ats": "smartrecruiters", "slug": "swiggy"}]
    with patch("careeros.cli.registry_cmd.registry_mod.find_company", return_value=registry_match), \
         patch("careeros.ats_resolve.resolve_company_ats", return_value=ResolvedAts("smartrecruiters", "swiggy", "https://swiggy.com")), \
         patch("careeros.cli.registry_cmd._scrape_entry", return_value=("smartrecruiters", [_fresh_row()])):
        result = _run(tmp_path, "--candidate", "Swiggy=https://swiggy.com")

    assert result.exit_code == 0, result.output
    assert "also present in the reference registry" in result.output
    assert "VALIDATED" in result.output  # fell through to live validation, not skipped
    companies = _read_watchlist(tmp_path)
    assert len(companies) == 1
    assert companies[0]["name"] == "Swiggy"


def test_discover_registry_substring_near_miss_is_not_treated_as_a_hit(tmp_path, monkeypatch):
    """Regression: the registry lookup is a SUBSTRING search (registry.py's
    own convenience-search behavior) and returns its best substring hit
    even with no exact match -- "Ramp" must not be treated as a registry
    hit on "Trampoline Systems"."""
    monkeypatch.chdir(tmp_path)
    _write_profile(tmp_path)
    registry_match = [{"name": "Trampoline Systems", "ats": "greenhouse", "slug": "trampoline"}]
    with patch("careeros.cli.registry_cmd.registry_mod.find_company", return_value=registry_match), \
         patch("careeros.ats_resolve.resolve_company_ats", return_value=ResolvedAts("ashby", "ramp", "https://ramp.com")), \
         patch("careeros.cli.registry_cmd._scrape_entry", return_value=("ashby", [_fresh_row()])):
        result = _run(tmp_path, "--candidate", "Ramp=https://ramp.com")

    assert result.exit_code == 0, result.output
    assert "also present in the reference registry" not in result.output
    companies = _read_watchlist(tmp_path)
    assert len(companies) == 1
    assert companies[0]["name"] == "Ramp"  # not renamed to the near-miss "Trampoline Systems"


def test_discover_skips_candidate_already_on_watchlist(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_profile(tmp_path)
    _write_watchlist(tmp_path, [{"name": "Acme Inc", "ats": "lever", "slug": "acme"}])
    with patch("careeros.cli.registry_cmd.registry_mod.find_company", return_value=[]):
        result = _run(tmp_path, "--candidate", "ACME, Inc.=https://acme.example")

    assert result.exit_code == 0, result.output
    assert "already on the watchlist" in result.output
    assert len(_read_watchlist(tmp_path)) == 1


def test_discover_skips_recently_unresolved_within_ttl(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_profile(tmp_path)
    recent = (datetime.now(timezone.utc) - timedelta(days=10)).date().isoformat()
    _write_discovery_state(tmp_path, {"acme": {"status": "unresolved", "last_checked_at": recent}})
    with patch("careeros.cli.registry_cmd.registry_mod.find_company", return_value=[]), \
         patch("careeros.ats_resolve.resolve_company_ats") as mock_resolve:
        result = _run(tmp_path, "--candidate", "Acme=https://acme.example")

    assert result.exit_code == 0, result.output
    assert "recently unresolved" in result.output
    mock_resolve.assert_not_called()


def test_discover_rechecks_after_ttl_expires(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_profile(tmp_path)
    stale = (datetime.now(timezone.utc) - timedelta(days=31)).date().isoformat()
    _write_discovery_state(tmp_path, {"acme": {"status": "unresolved", "last_checked_at": stale}})
    with patch("careeros.cli.registry_cmd.registry_mod.find_company", return_value=[]), \
         patch("careeros.ats_resolve.resolve_company_ats", return_value=None) as mock_resolve:
        result = _run(tmp_path, "--candidate", "Acme=https://acme.example")

    assert result.exit_code == 0, result.output
    mock_resolve.assert_called_once()  # TTL expired -> re-tried, not skipped on the stale record alone


# ── quality bar ───────────────────────────────────────────────────────────

def test_discover_adds_candidate_that_passes_quality_bar(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_profile(tmp_path)
    with patch("careeros.cli.registry_cmd.registry_mod.find_company", return_value=[]), \
         patch("careeros.ats_resolve.resolve_company_ats", return_value=ResolvedAts("lever", "acme", "https://acme.example")), \
         patch("careeros.cli.registry_cmd._scrape_entry", return_value=("lever", [_fresh_row()])):
        result = _run(tmp_path, "--candidate", "Acme=https://acme.example")

    assert result.exit_code == 0, result.output
    assert "VALIDATED" in result.output
    companies = _read_watchlist(tmp_path)
    assert len(companies) == 1
    assert companies[0]["ats"] == "lever"
    assert companies[0]["source"] == "auto_discovered"
    assert companies[0]["website"] == "https://acme.example"
    assert _read_discovery_state(tmp_path) == {}  # no longer a "candidate" once added


def test_discover_rejects_candidate_with_jobs_but_no_role_match(tmp_path, monkeypatch):
    """The Fi Money shape: real jobs exist, none match the profile's
    role_priorities within the freshness window."""
    monkeypatch.chdir(tmp_path)
    _write_profile(tmp_path, role_priorities=("Product Manager",))
    rows = [_fresh_row(title="Growth Marketing Lead"), _fresh_row(title="ML Engineer")]
    with patch("careeros.cli.registry_cmd.registry_mod.find_company", return_value=[]), \
         patch("careeros.ats_resolve.resolve_company_ats", return_value=ResolvedAts("lever", "fimoney", "https://fi.money")), \
         patch("careeros.cli.registry_cmd._scrape_entry", return_value=("lever", rows)):
        result = _run(tmp_path, "--candidate", "Fi Money=https://fi.money")

    assert result.exit_code == 0, result.output
    assert "none role-matching" in result.output
    assert _read_watchlist(tmp_path) == []
    state = _read_discovery_state(tmp_path)
    assert state["fi money"]["status"] == "unresolved"
    assert "role-matching" in state["fi money"]["reason"]


def test_discover_quality_bar_uses_configured_title_exclusions(tmp_path, monkeypatch):
    """Regression: the quality bar must read `providers.ats-watchlist.
    title_exclusions` from config -- the SAME block AtsWatchlistProvider.
    fetch() reads -- not the hardcoded default. A title excluded by the
    user's own config must not be able to admit a company, since that
    title can never survive the same config once scraped daily."""
    monkeypatch.chdir(tmp_path)
    _write_profile(tmp_path, role_priorities=("Product Manager",))
    _write_config(tmp_path, {"ats-watchlist": {"title_exclusions": ["product manager"]}})
    rows = [_fresh_row(title="Product Manager")]  # would pass the DEFAULT exclusion list
    with patch("careeros.cli.registry_cmd.registry_mod.find_company", return_value=[]), \
         patch("careeros.ats_resolve.resolve_company_ats", return_value=ResolvedAts("lever", "acme", "https://acme.example")), \
         patch("careeros.cli.registry_cmd._scrape_entry", return_value=("lever", rows)):
        result = _run(tmp_path, "--candidate", "Acme=https://acme.example")

    assert result.exit_code == 0, result.output
    assert "none role-matching" in result.output
    assert _read_watchlist(tmp_path) == []


def test_discover_records_unresolved_when_ats_undetectable(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_profile(tmp_path)
    with patch("careeros.cli.registry_cmd.registry_mod.find_company", return_value=[]), \
         patch("careeros.ats_resolve.resolve_company_ats", return_value=None):
        result = _run(tmp_path, "--candidate", "Ghost Co=https://ghost.example")

    assert result.exit_code == 0, result.output
    assert "UNRESOLVED" in result.output
    state = _read_discovery_state(tmp_path)
    assert state["ghost"]["status"] == "unresolved"
    assert state["ghost"]["reason"] == "no detectable ATS"


# ── --max-add ceiling ────────────────────────────────────────────────────

def test_discover_max_add_ceiling_stops_adding_but_still_reports(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_profile(tmp_path)

    def fake_resolve(website, **_):
        slug = website.split("//")[1].split(".")[0]
        return ResolvedAts("lever", slug, website)

    with patch("careeros.cli.registry_cmd.registry_mod.find_company", return_value=[]), \
         patch("careeros.ats_resolve.resolve_company_ats", side_effect=fake_resolve), \
         patch("careeros.cli.registry_cmd._scrape_entry", return_value=("lever", [_fresh_row()])):
        result = _run(
            tmp_path, "--max-add", "1",
            "--candidate", "Acme=https://acme.example",
            "--candidate", "Beta=https://beta.example",
        )

    assert result.exit_code == 0, result.output
    companies = _read_watchlist(tmp_path)
    assert len(companies) == 1  # ceiling respected
    assert "max-add 1 reached this run" in result.output
    assert "considered 2" in result.output
    assert "added 1" in result.output


# ── pending_unsupported_ats ───────────────────────────────────────────────

def test_discover_records_pending_unsupported_ats_when_no_adapter(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_profile(tmp_path)
    with patch("careeros.cli.registry_cmd.registry_mod.find_company", return_value=[]), \
         patch("careeros.ats_resolve.resolve_company_ats", return_value=ResolvedAts("keka", "acme", "https://acme.example")), \
         patch("careeros.cli.registry_cmd._has_adapter", return_value=False):
        result = _run(tmp_path, "--candidate", "Acme=https://acme.example")

    assert result.exit_code == 0, result.output
    assert "pending_unsupported_ats" in result.output
    assert _read_watchlist(tmp_path) == []
    state = _read_discovery_state(tmp_path)
    assert state["acme"] == {
        "status": "pending_unsupported_ats", "ats": "keka", "slug": "acme",
        "website": "https://acme.example", "last_checked_at": state["acme"]["last_checked_at"],
    }


def test_discover_auto_promotes_pending_unsupported_ats_once_adapter_exists(tmp_path, monkeypatch):
    """Zero re-fetch of the careers page — a raising resolve_company_ats
    proves the promotion path never calls it, only the cheap local
    `_has_adapter` check."""
    monkeypatch.chdir(tmp_path)
    _write_profile(tmp_path)
    today = datetime.now(timezone.utc).date().isoformat()
    _write_discovery_state(tmp_path, {
        "acme": {
            "status": "pending_unsupported_ats", "ats": "keka", "slug": "acme",
            "website": "https://acme.example", "last_checked_at": today,
        }
    })

    def _boom(*a, **k):
        raise AssertionError("should not re-fetch the careers page for a cheap adapter recheck")

    with patch("careeros.cli.registry_cmd.registry_mod.find_company", return_value=[]), \
         patch("careeros.ats_resolve.resolve_company_ats", side_effect=_boom), \
         patch("careeros.cli.registry_cmd._has_adapter", return_value=True), \
         patch("careeros.cli.registry_cmd._scrape_entry", return_value=("keka", [_fresh_row()])):
        result = _run(tmp_path, "--candidate", "Acme=https://acme.example")

    assert result.exit_code == 0, result.output
    companies = _read_watchlist(tmp_path)
    assert len(companies) == 1
    assert companies[0]["ats"] == "keka"
    assert _read_discovery_state(tmp_path) == {}


def test_discover_pending_unsupported_ats_skipped_within_ttl_when_still_unsupported(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_profile(tmp_path)
    recent = (datetime.now(timezone.utc) - timedelta(days=5)).date().isoformat()
    _write_discovery_state(tmp_path, {
        "acme": {
            "status": "pending_unsupported_ats", "ats": "keka", "slug": "acme",
            "website": "https://acme.example", "last_checked_at": recent,
        }
    })
    with patch("careeros.cli.registry_cmd.registry_mod.find_company", return_value=[]), \
         patch("careeros.cli.registry_cmd._has_adapter", return_value=False), \
         patch("careeros.ats_resolve.resolve_company_ats") as mock_resolve:
        result = _run(tmp_path, "--candidate", "Acme=https://acme.example")

    assert result.exit_code == 0, result.output
    assert "still unsupported" in result.output
    mock_resolve.assert_not_called()


# ── malformed --candidate ────────────────────────────────────────────────

def test_discover_skips_malformed_candidate_without_crashing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_profile(tmp_path)
    with patch("careeros.cli.registry_cmd.registry_mod.find_company", return_value=[]):
        result = _run(tmp_path, "--candidate", "no-equals-sign-here")

    assert result.exit_code == 0, result.output
    assert "skipping malformed" in result.output
    assert "considered 0" in result.output
    assert _read_watchlist(tmp_path) == []


# ── watchlist list surfaces parked discovery candidates ──────────────────

def test_watchlist_list_surfaces_pending_unsupported_ats(tmp_path, monkeypatch):
    """Regression: a pending_unsupported_ats candidate must be visible in
    `watchlist list`, not only in discovery_candidates.json on disk --
    the feature's whole promise is "we kept it for you"."""
    monkeypatch.chdir(tmp_path)
    _write_discovery_state(tmp_path, {
        "acme": {
            "status": "pending_unsupported_ats", "ats": "keka", "slug": "acme",
            "website": "https://acme.example", "last_checked_at": "2026-08-11",
        }
    })
    result = runner.invoke(app, ["watchlist", "list"])

    assert result.exit_code == 0, result.output
    assert "pending_unsupported_ats" in result.output
    assert "keka/acme" in result.output


def test_watchlist_list_empty_watchlist_and_no_candidates_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["watchlist", "list"])

    assert result.exit_code == 0, result.output
    assert "empty or missing" in result.output
    assert "pending_unsupported_ats" not in result.output
