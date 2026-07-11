"""Tests for careeros/cli.py's `doctor` pre-flight checklist. Pure logic —
reads env vars/config/filesystem but makes no network calls and mutates
nothing. `Config.careeros_dir`/`profile_path` are hardcoded relative to cwd,
so each test chdirs into a fresh tmp_path (matches this module's need, no
existing test file needed this pattern before)."""

from __future__ import annotations

import json

from careeros import budget
from careeros.cli import _CheckStatus, _run_doctor_checks
from careeros.config import Config


def _cfg(**overrides) -> Config:
    """v1.2: `_run_doctor_checks` reads `enabled_providers(cfg)` (the
    `providers:` model), not `cfg.provider` — default to exactly ONE enabled
    provider matching whatever `provider=` a test passes (mirroring the
    single-provider behavior these tests were written against). A test that
    wants a different/additional set can still pass `providers=...` directly."""
    provider_name = overrides.get("provider", "fantastic-jobs")
    defaults = dict(
        provider="fantastic-jobs",
        threshold=4.0, consider_threshold=3.5,
        gate_batch_size=50, description_max_chars=4000,
        goals={}, prompts={},
        sheets={}, apify={}, api={}, fx_rates={}, drive={"enabled": False},
        providers={provider_name: {"enabled": True}},
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


def test_discovery_limit_shown_with_assumed_free_plan_when_plan_unset(tmp_path, monkeypatch):
    """P2.9.1: an unset plan now assumes Free rather than being purely
    informational, so the Discovery limit row shows using that assumption,
    clearly flagged as assumed rather than an explicit user choice."""
    monkeypatch.chdir(tmp_path)
    _init_careeros_dir(tmp_path, _VALID_PROFILE)
    monkeypatch.setenv("X", "k")
    cfg = _cfg(api={"transport": "direct", "api_key_env": "X", "endpoint": "both"})  # no plan
    results = _run_doctor_checks(cfg)
    status, detail = _status_for(results, "Discovery limit")
    assert status == _CheckStatus.WARN
    assert "current=100" in detail
    assert "recommended=71" in detail
    assert "assumed default" in detail


def test_discovery_limit_no_assumed_note_when_plan_explicit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _init_careeros_dir(tmp_path, _VALID_PROFILE)
    monkeypatch.setenv("X", "k")
    cfg = _cfg(api={"transport": "direct", "api_key_env": "X", "endpoint": "both", "plan": "free"})
    results = _run_doctor_checks(cfg)
    status, detail = _status_for(results, "Discovery limit")
    assert status == _CheckStatus.WARN
    assert "assumed default" not in detail


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


# ── v1.3: per-provider last-run health/timing + Apify token pool status ──

def _write_discover_raw(tmp_path, date: str, meta: dict) -> None:
    stage_dir = tmp_path / ".careeros" / "runs" / date / "01_discover"
    stage_dir.mkdir(parents=True)
    (stage_dir / "raw.json").write_text(json.dumps({"providers": list(meta), "items": {}, "meta": meta}))


def test_no_run_history_shows_never_run_for_every_active_provider(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _init_careeros_dir(tmp_path, _VALID_PROFILE)
    cfg = _cfg(providers={"fantastic-jobs": {"enabled": True}, "remoteok": {"enabled": True}})

    results = _run_doctor_checks(cfg)
    # No .careeros/runs/ at all -> _latest_discovery_meta returns nothing,
    # so no "Last run" lines are added (nothing to report yet, not a FAIL).
    assert _status_for(results, "Last run (fantastic-jobs)") == (None, None)


def test_last_run_shows_success_with_items_and_duration(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _init_careeros_dir(tmp_path, _VALID_PROFILE)
    _write_discover_raw(tmp_path, "2026-07-11", {
        "remoteok": {"cost_usd": 0.0, "requests": 1, "records": 42, "seconds": 1.23,
                     "warnings": [], "errors": [], "skipped": False, "skip_reason": None},
    })
    cfg = _cfg(providers={"remoteok": {"enabled": True}})

    results = _run_doctor_checks(cfg)
    status, detail = _status_for(results, "Last run (remoteok)")
    assert status == _CheckStatus.PASS
    assert "42 items" in detail
    assert "1.2s" in detail


def test_last_run_shows_skip_reason_for_a_skipped_provider(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _init_careeros_dir(tmp_path, _VALID_PROFILE)
    _write_discover_raw(tmp_path, "2026-07-11", {
        "glassdoor": {"cost_usd": 0.0, "requests": 0, "records": 0, "seconds": 0.0,
                      "warnings": [], "errors": [], "skipped": True,
                      "skip_reason": "monthly Apify budget exhausted"},
    })
    cfg = _cfg(providers={"glassdoor": {"enabled": True}})

    results = _run_doctor_checks(cfg)
    status, detail = _status_for(results, "Last run (glassdoor)")
    assert status == _CheckStatus.WARN
    assert "monthly Apify budget exhausted" in detail


def test_last_run_shows_never_run_for_a_provider_missing_from_latest_meta(tmp_path, monkeypatch):
    """A provider just enabled since the last discover run has no entry in
    that run's meta block at all — distinct from "no run history exists"."""
    monkeypatch.chdir(tmp_path)
    _init_careeros_dir(tmp_path, _VALID_PROFILE)
    _write_discover_raw(tmp_path, "2026-07-11", {
        "remoteok": {"cost_usd": 0.0, "requests": 1, "records": 5, "seconds": 0.5,
                     "warnings": [], "errors": [], "skipped": False, "skip_reason": None},
    })
    cfg = _cfg(providers={"remoteok": {"enabled": True}, "naukri": {"enabled": True}})

    results = _run_doctor_checks(cfg)
    status, detail = _status_for(results, "Last run (naukri)")
    assert status == _CheckStatus.WARN
    assert "never run" in detail


def test_last_run_picks_the_most_recent_of_multiple_run_dates(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _init_careeros_dir(tmp_path, _VALID_PROFILE)
    _write_discover_raw(tmp_path, "2026-07-08", {
        "remoteok": {"cost_usd": 0.0, "requests": 1, "records": 1, "seconds": 0.1,
                     "warnings": [], "errors": [], "skipped": False, "skip_reason": None},
    })
    _write_discover_raw(tmp_path, "2026-07-11", {
        "remoteok": {"cost_usd": 0.0, "requests": 1, "records": 99, "seconds": 2.0,
                     "warnings": [], "errors": [], "skipped": False, "skip_reason": None},
    })
    cfg = _cfg(providers={"remoteok": {"enabled": True}})

    results = _run_doctor_checks(cfg)
    status, detail = _status_for(results, "Last run (remoteok)")
    assert "99 items" in detail
    assert "2026-07-11" in detail


def test_apify_token_pool_not_shown_when_no_monthly_provider_enabled(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _init_careeros_dir(tmp_path, _VALID_PROFILE)
    cfg = _cfg(providers={"remoteok": {"enabled": True}})

    results = _run_doctor_checks(cfg)
    assert _status_for(results, "Apify token pool") == (None, None)


def test_apify_token_pool_shows_all_available_when_none_exhausted(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _init_careeros_dir(tmp_path, _VALID_PROFILE)
    monkeypatch.setenv("APIFY_TOKENS", "tok-a,tok-b")
    cfg = _cfg(
        apify={"tokens_env": "APIFY_TOKENS"},
        providers={"naukri": {"enabled": True, "max_monthly_budget_usd": None}},
    )

    results = _run_doctor_checks(cfg)
    status, detail = _status_for(results, "Apify token pool")
    assert status == _CheckStatus.PASS
    assert "2/2 token(s) available" in detail


def test_apify_token_pool_shows_exhausted_count_and_fails_when_all_exhausted(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _init_careeros_dir(tmp_path, _VALID_PROFILE)
    monkeypatch.setenv("APIFY_TOKENS", "tok-a,tok-b")
    state = budget.load_apify_tokens_state(tmp_path / ".careeros", "2026-07-11")
    budget.mark_token_exhausted(state, "tok-a")
    budget.mark_token_exhausted(state, "tok-b")
    budget.save_apify_tokens_state(tmp_path / ".careeros", state)
    cfg = _cfg(
        apify={"tokens_env": "APIFY_TOKENS"},
        providers={"naukri": {"enabled": True, "max_monthly_budget_usd": None}},
    )

    results = _run_doctor_checks(cfg)
    status, detail = _status_for(results, "Apify token pool")
    assert status == _CheckStatus.FAIL
    assert "0/2 token(s) available" in detail
    assert "2 exhausted" in detail
