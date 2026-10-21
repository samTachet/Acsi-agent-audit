"""Rejeu des scénarios de référence contre une baseline versionnée.

À lancer après TOUTE modification de prompt, de modèle ou de tool (la CI et le hook pre-push le font).
L'empreinte (prompt, modèle, planner, specs, code des tools) est comparée à celle de la baseline :
si elle diffère, la commande échoue (code 2) tant que la baseline n'a pas été régénérée explicitement
(`--update-baseline`) puis relue dans le diff git. Une baseline ne se met donc jamais à jour « sans le vouloir ».

Usage : python -m acsi_agent.regress [--update-baseline] [--baseline FICHIER]
Codes : 0 = conforme ; 1 = régression de comportement ; 2 = empreinte changée sans baseline mise à jour.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from .agent import Runtime, fingerprint
from .eval import ScenarioReport, aggregate, render, run_all
from .specs import ROOT

BASELINE_PATH = ROOT / "baselines" / "reference.json"
DEFAULT_TOLERANCES = {"steps": 0, "tokens_ratio": 0.10, "cost_ratio": 0.10}


def snapshot(reports: list[ScenarioReport]) -> dict[str, Any]:
    return {
        r.id: {
            "status": r.actual_status, "success": r.success, "tool_sequence": r.tool_sequence,
            "steps": r.metrics.get("steps", 0), "tokens": r.metrics.get("tokens", 0), "cost_usd": r.metrics.get("cost_usd", 0.0),
        }
        for r in reports if not r.skipped
    }


def build_baseline(reports: list[ScenarioReport], planner_id: str = "rule-planner-v1") -> dict[str, Any]:
    agg = aggregate(reports)
    return {"fingerprint": fingerprint(planner_id), "tolerances": DEFAULT_TOLERANCES,
            "aggregate": {k: agg[k] for k in ("task_success_rate", "tool_correct_rate", "mean_steps", "mean_cost_usd")},
            "scenarios": snapshot(reports)}


def compare(baseline: dict[str, Any], reports: list[ScenarioReport], planner_id: str = "rule-planner-v1") -> tuple[list[str], list[str]]:
    """Retourne (échecs, informations). Les échecs de comportement priment sur l'empreinte."""
    fails: list[str] = []
    info: list[str] = []
    tol = {**DEFAULT_TOLERANCES, **baseline.get("tolerances", {})}
    now = snapshot(reports)
    base = baseline["scenarios"]
    for sid in sorted(set(base) - set(now)):
        fails.append(f"{sid}: scénario de la baseline absent de l'exécution")
    for sid in sorted(set(now) - set(base)):
        fails.append(f"{sid}: nouveau scénario absent de la baseline (régénérer et relire)")
    for sid in sorted(set(base) & set(now)):
        b, c = base[sid], now[sid]
        if b["success"] and not c["success"]:
            fails.append(f"{sid}: RÉGRESSION — le scénario réussissait, il échoue")
        if b["status"] != c["status"]:
            fails.append(f"{sid}: statut {b['status']} → {c['status']}")
        if b["tool_sequence"] != c["tool_sequence"]:
            fails.append(f"{sid}: trajectoire {b['tool_sequence']} → {c['tool_sequence']}")
        if c["steps"] > b["steps"] + tol["steps"]:
            fails.append(f"{sid}: étapes {b['steps']} → {c['steps']} (tolérance +{tol['steps']})")
        if b["tokens"] and c["tokens"] > b["tokens"] * (1 + tol["tokens_ratio"]):
            fails.append(f"{sid}: tokens {b['tokens']} → {c['tokens']} (> +{tol['tokens_ratio']:.0%})")
        if b["cost_usd"] and c["cost_usd"] > b["cost_usd"] * (1 + tol["cost_ratio"]):
            fails.append(f"{sid}: coût {b['cost_usd']} → {c['cost_usd']} (> +{tol['cost_ratio']:.0%})")
        if not b["success"] and c["success"]:
            info.append(f"{sid}: le scénario échouait dans la baseline et réussit maintenant")
    fp_old, fp_new = baseline["fingerprint"], fingerprint(planner_id)
    changed = {k: (fp_old.get(k), fp_new[k]) for k in fp_new if fp_old.get(k) != fp_new[k]}
    if changed:
        info.append("empreinte modifiée : " + ", ".join(f"{k} {a} → {b}" for k, (a, b) in changed.items()))
    return fails, info


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="acsi-regress")
    ap.add_argument("--update-baseline", action="store_true")
    ap.add_argument("--baseline", default=str(BASELINE_PATH))
    a = ap.parse_args(argv)
    path = Path(a.baseline)
    reports = run_all(Runtime(Path(tempfile.mkdtemp(prefix="acsi-regress-"))))
    print(render(reports, aggregate(reports)))
    failed = [r.id for r in reports if not r.success and not r.skipped]
    if a.update_baseline:
        if failed:
            print(f"\n✗ baseline NON mise à jour : scénarios en échec {failed}")
            return 1
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(build_baseline(reports), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"\n✓ baseline écrite : {path} (relire le diff git avant de commiter)")
        return 0
    if not path.exists():
        print(f"\n✗ baseline absente : {path} (créer avec --update-baseline)")
        return 1
    fails, info = compare(json.loads(path.read_text(encoding="utf-8")), reports)
    print()
    for i in info:
        print("ℹ", i)
    for f in fails:
        print("✗", f)
    if fails:
        print(f"\nÉCHEC : {len(fails)} régression(s)")
        return 1
    if any(i.startswith("empreinte modifiée") for i in info):
        print("\nComportement identique mais empreinte modifiée : régénérer la baseline (--update-baseline) et la relire.")
        return 2
    print("\nOK : comportement conforme à la baseline")
    return 0


if __name__ == "__main__":
    sys.exit(main())
