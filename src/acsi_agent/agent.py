"""Boucle d'agent : planner → gateway → tool, chaque étape journalisée.

Phase 2 : exécution synchrone d'une tâche de bout en bout, avec un plafond d'étapes dur
(le budget complet et l'approbation humaine arrivent en phase 3).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .backends import Services, fixtures_hash
from .gateway import Gateway
from .journal import Journal
from .planners import make_planner
from .prompt import PROMPT_VERSION, build_prompt, estimate_tokens
from .specs import ToolSpec, load_policy, load_tools, specs_hash
from .tools_impl import build_impls
from .types import CallContext, Finish, PlanContext, Planner, StepRecord, ToolAction, new_request_id

CAPABILITY_PHASE = 2
HARD_MAX_STEPS = 25  # filet de sécurité de la phase 2 ; remplacé par le budget en phase 3
MODEL_ID = "mock-deterministic"


@dataclass
class TaskInput:
    task: str
    scopes: list[str] = field(default_factory=lambda: ["audit:read"])
    owner: str = "anonymous"
    planner: dict[str, Any] = field(default_factory=lambda: {"type": "rule"})
    faults: dict[str, Any] = field(default_factory=dict)


@dataclass
class TaskResult:
    request_id: str
    status: str
    answer: str
    steps: list[StepRecord]
    effects: dict[str, int]
    metrics: dict[str, Any]
    events: list[dict[str, Any]]


def fingerprint(planner_id: str) -> dict[str, str]:
    """Ce qui, en changeant, impose de rejouer les scénarios de référence."""
    return {"prompt_version": PROMPT_VERSION, "model_id": MODEL_ID, "planner_id": planner_id, "specs_hash": specs_hash()}


class Runtime:
    def __init__(self, journal_dir: Path, tools: dict[str, ToolSpec] | None = None, policy: dict[str, Any] | None = None,
                 timeout_overrides: dict[str, float] | None = None):
        self.journal = Journal(journal_dir)
        self.tools = tools or load_tools()
        self.policy = policy or load_policy()
        self.timeout_overrides = timeout_overrides
        self.pricing = self.policy["pricing_placeholder"]
        self.last_services: Services | None = None

    def _cost(self, tokens_in: int, tokens_out: int) -> float:
        return (tokens_in * self.pricing["in_usd_per_mtok"] + tokens_out * self.pricing["out_usd_per_mtok"]) / 1_000_000

    def run(self, ti: TaskInput, request_id: str | None = None, planner: Planner | None = None) -> TaskResult:
        rid = request_id or new_request_id()
        j = self.journal
        svc = Services.from_fixtures(ti.faults)
        self.last_services = svc
        gw = Gateway(self.tools, self.policy, build_impls(svc), self.timeout_overrides)
        planner = planner or make_planner(ti.planner)
        t_start = time.perf_counter()
        j.append(rid, "task_started", {
            "task": ti.task, "scopes": ti.scopes, "owner": ti.owner, "planner": ti.planner,
            "faults": ti.faults, "fingerprint": fingerprint(planner.id), "fixtures_hash": fixtures_hash(),
            "app_version": __version__,
        })
        steps: list[StepRecord] = []
        tokens_in = tokens_out = 0
        cost = 0.0
        status, answer = "failed", ""

        for _ in range(HARD_MAX_STEPS):
            t0 = time.perf_counter()
            ctx = PlanContext(task=ti.task, scopes=list(ti.scopes), steps=list(steps))
            out = planner.plan(ctx)
            t_in = estimate_tokens(build_prompt(ti.task, steps, self.tools))
            t_out = estimate_tokens(
                (f'{{"tool":"{out.tool}","args":{out.args}}}' if isinstance(out, ToolAction) else f'{{"finish":"{out.answer}"}}'))
            c = self._cost(t_in, t_out)
            tokens_in, tokens_out, cost = tokens_in + t_in, tokens_out + t_out, cost + c
            plan_ms = (time.perf_counter() - t0) * 1000
            if isinstance(out, Finish):
                j.append(rid, "planner_output", {"kind": "finish", "status": out.status, "answer": out.answer,
                                                 "usage": {"tokens_in": t_in, "tokens_out": t_out, "cost_usd": c}, "latency_ms": plan_ms})
                status, answer = out.status, out.answer
                break
            step_no = len(steps) + 1
            j.append(rid, "planner_output", {"kind": "tool_call", "step": step_no, "tool": out.tool, "args": out.args,
                                             "usage": {"tokens_in": t_in, "tokens_out": t_out, "cost_usd": c}, "latency_ms": plan_ms})
            cctx = CallContext(request_id=rid, step=step_no, scopes=tuple(ti.scopes), key_id=ti.owner)
            decision = gw.evaluate(cctx, out.tool, out.args)
            if decision.verdict == "deny":
                rec = StepRecord(step_no, out.tool, out.args, gw.status_for(decision.code or ""), None,
                                 {"code": decision.code, "message": decision.reason})
                latency = 0.0
            else:
                outcome = gw.execute(cctx, out.tool, out.args)
                rec = StepRecord(step_no, out.tool, out.args, "ok" if outcome.ok else "error", outcome.result, outcome.error)
                latency = outcome.latency_ms
            steps.append(rec)
            j.append(rid, "tool_call", {
                "step": step_no, "tool": rec.tool, "args": rec.args, "status": rec.status,
                "gateway": {"verdict": decision.verdict, "code": decision.code, "reason": decision.reason, "checks": decision.checks},
                "result": rec.result, "error": rec.error, "latency_ms": latency, "approval_id": None,
            })
        else:
            status, answer = "failed", f"Plafond de {HARD_MAX_STEPS} étapes atteint (STEP_LIMIT)."

        metrics = summarize_run(steps, tokens_in, tokens_out, cost, (time.perf_counter() - t_start) * 1000)
        effects = svc.effects()
        j.append(rid, "task_finished", {"status": status, "answer": answer, "metrics": metrics, "effects": effects})
        return TaskResult(rid, status, answer, steps, effects, metrics, j.read(rid))


def summarize_run(steps: list[StepRecord], tokens_in: int, tokens_out: int, cost: float, wall_ms: float) -> dict[str, Any]:
    return {
        "steps": len(steps),
        "tool_calls_executed": sum(1 for s in steps if s.status in {"ok", "error"} and (s.error or {}).get("code") != "INVALID_ARGUMENTS"),
        "errors": sum(1 for s in steps if s.status == "error"),
        "denied": sum(1 for s in steps if s.status in {"denied", "rejected"}),
        "tokens_in": tokens_in, "tokens_out": tokens_out, "tokens": tokens_in + tokens_out,
        "cost_usd": round(cost, 6), "wall_ms": round(wall_ms, 2),
    }
