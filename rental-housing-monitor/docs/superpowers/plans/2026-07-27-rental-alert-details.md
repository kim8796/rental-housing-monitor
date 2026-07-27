# 임대주택 알림 상세정보 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 신규 임대주택 알림을 한국어 상세 공고 메시지와 실제 상세 URL로 전송한다.

**Architecture:** `render_payload`가 고정 임대주택 adapter의 검증된 item만
`Announcement`로 복원해 기존 formatter를 사용한다. 일반 모니터와 잘못된 item은
콘텐츠를 숨긴 한국어 일반 알림으로 폴백한다.

**Tech Stack:** Python 3.12, pytest, SQLite outbox, Telegram plain text

## Global Constraints

- 일반 Scrapling item 콘텐츠는 알림에 노출하지 않는다.
- 기존 outbox dedupe key와 DB schema는 변경하지 않는다.
- 구현은 현재 메인 세션에서 순차적으로 진행한다.

---

### Task 1: 안전한 임대주택 알림 렌더링

**Files:**
- Modify: `src/personal_monitor/engine/runner.py`
- Modify: `tests/personal_monitor/engine/test_runner.py`
- Modify: `tests/personal_monitor/integration/test_static_monitor.py`

**Interfaces:**
- Consumes: `MonitorSpec`, `ObservedItem`, `RuleMatch`
- Produces: `render_payload(...) -> dict[str, object]`

- [ ] **Step 1: Write the failing tests**

임대주택 item의 제목·상세 URL·한국어 제목이 payload에 포함되고, 일반 item 콘텐츠는
계속 제외되며 손상된 임대주택 item은 일반 알림으로 폴백하는 literal assertion을
추가한다.

- [ ] **Step 2: Run tests to verify RED**

Run: `.venv/bin/python -m pytest tests/personal_monitor/engine/test_runner.py -k payload -q`

Expected: 임대주택 상세 payload 테스트가 현재 일반 `new_item` payload 때문에 실패.

- [ ] **Step 3: Implement the minimal renderer**

`python_plugin/rental_housing`와 `new_item` 조합만 검증된 `Announcement`로 변환해
`format_announcement`를 사용한다. 변환 실패 또는 일반 monitor는 한국어 kind
label과 query·fragment가 제거된 기존 public URL을 사용한다.

- [ ] **Step 4: Verify GREEN and regression**

Run:

```bash
.venv/bin/python -m pytest tests/personal_monitor/engine/test_runner.py \
  tests/personal_monitor/integration/test_static_monitor.py
.venv/bin/python -m pytest
.venv/bin/ruff check .
git diff --check
```

Expected: 모든 테스트와 정적 검사가 통과.

- [ ] **Step 5: Deploy and smoke test**

새 커밋을 rollback 가능 절차로 GCP VM에 배포한 뒤, 저장된 오늘 공고 item으로
`render_payload`를 호출해 제목·상세 URL이 나오고 `new_item`·목록 URL이 없는지
확인한다. 운영 서비스·DB·heartbeat·백업을 재검증한다.
