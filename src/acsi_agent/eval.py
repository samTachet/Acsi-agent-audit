"""Évaluation des scénarios de référence : tool correctness et succès de tâche.

Tool correctness = bon tool, bons paramètres, bon résultat (et bonne issue : ok / refus / erreur).
Tâche réussie = tool correct ET statut final attendu ET effets observables attendus.

Usage : python -m acsi_agent.eval [--scenario ID] [--json FICHIER] [--journal-dir DIR]
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import agent as agent_mod
from .agent import Runtime, TaskInput, fingerprint
from .specs import load_scenarios
from .types import StepRecord


def subset(expected: Any, actual: Any) -> bool:
    if isinstance(expected, dict):
        if set(expected) == {"$contains"}:
            return isinstance(actual, str) and expected["$contains"] in actual
        return isinstance(actual, dict) and all(k in actual and subset(v, actual[k]) for k, v in expected.items())
    return expected == actual


def expand(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for c in calls:
        out += [{k: v for k, v in c.items() if k != "repeat"}] * int(c.get("repeat", 1))
    return out


def align(expected: list[dict[str, Any]], steps: list[StepRecord]):
    """Appariement ordonné glouton : chaque attendu prend le premier appel suivant du même tool."""
    pairs, used, pos = [], set(), 0
    for e in expected:
        j = next((i for i in range(pos, len(steps)) if steps[i].tool == e["tool"]), None)
        if j is None:
            pairs.append((e, None))
            continue
        pairs.append((e, steps[j]))
        used.add(j)
        pos = j + 1
    extra = [s for i, s in enumerate(steps) if i not in used]
    return pairs, extra


@dataclass
class CallScore:
    tool: str
    matched: bool
    args_ok: bool = False
    outcome_ok: bool = False
    result_ok: bool = False
    approval_ok: bool = False


@dataclass
class ScenarioReport:
    id: str
    category: str
    skipped: str | None = None
    request_id: str | None = None
    expected_status: str = ""
    actual_status: str = ""
    calls: list[CallScore] = field(default_factory=list)
    extra_calls: list[str] = field(default_factory=list)
    selection_ok: bool = False
    args_ok: bool = False
    outcome_ok: bool = False
    result_ok: bool = False
    tool_correct: bool = False
    status_ok: bool = False
    effects_ok: bool = False
    budget_ok: bool = True
    success: bool = False
    issues: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    tool_sequence: list[str] = field(default_factory=list)


def score(sc: dict[str, Any], result) -> ScenarioReport:
    exp = sc["expected"]
    rep = ScenarioReport(sc["id"], sc["category"], request_id=result.request_id,
                         expected_status=exp["status"], actual_status=result.status,
                         metrics=result.metrics, tool_sequence=[s.tool for s in result.steps])
    pairs, extra = align(expand(exp["calls"]), result.steps)
    for e, s in pairs:
        if s is None:
            rep.calls.append(CallScore(e["tool"], False))
            rep.issues.append(f"appel attendu manquant : {e['tool']}")
            continue
        cs = CallScore(e["tool"], True)
        cs.args_ok = subset(e.get("args", {}), s.args)
        cs.outcome_ok = s.status == e["outcome"] and (e.get("code") is None or (s.error or {}).get("code") == e["code"])
        cs.result_ok = subset(e.get("result", {}), s.result or {}) if s.status == "ok" else ("result" not in e)
        cs.approval_ok = bool(s.approval_id) == bool(e.get("approval", False))
        if not cs.args_ok:
            rep.issues.append(f"{e['tool']} : arguments {s.args} ≠ attendu {e.get('args')}")
        if not cs.outcome_ok:
            rep.issues.append(f"{e['tool']} : issue {s.status}/{(s.error or {}).get('code')} ≠ attendu {e['outcome']}/{e.get('code')}")
        if not cs.result_ok:
            rep.issues.append(f"{e['tool']} : résultat inattendu")
        if not cs.approval_ok:
            rep.issues.append(f"{e['tool']} : approbation {'présente' if s.approval_id else 'absente'} contre attendu {e.get('approval', False)}")
        rep.calls.append(cs)
    rep.extra_calls = [s.tool for s in extra]
    allow_extra = bool(exp.get("allow_extra_calls"))
    if rep.extra_calls and not allow_extra:
        rep.issues.append(f"appels non attendus : {rep.extra_calls}")
    matched = [c for c in rep.calls if c.matched]
    rep.selection_ok = all(c.matched for c in rep.calls) and (allow_extra or not rep.extra_calls)
    rep.args_ok = all(c.args_ok for c in matched)
    rep.outcome_ok = all(c.outcome_ok and c.approval_ok for c in matched)
    rep.result_ok = all(c.result_ok for c in matched)
    rep.tool_correct = rep.selection_ok and rep.args_ok and rep.outcome_ok and rep.result_ok
    rep.status_ok = result.status == exp["status"]
    if not rep.status_ok:
        rep.issues.append(f"statut {result.status} ≠ attendu {exp['status']}")
    rep.effects_ok = result.effects == exp["effects"]
    if not rep.effects_ok:
        rep.issues.append(f"effets {result.effects} ≠ attendus {exp['effects']}")
    if exp.get("budget_dimension"):
        got = result.metrics.get("budget_dimension")
        rep.budget_ok = got == exp["budget_dimension"]
        if not rep.budget_ok:
            rep.issues.append(f"dimension de budget {got} ≠ attendue {exp['budget_dimension']}")
    rep.success = rep.tool_correct and rep.status_ok and rep.effects_ok and rep.budget_ok
    return rep


def to_task_input(sc: dict[str, Any]) -> TaskInput:
    inp = sc["input"]
    return TaskInput(task=inp["task"], scopes=list(inp["scopes"]), owner="eval-requester",
                     planner=inp["planner"], faults=inp.get("faults", {}))


def run_scenario(sc: dict[str, Any], runtime: Runtime) -> ScenarioReport:
    if sc.get("min_phase", 2) > agent_mod.CAPABILITY_PHASE:
        return ScenarioReport(sc["id"], sc["category"], skipped=f"nécessite la phase {sc['min_phase']}")
    result = runtime.run(to_task_input(sc))
    return score(sc, result)


def aggregate(reports: list[ScenarioReport]) -> dict[str, Any]:
    ran = [r for r in reports if not r.skipped]
    n = len(ran) or 1
    pairs = [c for r in ran for c in r.calls]
    matched = [c for c in pairs if c.matched]
    return {
        "scenarios_run": len(ran), "scenarios_skipped": len(reports) - len(ran),
        "task_success_rate": sum(r.success for r in ran) / n,
        "tool_correct_rate": sum(r.tool_correct for r in ran) / n,
        "tool_selection_accuracy": len(matched) / (len(pairs) or 1),
        "argument_accuracy": sum(c.args_ok for c in matched) / (len(matched) or 1),
        "outcome_accuracy": sum(c.outcome_ok and c.approval_ok for c in matched) / (len(matched) or 1),
        "result_accuracy": sum(c.result_ok for c in matched) / (len(matched) or 1),
        "mean_steps": sum(r.metrics.get("steps", 0) for r in ran) / n,
        "mean_cost_usd": sum(r.metrics.get("cost_usd", 0) for r in ran) / n,
        "max_cost_usd": max((r.metrics.get("cost_usd", 0) for r in ran), default=0),
    }


def run_all(runtime: Runtime, only: str | None = None) -> list[ScenarioReport]:
    scenarios = [s for s in load_scenarios() if only in (None, s["id"])]
    if only and not scenarios:
        raise SystemExit(f"scénario inconnu : {only}")
    return [run_scenario(s, runtime) for s in scenarios]


def render(reports: list[ScenarioReport], agg: dict[str, Any]) -> str:
    lines = [f"{'scénario':32} {'statut':16} {'outils':7} {'étapes':>6} {'tokens':>7} {'coût $':>9}  résultat"]
    for r in reports:
        if r.skipped:
            lines.append(f"{r.id:32} {'—':16} {'—':7} {'—':>6} {'—':>7} {'—':>9}  ignoré ({r.skipped})")
            continue
        m = r.metrics
        lines.append(f"{r.id:32} {r.actual_status:16} {'ok' if r.tool_correct else 'KO':7} {m.get('steps', 0):>6} "
                     f"{m.get('tokens', 0):>7} {m.get('cost_usd', 0):>9.5f}  {'✓' if r.success else '✗'}")
        for i in r.issues:
            lines.append(f"    ! {i}")
    lines.append("")
    lines.append(f"succès de tâche {agg['task_success_rate']:.0%} | tool correct {agg['tool_correct_rate']:.0%} | "
                 f"sélection {agg['tool_selection_accuracy']:.0%} | arguments {agg['argument_accuracy']:.0%} | "
                 f"issue {agg['outcome_accuracy']:.0%} | résultat {agg['result_accuracy']:.0%}")
    lines.append(f"étapes moy. {agg['mean_steps']:.1f} | coût moy. {agg['mean_cost_usd']:.5f} $ | max {agg['max_cost_usd']:.5f} $ "
                 f"| {agg['scenarios_run']} exécutés, {agg['scenarios_skipped']} ignorés")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="acsi-eval")
    ap.add_argument("--scenario")
    ap.add_argument("--json", dest="json_out")
    ap.add_argument("--journal-dir")
    a = ap.parse_args(argv)
    jdir = Path(a.journal_dir) if a.journal_dir else Path(tempfile.mkdtemp(prefix="acsi-eval-"))
    runtime = Runtime(jdir)
    reports = run_all(runtime, a.scenario)
    agg = aggregate(reports)
    print(render(reports, agg))
    if a.json_out:
        Path(a.json_out).write_text(json.dumps({
            "fingerprint": fingerprint("rule-planner-v1"), "aggregate": agg, "scenarios": [asdict(r) for r in reports],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if all(r.success for r in reports if not r.skipped) else 1


if __name__ == "__main__":
    sys.exit(main())
