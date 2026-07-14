"""Small helpers shared across careeros/cli/*.py command modules.

Deliberately dependency-free of `careeros.cli`'s own `app`/`sheets_app` (see
careeros/cli/__init__.py) so every command module can import from here with
no circular-import ordering concerns.
"""

from __future__ import annotations

import datetime
from pathlib import Path

from careeros.config import Config, load_config, provider_config_block
from careeros.models import Profile

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _today() -> str:
    """Run date. Callers may override via --date for reproducible/resumed
    runs; this is the only place "today" is computed so tests can pass a
    fixed date instead."""
    return datetime.date.today().isoformat()


def _config() -> Config:
    return load_config()


def _load_profile(cfg: Config) -> Profile:
    import yaml
    with open(cfg.profile_path) as f:
        return Profile.from_dict(yaml.safe_load(f))


def _provider_query_cfg(cfg: Config, provider_name: str) -> dict:
    """Thin alias for `config.provider_config_block` (v1.2) — kept under this
    name since it's used throughout the cli package. See that function's
    docstring for the exact per-provider resolution and why `cfg.apify` is
    deliberately NOT merged in generically (it would leak Apify-budget guard
    capability into the free providers)."""
    return provider_config_block(cfg, provider_name)
