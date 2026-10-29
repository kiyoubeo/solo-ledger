"""Development Plan & Architecture Spec Traceability Verifier for SoloLedger Harness."""

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from sololedger import __version__
from sololedger.api import app
from sololedger.config import PROJECT_ROOT

PHASE_TEST_MAPPING = {
    "Phase 1": [
        "tests/test_schema.py",
        "tests/test_sms_parser.py",
        "tests/test_tax_engine.py",
        "tests/test_reconciler.py",
        "tests/test_api_webhook.py",
    ],
    "Phase 2": ["tests/test_nts_client.py"],
    "Phase 3": ["tests/test_ocr_and_bot.py"],
    "Phase 4": ["tests/test_import_export.py"],
    "Phase 5": ["tests/test_e2e_lifecycle.py"],
}

REQUIRED_PLAN_SECTIONS = [
    "1. 개발 목표 및 핵심 원칙",
    "2. 전체 개발 단계(Phase) 로드맵",
    "3. 단계별 상세 개발 명세 및 검증 계획",
    "4. 디렉토리 및 모듈 확장 설계도",
    "5. 하네스 엔지니어링 완료 기준",
]


def _get_registered_api_routes() -> List[str]:
    """Return list of 'METHOD /path' strings registered in the FastAPI app."""
    routes = []
    for route in app.routes:
        methods = getattr(route, "methods", None)
        path = getattr(route, "path", None)
        if methods and path:
            for m in methods:
                routes.append(f"{m.upper()} {path}")
    return routes


def parse_development_plan(plan_path: Optional[Path] = None) -> Dict[str, Any]:
    """Parse docs/DEVELOPMENT_PLAN.md into structured phase roadmap and section metadata."""
    target_path = plan_path or (PROJECT_ROOT / "docs" / "DEVELOPMENT_PLAN.md")
    if not target_path.exists():
        raise FileNotFoundError(f"Development plan not found: {target_path}")

    content = target_path.read_text(encoding="utf-8")
    missing_sections = [s for s in REQUIRED_PLAN_SECTIONS if s not in content]

    # Parse roadmap table rows: | **Phase X** | `vX.Y.Z` | scope | deliverables | effort | status |
    row_pattern = re.compile(
        r"\|\s*\*\*(Phase\s+\d+)\*\*\s*\|\s*`v?([0-9.]+)`\s*\|\s*([^|]+)\|\s*([^|]+)\|\s*([^|]+)\|\s*([^|]+)\|"
    )
    phases = []
    for match in row_pattern.finditer(content):
        phase_name, version_str, scope, deliverables_raw, effort, status_raw = [
            g.strip() for g in match.groups()
        ]
        deliverables = [
            d.strip(" `")
            for d in re.findall(r"`([^`]+)`", deliverables_raw)
            if d.strip(" `")
        ]
        is_completed = "완료" in status_raw

        # Extract detailed section for this phase to inspect promised API endpoints
        sec_pattern = re.compile(
            rf"###\s+{re.escape(phase_name)}:[\s\S]*?(?=\n###\s+Phase\s+\d+:|\n##\s+4\.|\Z)"
        )
        sec_match = sec_pattern.search(content)
        sec_text = sec_match.group(0) if sec_match else ""
        endpoints = re.findall(
            r"`((?:GET|POST|PUT|DELETE|PATCH)\s+/[a-zA-Z0-9_\-/]+)", sec_text
        )

        phases.append(
            {
                "phase": phase_name,
                "version": version_str,
                "scope": scope,
                "deliverables": deliverables,
                "effort": effort,
                "status": status_raw,
                "is_completed": is_completed,
                "documented_endpoints": endpoints,
                "expected_tests": PHASE_TEST_MAPPING.get(phase_name, []),
            }
        )

    return {
        "path": str(target_path),
        "missing_sections": missing_sections,
        "phases": phases,
    }


def verify_plan_traceability(
    repo_root: Optional[Path] = None, plan_path: Optional[Path] = None
) -> Dict[str, Any]:
    """Verify that docs/DEVELOPMENT_PLAN.md is structurally valid and 100% consistent
    with implemented modules, tests, FastAPI routes, and package version.
    """
    root = repo_root or PROJECT_ROOT
    spec_path = root / "docs" / "ARCHITECTURE_SPEC.md"
    errors: List[str] = []

    if not spec_path.exists():
        errors.append("Missing architecture specification: docs/ARCHITECTURE_SPEC.md")

    parsed = parse_development_plan(plan_path or (root / "docs" / "DEVELOPMENT_PLAN.md"))
    if parsed["missing_sections"]:
        errors.append(
            f"Missing required sections in DEVELOPMENT_PLAN.md: {parsed['missing_sections']}"
        )

    phases = parsed["phases"]
    if len(phases) < 5:
        errors.append(f"Expected 5 phases in roadmap table, found {len(phases)}")

    registered_routes = set(_get_registered_api_routes())
    completed_phases = [p for p in phases if p["is_completed"]]
    upcoming_phases = [p for p in phases if not p["is_completed"]]

    for p in completed_phases:
        p_name = p["phase"]
        # 1. Check deliverable files exist
        for deliv in p["deliverables"]:
            candidate_paths = [
                root / deliv,
                root / "src" / "sololedger" / deliv,
            ]
            if not any(cp.exists() for cp in candidate_paths):
                errors.append(f"[{p_name}] Missing declared deliverable file: {deliv}")

        # 2. Check expected test files exist
        for test_rel in p["expected_tests"]:
            test_file = root / test_rel
            if not test_file.exists():
                errors.append(f"[{p_name}] Missing required test suite: {test_rel}")

        # 3. Check documented API endpoints are registered in FastAPI app
        for ep in p["documented_endpoints"]:
            if ep not in registered_routes:
                errors.append(
                    f"[{p_name}] Documented API endpoint '{ep}' is not registered in FastAPI app"
                )

    # 4. Verify package __version__ matches latest completed phase version
    if completed_phases:
        latest_phase = completed_phases[-1]["phase"]
        latest_completed_version = completed_phases[-1]["version"]
        if __version__ != latest_completed_version:
            errors.append(
                f"Version mismatch: sololedger.__version__ is '{__version__}', "
                f"but latest completed phase ({latest_phase}) in plan is '{latest_completed_version}'"
            )

    next_phase = upcoming_phases[0] if upcoming_phases else None

    return {
        "passed": len(errors) == 0,
        "package_version": __version__,
        "completed_phases": [p["phase"] for p in completed_phases],
        "upcoming_phases": [p["phase"] for p in upcoming_phases],
        "next_phase": next_phase,
        "registered_routes_count": len(registered_routes),
        "errors": errors,
    }
