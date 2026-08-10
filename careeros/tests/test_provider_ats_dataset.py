"""Tests for careeros/providers/ats_dataset.py — the free ats-scrapers-backed
discovery source (v1.9, replaces fantastic-jobs as the default). Offline by
default; the live smoke test at the bottom is opt-in via CAREEROS_LIVE_TESTS=1
since it hits the real hosted dataset.

Fixture dates are rewritten to be relative to "now" at test-run time (see
`_fixture_rows`) rather than trusting the static JSON's baked-in dates — a
hardcoded near-present date would silently become "stale" and break the
freshness assertions the moment real wall-clock time moves past it.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from careeros.config import Config
from careeros.models import Profile
from careeros.pipeline.normalize import normalize_one
from careeros.providers.ats_dataset import (
    PROVIDER,
    GeoSpec,
    build_geo_spec,
    clean_row,
    matched_geo_tier,
    row_is_fresh,
    row_matches_geo,
    seniority_excluded,
    to_job_dict,
    years_exceed,
    years_required,
    _row_looks_remote_by_text,
    _salary,
    _title_excluded,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "ats_dataset_rows.json"


def _cfg() -> Config:
    return Config(threshold=4.0, consider_threshold=3.5, gate_batch_size=50, description_max_chars=4000)


def _profile(**overrides) -> Profile:
    base = dict(
        version=1, candidate={}, headline="", targets=[], experience=[],
        role_priorities=["Product Manager", "Founder's Office"],
        work_mode_priority=["india_remote", "mumbai_onsite"],
        location={"onsite_ok": ["Mumbai"]},
        deal_breakers={"min_years_ok": 3},
    )
    base.update(overrides)
    return Profile(**base)


def _fixture_rows() -> list[dict]:
    """Loads the fixture and rewrites `posted_at` relative to now — see
    module docstring. Every row is "fresh" (1 day ago) except the row named
    "stale-job", which is pinned 40 days ago (older than the 30-day
    default window)."""
    with open(FIXTURE_PATH) as f:
        rows = json.load(f)
    now = datetime.now(timezone.utc)
    for row in rows:
        if row["global_id"] == "stale-job":
            row["posted_at"] = (now - timedelta(days=40)).isoformat()
        else:
            row["posted_at"] = (now - timedelta(days=1)).isoformat()
    return rows


def _row(global_id: str) -> dict:
    return next(r for r in _fixture_rows() if r["global_id"] == global_id)


def _write_profile(tmp_path, profile: Profile) -> None:
    careeros_dir = tmp_path / ".careeros"
    careeros_dir.mkdir(parents=True, exist_ok=True)
    with open(careeros_dir / "profile.yaml", "w") as f:
        import yaml
        yaml.safe_dump(
            {k: v for k, v in profile.__dict__.items() if k != "experience"} | {"experience": []},
            f,
        )


# ── build_geo_spec / row_matches_geo ──────────────────────────────────

def test_build_geo_spec_parses_remote_and_onsite_tiers():
    spec = build_geo_spec(_profile())
    assert spec.remote_places == ["india"]
    assert spec.onsite_cities == ["Mumbai"]
    assert not spec.remote_anywhere
    assert not spec.unrestricted


def test_build_geo_spec_global_remote_tier():
    spec = build_geo_spec(_profile(work_mode_priority=["global_remote"], location={}))
    assert spec.remote_anywhere is True


def test_build_geo_spec_no_tiers_is_unrestricted():
    spec = build_geo_spec(_profile(work_mode_priority=[], location={}))
    assert spec.unrestricted is True


def test_row_matches_geo_india_remote_row():
    assert row_matches_geo(_row("keka-1"), build_geo_spec(_profile())) is True


def test_row_matches_geo_wrong_country_remote_row():
    assert row_matches_geo(_row("gh-us-remote"), build_geo_spec(_profile())) is False


def test_row_matches_geo_onsite_city_match():
    assert row_matches_geo(_row("lever-mmr-onsite"), build_geo_spec(_profile())) is True


def test_row_matches_geo_onsite_city_miss():
    assert row_matches_geo(_row("lever-blr-onsite"), build_geo_spec(_profile())) is False


def test_row_matches_geo_global_remote_only_matches_blank_country():
    """global_remote is deliberately conservative — NOT "match every remote
    row" (see row_matches_geo's docstring for the live evidence this was
    fixed against: region is always empty, and text-based "worldwide"
    matching produced false positives from company names/boilerplate)."""
    spec = build_geo_spec(_profile(work_mode_priority=["global_remote"], location={}))
    blank_country_row = {"is_remote": True, "country_iso": "", "location": "Remote"}
    assert row_matches_geo(blank_country_row, spec) is True


def test_row_matches_geo_global_remote_excludes_country_locked_rows():
    spec = build_geo_spec(_profile(work_mode_priority=["global_remote"], location={}))
    assert row_matches_geo(_row("gh-us-remote"), spec) is False


def test_row_matches_geo_unrestricted_accepts_anything():
    spec = GeoSpec(unrestricted=True)
    assert row_matches_geo(_row("gh-us-remote"), spec) is True


# ── is_remote=None reachability fallback ────────────────────────────────
# Greenhouse leaves `is_remote` unpopulated on ~100% of its rows (measured
# live) — without this fallback, india_remote/global_remote are silently
# unreachable for that entire platform regardless of profile config.

def test_row_looks_remote_by_text_matches_location():
    assert _row_looks_remote_by_text({"location": "Remote, India"}) is True


def test_row_looks_remote_by_text_ignores_description():
    row = {"location": "Bengaluru, India", "description": "remote team collaboration tools"}
    assert _row_looks_remote_by_text(row) is False


def test_row_matches_geo_null_remote_with_text_signal_matches_india():
    assert row_matches_geo(_row("gh-null-remote-india"), build_geo_spec(_profile())) is True


def test_row_matches_geo_null_remote_no_text_signal_falls_back_to_onsite():
    # No remote text signal, and Bengaluru isn't in onsite_ok (Mumbai only)
    assert row_matches_geo(_row("gh-null-onsite-non-mmr"), build_geo_spec(_profile())) is False


def test_row_matches_geo_null_remote_description_only_does_not_match():
    # "remote" only appears in `description` — must not trigger the fallback
    row = _row("gh-null-remote-only-in-description")
    assert row_matches_geo(row, build_geo_spec(_profile())) is False


def test_row_matches_geo_null_remote_text_signal_respects_global_remote():
    spec = build_geo_spec(_profile(work_mode_priority=["global_remote"], location={}))
    row = dict(_row("gh-null-remote-india"))
    row["country_iso"] = ""  # global_remote only matches blank country_iso
    assert row_matches_geo(row, spec) is True


def test_row_matches_geo_explicit_false_is_not_overridden_by_text():
    # is_remote=False is trusted as-is; the text fallback only applies to None
    row = dict(_row("gh-null-remote-india"))
    row["is_remote"] = False
    row["location"] = "Remote, India"  # text says remote, but flag says no
    assert row_matches_geo(row, build_geo_spec(_profile())) is False


# ── matched_geo_tier ─────────────────────────────────────────────────────
# Real per-job tier provenance (v2.0) — every value here must agree with
# row_matches_geo's bool verdict on the same inputs (non-None <=> True).

def test_matched_geo_tier_india_remote():
    assert matched_geo_tier(_row("keka-1"), build_geo_spec(_profile())) == "india_remote"


def test_matched_geo_tier_global_remote():
    spec = build_geo_spec(_profile(work_mode_priority=["global_remote"], location={}))
    row = {"is_remote": True, "country_iso": "", "location": "Remote"}
    assert matched_geo_tier(row, spec) == "global_remote"


def test_matched_geo_tier_onsite_match():
    assert matched_geo_tier(_row("lever-mmr-onsite"), build_geo_spec(_profile())) == "onsite"


def test_matched_geo_tier_no_match_returns_none():
    assert matched_geo_tier(_row("lever-blr-onsite"), build_geo_spec(_profile())) is None
    assert matched_geo_tier(_row("gh-us-remote"), build_geo_spec(_profile())) is None


def test_matched_geo_tier_unrestricted():
    spec = GeoSpec(unrestricted=True)
    assert matched_geo_tier(_row("gh-us-remote"), spec) == "unrestricted"


def test_matched_geo_tier_agrees_with_row_matches_geo_bool():
    """matched_geo_tier is None exactly when row_matches_geo is False, on
    every fixture row — the refactor must not change the bool predicate."""
    spec = build_geo_spec(_profile())
    for row in _fixture_rows():
        tier = matched_geo_tier(row, spec)
        assert (tier is not None) == row_matches_geo(row, spec), row["global_id"]


def test_matched_geo_tier_reconstructs_multiword_place_tier_name():
    spec = build_geo_spec(_profile(work_mode_priority=["united_states_remote"], location={}))
    row = {"is_remote": True, "country_iso": "US", "location": "Remote - US"}
    assert matched_geo_tier(row, spec) == "united_states_remote"


# ── row_is_fresh ───────────────────────────────────────────────────────

def test_row_is_fresh_within_window():
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    assert row_is_fresh(_row("keka-1"), cutoff) is True


def test_row_is_fresh_stale_row_excluded():
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    assert row_is_fresh(_row("stale-job"), cutoff) is False


def test_row_is_fresh_missing_posted_at():
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    assert row_is_fresh({}, cutoff) is False


# ── title exclusion / seniority / years ────────────────────────────────

def test_title_excluded_matches_default_exclusion():
    row = _row("marketing-excluded")
    assert _title_excluded(row["title"], ["marketing"]) is True


def test_title_excluded_no_match():
    assert _title_excluded("Product Manager", ["marketing", "intern"]) is False


def test_seniority_excluded_principal():
    assert seniority_excluded(_row("principal-excluded")["title"]) is True


def test_seniority_excluded_keeps_senior_lead_staff():
    """This project's own measurement (2026-08-04, 510 India jobs): Senior/
    Lead/Staff convert at 26.7%, excluding them costs 17pts of recall for
    zero precision gain — see module docstring."""
    assert seniority_excluded("Senior Product Manager") is False
    assert seniority_excluded("Lead Product Manager") is False
    assert seniority_excluded("Staff Product Manager") is False


def test_years_required_extracts_stated_minimum():
    row = _row("years-excluded")
    assert years_required(row["description"]) == 8


def test_years_required_ignores_unrelated_number():
    assert years_required("Founded 5 years ago, we are growing fast.") is None


def test_years_exceed_true_when_clearly_over_floor():
    row = _row("years-excluded")
    assert years_exceed(row["description"], min_years_ok=3) is True


def test_years_exceed_false_when_ambiguous():
    assert years_exceed("Great opportunity for product folks.", min_years_ok=3) is False


def test_years_exceed_false_when_within_tolerance():
    # "3+ years" against a floor of 3 (+1 tolerance) must NOT exceed
    row = _row("keka-1")
    assert years_exceed(row["description"], min_years_ok=3) is False


# ── clean_row: the NaN/numpy/Timestamp safety net ──────────────────────

def test_clean_row_converts_float_nan_to_none():
    assert clean_row({"a": float("nan")}) == {"a": None}


def test_clean_row_converts_numpy_like_scalar_via_item():
    class FakeNumpyInt:
        def item(self):
            return 42

    assert clean_row({"a": FakeNumpyInt()}) == {"a": 42}


def test_clean_row_converts_nat_like_object_to_none():
    class FakeNaT:
        pass

    FakeNaT.__name__ = "NaTType"
    assert clean_row({"a": FakeNaT()}) == {"a": None}


def test_clean_row_converts_timestamp_like_object_via_isoformat():
    class FakeTimestamp:
        def isoformat(self):
            return "2026-08-08T00:00:00"

    assert clean_row({"a": FakeTimestamp()}) == {"a": "2026-08-08T00:00:00"}


def test_clean_row_passes_through_plain_values():
    assert clean_row({"a": "text", "b": 5, "c": None, "d": True}) == {"a": "text", "b": 5, "c": None, "d": True}


def test_clean_row_result_is_json_serializable():
    from careeros.models import dumps

    class FakeNumpyFloat:
        def item(self):
            return 1.5

    row = {"a": float("nan"), "b": FakeNumpyFloat(), "c": "ok"}
    dumps(clean_row(row))  # must not raise


# ── _salary: the 12x period-ambiguity trap ─────────────────────────────

def test_salary_none_when_period_unmapped():
    row = _row("salary-unmapped-period")
    assert _salary(row) is None


def test_salary_maps_correctly_with_known_period():
    row = _row("salary-mapped-year")
    assert _salary(row) == {"min": 1800000, "max": 2400000, "currency": "INR", "unit": "year"}


def test_salary_none_when_no_amount():
    assert _salary({"salary_min": None, "salary_max": None, "salary_period": "year"}) is None


# ── to_job_dict ────────────────────────────────────────────────────────

def test_to_job_dict_maps_valid_row():
    mapped = to_job_dict(_row("keka-1"))
    assert mapped["title"] == "Senior Product Manager"
    assert mapped["company"] == "Acme India"
    assert mapped["apply_url"].startswith("https://acme.keka.com/")
    assert mapped["remote"] is True
    assert mapped["employment_type"] == "full_time"


def test_to_job_dict_apostrophe_title_preserved():
    mapped = to_job_dict(_row("founders-office-apostrophe"))
    assert mapped is not None
    assert "Founder" in mapped["title"]


def test_to_job_dict_returns_none_for_missing_apply_url():
    assert to_job_dict(_row("no-apply-url")) is None


def test_to_job_dict_falls_back_to_url_when_apply_url_absent():
    row = dict(_row("keka-1"))
    row["apply_url"] = None
    mapped = to_job_dict(row)
    assert mapped is not None
    assert mapped["apply_url"] == row["url"]


# ── normalize_one: apply_url is never LinkedIn ─────────────────────────

def test_normalized_job_apply_url_is_never_linkedin():
    job = normalize_one(_row("keka-1"), PROVIDER, source="ats-dataset")
    assert job is not None
    assert "linkedin.com" not in job.apply_url
    assert job.apply_url.startswith("https://acme.keka.com/")


# ── validate() ─────────────────────────────────────────────────────────

def test_validate_reports_missing_profile(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    problems = PROVIDER.validate(_cfg())
    assert any("profile.yaml" in p for p in problems)


def test_validate_clean_when_profile_present(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_profile(tmp_path, _profile())
    problems = PROVIDER.validate(_cfg())
    assert problems == []


def test_validate_reports_missing_package(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_profile(tmp_path, _profile())
    with patch("careeros.providers.ats_dataset.importlib.util.find_spec", return_value=None):
        problems = PROVIDER.validate(_cfg())
    assert any("pip install" in p for p in problems)


# ── fetch(): pandas tier — full pipeline against the fixture ──────────

pd = pytest.importorskip("pandas")


def _patched_load_slice(ats: str):
    return pd.DataFrame(_fixture_rows())


def test_fetch_full_pipeline_keeps_only_expected_rows(tmp_path, monkeypatch):
    """The one true end-to-end check: of 15 fixture rows, exactly the 7
    that should survive title+freshness+exclusion+seniority+years+geo
    filtering do, and the other 8 are dropped — each for a distinct,
    named reason (wrong country, onsite-city miss, stale, excluded title,
    seniority, years-of-experience, no remote signal at all, remote
    mentioned only in description)."""
    monkeypatch.chdir(tmp_path)
    _write_profile(tmp_path, _profile())
    cfg = _cfg()

    with patch("careeros.providers.ats_dataset._load_slice", side_effect=_patched_load_slice):
        result = PROVIDER.fetch(cfg, limit=100)

    assert not result.skipped
    surviving_ids = {r["global_id"] for r in result.items}
    assert surviving_ids == {
        "keka-1", "lever-mmr-onsite", "founders-office-apostrophe",
        "no-apply-url", "salary-unmapped-period", "salary-mapped-year",
        "gh-null-remote-india",
    }


def test_fetch_populates_real_tiers_not_a_flat_default(tmp_path, monkeypatch):
    """v2.0: `fetch()` must return real per-item geo tiers — the whole
    reason `default: 413`-style reporting existed was that no provider ever
    populated this field."""
    monkeypatch.chdir(tmp_path)
    _write_profile(tmp_path, _profile())
    cfg = _cfg()

    with patch("careeros.providers.ats_dataset._load_slice", side_effect=_patched_load_slice):
        result = PROVIDER.fetch(cfg, limit=100)

    assert len(result.tiers) == len(result.items)
    tiers_by_id = dict(zip((r["global_id"] for r in result.items), (t[0] for t in result.tiers)))
    assert tiers_by_id["keka-1"] == "india_remote"
    assert tiers_by_id["lever-mmr-onsite"] == "onsite"
    assert "default" not in {t[0] for t in result.tiers}
    # _geo_tier is bookkeeping only — must not leak into the returned items
    assert all("_geo_tier" not in item for item in result.items)


def test_fetch_respects_limit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_profile(tmp_path, _profile())
    cfg = _cfg()

    with patch("careeros.providers.ats_dataset._load_slice", side_effect=_patched_load_slice):
        result = PROVIDER.fetch(cfg, limit=2)

    assert len(result.items) == 2


def test_fetch_warns_when_truncated_by_limit(tmp_path, monkeypatch):
    """Silent truncation was the exact failure mode that made the recall
    cap invisible — a truncating fetch must now say so in `warnings`."""
    monkeypatch.chdir(tmp_path)
    _write_profile(tmp_path, _profile())
    cfg = _cfg()

    with patch("careeros.providers.ats_dataset._load_slice", side_effect=_patched_load_slice):
        result = PROVIDER.fetch(cfg, limit=2)
    assert any("kept only the 2 most recent" in w for w in result.warnings)


def test_fetch_no_warning_when_under_limit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_profile(tmp_path, _profile())
    cfg = _cfg()

    with patch("careeros.providers.ats_dataset._load_slice", side_effect=_patched_load_slice):
        result = PROVIDER.fetch(cfg, limit=1000)
    assert not any("kept only" in w for w in result.warnings)


def test_fetch_sorts_by_posted_at_descending(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_profile(tmp_path, _profile())
    cfg = _cfg()

    with patch("careeros.providers.ats_dataset._load_slice", side_effect=_patched_load_slice):
        result = PROVIDER.fetch(cfg, limit=100)

    dates = [r["posted_at"] for r in result.items]
    assert dates == sorted(dates, reverse=True)


def test_fetch_records_warning_when_a_slice_fails_without_crashing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_profile(tmp_path, _profile())
    scoped_slices = ["keka", "darwinbox", "greenhouse", "lever", "ashby"]
    cfg = Config(
        threshold=4.0, consider_threshold=3.5, gate_batch_size=50, description_max_chars=4000,
        providers={"ats-dataset": {"ats": scoped_slices}},
    )

    def _flaky_load(ats: str):
        if ats == "keka":
            raise RuntimeError("boom")
        return pd.DataFrame(_fixture_rows())

    with patch("careeros.providers.ats_dataset._load_slice", side_effect=_flaky_load):
        result = PROVIDER.fetch(cfg, limit=100)

    assert not result.skipped
    assert any("keka" in w for w in result.warnings)
    assert result.requests == len(scoped_slices) - 1  # keka excluded (failed to load)


def test_fetch_result_items_are_json_serializable(tmp_path, monkeypatch):
    """Guards clean_row's contract end to end: models.dumps() must never
    raise on what fetch() returns."""
    from careeros.models import dumps

    monkeypatch.chdir(tmp_path)
    _write_profile(tmp_path, _profile())
    cfg = _cfg()

    with patch("careeros.providers.ats_dataset._load_slice", side_effect=_patched_load_slice):
        result = PROVIDER.fetch(cfg, limit=100)

    dumps(result.items)  # must not raise


def test_fetch_skips_when_ats_scrapers_not_installed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_profile(tmp_path, _profile())
    cfg = _cfg()

    with patch("careeros.providers.ats_dataset.importlib.util.find_spec", return_value=None):
        result = PROVIDER.fetch(cfg, limit=10)

    assert result.skipped
    assert "pip install" in result.skip_reason


# ── live smoke test (opt-in, real network) ────────────────────────────

@pytest.mark.skipif(
    not os.environ.get("CAREEROS_LIVE_TESTS"),
    reason="hits the real ats-scrapers hosted dataset — set CAREEROS_LIVE_TESTS=1 to run",
)
def test_live_fetch_against_real_keka_slice(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_profile(tmp_path, _profile(work_mode_priority=["india_remote"], location={}))
    cfg = _cfg()

    with patch("careeros.providers.ats_dataset._DEFAULT_SLICES", ["keka"]):
        result = PROVIDER.fetch(cfg, limit=10)

    assert not result.skipped
    for raw in result.items:
        mapped = to_job_dict(raw)
        if mapped is not None:
            assert "linkedin.com" not in mapped["apply_url"]
