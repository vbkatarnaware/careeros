"""`careeros tune` (v2.0) — the query-tuning loop's CLI surface. SHIPS
DORMANT: every sub-command re-checks per-tier arming
(careeros/pipeline/ledger.py's compute_arming) itself, so nothing here can
act on a tier before ~4 weeks / 400 records / 8 apply-or-consider events
exist for it — see that module for the sample-size arithmetic behind those
numbers.

Deliberately NOT a keyword-mining oracle: `--propose` does not invent an
exclusion term or a new limit on its own. The ledger (careeros/pipeline/
ledger.py) aggregates rejection REASONS (e.g. "role-mismatch") but not
per-job titles by design — keeping per-job title data out of the ledger is
what keeps the tuner's only inputs to deterministic, gate-derived signals
(see the Goodhart-guard note in ledger.py's module docstring). Deciding the
actual proposed value (which title to exclude, what the new limit should
be) and citing its evidence is a human or reviewing-agent's call, made by
reading `careeros tune --status`'s reject-reason breakdown and the day's
actual gate/constraints output; `--propose` then mechanically validates
that specific, already-decided change against every guardrail before it's
allowed anywhere near the overlay.

Never touches `.careeros/config.yaml` — only `.careeros/tuning/
overlay.yaml`, merged in by careeros/config.py's `load_config` under a
hard-coded key allowlist (`TUNING_OVERLAY_ALLOWLIST`) that this module
cannot widen no matter what it writes.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

import typer
import yaml

from careeros import runmeta
from careeros.cli import app
from careeros.cli._shared import _config, _today
from careeros.config import Config
from careeros.pipeline import ledger as ledger_mod
from careeros.pipeline.tuning import (
    Proposal, apply_proposal_to_overlay, find_tuning_flags, is_check_due, validate_proposal,
)


def _tuning_dir(cfg: Config) -> Path:
    return cfg.careeros_dir / "tuning"


def _overlay_path(cfg: Config) -> Path:
    return Path(cfg.tuning.get("overlay_path", ".careeros/tuning/overlay.yaml"))


def _read_overlay_api(cfg: Config) -> dict:
    path = _overlay_path(cfg)
    if not path.exists():
        return {}
    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    return raw.get("api", {}) or {}


def _write_overlay_api(cfg: Config, api: dict) -> None:
    path = _overlay_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.safe_dump({"api": api}, f, sort_keys=True)


def _changes_path(cfg: Config) -> Path:
    return _tuning_dir(cfg) / "changes.jsonl"


def _load_changes(cfg: Config) -> list[dict]:
    path = _changes_path(cfg)
    if not path.exists():
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def _changes_this_cycle(cfg: Config, cadence_days: int, today: str) -> int:
    """How many changes were applied within `cadence_days` of today — the
    max-one-change-per-cycle guardrail."""
    from datetime import date as _date
    changes = _load_changes(cfg)
    try:
        today_d = _date.fromisoformat(today)
    except ValueError:
        return 0
    count = 0
    for c in changes:
        if c.get("reverted"):
            continue
        try:
            applied = _date.fromisoformat(c["date"])
        except (ValueError, KeyError):
            continue
        if (today_d - applied).days < cadence_days:
            count += 1
    return count


def _ledger_summary(cfg: Config, since: int = 30) -> tuple[dict, dict]:
    quarantined = ledger_mod.load_quarantine(cfg)
    from careeros.cli.ledger_cmd import _candidate_dates
    dates = [d for d in _candidate_dates(cfg) if d not in quarantined]
    dates = dates[-since:] if since > 0 else dates
    entries = []
    for d in dates:
        entries.extend(ledger_mod.compute_run_tier_stats(cfg, d))
    summary = ledger_mod.aggregate_entries(entries)
    arming = ledger_mod.compute_arming(
        summary,
        min_days=cfg.tuning.get("min_days", 28),
        min_records=cfg.tuning.get("min_records", 400),
        min_events=cfg.tuning.get("min_events", 8),
    )
    return summary, arming


@app.command(hidden=True)
def tune(
    status: bool = typer.Option(False, "--status", help="Show per-tier arming status and current overlay/changes"),
    check_due: bool = typer.Option(
        False, "--check-due",
        help="Weekly, automatic-safe check: if due, refresh the ledger and report any tier with a "
             "pattern worth asking the candidate about (see prompts/tune_v1.md). Cheap and safe to "
             "call on every `daily` run — a no-op most days.",
    ),
    propose: bool = typer.Option(False, "--propose", help="Validate and stage a specific change (see --tier/--field/--value/--evidence)"),
    apply_: bool = typer.Option(False, "--apply", help="Apply the currently staged proposal.json to the overlay"),
    revert: bool = typer.Option(False, "--revert", help="Revert the most recent non-reverted change"),
    tier: Optional[str] = typer.Option(None, help="--propose: which discovery tier"),
    field: Optional[str] = typer.Option(None, help="--propose: title_search | title_exclusion_search | tier_limits"),
    value: Optional[str] = typer.Option(None, help="--propose: the value to add/set (tier_limits takes an int)"),
    evidence: Optional[str] = typer.Option(None, help="--propose: what motivates this change — required, never generated automatically"),
    sunset: Optional[str] = typer.Option(None, help="--propose: ISO date this exclusion expires (required for title_exclusion_search)"),
):
    """[dev] v2.0 query-tuning loop. Dormant until a tier is armed — see
    careeros/pipeline/ledger.py's compute_arming. See this module's
    docstring for why --propose asks you to name the change rather than
    inventing one."""
    cfg = _config()
    all_flags = [status, check_due, propose, apply_, revert]
    if sum(bool(f) for f in all_flags) != 1:
        typer.echo("Pass exactly one of --status, --check-due, --propose, --apply, --revert.", err=True)
        raise typer.Exit(1)

    if status:
        _tune_status(cfg)
    elif check_due:
        _tune_check_due(cfg)
    elif propose:
        _tune_propose(cfg, tier=tier, field=field, value=value, evidence=evidence, sunset=sunset)
    elif apply_:
        _tune_apply(cfg)
    else:
        _tune_revert(cfg)


def _last_check_path(cfg: Config) -> Path:
    return _tuning_dir(cfg) / "last_check.json"


def _tune_check_due(cfg: Config) -> None:
    """The automatic, daily-safe entry point (see skills/daily.md's Step 0.5
    and prompts/tune_v1.md). Cheap on a not-due day (one small file read, no
    ledger recompute). On a due day: refreshes the ledger, checks arming,
    and — only if something is actually worth a candidate's attention —
    writes `.careeros/tuning/pending_flags.json` and prints each flag's
    plain-language `label`. Prints NOTHING flag-shaped on a quiet week; the
    calling agent should stay silent too in that case (matches the
    candidate's own stated preference — only speak up when there's a real
    decision to make)."""
    today = _today()
    last_check_path = _last_check_path(cfg)
    last_checked = None
    if last_check_path.exists():
        with open(last_check_path) as f:
            last_checked = json.load(f).get("last_checked")

    cadence = cfg.tuning.get("check_cadence_days", 7)
    if not is_check_due(last_checked, today, cadence_days=cadence):
        typer.echo(f"[tune:check-due] Not due yet (last checked {last_checked}, every {cadence}d).")
        return

    summary, arming = _ledger_summary(cfg)
    flags = find_tuning_flags(summary, arming, control_tier=cfg.tuning.get("control_tier", "india_remote"))

    last_check_path.parent.mkdir(parents=True, exist_ok=True)
    with open(last_check_path, "w") as f:
        json.dump({"last_checked": today}, f, indent=2)

    pending_path = _tuning_dir(cfg) / "pending_flags.json"
    if flags:
        with open(pending_path, "w") as f:
            json.dump([fl.to_dict() for fl in flags], f, indent=2, sort_keys=True)
        typer.echo(f"[tune:check-due] {len(flags)} pattern(s) worth asking the candidate about:")
        for fl in flags:
            typer.echo(f"  - {fl.label}")
        typer.echo(
            "Agent: ask the candidate about each one in plain language (see prompts/tune_v1.md) "
            "before continuing to Step 1. If they say yes, use `careeros tune --propose` with the "
            "specific value they agreed to, citing this evidence."
        )
    else:
        if pending_path.exists():
            pending_path.unlink()
        typer.echo("[tune:check-due] Checked — nothing worth flagging this week.")


def _tune_status(cfg: Config) -> None:
    summary, arming = _ledger_summary(cfg)
    if not summary:
        typer.echo("[tune:status] No ledger data yet — run `careeros ledger` after at least one dated run.")
        return
    overlay_api = _read_overlay_api(cfg)
    changes = [c for c in _load_changes(cfg) if not c.get("reverted")]
    typer.echo(f"[tune:status] {len(summary)} tier(s) in the ledger:")
    for t in sorted(summary):
        a = arming[t]
        s = summary[t]
        state = "ARMED" if a["armed"] else f"dormant ({a['reason']})"
        typer.echo(
            f"  {t}: {state} — {s['total_records']} records, "
            f"{s['apply_or_consider_events']} apply/consider events, "
            f"gate-keep {s['pooled_gate_keep_rate']:.1%}" if s["pooled_gate_keep_rate"] is not None
            else f"  {t}: {state} — {s['total_records']} records"
        )
    typer.echo(f"[tune:status] current overlay ({_overlay_path(cfg)}): {overlay_api or '(empty)'}")
    typer.echo(f"[tune:status] {len(changes)} active (non-reverted) change(s) on record.")


def _tune_propose(
    cfg: Config, *, tier: Optional[str], field: Optional[str],
    value: Optional[str], evidence: Optional[str], sunset: Optional[str],
) -> None:
    if not all([tier, field, evidence]) or value is None:
        typer.echo(
            "Usage: careeros tune --propose --tier T --field {title_search|title_exclusion_search|tier_limits} "
            "--value V --evidence \"why\" [--sunset YYYY-MM-DD]",
            err=True,
        )
        raise typer.Exit(1)

    parsed_value: object = value
    if field == "tier_limits":
        try:
            parsed_value = int(value)
        except ValueError:
            typer.echo(f"--value must be an integer for tier_limits, got {value!r}", err=True)
            raise typer.Exit(1)

    proposal = Proposal(tier=tier, field=field, value=parsed_value, evidence=evidence, sunset=sunset)

    _summary, arming = _ledger_summary(cfg)
    overlay_api = _read_overlay_api(cfg)
    today = _today()
    cadence_days = cfg.tuning.get("cadence_days", 30)
    changes_this_cycle = _changes_this_cycle(cfg, cadence_days, today)

    # num_tiers/daily_total feed the record floor (careeros/pipeline/
    # tuning.py's record_floor) -- best-effort from the live profile/api
    # config rather than re-deriving the full query plan here.
    num_tiers = max(1, len(_summary_tier_names(cfg)))
    daily_total = cfg.api.get("limit") or 100

    violations = validate_proposal(
        proposal, arming=arming, control_tier=cfg.tuning.get("control_tier", "india_remote"),
        current_overlay_api=overlay_api, changes_this_cycle=changes_this_cycle,
        num_tiers=num_tiers, daily_total=daily_total,
        max_exclusion_entries=cfg.tuning.get("max_exclusion_entries", 8),
        tier_limit_max_delta_pct=cfg.tuning.get("tier_limit_max_delta_pct", 0.25),
    )
    if violations:
        typer.echo("[tune:propose] REJECTED:\n" + "\n".join(f"  - {v}" for v in violations), err=True)
        raise typer.Exit(1)

    proposal_path = _tuning_dir(cfg) / "proposal.json"
    proposal_path.parent.mkdir(parents=True, exist_ok=True)
    with open(proposal_path, "w") as f:
        json.dump({**proposal.to_dict(), "proposed_at": today}, f, indent=2, sort_keys=True)
    typer.echo(f"[tune:propose] Valid. Staged to {proposal_path} — run `careeros tune --apply` to apply it.")


def _summary_tier_names(cfg: Config) -> list[str]:
    summary, _ = _ledger_summary(cfg)
    return list(summary.keys())


def _tune_apply(cfg: Config) -> None:
    proposal_path = _tuning_dir(cfg) / "proposal.json"
    if not proposal_path.exists():
        typer.echo("No staged proposal — run `careeros tune --propose` first.", err=True)
        raise typer.Exit(1)
    with open(proposal_path) as f:
        staged = json.load(f)
    proposal = Proposal(tier=staged["tier"], field=staged["field"], value=staged["value"],
                        evidence=staged["evidence"], sunset=staged.get("sunset"))

    # Re-validate against CURRENT state -- ledger/overlay may have moved
    # since --propose ran.
    summary, arming = _ledger_summary(cfg)
    overlay_api = _read_overlay_api(cfg)
    today = _today()
    cadence_days = cfg.tuning.get("cadence_days", 30)
    changes_this_cycle = _changes_this_cycle(cfg, cadence_days, today)
    num_tiers = max(1, len(summary))
    daily_total = cfg.api.get("limit") or 100

    violations = validate_proposal(
        proposal, arming=arming, control_tier=cfg.tuning.get("control_tier", "india_remote"),
        current_overlay_api=overlay_api, changes_this_cycle=changes_this_cycle,
        num_tiers=num_tiers, daily_total=daily_total,
        max_exclusion_entries=cfg.tuning.get("max_exclusion_entries", 8),
        tier_limit_max_delta_pct=cfg.tuning.get("tier_limit_max_delta_pct", 0.25),
    )
    if violations:
        typer.echo(
            "[tune:apply] REJECTED on re-validation (state changed since --propose):\n"
            + "\n".join(f"  - {v}" for v in violations), err=True,
        )
        raise typer.Exit(1)

    before = (
        list(overlay_api.get(proposal.field, [])) if proposal.field != "tier_limits"
        else dict(overlay_api.get("tier_limits", {})).get(proposal.tier)
    )
    new_api = apply_proposal_to_overlay(overlay_api, proposal)
    _write_overlay_api(cfg, new_api)

    change_record = {
        **proposal.to_dict(), "date": today, "before": before,
        "after": proposal.value, "reverted": False,
    }
    changes_path = _changes_path(cfg)
    changes_path.parent.mkdir(parents=True, exist_ok=True)
    with open(changes_path, "a") as f:
        f.write(json.dumps(change_record, sort_keys=True) + "\n")

    proposal_path.unlink()
    typer.echo(f"[tune:apply] Applied {proposal.field}={proposal.value!r} to tier '{proposal.tier}'. Logged to {changes_path}.")


def _tune_revert(cfg: Config) -> None:
    changes = _load_changes(cfg)
    active = [c for c in changes if not c.get("reverted")]
    if not active:
        typer.echo("[tune:revert] No active (non-reverted) change to revert.")
        return
    target = active[-1]

    overlay_api = _read_overlay_api(cfg)
    if target["field"] == "tier_limits":
        limits = dict(overlay_api.get("tier_limits", {}))
        if target["before"] is None:
            limits.pop(target["tier"], None)
        else:
            limits[target["tier"]] = target["before"]
        overlay_api = {**overlay_api, "tier_limits": limits}
    else:
        current = list(overlay_api.get(target["field"], []))
        if target["after"] in current:
            current.remove(target["after"])
        overlay_api = {**overlay_api, target["field"]: current}
    _write_overlay_api(cfg, overlay_api)

    # Rewrite changes.jsonl marking this entry reverted, and quarantine it —
    # auto-revert is one-way: it may restore without asking, but a reverted
    # change must never be silently re-applied. Re-authorization is a human
    # decision (re-run --propose deliberately), tracked here only as a record.
    all_changes = _load_changes(cfg)
    for c in all_changes:
        if c is target or (c["date"] == target["date"] and c["tier"] == target["tier"]
                           and c["field"] == target["field"] and not c.get("reverted")):
            c["reverted"] = True
            c["reverted_at"] = _today()
    with open(_changes_path(cfg), "w") as f:
        for c in all_changes:
            f.write(json.dumps(c, sort_keys=True) + "\n")

    quarantine_path = _tuning_dir(cfg) / "quarantine.json"
    quarantine = json.load(open(quarantine_path)) if quarantine_path.exists() else {"reverted_changes": []}
    quarantine.setdefault("reverted_changes", []).append({**target, "reverted": True, "reverted_at": _today()})
    with open(quarantine_path, "w") as f:
        json.dump(quarantine, f, indent=2, sort_keys=True)

    typer.echo(
        f"[tune:revert] Reverted {target['field']} for tier '{target['tier']}' "
        f"(back to {target['before']!r}). Quarantined in {quarantine_path} -- "
        "requires a fresh --propose to re-apply, never automatic."
    )
