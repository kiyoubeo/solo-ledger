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
| 2026-10-27 | README.md, logs/last_verification.json, logs/verification_history.jsonl, docs/DEVELOPMENT_PLAN.md | [docs] SoloLedger 5단계(Phase 1~5) 마스터 개발 계획서(docs/DEVELOPMENT_PLAN.md) 작성 및 README 목차 갱신 |
| 2026-10-27 | .env.example, docs/DEVELOPMENT_PLAN.md, logs/last_verification.json, logs/verification_history.jsonl (+8 files) | [feat] Phase 2(v0.2.0) 국세청 사업자 상태조회 API 연동, 사업자번호 체크섬 검증 및 30일 TTL 캐시 엔진 구현 |
| 2026-10-29 | .env.example, .gitignore, docs/DEVELOPMENT_PLAN.md, logs/last_verification.json (+11 files) | [feat] Phase 3(v0.3.0) 텔레그램 법인전표봇 양방향 연동, 멀티모달 Vision OCR 및 SHA-256 정규화 아카이빙 구현 |
| 2026-10-29 | .agents/skills/sololedger-harness/SKILL.md, AGENTS.md, README.md, logs/last_verification.json (+4 files) | [feat] 하네스 엔지니어링에 개발 계획서 추적성 및 정합성 검토(review-plan / plan_verifier) 단계 추가 |
| 2026-10-29 | docs/DEVELOPMENT_PLAN.md, logs/last_verification.json, logs/verification_history.jsonl, src/sololedger/__init__.py (+8 files) | [feat] Phase 4(v0.4.0) 홈택스 CSV 배치 대사 파서, 업무용승용차 1500만원 한도 트래커 및 세무대리인 제출 패키지(CSV+ZIP) 생성기 구현 |
| 2026-10-29 | docs/DEVELOPMENT_PLAN.md, logs/last_verification.json, logs/verification_history.jsonl, src/sololedger/__init__.py (+7 files) | [feat] Phase 5(v1.0.0) 경량 웹 대시보드, 운영 CLI, Docker Compose 배포 구성 및 전 주기 E2E 통합 테스트 완성 |
| 2026-10-29 | logs/last_verification.json, logs/verification_history.jsonl, src/sololedger/ocr_engine.py, src/sololedger/plan_verifier.py (+4 files) | [test] 한국형 영수증 8대 변칙 케이스 골든 데이터셋(receipt_dataset.json) 구축 및 복합과세·번호노이즈·품목분류 엔진 고도화 |
| 2026-10-29 | logs/last_verification.json, logs/verification_history.jsonl | [chore] 전 주기 통합 검증(하네스 5단계, 실서버 HTTP/대시보드, CLI 및 세무패키지 CSV+ZIP) 실행 완료 기록 |
| 2026-10-29 | docs/DEVELOPMENT_PLAN.md, logs/last_verification.json, logs/verification_history.jsonl, src/sololedger/api.py (+7 files) | [feat] ipDISK NAS(Z:\HDD1\Data\SoloLedger) 원장 온라인 백업 및 증빙 아카이브 자동 동기화(nas_sync.py) 구현 |
| 2026-10-29 | .gitignore, logs/last_verification.json, logs/verification_history.jsonl, tests/test_nas_sync.py (+2 files) | [feat] 중고 노트북·미니PC 24시간 무인 서버 자동 시작 및 일일 NAS 백업 프로비저닝 스크립트(server_service.ps1) 및 가이드 추가 |
| 2026-10-29 | .env.example, .gitignore, logs/last_verification.json, logs/verification_history.jsonl (+5 files) | [feat] 테스트 전용 샌드박스 환경(.env test 모드, sololeger_test.db, SoloLedger_Test NAS 분리) 및 샘플 시드·초기화 기능 추가 |
