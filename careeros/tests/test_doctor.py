"""Tests for careeros/cli.py's `doctor` pre-flight checklist. Pure logic —
reads env vars/config/filesystem but makes no network calls and mutates
nothing. `Config.careeros_dir`/`profile_path` are hardcoded relative to cwd,
so each test chdirs into a fresh tmp_path (matches this module's need, no
existing test file needed this pattern before)."""

from __future__ import annotations

from careeros import budget
from careeros.cli import _CheckStatus, _run_doctor_checks
from careeros.config import Config


def _cfg(**overrides) -> Config:
    defaults = dict(
        provider="fantastic-jobs",
        threshold=4.0, consider_threshold=3.5,
        gate_batch_size=50, description_max_chars=4000,
        goals={}, prompts={},
        sheets={}, apify={}, api={}, fx_rates={}, drive={"enabled": False},
    )
    defaults.update(overrides)
    return Config(**defaults)


def _status_for(results, label_substr):
    for status, label, detail in results:
        if label_substr in label:
            return status, detail
    return None, None


def test_missing_careeros_dir_is_the_only_result(tmp_path, monkeypatch):
    """A totally fresh checkout (no `careeros init` yet) should short-circuit
    to one clear instruction, not a wall of unrelated failures."""
    monkeypatch.chdir(tmp_path)
    results = _run_doctor_checks(_cfg())
    assert len(results) == 2
    assert results[0][0] == _CheckStatus.PASS  # Python version
    assert results[1][0] == _CheckStatus.FAIL
    assert "careeros init" in results[1][2]


def _init_careeros_dir(tmp_path, profile_yaml: str | None = None):
    careeros_dir = tmp_path / ".careeros"
    careeros_dir.mkdir()
    if profile_yaml is not None:
        (careeros_dir / "profile.yaml").write_text(profile_yaml)


_VALID_PROFILE = (
    "version: 1\ncandidate: {full_name: A, email: a@x.com}\n"
    "headline: h\ntargets: [pm]\nexperience: []\n"
)


def test_profile_missing_is_reported(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _init_careeros_dir(tmp_path)
    results = _run_doctor_checks(_cfg())
    status, detail = _status_for(results, "Profile")
    assert status == _CheckStatus.FAIL
    assert "not found" in detail


def test_profile_present_and_valid(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _init_careeros_dir(tmp_path, _VALID_PROFILE)
    results = _run_doctor_checks(_cfg())
    status, _ = _status_for(results, "Profile")
    assert status == _CheckStatus.PASS


def test_profile_present_but_invalid(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _init_careeros_dir(tmp_path, "not: a valid profile\n")  # missing required fields
    results = _run_doctor_checks(_cfg())
    status, detail = _status_for(results, "Profile")
    assert status == _CheckStatus.FAIL
    assert "invalid" in detail


def test_direct_transport_with_key_set_passes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _init_careeros_dir(tmp_path, _VALID_PROFILE)
    monkeypatch.setenv("FANTASTIC_API_KEY", "x")
    cfg = _cfg(api={"transport": "direct", "api_key_env": "FANTASTIC_API_KEY", "endpoint": "both"})
    results = _run_doctor_checks(cfg)
    status, detail = _status_for(results, "Discovery credentials")
    assert status == _CheckStatus.PASS
    assert "FANTASTIC_API_KEY" in detail


def test_direct_transport_without_key_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _init_careeros_dir(tmp_path, _VALID_PROFILE)
    monkeypatch.delenv("FANTASTIC_API_KEY", raising=False)
    cfg = _cfg(api={"transport": "direct", "api_key_env": "FANTASTIC_API_KEY", "endpoint": "both"})
    results = _run_doctor_checks(cfg)
    status, detail = _status_for(results, "Discovery credentials")
    assert status == _CheckStatus.FAIL
    assert "FANTASTIC_API_KEY" in detail


def test_transport_unset_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _init_careeros_dir(tmp_path, _VALID_PROFILE)
    cfg = _cfg(api={"transport": None, "endpoint": "both"})
    results = _run_doctor_checks(cfg)
    status, detail = _status_for(results, "Discovery credentials")
    assert status == _CheckStatus.FAIL
    assert "transport" in detail


def test_legacy_actor_provider_checks_apify_token(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _init_careeros_dir(tmp_path, _VALID_PROFILE)
    monkeypatch.delenv("APIFY_TOKEN", raising=False)
    monkeypatch.delenv("APIFY_TOKENS", raising=False)
    cfg = _cfg(provider="fantastic-jobs-actor",
               apify={"token_env": "APIFY_TOKEN", "tokens_env": "APIFY_TOKENS"})

    results = _run_doctor_checks(cfg)
    status, _ = _status_for(results, "legacy actor")
    assert status == _CheckStatus.FAIL

    monkeypatch.setenv("APIFY_TOKEN", "tok")
    results = _run_doctor_checks(cfg)
    status, _ = _status_for(results, "legacy actor")
    assert status == _CheckStatus.PASS


def test_sheets_not_configured_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _init_careeros_dir(tmp_path, _VALID_PROFILE)
    monkeypatch.setenv("X", "k")
    cfg = _cfg(api={"transport": "direct", "api_key_env": "X", "endpoint": "both"},
               sheets={"spreadsheet_id": None, "credentials_path": None})
    results = _run_doctor_checks(cfg)
    status, detail = _status_for(results, "Google Sheets")
    assert status == _CheckStatus.FAIL
    assert "google-setup.md" in detail


def test_sheets_creds_path_missing_file_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _init_careeros_dir(tmp_path, _VALID_PROFILE)
    monkeypatch.setenv("X", "k")
    missing = tmp_path / "does-not-exist.json"
    cfg = _cfg(api={"transport": "direct", "api_key_env": "X", "endpoint": "both"},
               sheets={"spreadsheet_id": "sid", "credentials_path": str(missing)})
    results = _run_doctor_checks(cfg)
    status, detail = _status_for(results, "Google Sheets")
    assert status == _CheckStatus.FAIL
    assert "does not exist" in detail


def test_sheets_fully_configured_passes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _init_careeros_dir(tmp_path, _VALID_PROFILE)
    monkeypatch.setenv("X", "k")
    creds = tmp_path / "creds.json"
    creds.write_text("{}")
    cfg = _cfg(api={"transport": "direct", "api_key_env": "X", "endpoint": "both"},
               sheets={"spreadsheet_id": "sid", "credentials_path": str(creds)})
    results = _run_doctor_checks(cfg)
    status, _ = _status_for(results, "Google Sheets")
    assert status == _CheckStatus.PASS


def test_drive_disabled_is_a_warning_not_a_failure(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _init_careeros_dir(tmp_path, _VALID_PROFILE)
    monkeypatch.setenv("X", "k")
    creds = tmp_path / "c.json"
    creds.write_text("{}")
    cfg = _cfg(api={"transport": "direct", "api_key_env": "X", "endpoint": "both"},
               sheets={"spreadsheet_id": "sid", "credentials_path": str(creds)},
               drive={"enabled": False})
    results = _run_doctor_checks(cfg)
    status, _ = _status_for(results, "Google Drive")
    assert status == _CheckStatus.WARN


def test_drive_enabled_missing_client_secret_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _init_careeros_dir(tmp_path, _VALID_PROFILE)
    monkeypatch.setenv("X", "k")
    creds = tmp_path / "c.json"
    creds.write_text("{}")
    cfg = _cfg(api={"transport": "direct", "api_key_env": "X", "endpoint": "both"},
               sheets={"spreadsheet_id": "sid", "credentials_path": str(creds)},
               drive={"enabled": True, "client_secret_path": None, "root_folder_id": "f"})
    results = _run_doctor_checks(cfg)
    status, detail = _status_for(results, "Google Drive")
    assert status == _CheckStatus.FAIL
    assert "client_secret_path" in detail


def test_drive_enabled_missing_root_folder_id_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _init_careeros_dir(tmp_path, _VALID_PROFILE)
    monkeypatch.setenv("X", "k")
    creds = tmp_path / "c.json"
    creds.write_text("{}")
    secret = tmp_path / "secret.json"
    secret.write_text("{}")
    cfg = _cfg(api={"transport": "direct", "api_key_env": "X", "endpoint": "both"},
               sheets={"spreadsheet_id": "sid", "credentials_path": str(creds)},
               drive={"enabled": True, "client_secret_path": str(secret), "root_folder_id": None})
    results = _run_doctor_checks(cfg)
    status, detail = _status_for(results, "Google Drive")
    assert status == _CheckStatus.FAIL
    assert "root_folder_id" in detail


def test_no_last_discovery_error_shows_pass(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _init_careeros_dir(tmp_path, _VALID_PROFILE)
    monkeypatch.setenv("X", "k")
    cfg = _cfg(api={"transport": "direct", "api_key_env": "X", "endpoint": "both"})
    results = _run_doctor_checks(cfg)
    status, detail = _status_for(results, "Last discovery run")
    assert status == _CheckStatus.PASS
    assert "no recorded failures" in detail


def test_last_discovery_error_surfaces_as_warning_from_local_state_only(tmp_path, monkeypatch):
    """P2.9: doctor reads the persisted last-failure file — no live API call."""
    monkeypatch.chdir(tmp_path)
    _init_careeros_dir(tmp_path, _VALID_PROFILE)
    monkeypatch.setenv("X", "k")
    budget.record_last_error(tmp_path / ".careeros", "2026-07-09", "API key rejected (HTTP 401)")
    cfg = _cfg(api={"transport": "direct", "api_key_env": "X", "endpoint": "both"})
    results = _run_doctor_checks(cfg)
    status, detail = _status_for(results, "Last discovery run")
    assert status == _CheckStatus.WARN
    assert "2026-07-09" in detail
    assert "API key rejected" in detail


def test_discovery_limit_warns_when_current_exceeds_recommended(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _init_careeros_dir(tmp_path, _VALID_PROFILE)  # no work_mode_priority -> 1 query tier
    monkeypatch.setenv("X", "k")
    # api.limit unset -> DEFAULT_LIMIT (100); recommended = 500 // 7 // 1 = 71
    cfg = _cfg(api={"transport": "direct", "api_key_env": "X", "endpoint": "both", "plan": "free"})
    results = _run_doctor_checks(cfg)
    status, detail = _status_for(results, "Discovery limit")
    assert status == _CheckStatus.WARN
    assert "current=100" in detail
    assert "recommended=71" in detail


def test_discovery_limit_passes_when_within_recommendation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _init_careeros_dir(tmp_path, _VALID_PROFILE)
    monkeypatch.setenv("X", "k")
    cfg = _cfg(api={"transport": "direct", "api_key_env": "X", "endpoint": "both",
                     "plan": "free", "limit": 50})
    results = _run_doctor_checks(cfg)
    status, detail = _status_for(results, "Discovery limit")
    assert status == _CheckStatus.PASS
    assert "current=50" in detail


def test_discovery_limit_not_shown_when_plan_unknown(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _init_careeros_dir(tmp_path, _VALID_PROFILE)
    monkeypatch.setenv("X", "k")
    cfg = _cfg(api={"transport": "direct", "api_key_env": "X", "endpoint": "both"})  # no plan
    results = _run_doctor_checks(cfg)
    status, _ = _status_for(results, "Discovery limit")
    assert status is None


def test_no_failures_means_a_fully_configured_setup_is_all_clear(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _init_careeros_dir(tmp_path, _VALID_PROFILE)
    monkeypatch.setenv("X", "k")
    creds = tmp_path / "c.json"
    creds.write_text("{}")
    cfg = _cfg(api={"transport": "direct", "api_key_env": "X", "endpoint": "both"},
               sheets={"spreadsheet_id": "sid", "credentials_path": str(creds)},
               drive={"enabled": False})
    results = _run_doctor_checks(cfg)
    assert not [r for r in results if r[0] == _CheckStatus.FAIL]
