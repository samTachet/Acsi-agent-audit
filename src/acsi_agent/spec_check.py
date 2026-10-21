"""Vérificateur de cohérence entre tools, politique et scénarios.

Usage : python -m acsi_agent.spec_check   (code retour 1 si une incohérence est trouvée)
"""
from __future__ import annotations

import re
import sys
from typing import Any

from jsonschema import Draft202012Validator

from .specs import load_policy, load_scenarios, load_tools

SIDE_EFFECTS = {"read", "write_reversible", "irreversible_local", "irreversible_external"}
OUTCOMES = {"ok", "error", "denied", "rejected"}
STATUSES = {"completed", "refused", "failed", "budget_exceeded", "expired"}
CATEGORIES = {"nominal", "approval", "refusal", "error_recovery", "budget", "injection"}
EFFECT_KEYS = {"mail_sent", "drafts_created", "drafts_deleted"}
PLANNERS = {"rule", "scripted", "runaway"}
BUDGET_DIMS = {"steps", "tool_calls", "tokens", "cost", "wall"}


def check() -> list[str]:
    errs: list[str] = []
    tools = load_tools()
    policy = load_policy()
    scenarios = load_scenarios()

    all_scopes = set(policy["scopes"])
    role_scopes = {s for r in policy["roles"].values() for s in r}
    forbidden = {f["name"] for f in policy["forbidden_actions"]}
    approval_tools = {a["tool"] for a in policy["approval_required"]}
    common = {e["code"]: e["kind"] for e in policy["common_errors"]}
    tool_codes = {t.name: {e["code"] for e in t.errors} for t in tools.values()}

    if role_scopes - all_scopes:
        errs.append(f"rôles : portées inconnues {sorted(role_scopes - all_scopes)}")
    if set(policy["default_task_scopes"]) - all_scopes:
        errs.append("default_task_scopes contient une portée inconnue")
    if policy["default"] != "deny":
        errs.append("la politique par défaut doit être 'deny'")
    if forbidden & set(tools):
        errs.append(f"tools à la fois autorisés et interdits : {sorted(forbidden & set(tools))}")

    for t in tools.values():
        if not re.fullmatch(r"[a-z][a-z0-9_]*", t.name):
            errs.append(f"{t.name}: nom invalide")
        if t.side_effect not in SIDE_EFFECTS:
            errs.append(f"{t.name}: side_effect inconnu {t.side_effect}")
        if t.permission not in all_scopes:
            errs.append(f"{t.name}: permission inconnue {t.permission}")
        if t.permission == policy["approval"]["approver_scope"]:
            errs.append(f"{t.name}: la portée d'approbation ne peut pas servir de permission de tool")
        if t.side_effect.startswith("irreversible") and not t.requires_approval:
            errs.append(f"{t.name}: action irréversible sans approbation")
        if t.requires_approval != (t.name in approval_tools):
            errs.append(f"{t.name}: incohérence entre approval et policy.approval_required")
        if t.timeout_s <= 0 or t.max_calls_per_task < 1:
            errs.append(f"{t.name}: timeout ou plafond invalide")
        for label, schema in (("input", t.input_schema), ("output", t.output_schema)):
            try:
                Draft202012Validator.check_schema(schema)
            except Exception as e:  # noqa: BLE001
                errs.append(f"{t.name}: schéma {label} invalide ({e.__class__.__name__})")
        if t.input_schema.get("type") != "object" or t.input_schema.get("additionalProperties") is not False:
            errs.append(f"{t.name}: input_schema doit être un objet fermé (additionalProperties=false)")
        for e in t.errors:
            if not re.fullmatch(r"[A-Z_]+", e["code"]) or e["code"] in common:
                errs.append(f"{t.name}: code d'erreur invalide ou dupliqué {e['code']}")
    for a in approval_tools - set(tools):
        errs.append(f"approval_required référence un tool inconnu : {a}")
    for r in policy["argument_rules"]:
        tool = tools.get(r["tool"])
        if not tool or r["arg"] not in tool.input_schema["properties"]:
            errs.append(f"argument_rules : cible inconnue {r['tool']}.{r['arg']}")
        if r["code"] not in common:
            errs.append(f"argument_rules : code inconnu {r['code']}")
    b = policy["budget_default"]
    for k in ("max_steps", "max_tool_calls", "max_tokens", "max_cost_usd", "max_wall_s"):
        if not isinstance(b.get(k), (int, float)) or b[k] <= 0:
            errs.append(f"budget_default.{k} invalide")
    if policy["budget_on_exceed"]["action"] != "abort":
        errs.append("budget_on_exceed.action : seule 'abort' est implémentée")

    # --- scénarios
    ids = [s["id"] for s in scenarios]
    if len(ids) != len(set(ids)):
        errs.append("identifiants de scénario dupliqués")
    covered_ok: set[str] = set()
    covered_forbidden: set[str] = set()
    has_rejected = False
    for s in scenarios:
        sid = s["id"]
        inp, exp = s["input"], s["expected"]
        if s["category"] not in CATEGORIES:
            errs.append(f"{sid}: catégorie inconnue")
        if exp["status"] not in STATUSES:
            errs.append(f"{sid}: statut attendu inconnu")
        if s.get("min_phase") not in {2, 3}:
            errs.append(f"{sid}: min_phase doit valoir 2 ou 3")
        if inp["planner"]["type"] not in PLANNERS:
            errs.append(f"{sid}: planner inconnu")
        if set(inp["scopes"]) - all_scopes:
            errs.append(f"{sid}: portées inconnues")
        if set(exp["effects"]) != EFFECT_KEYS:
            errs.append(f"{sid}: effects doit définir exactement {sorted(EFFECT_KEYS)}")
        if exp["status"] == "budget_exceeded" and exp.get("budget_dimension") not in BUDGET_DIMS:
            errs.append(f"{sid}: budget_dimension manquante ou inconnue")
        approvals_needed = 0
        for c in exp["calls"]:
            name, outcome = c["tool"], c["outcome"]
            if outcome not in OUTCOMES:
                errs.append(f"{sid}: outcome inconnu {outcome}")
            if name in forbidden:
                covered_forbidden.add(name)
                if outcome != "denied" or c.get("code") != "TOOL_FORBIDDEN":
                    errs.append(f"{sid}: un tool interdit doit être denied/TOOL_FORBIDDEN")
                continue
            if name not in tools:
                errs.append(f"{sid}: tool inconnu {name}")
                continue
            t = tools[name]
            code = c.get("code")
            if outcome == "ok":
                covered_ok.add(name)
                if t.permission not in inp["scopes"]:
                    errs.append(f"{sid}: {name} ok mais la portée {t.permission} est absente")
                if t.requires_approval != bool(c.get("approval")):
                    errs.append(f"{sid}: {name} ok, drapeau approval incohérent avec la spec")
            if outcome == "rejected":
                has_rejected = True
            if outcome != "ok":
                kinds = {"error": "error", "denied": "denied", "rejected": "rejected"}
                known = {**{k: v for k, v in common.items()}, **{k: "error" for k in tool_codes[name]}}
                if code not in known:
                    errs.append(f"{sid}: code {code} inconnu pour {name}")
                elif known[code] != kinds[outcome]:
                    errs.append(f"{sid}: code {code} de type {known[code]} incompatible avec outcome {outcome}")
            if c.get("approval") and outcome in {"ok", "rejected", "error"}:
                approvals_needed += 1
            if outcome == "denied" and c.get("approval"):
                errs.append(f"{sid}: un appel refusé ne doit jamais demander d'approbation")
        if approvals_needed > len(inp.get("approvals", [])):
            errs.append(f"{sid}: {approvals_needed} approbation(s) attendue(s), {len(inp.get('approvals', []))} fournie(s)")
        if exp["effects"]["mail_sent"] and "audit:submit" not in inp["scopes"]:
            errs.append(f"{sid}: mail_sent > 0 sans portée audit:submit")

    for name in tools:
        if name not in covered_ok:
            errs.append(f"couverture : aucun scénario ok pour le tool {name}")
    for name in forbidden:
        if name in {"shell_exec", "delete_dossier"} and name not in covered_forbidden:
            errs.append(f"couverture : aucun scénario de refus pour {name}")
    if not has_rejected:
        errs.append("couverture : aucun scénario d'approbation refusée")
    return errs


def main() -> int:
    errs = check()
    for e in errs:
        print("✗", e)
    print(f"{'OK' if not errs else 'ÉCHEC'} : {len(errs)} incohérence(s)")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
