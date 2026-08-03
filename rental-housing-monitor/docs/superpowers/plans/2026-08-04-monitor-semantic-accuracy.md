# Monitor Semantic Accuracy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 임대 공고와 GPT 자연어 모니터의 오탐·무응답·내용 불일치를 차단한다.

**Architecture:** 공식 수집기의 사실 필드와 제목 필터를 바로잡고, planner 승인 경계에
결정적 의미 검증을 추가한다. 사용자가 승인·알림 단계에서 실제 조건과 관측값을 확인할
수 있도록 안전한 출력만 보강한다.

**Tech Stack:** Python 3.12, Pydantic, Scrapling, BeautifulSoup, pytest, SQLite

## Global Constraints

- 기존 세 공급유형과 DB schema를 유지한다.
- 비밀값과 credential query를 출력하지 않는다.
- 테스트는 코드 변경이 끝난 뒤 커밋·배포 직전에 한 번만 실행한다.

---

### Task 1: 공식 임대주택 정확도

**Files:** `src/rental_monitor/filters.py`, `src/rental_monitor/collectors/lh.py`,
`tests/test_filters.py`, `tests/test_lh_collector.py`

- [x] 후속 발표·동호선정 제목 회귀 테스트와 LH 마감일·대상 회귀 테스트를 작성한다.
- [x] 공백 차이를 정규화한 후속글 차단과 공공데이터 사실 필드 매핑을 구현한다.

### Task 2: GPT 제안 의미 검증

**Files:** `src/personal_monitor/control/planner.py`,
`tests/personal_monitor/control/test_planner.py`

- [x] 0건 추출과 사용자 문장에 없는 키워드·비교값을 거부하는 테스트를 작성한다.
- [x] 조건별 결정적 의미 검증을 후보 승인 경계에 구현한다.

### Task 3: 확인 가능한 Telegram 출력

**Files:** `src/personal_monitor/control/preview.py`, `src/personal_monitor/engine/runner.py`,
`tests/personal_monitor/control/test_preview.py`, `tests/personal_monitor/engine/test_runner.py`

- [x] 실제 키워드 미리보기와 안전한 매칭값 알림 테스트를 작성한다.
- [x] 길이 제한·비밀값 제거를 유지하며 사용자 확인 정보를 렌더링한다.

### Task 4: 검증·문서·배포

**Files:** `PROJECT_HANDOFF.md`

- [x] 인수인계 문서를 갱신한다.
- [x] 전체 pytest, Ruff, diff 검사를 실행한다.
- [x] 커밋, push, PR, merge 후 rollback 가능한 방식으로 GCP VM에 배포하고 운영 상태를 확인한다.
