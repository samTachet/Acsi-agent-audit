"""Replay de trajectoire : lecture humaine du journal et ré-exécution contrôlée.

`reexecute` rejoue une tâche dans un runtime vierge et ISOLÉ (journal temporaire, services simulés neufs) :
le planner est remplacé par les sorties enregistrées, les approbations par les décisions enregistrées. Tout
passe par la vraie gateway. Si le comportement diffère (autre décision de gateway, autre résultat, autre effet),
c'est que la politique, un tool ou les données ont changé depuis l'enregistrement — ce qui est exactement ce
qu'on veut savoir après une modification.

Limites : valable pour le backend simulé ; un budget de temps (`wall`) dépend de l'horloge et n'est pas rejouable ;
une tâche clôturée par RUNTIME_RESTARTED ne l'est pas non plus.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from .agent import Runtime, TaskInput, fingerprint
from .backends import fixtures_hash
from .journal import Journal
from .specs import ToolSpec
from .types import Finish, PlanContext, ToolAction


class RecordedPlanner:
    id = "recorded-planner"

    def __init__(self, outputs: list[dict[str, Any]]):
        self.outputs, self.i = outputs, 0

    def plan(self, ctx: PlanContext) -> ToolAction | Finish:
        if self.i >= len(self.outputs):
            return Finish("failed", "journal épuisé : le rejeu demande plus de décisions que l'enregistrement n'en contient")
        o = self.outputs[self.i]
        self.i += 1
        return Finish(o["status"], o["answer"]) if o["kind"] == "finish" else ToolAction(o["tool"], o["args"])


class _Clock:
    def __init__(self, t: float):
        self.t = t

    def __call__(self) -> float:
        return self.t


def _c(x: Any) -> str:
    return json.dumps(x, sort_keys=True, ensure_ascii=False)


def behavior(events: list[dict[str, Any]]) -> list[tuple]:
    """Vue comparable : tout ce qui est comportement, rien de ce qui est temps, hachage ou coût."""
    out: list[tuple] = []
    for e in events:
        t, d = e["type"], e["data"]
        if t == "task_started":
            out.append((t, d["task"], tuple(d["scopes"]), d["owner"], _c(d["budget"]["effective"])))
        elif t == "planner_output":
            out.append((t, d["kind"], d.get("tool"), _c(d.get("args")), d.get("status"), d.get("answer")))
        elif t == "tool_call":
            g = d["gateway"]
            out.append((t, d["step"], d["tool"], _c(d["args"]), d["status"], g["verdict"], g["code"], tuple(g["checks"]),
                        _c(d["result"]), (d["error"] or {}).get("code"), d["approval_id"], d["executed"]))
        elif t == "approval_requested":
            out.append((t, d["approval_id"], d["step"], d["tool"], d["args_hash"], d["requested_by"]))
        elif t == "approval_resolved":
            out.append((t, d["approval_id"], d["decision"], d["approver"]))
        elif t == "budget_exceeded":
            out.append((t, d["dimension"], d["limit"]))
        elif t == "budget_warning" and d["dimension"] != "wall":
            out.append((t, d["dimension"]))
        elif t == "task_finished":
            out.append((t, d["status"], d["answer"], _c(d["effects"])))
    return out


def usage(events: list[dict[str, Any]]) -> list[tuple[int, int]]:
    return [(e["data"]["usage"]["tokens_in"], e["data"]["usage"]["tokens_out"]) for e in events if e["type"] == "planner_output"]


def reexecute(journal: Journal, rid: str, tools: dict[str, ToolSpec] | None = None, policy: dict[str, Any] | None = None) -> dict[str, Any]:
    events = journal.read(rid)
    if not events:
        raise KeyError(rid)
    ok, why = journal.verify(rid)
    base: dict[str, Any] = {"request_id": rid, "chain_ok": ok, "replayable": False, "identical": False,
                            "differences": [], "usage_drift": [], "fingerprint_changes": {}, "fixtures_changed": False}
    if not ok:
        return {**base, "reason": f"journal non intègre ({why}) : rejeu refusé"}
    started = events[0]["data"]
    fin = next((e["data"] for e in reversed(events) if e["type"] == "task_finished"), None)
    if fin and "RUNTIME_RESTARTED" in fin["answer"]:
        return {**base, "reason": "tâche clôturée par un redémarrage du runtime : non rejouable"}
    if any(e["type"] == "budget_exceeded" and e["data"]["dimension"] == "wall" for e in events):
        return {**base, "reason": "dépassement du budget de temps : dépend de l'horloge, non rejouable"}

    clock = _Clock(events[0]["ts"])
    scratch = Runtime(Path(tempfile.mkdtemp(prefix="acsi-replay-")), tools, policy, clock=clock)
    ti = TaskInput(task=started["task"], scopes=list(started["scopes"]), owner=started["owner"], planner=started["planner"],
                   faults=started["faults"], budget=started["budget"]["effective"])
    outputs = [e["data"] for e in events if e["type"] == "planner_output"]
    res = scratch.start(ti, request_id=rid, planner=RecordedPlanner(outputs))
    for r in (e["data"] for e in events if e["type"] == "approval_resolved"):
        if res.status != "awaiting_approval":
            break
        if r["decision"] == "expired":
            clock.t += scratch.policy["approval"]["ttl_s"] + 1
            res = scratch.refresh(rid)
        else:
            res = scratch.resolve_approval(rid, res.pending_approval["approval_id"], r["decision"], r["approver"], ("audit:approve",))

    a, b = behavior(events), behavior(res.events)
    diffs = []
    for i in range(max(len(a), len(b))):
        x, y = (a[i] if i < len(a) else None), (b[i] if i < len(b) else None)
        if x != y:
            diffs.append({"index": i, "recorded": x, "replayed": y})
    fp_old = started.get("fingerprint", {})
    fp_new = fingerprint(fp_old.get("planner_id", ""))
    return {
        **base, "replayable": True, "identical": not diffs, "differences": diffs[:20],
        "usage_drift": [] if usage(events) == usage(res.events) else [{"recorded": usage(events), "replayed": usage(res.events)}],
        "fingerprint_changes": {k: [fp_old.get(k), v] for k, v in fp_new.items() if fp_old.get(k) != v},
        "fixtures_changed": started.get("fixtures_hash") != fixtures_hash(),
        "events_recorded": len(events), "events_replayed": len(res.events),
    }


def render_timeline(events: list[dict[str, Any]]) -> str:
    if not events:
        return "(journal vide)"
    t0 = events[0]["ts"]
    s = events[0]["data"]
    b = s["budget"]["effective"]
    lines = [f"{events[0]['request_id']} — {s['owner']} : « {s['task']} »",
             f"portées {s['scopes']} · budget {b['max_steps']} étapes / {b['max_tool_calls']} appels / {b['max_tokens']} tokens / "
             f"{b['max_cost_usd']} $ / {b['max_wall_s']} s"]
    for e in events[1:]:
        t, d = e["type"], e["data"]
        at = f"#{e['seq']:<2} +{(e['ts'] - t0) * 1000:>7.1f} ms"
        if t == "planner_output":
            u = d["usage"]
            what = (f"→ {d['tool']} {_c(d['args'])}" if d["kind"] == "tool_call" else f"→ conclut ({d['status']})")
            lines.append(f"{at}  planner   {what}   [{u['tokens_in'] + u['tokens_out']} tok, {u['cost_usd']:.4f} $]")
        elif t == "tool_call":
            g = d["gateway"]
            mark = {"ok": "OK", "error": "ERREUR", "denied": "REFUSÉ", "rejected": "REJETÉ"}[d["status"]]
            detail = ""
            if d["status"] == "ok":
                detail = "  " + _c(d["result"])[:110]
            elif d["error"]:
                detail = f"  {d['error']['code']} — {d['error']['message'][:90]}"
            lines.append(f"{at}  {d['tool']:<22} {mark}{detail}")
            lines.append(f"{'':27}contrôles : {' '.join(c.replace(':pass', '✓').replace(':fail', '✗') for c in g['checks'])}"
                         + (f"  · approbation {d['approval_id']}" if d["approval_id"] else ""))
        elif t == "approval_requested":
            lines.append(f"{at}  APPROBATION demandée {d['approval_id']} pour {d['tool']} {_c(d['args'])} (par {d['requested_by']})")
        elif t == "approval_resolved":
            lines.append(f"{at}  APPROBATION {d['decision']} par {d['approver']}")
        elif t == "budget_warning":
            lines.append(f"{at}  budget : alerte {d['dimension']} à {d['ratio']:.0%}")
        elif t == "budget_exceeded":
            lines.append(f"{at}  BUDGET DÉPASSÉ ({d['dimension']} : {d['observed']} > {d['limit']}) → {d['action']} ; "
                         f"action proposée non exécutée : {_c(d['proposed_action_not_executed'])[:100]}")
        elif t == "task_finished":
            m = d["metrics"]
            lines.append(f"{at}  FIN {d['status']} — {d['answer'][:140]}")
            lines.append(f"{'':27}{m['planner_calls']} appels planner, {m['tool_calls_executed']} tools exécutés, {m['tokens']} tokens, "
                         f"{m['cost_usd']:.4f} $, effets {d['effects']}")
    return "\n".join(lines)
