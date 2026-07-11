"""Tests for careeros/providers/we_work_remotely.py — RSS parsing, the
"Company: Job Title" splitting convention, and field mapping. No real
network/RSS calls: `requests.get` is mocked via unittest.mock, matching
this repo's existing pattern (see test_provider_fantastic_jobs_errors.py).
The fixture XML below uses the exact tag names verified live against the
real feed this session (title/region/country/state/skills/category/type/
description/pubDate/expires_at/guid/link, plus a namespaced media:content
logo element that should be ignored)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from careeros.providers.base import ProviderResult
from careeros.providers.we_work_remotely import (
    PROVIDER, _map_employment_type, _parse_feed, _posted_at, _split_title,
)

_FIXTURE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">
<channel>
<title>We Work Remotely</title>
<item>
<title>APEX TRADE: Entry-Level Crypto Market Specialist</title>
<region>Anywhere in the World</region>
<country></country>
<state></state>
<skills>Crypto, Trading, Communication</skills>
<category>All Other Remote</category>
<type>Full-Time</type>
<description>&lt;p&gt;Join our trading desk.&lt;/p&gt;</description>
<pubDate>Fri, 10 Jul 2026 15:21:26 +0000</pubDate>
<expires_at>Fri, 07 Aug 2026 15:21:26 +0000</expires_at>
<guid>https://weworkremotely.com/remote-jobs/apex-trade-entry-level-crypto-market-specialist</guid>
<link>https://weworkremotely.com/remote-jobs/apex-trade-entry-level-crypto-market-specialist</link>
<media:content url="https://weworkremotely.com/logo1.png" type="image/png"/>
</item>
<item>
<title>Acme Studio: Senior Product Designer</title>
<region>US Only</region>
<country>United States</country>
<state></state>
<skills>Figma, Design Systems</skills>
<category>Design</category>
<type>Contract</type>
<description>&lt;p&gt;Design polished product surfaces.&lt;/p&gt;</description>
<pubDate>Thu, 09 Jul 2026 09:00:00 +0000</pubDate>
<expires_at>Thu, 06 Aug 2026 09:00:00 +0000</expires_at>
<guid>https://weworkremotely.com/remote-jobs/acme-studio-senior-product-designer</guid>
<link>https://weworkremotely.com/remote-jobs/acme-studio-senior-product-designer</link>
<media:content url="https://weworkremotely.com/logo2.png" type="image/png"/>
</item>
<item>
<title>No Colon Title Here</title>
<region></region>
<country></country>
<state></state>
<skills></skills>
<category>Engineering</category>
<type>Some Weird Type</type>
<description></description>
<pubDate>Wed, 08 Jul 2026 12:00:00 +0000</pubDate>
<expires_at></expires_at>
<guid>https://weworkremotely.com/remote-jobs/no-colon-title-here</guid>
<link>https://weworkremotely.com/remote-jobs/no-colon-title-here</link>
<media:content url="https://weworkremotely.com/logo3.png" type="image/png"/>
</item>
</channel>
</rss>
"""


def _resp(status_code: int = 200, text: str = _FIXTURE_RSS) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    return resp


# ── _parse_feed: RSS -> raw record dicts ────────────────────────────────

def test_parse_feed_returns_one_raw_dict_per_item():
    items = _parse_feed(_FIXTURE_RSS)
    assert len(items) == 3


def test_parse_feed_reads_exact_tag_names():
    items = _parse_feed(_FIXTURE_RSS)
    first = items[0]
    assert first["title"] == "APEX TRADE: Entry-Level Crypto Market Specialist"
    assert first["region"] == "Anywhere in the World"
    assert first["category"] == "All Other Remote"
    assert first["type"] == "Full-Time"
    assert first["description"] == "<p>Join our trading desk.</p>"
    assert first["pubDate"] == "Fri, 10 Jul 2026 15:21:26 +0000"
    assert first["link"] == "https://weworkremotely.com/remote-jobs/apex-trade-entry-level-crypto-market-specialist"
    assert first["guid"] == first["link"]


def test_parse_feed_empty_tags_become_empty_string():
    items = _parse_feed(_FIXTURE_RSS)
    assert items[0]["country"] == ""
    assert items[0]["state"] == ""


def test_parse_feed_ignores_media_content_element():
    items = _parse_feed(_FIXTURE_RSS)
    assert "media:content" not in items[0]
    assert "content" not in items[0]


# ── _split_title ──────────────────────────────────────────────────────────

def test_split_title_splits_on_first_colon_space():
    company, title = _split_title("APEX TRADE: Entry-Level Crypto Market Specialist")
    assert company == "APEX TRADE"
    assert title == "Entry-Level Crypto Market Specialist"


def test_split_title_handles_colon_in_title_by_splitting_on_first_occurrence():
    company, title = _split_title("Acme: Senior Eng: Backend")
    assert company == "Acme"
    assert title == "Senior Eng: Backend"


def test_split_title_falls_back_to_unknown_company_when_no_colon():
    company, title = _split_title("No Colon Title Here")
    assert company == "Unknown"
    assert title == "No Colon Title Here"


# ── _map_employment_type ─────────────────────────────────────────────────

def test_map_employment_type_full_time():
    assert _map_employment_type("Full-Time") == "full_time"
    assert _map_employment_type("full time") == "full_time"


def test_map_employment_type_part_time():
    assert _map_employment_type("Part-Time") == "part_time"


def test_map_employment_type_contract_and_aliases():
    assert _map_employment_type("Contract") == "contract"
    assert _map_employment_type("Freelance") == "contract"
    assert _map_employment_type("Contractor") == "contract"


def test_map_employment_type_internship():
    assert _map_employment_type("Internship") == "internship"


def test_map_employment_type_unknown_maps_to_none():
    assert _map_employment_type("Some Weird Type") is None
    assert _map_employment_type(None) is None
    assert _map_employment_type("") is None


# ── _posted_at ────────────────────────────────────────────────────────────

def test_posted_at_parses_rfc822_to_iso():
    iso = _posted_at("Fri, 10 Jul 2026 15:21:26 +0000")
    assert iso is not None
    assert iso.startswith("2026-07-10")


def test_posted_at_falls_back_to_raw_string_on_bad_input():
    assert _posted_at("not a real date") == "not a real date"


def test_posted_at_none_when_missing():
    assert _posted_at(None) is None
    assert _posted_at("") is None


# ── to_job_dict ────────────────────────────────────────────────────────────

def test_to_job_dict_splits_company_and_title():
    raw = _parse_feed(_FIXTURE_RSS)[0]
    job = PROVIDER.to_job_dict(raw)
    assert job["company"] == "APEX TRADE"
    assert job["title"] == "Entry-Level Crypto Market Specialist"


def test_to_job_dict_falls_back_to_unknown_company():
    raw = _parse_feed(_FIXTURE_RSS)[2]
    job = PROVIDER.to_job_dict(raw)
    assert job["company"] == "Unknown"
    assert job["title"] == "No Colon Title Here"


def test_to_job_dict_returns_none_when_link_missing():
    raw = {"title": "Some Co: A Title", "link": ""}
    assert PROVIDER.to_job_dict(raw) is None


def test_to_job_dict_returns_none_when_link_not_http():
    raw = {"title": "Some Co: A Title", "link": "ftp://weworkremotely.com/x"}
    assert PROVIDER.to_job_dict(raw) is None


def test_to_job_dict_returns_none_when_title_missing():
    raw = {"title": "", "link": "https://weworkremotely.com/remote-jobs/x"}
    assert PROVIDER.to_job_dict(raw) is None


def test_to_job_dict_maps_full_time_employment_type():
    raw = _parse_feed(_FIXTURE_RSS)[0]
    job = PROVIDER.to_job_dict(raw)
    assert job["employment_type"] == "full_time"


def test_to_job_dict_unmapped_type_is_none():
    raw = _parse_feed(_FIXTURE_RSS)[2]
    job = PROVIDER.to_job_dict(raw)
    assert job["employment_type"] is None


def test_to_job_dict_remote_always_true():
    for raw in _parse_feed(_FIXTURE_RSS):
        assert PROVIDER.to_job_dict(raw)["remote"] is True


def test_to_job_dict_location_defaults_to_remote_when_region_empty():
    raw = _parse_feed(_FIXTURE_RSS)[2]  # empty region
    job = PROVIDER.to_job_dict(raw)
    assert job["location"] == "Remote"


def test_to_job_dict_location_uses_region_when_present():
    raw = _parse_feed(_FIXTURE_RSS)[0]
    job = PROVIDER.to_job_dict(raw)
    assert job["location"] == "Anywhere in the World"


def test_to_job_dict_apply_url_is_link():
    raw = _parse_feed(_FIXTURE_RSS)[0]
    job = PROVIDER.to_job_dict(raw)
    assert job["apply_url"] == raw["link"]


def test_to_job_dict_description_preserved_as_is():
    raw = _parse_feed(_FIXTURE_RSS)[0]
    job = PROVIDER.to_job_dict(raw)
    assert job["description"] == "<p>Join our trading desk.</p>"


def test_to_job_dict_unmapped_fields_are_none():
    raw = _parse_feed(_FIXTURE_RSS)[0]
    job = PROVIDER.to_job_dict(raw)
    assert job["salary"] is None
    assert job["contact"] is None
    assert job["company_linkedin"] is None
    assert job["seniority"] is None


# ── fetch(): mocked HTTP, real parsing ──────────────────────────────────

def test_fetch_returns_provider_result_with_zero_cost():
    with patch("requests.get", return_value=_resp()):
        result = PROVIDER.fetch(config=None)
    assert isinstance(result, ProviderResult)
    assert result.provider == "we-work-remotely"
    assert result.cost_usd == 0.0
    assert result.requests == 1
    assert result.records == 3
    assert len(result.items) == 3


def test_fetch_respects_limit():
    with patch("requests.get", return_value=_resp()):
        result = PROVIDER.fetch(config=None, limit=2)
    assert result.records == 2
    assert len(result.items) == 2


def test_fetch_sends_user_agent_header():
    with patch("requests.get", return_value=_resp()) as mock_get:
        PROVIDER.fetch(config=None)
    _, kwargs = mock_get.call_args
    assert "User-Agent" in kwargs["headers"]


def test_fetch_ignores_search_and_query_kwargs_without_crashing():
    with patch("requests.get", return_value=_resp()):
        result = PROVIDER.fetch(config=None, search="python", query={"foo": "bar"})
    assert result.records == 3


# ── validate() ────────────────────────────────────────────────────────────

def test_validate_always_returns_empty_list():
    assert PROVIDER.validate(config=None) == []
