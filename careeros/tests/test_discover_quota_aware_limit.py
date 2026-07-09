"""End-to-end tests for `discover`'s P2.9 quota-aware default limit: when no
explicit limit is set (CLI --limit or api.limit) and the weekly quota is
known, the computed recommendation is used as the actual fetch limit instead
of the hardcoded 100 — closing the gap between what `careeros config`/
`start` recommend and what `discover` fetches. A fake provider replaces the
real network call (registered into the real registry for the test's
duration via monkeypatch.setitem, matching this repo's provider-pluggable
design) so no HTTP happens and the fetched `limit` is directly observable."""

from __future__ import annotations

from typer.testing import CliRunner

from careeros.cli import app
from careeros.providers import registry

runner = CliRunner()


class _FakeProvider:
    id = "fake-fj"

    def __init__(self):
        self.limits: list[int] = []

    def fetch(self, config, *, limit=100, search="", query=None):
        self.limits.append(limit)
        return [], 0.0

    def to_job_dict(self, raw):
        return None


def _write_config(tmp_path, extra_api: str = "") -> None:
    (tmp_path / ".careeros").mkdir()
    (tmp_path / ".careeros" / "config.yaml").write_text(
        f"provider: fake-fj\napi:\n  endpoint: both\n  plan: free\n{extra_api}"
    )


def test_recommended_limit_applied_when_unset(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    fake = _FakeProvider()
    monkeypatch.setitem(registry._REGISTRY, "fake-fj", fake)
    _write_config(tmp_path)  # no api.limit -> should fall back to the computed recommendation

    result = runner.invoke(app, ["discover"])
    assert result.exit_code == 0, result.output
    # No profile.yaml -> single fallback query (1 tier); 500 // 7 // 1 = 71
    assert fake.limits == [71]


def test_explicit_cli_limit_always_wins(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    fake = _FakeProvider()
    monkeypatch.setitem(registry._REGISTRY, "fake-fj", fake)
    _write_config(tmp_path)

    result = runner.invoke(app, ["discover", "--limit", "40"])
    assert result.exit_code == 0, result.output
    assert fake.limits == [40]


def test_explicit_config_limit_is_never_overridden(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    fake = _FakeProvider()
    monkeypatch.setitem(registry._REGISTRY, "fake-fj", fake)
    _write_config(tmp_path, extra_api="  limit: 15\n")

    result = runner.invoke(app, ["discover"])
    assert result.exit_code == 0, result.output
    assert fake.limits == [15]


def test_unknown_plan_keeps_hardcoded_default(tmp_path, monkeypatch):
    """No known quota (plan unset) -> nothing to recommend from, so the
    guard stays purely informational and the old 100 default holds."""
    monkeypatch.chdir(tmp_path)
    fake = _FakeProvider()
    monkeypatch.setitem(registry._REGISTRY, "fake-fj", fake)
    (tmp_path / ".careeros").mkdir()
    (tmp_path / ".careeros" / "config.yaml").write_text("provider: fake-fj\napi:\n  endpoint: both\n")

    result = runner.invoke(app, ["discover"])
    assert result.exit_code == 0, result.output
    assert fake.limits == [100]
