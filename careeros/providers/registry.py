"""Provider registry. The pipeline calls `get(name)`, never a provider module
directly — this is what makes providers pluggable without touching pipeline
code. Adding a provider = write the file, add one line here.
"""

from __future__ import annotations

from careeros.providers.fantastic_jobs import PROVIDER as FANTASTIC_JOBS
from careeros.providers.legacy.fantastic_jobs_actor import PROVIDER as FANTASTIC_JOBS_ACTOR
from careeros.providers.foundit import PROVIDER as FOUNDIT
from careeros.providers.glassdoor import PROVIDER as GLASSDOOR
from careeros.providers.indeed import PROVIDER as INDEED
from careeros.providers.naukri import PROVIDER as NAUKRI
from careeros.providers.remoteok import PROVIDER as REMOTEOK
from careeros.providers.we_work_remotely import PROVIDER as WE_WORK_REMOTELY
from careeros.providers.ziprecruiter import PROVIDER as ZIPRECRUITER

_REGISTRY = {
    FANTASTIC_JOBS.id: FANTASTIC_JOBS,
    FANTASTIC_JOBS_ACTOR.id: FANTASTIC_JOBS_ACTOR,
    REMOTEOK.id: REMOTEOK,
    WE_WORK_REMOTELY.id: WE_WORK_REMOTELY,
    NAUKRI.id: NAUKRI,
    FOUNDIT.id: FOUNDIT,
    INDEED.id: INDEED,
    GLASSDOOR.id: GLASSDOOR,
    ZIPRECRUITER.id: ZIPRECRUITER,
}


def get(name: str):
    if name not in _REGISTRY:
        available = ", ".join(sorted(_REGISTRY))
        raise ValueError(f"Unknown provider '{name}'. Available: {available}")
    return _REGISTRY[name]


def list_providers() -> list[str]:
    return sorted(_REGISTRY)
