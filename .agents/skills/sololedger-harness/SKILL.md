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

1. **Implement & Add Tests**:
   - Implement domain logic under `src/sololedger/` and SQL changes in `sql/schema.sql`.
   - Add or update unit/integration tests in `tests/`.

2. **Run Harness Verification**:
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts/harness.ps1 verify
   ```
   - Checks:
     1. `compileall` (Python syntax & bytecode check)
     2. `ruff check src tests` (Static lint analysis)
     3. `schema_integrity` (SQLite DDL & seed rules verification)
     4. `pytest` (Unit & FastAPI integration test suite)
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
