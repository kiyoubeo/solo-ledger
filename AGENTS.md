# AGENTS.md — SoloLedger 에이전트 하네스 엔지니어링 규칙

## 1. 프로젝트 정체성 및 네임스페이스 기준

- **프로젝트명**: `SoloLedger` (1인 법인 전용 회계·경비 원장 및 세무증빙 자동화 시스템)
- **Git 원격 저장소**: `https://github.com/kiyoubeo/solo-ledger.git` (`main` 브랜치)
- **설계 기준 문서**: [docs/ARCHITECTURE_SPEC.md](./docs/ARCHITECTURE_SPEC.md) (원본: `https://share.gemini.google/M28P4cajbzfH`)
- **표준 식별자**:
  - Python 패키지: `sololedger` (`src/sololedger/`)
  - 기본 SQLite DB: `sololedger_prod.db` (`SOLOLEDGER_DB_PATH`)
  - 인증 헤더 / 환경변수: `SOLOLEDGER_API_KEY` (또는 `X-API-Key`)
  - 텔레그램 봇: `법인전표봇 (@SoloLedgerBot)`

---

## 2. 필수 하네스 엔지니어링 수칙 (절대 준수)

이 작업 공간에서 코드를 생성, 수정, 리팩토링하거나 문서를 변경하는 모든 에이전트는 **반드시** 아래 3단계 프로토콜을 준수해야 합니다.

### 수칙 1: 개발 계획서 사전 검토 (`review-plan`) 및 5단계 하네스 검증 (`verify`)
- 새로운 Phase 또는 기능 개발을 시작하기 전, 개발 계획서([docs/DEVELOPMENT_PLAN.md](./docs/DEVELOPMENT_PLAN.md))의 목표 산출물·API 엔드포인트·테스트 대상을 검토합니다:
  ```powershell
  powershell -ExecutionPolicy Bypass -File scripts/harness.ps1 review-plan
  ```
- 파일 편집 완료 후 반드시 5단계 하네스 검증 파이프라인을 실행하여 정합성을 확인합니다:
  ```powershell
  powershell -ExecutionPolicy Bypass -File scripts/harness.ps1 verify
  ```
- 새로운 기능을 추가하거나 버그를 수정할 때는 반드시 `tests/` 하위에 해당 동작을 검증하는 `pytest` 테스트 케이스를 함께 추가해야 합니다.
- 검증 파이프라인(`plan_review` → `compileall` → `ruff check` → `schema_integrity` → `pytest`)의 모든 5단계가 `PASSED`여야만 다음 단계로 진행할 수 있습니다.

### 수칙 2: 모든 개발 변경사항 로그 기록 (`log`)
- 모든 개발 변경사항은 다음 3곳에 동기화되어 기록되어야 합니다:
  1. `CHANGELOG.md` (변경 이력 마스터 테이블)
  2. `CLAUDE.md` (변경 이력 섹션)
  3. `logs/dev_changes.jsonl` (타임스탬프, 카테고리, 변경 파일 목록, 검증 상태가 포함된 구조화 JSONL 로그)

### 수칙 3: 편집 이후 항상 커밋 및 푸시 (`commit-push`)
- 파일 편집이 발생한 작업 턴을 종료하기 전에는 **항상** 검증, 로그 기록, Git 커밋, 원격 저장소(`origin main`) 푸시를 완료해야 합니다.
- 아래 단일 명령어로 검증 + 로그 기록 + 커밋 + 푸시를 원자적(Atomic)으로 수행할 수 있습니다:
  ```powershell
  powershell -ExecutionPolicy Bypass -File scripts/harness.ps1 commit-push -Message "변경 내용 요약" -Category "feat"
  ```
  *(Category 값 예시: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`)*
- 작업 종료 시점에 `git status --porcelain`은 비어 있어야 하며(Working Tree Clean), 로컬 `main` 브랜치와 `origin/main`이 완전히 동기화되어 있어야 합니다.
