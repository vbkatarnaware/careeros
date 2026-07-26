"""`careeros doctor` — read-only environment/credentials checklist."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

from careeros import budget
from careeros.cli import app
from careeros.cli._shared import _config, _load_profile
from careeros.config import Config, enabled_providers
from careeros.pipeline.queryplan import build_query_plan
from careeros.providers.base import ProviderError
from careeros.providers.registry import get as get_provider


def _latest_discovery_meta(cfg: Config) -> tuple[Optional[str], dict[str, dict]]:
    """The most recent run's `01_discover/raw.json` "meta" block (per
    provider: cost/requests/records/seconds/skipped/skip_reason — exactly
    what `discover` already writes, nothing new persisted for this), if any
    run has happened yet. Read-only, no network call — safe for `doctor` to
    call on every invocation. Returns (date, meta) or (None, {}) if
    `.careeros/runs/` has no discover output at all. Run dates are ISO
    (`YYYY-MM-DD`) or a QA label; picking the lexicographically latest
    directory that actually has a raw.json is a reasonable "most recent"
    without needing a separate index file."""
    runs_dir = cfg.runs_dir
    if not runs_dir.exists():
        return None, {}
    candidates = sorted(
        (d for d in runs_dir.iterdir() if d.is_dir() and (d / "01_discover" / "raw.json").exists()),
        key=lambda d: d.name,
    )
    if not candidates:
        return None, {}
    latest = candidates[-1]
    try:
        raw = json.loads((latest / "01_discover" / "raw.json").read_text())
    except (json.JSONDecodeError, OSError):
        return latest.name, {}
    return latest.name, raw.get("meta", {})


class _CheckStatus:
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


def _check_result(status: str, label: str, detail: str = "") -> tuple[str, str, str]:
    return (status, label, detail)


def _run_doctor_checks(cfg: Config) -> list[tuple[str, str, str]]:
    """Pure(ish) — reads env vars, config, and the filesystem; makes no
    network calls and changes nothing. Each check is independent so one
    failure never hides the rest."""
    import os
    import sys as _sys

    results: list[tuple[str, str, str]] = []

    # Python version
    if _sys.version_info >= (3, 11):
        results.append(_check_result(_CheckStatus.PASS, "Python version",
                                     f"{_sys.version_info.major}.{_sys.version_info.minor} (>= 3.11 required)"))
    else:
        results.append(_check_result(_CheckStatus.FAIL, "Python version",
                                     f"{_sys.version_info.major}.{_sys.version_info.minor} — CareerOS needs Python 3.11+"))

    # .careeros/ scaffolding
    if not cfg.careeros_dir.exists():
        results.append(_check_result(_CheckStatus.FAIL, ".careeros/ scaffolding",
                                     "not found — run `careeros init` first"))
        return results  # nothing else is checkable yet
    results.append(_check_result(_CheckStatus.PASS, ".careeros/ scaffolding", str(cfg.careeros_dir)))

    # Profile
    if not cfg.profile_path.exists():
        results.append(_check_result(_CheckStatus.FAIL, "Profile (.careeros/profile.yaml)",
                                     "not found — run `/careeros start` or hand-edit the template"))
    else:
        try:
            _load_profile(cfg)
            results.append(_check_result(_CheckStatus.PASS, "Profile (.careeros/profile.yaml)", "present and valid"))
        except Exception as e:
            results.append(_check_result(_CheckStatus.FAIL, "Profile (.careeros/profile.yaml)",
                                         f"invalid — {type(e).__name__}: {e}"))

    # Discovery provider credentials — looped over every ENABLED provider
    # (config.providers), since several can run at once. Fantastic Jobs keeps
    # its existing rich, battle-tested diagnostics (transport/endpoint/
    # last-error/discovery-limit); every other provider gets a uniform check
    # via its own `validate()` — the same method `discover` calls before
    # `fetch()`.
    active = enabled_providers(cfg)

    if "fantastic-jobs" in active:
        transport = cfg.api.get("transport")
        if transport == "direct":
            key_env = cfg.api.get("api_key_env", "FANTASTIC_API_KEY")
            if os.environ.get(key_env):
                results.append(_check_result(_CheckStatus.PASS, "Discovery credentials",
                                             f"transport=direct, {key_env} is set"))
            else:
                results.append(_check_result(_CheckStatus.FAIL, "Discovery credentials",
                                             f"transport=direct but {key_env} is not set"))
        elif transport == "rapidapi":
            key_env = cfg.api.get("rapidapi_key_env", "RAPIDAPI_KEY")
            if os.environ.get(key_env):
                results.append(_check_result(_CheckStatus.PASS, "Discovery credentials",
                                             f"transport=rapidapi, {key_env} is set"))
            else:
                results.append(_check_result(_CheckStatus.FAIL, "Discovery credentials",
                                             f"transport=rapidapi but {key_env} is not set"))
        else:
            results.append(_check_result(_CheckStatus.FAIL, "Discovery credentials",
                                         'api.transport not set — choose "direct" or "rapidapi" in config.yaml'))
        endpoint = cfg.api.get("endpoint", "both")
        results.append(_check_result(_CheckStatus.PASS, "Discovery endpoint", f"endpoint={endpoint}"))

        # Last discovery failure (P2.9) — LOCAL STATE ONLY: read from the file
        # `discover` wrote on its last failed attempt. Never a live API call,
        # so `doctor` never spends quota just by being run.
        last_error = budget.load_last_error(cfg.careeros_dir)
        if last_error:
            results.append(_check_result(_CheckStatus.WARN, "Last discovery run",
                                         f"failed on {last_error.get('date')}: {last_error.get('message')}"))
        else:
            results.append(_check_result(_CheckStatus.PASS, "Last discovery run", "no recorded failures"))

        # Recommended vs configured discovery limit — same formula
        # `careeros config`/`start` already print, surfaced here too so
        # `doctor` is a one-stop diagnostic. Display only; never mutates.
        # Compares DAILY TOTALS (the unit api.limit is in since v1.7) and
        # spells out the per-search split, so tier drift is visible: add a
        # work_mode_priority entry and the daily total holds while the
        # per-search number drops.
        if cfg.profile_path.exists():
            try:
                num_queries = len(build_query_plan(_load_profile(cfg), cfg.api)) or 1
            except Exception:
                num_queries = 1
            rec = budget.recommend(cfg.api, cfg.goals, num_queries)
            if rec.quota and rec.recommended_daily_total is not None:
                plan_note = f"{rec.plan} — assumed default, set api.plan to silence" if rec.plan_is_assumed else rec.plan
                split = (f"{rec.configured_per_search} per search × {num_queries} search(es)")
                if rec.configured_records_per_day > rec.recommended_daily_total:
                    results.append(_check_result(
                        _CheckStatus.WARN, "Discovery limit",
                        f"current={rec.configured_records_per_day} jobs/day ({split}), "
                        f"recommended={rec.recommended_daily_total} "
                        f"(plan {plan_note}: {rec.quota} records/wk ÷ {rec.active_days} active days) "
                        "— edit api.limit in .careeros/config.yaml, or re-run `careeros start`."
                    ))
                else:
                    results.append(_check_result(
                        _CheckStatus.PASS, "Discovery limit",
                        f"current={rec.configured_records_per_day} jobs/day ({split}), "
                        f"recommended={rec.recommended_daily_total} — within quota"
                    ))
    # Every other provider: uniform validate()-based check, no special cases.
    # An unregistered name (a typo in config.yaml's `providers:`, or a source
    # removed by an upgrade) is a config problem to REPORT, not a traceback —
    # `doctor` exists to explain exactly this kind of breakage.
    for name in active:
        if name == "fantastic-jobs":
            continue
        try:
            provider = get_provider(name)
        except ValueError as e:
            results.append(_check_result(_CheckStatus.FAIL, f"Discovery provider ({name})", str(e)))
            continue
        problems = provider.validate(cfg)
        if problems:
            results.append(_check_result(_CheckStatus.FAIL, f"Discovery credentials ({name})",
                                         "; ".join(problems)))
        else:
            results.append(_check_result(_CheckStatus.PASS, f"Discovery credentials ({name})", "configured"))

    # v1.3: per-provider last-run health + timing, read from the most
    # recent discover run's already-persisted raw.json "meta" block — no
    # new data model, no network call, just surfacing what's already there
    # (helps answer "which provider is slow / which one keeps failing"
    # without grepping run history by hand).
    latest_date, latest_meta = _latest_discovery_meta(cfg)
    if latest_date:
        for name in active:
            provider_meta = latest_meta.get(name)
            if provider_meta is None:
                results.append(_check_result(_CheckStatus.WARN, f"Last run ({name})",
                                             f"never run as of {latest_date}"))
            elif provider_meta.get("skipped"):
                results.append(_check_result(
                    _CheckStatus.WARN, f"Last run ({name})",
                    f"skipped on {latest_date} — {provider_meta.get('skip_reason') or 'unknown reason'}"
                ))
            else:
                results.append(_check_result(
                    _CheckStatus.PASS, f"Last run ({name})",
                    f"{provider_meta.get('records', 0)} items on {latest_date} "
                    f"({provider_meta.get('seconds', 0):.1f}s, ${provider_meta.get('cost_usd', 0):.4f})"
                ))

    # Sheets (optional — only checked if enabled)
    if cfg.sheets.get("enabled"):
        spreadsheet_id = cfg.sheets.get("spreadsheet_id")
        creds_path = cfg.sheets.get("credentials_path")
        if not spreadsheet_id or not creds_path:
            results.append(_check_result(_CheckStatus.FAIL, "Google Sheets (enabled)",
                                         "sheets.spreadsheet_id and/or sheets.credentials_path not set in "
                                         "config.yaml — see docs/google-setup.md"))
        elif not Path(creds_path).exists():
            results.append(_check_result(_CheckStatus.FAIL, "Google Sheets (enabled)",
                                         f"sheets.credentials_path does not exist: {creds_path}"))
        else:
            results.append(_check_result(_CheckStatus.PASS, "Google Sheets (enabled)",
                                         "spreadsheet_id set, credentials file found"))
    else:
        results.append(_check_result(_CheckStatus.WARN, "Google Sheets",
                                     "disabled (sheets.enabled: false) — optional, results stay local under "
                                     ".careeros/results/"))

    # Drive (optional — only checked if enabled)
    if cfg.drive.get("enabled"):
        client_secret_path = cfg.drive.get("client_secret_path")
        root_folder_id = cfg.drive.get("root_folder_id")
        if not client_secret_path or not Path(client_secret_path).exists():
            results.append(_check_result(_CheckStatus.FAIL, "Google Drive (enabled)",
                                         f"client_secret_path missing or not found: {client_secret_path}"))
        elif not root_folder_id:
            results.append(_check_result(_CheckStatus.FAIL, "Google Drive (enabled)",
                                         "drive.root_folder_id not set in config.yaml"))
        else:
            try:
                import google_auth_oauthlib  # noqa: F401
                results.append(_check_result(_CheckStatus.PASS, "Google Drive (enabled)",
                                             "credentials configured, [drive] extra installed"))
            except ImportError:
                results.append(_check_result(_CheckStatus.FAIL, "Google Drive (enabled)",
                                             'credentials configured but [drive] extra not installed — '
                                             'run: pip install -e ".[drive]"'))
        # PDF rendering for Resume/Cover (the only two artifacts Drive ever
        # attempts PDF for). v1.4.0's primary renderer is Typst
        # (careeros/typst_render.py) + pypdf (the ATS one-page gate in
        # `careeros artifacts --finalize`); both ship as part of the
        # [drive]/[resume] extra, so this should always be present once
        # [drive] is — checked separately (not folded into the Google Drive
        # check above) so a missing dependency is its own clear, actionable
        # line rather than being silently swallowed as a per-file warning
        # during `daily`.
        try:
            import typst  # noqa: F401
            import pypdf  # noqa: F401
            results.append(_check_result(_CheckStatus.PASS, "Resume PDF rendering (Typst)",
                                         "typst + pypdf installed"))
        except ImportError:
            results.append(_check_result(_CheckStatus.FAIL, "Resume PDF rendering (Typst)",
                                         'typst and/or pypdf not installed — resume.pdf would fail to '
                                         'render locally (finalize would fall back to a plainer legacy '
                                         'PDF, or Markdown if that fails too). Run: pip install -e ".[drive]"'))
        # fpdf2: the legacy, last-resort renderer (careeros/pdf.py), used
        # only if Typst itself is unavailable or a render genuinely fails.
        # WARN (not FAIL) — Typst is the primary path now; fpdf2 missing on
        # its own doesn't block a normal `daily` run.
        try:
            import fpdf  # noqa: F401
            results.append(_check_result(_CheckStatus.PASS, "PDF rendering fallback (fpdf2)",
                                         "fpdf2 installed"))
        except ImportError:
            results.append(_check_result(_CheckStatus.WARN, "PDF rendering fallback (fpdf2)",
                                         'fpdf2 not installed — the last-resort PDF fallback (used only '
                                         'if Typst itself is unavailable) would degrade to Markdown. '
                                         'Run: pip install -e ".[drive]"'))
    else:
        results.append(_check_result(_CheckStatus.WARN, "Google Drive", "disabled (drive.enabled: false) — optional"))

    # Playwright (optional — the `apply` stage's fallback tier for JS-gated
    # forms; the primary HTTP tier works without it). Two independent things
    # can be missing: the `careeros[apply]` extra's Python package, and the
    # `chromium` browser BINARY it still needs (`playwright install
    # chromium`) — pip alone does not install the binary. Distinguishing
    # them here is exactly what makes "⚙️ Playwright Missing" in the apply
    # stage's status column actionable rather than a dead end.
    try:
        import playwright.sync_api  # noqa: F401
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                browser.close()
            results.append(_check_result(_CheckStatus.PASS, "Playwright (apply fallback)",
                                         "[apply] extra installed, chromium browser available"))
        except Exception:
            results.append(_check_result(
                _CheckStatus.WARN, "Playwright (apply fallback)",
                "[apply] extra installed but the chromium browser binary is missing — "
                "run: playwright install chromium"
            ))
    except ImportError:
        results.append(_check_result(
            _CheckStatus.WARN, "Playwright (apply fallback)",
            'not installed — optional, only needed for JS-gated application forms; '
            'run: pip install -e ".[apply]" && playwright install chromium'
        ))

    return results


def _run_doctor_live_checks(cfg: Config) -> list[tuple[str, str, str]]:
    """LIVE checks — actually reach each enabled external source and report
    what it says right now, instead of a locally stored/calculated guess.
    This is the answer to "the API is running or not" being verified every
    day, per AGENT_GUIDE.md — `_run_doctor_checks` above stays network-free
    (so plain `doctor` never spends quota just by being run); this function
    is opt-in via `--live` and spends a small, bounded amount of real quota
    (one 1-record Fantastic Jobs fetch) specifically to verify."""
    results: list[tuple[str, str, str]] = []
    active = enabled_providers(cfg)

    if "fantastic-jobs" in active:
        try:
            provider = get_provider("fantastic-jobs")
            result = provider.fetch(cfg, limit=1, search="", query=None)
            if result.live_quota:
                rq = result.live_quota.get("requests_remaining")
                rj = result.live_quota.get("jobs_remaining")
                results.append(_check_result(
                    _CheckStatus.PASS, "Fantastic Jobs (LIVE)",
                    f"reachable — requests_remaining={rq or 'n/a'}, jobs_remaining={rj or 'n/a'}"
                ))
            else:
                results.append(_check_result(
                    _CheckStatus.PASS, "Fantastic Jobs (LIVE)",
                    "reachable — no rate-limit headers returned on this response"
                ))
        except ProviderError as e:
            results.append(_check_result(_CheckStatus.FAIL, "Fantastic Jobs (LIVE)", str(e)))

    return results


@app.command(rich_help_panel="Setup")
def doctor(
    live: bool = typer.Option(
        False, "--live",
        help="Also verify each enabled provider against its LIVE API (spends a small, "
             "bounded amount of real quota) instead of only local/stored state.",
    ),
):
    """First-run checklist: Python version, profile, discovery credentials,
    and (if enabled) Sheets/Drive. Checks only — never modifies anything.
    Exits non-zero if any check FAILs, so it's safe to gate a first `daily`
    run on `careeros doctor && careeros daily`-style scripting. Pass --live
    to also verify each provider against its real API right now, rather
    than trusting local/stored state alone (see AGENT_GUIDE.md)."""
    cfg = _config()
    results = _run_doctor_checks(cfg)

    icon = {_CheckStatus.PASS: "✓", _CheckStatus.WARN: "!", _CheckStatus.FAIL: "✗"}
    for status, label, detail in results:
        typer.echo(f"[{icon[status]}] {label:32} {detail}")

    if live:
        typer.echo("")
        typer.echo("Live checks (verifying against real APIs, not stored state):")
        live_results = _run_doctor_live_checks(cfg)
        for status, label, detail in live_results:
            typer.echo(f"[{icon[status]}] {label:32} {detail}")
        results = results + live_results

    fails = [r for r in results if r[0] == _CheckStatus.FAIL]
    typer.echo("")
    if fails:
        typer.echo(f"{len(fails)} check(s) failed — fix the items marked [✗] above before running `daily`.")
        raise typer.Exit(1)
    typer.echo("All checks passed. You're ready to run `/careeros daily`.")
