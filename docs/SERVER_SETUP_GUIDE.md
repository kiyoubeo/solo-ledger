# 중고 노트북 / 미니 PC 24시간 무인 서버 구축 가이드 (`SERVER_SETUP_GUIDE.md`)

본 문서는 추후 중고 노트북이나 저전력 미니 PC를 도입하여 **SoloLedger를 24시간 365일 무인 가동**하고 **`khomeNAS (Z:\HDD1\Data\SoloLedger)`에 자동 백업**하기 위한 원클릭 구축 매뉴얼입니다.

---

## 1. 사전 준비 (노트북 물리 세팅)

1. **배터리 안전 조치**:
   - 탈착식 배터리 노트북: 배터리를 분리하고 어댑터 전원만 연결합니다.
   - 내장형 배터리 노트북: 제조사 프로그램에서 **배터리 충전 한도를 60% ~ 80%로 제한**합니다.
2. **네트워크 및 ipDISK 연결**:
   - ipTIME 공유기에 유선 랜(LAN)으로 연결합니다.
   - `ipDISK Drive` 프로그램을 설치하고 `ybkhome2.ipdisk.co.kr` (`Z:\` 드라이브)의 **`[윈도우 시작시 자동실행]`**을 체크합니다.

---

## 2. 서버 PC에 SoloLedger 복제 (최초 1회)

새 노트북/서버 PC에서 PowerShell을 열고 아래 명령어를 순서대로 실행합니다.

```powershell
# 1) 저장소 복제
git clone https://github.com/kiyoubeo/solo-ledger.git C:\SoloLedger
cd C:\SoloLedger

# 2) 가상환경 및 패키지 설치 (uv 사용)
uv sync

# 3) DB 초기화
.venv\Scripts\python.exe -m sololedger.cli init-db
```

---

## 3. 윈도우 재부팅 시 자동 시작 & 매일 새벽 NAS 백업 등록

이미 프로젝트에 구축되어 있는 [scripts/server_service.ps1](../scripts/server_service.ps1) 스크립트로 **① 절전 방지 전원 설정**, **② 부팅 시 FastAPI 서버 자동 기동**, **③ 매일 새벽 02:00 `Z:\HDD1\Data\SoloLedger` 자동 백업**을 한 번에 등록할 수 있습니다.

```powershell
# 1) 노트북 24시간 가동용 전원 정책 자동 설정 (AC 전원 시 절전 해제, 모니터만 5분 뒤 끄기)
powershell -ExecutionPolicy Bypass -File scripts/server_service.ps1 configure-power

# 2) 윈도우 작업 스케줄러에 자동 시작(SoloLedger-API-Server) 및 일일 NAS 백업(SoloLedger-Daily-NAS-Backup) 등록
powershell -ExecutionPolicy Bypass -File scripts/server_service.ps1 install

# 3) 등록 상태 확인
powershell -ExecutionPolicy Bypass -File scripts/server_service.ps1 status
```

### 등록 해제 및 수동 백그라운드 제어 명령어
```powershell
# 자동 시작 작업 스케줄러 등록 해제 (작업용 PC로 되돌릴 때)
powershell -ExecutionPolicy Bypass -File scripts/server_service.ps1 uninstall

# 스케줄러 등록 없이 현재 세션에서만 백그라운드로 서버 띄우기 / 내리기
powershell -ExecutionPolicy Bypass -File scripts/server_service.ps1 start-background
powershell -ExecutionPolicy Bypass -File scripts/server_service.ps1 stop-background
```

---

## 4. 외부 스마트폰 접속 연결 (택 1)

1. **방법 A — ipTIME 공유기 포트포워딩 (`ybkhome2.ipdisk.co.kr`)**:
   - 공유기 설정(`http://192.168.0.1`) → `[고급 설정] → [NAT/라우터 관리] → [포트포워드 설정]`
   - 규칙 이름: `SoloLedger`, 내부 IP: `(노트북 내부 IP)`, 프로토콜: `TCP`, 외부 포트: `8000`, 내부 포트: `8000`
   - 스마트폰 SMS 웹훅 주소: `http://ybkhome2.ipdisk.co.kr:8000/api/v1/webhooks/sms`
   - 웹 대시보드 주소: `http://ybkhome2.ipdisk.co.kr:8000/dashboard`
2. **방법 B — 무료 Tailscale 설치 (포트포워딩 불필요, 최고 보안)**:
   - 서버 노트북과 스마트폰에 [Tailscale](https://tailscale.com/) 설치 및 동일 계정 로그인
   - 부여된 Tailscale IP(`100.x.y.z`)를 사용하여 `http://100.x.y.z:8000/dashboard`로 외부 어디서나 암호화 접속
