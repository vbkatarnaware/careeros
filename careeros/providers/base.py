"""The provider contract.

A provider is one file exposing `fetch(config) -> list[dict]`, where each
dict is a RAW record in whatever shape that provider's source returns. The
pipeline's `normalize` stage (careeros/pipeline/normalize.py) is what turns
raw records into `Job` objects — a provider itself never constructs a `Job`.

This split matters: normalize.py holds ALL of CareerOS's field-mapping logic
in one place, so adding a provider means writing a `fetch()` that returns
whatever shape is natural for that source, plus one mapping function next to
it. The pipeline never imports a provider directly — see `providers/registry.py`.
"""

from __future__ import annotations

from typing import Any, Callable, Protocol

from careeros.config import Config


class ProviderError(RuntimeError):
    """Raised for an expected, actionable provider failure (missing/exhausted
    credentials, budget cap hit). The CLI catches this and prints the message
    directly instead of an unhandled-exception traceback — the distinction
    that matters is "tell the user what to do" vs "crash."""


class Provider(Protocol):
    id: str

    def fetch(self, config: Config, **kwargs: Any) -> list[dict[str, Any]]:
        """Return raw job records exactly as the source returns them.
        No normalization, no field renaming — that's normalize.py's job.

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
