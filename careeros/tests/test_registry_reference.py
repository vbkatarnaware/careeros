"""Tests for careeros/registry.py — the optional local reference-company
cache. `find_company` is pure/offline (reads a local CSV); `sync_reference`
is network and is tested with the manifest fetch + HTTP GET mocked, same
spirit as ats_dataset.py's `_load_slice` seam.
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from careeros.config import Config
from careeros.registry import find_company, load_meta, sync_reference
from careeros.tests.conftest import requires_ats_scrapers


def _cfg() -> Config:
    return Config(threshold=4.0, consider_threshold=3.5, gate_batch_size=50, description_max_chars=4000)


def _write_companies_csv(tmp_path, rows: list[dict]) -> None:
    registry_dir = tmp_path / ".careeros" / "registry"
    registry_dir.mkdir(parents=True, exist_ok=True)
    with open(registry_dir / "companies.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["ats", "name", "slug", "url"])
        writer.writeheader()
        writer.writerows(rows)


# ── find_company ─────────────────────────────────────────────────────────

def test_find_company_returns_empty_when_never_synced(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert find_company(_cfg(), "Acme") == []


def test_find_company_matches_by_normalized_name(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_companies_csv(tmp_path, [
        {"ats": "greenhouse", "name": "Acme, Inc.", "slug": "acme", "url": "https://boards.greenhouse.io/acme"},
    ])
    matches = find_company(_cfg(), "acme inc")
    assert len(matches) == 1
    assert matches[0]["ats"] == "greenhouse"


def test_find_company_matches_by_slug_substring(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_companies_csv(tmp_path, [
        {"ats": "lever", "name": "Some Other Name Ltd", "slug": "acme-hq", "url": "https://jobs.lever.co/acme-hq"},
    ])
    matches = find_company(_cfg(), "acme")
    assert len(matches) == 1


def test_find_company_ranks_exact_slug_match_first(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_companies_csv(tmp_path, [
        {"ats": "greenhouse", "name": "Acme Global", "slug": "acmeglobal", "url": "https://x"},
        {"ats": "lever", "name": "Acme", "slug": "acme", "url": "https://y"},
    ])
    matches = find_company(_cfg(), "acme")
    assert matches[0]["slug"] == "acme"


def test_find_company_empty_query_returns_empty(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_companies_csv(tmp_path, [{"ats": "greenhouse", "name": "Acme", "slug": "acme", "url": "https://x"}])
    assert find_company(_cfg(), "   ") == []


def test_find_company_respects_limit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_companies_csv(tmp_path, [
        {"ats": "greenhouse", "name": f"Acme {i}", "slug": f"acme-{i}", "url": "https://x"} for i in range(5)
    ])
    assert len(find_company(_cfg(), "acme", limit=2)) == 2


# ── sync_reference ────────────────────────────────────────────────────────

def _fake_manifest():
    manifest = MagicMock()
    manifest.generated_at = datetime(2026, 8, 7, 14, 30, tzinfo=timezone.utc)
    manifest.url_for_companies.return_value = "https://storage.stapply.ai/jobhive/v1/companies.csv"
    return manifest


@requires_ats_scrapers
def test_sync_reference_writes_csv_and_meta(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    csv_bytes = b"ats,name,slug,url\ngreenhouse,Acme,acme,https://x\n"

    fake_response = MagicMock()
    fake_response.content = csv_bytes
    fake_response.raise_for_status = MagicMock()

    with patch("ats_scrapers.manifest.Manifest.fetch", return_value=_fake_manifest()), \
         patch("httpx.get", return_value=fake_response):
        meta = sync_reference(_cfg())

    companies_path = tmp_path / ".careeros" / "registry" / "companies.csv"
    assert companies_path.read_bytes() == csv_bytes
    assert meta["row_count"] == 1
    assert meta["manifest_generated_at"] == "2026-08-07T14:30:00+00:00"
    assert "sha256" in meta and "imported_at" in meta and "last_synced_at" in meta


@requires_ats_scrapers
def test_sync_reference_preserves_original_imported_at_on_resync(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    csv_bytes = b"ats,name,slug,url\ngreenhouse,Acme,acme,https://x\n"
    fake_response = MagicMock()
    fake_response.content = csv_bytes
    fake_response.raise_for_status = MagicMock()

    with patch("ats_scrapers.manifest.Manifest.fetch", return_value=_fake_manifest()), \
         patch("httpx.get", return_value=fake_response):
        first = sync_reference(_cfg())
        second = sync_reference(_cfg())

    assert first["imported_at"] == second["imported_at"]
    assert second["last_synced_at"]  # refreshed each sync, not asserted equal


@requires_ats_scrapers
def test_sync_reference_skips_download_when_manifest_unchanged(tmp_path, monkeypatch):
    """The maintenance-step optimization this whole mechanism exists for:
    a second sync against an UNCHANGED upstream manifest must not
    re-download the 6.8MB CSV — only `last_synced_at` moves."""
    monkeypatch.chdir(tmp_path)
    csv_bytes = b"ats,name,slug,url\ngreenhouse,Acme,acme,https://x\n"
    fake_response = MagicMock()
    fake_response.content = csv_bytes
    fake_response.raise_for_status = MagicMock()
    manifest = _fake_manifest()  # same generated_at both calls

    with patch("ats_scrapers.manifest.Manifest.fetch", return_value=manifest), \
         patch("httpx.get", return_value=fake_response) as mock_get:
        sync_reference(_cfg())
        second = sync_reference(_cfg())

    mock_get.assert_called_once()  # NOT called a second time
    assert second["skipped"] is True
    assert second["row_count"] == 1  # preserved from the first real sync


@requires_ats_scrapers
def test_sync_reference_redownloads_when_manifest_changed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    old_bytes = b"ats,name,slug,url\ngreenhouse,Acme,acme,https://x\n"
    new_bytes = b"ats,name,slug,url\ngreenhouse,Acme,acme,https://x\nlever,Beta,beta,https://y\n"

    resp1 = MagicMock(content=old_bytes, raise_for_status=MagicMock())
    resp2 = MagicMock(content=new_bytes, raise_for_status=MagicMock())

    with patch("ats_scrapers.manifest.Manifest.fetch", return_value=_fake_manifest()), \
         patch("httpx.get", return_value=resp1):
        first = sync_reference(_cfg())

    newer_manifest = _fake_manifest()
    newer_manifest.generated_at = datetime(2026, 8, 9, tzinfo=timezone.utc)
    with patch("ats_scrapers.manifest.Manifest.fetch", return_value=newer_manifest), \
         patch("httpx.get", return_value=resp2) as mock_get:
        second = sync_reference(_cfg())

    mock_get.assert_called_once()
    assert first["row_count"] == 1
    assert second["row_count"] == 2
    assert second.get("skipped") is False


def test_load_meta_returns_none_when_never_synced(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert load_meta(_cfg()) is None


@requires_ats_scrapers
def test_load_meta_after_sync(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    fake_response = MagicMock()
    fake_response.content = b"ats,name,slug,url\n"
    fake_response.raise_for_status = MagicMock()

    with patch("ats_scrapers.manifest.Manifest.fetch", return_value=_fake_manifest()), \
         patch("httpx.get", return_value=fake_response):
        sync_reference(_cfg())

    meta = load_meta(_cfg())
    assert meta is not None
    assert meta["row_count"] == 0
