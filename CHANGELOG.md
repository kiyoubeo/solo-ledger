# CHANGELOG — SoloLedger 개발 변경 이력

모든 개발 변경사항은 하네스 엔지니어링 파이프라인(`scripts/harness.ps1`)을 통해 검증된 후 이 문서와 `logs/dev_changes.jsonl`에 자동 기록됩니다.

| 날짜 | 분류 | 변경 파일 | 변경 내용 요약 | 검증 상태 |
|------|------|-----------|----------------|-----------|
| 2026-10-27 | `feat` | `.agents/, .env.example, .githooks/, .gitignore (+13 files)` | SoloLedger 초기 아키텍처, 코어 모듈(SMS 파서·세무 규칙·3단계 대사·FastAPI) 및 하네스 엔지니어링(자동 검증·변경 로깅·커밋/푸시 가드) 구축 | `PASSED` |
