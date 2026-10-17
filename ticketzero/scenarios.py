"""Demo scenarios: a working folder plus a preset ticket, loaded from ticketzero/scenario_data/<name>/.

Each scenario directory holds:
  scenario.json  label and hint (example button), title, description, expected_outcome, order, and the ticket
                 (title, description, expected_behavior)
  logs.txt       the ticket's (already redacted) error logs
  repo/          the working folder a demo run uses, exactly like a folder a user picks (read-only for demos)
"""
import json
import shutil
from pathlib import Path

from . import config

DATA_DIR = Path(__file__).parent / "scenario_data"


def _load_one(d: Path) -> dict:
    meta = json.loads((d / "scenario.json").read_text(encoding="utf-8"))
    ticket = dict(meta["ticket"], logs=(d / "logs.txt").read_text(encoding="utf-8"))
    return {"title": meta["title"], "description": meta["description"], "expected_outcome": meta["expected_outcome"],
            "label": meta.get("label", meta["title"]), "hint": meta.get("hint", ""),
            "order": meta.get("order", 99), "ticket": ticket, "folder": (d / "repo").resolve()}


def load_scenarios() -> dict[str, dict]:
    found = {d.name: _load_one(d) for d in DATA_DIR.iterdir() if (d / "scenario.json").is_file()}
    return dict(sorted(found.items(), key=lambda kv: kv[1]["order"]))


SCENARIOS = load_scenarios()


def write_scenario_templates() -> None:
    """Copy the templates to TICKETZERO_HOME/scenarios for inspection."""
    target = config.HOME / "scenarios"
    shutil.copytree(DATA_DIR, target, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", "*.wav"))
