# SoloLedger (`v1.0.0`) 사용자 사용 매뉴얼 가이드

> **대상 시스템**: `SoloLedger` — 1인 법인 전용 회계·경비 원장 및 세무증빙 자동화 시스템
> **웹 대시보드 주소**: `http://127.0.0.1:8000/dashboard`
> **API 문서(Swagger) 주소**: `http://127.0.0.1:8000/docs`

---

## 목차
1. [운용 모드 이해 및 전환 (테스트 모드 vs 실운영 모드)](#1-운용-모드-이해-및-전환-테스트-모드-vs-실운영-모드)
2. [서버 시작·종료 및 웹 대시보드 화면 가이드](#2-서버-시작종료-및-웹-대시보드-화면-가이드)
3. [실무 워크플로우 A: 월말 10분 홈택스 CSV 일괄 정산 모드 (API 키 불필요)](#3-실무-워크플로우-a-월말-10분-홈택스-csv-일괄-정산-모드-api-키-불필요)
4. [실무 워크플로우 B: 실시간 카드 SMS + 텔레그램 봇(@SoloLedgerBot) 자동화 모드](#4-실무-워크플로우-b-실시간-카드-sms--텔레그램-봇sololedgerbot-자동화-모드)
5. [터미널(CLI) 명령어 7종 요약표](#5-터미널cli-명령어-7종-요약표)
6. [1인 법인 표준 계정과목 및 세무 처리 기준표](#6-1인-법인-표준-계정과목-및-세무-처리-기준표)
7. [개인 NAS(khomeNAS Z:\) 백업 및 24시간 서버 노트북 확장](#7-개인-naskhomenas-z-백업-및-24시간-서버-노트북-확장)

---

## 1. 운용 모드 이해 및 전환 (테스트 모드 vs 실운영 모드)

SoloLedger는 연습·테스트 데이터가 실제 법인 장부와 섞이지 않도록 프로젝트 루트의 `.env` 파일 설정에 따라 **테스트 모드(`test`)**와 **실운영 모드(`prod`)**를 완전히 분리합니다.

| 구분 | 테스트 모드 (`SOLOLEDGER_ENV=test`) — **현재 설정** | 실운영 모드 (`SOLOLEDGER_ENV=prod`) |
| :--- | :--- | :--- |
| **원장 DB 파일** | `sololedger_test.db` | `sololedger_prod.db` |
| **로컬 영수증 폴더** | `archive_test/` | `archive/` |
| **NAS(`Z:\`) 백업 폴더** | `Z:\HDD1\Data\SoloLedger_Test` | `Z:\HDD1\Data\SoloLedger` |
| **대시보드 상단 배지** | 초록색 `TEST MODE` 표시 | `PROD MODE` 표시 |

### 실운영 모드로 전환하는 방법
충분히 테스트하신 뒤 실제 법인카드 장부로 쓰실 때는 `.env` 파일을 열어 아래와 같이 수정하고 서버를 재시작하면 됩니다.
```ini
SOLOLEDGER_ENV=prod
SOLOLEDGER_DB_PATH=./sololedger_prod.db
SOLOLEDGER_ARCHIVE_DIR=./archive
SOLOLEDGER_NAS_DIR=Z:\HDD1\Data\SoloLedger
SOLOLEDGER_API_KEY=나만의-비밀-API-키
```

---

## 2. 서버 시작·종료 및 웹 대시보드 화면 가이드

### 2.1 서버 켜기 및 끄기
PowerShell 터미널(`C:\ybkim\workspace\SoloLedger`)에서 아래 명령어로 서버를 제어합니다.

```powershell
# 1) 창 없이 백그라운드로 서버 켜기 (추천)
powershell -ExecutionPolicy Bypass -File scripts/server_service.ps1 start-background

# 2) 현재 서버 실행 상태(PID) 확인
powershell -ExecutionPolicy Bypass -File scripts/server_service.ps1 status

# 3) 백그라운드 서버 끄기
powershell -ExecutionPolicy Bypass -File scripts/server_service.ps1 stop-background
```

### 2.2 웹 대시보드 (`http://127.0.0.1:8000/dashboard`) 구성 및 기능

브라우저에서 `http://127.0.0.1:8000/dashboard`에 접속하면 다음 4개 영역이 표시됩니다.

1. **상단 컨트롤 바 (4대 원클릭 버튼)**
   - **`[샘플 데이터 생성]`**: 연습용 법인카드 거래 6건(쿠팡, 네이버페이 4K 모니터, 스타벅스 단독 식사, 한우명가 거래처 미팅, SK에너지 주유소, 미분류 문구점)과 샘플 영수증을 즉시 생성합니다.
   - **`[테스트 DB 초기화]`**: 연습 삼아 입력한 모든 전표와 영수증을 지우고 백지(0건) 상태로 리셋합니다.
   - **`[NAS(Z:\) 백업 동기화]`**: 현재 원장 DB 스냅샷과 영수증·세무사 ZIP 파일들을 `Z:\HDD1\Data\SoloLedger_Test` (`khomeNAS`)로 즉시 안전 복사합니다.
   - **`[세무사 제출용 ZIP+CSV 생성]`**: 해당 귀속월의 세무사 제출용 기장원장 CSV(엑셀 한글 호환 `utf-8-sig`)와 정규화된 영수증 이미지들을 하나의 `.zip` 파일로 압축 생성합니다.

2. **핵심 요약 카드 (4종)**
   - **월 총 지출액**: 취소 전표(`CANCELED`)를 제외한 당월 법인카드 총 승인 금액
   - **공제 가능 부가세 합계**: 부가세 신고 시 환급/공제받을 수 있는 매입세액(`DEDUCTIBLE`) 합계
   - **불공제 지출 합계**: 접대비(`813`)·업무용승용차(`822`)·간이과세자 등 공제받지 못하는 지출 및 불공제 부가세 합계
   - **미분류·소명 필요 건수**: 계정과목이 `999(미분류)`이거나 품목/참석자 적요가 비어 있는 전표 건수

3. **업무용승용차 연간 1,500만 원 특례 한도 트래커**
   - 법인세법상 **운행기록부 작성 없이 비용 인정이 가능한 연간 1,500만 원 한도** 대비 현재까지의 누적 차량 관련 지출(`822 차량유지비`: 주유·정비·통행료·주차비 등) 사용률(%)과 잔여 한도를 게이지 바로 보여줍니다.
   - 80%(1,200만 원) 도달 시 `WARNING_80PCT`, 1,500만 원 초과 시 `LIMIT_EXCEEDED` 경고가 표시됩니다.

4. **미분류(`999`) 및 품목·참석자 소명 필요 전표 테이블 (인라인 수정)**
   - 적요가 비어 있거나 미분류된 전표 옆의 입력창에 **`"모니터암 구매"`** 또는 **`"B사 박영희 이사 미팅"`**을 입력하고 **`[저장]`** 버튼을 누르면 즉시 원장에 반영되며, 연결된 영수증 파일명도 `{YYYYMMDD}_{가맹점}_{계정과목}_{적요}_{금액}.{ext}` 형식으로 자동 변경됩니다.

---

## 3. 실무 워크플로우 A: 월말 10분 홈택스 CSV 일괄 정산 모드 (API 키 불필요)

별도의 외부 API 키나 스마트폰 설정 없이 **매월 말(또는 분기별 부가세·법인세 신고 전) 딱 10분 만에 세무사 제출 자료를 만드는 방법**입니다.

```mermaid
flowchart LR
    Step1["1. 홈택스/카드사에서\n매입내역 CSV 다운로드"] --> Step2["2. CLI로 CSV 일괄 임포트\n(import-csv)"]
    Step2 --> Step3["3. 웹 대시보드에서\n미분류 적요 입력"]
    Step3 --> Step4["4. 세무사 제출용 ZIP 생성\n& NAS(Z:\\) 백업"]
```

1. **홈택스 CSV 다운로드**: 홈택스 `[조회/발급 → 사업용신용카드 → 매입내역 조회]` 또는 카드사 홈페이지에서 월별 이용내역을 CSV(UTF-8 또는 CP949 모두 자동 인식)로 저장합니다.
2. **CSV 일괄 임포트 및 자동 대사**:
   ```powershell
   .venv\Scripts\python.exe -m sololedger.cli import-csv C:\Downloads\hometax_202610.csv
   ```
   - 기존에 SMS나 영수증으로 들어온 전표가 있으면 **1순위(승인번호) → 2순위(금액+10분 시간창)** 순으로 자동 병합하고, 없는 내역만 신규 생성(`3순위`)합니다.
   - 간이과세자·면세사업자·폐업자 가맹점은 자동으로 부가세 불공제(`NON_DEDUCTIBLE` / `EXEMPT`) 처리됩니다.
3. **영수증 파일 업로드 (선택)**: 온라인 쇼핑(쿠팡·네이버페이)이나 고액 영수증 이미지를 업로드하면 SHA-256 해시로 중복 여부를 검사한 뒤 전표와 자동 매칭합니다.
4. **세무사 제출용 패키지 추출**:
   ```powershell
   .venv\Scripts\python.exe -m sololedger.cli export-pack 202610
   ```
   - `archive_test/exports/SoloLedger_세무증빙_202610.zip` 파일이 생성되며, 한 번 출력된 전표는 `exported_year_month = '202610'`으로 마킹되어 다음 달 중복 신고를 방지합니다(재출력이 필요할 때는 `--force` 옵션 사용).

---

## 4. 실무 워크플로우 B: 실시간 카드 SMS + 텔레그램 봇(`@SoloLedgerBot`) 자동화 모드

결제하는 즉시 스마트폰에서 10초 만에 품목/참석자 소명을 끝내고 싶을 때 사용하는 **실시간 자동화 모드**입니다.

### 4.1 무료 외부 API 키 3종 설정 (`.env`)
1. **`TELEGRAM_BOT_TOKEN` & `TELEGRAM_CHAT_ID`**: 텔레그램 `@BotFather`에서 `/newbot`으로 발급
2. **`GEMINI_API_KEY`**: [Google AI Studio](https://aistudio.google.com/)에서 무료 발급 (영수증 사진 자동 파싱용)
3. **`NTS_API_SERVICE_KEY`**: [공공데이터포털(data.go.kr)](https://www.data.go.kr/)에서 `국세청 사업자등록정보 상태조회` 신청

### 4.2 스마트폰 법인카드 승인 문자 웹훅 설정
- **요청 URL**: `http://<서버주소>:8000/api/v1/webhooks/sms`
- **HTTP 메서드**: `POST`
- **헤더(Headers)**:
  - `Content-Type: application/json`
  - `X-API-Key: dev-sololedger-secret-key` (`.env`의 `SOLOLEDGER_API_KEY` 값)
- **본문(JSON Body)**:
  ```json
  {
    "sender": "15447200",
    "raw_message": "[Web발신] 신한법인(9876) 승인 45,000원 일시불 10/29 12:30 스타벅스역삼점 승인번호:12345678"
  }
  ```

### 4.3 결제 직후 텔레그램 `@SoloLedgerBot` 사용법
1. **식당·카페 결제 시 (인라인 버튼 클릭)**:
   - 카드를 긁으면 텔레그램으로 **`[단독 식사(811 복리후생비)]`** / **`[거래처 미팅(813 기업업무추진비·불공제)]`** 버튼이 도착합니다.
   - 거래처와 식사한 경우 `[거래처 미팅]` 버튼을 누르고 답장으로 `"A사 홍길동 대표 미팅"`이라고 보내면 즉시 접대비 불공제 전환 + 참석자 기록이 완료됩니다.
2. **온라인 쇼핑몰(쿠팡·네이버) 결제 시 (답장 또는 캡처 전송)**:
   - 알림 메시지에 답장으로 `"사무용 복사용지 구매"`(소모품비 유지) 또는 `"모니터 구매 (비품)"`(`212 비품` 자동 전환)을 입력하거나 주문내역 캡처 이미지를 보내면 품목이 자동 기록됩니다.

---

## 5. 터미널(CLI) 명령어 7종 요약표

모든 기능은 웹 대시보드뿐만 아니라 터미널 CLI로도 동일하게 실행할 수 있습니다.

| 명령어 | 실행 예시 | 동작 설명 |
| :--- | :--- | :--- |
| **`init-db`** | `.venv\Scripts\python.exe -m sololedger.cli init-db` | SQLite 4대 테이블 및 세무 분류 규칙 초기화 |
| **`status`** | `.venv\Scripts\python.exe -m sololedger.cli status --year-month 202610` | 현재 원장 건수, 월별 지출·부가세 합계, 업무용승용차 한도 JSON 출력 |
| **`seed-demo`** | `.venv\Scripts\python.exe -m sololedger.cli seed-demo --year-month 202610` | 테스트용 실전 샘플 전표 6건 및 샘플 영수증 자동 생성 |
| **`reset-db`** | `.venv\Scripts\python.exe -m sololedger.cli reset-db` | 테스트 DB 전표·영수증·가맹점 캐시 전체 초기화 |
| **`import-csv`** | `.venv\Scripts\python.exe -m sololedger.cli import-csv hometax.csv` | 홈택스/카드사 CSV(UTF-8/CP949) 일괄 임포트 및 3단계 대사 |
| **`export-pack`** | `.venv\Scripts\python.exe -m sololedger.cli export-pack 202610 --force` | 세무대리인 제출용 표준 CSV + 정규화 영수증 ZIP 번들 생성 |
| **`backup-nas`** | `.venv\Scripts\python.exe -m sololedger.cli backup-nas` | `khomeNAS (Z:\)`로 SQLite 무결성 스냅샷 및 영수증 증분 동기화 |

---

## 6. 1인 법인 표준 계정과목 및 세무 처리 기준표

SoloLedger 세무 엔진이 자동 분류하는 1인 법인 핵심 계정과목 기준입니다.

| 계정코드 | 계정과목명 | 주요 적용 대상 | 부가세 공제 여부 | 실무 세무 포인트 |
| :---: | :--- | :--- | :---: | :--- |
| **`811`** | **복리후생비** | 대표이사 단독 식사, 탕비실 음료/커피 | **공제 (`DEDUCTIBLE`)** | 일반과세 음식점/카페 결제 시 매입세액 공제 가능 |
| **`813`** | **기업업무추진비** | 거래처 미팅 식사·다과, 경조사비 | **불공제 (`NON_DEDUCTIBLE`)** | 구 접대비. 부가세 공제 불가하며 참석자/거래처 메모 필수 |
| **`822`** | **차량유지비** | 주유소, 전기차 충전, 정비, 주차, 하이패스 | **불공제 (`NON_DEDUCTIBLE`)** | 비영업용 소형승용차 매입세액 불공제 + **연간 1,500만 원 한도 관리** |
| **`826`** | **도서인쇄비** | 서점(교보문고 등) 기술서적, 명함/서류 인쇄 | 도서 **면세 (`EXEMPT`)** / 인쇄 **공제** | 도서 구매는 부가세 0원(`EXEMPT`) 자동 반영 |
| **`830`** | **소모품비** | 쿠팡·네이버 사무용품, 30만 원 미만 소액 물품 | **공제 (`DEDUCTIBLE`)** | 온라인 PG 결제 시 실제 구매 품목명(`모니터암 외 1건`) 소명 필수 |
| **`831`** | **지급수수료** | ChatGPT, GitHub, AWS, 클라우드·SaaS 구독 | 국내 **공제** / 해외 **면세·영세** | 해외 달러 결제 시 카드사 매입 확정 원화 금액으로 대사 |
| **`212`** | **비품** | 노트북, 4K 모니터, 책상 등 **30만 원 이상 자산** | **공제 (`DEDUCTIBLE`)** | 30만 원 이상 고액 장비 또는 `'비품'` 명시 시 자동 승격 |
| **`999`** | **가계정(미분류)** | 규칙 미매칭 신규 가맹점 | **확인 필요 (`PENDING`)** | 대시보드에서 품목/용도 입력 시 해당 계정으로 즉시 전환 |

---

## 7. 개인 NAS(`khomeNAS Z:\`) 백업 및 24시간 서버 노트북 확장

1. **개인 NAS(`Z:\`) 백업 폴더 구조**:
   - `backups/sololedger_prod_latest.db`: 가장 최근 동기화된 원장 DB 최신본
   - `backups/sololedger_prod_YYYYMMDD.db`: 일자별 원장 DB 스냅샷
   - `archive/original/`: 업로드된 영수증 원본 이미지
   - `archive/normalized/`: `{YYYYMMDD}_{가맹점}_{계정과목}_{품목}_{금액}.{ext}` 규칙으로 정리된 5년 보관용 영수증
   - `archive/exports/`: 세무사에게 바로 전달할 월별 `SoloLedger_기장원장_YYYYMM.csv` 및 `SoloLedger_세무증빙_YYYYMM.zip`
2. **추후 중고 노트북 / 미니 PC를 24시간 무인 서버로 세팅할 때**:
   - 상세 절차는 [docs/SERVER_SETUP_GUIDE.md](./SERVER_SETUP_GUIDE.md)에 정리되어 있으며, 새 노트북에서 `scripts/server_service.ps1 configure-power` 및 `scripts/server_service.ps1 install` 두 줄만 실행하면 부팅 시 자동 실행과 매일 새벽 2시 NAS 자동 백업이 등록됩니다.
