"""Chargement des specs (source de vérité : specs/*.json) et des scénarios."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(os.environ.get("ACSI_ROOT", Path(__file__).resolve().parents[2]))
SPECS_DIR = ROOT / "specs"
SCENARIOS_PATH = ROOT / "scenarios" / "reference.json"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    purpose: str
    side_effect: str
    permission: str
    approval: str
    timeout_s: float
    max_calls_per_task: int
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    errors: tuple[dict[str, Any], ...]
    notes: str = ""

    @property
    def requires_approval(self) -> bool:
        return self.approval == "always"


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_tools(path: Path | None = None) -> dict[str, ToolSpec]:
    raw = _load(path or SPECS_DIR / "tools.json")
    tools = {}
    for t in raw["tools"]:
        tools[t["name"]] = ToolSpec(
            name=t["name"], purpose=t["purpose"], side_effect=t["side_effect"],
            permission=t["permission"], approval=t["approval"],
            timeout_s=float(t["timeout_s"]), max_calls_per_task=int(t["max_calls_per_task"]),
            input_schema=t["input_schema"], output_schema=t["output_schema"],
            errors=tuple(t.get("errors", [])), notes=t.get("notes", ""),
        )
    return tools


def load_policy(path: Path | None = None) -> dict[str, Any]:
    return _load(path or SPECS_DIR / "policy.json")


def load_scenarios(path: Path | None = None) -> list[dict[str, Any]]:
    return _load(path or SCENARIOS_PATH)["scenarios"]


def specs_hash() -> str:
    """Empreinte des specs (tools + politique) : change dès qu'un tool ou une règle change."""
    h = hashlib.sha256()
    for name in ("tools.json", "policy.json"):
        h.update((SPECS_DIR / name).read_bytes())
    return h.hexdigest()[:16]
