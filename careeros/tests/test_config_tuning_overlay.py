"""Tests for the v2.0 tuning overlay merge in careeros/config.py — the
structural guardrail that lets the (currently dormant) query tuner change
only three specific api.* keys, enforced by load_config itself rather than
by tuner policy alone."""

from __future__ import annotations

from careeros.config import TUNING_OVERLAY_ALLOWLIST, load_config


def _write(tmp_path, rel_path: str, content: str) -> None:
    path = tmp_path / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_overlay_allowlisted_key_overrides_user_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, ".careeros/config.yaml", "api:\n  title_search: ['Product Manager']\n")
    _write(tmp_path, ".careeros/tuning/overlay.yaml", "api:\n  title_search: ['Product Manager', 'Growth PM']\n")

    cfg = load_config()
    assert cfg.api["title_search"] == ["Product Manager", "Growth PM"]


def test_overlay_non_allowlisted_key_is_silently_dropped(tmp_path, monkeypatch):
    """The overlay must NEVER be able to touch api.limit, api.endpoint, etc.
    -- this is the guardrail an agent structurally cannot bypass by writing
    a differently-shaped overlay file."""
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, ".careeros/config.yaml", "api:\n  limit: 100\n")
    _write(tmp_path, ".careeros/tuning/overlay.yaml", "api:\n  limit: 99999\n  endpoint: active-jb\n")

    cfg = load_config()
    assert cfg.api["limit"] == 100, "a non-allowlisted overlay key must never take effect"
    assert cfg.api.get("endpoint") != "active-jb"


def test_overlay_leaves_other_api_keys_from_user_config_untouched(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, ".careeros/config.yaml", "api:\n  transport: direct\n  time_range: 7d\n")
    _write(tmp_path, ".careeros/tuning/overlay.yaml", "api:\n  tier_limits: {onsite: 20}\n")

    cfg = load_config()
    assert cfg.api["transport"] == "direct"
    assert cfg.api["time_range"] == "7d"
    assert cfg.api["tier_limits"] == {"onsite": 20}


def test_no_overlay_file_is_a_complete_noop(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, ".careeros/config.yaml", "api:\n  title_search: ['PM']\n")
    cfg = load_config()
    assert cfg.api["title_search"] == ["PM"]


def test_empty_overlay_file_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, ".careeros/config.yaml", "api:\n  title_search: ['PM']\n")
    _write(tmp_path, ".careeros/tuning/overlay.yaml", "")
    cfg = load_config()
    assert cfg.api["title_search"] == ["PM"]


def test_custom_overlay_path_is_respected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, ".careeros/config.yaml",
           "tuning:\n  overlay_path: .careeros/tuning/custom.yaml\napi:\n  title_search: ['PM']\n")
    _write(tmp_path, ".careeros/tuning/custom.yaml", "api:\n  title_search: ['PM', 'Custom']\n")
    cfg = load_config()
    assert cfg.api["title_search"] == ["PM", "Custom"]


def test_allowlist_constant_is_exactly_the_three_documented_keys():
    assert set(TUNING_OVERLAY_ALLOWLIST) == {"title_search", "title_exclusion_search", "tier_limits"}
