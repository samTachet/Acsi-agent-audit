"""Génère docs/tools.md, docs/policy.md et docs/scenarios.md à partir des specs.

Usage : python -m acsi_agent.docs_gen [--check]
--check échoue si les documents versionnés ne correspondent plus aux specs (dérive).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .specs import ROOT, load_policy, load_scenarios, load_tools

DOCS = ROOT / "docs"
HEADER = "<!-- Généré par `python -m acsi_agent.docs_gen` à partir de specs/ et scenarios/. Ne pas éditer à la main. -->\n\n"


def _fields(schema: dict) -> list[str]:
    req = set(schema.get("required", []))
    out = []
    for name, p in schema.get("properties", {}).items():
        if "enum" in p:
            typ = " | ".join(json.dumps(v, ensure_ascii=False) for v in p["enum"])
        elif "const" in p:
            typ = json.dumps(p["const"], ensure_ascii=False)
        else:
            typ = str(p.get("type", "object"))
            for k in ("minLength", "maxLength", "pattern", "format", "minimum"):
                if k in p:
                    typ += f", {k}={p[k]}"
        out.append(f"`{name}` ({'requis' if name in req else 'optionnel'}) : {typ}")
    return out


def render_tools() -> str:
    tools = load_tools()
    policy = load_policy()
    lines = [HEADER, "# Catalogue des tools\n"]
    lines.append("| Tool | Effet | Permission | Approbation | Timeout | Plafond / tâche |")
    lines.append("|---|---|---|---|---|---|")
    for t in tools.values():
        lines.append(f"| `{t.name}` | {t.side_effect} | `{t.permission}` | {t.approval} | {t.timeout_s:g} s | {t.max_calls_per_task} |")
    lines.append("")
    for t in tools.values():
        lines.append(f"## `{t.name}`\n")
        lines.append(f"**Objectif.** {t.purpose}\n")
        lines.append("**Entrées** (objet fermé : tout champ non listé est refusé)\n")
        lines += [f"- {f}" for f in _fields(t.input_schema)]
        lines.append("\n**Sorties**\n")
        lines += [f"- {f}" for f in _fields(t.output_schema)]
        lines.append("\n**Erreurs propres au tool**\n")
        if t.errors:
            lines += [f"- `{e['code']}` (réessayable : {'oui' if e['retryable'] else 'non'}) — {e['when']}" for e in t.errors]
        else:
            lines.append("- aucune (hors erreurs communes ci-dessous)")
        if t.notes:
            lines.append(f"\n**Note.** {t.notes}")
        lines.append("")
    lines.append("## Erreurs communes (gateway)\n")
    lines.append("| Code | Type | Quand |")
    lines.append("|---|---|---|")
    for e in policy["common_errors"]:
        lines.append(f"| `{e['code']}` | {e['kind']} | {e['when']} |")
    return "\n".join(lines) + "\n"


def render_policy() -> str:
    p = load_policy()
    lines = [HEADER, "# Politique d'actions\n", f"Politique par défaut : **{p['default']}** (tout tool absent de l'allowlist est refusé).\n"]
    lines.append("## Actions interdites (jamais exécutées, même avec toutes les portées)\n")
    lines += [f"- `{f['name']}` — {f['reason']}" for f in p["forbidden_actions"]]
    lines.append("\n## Actions soumises à approbation humaine\n")
    a = p["approval"]
    lines += [f"- `{x['tool']}` — {x['why']}" for x in p["approval_required"]]
    lines.append(
        f"\nRègles d'approbation : expiration {a['ttl_s']} s ; usage unique : {a['single_use']} ; "
        f"liée aux arguments exacts : {a['args_bound']} ; approbateur distinct du demandeur : {a['require_distinct_approver']} ; "
        f"portée requise : `{a['approver_scope']}`."
    )
    lines.append("\n## Règles sur les arguments\n")
    lines += [f"- `{r['tool']}.{r['arg']}` : {r['rule']} {r['allowed_domains']} → `{r['code']}`" for r in p["argument_rules"]]
    lines.append("\n## Rôles et portées\n")
    lines.append("| Rôle | Portées |")
    lines.append("|---|---|")
    for role, scopes in p["roles"].items():
        lines.append(f"| `{role}` | {', '.join(f'`{s}`' for s in scopes)} |")
    lines.append(f"\nPortées par défaut d'une tâche : {', '.join(f'`{s}`' for s in p['default_task_scopes'])} (minimum nécessaire ; l'appelant doit en demander davantage explicitement).")
    lines.append("\n## Budget par tâche\n")
    lines += [f"- `{k}` : {v}" for k, v in p["budget_default"].items()]
    b = p["budget_on_exceed"]
    lines.append(f"\nEn cas de dépassement : **{b['action']}**. {b['semantics']} Alerte à {int(b['warn_ratio'] * 100)} % du plafond.")
    lines.append("\n## Débit\n")
    lines += [f"- `{k}` : {v}" for k, v in p["rate_limits"].items()]
    return "\n".join(lines) + "\n"


def render_scenarios() -> str:
    lines = [HEADER, "# Scénarios de référence\n"]
    lines.append("| ID | Catégorie | Statut attendu | Tools attendus (résultat) | Approbations | Effets attendus |")
    lines.append("|---|---|---|---|---|---|")
    for s in load_scenarios():
        calls = []
        for c in s["expected"]["calls"]:
            rep = f" ×{c['repeat']}" if c.get("repeat") else ""
            tag = c["outcome"] + (f"/{c['code']}" if c.get("code") else "")
            calls.append(f"`{c['tool']}`{rep} → {tag}")
        calls_txt = "<br>".join(calls) if calls else "aucun appel"
        if s["expected"].get("budget_dimension"):
            calls_txt += f"<br>(budget : {s['expected']['budget_dimension']})"
        eff = s["expected"]["effects"]
        eff_txt = ", ".join(f"{k}={v}" for k, v in eff.items() if v) or "aucun effet"
        appr = ", ".join(s["input"].get("approvals", [])) or "—"
        lines.append(f"| `{s['id']}` | {s['category']} | {s['expected']['status']} | {calls_txt} | {appr} | {eff_txt} |")
    lines.append("")
    for s in load_scenarios():
        lines.append(f"- **{s['id']}** — {s['title']}. Tâche : « {s['input']['task']} »")
    return "\n".join(lines) + "\n"


FILES = {"tools.md": render_tools, "policy.md": render_policy, "scenarios.md": render_scenarios}


def main(argv: list[str]) -> int:
    check = "--check" in argv
    stale = []
    DOCS.mkdir(exist_ok=True)
    for name, fn in FILES.items():
        path: Path = DOCS / name
        content = fn()
        if check:
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                stale.append(name)
        else:
            path.write_text(content, encoding="utf-8")
    if stale:
        print("docs périmées :", ", ".join(stale), "— relancer `python -m acsi_agent.docs_gen`")
        return 1
    print("docs à jour" if check else "docs générées")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
