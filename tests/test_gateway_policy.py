import pytest

from acsi_agent.backends import Services
from acsi_agent.gateway import Gateway
from acsi_agent.ratelimit import RateLimiter
from acsi_agent.specs import load_policy, load_tools
from acsi_agent.tools_impl import build_impls
from acsi_agent.types import CallContext

ALL = ("audit:read", "audit:draft", "audit:submit", "audit:delete")


def ctx(*scopes, key="k"):
    return CallContext(request_id="req_" + "1" * 32, step=1, scopes=scopes or ALL, key_id=key)


def gw(limiter=None):
    return Gateway(load_tools(), load_policy(), build_impls(Services.from_fixtures()), limiter=limiter)


SUBMIT = {"dossier_id": "D-001", "recipient": "audit@client.example"}


def test_forbidden_beats_permission_and_validation():
    d = gw().evaluate(ctx(*ALL), "shell_exec", {"command": "id"})
    assert d.code == "TOOL_FORBIDDEN"


def test_missing_scope_is_denied_before_argument_validation():
    d = gw().evaluate(ctx("audit:read"), "draft_finding", {"garbage": True})
    assert d.code == "PERMISSION_DENIED"


def test_least_privilege_read_scope_cannot_write():
    g = gw()
    for tool, args in [("draft_finding", {"dossier_id": "D-001", "nc_id": "NC-011", "text": "x" * 20}),
                       ("delete_draft", {"draft_id": "DR-0001"}), ("submit_dossier", SUBMIT)]:
        assert g.evaluate(ctx("audit:read"), tool, args).code == "PERMISSION_DENIED"


@pytest.mark.parametrize("recipient", [
    "fuite@evil.example",
    "audit@client.example.evil.com",
    "audit@sub.client.example",
    "a@evil.com@client.example",          # double @ : ambiguïté d'analyse
    "audit@client.example,boss@evil.com",  # destinataires multiples
    "audit@client.example;boss@evil.com",
    "audit@client.example boss@evil.com",
])
def test_recipient_outside_allowlist_is_denied_never_sent_for_approval(recipient):
    d = gw().evaluate(ctx(), "submit_dossier", {"dossier_id": "D-001", "recipient": recipient})
    assert d.verdict == "deny" and d.code == "RECIPIENT_NOT_ALLOWED"


def test_allowed_recipient_is_case_insensitive_and_needs_approval():
    d = gw().evaluate(ctx(), "submit_dossier", {"dossier_id": "D-001", "recipient": "Audit@CLIENT.example"})
    assert d.verdict == "needs_approval"


def test_approval_is_the_last_check_and_approved_flag_allows():
    g = gw()
    d = g.evaluate(ctx(), "delete_draft", {"draft_id": "DR-0001"})
    assert d.verdict == "needs_approval" and d.checks[-1] == "approval:required"
    assert g.evaluate(ctx(), "delete_draft", {"draft_id": "DR-0001"}, approved=True).verdict == "allow"


def test_approved_flag_does_not_bypass_other_checks():
    d = gw().evaluate(ctx("audit:read"), "submit_dossier", SUBMIT, approved=True)
    assert d.code == "PERMISSION_DENIED"
    d = gw().evaluate(ctx(), "submit_dossier", {"dossier_id": "D-001", "recipient": "x@evil.example"}, approved=True)
    assert d.code == "RECIPIENT_NOT_ALLOWED"


def test_per_task_call_cap():
    d = gw().evaluate(ctx(), "submit_dossier", SUBMIT, call_counts={"submit_dossier": 1})
    assert d.code == "CALL_LIMIT_EXCEEDED"


def test_rate_limit_per_key_with_refill():
    t = [0.0]
    lim = RateLimiter(2, clock=lambda: t[0])  # 2 appels / minute
    g = gw(lim)
    args = {"regulation": "RGPD", "article": "5"}
    for _ in range(2):
        assert g.evaluate(ctx(key="a"), "lookup_article", args).verdict == "allow"
        g.execute(ctx(key="a"), "lookup_article", args)
    assert g.evaluate(ctx(key="a"), "lookup_article", args).code == "RATE_LIMITED"
    assert g.evaluate(ctx(key="b"), "lookup_article", args).verdict == "allow"  # autre clé : autre seau
    t[0] = 31.0  # 1 jeton regagné (2/min = 1 toutes les 30 s)
    assert g.evaluate(ctx(key="a"), "lookup_article", args).verdict == "allow"


def test_checks_trail_is_recorded():
    d = gw().evaluate(ctx(), "lookup_article", {"regulation": "RGPD", "article": "5"})
    assert d.checks == ["allowlist:pass", "permission:pass", "schema:pass", "argument_rules:pass",
                        "call_cap:pass", "rate_limit:pass", "approval:not_required"]
