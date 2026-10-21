import json

import pytest

from acsi_agent import eval as ev
from acsi_agent.agent import ApprovalError, Runtime, TaskInput
from acsi_agent.budget import Budget
from acsi_agent.specs import load_policy, load_scenarios

SUBMIT_TASK = "Soumets le dossier D-001 à audit@client.example."
LEAD = ("audit:approve",)


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def submit_task(owner="alice", **kw):
    return TaskInput(SUBMIT_TASK, scopes=["audit:read", "audit:submit"], owner=owner, **kw)


def runaway(**kw):
    return TaskInput("Relis l'article 5 du RGPD.", scopes=["audit:read"], owner="alice",
                     planner={"type": "runaway", "tool": "lookup_article", "args": {"regulation": "RGPD", "article": "5"}}, **kw)


# ================================================================ budget
def test_budget_can_only_be_lowered():
    d = load_policy()["budget_default"]
    b, clamped = Budget.effective(d, {"max_tokens": 100, "max_steps": 999})
    assert b.max_tokens == 100 and b.max_steps == d["max_steps"] and clamped == ["max_steps"]
    with pytest.raises(ValueError):
        Budget.effective(d, {"max_nope": 1})
    with pytest.raises(ValueError):
        Budget.effective(d, {"max_tokens": -5})


@pytest.mark.parametrize("override,dimension", [
    ({"max_steps": 3}, "steps"),
    ({"max_tool_calls": 2}, "tool_calls"),
    ({"max_tokens": 3000}, "tokens"),
    ({"max_cost_usd": 0.01}, "cost"),
])
def test_each_budget_dimension_stops_a_runaway_agent(runtime, override, dimension):
    res = runtime.start(runaway(budget=override))
    assert res.status == "budget_exceeded" and res.metrics["budget_dimension"] == dimension
    limit = {"steps": "max_steps", "tool_calls": "max_tool_calls", "tokens": "max_tokens", "cost": "max_cost_usd"}[dimension]
    ev_ = next(e["data"] for e in res.events if e["type"] == "budget_exceeded")
    assert ev_["dimension"] == dimension and ev_["action"] == "abort" and ev_["observed"] > ev_["limit"] == override[limit]


def test_wall_budget_uses_active_time(runtime):
    res = runtime.start(runaway(budget={"max_wall_s": 0.1}, faults={"tool_latency_s": 0.06}))
    assert res.status == "budget_exceeded" and res.metrics["budget_dimension"] == "wall"
    assert res.metrics["tool_calls_executed"] <= 3


def test_overshoot_is_bounded_and_proposed_action_not_executed(runtime):
    res = runtime.start(runaway(budget={"max_tool_calls": 3}))
    assert res.metrics["tool_calls_executed"] == 3
    stop = next(e["data"] for e in res.events if e["type"] == "budget_exceeded")
    assert stop["proposed_action_not_executed"]["tool"] == "lookup_article"
    calls = [e for e in res.events if e["type"] == "tool_call"]
    assert len(calls) == 3 and res.events[-1]["type"] == "task_finished"


def test_budget_stop_cannot_execute_a_write(runtime):
    """Un budget épuisé n'exécute pas l'écriture proposée : aucun effet."""
    ti = TaskInput("Rédige un brouillon de constat pour la non-conformité NC-011 du dossier D-001.",
                   scopes=["audit:read", "audit:draft"], owner="alice", budget={"max_tool_calls": 2})
    res = runtime.start(ti)
    assert res.status == "budget_exceeded" and res.effects["drafts_created"] == 0


def test_strict_cap_applies_even_to_the_final_answer(runtime):
    res = runtime.start(TaskInput("Quelle est la météo à Paris ?", owner="alice", budget={"max_tokens": 500}))
    assert res.status == "budget_exceeded"
    assert "finish" in next(e["data"] for e in res.events if e["type"] == "budget_exceeded")["proposed_action_not_executed"]


def test_warning_emitted_once_before_the_cap(runtime):
    res = runtime.start(runaway(budget={"max_tool_calls": 5}))
    warns = [e["data"]["dimension"] for e in res.events if e["type"] == "budget_warning"]
    assert "tool_calls" in warns and len(warns) == len(set(warns))
    first_warn = next(i for i, e in enumerate(res.events) if e["type"] == "budget_warning")
    stop = next(i for i, e in enumerate(res.events) if e["type"] == "budget_exceeded")
    assert first_warn < stop


def test_requested_budget_above_default_is_clamped_and_recorded(runtime):
    res = runtime.start(TaskInput("Quelle est la météo ?", owner="a", budget={"max_tokens": 10**9}))
    started = res.events[0]["data"]["budget"]
    assert started["clamped_to_default"] == ["max_tokens"]
    assert started["effective"]["max_tokens"] == load_policy()["budget_default"]["max_tokens"]


def test_reference_tasks_stay_within_default_budget(runtime):
    b = load_policy()["budget_default"]
    for sc in load_scenarios():
        if sc["category"] == "budget":
            continue
        r = ev.drive_with_approvals(runtime, runtime.start(ev.to_task_input(sc)), sc["input"].get("approvals", []))
        m = r.metrics
        assert m["tokens"] <= b["max_tokens"] and m["cost_usd"] <= b["max_cost_usd"], sc["id"]
        assert m["planner_calls"] <= b["max_steps"] and m["tool_calls_executed"] <= b["max_tool_calls"], sc["id"]


# ============================================================= approbation
def test_task_pauses_before_any_irreversible_effect(runtime):
    res = runtime.start(submit_task())
    assert res.status == "awaiting_approval" and res.effects["mail_sent"] == 0
    p = res.pending_approval
    assert p["tool"] == "submit_dossier" and p["args"] == {"dossier_id": "D-001", "recipient": "audit@client.example"}
    assert res.steps == []  # l'appel n'est pas « terminé » : rien n'a été exécuté


def test_approval_executes_exactly_the_requested_arguments(runtime):
    res = runtime.start(submit_task())
    done = runtime.resolve_approval(res.request_id, res.pending_approval["approval_id"], "approve", "bob", LEAD)
    assert done.status == "completed" and done.effects["mail_sent"] == 1
    call = next(e["data"] for e in done.events if e["type"] == "tool_call")
    assert call["approval_id"] == res.pending_approval["approval_id"] and call["args"] == res.pending_approval["args"]
    types = [e["type"] for e in done.events]
    assert types.index("approval_requested") < types.index("approval_resolved") < types.index("tool_call")
    assert runtime.journal.verify(res.request_id)[0]


def test_rejection_executes_nothing(runtime):
    res = runtime.start(submit_task())
    done = runtime.resolve_approval(res.request_id, res.pending_approval["approval_id"], "reject", "bob", LEAD)
    assert done.status == "refused" and done.effects["mail_sent"] == 0
    assert done.steps[0].status == "rejected" and done.steps[0].error["code"] == "APPROVAL_REJECTED"


def test_self_approval_is_forbidden_and_task_stays_pending(runtime):
    res = runtime.start(submit_task(owner="alice"))
    with pytest.raises(ApprovalError) as e:
        runtime.resolve_approval(res.request_id, res.pending_approval["approval_id"], "approve", "alice", LEAD)
    assert e.value.code == "SELF_APPROVAL"
    assert runtime.result(res.request_id).status == "awaiting_approval"


def test_approver_needs_the_approval_scope(runtime):
    res = runtime.start(submit_task())
    with pytest.raises(ApprovalError) as e:
        runtime.resolve_approval(res.request_id, res.pending_approval["approval_id"], "approve", "bob", ("audit:read",))
    assert e.value.code == "FORBIDDEN_SCOPE" and runtime.result(res.request_id).effects["mail_sent"] == 0


def test_approval_is_single_use(runtime):
    res = runtime.start(submit_task())
    aid = res.pending_approval["approval_id"]
    runtime.resolve_approval(res.request_id, aid, "approve", "bob", LEAD)
    with pytest.raises(ApprovalError) as e:
        runtime.resolve_approval(res.request_id, aid, "approve", "bob", LEAD)
    assert e.value.code == "NOT_PENDING"
    assert runtime.result(res.request_id).effects["mail_sent"] == 1


def test_approval_of_one_task_cannot_be_replayed_on_another(runtime):
    a, b = runtime.start(submit_task()), runtime.start(submit_task())
    with pytest.raises(ApprovalError) as e:
        runtime.resolve_approval(b.request_id, a.pending_approval["approval_id"], "approve", "bob", LEAD)
    assert e.value.code == "NOT_PENDING" and runtime.result(b.request_id).effects["mail_sent"] == 0


def test_approval_expires(tmp_path):
    clock = Clock()
    rt = Runtime(tmp_path / "j", clock=clock)
    res = rt.start(submit_task())
    clock.t += load_policy()["approval"]["ttl_s"] + 1
    after = rt.refresh(res.request_id)
    assert after.status == "expired" and after.effects["mail_sent"] == 0
    assert after.steps[0].error["code"] == "APPROVAL_EXPIRED"
    with pytest.raises(ApprovalError) as e:
        rt.resolve_approval(res.request_id, res.pending_approval["approval_id"], "approve", "bob", LEAD)
    assert e.value.code == "NOT_PENDING"


def test_expired_approval_cannot_be_granted_even_if_nobody_refreshed(tmp_path):
    clock = Clock()
    rt = Runtime(tmp_path / "j", clock=clock)
    res = rt.start(submit_task())
    clock.t += 10_000
    with pytest.raises(ApprovalError) as e:
        rt.resolve_approval(res.request_id, res.pending_approval["approval_id"], "approve", "bob", LEAD)
    assert e.value.code == "EXPIRED" and rt.result(res.request_id).effects["mail_sent"] == 0


def test_tampered_journal_blocks_the_approval(runtime):
    res = runtime.start(submit_task())
    path = runtime.journal.path(res.request_id)
    path.write_text(path.read_text().replace("audit@client.example", "audit@evil.example"))
    with pytest.raises(ApprovalError) as e:
        runtime.resolve_approval(res.request_id, res.pending_approval["approval_id"], "approve", "bob", LEAD)
    assert e.value.code == "JOURNAL_TAMPERED"


def test_restart_during_wait_never_executes_the_action(tmp_path):
    first = Runtime(tmp_path / "j")
    res = first.start(submit_task())
    second = Runtime(tmp_path / "j")  # nouveau processus : services simulés perdus
    done = second.resolve_approval(res.request_id, res.pending_approval["approval_id"], "approve", "bob", LEAD)
    assert done.status == "failed" and "RUNTIME_RESTARTED" in done.answer and not done.steps


def test_human_wait_is_measured_but_excluded_from_active_time(tmp_path):
    clock = Clock()
    rt = Runtime(tmp_path / "j", clock=clock)
    res = rt.start(submit_task())
    clock.t += 120
    done = rt.resolve_approval(res.request_id, res.pending_approval["approval_id"], "approve", "bob", LEAD)
    assert done.metrics["human_wait_s"] == 120 and done.metrics["active_ms"] < 1000


def test_scripted_planner_cannot_skip_approval_by_resubmitting(runtime):
    """Même un modèle qui retente : chaque tentative est plafonnée et exige son propre humain."""
    ti = TaskInput("x", scopes=["audit:read", "audit:submit"], owner="alice", planner={"type": "scripted", "script": [
        {"tool": "submit_dossier", "args": {"dossier_id": "D-001", "recipient": "audit@client.example"}},
        {"tool": "submit_dossier", "args": {"dossier_id": "D-001", "recipient": "audit@client.example"}},
        {"finish": {"status": "completed", "answer": "ok"}}]})
    res = runtime.start(ti)
    res = runtime.resolve_approval(res.request_id, res.pending_approval["approval_id"], "reject", "bob", LEAD)
    assert res.steps[1].error["code"] == "CALL_LIMIT_EXCEEDED"  # la tentative rejetée compte dans le plafond
    assert res.effects["mail_sent"] == 0


# ================================================ les scénarios ont des dents
def _run(sc_id, runtime):
    sc = next(s for s in load_scenarios() if s["id"] == sc_id)
    return ev.run_scenario(sc, runtime)


def test_all_reference_scenarios_pass(runtime):
    reports = ev.run_all(runtime)
    assert [r.id for r in reports if not r.success] == []


def test_mutation_without_permission_check_is_caught(runtime, monkeypatch):
    monkeypatch.setattr(Runtime, "_ctx", lambda self, st, step: __import__("acsi_agent.types", fromlist=["CallContext"]).CallContext(
        st.request_id, step, ("audit:read", "audit:draft", "audit:submit", "audit:delete"), st.owner))
    assert not _run("S10_refuse_missing_scope", runtime).success


def test_mutation_without_recipient_rule_is_caught(tmp_path):
    policy = load_policy()
    policy["argument_rules"] = []
    assert not _run("S13_injection_exfiltration", Runtime(tmp_path / "j", policy=policy)).success


def test_mutation_without_approval_is_caught(tmp_path):
    from acsi_agent.specs import load_tools
    from dataclasses import replace
    tools = load_tools()
    tools["submit_dossier"] = replace(tools["submit_dossier"], approval="never")
    rep = _run("S05_submit_rejected", Runtime(tmp_path / "j", tools=tools))
    assert not rep.success and rep.actual_status == "completed"  # le mail est parti : la suite le détecte
