"""`careeros ledger` (v2.0) — aggregate per-tier discovery statistics across
recent runs into `.careeros/learning/{ledger.jsonl, ledger.md}`. See
careeros/pipeline/ledger.py for the actual aggregation logic; this module is
thin CLI wiring only (finding which dates to include, writing the output).
"""

from __future__ import annotations

import json
from datetime import date as _date

import typer

from careeros import runmeta
from careeros.cli import app
from careeros.cli._shared import _config
from careeros.config import Config
from careeros.pipeline import ledger as ledger_mod


def _candidate_dates(cfg: Config) -> list[str]:
    """Every date under .careeros/runs/ that looks like an ISO date and has
    an outcomes.json (i.e. `threshold` actually ran) — a qa-*/free-text run
    label is skipped automatically rather than needing to be quarantined by
    hand."""
    if not cfg.runs_dir.exists():
        return []
    out = []
    for p in sorted(cfg.runs_dir.iterdir()):
        if not p.is_dir():
            continue
        try:
            _date.fromisoformat(p.name)
        except ValueError:
            continue
        if (p / "07_select" / "outcomes.json").exists():
            out.append(p.name)
    return sorted(out)


@app.command(hidden=True)
def ledger(
    since: int = typer.Option(30, "--since", help="How many recent (non-quarantined) runs to aggregate"),
):
    """[dev] Aggregate per-discovery-tier stats across recent runs into
    .careeros/learning/{ledger.jsonl,ledger.md} — zero AI, fully
    regenerable. Quarantined dates (.careeros/learning/quarantine.json) are
    excluded entirely, never partially weighted."""
    cfg = _config()
    quarantined = ledger_mod.load_quarantine(cfg)
    dates = [d for d in _candidate_dates(cfg) if d not in quarantined]
    dates = dates[-since:] if since > 0 else dates

    if not dates:
        typer.echo(
            "[ledger] No eligible runs found (need at least one `careeros threshold` "
            "run with 07_select/outcomes.json, and not quarantined). Nothing to do."
        )
        return

    all_entries = []
    for d in dates:
        all_entries.extend(ledger_mod.compute_run_tier_stats(cfg, d))

    learning_dir = cfg.careeros_dir / "learning"
    learning_dir.mkdir(parents=True, exist_ok=True)

    jsonl_path = learning_dir / "ledger.jsonl"
    with open(jsonl_path, "w") as f:
        for e in all_entries:
            f.write(json.dumps(e.to_dict(), sort_keys=True) + "\n")

    summary = ledger_mod.aggregate_entries(all_entries)
    arming = ledger_mod.compute_arming(summary)
    md = ledger_mod.render_ledger_markdown(summary, arming=arming)
    md_path = learning_dir / "ledger.md"
    with open(md_path, "w") as f:
        f.write(md)

    typer.echo(
        f"[ledger] Aggregated {len(dates)} run(s) ({dates[0]}..{dates[-1]}) across "
        f"{len(summary)} discovery tier(s) -> {jsonl_path}, {md_path}"
    )
    if quarantined:
        typer.echo(f"[ledger] Excluded {len(quarantined)} quarantined date(s): {', '.join(sorted(quarantined))}")
    for tier, a in sorted(arming.items()):
        status = "ARMED" if a["armed"] else f"dormant ({a['reason']})"
        typer.echo(f"[ledger]   {tier}: {status}")
