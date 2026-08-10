"""Company-identity normalization, shared by the gate fairness cap
(cli/gate_evaluate.py), and the watchlist/registry providers.

Salvaged from the legacy SQLite discovery registry (providers/legacy/
discovery/registry.py) ahead of that module's deletion — this function had
no dependency on SQLite or anything else in that package, it was just
colocated with it.
"""

from __future__ import annotations

import re

_LEGAL_SUFFIXES = [
    "private limited", "public limited company", "sdn bhd", "pvt ltd",
    "inc", "llc", "ltd", "corp", "corporation", "co", "plc", "gmbh", "sa", "ag", "bv", "nv",
]


def normalize_name(name: str) -> str:
    """Lowercase, punctuation-stripped, legal-suffix-stripped form used as
    the company identity key. Deliberately NOT full fuzzy matching (no edit
    distance, no aliasing table) — just the two cheap, predictable
    transforms above. Only ever MERGES more variants together; never
    un-merges an existing match, so tightening this is always backward
    compatible with anything keyed on it."""
    lowered = name.strip().lower()
    stripped = re.sub(r"[^\w\s]", "", lowered)
    collapsed = re.sub(r"\s+", " ", stripped).strip()
    for suffix in _LEGAL_SUFFIXES:
        if collapsed.endswith(" " + suffix):
            return collapsed[: -(len(suffix) + 1)].strip()
    return collapsed
