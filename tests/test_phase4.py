import json
import shutil

import pytest
from fastapi.testclient import TestClient

from acsi_agent import agent as agent_mod
from acsi_agent import eval as ev
from acsi_agent import prompt as prompt_mod
from acsi_agent import regress, replay, tools_impl
from acsi_agent.agent import Runtime, TaskInput
from acsi_agent.api import create_app
from acsi_agent.metrics import aggregate_journals
from acsi_agent.specs import load_policy, load_scenarios

LEAD = ("audit:approve",)


def run_ref(runtime):
    out = {}
    for sc in load_scenarios():
        out[sc["id"]] = ev.drive_with_approvals(runtime, runtime.start(ev.to_task_input(sc)), sc["input"].get("approvals", []))
    return out


# ============================================================ régression
def test_committed_baseline_matches_current_behavior():
    assert regress.main([]) == 0


def test_regression_detects_a_changed_trajectory_and_failure(runtime):
    reports = ev.run_all(runtime)
    base = regress.build_baseline(reports)
    broken = [r for r in reports]
    broken[0].tool_sequence = ["lookup_article"]
    broken[0].success = False
    fails, _ = regress.compare(base, broken)
    assert any("RÉGRESSION" in f for f in fails) and any("trajectoire" in f for f in fails)


def test_regression_detects_cost_and_step_growth(runtime):
    reports = ev.run_all(runtime)
    base = regress.build_baseline(reports)
    reports[2].metrics = {**reports[2].metrics, "cost_usd": reports[2].metrics["cost_usd"] * 1.5, "steps": reports[2].metrics["steps"] + 1,
                          "tokens": int(reports[2].metrics["tokens"] * 1.5)}
    fails, _ = regress.compare(base, reports)
    assert any("coût" in f for f in fails) and any("étapes" in f for f in fails) and any("tokens" in f for f in fails)


def test_prompt_change_forces_a_rerun_and_is_caught(monkeypatch):
    longer = prompt_mod.SYSTEM_PROMPT + "\n" + "Consigne supplémentaire verbeuse. " * 80
    monkeypatch.setattr(prompt_mod, "SYSTEM_PROMPT", longer)
    monkeypatch.setattr(agent_mod, "SYSTEM_PROMPT", longer)
    assert regress.main([]) == 1  # le coût en tokens dérive : échec de comportement, pas seulement d'empreinte


def test_tool_change_is_caught(monkeypatch):
    monkeypatch.setattr(tools_impl, "RULES", [r for r in tools_impl.RULES if r[1] != "10"])
    assert regress.main([]) == 1


def test_model_change_without_baseline_update_exits_2(monkeypatch):
    monkeypatch.setattr(agent_mod, "MODEL_ID", "autre-modele")
    assert regress.main([]) == 2


def test_baseline_is_only_updated_on_explicit_request_and_only_if_green(tmp_path, monkeypatch):
    target = tmp_path / "b.json"
    assert regress.main(["--baseline", str(target)]) == 1  # absente
    assert regress.main(["--baseline", str(target), "--update-baseline"]) == 0 and target.exists()
    assert regress.main(["--baseline", str(target)]) == 0
    monkeypatch.setattr(tools_impl, "RULES", [])
    target2 = tmp_path / "b2.json"
    assert regress.main(["--baseline", str(target2), "--update-baseline"]) == 1 and not target2.exists()


# ================================================================ replay
def test_replay_reproduces_every_reference_trajectory(runtime):
    for sid, res in run_ref(runtime).items():
        rep = replay.reexecute(runtime.journal, res.request_id, runtime.tools, runtime.policy)
        assert rep["replayable"] and rep["identical"], (sid, rep["differences"][:2])
        assert rep["usage_drift"] == [] and rep["fingerprint_changes"] == {} and not rep["fixtures_changed"]


def test_replay_is_isolated_from_the_original_journal(runtime):
    res = runtime.start(TaskInput("Quelle est la météo ?", owner="a"))
    path = runtime.journal.path(res.request_id)
    before = path.read_bytes()
    replay.reexecute(runtime.journal, res.request_id)
    assert path.read_bytes() == before


def test_replay_detects_a_policy_change(runtime):
    sc = next(s for s in load_scenarios() if s["id"] == "S04_submit_approved")
    res = ev.drive_with_approvals(runtime, runtime.start(ev.to_task_input(sc)), ["approve"])
    assert res.effects["mail_sent"] == 1
    stricter = load_policy()
    stricter["argument_rules"][0]["allowed_domains"] = ["cabinet.example"]
    rep = replay.reexecute(runtime.journal, res.request_id, runtime.tools, stricter)
    assert rep["replayable"] and not rep["identical"]
    assert any("RECIPIENT_NOT_ALLOWED" in str(d["replayed"]) for d in rep["differences"])


def test_replay_flags_prompt_drift_as_usage_and_fingerprint(runtime, monkeypatch):
    res = runtime.start(TaskInput("Quelle est la météo ?", owner="a"))
    longer = prompt_mod.SYSTEM_PROMPT + " ajout" * 50
    monkeypatch.setattr(prompt_mod, "SYSTEM_PROMPT", longer)
    monkeypatch.setattr(agent_mod, "SYSTEM_PROMPT", longer)
    rep = replay.reexecute(runtime.journal, res.request_id)
    assert rep["identical"] and rep["usage_drift"] and "prompt_hash" in rep["fingerprint_changes"]


def test_replay_refuses_a_tampered_journal(runtime):
    res = runtime.start(TaskInput("Quelle est la météo ?", owner="a"))
    p = runtime.journal.path(res.request_id)
    p.write_text(p.read_text().replace("Quelle est la météo", "Autre question"))
    rep = replay.reexecute(runtime.journal, res.request_id)
    assert not rep["replayable"] and not rep["chain_ok"]


def test_replay_of_an_expired_approval(tmp_path):
    class Clock:
        t = 1_000_000.0
        def __call__(self): return self.t
    clock = Clock()
    rt = Runtime(tmp_path / "j", clock=clock)
    res = rt.start(TaskInput("Soumets le dossier D-001 à audit@client.example.", scopes=["audit:read", "audit:submit"], owner="alice"))
    clock.t += 5000
    rt.refresh(res.request_id)
    rep = replay.reexecute(rt.journal, res.request_id)
    assert rep["identical"]


def test_replay_of_a_task_still_awaiting_approval(runtime):
    res = runtime.start(TaskInput("Soumets le dossier D-001 à audit@client.example.", scopes=["audit:read", "audit:submit"], owner="alice"))
    rep = replay.reexecute(runtime.journal, res.request_id)
    assert rep["identical"] and rep["events_replayed"] == rep["events_recorded"]


def test_replay_does_not_resurrect_a_restart_aborted_task(tmp_path):
    first = Runtime(tmp_path / "j")
    res = first.start(TaskInput("Soumets le dossier D-001 à audit@client.example.", scopes=["audit:read", "audit:submit"], owner="alice"))
    second = Runtime(tmp_path / "j")
    second.resolve_approval(res.request_id, res.pending_approval["approval_id"], "approve", "bob", LEAD)
    rep = replay.reexecute(second.journal, res.request_id)
    assert not rep["replayable"] and "redémarrage" in rep["reason"]


def test_timeline_is_readable(runtime):
    sc = next(s for s in load_scenarios() if s["id"] == "S07_draft_then_submit")
    res = ev.drive_with_approvals(runtime, runtime.start(ev.to_task_input(sc)), ["approve"])
    text = replay.render_timeline(res.events)
    for needle in ("draft_finding", "submit_dossier", "APPROBATION demandée", "APPROBATION approve par eval-approver", "FIN completed", "contrôles :"):
        assert needle in text


# ================================================================ métriques
def test_journal_metrics_aggregate(runtime):
    run_ref(runtime)
    m = aggregate_journals(runtime.journal)
    assert m["tasks"] == 18 and m["by_status"]["budget_exceeded"] == 2 and m["pending_approvals"] == 0
    assert m["budget_exceeded"] == {"tool_calls": 1, "tokens": 1}
    assert m["error_recovery_rate"] == pytest.approx(1 / 3)  # S11 se rétablit ; S12 et S17 échouent par construction
    assert m["cost_usd_total"] > 0 and m["cost_usd_per_task_max"] >= m["cost_usd_per_task_mean"]


def test_eval_reports_recovery_and_clean_failures(runtime):
    agg = ev.aggregate(ev.run_all(runtime))
    assert agg["error_recovery_rate"] == 1.0 and agg["clean_failure_rate"] == 1.0 and agg["task_success_rate"] == 1.0


# ============================================================ API (phase 4)
KEYS = {"k-alice": {"id": "alice", "role": "lead_auditor"}, "k-bob": {"id": "bob", "role": "lead_auditor"},
        "k-carol": {"id": "carol", "role": "viewer"}}
H = lambda k: {"X-API-Key": k}  # noqa: E731


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(Runtime(tmp_path / "j"), KEYS))


def test_api_replay_timeline_and_metrics(client):
    t = client.post("/tasks", json={"task": "Soumets le dossier D-001 à audit@client.example.", "scopes": ["audit:read", "audit:submit"]},
                    headers=H("k-alice")).json()
    client.post(f"/tasks/{t['request_id']}/approvals/{t['pending_approval']['approval_id']}", json={"decision": "approve"}, headers=H("k-bob"))
    rid = t["request_id"]
    rep = client.get(f"/tasks/{rid}/replay", headers=H("k-alice")).json()
    assert rep["replayable"] and rep["identical"] and rep["chain_ok"]
    assert "FIN completed" in client.get(f"/tasks/{rid}/timeline", headers=H("k-alice")).text
    assert client.get(f"/tasks/{rid}/replay", headers=H("k-carol")).status_code == 404
    assert client.get("/metrics", headers=H("k-carol")).status_code == 403
    m = client.get("/metrics", headers=H("k-bob")).json()
    assert m["tasks"] == 1 and m["by_status"] == {"completed": 1}
