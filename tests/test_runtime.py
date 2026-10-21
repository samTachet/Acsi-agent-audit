import socket
import time

import pytest

from acsi_agent import eval as ev
from acsi_agent.agent import Runtime, TaskInput
from acsi_agent.backends import Services
from acsi_agent.gateway import Gateway
from acsi_agent.journal import Journal
from acsi_agent.planners import ScriptedPlanner
from acsi_agent.specs import load_policy, load_scenarios, load_tools
from acsi_agent.tools_impl import ToolError, build_impls, classify
from acsi_agent.types import CallContext, new_request_id

CTX = CallContext(request_id="req_" + "0" * 32, step=1, scopes=("audit:read",))


# ------------------------------------------------------------------ tools
def test_classify_detects_bias_documentation_gap():
    r = classify("Le système de recrutement par IA n'a fait l'objet d'aucune documentation sur les biais.")
    assert (r["regulation"], r["article"], r["severity"]) == ("AI_ACT", "10", "high")


def test_classify_unknown_text_returns_none():
    assert classify("Le ciel est bleu aujourd'hui sur toute la région.")["regulation"] == "NONE"


def test_lookup_missing_article_raises_declared_error():
    impls = build_impls(Services.from_fixtures())
    with pytest.raises(ToolError) as e:
        impls["lookup_article"]({"regulation": "AI_ACT", "article": "99"}, CTX)
    assert e.value.code == "ARTICLE_NOT_FOUND"


def test_every_error_code_raised_is_declared_in_spec():
    """Les codes que les tools peuvent lever doivent figurer dans la spec."""
    import inspect, re
    from acsi_agent import tools_impl
    raised = set(re.findall(r'ToolError\("([A-Z_]+)"', inspect.getsource(tools_impl)))
    declared = {e["code"] for t in load_tools().values() for e in t.errors}
    assert raised <= declared, raised - declared


def test_draft_ids_are_deterministic_per_request_and_step():
    a, b = Services.from_fixtures(), Services.from_fixtures()
    args = {"dossier_id": "D-001", "nc_id": "NC-011", "text": "Constat de test suffisamment long."}
    ra = build_impls(a)["draft_finding"](args, CTX)
    rb = build_impls(b)["draft_finding"](args, CTX)
    assert ra["draft_id"] == rb["draft_id"]


# ---------------------------------------------------------------- journal
def test_journal_chain_detects_tampering(tmp_path):
    j = Journal(tmp_path)
    rid = new_request_id()
    j.append(rid, "a", {"x": 1})
    j.append(rid, "b", {"x": 2})
    assert j.verify(rid) == (True, "ok")
    p = j.path(rid)
    p.write_text(p.read_text().replace('"x":1', '"x":9'))
    ok, msg = j.verify(rid)
    assert not ok and "altéré" in msg


@pytest.mark.parametrize("bad", ["../etc/passwd", "req_1", "req_" + "g" * 32, "", "req_" + "0" * 32 + "/x"])
def test_journal_rejects_path_traversal_ids(tmp_path, bad):
    with pytest.raises(ValueError):
        Journal(tmp_path).path(bad)


# ---------------------------------------------------------------- gateway
def _gateway(impls=None, timeouts=None):
    svc = Services.from_fixtures()
    return Gateway(load_tools(), load_policy(), impls or build_impls(svc), timeouts)


def test_gateway_denies_unknown_and_forbidden_tools():
    gw = _gateway()
    assert gw.evaluate(CTX, "shell_exec", {}).code == "TOOL_FORBIDDEN"
    assert gw.evaluate(CTX, "inconnu", {}).code == "TOOL_NOT_ALLOWED"


@pytest.mark.parametrize("args", [
    {"regulation": "RGPD", "article": "abc"},
    {"regulation": "RGPD"},
    {"regulation": "RGPD", "article": "5", "extra": 1},
    {"regulation": "CCPA", "article": "5"},
])
def test_gateway_rejects_invalid_arguments(args):
    d = _gateway().evaluate(CTX, "lookup_article", args)
    assert d.verdict == "deny" and d.code == "INVALID_ARGUMENTS"


def test_gateway_fails_closed_on_approval_tools_in_phase_2():
    d = _gateway().evaluate(CTX, "submit_dossier", {"dossier_id": "D-001", "recipient": "audit@client.example"})
    assert d.verdict == "deny"


def test_gateway_timeout_marks_write_outcome_unknown():
    def slow(args, ctx):
        time.sleep(0.3)
        return {"draft_id": "DR-aaaa", "dossier_id": "D-001", "nc_id": "NC-011", "status": "draft"}
    gw = _gateway({"draft_finding": slow}, {"draft_finding": 0.05})
    out = gw.execute(CTX, "draft_finding", {})
    assert out.error["code"] == "TIMEOUT" and out.error["outcome_unknown"] is True


def test_gateway_hides_internal_exception_and_checks_output_contract():
    def boom(args, ctx):
        raise RuntimeError("secret interne")
    def bad(args, ctx):
        return {"regulation": "RGPD"}
    gw = _gateway({"lookup_article": boom, "classify_excerpt": bad})
    e1 = gw.execute(CTX, "lookup_article", {}).error
    assert e1["code"] == "INTERNAL_ERROR" and "secret" not in e1["message"]
    assert gw.execute(CTX, "classify_excerpt", {}).error["code"] == "INTERNAL_ERROR"


# ---------------------------------------------------------------- runtime
def test_every_tool_call_is_journaled_with_tool_args_result_error(runtime):
    sc = next(s for s in load_scenarios() if s["id"] == "S12_tool_error_not_found")
    res = runtime.run(ev.to_task_input(sc))
    calls = [e for e in res.events if e["type"] == "tool_call"]
    assert len(calls) == 1
    d = calls[0]["data"]
    assert d["tool"] == "list_nonconformities" and d["args"] == {"dossier_id": "D-999"}
    assert d["status"] == "error" and d["error"]["code"] == "DOSSIER_NOT_FOUND" and d["result"] is None
    assert [e["type"] for e in res.events] == ["task_started", "planner_output", "tool_call", "planner_output", "task_finished"]
    assert runtime.journal.verify(res.request_id) == (True, "ok")


def test_denied_calls_are_journaled_too(runtime):
    sc = next(s for s in load_scenarios() if s["id"] == "S08_refuse_shell")
    res = runtime.run(ev.to_task_input(sc))
    d = next(e["data"] for e in res.events if e["type"] == "tool_call")
    assert d["status"] == "denied" and d["error"]["code"] == "TOOL_FORBIDDEN" and d["gateway"]["verdict"] == "deny"


def test_usage_is_recorded_per_planner_call(runtime):
    sc = next(s for s in load_scenarios() if s["id"] == "S02_list_and_lookup")
    res = runtime.run(ev.to_task_input(sc))
    outs = [e["data"] for e in res.events if e["type"] == "planner_output"]
    assert len(outs) == 3  # 2 tool calls + finish
    assert all(o["usage"]["tokens_in"] > 0 and o["usage"]["cost_usd"] > 0 for o in outs)
    assert res.metrics["tokens"] == sum(o["usage"]["tokens_in"] + o["usage"]["tokens_out"] for o in outs)


# ------------------------------------------------------------------- eval
def _phase2_scenarios():
    return [s for s in load_scenarios() if s.get("min_phase", 2) <= 2]


@pytest.mark.parametrize("sc", _phase2_scenarios(), ids=lambda s: s["id"])
def test_reference_scenarios_pass(sc, runtime):
    rep = ev.run_scenario(sc, runtime)
    assert rep.success, rep.issues


def test_eval_detects_wrong_tool(runtime):
    sc = next(s for s in load_scenarios() if s["id"] == "S01_classify")
    wrong = ScriptedPlanner([{"tool": "lookup_article", "args": {"regulation": "RGPD", "article": "5"}},
                             {"finish": {"status": "completed", "answer": "x"}}])
    rep = ev.score(sc, runtime.run(ev.to_task_input(sc), planner=wrong))
    assert not rep.tool_correct and not rep.success


def test_eval_detects_wrong_arguments_and_wrong_result(runtime):
    sc = next(s for s in load_scenarios() if s["id"] == "S02_list_and_lookup")
    bad = ScriptedPlanner([{"tool": "list_nonconformities", "args": {"dossier_id": "D-001"}},
                           {"tool": "lookup_article", "args": {"regulation": "AI_ACT", "article": "9"}},
                           {"finish": {"status": "completed", "answer": "x"}}])
    rep = ev.score(sc, runtime.run(ev.to_task_input(sc), planner=bad))
    assert rep.selection_ok and not rep.args_ok and not rep.tool_correct


def test_eval_detects_unexpected_extra_call(runtime):
    sc = next(s for s in load_scenarios() if s["id"] == "S18_out_of_scope_question")
    p = ScriptedPlanner([{"tool": "lookup_article", "args": {"regulation": "RGPD", "article": "5"}},
                         {"finish": {"status": "refused", "answer": "x"}}])
    rep = ev.score(sc, runtime.run(ev.to_task_input(sc), planner=p))
    assert rep.extra_calls == ["lookup_article"] and not rep.success


def test_mocks_make_no_network_call(runtime, monkeypatch):
    def no_net(*a, **k):
        raise AssertionError("appel réseau interdit pendant l'évaluation")
    monkeypatch.setattr(socket.socket, "connect", no_net)
    monkeypatch.setattr(socket, "create_connection", no_net)
    reports = ev.run_all(runtime)
    assert all(r.success for r in reports if not r.skipped)
