"""Provider registry. The pipeline calls `get(name)`, never a provider module
directly — this is what makes providers pluggable without touching pipeline
code. Adding a provider = write the file, add one line here.

v1.7: deliberately down to ONE source. Seven providers (RemoteOK, We Work
Remotely, Glassdoor, ZipRecruiter, Naukri, Foundit, Indeed) plus the legacy
Apify actor were removed after two weeks of live evidence — see this
package's own README.md (careeros/providers/README.md)'s "Evaluated and
removed" section for the per-source numbers.

v1.8 (ATS discovery registry, REMOVED 2026-08-10): three hand-written
ATS-direct scrapers (`greenhouse`, `lever`, `ashby`) plus a SQLite
company/board registry to back them. Deleted after audit: every seeded
board was permanently `status='unverified'` on this project's own
checkout, and all three providers only ever read `status='live'` boards —
they were structurally skip-only and had never returned a single job.
Superseded by v1.9 (below) for the hosted-snapshot case and by v2.0's
`ats-watchlist` (using the same `ats-scrapers` library's 50+ maintained
adapters) for the targeted-scrape case — no functionality was lost, only
~1,500 lines of dead code and a SQLite dependency this project otherwise
has none of.

v1.9 (free ATS dataset): `ats-dataset` (careeros/providers/ats_dataset.py)
queries the free, open `ats-scrapers` hosted dataset — 65 ATS platforms
including India-specific ones (Darwinbox, Keka) the v1.8 registry never
covered, no company list to curate, no scraping infra of our own. This is
now the PRIMARY source (enabled by default, see
templates/config.example.yaml) — `fantastic-jobs` and the v1.8 trio are
disabled but kept registered here, which is the entire rollback mechanism:
flip two booleans in config.yaml, nothing else to restore.

v2.0 (Layer 2A watchlist): `ats-watchlist` (careeros/providers/
ats_watchlist.py) — a user-supplied list of specific companies scraped
live via the same `ats-scrapers` package's per-platform adapters, for
companies the Layer 1 hosted snapshot doesn't carry at all. Opt-in and
disabled by default (an empty watchlist is a normal, zero-cost state).

v2.2 (automated discovery): `careeros watchlist discover`
(careeros/cli/registry_cmd.py) + careeros/ats_resolve.py add profile-
driven, bounded, deterministically-validated company discovery ON TOP of
the watchlist — this provider's own `fetch()` is unchanged, it still only
ever scrapes what's already in `.careeros/watchlist.yaml`. See
docs/ats-registry.md's "Automated discovery (v2.2)" section.
"""

from __future__ import annotations

from careeros.providers.ats_dataset import PROVIDER as ATS_DATASET
from careeros.providers.ats_watchlist import PROVIDER as ATS_WATCHLIST
from careeros.providers.legacy.fantastic_jobs import PROVIDER as FANTASTIC_JOBS

_REGISTRY = {
    ATS_DATASET.id: ATS_DATASET,
    ATS_WATCHLIST.id: ATS_WATCHLIST,
    FANTASTIC_JOBS.id: FANTASTIC_JOBS,
}


def get(name: str):
    if name not in _REGISTRY:
        available = ", ".join(sorted(_REGISTRY))
        raise ValueError(f"Unknown provider '{name}'. Available: {available}")
    return _REGISTRY[name]


def list_providers() -> list[str]:
    return sorted(_REGISTRY)
