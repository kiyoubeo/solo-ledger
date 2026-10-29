---
name: sololedger-harness
description: >-
  Runbook for SoloLedger Harness Engineering. Use this skill whenever developing,
  modifying, testing, logging, or committing code in the SoloLedger repository to
  verify build/test health, record structured change logs, and commit/push changes.
---

# SoloLedger Harness Engineering Skill

Use `scripts/harness.ps1` to verify changes, record development logs, and commit/push to GitHub (`https://github.com/kiyoubeo/solo-ledger.git`).

## Standard Development Loop

0. **Review Development Plan (`review-plan`)**:
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts/harness.ps1 review-plan
   ```
   - Audits `docs/DEVELOPMENT_PLAN.md` and `docs/ARCHITECTURE_SPEC.md` against the codebase (`src/sololedger/`, `tests/`, registered FastAPI routes, and `sololedger.__version__`).

1. **Implement & Add Tests**:
   - Implement domain logic under `src/sololedger/` and SQL changes in `sql/schema.sql`.
   - Add or update unit/integration tests in `tests/`.
   - Update `docs/DEVELOPMENT_PLAN.md` when completing a Phase.

2. **Run 5-Stage Harness Verification**:
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts/harness.ps1 verify
   ```
   - Checks:
     1. `plan_review` (Plan-to-Code traceability: deliverables, test suites, FastAPI endpoints, version alignment)
     2. `compileall` (Python syntax & bytecode check)
     3. `ruff check src tests` (Static lint analysis)
     4. `schema_integrity` (SQLite DDL & seed rules verification)
     5. `pytest` (Unit & FastAPI integration test suite)
   - Inspect `logs/last_verification.json` if any check fails.

3. **Log, Commit, and Push (Atomic Execution)**:
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts/harness.ps1 commit-push -Message "구현 및 변경 내용 요약" -Category "feat"
   ```
   - Automatically runs `verify`, appends entries to `CHANGELOG.md`, `CLAUDE.md`, and `logs/dev_changes.jsonl`, creates the Git commit, and pushes to `origin main`.

4. **Check Harness Status**:
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts/harness.ps1 status
   ```
