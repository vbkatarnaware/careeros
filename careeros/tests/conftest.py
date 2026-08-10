"""Shared test factories. Kept minimal — these build the smallest valid
Job/Profile that satisfies the dataclasses' required fields, so each test
only overrides what it actually cares about."""

from __future__ import annotations

import importlib.util

import pytest

from careeros.models import Job, Profile

# Shared skip guard for tests whose SUBJECT is real ats_scrapers integration
# (not just an incidental import) — the optional `ats-dataset` extra, absent
# from a minimal `pip install -e ".[dev,resume,apply]"` (e.g. CI's default
# matrix). Consistent with how production code itself checks availability
# (see providers/ats_watchlist.py's `validate()`, providers/ats_dataset.py's
# `validate()`) — a missing extra is a clean skip, never a crash, here or
# there.
requires_ats_scrapers = pytest.mark.skipif(
    importlib.util.find_spec("ats_scrapers") is None,
    reason="requires the optional ats-dataset extra: pip install -e \".[ats-dataset]\"",
)


def make_job(**overrides) -> Job:
    defaults = dict(
        id="job-1",
        source="fantastic-jobs",
        title="Product Manager",
        company="Acme",
        apply_url="https://example.com/jobs/1",
        location="Mumbai, Maharashtra, India",
        remote=False,
    )
    defaults.update(overrides)
    return Job(**defaults)


def make_profile(**overrides) -> Profile:
    defaults = dict(
        version=1,
        candidate={"full_name": "Test Candidate", "email": "t@example.com"},
        headline="Product Manager",
        targets=["product-manager"],
        experience=[],
        location={"remote": "preferred", "onsite_ok": ["Mumbai", "Navi Mumbai"]},
        comp={"floor_lpa": 15, "target_lpa": [20, 28], "currency": "INR"},
    )
    defaults.update(overrides)
    return Profile(**defaults)


FX_RATES = {"INR": 1.0, "USD": 83.0, "EUR": 90.0, "GBP": 105.0}
