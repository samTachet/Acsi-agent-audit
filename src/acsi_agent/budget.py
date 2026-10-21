"""Budget par tâche : plafonds, vérifications et action en cas de dépassement.

Action de dépassement = `abort` (policy.budget_on_exceed) : l'action proposée n'est PAS exécutée,
la tâche s'arrête avec le statut `budget_exceeded`, les effets déjà produits sont listés mais pas annulés.

Les plafonds de tokens et de coût ne sont connus qu'après l'appel du planner : le dépassement
maximal est donc borné par UN appel de planner. Le « temps » est le temps actif cumulé (planner + tools,
attente d'approbation humaine exclue), reconstruit depuis le journal.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class Budget:
    max_steps: int
    max_tool_calls: int
    max_tokens: int
    max_cost_usd: float
    max_wall_s: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def effective(cls, defaults: dict[str, Any], overrides: dict[str, Any] | None) -> tuple["Budget", list[str]]:
        """Un appelant peut RÉDUIRE un plafond, jamais l'augmenter ; les tentatives sont listées."""
        vals, clamped = dict(defaults), []
        for k, v in (overrides or {}).items():
            if k not in defaults:
                raise ValueError(f"plafond inconnu : {k}")
            if not isinstance(v, (int, float)) or isinstance(v, bool) or v <= 0:
                raise ValueError(f"plafond invalide : {k}")
            if v > defaults[k]:
                clamped.append(k)
            else:
                vals[k] = v
        return cls(**vals), clamped


@dataclass(frozen=True)
class Meter:
    planner_calls: int
    tool_calls: int
    tokens: int
    cost_usd: float
    active_s: float


@dataclass(frozen=True)
class Violation:
    dimension: str  # steps | tool_calls | tokens | cost | wall
    limit: float
    observed: float


def _wall(b: Budget, m: Meter) -> Violation | None:
    return Violation("wall", b.max_wall_s, round(m.active_s, 3)) if m.active_s > b.max_wall_s else None


def check_pre_planner(b: Budget, m: Meter) -> Violation | None:
    if m.planner_calls >= b.max_steps:
        return Violation("steps", b.max_steps, m.planner_calls + 1)
    return _wall(b, m)


def check_post_planner(b: Budget, m: Meter) -> Violation | None:
    if m.tokens > b.max_tokens:
        return Violation("tokens", b.max_tokens, m.tokens)
    if m.cost_usd > b.max_cost_usd:
        return Violation("cost", b.max_cost_usd, round(m.cost_usd, 6))
    return _wall(b, m)


def check_pre_tool(b: Budget, m: Meter) -> Violation | None:
    if m.tool_calls >= b.max_tool_calls:
        return Violation("tool_calls", b.max_tool_calls, m.tool_calls + 1)
    return _wall(b, m)


def ratios(b: Budget, m: Meter) -> dict[str, float]:
    return {
        "steps": m.planner_calls / b.max_steps,
        "tool_calls": m.tool_calls / b.max_tool_calls,
        "tokens": m.tokens / b.max_tokens,
        "cost": m.cost_usd / b.max_cost_usd,
        "wall": m.active_s / b.max_wall_s,
    }
