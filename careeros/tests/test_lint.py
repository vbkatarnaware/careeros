"""Tests for careeros/lint.py — the voice-dna regex backstop and the
deterministic resume-truthfulness verbatim check. The latter is what makes
"selector, not writer" an enforced rule rather than a prompt-only suggestion;
these tests exist to prove it actually catches a fabricated bullet, not just
that it passes a clean one (both directions matter equally)."""

from __future__ import annotations

from careeros.lint import lint_text, verify_resume_bullets
from careeros.models import ProfileBullet, ProfileExperience
from careeros.tests.conftest import make_profile


def test_lint_text_flags_em_dash():
    issues = lint_text("This is a sentence — with an em dash.")
    assert any(i.kind == "em_dash" for i in issues)


def test_lint_text_flags_banned_word():
    issues = lint_text("We will leverage this synergy.")
    kinds_words = [(i.kind, i.snippet) for i in issues]
    assert any(i.kind == "banned_word" for i in issues)


def test_lint_text_flags_negative_parallelism():
    issues = lint_text("This isn't just a job, it's a calling.")
    assert any(i.kind == "negative_parallelism" for i in issues)


def test_lint_text_clean_text_has_no_issues():
    issues = lint_text("Owned the credit-decisioning engine at ICICI Bank.")
    assert issues == []


def _profile_with_bullets():
    return make_profile(
        experience=[
            ProfileExperience(
                company="ICICI Bank", role="Product Manager",
                bullets=[
                    ProfileBullet(
                        text="Owned the Business Rules Engine deciding credit eligibility.",
                        tags=["fintech"], visibility="headline",
                    ),
                ],
            ),
        ],
        summary_variants=[{"id": "default", "text": "Product Manager who ships end to end.", "jd_tags": []}],
        projects=[
            {"name": "Rizent AI", "bullets": [
                {"text": "Rizent AI: AI-driven investor outreach tool.", "tags": [], "visibility": "headline"},
            ]},
        ],
    )


def test_verify_resume_bullets_passes_verbatim_content():
    profile = _profile_with_bullets()
    resume_md = (
        "# Name\n\n## Summary\nProduct Manager who ships end to end.\n\n"
        "## Experience\n### ICICI Bank\n"
        "- Owned the Business Rules Engine deciding credit eligibility.\n"
    )
    assert verify_resume_bullets(resume_md, profile) == []


def test_verify_resume_bullets_catches_fabricated_bullet():
    profile = _profile_with_bullets()
    resume_md = (
        "## Summary\nProduct Manager who ships end to end.\n\n"
        "## Experience\n"
        "- Owned the Business Rules Engine deciding credit eligibility.\n"
        "- Led a team of 12 engineers to launch a mobile banking app used by 5 million users.\n"
    )
    issues = verify_resume_bullets(resume_md, profile)
    assert len(issues) == 1
    assert "Led a team of 12 engineers" in issues[0]


def test_verify_resume_bullets_catches_fabricated_summary():
    profile = _profile_with_bullets()
    resume_md = "## Summary\nA completely invented summary that isn't in the profile.\n"
    issues = verify_resume_bullets(resume_md, profile)
    assert any("Summary" in i for i in issues)


def test_verify_resume_bullets_allows_label_prefixed_bullet_when_fact_has_no_label():
    """When the profile's OWN bullet text has no baked-in label (unlike the
    Rizent AI fixture above, which does), a resume line that adds one as
    presentational markdown ('**Project**: ...') must still verify — the
    label itself isn't part of the fact being checked."""
    profile = make_profile(
        projects=[
            {"name": "Side Project", "bullets": [
                {"text": "Built a tool that does X.", "tags": [], "visibility": "headline"},
            ]},
        ],
    )
    resume_md = "## Projects\n- **Side Project**: Built a tool that does X.\n"
    assert verify_resume_bullets(resume_md, profile) == []


def test_verify_resume_bullets_matches_raw_bullet_with_its_own_colon_first():
    """A bullet whose OWN verbatim profile text starts with 'Name: ...' must
    match as-is before falling back to label-stripping."""
    profile = _profile_with_bullets()
    resume_md = "## Projects\n- Rizent AI: AI-driven investor outreach tool.\n"
    assert verify_resume_bullets(resume_md, profile) == []
