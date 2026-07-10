"""Tests for the top-level `--version` flag (v1.1) and the dynamic
`careeros.__version__` it reports. Version now has a SINGLE source of truth
(`pyproject.toml`'s `[project].version`) — `careeros/__init__.py` reads it
back via `importlib.metadata.version("careeros")` rather than duplicating
the string, so the two can never drift out of sync. `--version` is an eager
Typer callback, which is why this uses CliRunner (a direct function call
can't observe the early `typer.Exit`/exit-code path an eager option takes)."""

from __future__ import annotations

from typer.testing import CliRunner

from careeros import __version__
from careeros.cli import app

runner = CliRunner()


def test_version_flag_prints_version_and_exits_zero():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_version_flag_short_circuits_before_any_subcommand_runs():
    """--version must exit before dispatching to a subcommand, even when one
    is also given -- eager options win."""
    result = runner.invoke(app, ["--version", "providers"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_dunder_version_matches_installed_package_metadata():
    from importlib.metadata import version as pkg_version
    assert __version__ == pkg_version("careeros")


def test_no_args_still_shows_help_not_version():
    """Adding the --version eager callback must not change the pre-existing
    no_args_is_help behavior."""
    result = runner.invoke(app, [])
    assert "Usage:" in result.stdout
