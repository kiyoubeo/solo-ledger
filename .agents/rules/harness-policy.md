# SoloLedger Harness Engineering Policy

1. **Always Verify Edits**: After creating or editing any code, SQL schema, or configuration file in `SoloLedger`, run `powershell -ExecutionPolicy Bypass -File scripts/harness.ps1 verify` to execute bytecode compilation, `ruff` static analysis, SQLite DDL schema validation, and `pytest` unit/integration tests.
2. **Always Log Development Changes**: Every development change must be recorded in `CHANGELOG.md`, `CLAUDE.md`, and `logs/dev_changes.jsonl`.
3. **Always Commit and Push After Editing**: Before completing any turn where workspace files were modified, run `powershell -ExecutionPolicy Bypass -File scripts/harness.ps1 commit-push -Message "<summary>" -Category "<feat|fix|refactor|test|docs|chore>"` so that verification passes, change logs are updated, and the commit is pushed to `origin/main` (`https://github.com/kiyoubeo/solo-ledger.git`).
