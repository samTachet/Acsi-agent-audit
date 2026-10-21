import re

import pytest
from fastapi.testclient import TestClient

from acsi_agent.agent import Runtime
from acsi_agent.api import create_app

KEYS = {
    "k-alice": {"id": "alice", "role": "lead_auditor"},
    "k-bob": {"id": "bob", "role": "lead_auditor"},
    "k-carol": {"id": "carol", "role": "viewer"},
    "k-dave": {"id": "dave", "role": "auditor"},
}
H = lambda k: {"X-API-Key": k}  # noqa: E731


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(Runtime(tmp_path / "j"), KEYS))


def create(client, key, task, **extra):
    return client.post("/tasks", json={"task": task, **extra}, headers=H(key))


SUBMIT = "Soumets le dossier D-001 à audit@client.example."


def test_authentication_required_and_error_shape(client):
    assert client.get("/healthz").status_code == 200
    for headers in ({}, H("nope")):
        r = client.post("/tasks", json={"task": "Classifie « chiffrement absent »"}, headers=headers)
        assert r.status_code == 401 and r.json()["error"]["code"] == "UNAUTHENTICATED"


def test_no_configured_keys_means_everything_refused(tmp_path):
    c = TestClient(create_app(Runtime(tmp_path / "j"), {}))
    assert create(c, "anything", "Classifie « chiffrement absent »").status_code == 401


def test_every_task_gets_a_request_id_and_a_journal(client):
    r = create(client, "k-carol", "Classifie l'extrait suivant : « Les sauvegardes ne font l'objet d'aucun chiffrement. »")
    body = r.json()
    assert r.status_code == 201 and re.fullmatch(r"req_[0-9a-f]{32}", body["request_id"])
    assert body["status"] == "completed" and body["metrics"]["tokens"] > 0 and body["steps"][0]["tool"] == "classify_excerpt"
    traj = client.get(f"/tasks/{body['request_id']}/trajectory", headers=H("k-carol")).json()
    assert traj["chain_ok"] and traj["events"][0]["type"] == "task_started" and traj["events"][-1]["type"] == "task_finished"


def test_default_scope_is_read_only_least_privilege(client):
    r = create(client, "k-dave", "Rédige un brouillon de constat pour la non-conformité NC-011 du dossier D-001.").json()
    assert r["status"] == "refused" and r["steps"][-1]["code"] == "PERMISSION_DENIED" and r["effects"]["drafts_created"] == 0


def test_role_caps_requested_scopes(client):
    r = create(client, "k-carol", "Classifie « chiffrement absent des sauvegardes »", scopes=["audit:read", "audit:submit"])
    assert r.status_code == 403 and r.json()["error"]["code"] == "SCOPE_NOT_GRANTED"


def test_approval_scope_is_not_a_task_scope(client):
    r = create(client, "k-alice", "Classifie « chiffrement absent des sauvegardes »", scopes=["audit:read", "audit:approve"])
    assert r.status_code == 422


def test_full_human_approval_flow(client):
    t = create(client, "k-alice", SUBMIT, scopes=["audit:read", "audit:submit"]).json()
    assert t["status"] == "awaiting_approval" and t["effects"]["mail_sent"] == 0
    rid, aid = t["request_id"], t["pending_approval"]["approval_id"]
    url = f"/tasks/{rid}/approvals/{aid}"

    r = client.post(url, json={"decision": "approve"}, headers=H("k-alice"))  # le demandeur ne peut pas s'auto-approuver
    assert r.status_code == 403 and r.json()["error"]["code"] == "SELF_APPROVAL"
    assert client.get(f"/tasks/{rid}", headers=H("k-alice")).json()["status"] == "awaiting_approval"

    r = client.post(url, json={"decision": "approve"}, headers=H("k-bob"))
    assert r.status_code == 200 and r.json()["status"] == "completed" and r.json()["effects"]["mail_sent"] == 1
    assert client.post(url, json={"decision": "approve"}, headers=H("k-bob")).status_code == 409  # usage unique

    traj = client.get(f"/tasks/{rid}/trajectory", headers=H("k-alice")).json()
    kinds = [e["type"] for e in traj["events"]]
    assert traj["chain_ok"] and "approval_requested" in kinds and "approval_resolved" in kinds
    assert next(e for e in traj["events"] if e["type"] == "approval_resolved")["data"]["approver"] == "bob"


def test_approver_cannot_alter_the_arguments(client):
    t = create(client, "k-alice", SUBMIT, scopes=["audit:read", "audit:submit"]).json()
    url = f"/tasks/{t['request_id']}/approvals/{t['pending_approval']['approval_id']}"
    client.post(url, json={"decision": "approve", "args": {"recipient": "x@evil.example"}}, headers=H("k-bob"))
    sent = client.app.state.runtime.journal.read(t["request_id"])
    call = next(e["data"] for e in sent if e["type"] == "tool_call")
    assert call["args"]["recipient"] == "audit@client.example"


def test_viewer_cannot_approve_and_cannot_even_see_others_tasks(client):
    t = create(client, "k-alice", SUBMIT, scopes=["audit:read", "audit:submit"]).json()
    rid, aid = t["request_id"], t["pending_approval"]["approval_id"]
    assert client.get(f"/tasks/{rid}", headers=H("k-carol")).status_code == 404
    assert client.post(f"/tasks/{rid}/approvals/{aid}", json={"decision": "approve"}, headers=H("k-carol")).status_code == 404
    assert client.get(f"/tasks/{rid}", headers=H("k-bob")).status_code == 200  # un approbateur voit la file


def test_rejection_via_api(client):
    t = create(client, "k-alice", SUBMIT, scopes=["audit:read", "audit:submit"]).json()
    r = client.post(f"/tasks/{t['request_id']}/approvals/{t['pending_approval']['approval_id']}",
                    json={"decision": "reject"}, headers=H("k-bob")).json()
    assert r["status"] == "refused" and r["effects"]["mail_sent"] == 0 and r["steps"][0]["code"] == "APPROVAL_REJECTED"


@pytest.mark.parametrize("rid", ["../../etc/passwd", "req_1", "req_" + "z" * 32, "x" * 300])
def test_malformed_request_ids_are_404(client, rid):
    assert client.get(f"/tasks/{rid}", headers=H("k-alice")).status_code in (404, 422)
    assert client.get(f"/tasks/{rid}/trajectory", headers=H("k-alice")).status_code in (404, 422)


def test_task_rate_limit(client):
    codes = [create(client, "k-carol", "Classifie « chiffrement absent des sauvegardes »").status_code for _ in range(12)]
    assert codes[:10] == [201] * 10 and codes[10:] == [429, 429]


def test_budget_override_validation_and_enforcement(client):
    assert create(client, "k-alice", "Quelle est la météo ?", budget={"max_tokens": -1}).status_code == 422
    assert create(client, "k-alice", "Quelle est la météo ?", budget={"inconnu": 1}).status_code == 422
    r = create(client, "k-alice", "Classifie « chiffrement absent des sauvegardes »", budget={"max_tokens": 500}).json()
    assert r["status"] == "budget_exceeded" and r["metrics"]["budget_dimension"] == "tokens"


def test_client_cannot_choose_the_planner_or_inject_faults(client):
    r = client.post("/tasks", json={"task": "Quelle est la météo ?", "planner": {"type": "runaway"}, "faults": {"mail": "x"}},
                    headers=H("k-alice"))
    started = client.app.state.runtime.journal.read(r.json()["request_id"])[0]["data"]
    assert started["planner"] == {"type": "rule"} and started["faults"] == {}


def test_task_text_is_bounded(client):
    assert create(client, "k-alice", "x" * 2001).status_code == 422
    assert create(client, "k-alice", "x").status_code == 422
