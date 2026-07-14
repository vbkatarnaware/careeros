"""careeros CLI.

Thin dispatch only — every command here calls into careeros/{config,models,
cache,runmeta,lint,report,sheets}.py or careeros/{providers,pipeline}/. No
business logic lives in this package.

Split by concern into careeros/cli/*.py, each module registering its
commands onto the shared `app` (and `sheets_app` for the `sheets` sub-typer)
defined below via the `@app.command()` decorator at import time — see the
module imports at the bottom of this file. Every submodule can safely
`from careeros.cli import app` (and `sheets_app`) despite the apparent
circularity: by the time any submodule is imported, `app`/`sheets_app` are
already fully defined here.

Two tiers of commands:
  - End-user:  init, start, daily, prep, apply, publish, config, providers
  - Developer: discover, normalize, dedupe, constraints, gate, evaluate,
               threshold, artifacts, apply --prepare/--finalize, sheets,
               lint, verify-resume — each stage runnable standalone against
               a run directory, for debugging without re-running the whole
               pipeline.

AI stages (gate, evaluate, artifacts, apply --prepare/--finalize) follow the
host-CLI execution boundary: a `--prepare` half (Python writes the stage's
input + an instruction for the agent) and a `--finalize` half (Python
validates whatever the agent wrote). See skills/daily.md for the full
instruction sequence. `apply` additionally has an on-demand, single-job form
(`careeros apply <job-id>`, no --prepare/--finalize) for any job at any
score, run manually via its own host-CLI skill.

`constraints` is deterministic: it hard-rejects jobs violating an objective
profile deal-breaker (location, salary floor) BEFORE any AI is spent, and
`threshold` re-checks the same constraints as a backstop so a hard-rejected
job can never slip through as "apply" even if the AI mislabels it.
"""

from __future__ import annotations

from typing import Optional

import typer

# Re-exported here (not just imported by individual submodules) so that
# test-suite mocks written against the pre-split single-file `careeros.cli`
# (e.g. `patch("careeros.cli.sheets_mod.append_rows", ...)`) keep working
# unchanged: these are MODULE objects, and Python caches modules in
# sys.modules, so `careeros.cli.sheets_mod` and (say)
# `careeros.cli.sheets_cmds.sheets_mod` are the exact same object — patching
# an attribute through either path affects both.
from careeros import runmeta  # noqa: F401
from careeros import sheets as sheets_mod  # noqa: F401
from careeros.apply import browser as apply_browser  # noqa: F401

app = typer.Typer(
    add_completion=False, no_args_is_help=True,
    help="CareerOS — an AI-powered, deterministic job discovery and recommendation engine.\n\n"
         "CareerOS's AI steps run inside your coding CLI — use `/careeros <cmd>` there; "
         "the bare `careeros <cmd>` you see here is the deterministic half.",
)

sheets_app = typer.Typer(help="Google Sheets operations")
app.add_typer(sheets_app, name="sheets", hidden=True)


def _version_callback(show_version: bool) -> None:
    if show_version:
        from careeros import __version__
        typer.echo(f"careeros {__version__}")
        raise typer.Exit()


@app.callback()
def _main(
    version: Optional[bool] = typer.Option(
        None, "--version", callback=_version_callback, is_eager=True,
        help="Show the installed careeros version and exit.",
    ),
) -> None:
    pass


# Import every command submodule for its registration side-effect (each one
# calls @app.command()/@sheets_app.command() at import time). Order matters
# only where one submodule imports a name from another (e.g. sheets_cmds
# imports from apply_stage) — Python resolves that fine since imports here
# run top-to-bottom and each submodule's own imports are resolved on demand.
from careeros.cli import (  # noqa: E402,F401
    apply_stage,
    artifacts,
    discover,
    doctor,
    drive,
    gate_evaluate,
    lint_verify,
    perjob,
    pipeline,
    reports,
    setup,
    sheets_cmds,
    stubs,
)

# Backward-compatible re-exports: tests (and any external tooling) that did
# `from careeros.cli import _artifacts_finalize` etc. before this package
# split continue to work unchanged — every name below is the SAME object as
# in its home submodule, not a copy.
from careeros.cli.apply_stage import (  # noqa: E402,F401
    STATUS_BOT_CHECK,
    STATUS_CLOSED,
    STATUS_GENERATED,
    STATUS_LOGIN_REQUIRED,
    STATUS_MANUAL_REQUIRED,
    STATUS_NETWORK_ERROR,
    STATUS_NO_ESSAY_QUESTIONS,
    STATUS_PLAYWRIGHT_MISSING,
    _apply_finalize,
    _apply_prepare,
    _load_apply_status,
    _STATUS_LABELS,
    apply,
)
from careeros.cli.artifacts import _artifacts_finalize, _artifacts_prepare  # noqa: E402,F401
from careeros.cli.discover import discover  # noqa: E402,F401
from careeros.cli.doctor import _CheckStatus, _run_doctor_checks, _run_doctor_live_checks, doctor  # noqa: E402,F401
from careeros.cli.drive import backfill_drive  # noqa: E402,F401
from careeros.cli.gate_evaluate import _evaluate_finalize, _evaluate_prepare  # noqa: E402,F401
from careeros.cli.perjob import job, publish  # noqa: E402,F401
from careeros.cli.pipeline import constraints, dedupe, normalize, threshold  # noqa: E402,F401
from careeros.cli.reports import _build_discovery_stats, summary  # noqa: E402,F401
from careeros.cli.setup import config, init, migrate_config, providers  # noqa: E402,F401
from careeros.cli.sheets_cmds import sheets_append, sheets_migrate, sheets_sync_status  # noqa: E402,F401
from careeros.cli.stubs import daily, prep, scan, start  # noqa: E402,F401
