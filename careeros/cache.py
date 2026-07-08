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
from pathlib import Path
from typing import Any, Optional

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
        path = self._path(stage, key)
        if not path.exists():
            return None
        with open(path) as f:
            return json.load(f)

    def put(self, stage: str, key: str, value: dict) -> None:
        path = self._path(stage, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(value, f, indent=2, sort_keys=True)

    def stats(self) -> dict[str, int]:
        """Count cached entries per stage — surfaced in run.json for visibility
        into how much a `daily` run actually cost vs. reused."""
        if not self.cache_dir.exists():
            return {}
        return {
            d.name: len(list(d.glob("*.json")))
            for d in self.cache_dir.iterdir() if d.is_dir()
        }
