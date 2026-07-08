"""The run-dir: CareerOS's message bus, and run.json: its manifest.

Every `daily` run gets .careeros/runs/<date>/ containing one self-describing
folder per stage. run.json is a running ledger of what happened: counts in/out
of each stage, timings, which prompt versions ran, and cache hit/miss counts.
It is the free debug dashboard and KPI log this architecture is built around.

Stages are resumable by construction: `stage_output_path` always points at
the same file for a given (run_dir, stage), so re-running `daily` after a
partial failure just finds prior stages' outputs already on disk and skips
straight to the first missing one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import jsonschema

STAGE_DIRS = {
    "discover": "01_discover",
    "normalize": "02_normalize",
    "dedupe": "03_dedupe",
    "constraints": "04_constraints",
    "gate": "05_gate",
    "evaluate": "06_evaluate",
    "select": "07_select",
}

SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "schemas"


def run_dir(runs_root: Path, date: str) -> Path:
    return runs_root / date


def stage_dir(runs_root: Path, date: str, stage: str) -> Path:
    d = run_dir(runs_root, date) / STAGE_DIRS[stage]
    d.mkdir(parents=True, exist_ok=True)
    return d


def artifacts_dir(runs_root: Path, date: str, job_id: str) -> Path:
    d = run_dir(runs_root, date) / "artifacts" / job_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _meta_path(runs_root: Path, date: str, stage: str) -> Path:
    return run_dir(runs_root, date) / f"_meta_{stage}.json"


def write_stage_meta(runs_root: Path, date: str, stage: str, meta: dict) -> None:
    """Persist prepare-time state (start timestamp, cache hits/misses
    computed during --prepare) so --finalize, a SEPARATE process invocation,
    can compute the stage's real elapsed time (including the agent's
    reasoning span in between) and report accurate cache stats. Not tied to
    STAGE_DIRS, so this works for stages like 'artifacts' that don't have a
    numbered stage folder."""
    run_dir(runs_root, date).mkdir(parents=True, exist_ok=True)
    with open(_meta_path(runs_root, date, stage), "w") as f:
        json.dump(meta, f)


def read_stage_meta(runs_root: Path, date: str, stage: str) -> dict:
    path = _meta_path(runs_root, date, stage)
    if not path.exists():
        return {}
    with open(path) as f:
        return json.load(f)


def _manifest_path(runs_root: Path, date: str) -> Path:
    return run_dir(runs_root, date) / "run.json"


def load_manifest(runs_root: Path, date: str) -> dict:
    path = _manifest_path(runs_root, date)
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {"date": date, "stages": {}, "prompt_versions": {}, "cache": {}}


def save_manifest(runs_root: Path, date: str, manifest: dict) -> None:
    run_dir(runs_root, date).mkdir(parents=True, exist_ok=True)
    with open(_manifest_path(runs_root, date), "w") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)


# stage -> the human-readable funnel-stage name it contributes to `totals`.
_TOTALS_LABELS = {
    "discover": "discovered",
    "normalize": "normalized",
    "dedupe": "deduped",
    "constraints": "eligible",
    "gate": "gated",
    "evaluate": "evaluated",
    "select": "selected",
    "artifacts": "artifacts_generated",
    "sheets": "sheeted",
}


def estimate_tokens(*paths: Path) -> int:
    """Rough token estimate (~4 chars/token) from the byte size of the files
    an AI stage's agent turn actually reads (its input JSON, and profile.yaml
    where the stage's prompt says it reads it).

    This is NOT a real token count — the actual LLM call happens inside the
    host coding agent's own context window, which CareerOS's Python layer has
    no visibility into. Without this, "least AI/compute cost" (the project's
    own KPI) was completely unmeasured in run.json. A file-size-based
    estimate is the honest amount of visibility achievable from outside the
    agent — directionally useful for comparing runs/prompt versions, not
    precise to the token.
    """
    total_bytes = sum(p.stat().st_size for p in paths if p.exists())
    return total_bytes // 4


def record_stage(
    runs_root: Path,
    date: str,
    stage: str,
    *,
    count_in: int,
    count_out: int,
    seconds: float,
    prompt_version: Optional[str] = None,
    cache_hits: int = 0,
    cache_misses: int = 0,
    estimated_tokens: int = 0,
    errors: Optional[list[str]] = None,
) -> None:
    """Append one stage's outcome to run.json. Called by every stage's
    `--finalize` step (AI stages) or immediately (deterministic stages).

    Also recomputes the manifest's top-level `totals` block (the KPI-log
    funnel: discovered/deduped/eligible/gated/evaluated/selected/artifacts)
    from whatever stages have reported so far, so `totals` is always
    up to date after any stage runs, in any order or after a partial resume.
    """
    manifest = load_manifest(runs_root, date)
    manifest["stages"][stage] = {
        "count_in": count_in,
        "count_out": count_out,
        "seconds": round(seconds, 2),
        "cache_hits": cache_hits,
        "cache_misses": cache_misses,
        "estimated_tokens": estimated_tokens,
        "errors": errors or [],
    }
    if prompt_version:
        manifest["prompt_versions"][stage] = prompt_version

    totals = manifest.setdefault("totals", {})
    totals["estimated_tokens_total"] = sum(
        s.get("estimated_tokens", 0) for s in manifest["stages"].values()
    )
    for stage_name, label in _TOTALS_LABELS.items():
        if stage_name in manifest["stages"]:
            totals[label] = manifest["stages"][stage_name]["count_out"]

    save_manifest(runs_root, date, manifest)


def validate_stage(schema_name: str, records: list[dict]) -> list[str]:
    """Validate a list of records against schemas/<schema_name>.schema.json.

    Returns a list of human-readable error strings (empty = all valid). This
    is what a stage's `--finalize` step calls before accepting agent-written
    output — a schema failure here triggers a bounded re-instruction for just
    the failed items, never a silent pass-through.
    """
    schema_path = SCHEMAS_DIR / f"{schema_name}.schema.json"
    with open(schema_path) as f:
        schema = json.load(f)
    validator = jsonschema.Draft7Validator(schema)

    errors: list[str] = []
    for i, record in enumerate(records):
        record_id = record.get("id", f"index {i}")
        for err in validator.iter_errors(record):
            loc = "/".join(str(p) for p in err.path) or "(root)"
            errors.append(f"{record_id}: {loc}: {err.message}")
    return errors
