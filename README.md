# SoloLedger (`solo-ledger`)

> **1인 법인 전용 회계·경비 원장 및 실시간 법인카드 세무증빙 자동화 시스템**
> (Solo-Corp Expense & Automated Tax Evidence Ledger)

외부 상용 SaaS 구독료나 유료 금융 데이터 중계 API 없이 **100% 자체 개발(Zero-Cost Full In-house)** 로 운영하는 1인 법인 맞춤형 경비 원장 및 세무증빙 파이프라인입니다.

---

## 1. 핵심 기능 아키텍처

1. **실시간 법인카드 SMS 수집 Webhook (`POST /api/v1/webhooks/sms`)**
   - 스마트폰(Android MacroDroid / iOS 단축어)에서 포착한 국내 주요 카드사(신한, KB국민, 현대, 삼성, 하나, 우리, 기업/BC, NH농협) 승인·취소 문자를 파싱하여 원장(`card_transactions`)에 즉시 적재.
   - 중복 수신 방지를 위한 멱등성(Idempotency) 보장.
2. **1인 법인 세무 자동 분류 규칙 엔진 (`tax_classification_rules`)**
   - **PG사 / 온라인 쇼핑몰 (`쿠팡`, `네이버페이` 등)**: `830 소모품비` 1차 분류 후 품목 소명 요청.
   - **업무용승용차 관련 비용 (`주유소`, `스피드메이트`, `주차` 등)**: `822 차량유지비` + 부가세 강제 불공제(`NON_DEDUCTIBLE` / `PASSENGER_CAR`) + 연간 1,500만 원 특례 한도 추적(`is_vehicle_expense=True`).
   - **SaaS / 클라우드 구독 (`GITHUB`, `OPENAI`, `GOOGLE`, `AWS` 등)**: `831 지급수수료` 자동 분류.
   - **식음료 / 카페 (`스타벅스`, `컴포즈` 등)**: `811 복리후생비` 분류 후 단독 식사 vs 거래처 미팅(`813 기업업무추진비`) 확인 프롬프트 발송.
3. **3단계 자동 대사(Reconciliation) 엔진 (`POST /api/v1/reconcile`)**
   - **1순위**: 승인번호(`approval_no`) + 결제금액 정확 일치 병합
   - **2순위**: 결제금액 + 거래일시(`transacted_at`) 전후 10분 오차 범위 내 `SMS_ONLY` 레코드 병합
   - **3순위**: 미매칭 건 신규 원장 강제 생성(`INSERT`) 및 간이/면세/휴폐업 사업자 부가세 불공제 자동 반영

---

## 2. 하네스 엔지니어링 (Harness Engineering) 워크플로우

본 저장소는 모든 코드 변경의 **정합성 검증, 개발 이력 로깅, 자동 커밋·푸시**를 강제하는 하네스 엔지니어링 체계가 구축되어 있습니다.

### 2.1 통합 하네스 CLI (`scripts/harness.ps1`)

| 명령어 | 동작 설명 |
| :--- | :--- |
| `powershell -ExecutionPolicy Bypass -File scripts/harness.ps1 review-plan` | **개발 계획서 추적성 검토**: `docs/DEVELOPMENT_PLAN.md`의 완료/예정 Phase 산출물·API 엔드포인트·테스트 파일·패키지 버전 정합성 감사 |
| `powershell -ExecutionPolicy Bypass -File scripts/harness.ps1 verify` | **5단계 자동 검증 실행**: ① 계획서 정합성(`plan_review`) → ② 구문 컴파일(`compileall`) → ③ 정적 분석(`ruff check`) → ④ SQLite 스키마·시드 무결성 검사 → ⑤ 단위·통합 테스트(`pytest`). 결과를 `logs/last_verification.json` 및 `logs/verification_history.jsonl`에 기록 |
| `powershell -ExecutionPolicy Bypass -File scripts/harness.ps1 log -Message "변경 요약" -Category feat` | 변경 내역을 `CHANGELOG.md`, `CLAUDE.md`, `logs/dev_changes.jsonl`에 동시 기록 |
| `powershell -ExecutionPolicy Bypass -File scripts/harness.ps1 commit-push -Message "변경 요약" -Category feat` | **`verify` 검증 통과 시에만** 변경 로그를 기록하고 `git commit` 및 `git push origin main`까지 원스톱 수행 |
| `powershell -ExecutionPolicy Bypass -File scripts/harness.ps1 status` | 현재 Git 동기화 상태, 최근 검증 결과, 최근 개발 변경 로그 출력 |

### 2.2 에이전트 라이프사이클 훅 & 가드레일 (`.agents/hooks.json`)
- **`PostToolUse` 감사 훅 (`.agents/scripts/post_tool_audit.ps1`)**: `write_file` / `edit_file` 실행 시마다 수정 시각과 대상 파일을 `logs/tool_edit_audit.jsonl`에 자동 기록합니다.
- **`Stop` 완료 가드 훅 (`.agents/scripts/stop_guard.ps1`)**: 에이전트가 턴을 종료하기 전, 커밋되지 않은 변경사항이나 푸시되지 않은 커밋, 혹은 실패한 하네스 검증이 남아있으면 종료를 차단하고 **검증 → 로그 기록 → 커밋·푸시**를 완료하도록 강제합니다.
- **Git `pre-commit` 훅 (`.githooks/pre-commit`)**: 하네스 검증(`logs/last_verification.json`의 `PASSED` 상태)이 확인되지 않은 커밋을 원천 차단합니다.

---

## 3. 디렉토리 구조

```text
SoloLedger/
├── .agents/                    # Antigravity 에이전트 규칙, 스킬, 라이프사이클 훅
├── .githooks/                  # Git pre-commit 무결성 검증 훅
├── docs/
│   ├── ARCHITECTURE_SPEC.md    # 전체 시스템 아키텍처 및 도메인 설계 명세서
│   ├── DEVELOPMENT_PLAN.md     # 단계별(Phase 1~5) 마스터 개발 계획서
│   ├── USER_MANUAL.md          # SoloLedger 사용자 사용 매뉴얼 가이드 (Markdown)
│   ├── USER_MANUAL.html        # SoloLedger 사용자 사용 매뉴얼 가이드 (웹 브라우저용 HTML)
│   └── SERVER_SETUP_GUIDE.md   # 중고 노트북 / 미니 PC 24시간 무인 서버 구축 가이드
├── logs/
│   ├── dev_changes.jsonl       # 모든 개발 변경사항 구조화 로그 (JSONL)
│   ├── verification_history.jsonl # 하네스 검증 실행 이력 로그 (JSONL)
│   └── last_verification.json  # 최신 하네스 검증 결과 스냅샷
├── scripts/
│   └── harness.ps1             # 하네스 엔지니어링 통합 CLI (verify / log / commit-push / status)
├── sql/
│   └── schema.sql              # 4대 핵심 테이블 DDL 및 1인 법인 세무 규칙 시드 데이터
├── src/sololedger/             # SoloLedger 백엔드 패키지 (FastAPI, SMS 파서, 세무 엔진, 대사 엔진)
├── tests/                      # 단위 및 통합 테스트 스위트 (pytest)
├── AGENTS.md                   # 에이전트 작업 헌장 및 하네스 강제 규칙
├── CHANGELOG.md                # 개발 변경 이력 마스터 테이블
└── pyproject.toml              # Python 패키지 및 도구 설정 (uv)
```
