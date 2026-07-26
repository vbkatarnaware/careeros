"""Provider registry. The pipeline calls `get(name)`, never a provider module
directly — this is what makes providers pluggable without touching pipeline
code. Adding a provider = write the file, add one line here.

v1.7: deliberately down to ONE source. Seven providers (RemoteOK, We Work
Remotely, Glassdoor, ZipRecruiter, Naukri, Foundit, Indeed) plus the legacy
Apify actor were removed after two weeks of live evidence — see README.md's
"Evaluated and removed" section for the per-source numbers. This file being
one entry long is a decision about which sources are worth running, not a
change to the architecture that lets you add more.
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
