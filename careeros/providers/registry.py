"""Provider registry. The pipeline calls `get(name)`, never a provider module
directly — this is what makes providers pluggable without touching pipeline
code. Adding a provider = write the file, add one line here.
"""

from __future__ import annotations

from careeros.providers.fantastic_jobs import PROVIDER as FANTASTIC_JOBS

_REGISTRY = {
    FANTASTIC_JOBS.id: FANTASTIC_JOBS,
}


def get(name: str):
    if name not in _REGISTRY:
        available = ", ".join(sorted(_REGISTRY))
        raise ValueError(f"Unknown provider '{name}'. Available: {available}")
    return _REGISTRY[name]


def list_providers() -> list[str]:
    return sorted(_REGISTRY)
