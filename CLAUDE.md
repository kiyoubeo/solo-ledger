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
| 2026-10-29 | README.md, logs/last_verification.json, logs/verification_history.jsonl, tests/test_nas_sync.py (+1 files) | [docs] SoloLedger(v1.0.0) 사용자 사용 매뉴얼 가이드(docs/USER_MANUAL.md) 작성 및 테스트 검증 반영 |
| 2026-10-29 | README.md, logs/last_verification.json, logs/verification_history.jsonl, src/sololedger/api.py (+3 files) | [docs] HTML 사용자 매뉴얼(docs/USER_MANUAL.html) 및 웹 대시보드 매뉴얼 뷰어(/manual) 추가 |
| 2026-10-29 | logs/last_verification.json, logs/verification_history.jsonl, sql/schema.sql, src/sololedger/api.py (+5 files) | [feat] 갤럭시 S26+ 실기기 USB ADB 카드 문자 추출기(phone_adb.py) 및 7개 카드사 실제 문자 파싱 고도화 |
| 2026-10-29 | logs/last_verification.json, logs/verification_history.jsonl, src/sololedger/api.py, src/sololedger/archiver.py (+5 files) | [feat] 카드별 전표·영수증 분류 현황 대시보드, 카드별 필터링 및 by_card/ 보관 폴더 자동 분류 구현 |
| 2026-10-29 | logs/last_verification.json, logs/verification_history.jsonl, src/sololedger/dashboard.py, src/sololedger/phone_adb.py (+2 files) | [feat] 갤럭시 RCS(채팅+ content://im/chat) 오픈리치카드 메시지 통합 추출 및 2026년 전 기간 연속 조회 지원 |
| 2026-10-29 | logs/last_verification.json, logs/verification_history.jsonl, src/sololedger/phone_adb.py | [fix] 월별 카드대금 출금완료 안내 필터링 정교화 |
| 2026-10-29 | logs/last_verification.json, logs/verification_history.jsonl, src/sololedger/api.py, src/sololedger/dashboard.py (+1 files) | [feat] 대시보드 조회 기간을 시작일~종료일(start_date~end_date) 날짜 범위 선택 방식으로 변경 |
| 2026-10-29 | logs/last_verification.json, logs/verification_history.jsonl, sql/schema.sql, src/sololedger/api.py (+5 files) | [feat] Firefly III 확장 규칙·자동학습, Ledgerline 스마트 소명큐, Bigcapital 계정별 집계표 및 웹 파일 업로드 기능 적용 |
| 2026-10-29 | logs/last_verification.json, logs/verification_history.jsonl, src/sololedger/api.py, src/sololedger/dashboard.py (+1 files) | [feat] 거래일시 클릭 시 전자 신용카드 매출전표 및 실물 영수증 팝업 모달 표시 기능 추가 |
| 2026-10-30 | logs/last_verification.json, logs/verification_history.jsonl, src/sololedger/api.py, src/sololedger/dashboard.py (+2 files) | [feat] 자체 스크래핑 수집 엔진(7대 카드사·홈택스·쇼핑몰) 및 카드별 전자 매출전표 영수증 일괄 생성기 구현 |
| 2026-10-30 | logs/last_verification.json, logs/verification_history.jsonl, src/sololedger/dashboard.py, tests/test_sms_parser.py | [fix] 영수증 팝업 모달 인쇄 시 배경 대시보드 숨김 및 1페이지 단독 출력(@media print) 수정 |
| 2026-10-30 | logs/last_verification.json, logs/verification_history.jsonl, src/sololedger/api.py, src/sololedger/archiver.py (+3 files) | [feat] 영수증 PDF 인쇄 시 전표별 고유 식별 파일명(매출전표_일시_카드_가맹점_계정_금액_TX번호) 자동 지정 |
| 2026-11-02 | logs/last_verification.json, logs/verification_history.jsonl, src/sololedger/dashboard.py, src/sololedger/importers.py (+5 files) | [feat] 우리카드 실명세서 CSV(국내 이용내역 배너·줄바꿈 헤더·MM.DD 일시·취소금액) 파싱 및 10월 15건 원장 대사 반영 |
| 2026-11-02 | logs/last_verification.json, logs/verification_history.jsonl, src/sololedger/importers.py, src/sololedger/reconciler.py (+3 files) | [feat] 현대카드 이용대금명세서 HTML(.xls) 파싱(한글날짜·비정형 닫힘태그·소계제외·네이버현대카드 매핑) 및 10월 11건 반영 |
| 2026-11-02 | logs/last_verification.json, logs/verification_history.jsonl, src/sololedger/dashboard.py, src/sololedger/importers.py (+4 files) | [feat] KB국민카드 바이너리 XLS(BIFF8) 및 이용내역 120건(쿠팡와우·하이패스·마이위시) 파서 보강 및 원장 대사 반영 |
| 2026-11-02 | logs/last_verification.json, logs/verification_history.jsonl, src/sololedger/importers.py, src/sololedger/scraper_service.py (+3 files) | [feat] 롯데카드 다중 테이블 HTML(.xls) 및 부분취소 파싱 지원, 117건 이용내역 원장 대사 반영 |
| 2026-11-02 | logs/last_verification.json, logs/verification_history.jsonl, src/sololedger/dashboard.py, tests/test_sms_parser.py | [feat] 대시보드 카드별 지출 및 영수증 분류 현황을 카드 이름순(오름차순)으로 정렬 |
| 2026-11-02 | logs/last_verification.json, logs/verification_history.jsonl, src/sololedger/api.py, src/sololedger/dashboard.py (+2 files) | [feat] 쿠팡 주문목록 원클릭 브라우저 수집 스크립트 및 화면복사(DOM/SSR) 자동매칭 구현 |
| 2026-11-02 | logs/last_verification.json, logs/verification_history.jsonl, src/sololedger/api.py, src/sololedger/dashboard.py (+2 files) | [feat] 대시보드 전용 브라우저 세션(CDP) 기반 쿠팡 주문내역 원클릭 자동 크롤러(방법 B) 구현 |
| 2026-11-02 | logs/last_verification.json, logs/verification_history.jsonl, src/sololedger/dashboard.py, tests/test_sms_parser.py | [feat] 대시보드 조회 기간에 달별 기간 조회(월 선택 드롭다운, 이전/다음달 이동, 월별 바로가기 버튼) 추가 |
| 2026-11-02 | logs/last_verification.json, logs/verification_history.jsonl, src/sololedger/dashboard.py | [fix] 대시보드 상단 조회 기간 및 달별 조회 필터 바 줄넘김·가독성 레이아웃 정리 |
| 2026-11-02 | logs/last_verification.json, logs/verification_history.jsonl, src/sololedger/api.py, src/sololedger/dashboard.py (+2 files) | [feat] 소명 필요 전표 및 원장에 전자 거래명세서 발급·첨부·PDF 인쇄 기능 추가 |
| 2026-11-02 | logs/last_verification.json, logs/verification_history.jsonl, src/sololedger/api.py, src/sololedger/dashboard.py (+2 files) | [feat] 쿠팡 자동 크롤링 및 스크롤 수집 내역 기반 다품목 전자 거래명세서 자동 작성 구현 |
| 2026-11-02 | logs/last_verification.json, logs/verification_history.jsonl, src/sololedger/dashboard.py, src/sololedger/scraper_service.py (+1 files) | [feat] 거래명세서 합계 영역에 VAT 제외 가격, VAT 가격, 합계 가격 명시적 표시 |
| 2026-11-02 | logs/last_verification.json, logs/verification_history.jsonl, src/sololedger/api.py, src/sololedger/dashboard.py (+3 files) | [feat] 실시간 이메일 주문·결제내역 IMAP 자동 감지 및 웹훅 연동, 전자 거래명세서 자동 작성 구현 |
| 2026-11-03 | logs/last_verification.json, logs/verification_history.jsonl, src/sololedger/api.py, src/sololedger/dashboard.py (+2 files) | [feat] 다중 이메일 계정(네이버, Gmail, 회사메일 등 무제한) 등록·관리 및 통합 실시간 스캔 지원 |
| 2026-11-03 | logs/last_verification.json, logs/verification_history.jsonl, src/sololedger/dashboard.py, tests/test_sms_parser.py | [fix] 대시보드 다중 이메일 연동 모달 스크립트 따옴표 이스케이프 구문 오류 수정 |
| 2026-11-03 | logs/last_verification.json, logs/verification_history.jsonl, src/sololedger/email_watcher.py, tests/test_sms_parser.py | [fix] Gmail 16자리 앱 비밀번호 공백 자동 제거 및 IMAP 인증 오류 한국어 안내 보강 |
| 2026-11-03 | logs/last_verification.json, logs/verification_history.jsonl, src/sololedger/api.py, src/sololedger/dashboard.py (+3 files) | [fix] 이메일 주문 파서 중복 품목 제거 및 연동 결과 확인 UI 추가 |
| 2026-11-03 | logs/last_verification.json, logs/verification_history.jsonl, src/sololedger/api.py, src/sololedger/dashboard.py (+5 files) | [feat] 신규 이메일 주문 실시간 전표 자동생성 및 수동 스캔 최신구간 재탐색 적용 |
| 2026-11-03 | logs/last_verification.json, logs/verification_history.jsonl, src/sololedger/api.py, src/sololedger/dashboard.py (+3 files) | [feat] 쿠팡 직매입·직영몰 사업자번호 자동추출 및 오픈마켓 입점판매자 분리 적용 |
| 2026-11-03 | logs/last_verification.json, logs/verification_history.jsonl, src/sololedger/scraper_service.py | [fix] 거래명세서 갱신 시 기존 다품목 상세내역 보존 보강 |
| 2026-11-04 | logs/last_verification.json, logs/verification_history.jsonl, src/sololedger/dashboard.py, src/sololedger/scraper_service.py (+1 files) | [refactor] 매출전표·거래명세서에서 적격증빙 및 공급받는자 보관용 문구 제거 |
| 2026-11-04 | .gitignore, logs/last_verification.json, logs/verification_history.jsonl | [chore] 로컬 참고 매뉴얼 폴더(references/) .gitignore 추가 |
| 2026-11-05 | .gitignore, docs/DEVELOPMENT_PLAN.md, logs/last_verification.json, logs/verification_history.jsonl (+11 files) | [feat] 순수 Android 전용 앱(v1.1.0) 구축 및 원격 세무증빙 ZIP 다운로드·모바일 인증·우리카드 푸시 API 추가 |
| 2026-11-08 | .gitignore, logs/last_verification.json, logs/verification_history.jsonl, src/sololedger/importers.py (+3 files) | [feat] 법인카드 전용 필터링 정책(corp_card_policy) 및 개인카드 분리 백업·차단 기능 추가 |
| 2026-11-08 | logs/last_verification.json, logs/verification_history.jsonl, src/sololedger/api.py, src/sololedger/dashboard.py (+2 files) | [feat] 개인카드 데이터 백업·초기화 및 법인카드 전용 테스트 모드(개인카드·미매칭 메일 차단 및 법인카드 등록 관리) 전환 |
| 2026-11-09 | logs/last_verification.json, logs/verification_history.jsonl, src/sololedger/exporters.py, src/sololedger/importers.py (+6 files) | [feat] 2026년 1분기 홈택스 사업용신용카드(333건) 정밀 파싱·업태업종 세무분류 고도화 및 원장·매출전표·세무패키지 반영 |
| 2026-11-10 | .gitignore, logs/last_verification.json, logs/verification_history.jsonl | [chore] 로컬 법인 세무신고서 및 사업자등록증 보관 폴더(company/) .gitignore 추가 |
| 2026-11-10 | logs/last_verification.json, logs/verification_history.jsonl, src/sololedger/dashboard.py, src/sololedger/merchant_service.py (+4 files) | [feat] 주식회사 디자인도하다(인테리어·내장목공사 및 디자인업) 맞춤 공사원가 계정코드(501·511) 및 자동 소명 적요 규칙 적용 |
| 2026-11-10 | logs/last_verification.json, logs/verification_history.jsonl, src/sololedger/api.py, src/sololedger/dashboard.py (+1 files) | [feat] 대시보드 계정과목 요약표 클릭 시 분류된 전표 목록 펼침 및 필터 기능 추가 |
