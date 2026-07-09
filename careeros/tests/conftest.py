"""Shared test factories. Kept minimal — these build the smallest valid
Job/Profile that satisfies the dataclasses' required fields, so each test
only overrides what it actually cares about."""

from __future__ import annotations

from careeros.models import Job, Profile


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
