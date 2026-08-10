"""Tests for `careeros watchlist add` (careeros/cli/registry_cmd.py) — P0.12
user-provided ingestion. `_scrape_entry` is patched (same seam pattern as
test_provider_ats_watchlist.py) so nothing here makes a real network call;
`registry_mod.find_company` is patched to simulate reference-registry hits
without needing a synced 79,906-row cache on disk.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import yaml
from typer.testing import CliRunner

from careeros.cli import app
from careeros.providers.ats_watchlist import WatchlistConfigError, load_watchlist

runner = CliRunner()


def _read_watchlist(tmp_path):
    path = tmp_path / ".careeros" / "watchlist.yaml"
    if not path.exists():
        return []
    with open(path) as f:
        return (yaml.safe_load(f) or {}).get("companies", [])


def test_add_with_explicit_ats_slug_validates_and_writes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with patch("careeros.cli.registry_cmd.registry_mod.find_company", return_value=[]), \
         patch("careeros.cli.registry_cmd._scrape_entry", return_value=("smartrecruiters", [{"title": "PM"}] * 19)):
        result = runner.invoke(app, ["watchlist", "add", "Swiggy", "--ats", "smartrecruiters", "--slug", "swiggy"])

    assert result.exit_code == 0, result.output
    companies = _read_watchlist(tmp_path)
    assert len(companies) == 1
    assert companies[0]["name"] == "Swiggy"
    assert companies[0]["ats"] == "smartrecruiters"
    assert companies[0]["slug"] == "swiggy"
    assert companies[0]["source"] == "user_provided"
    assert "added_at" in companies[0]
    assert "19 live job(s)" in result.output


def test_add_company_not_found_writes_nothing(tmp_path, monkeypatch):
    from ats_scrapers.exceptions import CompanyNotFoundError

    monkeypatch.chdir(tmp_path)
    with patch("careeros.cli.registry_cmd.registry_mod.find_company", return_value=[]), \
         patch("careeros.cli.registry_cmd._scrape_entry", side_effect=CompanyNotFoundError("not found")):
        result = runner.invoke(app, ["watchlist", "add", "Ghost Co", "--ats", "greenhouse", "--slug", "ghost"])

    assert result.exit_code == 1
    assert "UNRESOLVED" in result.output
    assert _read_watchlist(tmp_path) == []


def test_add_validation_config_error_writes_nothing(tmp_path, monkeypatch):
    """e.g. the real darwinbox-has-no-scraper case."""
    monkeypatch.chdir(tmp_path)
    with patch("careeros.cli.registry_cmd.registry_mod.find_company", return_value=[]), \
         patch("careeros.cli.registry_cmd._scrape_entry", side_effect=WatchlistConfigError("no scraper for darwinbox")):
        result = runner.invoke(app, ["watchlist", "add", "Wakefit", "--ats", "darwinbox", "--slug", "wakefit"])

    assert result.exit_code == 1
    assert "UNRESOLVED" in result.output
    assert _read_watchlist(tmp_path) == []


def test_add_no_registry_match_and_no_explicit_source_is_unresolved(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with patch("careeros.cli.registry_cmd.registry_mod.find_company", return_value=[]):
        result = runner.invoke(app, ["watchlist", "add", "Totally Unknown Co"])

    assert result.exit_code == 1
    assert "UNRESOLVED" in result.output
    assert _read_watchlist(tmp_path) == []


def test_add_reuses_reference_registry_canonical_identity(tmp_path, monkeypatch):
    """Company identity rule: a company already in the reference registry
    must reuse its canonical name/ats/slug, never get a second identity —
    the WRITTEN entry must use the registry's spelling, not the user's raw
    (differently-cased) input."""
    monkeypatch.chdir(tmp_path)
    registry_match = [{"name": "Swiggy", "ats": "smartrecruiters", "slug": "swiggy", "url": "https://x"}]
    with patch("careeros.cli.registry_cmd.registry_mod.find_company", return_value=registry_match), \
         patch("careeros.cli.registry_cmd._scrape_entry", return_value=("smartrecruiters", [{"title": "PM"}])):
        result = runner.invoke(app, ["watchlist", "add", "swiggy inc"])  # different casing/spelling

    assert result.exit_code == 0, result.output
    companies = _read_watchlist(tmp_path)
    assert companies[0]["name"] == "Swiggy"  # canonical, not "swiggy inc"
    assert companies[0]["source"] == "reference_registry"


def test_add_skips_duplicate_by_normalized_name_and_same_ats(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    careeros_dir = tmp_path / ".careeros"
    careeros_dir.mkdir()
    with open(careeros_dir / "watchlist.yaml", "w") as f:
        yaml.safe_dump({"companies": [{"name": "Swiggy", "ats": "smartrecruiters", "slug": "swiggy"}]}, f)

    with patch("careeros.cli.registry_cmd.registry_mod.find_company", return_value=[]), \
         patch("careeros.cli.registry_cmd._scrape_entry") as mock_scrape:
        result = runner.invoke(app, ["watchlist", "add", "SWIGGY, Inc.", "--ats", "smartrecruiters", "--slug", "swiggy"])

    assert result.exit_code == 0, result.output
    assert "already tracked" in result.output
    mock_scrape.assert_not_called()  # never re-validates a known duplicate
    assert len(_read_watchlist(tmp_path)) == 1  # still just one entry


def test_add_allows_second_board_on_different_ats(tmp_path, monkeypatch):
    """Multi-board support: same company, different ATS, must NOT be
    treated as a duplicate — see entry_key's docstring."""
    monkeypatch.chdir(tmp_path)
    careeros_dir = tmp_path / ".careeros"
    careeros_dir.mkdir()
    with open(careeros_dir / "watchlist.yaml", "w") as f:
        yaml.safe_dump({"companies": [{"name": "Acme", "ats": "workday", "slug": "acme-workday"}]}, f)

    with patch("careeros.cli.registry_cmd.registry_mod.find_company", return_value=[]), \
         patch("careeros.cli.registry_cmd._scrape_entry", return_value=("eightfold", [{"title": "PM"}])):
        result = runner.invoke(app, ["watchlist", "add", "Acme", "--ats", "eightfold", "--slug", "acme-eightfold"])

    assert result.exit_code == 0, result.output
    companies = _read_watchlist(tmp_path)
    assert len(companies) == 2
    assert {c["ats"] for c in companies} == {"workday", "eightfold"}


def test_add_with_careers_url(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with patch("careeros.cli.registry_cmd.registry_mod.find_company", return_value=[]), \
         patch("careeros.cli.registry_cmd._scrape_entry", return_value=("greenhouse", [{"title": "PM"}] * 3)):
        result = runner.invoke(app, ["watchlist", "add", "Stripe", "--url", "https://boards.greenhouse.io/stripe"])

    assert result.exit_code == 0, result.output
    companies = _read_watchlist(tmp_path)
    assert companies[0]["careers_url"] == "https://boards.greenhouse.io/stripe"
    assert companies[0]["ats"] == "greenhouse"  # from _scrape_entry's resolved_ats, not guessed
