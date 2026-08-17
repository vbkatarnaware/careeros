"""Tests for careeros/pipeline/normalize.py, including the v2.0 tier
provenance wiring (`provenance` param -> Job.tiers)."""

from __future__ import annotations

from careeros.pipeline.normalize import normalize_all, normalize_one


class _FakeProvider:
    def to_job_dict(self, raw):
        if not raw.get("title") or not raw.get("url", "").startswith("http"):
            return None
        return {
            "title": raw["title"], "company": raw.get("company", "Unknown"),
            "apply_url": raw["url"], "description": raw.get("description"),
            "location": raw.get("location"), "remote": raw.get("remote"),
            "employment_type": None, "seniority": None,
            "posted_at": None, "salary": None, "contact": None, "company_linkedin": None,
        }


def _raw(title, **overrides):
    d = {"title": title, "url": f"https://example.com/{title.replace(' ', '-')}"}
    d.update(overrides)
    return d


def test_normalize_one_sets_tiers_when_provided():
    job = normalize_one(_raw("PM"), _FakeProvider(), source="fake", tiers=["global_remote"])
    assert job.tiers == ["global_remote"]


def test_normalize_one_tiers_none_when_not_provided():
    job = normalize_one(_raw("PM"), _FakeProvider(), source="fake")
    assert job.tiers is None


def test_normalize_all_aligns_provenance_by_index():
    raws = [_raw("PM1"), _raw("PM2"), _raw("PM3")]
    provenance = [["global_remote"], ["india_remote"], ["onsite"]]
    jobs = normalize_all(raws, _FakeProvider(), source="fake", provenance=provenance)
    assert [j.tiers for j in jobs] == [["global_remote"], ["india_remote"], ["onsite"]]


def test_normalize_all_provenance_absent_leaves_tiers_none():
    raws = [_raw("PM1"), _raw("PM2")]
    jobs = normalize_all(raws, _FakeProvider(), source="fake")
    assert all(j.tiers is None for j in jobs)


def test_normalize_all_rejected_record_does_not_desync_provenance_alignment():
    """A record the provider's own mapper rejects (returns None) is dropped
    entirely -- provenance stays aligned to RAW index, not output index, so
    the surviving jobs still get their own correct tier."""
    raws = [_raw("PM1"), {"title": "", "url": ""}, _raw("PM3")]  # index 1 is rejected
    provenance = [["global_remote"], ["india_remote"], ["onsite"]]
    jobs = normalize_all(raws, _FakeProvider(), source="fake", provenance=provenance)
    assert len(jobs) == 2
    assert jobs[0].tiers == ["global_remote"]
    assert jobs[1].tiers == ["onsite"]


# ── eligibility_note recovery (v2.2) ─────────────────────────────────────

def test_normalize_one_recovers_eligibility_note_from_truncated_tail():
    filler = "Great team, fast-growing startup. " * 150  # > 4000 chars
    desc = filler + "Must be authorized to work in the US without sponsorship."
    job = normalize_one(_raw("PM", description=desc), _FakeProvider(), source="fake",
                         description_max_chars=4000)
    assert job.eligibility_note is not None
    assert "authorized to work in the US" in job.eligibility_note
    # the kept `description` itself must NOT contain the recovered phrase --
    # it really was past the truncation cut.
    assert "authorized to work in the US" not in job.description


def test_normalize_one_eligibility_note_none_when_description_short():
    job = normalize_one(_raw("PM", description="Must be authorized to work in the US."),
                         _FakeProvider(), source="fake", description_max_chars=4000)
    assert job.eligibility_note is None  # already visible in the kept description


def test_normalize_one_eligibility_note_none_when_nothing_matches():
    filler = "Great team, fast-growing startup. " * 150
    job = normalize_one(_raw("PM", description=filler), _FakeProvider(), source="fake",
                         description_max_chars=4000)
    assert job.eligibility_note is None
