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

import typer

from careeros import registry as registry_mod
from careeros.cli import app
from careeros.cli._shared import _config, _today
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


@watchlist_app.command("add")
def watchlist_add(
    name: str = typer.Argument(..., help="Company name"),
    url: str = typer.Option(None, "--url", help="Careers URL, resolved automatically via ats-scrapers"),
    ats: str = typer.Option(None, "--ats", help="Explicit ATS platform (use together with --slug)"),
    slug: str = typer.Option(None, "--slug", help="Explicit tenant slug (use together with --ats)"),
):
    """Resolve NAME into a validated watchlist entry — P0.12 user-provided
    ingestion, the only automated company-discovery entry point this
    project ships today (see docs/ats-registry.md for why proactive
    discovery is deferred).

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
        matches = registry_mod.find_company(cfg, name, limit=1)
        if matches:
            m = matches[0]
            canonical_name = m.get("name") or name
            candidate_ats, candidate_slug = m.get("ats"), m.get("slug")
            source = "reference_registry"
            typer.echo(f"[watchlist:add] found in reference registry: {canonical_name!r} -> {candidate_ats}/{candidate_slug}")
        else:
            typer.echo(
                f"[watchlist:add] UNRESOLVED — {name!r} not in the reference registry "
                "and no --url/--ats+--slug given. Supply one to try a specific mapping."
            )
            raise typer.Exit(1)

    # Dedup on NORMALIZED identity (never raw string equality) — "Swiggy" and
    # "swiggy, Inc." must collide, per the same identity rule enforced for
    # the reference registry. Same normalized company + same ats is a
    # duplicate; same company on a DIFFERENT ats is a legitimate second
    # board (see entry_key's docstring on multi-board support) and is
    # allowed through to validation below.
    new_norm = normalize_name(canonical_name)
    for e in existing:
        if normalize_name(e.name) == new_norm and e.ats == candidate_ats:
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

    entry = {
        "name": canonical_name, "ats": resolved_ats or candidate_ats, "slug": candidate_slug,
        "careers_url": candidate_url, "source": source, "added_at": _today(),
    }
    entry = {k: v for k, v in entry.items() if v is not None}
    append_watchlist_entry(watchlist_path, entry)
    typer.echo(
        f"[watchlist:add] VALIDATED — {canonical_name!r} -> {entry.get('ats')}/"
        f"{entry.get('slug') or entry.get('careers_url')} ({len(rows)} live job(s)) -> {watchlist_path}"
    )


@watchlist_app.command("list")
def watchlist_list():
    """Show every watchlist.yaml entry with its last-known scrape status —
    read-only; state comes from whatever `careeros discover` last recorded."""
    cfg = _config()
    entries = load_watchlist(cfg.careeros_dir / "watchlist.yaml")
    if not entries:
        typer.echo("[watchlist:list] .careeros/watchlist.yaml is empty or missing — see templates/watchlist.example.yaml.")
        return
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
