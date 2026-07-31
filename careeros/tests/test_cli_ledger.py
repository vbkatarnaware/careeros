"""CLI-level test for `careeros ledger` (careeros/cli/ledger_cmd.py)."""

from __future__ import annotations

import json
import sys
from unittest.mock import patch

import careeros.cli  # noqa: F401 -- ensures careeros.cli.ledger_cmd is imported
from careeros import runmeta
from careeros.config import Config
from careeros.models import dumps

ledger_cmd = sys.modules["careeros.cli.ledger_cmd"]  # see test_cli_calibrate.py for why


def _cfg(**overrides) -> Config:
    defaults = dict(
        threshold=4.0, consider_threshold=3.5,
        gate_batch_size=50, description_max_chars=4000,
        goals={}, prompts={}, sheets={}, api={}, fx_rates={}, drive={"enabled": False},
    )
    defaults.update(overrides)
    return Config(**defaults)


def test_ledger_excludes_quarantined_dates_and_writes_outputs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()

    for date, tier in [("2026-07-30", "onsite"), ("2026-07-31", "onsite")]:
        normalize_dir = runmeta.stage_dir(cfg.runs_dir, date, "normalize")
        with open(normalize_dir / "jobs.json", "w") as f:
            f.write(dumps([{"id": f"{date}-job", "tiers": [tier]}]))
        with open(cfg.careeros_dir / "processed.jsonl", "a") as f:
            f.write(json.dumps({"id": f"{date}-job", "date": date, "terminal_stage": "select",
                                "reason": "apply", "score": 4.2}) + "\n")
        select_dir = runmeta.stage_dir(cfg.runs_dir, date, "select")
        with open(select_dir / "outcomes.json", "w") as f:
            f.write(dumps([{"id": f"{date}-job", "tier": "apply", "score": 4.2, "recommendation": "apply",
                            "shortfall": {}, "primary_gap": None, "secondary_gap": None, "tiers": [tier]}]))

    learning_dir = cfg.careeros_dir / "learning"
    learning_dir.mkdir(parents=True)
    with open(learning_dir / "quarantine.json", "w") as f:
        json.dump({"dates": ["2026-07-30"]}, f)

    with patch("careeros.cli.ledger_cmd._config", return_value=cfg):
        ledger_cmd.ledger(since=30)

    jsonl_lines = [json.loads(l) for l in (learning_dir / "ledger.jsonl").read_text().splitlines()]
    dates_seen = {r["date"] for r in jsonl_lines}
    assert dates_seen == {"2026-07-31"}, "the quarantined 2026-07-30 run must not appear"

    md = (learning_dir / "ledger.md").read_text()
    assert "onsite" in md
