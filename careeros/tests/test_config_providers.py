"""Tests for careeros/config.py's provider config model: `enabled_providers`
(config-order resolution) and `provider_config_block` (per-provider config
resolution).

The core invariant under test: the user's own `providers:` block is
authoritative for both MEMBERSHIP and ORDER, and each provider's resolved
config block stays UNMERGED with any other's — that's what keeps
`budget.guard_for`'s capability detection structural rather than name-based.

Fixture provider names below are deliberately neutral stand-ins
(`stub-source-a`) rather than real provider ids: these tests cover generic
multi-provider resolution, which must keep working for whatever source is
added next, and shouldn't need editing every time the shipped set changes.
"""

from __future__ import annotations

from careeros.config import Config, enabled_providers, load_config, provider_config_block


def _cfg(**overrides) -> Config:
    base = dict(
        threshold=4.0, consider_threshold=3.5, gate_batch_size=50,
        description_max_chars=4000, api={}, providers={},
    )
    base.update(overrides)
    return Config(**base)


# ── load_config: defaults and the providers: model ───────────────────────

def test_load_config_fresh_defaults_enable_only_fantastic_jobs(tmp_path, monkeypatch):
    """No config.yaml at all -> DEFAULT_CONFIG's providers dict applies."""
    monkeypatch.chdir(tmp_path)
    cfg = load_config()
    assert cfg.providers == {"fantastic-jobs": {"enabled": True}}
    assert enabled_providers(cfg) == ["fantastic-jobs"]


def test_load_config_reads_providers_block(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".careeros").mkdir()
    (tmp_path / ".careeros" / "config.yaml").write_text(
        "providers:\n  fantastic-jobs:\n    enabled: true\n  stub-source-a:\n    enabled: true\n"
    )
    cfg = load_config()
    assert cfg.providers["fantastic-jobs"]["enabled"] is True
    assert cfg.providers["stub-source-a"]["enabled"] is True
    assert capsys.readouterr().out == ""


def test_users_providers_block_is_authoritative_for_membership(tmp_path, monkeypatch):
    """A user's `providers:` block REPLACES the default set — it must never
    silently re-add a shipped default the user didn't list."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".careeros").mkdir()
    (tmp_path / ".careeros" / "config.yaml").write_text(
        "providers:\n  stub-source-a:\n    enabled: true\n"
    )
    cfg = load_config()
    assert cfg.providers == {"stub-source-a": {"enabled": True}}
    assert "fantastic-jobs" not in cfg.providers


# ── enabled_providers: config-order resolution ───────────────────────────

def test_enabled_providers_preserves_config_order(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".careeros").mkdir()
    (tmp_path / ".careeros" / "config.yaml").write_text(
        "providers:\n"
        "  stub-source-a:\n    enabled: true\n"
        "  fantastic-jobs:\n    enabled: true\n"
        "  stub-source-b:\n    enabled: false\n"
        "  stub-source-c:\n    enabled: true\n"
    )
    cfg = load_config()
    assert enabled_providers(cfg) == ["stub-source-a", "fantastic-jobs", "stub-source-c"]


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
    cfg = _cfg(api={"plan": "free"}, providers={"fantastic-jobs": {"enabled": True}})
    assert provider_config_block(cfg, "fantastic-jobs") is cfg.api


def test_provider_config_block_other_provider_resolves_to_its_own_entry():
    """CRITICAL: an unmetered provider's block must NOT pick up a
    max_monthly_budget_usd key from anywhere else — that would wrongly
    trigger the monthly spend guard for it (see budget.guard_for)."""
    cfg = _cfg(api={"plan": "free"}, providers={"stub-source-a": {"enabled": True}})
    block = provider_config_block(cfg, "stub-source-a")
    assert block == {"enabled": True}
    assert "max_monthly_budget_usd" not in block
    assert "plan" not in block  # must not leak fantastic-jobs' api: block either


def test_provider_config_block_metered_provider_keeps_monthly_budget_key():
    """A key present (even if null) is what opts a provider into the
    "monthly" spend capability."""
    cfg = _cfg(providers={"stub-source-a": {"enabled": True, "max_monthly_budget_usd": None}})
    block = provider_config_block(cfg, "stub-source-a")
    assert "max_monthly_budget_usd" in block


def test_provider_config_block_unknown_provider_is_empty_dict():
    cfg = _cfg(providers={"fantastic-jobs": {"enabled": True}})
    assert provider_config_block(cfg, "never-registered") == {}
