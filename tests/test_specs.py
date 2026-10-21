import json

from acsi_agent import docs_gen, spec_check
from acsi_agent.specs import ROOT, load_policy, load_scenarios, load_tools


def test_specs_are_consistent():
    assert spec_check.check() == []


def test_docs_are_up_to_date():
    assert docs_gen.main(["--check"]) == 0


def test_every_irreversible_tool_requires_approval():
    for t in load_tools().values():
        if t.side_effect.startswith("irreversible"):
            assert t.requires_approval, t.name


def test_default_deny_and_least_privilege():
    p = load_policy()
    assert p["default"] == "deny"
    assert p["default_task_scopes"] == ["audit:read"]


def test_checker_detects_a_broken_spec(tmp_path, monkeypatch):
    """Le vérificateur doit échouer si on retire l'approbation d'un tool irréversible."""
    raw = json.loads((ROOT / "specs" / "tools.json").read_text())
    for t in raw["tools"]:
        if t["name"] == "submit_dossier":
            t["approval"] = "never"
    broken = tmp_path / "tools.json"
    broken.write_text(json.dumps(raw))
    monkeypatch.setattr(spec_check, "load_tools", lambda: load_tools(broken))
    errs = spec_check.check()
    assert any("irréversible sans approbation" in e for e in errs)


def test_scenarios_cover_all_categories():
    cats = {s["category"] for s in load_scenarios()}
    assert cats == {"nominal", "approval", "refusal", "error_recovery", "budget", "injection"}
