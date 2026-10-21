from acsi_agent import demo


def test_demo_runs_for_real_and_every_claim_holds(tmp_path):
    text, facts = demo.run(tmp_path / "j")
    assert facts.pop("all_ok") is True
    assert facts and all(facts.values()), facts
    for needle in ("SELF_APPROVAL", "TOOL_FORBIDDEN", "RECIPIENT_NOT_ALLOWED", "PERMISSION_DENIED", "budget_exceeded", "identique = True"):
        assert needle in text
