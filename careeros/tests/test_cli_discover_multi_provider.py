"""End-to-end tests for `discover`'s v1.2 multi-provider loop
(`_discover_one_provider` in cli.py) — the generic, capability-driven
orchestration that replaced the old single-provider body. Fake providers
replace real network/Apify calls (registered into the real registry via
monkeypatch.setitem, matching this repo's provider-pluggable design and the
pattern in test_discover_quota_aware_limit.py), so nothing here makes a real
HTTP/Apify call.

Covers the concrete guarantees this redesign promises:
- multiple providers run and merge into one raw.json, in CONFIG ORDER;
- a provider that fails validate() is skipped (with a reason), the rest of
  the run continues;
- a provider whose capability guard says stop (monthly budget exhausted) is
  skipped without ever calling fetch();
- `normalize` correctly maps each provider's own items with its own
  to_job_dict and concatenates into one flat jobs.json, exactly the shape
  the rest of the pipeline (dedupe onward) already expects."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from careeros import budget
from careeros.cli import app
from careeros.providers.base import ProviderError, ProviderResult
from careeros.providers import registry

runner = CliRunner()


class _FakeProvider:
    """A minimal fake conforming to the v1.2 3-method contract. No
    "plan"/"max_monthly_budget_usd" keys in its own config block by default
    -> budget.guard_for resolves it to "none" (unmetered), matching
    RemoteOK/We Work Remotely's real shape."""

    def __init__(self, provider_id: str, items: list[dict], *, validate_errors: list[str] | None = None,
                 fetch_error: Exception | None = None):
        self.id = provider_id
        self._items = items
        self._validate_errors = validate_errors or []
        self._fetch_error = fetch_error
        self.fetch_called = False

    def validate(self, config):
        return list(self._validate_errors)

    def fetch(self, config, *, limit=100, search="", query=None):
        self.fetch_called = True
        if self._fetch_error is not None:
            raise self._fetch_error
        return ProviderResult(provider=self.id, items=list(self._items), cost_usd=0.0,
                               requests=1, records=len(self._items), seconds=0.1)

    def to_job_dict(self, raw):
        if not raw.get("title") or not raw.get("url", "").startswith("http"):
            return None
        return {
            "title": raw["title"], "company": raw.get("company", "Unknown"),
            "apply_url": raw["url"], "description": None, "location": None,
            "remote": None, "employment_type": None, "seniority": None,
            "posted_at": None, "salary": None, "contact": None, "company_linkedin": None,
        }


def _job(title: str, company: str = "Acme") -> dict:
    return {"title": title, "company": company, "url": f"https://example.com/{title.replace(' ', '-')}"}


def _write_config(tmp_path, providers_yaml: str) -> None:
    (tmp_path / ".careeros").mkdir()
    (tmp_path / ".careeros" / "config.yaml").write_text(f"providers:\n{providers_yaml}")


def test_multiple_providers_merge_in_config_order(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    p1 = _FakeProvider("fake-a", [_job("Role A1"), _job("Role A2")])
    p2 = _FakeProvider("fake-b", [_job("Role B1")])
    monkeypatch.setitem(registry._REGISTRY, "fake-a", p1)
    monkeypatch.setitem(registry._REGISTRY, "fake-b", p2)
    _write_config(tmp_path, "  fake-b:\n    enabled: true\n  fake-a:\n    enabled: true\n")

    result = runner.invoke(app, ["discover", "--date", "t1"])
    assert result.exit_code == 0, result.output

    raw = json.loads((tmp_path / ".careeros/runs/t1/01_discover/raw.json").read_text())
    # Config listed fake-b BEFORE fake-a -> that's the run/merge order.
    assert raw["providers"] == ["fake-b", "fake-a"]
    assert len(raw["items"]["fake-a"]) == 2
    assert len(raw["items"]["fake-b"]) == 1

    normalize_result = runner.invoke(app, ["normalize", "--date", "t1"])
    assert normalize_result.exit_code == 0, normalize_result.output
    jobs = json.loads((tmp_path / ".careeros/runs/t1/02_normalize/jobs.json").read_text())
    assert len(jobs) == 3
    sources = {j["source"] for j in jobs}
    assert sources == {"fake-a", "fake-b"}


def test_single_disabled_provider_is_never_fetched(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    on = _FakeProvider("fake-on", [_job("On")])
    off = _FakeProvider("fake-off", [_job("Off")])
    monkeypatch.setitem(registry._REGISTRY, "fake-on", on)
    monkeypatch.setitem(registry._REGISTRY, "fake-off", off)
    _write_config(tmp_path, "  fake-on:\n    enabled: true\n  fake-off:\n    enabled: false\n")

    result = runner.invoke(app, ["discover", "--date", "t2"])
    assert result.exit_code == 0, result.output
    assert on.fetch_called is True
    assert off.fetch_called is False

    raw = json.loads((tmp_path / ".careeros/runs/t2/01_discover/raw.json").read_text())
    assert raw["providers"] == ["fake-on"]


def test_validate_failure_skips_provider_and_continues(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    broken = _FakeProvider("fake-broken", [_job("Never")], validate_errors=["no token configured"])
    ok = _FakeProvider("fake-ok", [_job("Fine")])
    monkeypatch.setitem(registry._REGISTRY, "fake-broken", broken)
    monkeypatch.setitem(registry._REGISTRY, "fake-ok", ok)
    _write_config(tmp_path, "  fake-broken:\n    enabled: true\n  fake-ok:\n    enabled: true\n")

    result = runner.invoke(app, ["discover", "--date", "t3"])
    assert result.exit_code == 0, result.output
    assert broken.fetch_called is False  # validate() failed -> fetch() never called
    assert ok.fetch_called is True

    raw = json.loads((tmp_path / ".careeros/runs/t3/01_discover/raw.json").read_text())
    assert raw["providers"] == ["fake-broken", "fake-ok"]
    assert raw["items"]["fake-broken"] == []
    assert raw["meta"]["fake-broken"]["skipped"] is True
    assert "no token configured" in raw["meta"]["fake-broken"]["skip_reason"]
    assert raw["meta"]["fake-ok"]["skipped"] is False


def test_monthly_capability_guard_skips_without_calling_fetch(tmp_path, monkeypatch):
    """A provider whose own config declares max_monthly_budget_usd (even a
    key present with a low value) is guarded by budget.guard_for's "monthly"
    capability — already-exhausted budget means fetch() is never called."""
    monkeypatch.chdir(tmp_path)
    paid = _FakeProvider("fake-paid", [_job("Expensive")])
    monkeypatch.setitem(registry._REGISTRY, "fake-paid", paid)
    (tmp_path / ".careeros").mkdir()
    (tmp_path / ".careeros" / "config.yaml").write_text(
        "providers:\n  fake-paid:\n    enabled: true\n    max_monthly_budget_usd: 1.0\n"
    )
    # Pre-exhaust this month's Apify budget state directly.
    from careeros.config import load_config
    cfg = load_config()
    state = budget.load_apify_state(cfg.careeros_dir, "t4")
    state["spend_usd"] = 5.0
    budget.save_apify_state(cfg.careeros_dir, state)

    result = runner.invoke(app, ["discover", "--date", "t4"])
    assert result.exit_code == 0, result.output
    assert paid.fetch_called is False

    raw = json.loads((tmp_path / ".careeros/runs/t4/01_discover/raw.json").read_text())
    assert raw["meta"]["fake-paid"]["skipped"] is True
    assert "budget" in raw["meta"]["fake-paid"]["skip_reason"]


def test_ignore_budget_flag_overrides_monthly_guard(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    paid = _FakeProvider("fake-paid2", [_job("Expensive")])
    monkeypatch.setitem(registry._REGISTRY, "fake-paid2", paid)
    (tmp_path / ".careeros").mkdir()
    (tmp_path / ".careeros" / "config.yaml").write_text(
        "providers:\n  fake-paid2:\n    enabled: true\n    max_monthly_budget_usd: 1.0\n"
    )
    from careeros.config import load_config
    cfg = load_config()
    state = budget.load_apify_state(cfg.careeros_dir, "t5")
    state["spend_usd"] = 5.0
    budget.save_apify_state(cfg.careeros_dir, state)

    result = runner.invoke(app, ["discover", "--date", "t5", "--ignore-budget"])
    assert result.exit_code == 0, result.output
    assert paid.fetch_called is True


def test_hard_provider_error_skips_that_provider_and_run_continues(tmp_path, monkeypatch):
    """A HARD failure from the actor/account itself (e.g. every rotated Apify
    token exhausted or out of balance) must be caught per-provider inside the
    "monthly" capability branch and turned into a skip — NOT let escape to
    discover()'s command-level handler, which would abort the whole
    multi-provider run and never even attempt the providers listed after it.
    Mirrors the weekly-quota guard's "tell the user and move on" behavior."""
    monkeypatch.chdir(tmp_path)
    broke = _FakeProvider(
        "fake-broke", [_job("Never")], fetch_error=ProviderError("fake-broke: all tokens exhausted"),
    )
    after = _FakeProvider("fake-after", [_job("Still runs")])
    monkeypatch.setitem(registry._REGISTRY, "fake-broke", broke)
    monkeypatch.setitem(registry._REGISTRY, "fake-after", after)
    (tmp_path / ".careeros").mkdir()
    (tmp_path / ".careeros" / "config.yaml").write_text(
        "providers:\n"
        "  fake-broke:\n    enabled: true\n    max_monthly_budget_usd: 10.0\n"
        "  fake-after:\n    enabled: true\n"
    )

    result = runner.invoke(app, ["discover", "--date", "t8"])
    assert result.exit_code == 0, result.output
    assert broke.fetch_called is True  # fetch() WAS attempted, and raised
    assert after.fetch_called is True  # the run continued to the next provider

    raw = json.loads((tmp_path / ".careeros/runs/t8/01_discover/raw.json").read_text())
    assert raw["meta"]["fake-broke"]["skipped"] is True
    assert "exhausted" in raw["meta"]["fake-broke"]["skip_reason"]
    assert raw["meta"]["fake-after"]["skipped"] is False
    assert len(raw["items"]["fake-after"]) == 1


def test_weekly_capability_hard_error_skips_fantastic_jobs_and_run_continues(tmp_path, monkeypatch):
    """Regression for a real bug found in live use 2026-07-11: Fantastic
    Jobs (the only provider on the "weekly" capability path, resolved via
    cfg.api which has a "plan" key) hit an account-level HTTP 403 (usage
    meter exceeded). Before this fix, that ProviderError had no per-provider
    catch inside the weekly branch, so it escaped to discover()'s
    command-level handler and aborted the ENTIRE multi-provider run —
    RemoteOK/We Work Remotely/every paid provider never even got attempted,
    even though they were healthy. Must behave exactly like the monthly
    branch's existing "hard error -> skip, run continues" guarantee."""
    monkeypatch.chdir(tmp_path)
    broke = _FakeProvider(
        "fantastic-jobs", [_job("Never")],
        fetch_error=ProviderError("fantastic-jobs (active-ats): API key rejected (HTTP 403)"),
    )
    after = _FakeProvider("fake-after3", [_job("Still runs")])
    monkeypatch.setitem(registry._REGISTRY, "fantastic-jobs", broke)
    monkeypatch.setitem(registry._REGISTRY, "fake-after3", after)
    (tmp_path / ".careeros").mkdir()
    (tmp_path / ".careeros" / "config.yaml").write_text(
        "providers:\n"
        "  fantastic-jobs:\n    enabled: true\n"
        "  fake-after3:\n    enabled: true\n"
    )

    result = runner.invoke(app, ["discover", "--date", "t9"])
    assert result.exit_code == 0, result.output
    assert broke.fetch_called is True  # fetch() WAS attempted, and raised
    assert after.fetch_called is True  # the run continued to the next provider, not aborted

    raw = json.loads((tmp_path / ".careeros/runs/t9/01_discover/raw.json").read_text())
    assert raw["meta"]["fantastic-jobs"]["skipped"] is True
    assert "403" in raw["meta"]["fantastic-jobs"]["skip_reason"]
    assert raw["meta"]["fake-after3"]["skipped"] is False
    assert len(raw["items"]["fake-after3"]) == 1


def test_explicit_provider_flag_forces_single_provider_ignoring_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    a = _FakeProvider("fake-a2", [_job("A")])
    b = _FakeProvider("fake-b2", [_job("B")])
    monkeypatch.setitem(registry._REGISTRY, "fake-a2", a)
    monkeypatch.setitem(registry._REGISTRY, "fake-b2", b)
    _write_config(tmp_path, "  fake-a2:\n    enabled: true\n  fake-b2:\n    enabled: true\n")

    result = runner.invoke(app, ["discover", "--date", "t6", "--provider", "fake-b2"])
    assert result.exit_code == 0, result.output
    assert a.fetch_called is False
    assert b.fetch_called is True

    raw = json.loads((tmp_path / ".careeros/runs/t6/01_discover/raw.json").read_text())
    assert raw["providers"] == ["fake-b2"]


def test_no_enabled_providers_exits_cleanly(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".careeros").mkdir()
    (tmp_path / ".careeros" / "config.yaml").write_text("providers:\n  fantastic-jobs:\n    enabled: false\n")

    result = runner.invoke(app, ["discover", "--date", "t7"])
    assert result.exit_code == 1
    assert "nothing to do" in result.output


def test_every_provider_skipped_prints_plain_english_reasons_and_exits_zero(tmp_path, monkeypatch):
    """When every ENABLED provider ends up skipped (not disabled — actually
    enabled but each failed validate()/its guard/fetch()), discover must not
    look like it silently succeeded with real jobs: it should spell out
    every provider's skip reason in plain text. Exit code stays 0 (same as
    a single skipped provider, e.g. the monthly-budget-guard test above) —
    an all-skipped run is informational, not an error, by design; skills
    reading this output decide whether that's acceptable to continue from,
    not the CLI itself."""
    monkeypatch.chdir(tmp_path)
    broken = _FakeProvider("fake-brokenA", [_job("Never")], validate_errors=["no token configured"])
    also_broken = _FakeProvider("fake-brokenB", [_job("Never")], validate_errors=["missing credentials"])
    monkeypatch.setitem(registry._REGISTRY, "fake-brokenA", broken)
    monkeypatch.setitem(registry._REGISTRY, "fake-brokenB", also_broken)
    _write_config(tmp_path, "  fake-brokenA:\n    enabled: true\n  fake-brokenB:\n    enabled: true\n")

    result = runner.invoke(app, ["discover", "--date", "t10"])
    assert result.exit_code == 0, result.output
    assert "every enabled provider was skipped" in result.output.lower()
    assert "fake-brokenA: no token configured" in result.output
    assert "fake-brokenB: missing credentials" in result.output


# ── v1.3: concurrent fetch (preflight / fetch / bookkeeping phases) ──────

def test_none_capability_hard_error_skips_that_provider_and_run_continues(tmp_path, monkeypatch):
    """Regression: the "none" capability branch (RemoteOK/We Work Remotely's
    shape — no plan/max_monthly_budget_usd key) previously had NO try/except
    around fetch() at all, unlike the weekly/monthly branches — a real
    network/timeout ProviderError from a free provider would silently abort
    the WHOLE multi-provider discover run. Found while refactoring for
    concurrency; now consistent with every other capability."""
    monkeypatch.chdir(tmp_path)
    broke = _FakeProvider(
        "fake-free-broke", [_job("Never")],
        fetch_error=ProviderError("fake-free-broke: network timeout"),
    )
    after = _FakeProvider("fake-free-after", [_job("Still runs")])
    monkeypatch.setitem(registry._REGISTRY, "fake-free-broke", broke)
    monkeypatch.setitem(registry._REGISTRY, "fake-free-after", after)
    _write_config(tmp_path, "  fake-free-broke:\n    enabled: true\n  fake-free-after:\n    enabled: true\n")

    result = runner.invoke(app, ["discover", "--date", "t11"])
    assert result.exit_code == 0, result.output
    assert broke.fetch_called is True
    assert after.fetch_called is True

    raw = json.loads((tmp_path / ".careeros/runs/t11/01_discover/raw.json").read_text())
    assert raw["meta"]["fake-free-broke"]["skipped"] is True
    assert "timeout" in raw["meta"]["fake-free-broke"]["skip_reason"]
    assert raw["meta"]["fake-free-after"]["skipped"] is False


def test_concurrent_fetch_still_merges_in_config_order_regardless_of_completion_order(tmp_path, monkeypatch):
    """The provider LISTED FIRST in config finishes fetching LAST (via a
    sleep) — merge order in raw.json must still follow config order, not
    completion order, since dedupe's "keep first" contract depends on it."""
    import time as _time

    class _SlowFakeProvider(_FakeProvider):
        def __init__(self, provider_id, items, *, sleep_s=0.0):
            super().__init__(provider_id, items)
            self._sleep_s = sleep_s

        def fetch(self, config, *, limit=100, search="", query=None):
            _time.sleep(self._sleep_s)
            return super().fetch(config, limit=limit, search=search, query=query)

    slow_first = _SlowFakeProvider("fake-slow-first", [_job("First-listed")], sleep_s=0.15)
    fast_second = _SlowFakeProvider("fake-fast-second", [_job("Second-listed")], sleep_s=0.0)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(registry._REGISTRY, "fake-slow-first", slow_first)
    monkeypatch.setitem(registry._REGISTRY, "fake-fast-second", fast_second)
    _write_config(
        tmp_path,
        "  fake-slow-first:\n    enabled: true\n  fake-fast-second:\n    enabled: true\n",
    )

    result = runner.invoke(app, ["discover", "--date", "t12"])
    assert result.exit_code == 0, result.output

    raw = json.loads((tmp_path / ".careeros/runs/t12/01_discover/raw.json").read_text())
    # fake-fast-second's fetch() returns almost immediately and fake-slow-first's
    # takes 150ms, so completion order is [fast, slow] — but raw.json's
    # provider order must still be config order: [slow, fast].
    assert raw["providers"] == ["fake-slow-first", "fake-fast-second"]


def test_concurrent_fetches_actually_overlap_in_wall_clock(tmp_path, monkeypatch):
    """Three providers each sleep 150ms inside fetch(). Serial execution
    would take >=450ms; concurrent execution (discovery_max_workers default
    4) should take well under that — proving the fetches really run in
    parallel, not just that the code still produces correct results."""
    import time as _time

    class _SlowFakeProvider(_FakeProvider):
        def fetch(self, config, *, limit=100, search="", query=None):
            _time.sleep(0.15)
            return super().fetch(config, limit=limit, search=search, query=query)

    monkeypatch.chdir(tmp_path)
    names = ["fake-par-a", "fake-par-b", "fake-par-c"]
    for name in names:
        monkeypatch.setitem(registry._REGISTRY, name, _SlowFakeProvider(name, [_job("x")]))
    _write_config(tmp_path, "".join(f"  {n}:\n    enabled: true\n" for n in names))

    start = _time.time()
    result = runner.invoke(app, ["discover", "--date", "t13"])
    elapsed = _time.time() - start

    assert result.exit_code == 0, result.output
    assert elapsed < 0.4, f"expected concurrent fetches to overlap, took {elapsed:.2f}s"


def test_max_workers_1_forces_serial_and_still_merges_correctly(tmp_path, monkeypatch):
    """discovery_max_workers: 1 restores today's fully-serial behavior —
    still correct, just no overlap."""
    import time as _time

    class _SlowFakeProvider(_FakeProvider):
        def fetch(self, config, *, limit=100, search="", query=None):
            _time.sleep(0.1)
            return super().fetch(config, limit=limit, search=search, query=query)

    monkeypatch.chdir(tmp_path)
    names = ["fake-serial-a", "fake-serial-b"]
    for name in names:
        monkeypatch.setitem(registry._REGISTRY, name, _SlowFakeProvider(name, [_job("x")]))
    (tmp_path / ".careeros").mkdir()
    (tmp_path / ".careeros" / "config.yaml").write_text(
        "discovery_max_workers: 1\n"
        "providers:\n" + "".join(f"  {n}:\n    enabled: true\n" for n in names)
    )

    start = _time.time()
    result = runner.invoke(app, ["discover", "--date", "t14"])
    elapsed = _time.time() - start

    assert result.exit_code == 0, result.output
    assert elapsed >= 0.2, f"expected serial (max_workers=1) fetches NOT to overlap, took {elapsed:.2f}s"
    raw = json.loads((tmp_path / ".careeros/runs/t14/01_discover/raw.json").read_text())
    assert raw["providers"] == ["fake-serial-a", "fake-serial-b"]


def test_multiple_monthly_providers_concurrent_spend_all_recorded_no_lost_update(tmp_path, monkeypatch):
    """Three monthly-capability (Apify-style) providers, each costing real
    money, fetched CONCURRENTLY — bookkeeping happens serially afterward
    (see _record_provider_consumption), so all three spends must land in
    apify_budget.json with none lost to a write race."""

    class _PaidFakeProvider(_FakeProvider):
        def fetch(self, config, *, limit=100, search="", query=None):
            return ProviderResult(
                provider=self.id, items=list(self._items), cost_usd=1.0,
                requests=1, records=len(self._items), seconds=0.01,
            )

    monkeypatch.chdir(tmp_path)
    names = ["fake-spend-a", "fake-spend-b", "fake-spend-c"]
    for name in names:
        monkeypatch.setitem(registry._REGISTRY, name, _PaidFakeProvider(name, [_job(name)]))
    (tmp_path / ".careeros").mkdir()
    (tmp_path / ".careeros" / "config.yaml").write_text(
        "providers:\n" + "".join(
            f"  {n}:\n    enabled: true\n    max_monthly_budget_usd: 100\n" for n in names
        )
    )

    result = runner.invoke(app, ["discover", "--date", "t15"])
    assert result.exit_code == 0, result.output

    from careeros.config import load_config
    cfg = load_config()
    state = budget.load_apify_state(cfg.careeros_dir, "t15")
    # Each of the 3 providers spent $1.00 — total must be exactly $3.00, not
    # less (a lost update would silently under-count).
    assert state["spend_usd"] == 3.0
