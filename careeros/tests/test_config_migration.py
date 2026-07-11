"""Tests for careeros/config.py's v1.2 ONE-config-model migration:
`_migrate_legacy_provider` (the temporary, isolated upgrade-on-read shim for
the deprecated `provider:` key), `enabled_providers` (config-order
resolution), `provider_config_block` (per-provider config resolution), and
the `careeros migrate-config` CLI command (the permanent, on-disk upgrade —
modeled on `careeros sheets migrate`).

The core invariant under test: a config file that still uses the deprecated
single `provider:` key keeps working, auto-upgraded IN MEMORY on every load
with a one-time notice — never a second live config system — and
`migrate-config` is the explicit, idempotent way to make that upgrade
permanent on disk."""

from __future__ import annotations

import yaml
from typer.testing import CliRunner

from careeros.cli import app
from careeros.config import (
    LEGACY_PROVIDER_DEPRECATION_NOTICE, _migrate_legacy_provider, enabled_providers,
    load_config, provider_config_block,
)

runner = CliRunner()


# ── _migrate_legacy_provider: pure function ─────────────────────────────

def test_no_migration_when_providers_key_already_present():
    raw_user_cfg = {"providers": {"fantastic-jobs": {"enabled": True}}}
    merged = {"providers": {"fantastic-jobs": {"enabled": True}}}
    out, migrated = _migrate_legacy_provider(raw_user_cfg, merged)
    assert migrated is False
    assert out is merged


def test_no_migration_when_neither_key_present():
    raw_user_cfg = {"threshold": 4.0}
    merged = {"providers": {"fantastic-jobs": {"enabled": True}}}
    out, migrated = _migrate_legacy_provider(raw_user_cfg, merged)
    assert migrated is False
    assert out is merged


def test_migrates_legacy_provider_key_to_single_enabled_entry():
    raw_user_cfg = {"provider": "fantastic-jobs-actor"}
    merged = {"providers": {"fantastic-jobs": {"enabled": True}, "remoteok": {"enabled": True}}}
    out, migrated = _migrate_legacy_provider(raw_user_cfg, merged)
    assert migrated is True
    # REPLACES the default providers dict entirely — never silently enables
    # remoteok/other defaults for someone who only ever set `provider:`.
    assert out["providers"] == {"fantastic-jobs-actor": {"enabled": True}}


def test_migration_present_even_if_stale_provider_key_also_set():
    """`providers:` wins outright — a leftover `provider:` line is ignored,
    not merged or reconciled."""
    raw_user_cfg = {"provider": "fantastic-jobs-actor", "providers": {"remoteok": {"enabled": True}}}
    merged = {"providers": {"remoteok": {"enabled": True}}}
    out, migrated = _migrate_legacy_provider(raw_user_cfg, merged)
    assert migrated is False
    assert out["providers"] == {"remoteok": {"enabled": True}}


# ── load_config: end-to-end migration + one-time notice ─────────────────

def test_load_config_migrates_legacy_provider_and_flags_it(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".careeros").mkdir()
    (tmp_path / ".careeros" / "config.yaml").write_text("provider: fantastic-jobs-actor\n")

    cfg = load_config()
    assert cfg.provider_migrated is True
    assert cfg.providers == {"fantastic-jobs-actor": {"enabled": True}}
    out = capsys.readouterr().out
    assert LEGACY_PROVIDER_DEPRECATION_NOTICE.format(name="fantastic-jobs-actor") in out


def test_load_config_new_model_needs_no_migration(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".careeros").mkdir()
    (tmp_path / ".careeros" / "config.yaml").write_text(
        "providers:\n  fantastic-jobs:\n    enabled: true\n  remoteok:\n    enabled: true\n"
    )

    cfg = load_config()
    assert cfg.provider_migrated is False
    assert cfg.providers["fantastic-jobs"]["enabled"] is True
    assert cfg.providers["remoteok"]["enabled"] is True
    assert capsys.readouterr().out == ""


def test_load_config_fresh_defaults_need_no_migration(tmp_path, monkeypatch):
    """No config.yaml at all -> DEFAULT_CONFIG's providers dict applies
    directly (fantastic-jobs/remoteok/we-work-remotely enabled) — not a
    migration, just the shipped default."""
    monkeypatch.chdir(tmp_path)
    cfg = load_config()
    assert cfg.provider_migrated is False
    assert cfg.providers["fantastic-jobs"]["enabled"] is True
    assert cfg.providers["remoteok"]["enabled"] is True
    assert cfg.providers["naukri"]["enabled"] is False


# ── enabled_providers: config-order resolution ───────────────────────────

def test_enabled_providers_preserves_config_order(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".careeros").mkdir()
    (tmp_path / ".careeros" / "config.yaml").write_text(
        "providers:\n"
        "  ziprecruiter:\n    enabled: true\n"
        "  fantastic-jobs:\n    enabled: true\n"
        "  naukri:\n    enabled: false\n"
        "  remoteok:\n    enabled: true\n"
    )
    cfg = load_config()
    assert enabled_providers(cfg) == ["ziprecruiter", "fantastic-jobs", "remoteok"]


def test_enabled_providers_empty_when_none_enabled(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".careeros").mkdir()
    (tmp_path / ".careeros" / "config.yaml").write_text(
        "providers:\n  fantastic-jobs:\n    enabled: false\n"
    )
    cfg = load_config()
    assert enabled_providers(cfg) == []


# ── provider_config_block: per-provider resolution, no cross-leakage ────

def test_provider_config_block_fantastic_jobs_resolves_to_api():
    from careeros.config import Config
    cfg = Config(
        provider="fantastic-jobs", threshold=4.0, consider_threshold=3.5,
        gate_batch_size=50, description_max_chars=4000,
        api={"plan": "free"}, apify={"max_monthly_budget_usd": 10},
        providers={"fantastic-jobs": {"enabled": True}},
    )
    assert provider_config_block(cfg, "fantastic-jobs") is cfg.api


def test_provider_config_block_actor_resolves_to_apify():
    from careeros.config import Config
    cfg = Config(
        provider="fantastic-jobs-actor", threshold=4.0, consider_threshold=3.5,
        gate_batch_size=50, description_max_chars=4000,
        api={}, apify={"max_monthly_budget_usd": 10},
        providers={"fantastic-jobs-actor": {"enabled": True}},
    )
    assert provider_config_block(cfg, "fantastic-jobs-actor") is cfg.apify


def test_provider_config_block_new_provider_resolves_to_its_own_entry_unmerged():
    """CRITICAL: must NOT merge cfg.apify in — that would leak
    max_monthly_budget_usd into a free provider's resolved config and
    wrongly trigger the monthly budget guard for it (see budget.guard_for)."""
    from careeros.config import Config
    cfg = Config(
        provider="fantastic-jobs", threshold=4.0, consider_threshold=3.5,
        gate_batch_size=50, description_max_chars=4000,
        api={}, apify={"max_monthly_budget_usd": 10},
        providers={"remoteok": {"enabled": True}},
    )
    block = provider_config_block(cfg, "remoteok")
    assert block == {"enabled": True}
    assert "max_monthly_budget_usd" not in block


def test_provider_config_block_naukri_own_entry_has_monthly_budget_key():
    from careeros.config import Config
    cfg = Config(
        provider="fantastic-jobs", threshold=4.0, consider_threshold=3.5,
        gate_batch_size=50, description_max_chars=4000,
        api={}, apify={"max_monthly_budget_usd": 10},
        providers={"naukri": {"enabled": True, "max_monthly_budget_usd": None}},
    )
    block = provider_config_block(cfg, "naukri")
    assert "max_monthly_budget_usd" in block  # key present (even if null) -> "monthly" capability


# ── careeros migrate-config: permanent, on-disk, idempotent ─────────────

def test_migrate_config_rewrites_legacy_key(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".careeros").mkdir()
    config_path = tmp_path / ".careeros" / "config.yaml"
    config_path.write_text("provider: fantastic-jobs\nthreshold: 4.0\n")

    result = runner.invoke(app, ["migrate-config"])
    assert result.exit_code == 0, result.output

    rewritten = yaml.safe_load(config_path.read_text())
    assert "provider" not in rewritten
    assert rewritten["providers"] == {"fantastic-jobs": {"enabled": True}}
    assert rewritten["threshold"] == 4.0  # other keys preserved untouched


def test_migrate_config_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".careeros").mkdir()
    config_path = tmp_path / ".careeros" / "config.yaml"
    config_path.write_text("providers:\n  fantastic-jobs:\n    enabled: true\n")

    result = runner.invoke(app, ["migrate-config"])
    assert result.exit_code == 0, result.output
    assert "Already on the providers: model" in result.output


def test_migrate_config_no_op_when_no_provider_key(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".careeros").mkdir()
    config_path = tmp_path / ".careeros" / "config.yaml"
    config_path.write_text("threshold: 4.0\n")

    result = runner.invoke(app, ["migrate-config"])
    assert result.exit_code == 0, result.output
    assert "nothing to migrate" in result.output
    assert yaml.safe_load(config_path.read_text()) == {"threshold": 4.0}


def test_migrate_config_fails_cleanly_when_no_config_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["migrate-config"])
    assert result.exit_code == 1
    assert "not found" in result.output
