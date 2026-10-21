"""Métriques d'exploitation calculées depuis les journaux (sans vérité terrain).

En production il n'existe pas de « bonne réponse » de référence : on mesure donc le taux de
tâches menées à terme (`completion_rate`), pas un succès au sens de l'évaluation. Le succès
mesuré contre des attentes connues est celui de `eval.py` (scénarios de référence).
"""
from __future__ import annotations

from collections import Counter
from typing import Any

from .agent import State
from .journal import Journal


def task_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    st = State(events[0]["request_id"])
    for e in events:
        st.apply(e)
    status = st.finished["status"] if st.finished else ("awaiting_approval" if st.pending else "running")
    return {
        "request_id": st.request_id, "status": status, "steps": len(st.steps), "errors": sum(s.status == "error" for s in st.steps),
        "denied": sum(s.status in {"denied", "rejected"} for s in st.steps), "tool_calls": st.executed,
        "tokens": st.tokens_in + st.tokens_out, "cost_usd": st.cost, "active_ms": st.planner_ms + st.tool_ms,
        "human_wait_s": st.human_wait_s,
        "approvals_requested": sum(e["type"] == "approval_requested" for e in events),
        "budget_exceeded": st.budget_dimension,
    }


def aggregate_journals(journal: Journal) -> dict[str, Any]:
    tasks = [task_summary(ev) for rid in journal.list_ids() if (ev := journal.read(rid))]
    n = len(tasks)
    done = [t for t in tasks if t["status"] not in {"awaiting_approval", "running"}]
    with_err = [t for t in done if t["errors"] > 0]
    by_status = Counter(t["status"] for t in tasks)
    return {
        "tasks": n,
        "by_status": dict(by_status),
        "completion_rate": (by_status["completed"] / len(done)) if done else None,
        "mean_steps": sum(t["steps"] for t in done) / len(done) if done else None,
        "errors": sum(t["errors"] for t in tasks),
        "denied_or_rejected": sum(t["denied"] for t in tasks),
        # après une erreur d'outil, la tâche s'est-elle quand même terminée correctement ?
        "error_recovery_rate": (sum(t["status"] == "completed" for t in with_err) / len(with_err)) if with_err else None,
        "cost_usd_total": round(sum(t["cost_usd"] for t in tasks), 6),
        "cost_usd_per_task_mean": round(sum(t["cost_usd"] for t in done) / len(done), 6) if done else None,
        "cost_usd_per_task_max": round(max((t["cost_usd"] for t in done), default=0.0), 6),
        "tokens_total": sum(t["tokens"] for t in tasks),
        "budget_exceeded": dict(Counter(t["budget_exceeded"] for t in tasks if t["budget_exceeded"])),
        "pending_approvals": by_status["awaiting_approval"],
    }
