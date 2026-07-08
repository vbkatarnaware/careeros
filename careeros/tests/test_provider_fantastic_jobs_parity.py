"""P2.7 parity test: the new default REST provider's `to_job_dict()` was
copied verbatim from the legacy actor provider on the premise that both
sources return the identical Fantastic Jobs dataset with identical field
names (verified during the P2.6/P2.7 architecture review — see the module
docstrings). This test proves that premise against real sample data, so a
migration mistake (a field renamed in one copy but not the other) fails
loudly instead of silently changing what `normalize.py` sees downstream."""

from __future__ import annotations

import json
from pathlib import Path

from careeros.providers.fantastic_jobs import PROVIDER as REST_PROVIDER
from careeros.providers.legacy.fantastic_jobs_actor import PROVIDER as ACTOR_PROVIDER

_SAMPLE_PATH = Path(__file__).resolve().parent / "fixtures" / "sample_raw.json"


def _sample_records() -> list[dict]:
    with open(_SAMPLE_PATH) as f:
        return json.load(f)


def test_sample_data_file_exists_and_is_nonempty():
    records = _sample_records()
    assert isinstance(records, list)
    assert len(records) > 0


def test_to_job_dict_is_identical_between_rest_and_actor_providers_for_every_sample_record():
    for i, raw in enumerate(_sample_records()):
        rest_mapped = REST_PROVIDER.to_job_dict(raw)
        actor_mapped = ACTOR_PROVIDER.to_job_dict(raw)
        assert rest_mapped == actor_mapped, (
            f"record {i} (id={raw.get('id')!r}): REST and actor to_job_dict() diverged — "
            f"REST={rest_mapped!r} actor={actor_mapped!r}"
        )


def test_to_job_dict_produces_the_required_shape_for_at_least_one_record():
    """Sanity check that the fixture is actually exercising the mapper, not
    just two empty-ish dicts agreeing with each other."""
    mapped = next(
        m for raw in _sample_records()
        if (m := REST_PROVIDER.to_job_dict(raw)) is not None
    )
    for key in ("title", "company", "apply_url", "location", "remote",
                "employment_type", "seniority", "posted_at", "salary",
                "contact", "company_linkedin"):
        assert key in mapped
