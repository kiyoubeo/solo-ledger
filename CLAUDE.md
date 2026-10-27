# CLAUDE.md — SoloLedger 작업 컨텍스트 및 하네스 규칙

## 개요

1인 법인 전용 회계·경비 원장 및 실시간 법인카드 세무증빙 자동화 시스템(`SoloLedger`) 개발 저장소.
- 설계 원본: https://share.gemini.google/M28P4cajbzfH (`docs/ARCHITECTURE_SPEC.md`)
- GitHub 저장소: https://github.com/kiyoubeo/solo-ledger.git (`main` 브랜치)

## 작업 및 하네스 엔지니어링 규칙

1. **하네스 검증 필수**: 모든 코드/문서 수정 후 `powershell -ExecutionPolicy Bypass -File scripts/harness.ps1 verify`를 통해 구문 검사(`compileall`), 정적 분석(`ruff`), DB 스키마 무결성, 자동화 테스트(`pytest`) 통과 여부를 확인한다.
2. **모든 개발 변경사항 로그 기록**: 변경사항 발생 시 반드시 아래 `[변경 이력]`과 `CHANGELOG.md`, `logs/dev_changes.jsonl`에 기록을 남긴다.
3. **항상 편집 이후 커밋·푸시**: 편집 및 검증이 끝나면 `powershell -ExecutionPolicy Bypass -File scripts/harness.ps1 commit-push -Message "변경 내용" -Category "feat"`를 실행하여 변경 로그 기록 + 커밋 + `origin/main` 푸시를 완료한다.

## 변경 이력 (Change Log)

| 날짜 | 파일 | 변경 내용 |
|------|------|-----------|
| 2026-10-27 | .agents/, .env.example, .githooks/, .gitignore (+13 files) | [feat] SoloLedger 초기 아키텍처, 코어 모듈(SMS 파서·세무 규칙·3단계 대사·FastAPI) 및 하네스 엔지니어링(자동 검증·변경 로깅·커밋/푸시 가드) 구축 |
