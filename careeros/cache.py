"""Content-addressed cache for AI-stage outputs.

The whole point: never pay for an AI call whose answer we already have.

A cache key is a fingerprint of everything that could change the answer:
job content + profile version + prompt version (+ for artifacts, the eval
score). Because the prompt version is IN the key, bumping
`config.prompts.eval` from v1 to v2 busts only eval's cache — resumes/covers
downstream still get invalidated too since their own key includes the eval's
score, so the invalidation propagates correctly without any extra bookkeeping.

This is deliberately just files on disk (.careeros/cache/<stage>/<key>.json).
No database, no expiry logic — a cache miss just means "run the AI stage
again," which is always safe.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Optional

from careeros.models import stable_hash


def eval_cache_key(job_hash: str, profile_version: int, prompt_version: str) -> str:
    return stable_hash(job_hash, str(profile_version), prompt_version)


def artifact_cache_key(
    job_hash: str, profile_version: int, eval_score: float, prompt_version: str
) -> str:
    """Cache key for derived artifacts (resume, cover). Includes the eval score
    so that if a job gets re-evaluated (e.g. after a rubric change) and its
    score moves, the resume/cover regenerate too — they were tailored to the
    old score's strengths/keywords, which are no longer the source of truth.
    """
    return stable_hash(job_hash, str(profile_version), f"{eval_score:.1f}", prompt_version)


class Cache:
    def __init__(self, cache_dir: Path | str):
        self.cache_dir = Path(cache_dir)

    def _path(self, stage: str, key: str) -> Path:
        return self.cache_dir / stage / f"{key}.json"

    def get(self, stage: str, key: str) -> Optional[dict]:
        """A corrupt or truncated cache file (e.g. from a process killed
        mid-write, before `put`'s atomic replace below existed) is treated as
        a miss, not a crash — same tolerance as `budget.load_state`'s
        equivalent read. A cache miss just means "run the AI stage again,"
        which is always safe; a raised exception here would take the whole
        pipeline down over a file that only ever holds a re-computable
        result."""
        path = self._path(stage, key)
        if not path.exists():
            return None
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None

    def put(self, stage: str, key: str, value: dict) -> None:
        """Writes via a temp file + atomic `os.replace` so a process killed
        mid-write can never leave a half-written, corrupt cache entry behind
        for `get` to trip over later — the failure mode `get` above is
        tolerant of, but that this avoids creating in the first place."""
        path = self._path(stage, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(value, f, indent=2, sort_keys=True)
            os.replace(tmp_path, path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
