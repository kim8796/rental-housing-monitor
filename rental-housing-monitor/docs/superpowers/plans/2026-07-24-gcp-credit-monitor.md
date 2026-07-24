# GCP Credit Monitor Implementation Plan

> **For agentic workers:** Implement inline in this main session; do not delegate.

**Goal:** GCP 무료 크레딧과 프로젝트별 사용액을 텔레그램에서 조회하고 매일 경보한다.

**Architecture:** BigQuery Standard billing export → 고정 REST 집계 어댑터 → SQLite
스냅샷/경보 저장소 → 제어 서비스와 12:10/12:20 일일 작업 → Telegram.

**Tech Stack:** Python 3.12, SQLite, httpx, BigQuery REST, Docker Compose, GCE.

**Global Constraints:** 테스트를 먼저 실패시킨 뒤 최소 구현한다. 금액은 마이크로원,
로그는 식별자·토큰·쿼리를 제외하고, 실제 서버 이전은 자동 실행하지 않는다.

## Task 1: 저장과 계산

- Add: `src/personal_monitor/billing/{models,repository}.py`
- Modify: `src/personal_monitor/storage/schema.py`
- Test: `tests/personal_monitor/billing/test_repository.py`
- Implement 기준점, 스냅샷, 프로젝트 비용, 소진 예상, 경보 중복 방지.

## Task 2: BigQuery 동기화

- Add: `src/personal_monitor/billing/bigquery.py`
- Test: `tests/personal_monitor/billing/test_bigquery.py`
- Implement 고정 메타데이터 토큰 요청, 제한된 집계 쿼리, 엄격한 응답 파싱.

## Task 3: 텔레그램 조회와 일정

- Add: `src/personal_monitor/billing/service.py`
- Modify: `ai/contracts.py`, `control/intents.py`, `control/service.py`,
  `service.py`, `config.py`, `cli.py`
- Test: billing service tests plus focused control/service/config/CLI tests.
- Implement 자연어 `billing_status`, 12:10 동기화, 12:20 요약, 30/10/5% 및
  만료·30일 소진 경보, 10% 이전 체크리스트.

## Task 4: 배포 연결

- Modify: `compose.yaml`, `.env.example`, `infra/gcp/*`, operation docs.
- Test: deploy tests and shell syntax.
- Create US dataset, grant least-privilege BigQuery access, enable Standard export,
  seed the approved console baseline, deploy VM.

## Task 5: 검증과 통합

- Run focused tests, `ruff check .`, full `pytest`.
- Verify VM containers, latest billing snapshot, Telegram natural-language reply.
- Commit, push, merge to `main`, and confirm deployed revision.
