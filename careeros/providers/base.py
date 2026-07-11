"""The provider contract (v1.2: standardized across every provider, no
special cases).

A provider is one file exposing exactly three methods — this is the whole
contract, and every provider (Fantastic Jobs, the legacy actor, and every
v1.2 addition: RemoteOK, We Work Remotely, Naukri, Foundit, Indeed,
Glassdoor, ZipRecruiter) implements all three identically:

  - `validate(config) -> list[str]` — config/credential problems (empty =
    OK). Called by `careeros doctor` and by `discover` before `fetch()`, so
    a misconfigured provider is reported and skipped rather than crashing
    the whole run.
  - `fetch(config, **kwargs) -> ProviderResult` — raw records exactly as the
    source returns them, plus metadata (cost, requests, records, timing,
    warnings). No normalization, no field renaming — that's normalize.py's
    job. A provider never constructs a `Job`.
  - `to_job_dict(raw) -> dict | None` — map one raw record into the common
    pre-normalization shape. Return None to skip a record missing a
    required field, rather than raising.

`discover` (careeros/cli.py) loops over every enabled provider generically —
it never branches on a provider's name. Budget/quota enforcement is
CAPABILITY-driven (see `budget.guard_for`): a provider's own resolved config
block declares what it needs guarding (a weekly record quota, a monthly USD
budget, or nothing), not the provider's identity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from careeros.config import Config


class ProviderError(RuntimeError):
    """Raised for an expected, actionable provider failure (missing/exhausted
    credentials, budget cap hit). The CLI catches this and prints the message
    directly instead of an unhandled-exception traceback — the distinction
    that matters is "tell the user what to do" vs "crash."""


@dataclass
class ProviderResult:
    """The single standard result every provider's `fetch()` returns —
    replaces the old `(items, cost_usd)` tuple. `items` is what normalize.py
    consumes; everything else is metadata for `run.json`/`summary.md`'s
    per-provider discovery table (revision #6) and for diagnostics — the
    downstream pipeline never reads anything but `.items` (via normalize).

    `skipped`/`skip_reason` make a provider that was ENABLED but didn't
    actually run (budget exhausted, `validate()` failed, an upstream error)
    explicit in reporting instead of silently vanishing — a disabled
    provider (`enabled: false`) never reaches `fetch()` at all and is
    reported separately by `discover`, not via this field.
    """

    provider: str
    items: list[dict[str, Any]] = field(default_factory=list)
    cost_usd: float = 0.0
    requests: int = 0
    records: int = 0
    seconds: float = 0.0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    skipped: bool = False
    skip_reason: str | None = None

    @classmethod
    def skip(cls, provider: str, reason: str) -> "ProviderResult":
        """Convenience for the common "enabled but didn't run" case (used by
        `discover`'s capability guard, and available to a provider's own
        `fetch()` for a self-detected reason to bail out cleanly)."""
        return cls(provider=provider, skipped=True, skip_reason=reason)


class Provider(Protocol):
    id: str

    def validate(self, config: Config) -> list[str]:
        """Human-readable config/credential problems for THIS provider
        (empty list = OK). Pure/read-only: env vars + config only, no
        network calls — `careeros doctor` calls this on every run, so it
        must never spend quota or hit the network itself. `discover` calls
        it immediately before `fetch()` for each enabled provider; a
        non-empty result means the provider is reported (not crashed) and
        marked skipped for this run."""
        ...

    def fetch(self, config: Config, **kwargs: Any) -> ProviderResult:
        """Return a ProviderResult (raw records + metadata). No
        normalization, no field renaming — that's normalize.py's job. A free
        source just leaves `cost_usd` at 0.0.

        Common optional kwargs (a provider may ignore any it doesn't need):
        `limit` (max records), `search` (a single manual query override), and
        `query` (a segmented-discovery spec from pipeline/queryplan.py — one
        of N per-work-mode queries `discover` loops over; see
        fantastic_jobs.py's `fetch()` for the reference merge pattern).
        """
        ...

    def to_job_dict(self, raw: dict[str, Any]) -> dict[str, Any] | None:
        """Map one raw record into the common pre-normalization shape:
        {title, company, location, apply_url, description, ...}.
        Return None to skip a record that's missing required fields
        (e.g. no title or no usable URL) rather than raising.
        """
        ...
