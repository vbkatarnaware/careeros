"""Tests for careeros/lint.py — the voice-dna regex backstop and the
deterministic resume-truthfulness verbatim check. The latter is what makes
"selector, not writer" an enforced rule rather than a prompt-only suggestion;
these tests exist to prove it actually catches a fabricated bullet, not just
that it passes a clean one (both directions matter equally)."""

from __future__ import annotations

from careeros.lint import lint_text, verify_resume_bullets, verify_resume_facts
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


# ── verify_resume_facts (v2: reword-preserving-facts) ──────────────────────

def _profile_for_facts() -> object:
    return make_profile(
        experience=[
            ProfileExperience(
                company="ICICI Bank", role="Product Manager",
                bullets=[
                    ProfileBullet(
                        text="Owned the Business Rules Engine for millions of applications a year.",
                        tags=[], visibility="headline"),
                    ProfileBullet(
                        text="Shipped GeoIQ, raising approval quality by 15% with no increase in default rate.",
                        tags=[], visibility="supporting"),
                ],
            ),
        ],
    )


def test_verify_resume_facts_passes_a_faithful_reword():
    """Reworded language is fine as long as every number survives."""
    profile = _profile_for_facts()
    resume_json = {
        "tagline": "Product",
        "summary": "Summary.",
        "experience": [{
            "company": "ICICI Bank",
            "bullets": [
                "Managed the Business Rules Engine powering millions of applications annually.",
                "Launched GeoIQ, lifting approval quality 15% without raising defaults.",
            ],
        }],
        "skills": [],
    }
    assert verify_resume_facts(resume_json, profile) == []


def test_verify_resume_facts_catches_an_invented_metric():
    profile = _profile_for_facts()
    resume_json = {
        "tagline": "Product",
        "summary": "Summary.",
        "experience": [{
            "company": "ICICI Bank",
            "bullets": ["Shipped GeoIQ, raising approval quality by 40% with no increase in default rate."],
        }],
        "skills": [],
    }
    issues = verify_resume_facts(resume_json, profile)
    assert len(issues) == 1
    assert "40%" in issues[0]


def test_verify_resume_facts_catches_a_dropped_then_replaced_metric_as_invented():
    """A bullet that swaps out the source's real number for a different one
    is caught even if the new number 'sounds' plausible."""
    profile = _profile_for_facts()
    resume_json = {
        "tagline": "Product", "summary": "Summary.",
        "experience": [{
            "company": "ICICI Bank",
            "bullets": ["Owned the Business Rules Engine for 12 million applications a year."],
        }],
        "skills": [],
    }
    issues = verify_resume_facts(resume_json, profile)
    assert len(issues) == 1
    assert "12" in issues[0]


def test_verify_resume_facts_catches_unknown_company():
    profile = _profile_for_facts()
    resume_json = {
        "tagline": "Product", "summary": "Summary.",
        "experience": [{"company": "A Company Not In The Profile", "bullets": ["Did a thing."]}],
        "skills": [],
    }
    issues = verify_resume_facts(resume_json, profile)
    assert any("Unknown company" in i for i in issues)


def test_verify_resume_facts_catches_company_name_leak_in_bullet():
    profile = _profile_for_facts()
    resume_json = {
        "tagline": "Product", "summary": "Summary.",
        "experience": [{
            "company": "ICICI Bank",
            "bullets": ["Owned the Business Rules Engine, excited to bring this to Northwind Robotics."],
        }],
        "skills": [],
    }
    issues = verify_resume_facts(resume_json, profile, target_company="Northwind Robotics")
    assert any("transferable-language" in i for i in issues)


def test_verify_resume_facts_catches_company_name_leak_in_summary_and_tagline():
    profile = _profile_for_facts()
    resume_json = {
        "tagline": "Excited to join Northwind Robotics",
        "summary": "Looking forward to Northwind Robotics.",
        "experience": [],
        "skills": [],
    }
    issues = verify_resume_facts(resume_json, profile, target_company="Northwind Robotics")
    assert any("tagline" in i for i in issues)
    assert any("summary" in i for i in issues)


def test_verify_resume_facts_no_target_company_skips_leak_check():
    """Without a target_company argument, the leak check is a no-op (used
    when the caller doesn't have the job's company name handy)."""
    profile = _profile_for_facts()
    resume_json = {
        "tagline": "Excited to join Northwind Robotics", "summary": "Summary.",
        "experience": [], "skills": [],
    }
    assert verify_resume_facts(resume_json, profile) == []


# ── verify_resume_facts: companies/projects name validation ────────────────
#
# `companies` and `projects[].name` are selector fields — resume.json names a
# profile.yaml entry by exact string, and careeros/typst_render.py looks it
# up by that string. A typo here is still valid JSON matching the schema, so
# nothing else catches it; careeros/typst_render.py's build_render_data just
# silently filters the unmatched entry out, and the resume ships with real
# content missing. These tests prove that failure mode is caught.

def _profile_with_companies_and_projects():
    return make_profile(
        experience=[
            ProfileExperience(
                company="QRapid", role="Founder",
                bullets=[ProfileBullet(text="Shipped a POS product.", tags=[], visibility="headline")],
            ),
            ProfileExperience(
                company="ICICI Bank", role="Product Manager",
                bullets=[ProfileBullet(text="Owned the Business Rules Engine.", tags=[], visibility="headline")],
            ),
        ],
        projects=[
            {"name": "Rizent AI", "bullets": [{"text": "AI outreach tool.", "tags": [], "visibility": "headline"}]},
            {"name": "CareerOS", "bullets": [{"text": "Job-search pipeline.", "tags": [], "visibility": "headline"}]},
        ],
    )


def test_verify_resume_facts_passes_known_companies_and_projects():
    profile = _profile_with_companies_and_projects()
    resume_json = {
        "tagline": "Product", "summary": "Summary.",
        "companies": ["QRapid", "ICICI Bank"],
        "experience": [
            {"company": "QRapid", "bullets": ["Shipped a POS product."]},
            {"company": "ICICI Bank", "bullets": ["Owned the Business Rules Engine."]},
        ],
        "skills": [],
        "projects": [{"name": "Rizent AI"}, {"name": "CareerOS"}],
    }
    assert verify_resume_facts(resume_json, profile) == []


def test_verify_resume_facts_catches_unknown_company_in_companies_field():
    """A typo'd `companies` entry ('Qrapid' vs the real 'QRapid') doesn't fail
    schema validation and doesn't touch `experience`, so nothing else catches
    it — the real QRapid experience just silently never gets included."""
    profile = _profile_with_companies_and_projects()
    resume_json = {
        "tagline": "Product", "summary": "Summary.",
        "companies": ["Qrapid", "ICICI Bank"],
        "experience": [{"company": "ICICI Bank", "bullets": ["Owned the Business Rules Engine."]}],
        "skills": [],
    }
    issues = verify_resume_facts(resume_json, profile)
    assert any("companies" in i and "Qrapid" in i for i in issues)


def test_verify_resume_facts_catches_unknown_project_name():
    profile = _profile_with_companies_and_projects()
    resume_json = {
        "tagline": "Product", "summary": "Summary.",
        "experience": [], "skills": [],
        "projects": [{"name": "Rizent AI"}, {"name": "Rizent Ai"}],
    }
    issues = verify_resume_facts(resume_json, profile)
    assert any("projects" in i and "Rizent Ai" in i for i in issues)
