"""CareerOS: an AI-powered, deterministic job discovery and recommendation engine.

Deterministic code wherever possible. AI only where reasoning adds value.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    # pyproject.toml is the single source of truth for the version — this
    # reads back whatever setuptools recorded at install time (works for
    # both a normal install and `pip install -e .`), so there's no second
    # place to remember to bump.
    __version__ = version("careeros")
except PackageNotFoundError:
    # Running from a checkout that was never `pip install`-ed (e.g. via
    # PYTHONPATH directly) — no installed distribution record to read.
    __version__ = "0.0.0+unknown"
