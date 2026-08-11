"""Tests for the v2.2 `preferences` block — discovery-targeting signal only
(schemas/profile.schema.json, careeros/models.py's `Profile.preferences`).
Read only by careeros/ats_resolve.py + `watchlist discover`, never by
Gate/Evaluate/constraints — these tests only cover that it's a well-formed,
backward-compatible, optional part of the profile itself; scoring behavior
is out of scope here (preferences is deliberately never wired into it)."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from careeros.models import Profile

_SCHEMA_PATH = Path(__file__).resolve().parent.parent.parent / "schemas" / "profile.schema.json"


def _schema() -> dict:
    with open(_SCHEMA_PATH) as f:
        return json.load(f)


def _minimal_profile(**overrides) -> dict:
    base = {
        "version": 1,
        "candidate": {"full_name": "Jane Doe", "email": "jane@example.com"},
        "headline": "Product Manager",
        "targets": ["Product Manager"],
        "experience": [],
    }
    base.update(overrides)
    return base


def _validate(profile: dict) -> list[str]:
    return [e.message for e in jsonschema.Draft7Validator(_schema()).iter_errors(profile)]


# ── schema ────────────────────────────────────────────────────────────────

def test_schema_accepts_profile_without_preferences():
    assert _validate(_minimal_profile()) == []


def test_schema_accepts_profile_with_full_preferences_block():
    profile = _minimal_profile(preferences={
        "industries": ["fintech", "b2b saas"],
        "company_stage": ["seed", "series-a"],
        "target_companies": ["Acme"],
        "exclude_companies": ["Ghost Co"],
        "exclude_industries": ["gambling", "defense"],
    })
    assert _validate(profile) == []


def test_schema_accepts_empty_preferences_block():
    assert _validate(_minimal_profile(preferences={})) == []


def test_schema_rejects_unknown_key_inside_preferences():
    """Closed schema, per the project's own field-scoping convention —
    preferences is deliberately a fixed set of five arrays, no free-form
    escape hatch that could later grow into something Gate/Evaluate reads."""
    profile = _minimal_profile(preferences={"industries": ["fintech"], "unknown_field": ["x"]})
    assert _validate(profile) != []


def test_schema_rejects_non_string_array_items():
    profile = _minimal_profile(preferences={"industries": [1, 2]})
    assert _validate(profile) != []


# ── Profile.from_dict round-trip ────────────────────────────────────────

def test_profile_from_dict_without_preferences_defaults_to_empty_dict():
    profile = Profile.from_dict(_minimal_profile())
    assert profile.preferences == {}


def test_profile_from_dict_round_trips_preferences():
    prefs = {"industries": ["fintech"], "exclude_companies": ["Ghost Co"]}
    profile = Profile.from_dict(_minimal_profile(preferences=prefs))
    assert profile.preferences == prefs


def test_profile_from_dict_unknown_top_level_key_raises():
    """`Profile.from_dict`'s `**kwargs` splat onto the dataclass constructor
    means an unrecognized top-level key raises TypeError, not a silent
    drop — same backward-compat contract this field itself relies on."""
    with pytest.raises(TypeError):
        Profile.from_dict(_minimal_profile(totally_unknown_field="x"))
