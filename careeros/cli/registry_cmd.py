"""`careeros registry` — the OPTIONAL local reference-company lookup
(careeros/registry.py). Answers "do we already know this company, and
which ATS is it on?" before hand-adding an entry to
`.careeros/watchlist.yaml` — never a dependency of `careeros discover`
itself; skipping `registry sync` costs you nothing but this lookup.

`careeros watchlist` shows the current state of Layer 2A's targeted
company list (`.careeros/watchlist.yaml` + `.careeros/watchlist_state.json`,
see providers/ats_watchlist.py) — read-only; the watchlist itself is
edited by hand, and its state is updated as a side effect of `careeros
discover` actually scraping it, not by a separate verify step here.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import typer

from careeros import registry as registry_mod
from careeros.cli import app
from careeros.cli._shared import _config, _load_profile, _provider_query_cfg, _today
from careeros.companies import normalize_name
from careeros.providers.ats_watchlist import (
    STATE_FILENAME,
    WatchlistConfigError,
    WatchlistEntry,
    _scrape_entry,
    append_watchlist_entry,
    entry_key,
    load_state,
    load_watchlist,
)

registry_app = typer.Typer(help="Look up companies in the optional reference registry")
app.add_typer(registry_app, name="registry")

watchlist_app = typer.Typer(help="Inspect the Layer 2A targeted company watchlist")
app.add_typer(watchlist_app, name="watchlist")


@registry_app.command("sync")
def registry_sync(
    manifest_url: str = typer.Option(None, "--manifest-url", help="Override the default ats-scrapers manifest URL"),
):
    """Download the current ~80K-row company/tenant directory to
    .careeros/registry/companies.csv, with provenance. Safe to re-run —
    `imported_at` is preserved across syncs, only `last_synced_at` moves."""
    cfg = _config()
    meta = registry_mod.sync_reference(cfg, manifest_url=manifest_url)
    action = "unchanged upstream, download skipped" if meta.get("skipped") else "downloaded"
    typer.echo(
        f"[registry:sync] {meta['row_count']} companies ({action}) -> "
        f"{cfg.careeros_dir / 'registry' / 'companies.csv'}\n"
        f"  source snapshot generated_at={meta['manifest_generated_at']}"
    )


@registry_app.command("find")
def registry_find(
    name: str = typer.Argument(..., help="Company name or slug to search for"),
    limit: int = typer.Option(10, "--limit"),
):
    """Look up a company in the local reference cache (run `registry sync`
    first). Empty result is common and not an error — the 79,906-row
    directory has no country/domain columns, so this is a name/slug
    lookup only, not a filterable index."""
    cfg = _config()
    if registry_mod.load_meta(cfg) is None:
        typer.echo("[registry:find] No local cache yet — run `careeros registry sync` first.")
        raise typer.Exit(1)
    matches = registry_mod.find_company(cfg, name, limit=limit)
    if not matches:
        typer.echo(f"[registry:find] No match for {name!r}.")
        return
    for m in matches:
        typer.echo(f"{m.get('name', ''):<35} {m.get('ats', ''):<16} slug={m.get('slug', '')}")


@registry_app.command("stats")
def registry_stats():
    """Provenance and row count of the local reference cache."""
    cfg = _config()
    meta = registry_mod.load_meta(cfg)
    if meta is None:
        typer.echo("[registry:stats] No local cache yet — run `careeros registry sync` first.")
        return
    typer.echo(f"[registry:stats] {meta['row_count']} companies")
    typer.echo(f"  source snapshot generated_at: {meta['manifest_generated_at']}")
    typer.echo(f"  imported_at:    {meta['imported_at']}")
    typer.echo(f"  last_synced_at: {meta['last_synced_at']}")


def _resolve_via_registry(cfg, name: str) -> tuple[str, str | None, str | None] | None:
    """The one reference-registry lookup, shared by `watchlist add` (a miss
    here is a hard failure, since it has no other way to find an ats/slug)
    and `watchlist discover` (a hit here is ADVISORY only — registry
    presence is not the same claim as Layer 1 job coverage, see that
    command). Returns `(canonical_name, ats, slug)` on a match, else `None`.

    `registry_mod.find_company` is a SUBSTRING search (a convenience lookup
    for `registry find`, see its own docstring) and returns its best
    substring hit even with no exact match — "Ramp" substring-matches
    "Trampoline Systems", "Gem" matches "Gemini Data". Only trust it here
    when the top hit's normalized name or slug is EXACTLY the probe;
    anything looser is a near-miss, not the same company."""
    matches = registry_mod.find_company(cfg, name, limit=1)
    if not matches:
        return None
    m = matches[0]
    canonical = m.get("name") or name
    slug = (m.get("slug") or "").strip().lower()
    probe = normalize_name(name)
    if normalize_name(canonical) != probe and slug != probe:
        return None
    return canonical, m.get("ats"), m.get("slug")


def _is_duplicate(existing: list[WatchlistEntry], canonical_name: str, ats: str | None) -> bool:
    """NORMALIZED identity (never raw string equality) — "Swiggy" and
    "swiggy, Inc." must collide, same identity rule as the reference
    registry. Same normalized company + same ats is a duplicate; same
    company on a DIFFERENT ats is a legitimate second board (see
    `entry_key`'s docstring on multi-board support) and is not a dup."""
    new_norm = normalize_name(canonical_name)
    return any(normalize_name(e.name) == new_norm and e.ats == ats for e in existing)


def _append_entry(
    watchlist_path,
    *, canonical_name: str, resolved_ats: str | None, candidate_ats: str | None,
    candidate_slug: str | None, candidate_url: str | None, source: str,
    website: str | None = None,
) -> dict:
    """The actual write — used only after `_scrape_entry` has already
    confirmed real job data exists, by both `watchlist add` (unconditionally)
    and `watchlist discover` (after its own quality bar passes, see that
    command). `website` (new, optional) is the company's own domain, absent
    for hand-supplied `--url`/`--ats`+`--slug` entries — populated by
    `discover` so a later stale mapping can be automatically re-resolved
    (see `providers/ats_watchlist.py`'s re-resolve-on-stale logic)."""
    entry = {
        "name": canonical_name, "ats": resolved_ats or candidate_ats, "slug": candidate_slug,
        "careers_url": candidate_url, "website": website, "source": source, "added_at": _today(),
    }
    entry = {k: v for k, v in entry.items() if v is not None}
    append_watchlist_entry(watchlist_path, entry)
    return entry


@watchlist_app.command("add")
def watchlist_add(
    name: str = typer.Argument(..., help="Company name"),
    url: str = typer.Option(None, "--url", help="Careers URL, resolved automatically via ats-scrapers"),
    ats: str = typer.Option(None, "--ats", help="Explicit ATS platform (use together with --slug)"),
    slug: str = typer.Option(None, "--slug", help="Explicit tenant slug (use together with --ats)"),
):
    """Resolve NAME into a validated watchlist entry — manual, user-driven
    ingestion (see `watchlist discover` for the automatic, profile-driven
    equivalent — both converge on the same validate-and-append tail below).

    Order: if NAME already matches the reference registry
    (`careeros registry sync`/`find`), its canonical name/ats/slug are
    REUSED — never create a second identity for a company we already know
    (--url/--ats/--slug override this and go straight to validation).
    Then the resolved (ats, slug) is validated against REAL live job data
    via the same `_scrape_entry` seam `careeros discover` itself uses.
    Only a validated entry is written to `.careeros/watchlist.yaml` — a
    failure is reported UNRESOLVED and nothing is written; this command
    never guesses an unvalidated mapping into existence."""
    from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError

    cfg = _config()
    watchlist_path = cfg.careeros_dir / "watchlist.yaml"
    existing = load_watchlist(watchlist_path)

    canonical_name, candidate_ats, candidate_slug, candidate_url = name, ats, slug, url
    source = "user_provided"

    if url or (ats and slug):
        pass  # explicit override — skip the registry lookup entirely
    else:
        resolved = _resolve_via_registry(cfg, name)
        if resolved is None:
            typer.echo(
                f"[watchlist:add] UNRESOLVED — {name!r} not in the reference registry "
                "and no --url/--ats+--slug given. Supply one to try a specific mapping."
            )
            raise typer.Exit(1)
        canonical_name, candidate_ats, candidate_slug = resolved
        source = "reference_registry"
        typer.echo(f"[watchlist:add] found in reference registry: {canonical_name!r} -> {candidate_ats}/{candidate_slug}")

    if _is_duplicate(existing, canonical_name, candidate_ats):
        typer.echo(f"[watchlist:add] already tracked: {canonical_name!r} on {candidate_ats} — skipping duplicate.")
        raise typer.Exit(0)

    candidate = WatchlistEntry(name=canonical_name, ats=candidate_ats, slug=candidate_slug, careers_url=candidate_url)

    try:
        resolved_ats, rows = _scrape_entry(candidate)
    except CompanyNotFoundError as e:
        typer.echo(f"[watchlist:add] UNRESOLVED — {canonical_name!r} not found on {candidate_ats}/{candidate_slug}: {e}")
        raise typer.Exit(1)
    except (WatchlistConfigError, ScraperError) as e:
        typer.echo(f"[watchlist:add] UNRESOLVED — could not validate {canonical_name!r}: {e}")
        raise typer.Exit(1)

    entry = _append_entry(
        watchlist_path, canonical_name=canonical_name, resolved_ats=resolved_ats,
        candidate_ats=candidate_ats, candidate_slug=candidate_slug,
        candidate_url=candidate_url, source=source,
    )
    typer.echo(
        f"[watchlist:add] VALIDATED — {canonical_name!r} -> {entry.get('ats')}/"
        f"{entry.get('slug') or entry.get('careers_url')} ({len(rows)} live job(s)) -> {watchlist_path}"
    )


_DISCOVERY_CANDIDATES_FILENAME = "discovery_candidates.json"
_DISCOVERY_TTL_DAYS = 30
_DISCOVERY_FRESHNESS_DAYS = 90


def _load_discovery_candidates(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_discovery_candidates(path: Path, data: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)


def _days_since(iso_date: str, today: str) -> int | None:
    try:
        return (date.fromisoformat(today) - date.fromisoformat(iso_date)).days
    except ValueError:
        return None  # unparseable -- caller treats this conservatively


def _has_adapter(ats: str) -> bool:
    """darwinbox and zoho_recruit are special cases throughout this
    codebase (see `_scrape_entry`): neither has a member in the installed
    `ats_scrapers` package's ATSType enum at all, so `ScraperRegistry.
    has_scraper` would wrongly say "unsupported" for a platform this
    project actually does support, via `providers/darwinbox.py` and
    `providers/zoho_recruit.py`'s own bespoke fetchers."""
    if ats in ("darwinbox", "zoho_recruit"):
        return True
    from ats_scrapers.scrapers.base import ScraperRegistry
    return ScraperRegistry.has_scraper(ats)


def _passes_quality_bar(rows: list[dict], profile, cutoff: datetime, title_exclusions: list[str]) -> bool:
    """The discovery-specific bar (distinct from watchlist add's "any real
    job" bar): at least one ROLE-MATCHING title posted within the freshness
    window. Deliberately does NOT require geo-eligibility — that stays a
    downstream per-job constraint (see providers/ats_watchlist.py's own
    filter chain, which every entry — including auto-discovered ones —
    passes through on every real `discover` run regardless of this bar).

    `title_exclusions` is caller-supplied (the SAME `providers.ats-watchlist.
    title_exclusions` config block `AtsWatchlistProvider.fetch` reads, see
    `_provider_query_cfg`) rather than the hardcoded default — a company
    must not be admitted on a job title the user's own config would filter
    out once it's actually scraped daily."""
    from careeros.providers.ats_dataset import _title_excluded, row_is_fresh, seniority_excluded

    includes = [r.lower() for r in (getattr(profile, "role_priorities", []) or [])]
    for row in rows:
        title = row.get("title")
        if not row_is_fresh(row, cutoff):
            continue
        if _title_excluded(title, title_exclusions) or seniority_excluded(title):
            continue
        if not includes or any(r in (title or "").lower() for r in includes):
            return True
    return False


@watchlist_app.command("discover")
def watchlist_discover(
    candidate: list[str] = typer.Option(
        ..., "--candidate", "-c",
        help='One candidate per flag: "Name=https://company-domain.com". Repeat for multiple.',
    ),
    max_add: int = typer.Option(
        5, "--max-add",
        help="CEILING on how many clear the bar AND get added this run — remaining qualifying "
             "candidates are still reported, never silently added past this.",
    ),
):
    """Automatic, profile-driven company discovery — the same
    validate-then-append boundary `watchlist add` uses (see `_append_entry`),
    driven by a NAME+website pair instead of a human-supplied
    --url/--ats/--slug. An agent proposing a candidate here carries no more
    authority than one typing --url by hand; nothing is added on say-so.

    Per candidate: already-in-registry check -> already-on-watchlist check
    -> 30-day unresolved-recheck TTL -> ATS resolution
    (careeros/ats_resolve.py, reusing the company's own careers page) ->
    live scrape -> quality bar (>=1 role-matching job posted in the last
    ~90 days) -> append, capped at --max-add.

    A company whose ATS resolves to a real platform this install just
    doesn't have an adapter for is NOT rejected and forgotten — it's kept
    in `.careeros/discovery_candidates.json` as `pending_unsupported_ats`,
    with the resolved ats/slug/website retained. Every future run cheaply
    rechecks whether that platform gained support (zero HTTP — a live
    `ScraperRegistry.has_scraper` call) and promotes it automatically the
    moment it does, using the mapping already on file — no re-discovery,
    no user action. If it's still unsupported after 30 days, the careers
    page itself is re-resolved too, in case the company changed ATS to one
    already supported.

    `--max-add` is a ceiling, never a quota: 2 good candidates out of 15
    adds exactly 2."""
    from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
    from ats_scrapers.fetch import MalformedJSONError

    from careeros.ats_resolve import ResolvedAts, resolve_company_ats
    from careeros.providers.ats_dataset import _DEFAULT_TITLE_EXCLUSIONS

    cfg = _config()
    watchlist_path = cfg.careeros_dir / "watchlist.yaml"
    candidates_path = cfg.careeros_dir / _DISCOVERY_CANDIDATES_FILENAME
    existing = load_watchlist(watchlist_path)
    discovery_state = _load_discovery_candidates(candidates_path)
    today = _today()
    # Same config block AtsWatchlistProvider.fetch reads (ats_watchlist.py's
    # `block.get("title_exclusions", _DEFAULT_TITLE_EXCLUSIONS)`) — a company
    # must not be admitted on a title the user's own daily scrape would drop.
    title_exclusions = _provider_query_cfg(cfg, "ats-watchlist").get("title_exclusions", _DEFAULT_TITLE_EXCLUSIONS)

    try:
        profile = _load_profile(cfg)
    except (OSError, TypeError, KeyError) as e:
        typer.echo(f"[watchlist:discover] could not read .careeros/profile.yaml: {e}", err=True)
        raise typer.Exit(1)

    cutoff = datetime.now(timezone.utc) - timedelta(days=_DISCOVERY_FRESHNESS_DAYS)

    considered = in_registry = already_known = added = pending_unsupported = rejected = 0

    for raw in candidate:
        name, sep, website = raw.partition("=")
        name, website = name.strip(), website.strip()
        if not sep or not name or not website:
            typer.echo(f'[watchlist:discover] skipping malformed --candidate {raw!r} — expected "Name=https://domain"')
            continue
        considered += 1

        # ADVISORY only, never a skip: registry presence is a company-
        # directory hit, not a claim that Layer 1's job dataset actually
        # carries this company (docs/ats-registry.md documents Swiggy as
        # exactly this case — registered, but confirmed absent from its
        # ATS's dataset slice). Overlap with real Layer 1 coverage is
        # absorbed by cross-layer dedupe and measured by the ledger, both
        # already built for this. Adopt the canonical identity when found,
        # same as `watchlist add` does, so this candidate and any existing
        # registry-sourced entry can never diverge into two identities.
        registry_match = _resolve_via_registry(cfg, name)
        if registry_match is not None:
            canonical_name = registry_match[0]
            typer.echo(
                f"[watchlist:discover] {name!r}: also present in the reference registry as "
                f"{canonical_name!r} (registry presence ≠ Layer 1 job coverage — continuing to validate live)."
            )
            name = canonical_name
            in_registry += 1

        norm = normalize_name(name)

        if any(normalize_name(e.name) == norm for e in existing):
            typer.echo(f"[watchlist:discover] {name!r}: already on the watchlist — skipping.")
            already_known += 1
            continue

        record = discovery_state.get(norm)
        resolved: ResolvedAts | None = None

        if record is not None and record.get("status") == "pending_unsupported_ats":
            # A known company/ATS mapping, just waiting on adapter support —
            # never re-fetches the careers page for this alone; only a cheap
            # local registry check, unless the TTL below decides the company
            # itself is worth re-probing.
            if _has_adapter(record["ats"]):
                resolved = ResolvedAts(record["ats"], record["slug"], record.get("website", website))
            else:
                age = _days_since(record.get("last_checked_at", ""), today)
                if age is not None and age < _DISCOVERY_TTL_DAYS:
                    typer.echo(
                        f"[watchlist:discover] {name!r}: ATS {record['ats']!r} still unsupported "
                        f"(checked {record.get('last_checked_at')}) — skipping."
                    )
                    already_known += 1
                    continue
                # TTL expired (or unparseable) — falls through to a fresh
                # resolve below, in case the company itself moved ATS.
        elif record is not None and record.get("status") == "unresolved":
            age = _days_since(record.get("last_checked_at", ""), today)
            if age is not None and age < _DISCOVERY_TTL_DAYS:
                typer.echo(f"[watchlist:discover] {name!r}: recently unresolved (within {_DISCOVERY_TTL_DAYS}d) — skipping.")
                already_known += 1
                continue

        if resolved is None:
            resolved = resolve_company_ats(website)
        if resolved is None:
            typer.echo(f"[watchlist:discover] {name!r}: UNRESOLVED — no detectable ATS at {website}")
            discovery_state[norm] = {"status": "unresolved", "last_checked_at": today, "reason": "no detectable ATS"}
            rejected += 1
            continue

        if not _has_adapter(resolved.ats):
            typer.echo(
                f"[watchlist:discover] {name!r}: ATS detected ({resolved.ats}/{resolved.slug}) but no adapter "
                f"installed yet — kept as pending_unsupported_ats, will auto-promote once supported."
            )
            discovery_state[norm] = {
                "status": "pending_unsupported_ats", "ats": resolved.ats, "slug": resolved.slug,
                "website": website, "last_checked_at": today,
            }
            pending_unsupported += 1
            continue

        wc = WatchlistEntry(name=name, ats=resolved.ats, slug=resolved.slug)
        try:
            resolved_ats, rows = _scrape_entry(wc)
        except CompanyNotFoundError as e:
            typer.echo(f"[watchlist:discover] {name!r}: UNRESOLVED — {resolved.ats}/{resolved.slug} not found: {e}")
            discovery_state[norm] = {"status": "unresolved", "last_checked_at": today, "reason": f"not found on {resolved.ats}"}
            rejected += 1
            continue
        except (WatchlistConfigError, MalformedJSONError, ScraperError) as e:
            typer.echo(f"[watchlist:discover] {name!r}: UNRESOLVED — could not validate: {e}")
            discovery_state[norm] = {"status": "unresolved", "last_checked_at": today, "reason": str(e)[:200]}
            rejected += 1
            continue

        if not _passes_quality_bar(rows, profile, cutoff, title_exclusions):
            typer.echo(
                f"[watchlist:discover] {name!r}: UNRESOLVED — {resolved.ats}/{resolved.slug} has "
                f"{len(rows)} job(s) but none role-matching within {_DISCOVERY_FRESHNESS_DAYS}d"
            )
            discovery_state[norm] = {"status": "unresolved", "last_checked_at": today, "reason": "no role-matching job within freshness window"}
            rejected += 1
            continue

        if added >= max_add:
            typer.echo(
                f"[watchlist:discover] {name!r}: qualified ({resolved.ats}/{resolved.slug}, {len(rows)} job(s)) "
                f"but --max-add {max_add} reached this run — not added."
            )
            continue

        entry = _append_entry(
            watchlist_path, canonical_name=name, resolved_ats=resolved_ats,
            candidate_ats=resolved.ats, candidate_slug=resolved.slug,
            candidate_url=None, source="auto_discovered", website=website,
        )
        discovery_state.pop(norm, None)  # on the watchlist now — no longer a "candidate"
        typer.echo(
            f"[watchlist:discover] {name!r}: VALIDATED -> {entry.get('ats')}/{entry.get('slug')} "
            f"({len(rows)} live job(s)) -> {watchlist_path}"
        )
        added += 1

    _save_discovery_candidates(candidates_path, discovery_state)
    typer.echo(
        f"[watchlist:discover] considered {considered}, also-in-registry {in_registry} (informational, not skipped), "
        f"already-known {already_known}, added {added}, pending-unsupported-ats {pending_unsupported}, "
        f"unresolved/rejected {rejected}."
    )


@watchlist_app.command("list")
def watchlist_list():
    """Show every watchlist.yaml entry with its last-known scrape status,
    plus a summary of `watchlist discover` candidates parked in
    `.careeros/discovery_candidates.json` — read-only; state comes from
    whatever `careeros discover`/`watchlist discover` last recorded."""
    cfg = _config()
    entries = load_watchlist(cfg.careeros_dir / "watchlist.yaml")
    if not entries:
        typer.echo("[watchlist:list] .careeros/watchlist.yaml is empty or missing — see templates/watchlist.example.yaml.")
    else:
        state = load_state(cfg.careeros_dir / STATE_FILENAME)
        for entry in entries:
            s = state.get(entry_key(entry), {})
            status = s.get("verification_status", "never checked")
            ats = s.get("ats") or entry.ats or "?"
            jobs = s.get("job_count")
            checked = s.get("last_checked_at")
            detail = f"jobs={jobs}" if jobs is not None else "not yet scraped"
            when = f" (checked {checked})" if checked else ""
            typer.echo(f"{entry.name:<30} {ats:<14} status={status:<16} {detail}{when}")

    candidates = _load_discovery_candidates(cfg.careeros_dir / _DISCOVERY_CANDIDATES_FILENAME)
    pending = {k: v for k, v in candidates.items() if v.get("status") == "pending_unsupported_ats"}
    unresolved = {k: v for k, v in candidates.items() if v.get("status") == "unresolved"}
    if not pending and not unresolved:
        return
    typer.echo("")
    typer.echo(f"[watchlist:list] discovery candidates ({_DISCOVERY_CANDIDATES_FILENAME}):")
    for norm, rec in sorted(pending.items()):
        typer.echo(
            f"  {norm:<28} pending_unsupported_ats  ats={rec.get('ats')}/{rec.get('slug')}  "
            f"will auto-promote once supported (last checked {rec.get('last_checked_at')})"
        )
    if unresolved:
        typer.echo(f"  ...and {len(unresolved)} unresolved candidate(s), retried automatically after the TTL.")
