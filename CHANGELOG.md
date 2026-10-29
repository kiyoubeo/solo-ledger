# CHANGELOG — SoloLedger 개발 변경 이력

모든 개발 변경사항은 하네스 엔지니어링 파이프라인(`scripts/harness.ps1`)을 통해 검증된 후 이 문서와 `logs/dev_changes.jsonl`에 자동 기록됩니다.

| 날짜 | 분류 | 변경 파일 | 변경 내용 요약 | 검증 상태 |
|------|------|-----------|----------------|-----------|
| 2026-10-27 | `feat` | `.agents/, .env.example, .githooks/, .gitignore (+13 files)` | SoloLedger 초기 아키텍처, 코어 모듈(SMS 파서·세무 규칙·3단계 대사·FastAPI) 및 하네스 엔지니어링(자동 검증·변경 로깅·커밋/푸시 가드) 구축 | `PASSED` |
| 2026-10-27 | `docs` | `README.md, logs/last_verification.json, logs/verification_history.jsonl, docs/DEVELOPMENT_PLAN.md` | SoloLedger 5단계(Phase 1~5) 마스터 개발 계획서(docs/DEVELOPMENT_PLAN.md) 작성 및 README 목차 갱신 | `PASSED` |
| 2026-10-27 | `feat` | `.env.example, docs/DEVELOPMENT_PLAN.md, logs/last_verification.json, logs/verification_history.jsonl (+8 files)` | Phase 2(v0.2.0) 국세청 사업자 상태조회 API 연동, 사업자번호 체크섬 검증 및 30일 TTL 캐시 엔진 구현 | `PASSED` |
| 2026-10-29 | `feat` | `.env.example, .gitignore, docs/DEVELOPMENT_PLAN.md, logs/last_verification.json (+11 files)` | Phase 3(v0.3.0) 텔레그램 법인전표봇 양방향 연동, 멀티모달 Vision OCR 및 SHA-256 정규화 아카이빙 구현 | `PASSED` |
| 2026-10-29 | `feat` | `.agents/skills/sololedger-harness/SKILL.md, AGENTS.md, README.md, logs/last_verification.json (+4 files)` | 하네스 엔지니어링에 개발 계획서 추적성 및 정합성 검토(review-plan / plan_verifier) 단계 추가 | `PASSED` |
| 2026-10-29 | `feat` | `docs/DEVELOPMENT_PLAN.md, logs/last_verification.json, logs/verification_history.jsonl, src/sololedger/__init__.py (+8 files)` | Phase 4(v0.4.0) 홈택스 CSV 배치 대사 파서, 업무용승용차 1500만원 한도 트래커 및 세무대리인 제출 패키지(CSV+ZIP) 생성기 구현 | `PASSED` |
| 2026-10-29 | `feat` | `docs/DEVELOPMENT_PLAN.md, logs/last_verification.json, logs/verification_history.jsonl, src/sololedger/__init__.py (+7 files)` | Phase 5(v1.0.0) 경량 웹 대시보드, 운영 CLI, Docker Compose 배포 구성 및 전 주기 E2E 통합 테스트 완성 | `PASSED` |
| 2026-10-29 | `test` | `logs/last_verification.json, logs/verification_history.jsonl, src/sololedger/ocr_engine.py, src/sololedger/plan_verifier.py (+4 files)` | 한국형 영수증 8대 변칙 케이스 골든 데이터셋(receipt_dataset.json) 구축 및 복합과세·번호노이즈·품목분류 엔진 고도화 | `PASSED` |
| 2026-10-29 | `chore` | `logs/last_verification.json, logs/verification_history.jsonl` | 전 주기 통합 검증(하네스 5단계, 실서버 HTTP/대시보드, CLI 및 세무패키지 CSV+ZIP) 실행 완료 기록 | `PASSED` |
| 2026-10-29 | `feat` | `docs/DEVELOPMENT_PLAN.md, logs/last_verification.json, logs/verification_history.jsonl, src/sololedger/api.py (+7 files)` | ipDISK NAS(Z:\HDD1\Data\SoloLedger) 원장 온라인 백업 및 증빙 아카이브 자동 동기화(nas_sync.py) 구현 | `PASSED` |
| 2026-10-29 | `feat` | `.gitignore, logs/last_verification.json, logs/verification_history.jsonl, tests/test_nas_sync.py (+2 files)` | 중고 노트북·미니PC 24시간 무인 서버 자동 시작 및 일일 NAS 백업 프로비저닝 스크립트(server_service.ps1) 및 가이드 추가 | `PASSED` |
| 2026-10-29 | `feat` | `.env.example, .gitignore, logs/last_verification.json, logs/verification_history.jsonl (+5 files)` | 테스트 전용 샌드박스 환경(.env test 모드, sololeger_test.db, SoloLedger_Test NAS 분리) 및 샘플 시드·초기화 기능 추가 | `PASSED` |
| 2026-10-29 | `docs` | `README.md, logs/last_verification.json, logs/verification_history.jsonl, tests/test_nas_sync.py (+1 files)` | SoloLedger(v1.0.0) 사용자 사용 매뉴얼 가이드(docs/USER_MANUAL.md) 작성 및 테스트 검증 반영 | `PASSED` |
| 2026-10-29 | `docs` | `README.md, logs/last_verification.json, logs/verification_history.jsonl, src/sololedger/api.py (+3 files)` | HTML 사용자 매뉴얼(docs/USER_MANUAL.html) 및 웹 대시보드 매뉴얼 뷰어(/manual) 추가 | `PASSED` |
| 2026-10-29 | `feat` | `logs/last_verification.json, logs/verification_history.jsonl, sql/schema.sql, src/sololedger/api.py (+5 files)` | 갤럭시 S26+ 실기기 USB ADB 카드 문자 추출기(phone_adb.py) 및 7개 카드사 실제 문자 파싱 고도화 | `PASSED` |
| 2026-10-29 | `feat` | `logs/last_verification.json, logs/verification_history.jsonl, src/sololedger/api.py, src/sololedger/archiver.py (+5 files)` | 카드별 전표·영수증 분류 현황 대시보드, 카드별 필터링 및 by_card/ 보관 폴더 자동 분류 구현 | `PASSED` |
| 2026-10-29 | `feat` | `logs/last_verification.json, logs/verification_history.jsonl, src/sololedger/dashboard.py, src/sololedger/phone_adb.py (+2 files)` | 갤럭시 RCS(채팅+ content://im/chat) 오픈리치카드 메시지 통합 추출 및 2026년 전 기간 연속 조회 지원 | `PASSED` |
| 2026-10-29 | `fix` | `logs/last_verification.json, logs/verification_history.jsonl, src/sololedger/phone_adb.py` | 월별 카드대금 출금완료 안내 필터링 정교화 | `PASSED` |
