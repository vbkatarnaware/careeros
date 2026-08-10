"""Tests for `careeros doctor --live` (careeros/cli/'s
`_run_doctor_live_checks`) — the 2026-07-12 fix for a real incident: quota
was only ever a locally stored/calculated guess (a Monday-reset counter
independent of which API key was configured), so replacing an exhausted
Fantastic Jobs key still reported "quota exhausted" with no live
verification. `--live` actually reaches each enabled provider's real API.
No real network calls in these tests: `requests.get` is mocked, matching
this repo's existing test patterns."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from careeros.cli import _CheckStatus, _run_doctor_live_checks
from careeros.config import Config


def _cfg(**overrides) -> Config:
    defaults = dict(
        threshold=4.0, consider_threshold=3.5,
        gate_batch_size=50, description_max_chars=4000,
        goals={}, prompts={},
        sheets={}, api={"transport": "direct"}, fx_rates={},
        drive={"enabled": False},
        providers={"fantastic-jobs": {"enabled": True}},
    )
    defaults.update(overrides)
    return Config(**defaults)


def _status_for(results, label_substr):
    for status, label, detail in results:
        if label_substr in label:
            return status, detail
    return None, None


def _fj_resp(status_code=200, headers=None, json_body=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = headers or {}
    resp.json.return_value = json_body if json_body is not None else []
    return resp


def test_live_fj_check_reports_real_remaining_quota(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FANTASTIC_API_KEY", "fake-key")
    cfg = _cfg()

    resp = _fj_resp(headers={"x-ratelimit-requests-remaining": "99", "x-ratelimit-jobs-remaining": "480"})
    with patch("requests.get", return_value=resp):
        results = _run_doctor_live_checks(cfg)

    status, detail = _status_for(results, "Fantastic Jobs (LIVE)")
    assert status == _CheckStatus.PASS
    assert "requests_remaining=99" in detail
    assert "jobs_remaining=480" in detail


def test_live_fj_check_fails_on_rejected_key(tmp_path, monkeypatch):
    """This is the exact incident: a live check must actually SURFACE a real
    rejection instead of silently trusting stale local state."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FANTASTIC_API_KEY", "fake-key")
    cfg = _cfg()

    resp = _fj_resp(status_code=401)
    with patch("requests.get", return_value=resp):
        results = _run_doctor_live_checks(cfg)

    status, detail = _status_for(results, "Fantastic Jobs (LIVE)")
    assert status == _CheckStatus.FAIL
    assert "API key rejected" in detail


# ── upstream manifest / unconfigured-ATS-source warning (2026-08-10) ────

def _ats_cfg(configured_ats, tmp_path):
    return Config(
        threshold=4.0, consider_threshold=3.5,
        gate_batch_size=50, description_max_chars=4000,
        goals={}, prompts={},
        sheets={}, api={}, fx_rates={}, drive={"enabled": False},
        providers={"ats-dataset": {"enabled": True, "ats": configured_ats}},
    )


def _fake_manifest(generated_at, by_ats_keys):
    manifest = MagicMock()
    manifest.generated_at = generated_at
    manifest.by_ats = {k: MagicMock() for k in by_ats_keys}
    return manifest


def test_live_manifest_check_reachable_with_no_local_cache(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    monkeypatch.chdir(tmp_path)
    cfg = _ats_cfg(["greenhouse", "lever"], tmp_path)
    manifest = _fake_manifest(datetime(2026, 8, 7, 14, 30, tzinfo=timezone.utc), ["greenhouse", "lever"])

    with patch("ats_scrapers.manifest.Manifest.fetch", return_value=manifest):
        results = _run_doctor_live_checks(cfg)

    status, detail = _status_for(results, "Upstream manifest (LIVE)")
    assert status == _CheckStatus.PASS
    assert "no local cache yet" in detail


def test_live_manifest_check_flags_stale_local_cache(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    monkeypatch.chdir(tmp_path)
    dataset_dir = tmp_path / ".careeros" / "dataset"
    dataset_dir.mkdir(parents=True)
    old = datetime.now(timezone.utc) - timedelta(days=10)
    (dataset_dir / f"greenhouse-{old.strftime('%Y%m%dT%H%M%S')}.parquet").write_bytes(b"fake")

    cfg = _ats_cfg(["greenhouse"], tmp_path)
    manifest = _fake_manifest(datetime.now(timezone.utc), ["greenhouse"])

    with patch("ats_scrapers.manifest.Manifest.fetch", return_value=manifest):
        results = _run_doctor_live_checks(cfg)

    status, detail = _status_for(results, "Upstream manifest (LIVE)")
    assert status == _CheckStatus.WARN
    assert "days behind" in detail


def test_live_manifest_check_current_when_dates_match(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    monkeypatch.chdir(tmp_path)
    now = datetime.now(timezone.utc)
    dataset_dir = tmp_path / ".careeros" / "dataset"
    dataset_dir.mkdir(parents=True)
    (dataset_dir / f"greenhouse-{now.strftime('%Y%m%dT%H%M%S')}.parquet").write_bytes(b"fake")

    cfg = _ats_cfg(["greenhouse"], tmp_path)
    manifest = _fake_manifest(now, ["greenhouse"])

    with patch("ats_scrapers.manifest.Manifest.fetch", return_value=manifest):
        results = _run_doctor_live_checks(cfg)

    status, detail = _status_for(results, "Upstream manifest (LIVE)")
    assert status == _CheckStatus.PASS
    assert "current" in detail


def test_live_check_warns_on_unconfigured_upstream_sources(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    monkeypatch.chdir(tmp_path)
    cfg = _ats_cfg(["greenhouse"], tmp_path)  # only 1 of 3 upstream sources
    manifest = _fake_manifest(datetime.now(timezone.utc), ["greenhouse", "lever", "ashby"])

    with patch("ats_scrapers.manifest.Manifest.fetch", return_value=manifest):
        results = _run_doctor_live_checks(cfg)

    status, detail = _status_for(results, "Unconfigured upstream ATS sources")
    assert status == _CheckStatus.WARN
    assert "lever" in detail and "ashby" in detail
    assert "not auto-enabled" in detail


def test_live_check_passes_when_all_upstream_sources_configured(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    monkeypatch.chdir(tmp_path)
    cfg = _ats_cfg(["greenhouse", "lever"], tmp_path)
    manifest = _fake_manifest(datetime.now(timezone.utc), ["greenhouse", "lever"])

    with patch("ats_scrapers.manifest.Manifest.fetch", return_value=manifest):
        results = _run_doctor_live_checks(cfg)

    status, detail = _status_for(results, "Unconfigured upstream ATS sources")
    assert status == _CheckStatus.PASS
    assert "none" in detail


def test_live_manifest_check_handles_fetch_failure_gracefully(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _ats_cfg(["greenhouse"], tmp_path)

    with patch("ats_scrapers.manifest.Manifest.fetch", side_effect=RuntimeError("network down")):
        results = _run_doctor_live_checks(cfg)  # must not raise

    status, detail = _status_for(results, "Upstream manifest (LIVE)")
    assert status == _CheckStatus.WARN
    assert "could not reach manifest" in detail
