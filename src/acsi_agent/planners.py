"""Planners déterministes (aucun appel de modèle).

- RuleBasedPlanner : planner de référence, il joue un modèle compétent et prudent.
- ScriptedPlanner  : rejoue un script fixe, il joue un modèle trompé ou maladroit
                     (injection, arguments invalides, doublons) pour éprouver la gateway.
- RunawayPlanner   : ne s'arrête jamais, pour éprouver le budget.

La sécurité ne repose PAS sur le planner : elle est garantie par la gateway, que ces
planners « adverses » permettent de tester indépendamment du comportement du modèle.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Callable

from .types import Finish, PlanContext, StepRecord, ToolAction

DOSSIER_RE = re.compile(r"\bD-\d{3}\b")
NC_RE = re.compile(r"\bNC-\d{3}\b")
DRAFT_RE = re.compile(r"\bDR-[0-9a-z]{4,12}\b")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
QUOTED_RE = re.compile(r"«\s*(.+?)\s*»", re.S)
BACKTICK_RE = re.compile(r"`([^`]+)`")


def _norm(s: str) -> str:
    return unicodedata.normalize("NFKD", s.casefold()).encode("ascii", "ignore").decode()


@dataclass
class Done:
    consumed: int


Phase = Callable[[str, list[StepRecord]], "ToolAction | Finish | Done"]


# ---------------------------------------------------------------- phases
def _phase_classify(task: str, steps: list[StepRecord]):
    if steps:
        return Done(1)
    m = QUOTED_RE.search(task)
    return ToolAction("classify_excerpt", {"text": m.group(1) if m else task})


def _phase_list_lookup(task: str, steps: list[StepRecord]):
    d = DOSSIER_RE.search(task)
    if not d:
        return Finish("failed", "Aucun identifiant de dossier (D-xxx) dans la demande.")
    if not steps:
        return ToolAction("list_nonconformities", {"dossier_id": d.group(0)})
    high = [n for n in steps[0].result["nonconformities"] if n["severity"] == "high" and n["status"] == "open"]
    done = len(steps) - 1
    if done < len(high):
        return ToolAction("lookup_article", {"regulation": high[done]["regulation"], "article": high[done]["article"]})
    return Done(1 + len(high))


def _phase_draft(task: str, steps: list[StepRecord]):
    d, nc = DOSSIER_RE.search(task), NC_RE.search(task)
    if not d or not nc:
        return Finish("failed", "Dossier (D-xxx) ou non-conformité (NC-xxx) manquant dans la demande.")
    if not steps:
        return ToolAction("list_nonconformities", {"dossier_id": d.group(0)})
    found = next((n for n in steps[0].result["nonconformities"] if n["id"] == nc.group(0)), None)
    if found is None:
        return Finish("failed", f"{nc.group(0)} n'appartient pas au dossier {d.group(0)}.")
    if len(steps) == 1:
        return ToolAction("lookup_article", {"regulation": found["regulation"], "article": found["article"]})
    if len(steps) == 2:
        art = steps[1].result
        text = (f"Constat {found['id']} — {found['title']}. Référence : {found['regulation']} art. {art['article']} "
                f"({art['title']}). Gravité : {found['severity']}.")
        return ToolAction("draft_finding", {"dossier_id": d.group(0), "nc_id": found["id"], "text": text})
    return Done(3)


def _phase_submit(task: str, steps: list[StepRecord]):
    d, e = DOSSIER_RE.search(task), EMAIL_RE.search(task)
    if not d or not e:
        return Finish("failed", "Dossier (D-xxx) ou destinataire manquant dans la demande.")
    if not steps:
        return ToolAction("submit_dossier", {"dossier_id": d.group(0), "recipient": e.group(0)})
    return Done(1)


def _phase_delete_draft(task: str, steps: list[StepRecord]):
    m = DRAFT_RE.search(task)
    if not m:
        return Finish("failed", "Identifiant de brouillon (DR-…) manquant.")
    return Done(1) if steps else ToolAction("delete_draft", {"draft_id": m.group(0)})


def _phase_shell(task: str, steps: list[StepRecord]):
    if steps:
        return Done(1)
    m = BACKTICK_RE.search(task)
    return ToolAction("shell_exec", {"command": m.group(1) if m else task})


def _phase_delete_dossier(task: str, steps: list[StepRecord]):
    m = DOSSIER_RE.search(task)
    if not m:
        return Finish("failed", "Identifiant de dossier manquant.")
    return Done(1) if steps else ToolAction("delete_dossier", {"dossier_id": m.group(0)})


def phases_for(task: str) -> list[Phase]:
    t = _norm(task)
    submit = bool(re.search(r"\b(soumets|soumettre|transmets|transmettre|envoie)\b", t))
    if re.search(r"\bcommande\b|\bshell\b", t):
        return [_phase_shell]
    if re.search(r"\bsupprim", t):
        if "brouillon" in t:
            return [_phase_delete_draft]
        return [_phase_delete_dossier] if DOSSIER_RE.search(task) else []
    if re.search(r"\bclassifi|\bclasse\b", t):
        return [_phase_classify]
    if "brouillon" in t or "redige" in t:
        return [_phase_draft] + ([_phase_submit] if submit else [])
    if submit:
        return [_phase_submit]
    if ("non-conformit" in t or "non conformit" in t) and DOSSIER_RE.search(task):
        return [_phase_list_lookup]
    return []


def _summarize(steps: list[StepRecord]) -> str:
    parts = []
    for s in steps:
        r = s.result or {}
        if s.tool == "classify_excerpt":
            parts.append(f"Extrait classé : {r['regulation']} art. {r['article']} (gravité {r['severity']}).")
        elif s.tool == "list_nonconformities":
            parts.append(f"Dossier {r['dossier_id']} ({r['client']}) : {len(r['nonconformities'])} non-conformité(s).")
        elif s.tool == "lookup_article":
            parts.append(f"{r['regulation']} art. {r['article']} — {r['title']}.")
        elif s.tool == "draft_finding":
            parts.append(f"Brouillon {r['draft_id']} créé pour {r['nc_id']}.")
        elif s.tool == "delete_draft":
            parts.append(f"Brouillon {r['draft_id']} supprimé.")
        elif s.tool == "submit_dossier":
            parts.append(f"Dossier {r['dossier_id']} transmis à {r['recipient']} ({r['drafts_submitted']} brouillon(s)), réf. {r['submission_id']}.")
    return " ".join(parts) or "Tâche accomplie."


class RuleBasedPlanner:
    id = "rule-planner-v1"

    def plan(self, ctx: PlanContext) -> ToolAction | Finish:
        phases = phases_for(ctx.task)
        if not phases:
            return Finish("refused", "Cette demande sort du périmètre des tools d'audit disponibles ; aucun tool n'a été appelé.")
        steps = ctx.steps
        if steps and steps[-1].status != "ok":
            last = steps[-1]
            code = (last.error or {}).get("code", "?")
            if last.status in {"denied", "rejected"}:
                return Finish("refused", f"Action `{last.tool}` non exécutée ({code}). Je ne tente pas de la contourner.")
            return Finish("failed", f"Échec de `{last.tool}` ({code}) : {(last.error or {}).get('message', '')}".strip())
        consumed = 0
        for phase in phases:
            r = phase(ctx.task, steps[consumed:])
            if isinstance(r, Done):
                consumed += r.consumed
                continue
            return r
        return Finish("completed", _summarize(steps))


class ScriptedPlanner:
    id = "scripted-planner"

    def __init__(self, script: list[dict[str, Any]]):
        self.script = script

    def plan(self, ctx: PlanContext) -> ToolAction | Finish:
        i = len(ctx.steps)
        if i >= len(self.script):
            return Finish("failed", "Script épuisé sans conclusion.")
        item = self.script[i]
        if "finish" in item:
            return Finish(item["finish"]["status"], item["finish"]["answer"])
        return ToolAction(item["tool"], dict(item["args"]))


class RunawayPlanner:
    id = "runaway-planner"

    def __init__(self, tool: str, args: dict[str, Any]):
        self.tool, self.args = tool, args

    def plan(self, ctx: PlanContext) -> ToolAction | Finish:
        return ToolAction(self.tool, dict(self.args))


def make_planner(spec: dict[str, Any]):
    kind = spec["type"]
    if kind == "rule":
        return RuleBasedPlanner()
    if kind == "scripted":
        return ScriptedPlanner(spec["script"])
    if kind == "runaway":
        return RunawayPlanner(spec["tool"], spec["args"])
    raise ValueError(f"planner inconnu : {kind}")
