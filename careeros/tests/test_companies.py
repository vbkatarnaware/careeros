"""Tests for careeros/companies.py — ported from test_discovery_registry.py's
normalize_name coverage ahead of that legacy module's deletion."""

from __future__ import annotations

from careeros.companies import normalize_name


def test_normalize_name_lowercases_and_strips_punctuation():
    assert normalize_name("Stripe, Corp.") == "stripe"


def test_normalize_name_collapses_whitespace():
    assert normalize_name("  Stripe   Corp  ") == "stripe"


def test_normalize_name_strips_legal_suffix():
    assert normalize_name("BJAK Sdn Bhd") == "bjak"
    assert normalize_name("BJAK") == "bjak"


def test_normalize_name_merges_punctuation_and_suffix_variants():
    # Two spellings of the same company must collapse to the same key.
    assert normalize_name("Stripe, Corp.") == normalize_name("  Stripe   Corp  ")
