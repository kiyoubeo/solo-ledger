"""Unit tests for Development Plan & Architecture Spec Traceability Verifier."""

from sololedger.plan_verifier import parse_development_plan, verify_plan_traceability


def test_parse_development_plan_structure():
    parsed = parse_development_plan()
    assert parsed["missing_sections"] == []
    assert len(parsed["phases"]) == 5
    assert parsed["phases"][0]["phase"] == "Phase 1"
    assert parsed["phases"][0]["is_completed"] is True


def test_verify_plan_traceability_passes_on_current_repo():
    report = verify_plan_traceability()
    assert report["passed"] is True, f"Plan traceability errors: {report['errors']}"
    assert "Phase 1" in report["completed_phases"]
    assert "Phase 2" in report["completed_phases"]
    assert "Phase 3" in report["completed_phases"]
    assert "Phase 4" in report["completed_phases"]


def test_verify_plan_detects_missing_deliverable(tmp_path):
    # Create a fake plan claiming a non-existent deliverable `missing_module_xyz.py` is required in Phase 1
    real_plan = parse_development_plan()["path"]
    with open(real_plan, "r", encoding="utf-8") as f:
        text = f.read()

    old_str = "`db.py`, `sms_parser.py`"
    new_str = "`db.py`, `missing_module_xyz.py`, `sms_parser.py`"
    tampered = text.replace(old_str, new_str)
    fake_plan_file = tmp_path / "DEVELOPMENT_PLAN.md"
    fake_plan_file.write_text(tampered, encoding="utf-8")

    report = verify_plan_traceability(plan_path=fake_plan_file)
    assert report["passed"] is False
    assert any("missing_module_xyz.py" in err for err in report["errors"])
