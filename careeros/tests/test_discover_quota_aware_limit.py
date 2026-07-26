"""End-to-end tests for `discover`'s quota-aware default limit: when no
explicit limit is set (CLI --limit or api.limit) and the weekly quota is
known, the computed recommendation is used as the actual fetch limit instead
of the hardcoded 100 — closing the gap between what `careeros config`/
`start` recommend and what `discover` fetches. Plus (v1.7) the daily-total
semantics: `limit` is jobs per DAY, divided across the candidate's search
tiers, not a per-search count.

A fake provider replaces the real network call (registered into the real
registry AS "fantastic-jobs" for the test's duration via
monkeypatch.setitem, matching this repo's provider-pluggable design) so no
HTTP happens and the fetched `limit` is directly observable.

The fake is registered under the REAL "fantastic-jobs" id (not an arbitrary
"fake-fj") because `config.provider_config_block` resolves that name to
`cfg.api` and every other name to its own `providers:` entry — the
weekly-quota-guard arithmetic these tests assert on is specifically
Fantastic Jobs' `api:` block, so the fake must resolve the same way the real
provider does."""

from __future__ import annotations

from typer.testing import CliRunner

from careeros.cli import app
from careeros.providers.base import ProviderResult
from careeros.providers import registry

runner = CliRunner()


class _FakeProvider:
    id = "fantastic-jobs"

    def __init__(self):
        self.limits: list[int] = []

    def validate(self, config):
        return []

    def fetch(self, config, *, limit=100, search="", query=None):
        self.limits.append(limit)
        return ProviderResult(provider=self.id, items=[], cost_usd=0.0)

    def to_job_dict(self, raw):
        return None


def _write_config(tmp_path, extra_api: str = "") -> None:
    (tmp_path / ".careeros").mkdir()
    (tmp_path / ".careeros" / "config.yaml").write_text(
        "providers:\n  fantastic-jobs:\n    enabled: true\n"
        f"api:\n  endpoint: both\n  plan: free\n{extra_api}"
    )


def test_recommended_limit_applied_when_unset(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    fake = _FakeProvider()
    monkeypatch.setitem(registry._REGISTRY, "fantastic-jobs", fake)
    _write_config(tmp_path)  # no api.limit -> should fall back to the computed recommendation

    result = runner.invoke(app, ["discover"])
    assert result.exit_code == 0, result.output
    # No profile.yaml -> single fallback query (1 tier); 500 // 7 // 1 = 71
    assert fake.limits == [71]


def test_explicit_cli_limit_always_wins(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    fake = _FakeProvider()
    monkeypatch.setitem(registry._REGISTRY, "fantastic-jobs", fake)
    _write_config(tmp_path)

    result = runner.invoke(app, ["discover", "--limit", "40"])
    assert result.exit_code == 0, result.output
    assert fake.limits == [40]


def test_explicit_config_limit_is_never_overridden(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    fake = _FakeProvider()
    monkeypatch.setitem(registry._REGISTRY, "fantastic-jobs", fake)
    _write_config(tmp_path, extra_api="  limit: 15\n")

    result = runner.invoke(app, ["discover"])
    assert result.exit_code == 0, result.output
    assert fake.limits == [15]


def test_unset_plan_defaults_to_free_tier_recommendation(tmp_path, monkeypatch):
    """P2.9.1: no api.plan configured -> assume Free (the safe OSS default)
    and compute a real recommendation, instead of silently fetching at the
    old hardcoded 100 default."""
    monkeypatch.chdir(tmp_path)
    fake = _FakeProvider()
    monkeypatch.setitem(registry._REGISTRY, "fantastic-jobs", fake)
    (tmp_path / ".careeros").mkdir()
    (tmp_path / ".careeros" / "config.yaml").write_text(
        "providers:\n  fantastic-jobs:\n    enabled: true\napi:\n  endpoint: both\n"
    )

    result = runner.invoke(app, ["discover"])
    assert result.exit_code == 0, result.output
    # No profile.yaml -> single fallback query (1 tier); free tier assumed:
    # 500 // 7 // 1 = 71
    assert fake.limits == [71]
    assert "Assuming the Free plan" in result.output


def test_explicit_unknown_plan_keeps_hardcoded_default(tmp_path, monkeypatch):
    """An EXPLICITLY-set plan with no verified quota (e.g. a typo'd or
    not-yet-supported plan name) stays purely informational — the free
    default only applies when api.plan is unset entirely."""
    monkeypatch.chdir(tmp_path)
    fake = _FakeProvider()
    monkeypatch.setitem(registry._REGISTRY, "fantastic-jobs", fake)
    (tmp_path / ".careeros").mkdir()
    (tmp_path / ".careeros" / "config.yaml").write_text(
        "providers:\n  fantastic-jobs:\n    enabled: true\napi:\n  endpoint: both\n  plan: paid\n"
    )

    result = runner.invoke(app, ["discover"])
    assert result.exit_code == 0, result.output
    assert fake.limits == [100]
    assert "Assuming the Free plan" not in result.output


# ── v1.7: `limit` is a DAILY TOTAL, divided across the candidate's tiers ──
#
# These use a real profile so `build_query_plan` produces more than one tier
# — that's the whole point of the change, and a single-tier config can't
# distinguish a daily total from a per-search count.

_THREE_TIER_PROFILE = (
    "version: 1\n"
    "candidate: {full_name: A, email: user@example.com}\n"
    "headline: h\ntargets: [pm]\nexperience: []\n"
    "role_priorities: [Product Manager]\n"
    "work_mode_priority: [global_remote, india_remote, mumbai_onsite]\n"
    "location: {onsite_ok: [Mumbai]}\n"
)


def _write_profile(tmp_path, profile: str = _THREE_TIER_PROFILE) -> None:
    (tmp_path / ".careeros" / "profile.yaml").write_text(profile)


def test_daily_total_is_divided_across_tiers(tmp_path, monkeypatch):
    """api.limit 120 on a 3-tier profile fetches 40 per search, 120/day —
    NOT 120 per search (the pre-v1.7 reading, which fetched 360/day)."""
    monkeypatch.chdir(tmp_path)
    fake = _FakeProvider()
    monkeypatch.setitem(registry._REGISTRY, "fantastic-jobs", fake)
    _write_config(tmp_path, extra_api="  limit: 120\n")
    _write_profile(tmp_path)

    result = runner.invoke(app, ["discover"])
    assert result.exit_code == 0, result.output
    assert fake.limits == [40, 40, 40]
    assert sum(fake.limits) == 120


def test_cli_limit_is_also_a_daily_total(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    fake = _FakeProvider()
    monkeypatch.setitem(registry._REGISTRY, "fantastic-jobs", fake)
    _write_config(tmp_path)
    _write_profile(tmp_path)

    result = runner.invoke(app, ["discover", "--limit", "90"])
    assert result.exit_code == 0, result.output
    assert fake.limits == [30, 30, 30]


def test_unset_limit_uses_recommended_daily_total_across_tiers(tmp_path, monkeypatch):
    """Free plan, 7 active days -> 71 jobs/day recommended, split 3 ways."""
    monkeypatch.chdir(tmp_path)
    fake = _FakeProvider()
    monkeypatch.setitem(registry._REGISTRY, "fantastic-jobs", fake)
    _write_config(tmp_path)
    _write_profile(tmp_path)

    result = runner.invoke(app, ["discover"])
    assert result.exit_code == 0, result.output
    assert fake.limits == [23, 23, 23]  # 500 // 7 = 71, // 3 tiers = 23


def test_daily_total_below_tier_count_floors_at_one_and_warns(tmp_path, monkeypatch):
    """Asking for fewer jobs/day than you have searches still fetches one per
    search — the real total EXCEEDS the request, so say so rather than
    silently dropping tiers."""
    monkeypatch.chdir(tmp_path)
    fake = _FakeProvider()
    monkeypatch.setitem(registry._REGISTRY, "fantastic-jobs", fake)
    _write_config(tmp_path, extra_api="  limit: 2\n")
    _write_profile(tmp_path)

    result = runner.invoke(app, ["discover"])
    assert result.exit_code == 0, result.output
    assert fake.limits == [1, 1, 1]
    assert "fewer than your 3 search tier(s)" in result.output


def test_lowering_active_days_raises_the_daily_total(tmp_path, monkeypatch):
    """The documented lever: the same weekly quota over fewer active days
    means more jobs on each day you actually run."""
    monkeypatch.chdir(tmp_path)
    fake = _FakeProvider()
    monkeypatch.setitem(registry._REGISTRY, "fantastic-jobs", fake)
    _write_config(tmp_path, extra_api="  active_days_per_week: 5\n")
    _write_profile(tmp_path)

    result = runner.invoke(app, ["discover"])
    assert result.exit_code == 0, result.output
    assert fake.limits == [33, 33, 33]  # 500 // 5 = 100, // 3 tiers = 33
