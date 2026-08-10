"""Tests for careeros/providers/ats_dataset.py's `_load_slice` disk cache.

Every `careeros discover` run was re-downloading ~2GB across 33 slices even
on days the upstream manifest hadn't changed (it publishes every few days,
not continuously). `_load_slice` now caches each slice under
`.careeros/dataset/<ats>-<generated_at>.parquet`, keyed by the manifest's
own `generated_at`. These tests mock the network boundary (`Manifest.fetch`,
`ats_scrapers.Client`) — nothing here touches the real dataset.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

pd = pytest.importorskip("pandas")

from careeros.providers.ats_dataset import _load_slice, _manifest_generated_at  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_manifest_cache():
    """`_manifest_generated_at` is process-lifetime `lru_cache`d — clear it
    between tests so each test's mocked manifest is actually consulted."""
    _manifest_generated_at.cache_clear()
    yield
    _manifest_generated_at.cache_clear()


def _fake_manifest(generated_at: datetime):
    manifest = MagicMock()
    manifest.generated_at = generated_at
    return manifest


def _fake_client(df: pd.DataFrame):
    client = MagicMock()
    client.load.return_value = df
    return client


def test_cache_miss_downloads_and_writes_cache(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    df = pd.DataFrame({"title": ["Product Manager"], "company": ["Acme"]})
    client = _fake_client(df)

    with patch("ats_scrapers.manifest.Manifest.fetch", return_value=_fake_manifest(datetime(2026, 8, 7, tzinfo=timezone.utc))), \
         patch("ats_scrapers.Client", return_value=client):
        result = _load_slice("greenhouse")

    assert result.equals(df)
    client.load.assert_called_once_with(ats="greenhouse")
    cached_files = list((tmp_path / ".careeros" / "dataset").glob("greenhouse-*.parquet"))
    assert len(cached_files) == 1


def test_cache_hit_skips_network_download(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    df = pd.DataFrame({"title": ["Product Manager"], "company": ["Acme"]})
    client = _fake_client(df)
    manifest = _fake_manifest(datetime(2026, 8, 7, tzinfo=timezone.utc))

    with patch("ats_scrapers.manifest.Manifest.fetch", return_value=manifest), \
         patch("ats_scrapers.Client", return_value=client):
        _load_slice("greenhouse")  # first call: cache miss, populates cache
        _manifest_generated_at.cache_clear()  # simulate a fresh process
        result = _load_slice("greenhouse")  # second call: should hit cache

    assert result.equals(df)
    client.load.assert_called_once()  # NOT called a second time


def test_new_manifest_generated_at_prunes_old_cache_and_redownloads(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    df1 = pd.DataFrame({"title": ["Old Job"]})
    df2 = pd.DataFrame({"title": ["New Job"]})

    with patch("ats_scrapers.manifest.Manifest.fetch", return_value=_fake_manifest(datetime(2026, 8, 7, tzinfo=timezone.utc))), \
         patch("ats_scrapers.Client", return_value=_fake_client(df1)):
        _load_slice("greenhouse")

    _manifest_generated_at.cache_clear()

    with patch("ats_scrapers.manifest.Manifest.fetch", return_value=_fake_manifest(datetime(2026, 8, 9, tzinfo=timezone.utc))), \
         patch("ats_scrapers.Client", return_value=_fake_client(df2)) as client_cls:
        result = _load_slice("greenhouse")

    assert result.equals(df2)
    cache_dir = tmp_path / ".careeros" / "dataset"
    cached_files = list(cache_dir.glob("greenhouse-*.parquet"))
    assert len(cached_files) == 1  # old one pruned, only the new key remains


def test_different_ats_have_independent_cache_entries(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    gh_df = pd.DataFrame({"title": ["GH Job"]})
    lever_df = pd.DataFrame({"title": ["Lever Job"]})
    manifest = _fake_manifest(datetime(2026, 8, 7, tzinfo=timezone.utc))

    with patch("ats_scrapers.manifest.Manifest.fetch", return_value=manifest):
        with patch("ats_scrapers.Client", return_value=_fake_client(gh_df)):
            _load_slice("greenhouse")
        _manifest_generated_at.cache_clear()
        with patch("ats_scrapers.manifest.Manifest.fetch", return_value=manifest), \
             patch("ats_scrapers.Client", return_value=_fake_client(lever_df)):
            _load_slice("lever")

    cache_dir = tmp_path / ".careeros" / "dataset"
    assert len(list(cache_dir.glob("greenhouse-*.parquet"))) == 1
    assert len(list(cache_dir.glob("lever-*.parquet"))) == 1


def test_manifest_fetch_failure_falls_through_to_live_load(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    df = pd.DataFrame({"title": ["Product Manager"]})
    client = _fake_client(df)

    with patch("ats_scrapers.manifest.Manifest.fetch", side_effect=RuntimeError("network down")), \
         patch("ats_scrapers.Client", return_value=client):
        result = _load_slice("greenhouse")  # must not raise

    assert result.equals(df)
    client.load.assert_called_once_with(ats="greenhouse")


def test_corrupt_cache_file_falls_through_to_fresh_download(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    df = pd.DataFrame({"title": ["Product Manager"]})
    client = _fake_client(df)
    manifest = _fake_manifest(datetime(2026, 8, 7, tzinfo=timezone.utc))

    cache_dir = tmp_path / ".careeros" / "dataset"
    cache_dir.mkdir(parents=True)
    (cache_dir / "greenhouse-20260807T000000.parquet").write_text("not a real parquet file")

    with patch("ats_scrapers.manifest.Manifest.fetch", return_value=manifest), \
         patch("ats_scrapers.Client", return_value=client):
        result = _load_slice("greenhouse")  # must not raise

    assert result.equals(df)
    client.load.assert_called_once()
