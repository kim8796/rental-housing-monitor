# URL 없는 모니터 등록 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:test-driven-development` and execute the tasks in order.

**Goal:** 사용자가 URL 없이 사이트명·게시판명만 말해도 공식 URL 후보를 찾아 검증하고, Telegram에서 선택·승인해 모니터를 등록할 수 있게 한다.

**Architecture:** 일반 의도 분석은 현재처럼 웹 검색을 차단한다. `URL 없는 신규 등록`만 별도 검색 전용 Codex 경계로 보내고, 검색 결과는 기존 URL 정책과 Scrapling 실제 접속을 모두 통과한 경우에만 후보로 사용한다.

**Tech Stack:** Python 3.12, Codex CLI(ChatGPT 로그인), Scrapling, SQLite, Telegram Bot API, pytest

## 전역 제약

- 검색 전용 실행 외에는 `web_search="disabled"`를 유지한다.
- 후보는 공식 사이트로 판단되는 URL을 최대 3개만 반환한다.
- 검색 결과와 저장된 별칭은 신뢰하지 않고 사용할 때마다 URL 정책과 실제 접속을 다시 검증한다.
- 검색 실패나 복수 후보의 모호성을 임의 URL 생성으로 해결하지 않는다.
- 기존 `등록·수정·취소` 승인 흐름과 일반 URL 입력 흐름을 보존한다.

## 구현 순서

### 1. URL 없는 신규 등록 의도

**수정:** `src/personal_monitor/ai/contracts.py`, `control/intents.py`, `control/service.py`  
**테스트:** `tests/personal_monitor/control/test_intents.py`, `test_service.py`

- `CREATE` 의도에서 URL이 없어도 사이트명·게시판명을 검색 요청으로 전달한다.
- 사이트를 식별할 설명이 부족하면 검색하지 않고 추가 설명을 요청한다.

### 2. 검색 전용 Codex 경계

**수정:** `ai/codex_cli.py`, `ai/prompts.py`, `ai/contracts.py`  
**테스트:** `tests/personal_monitor/ai/test_codex_cli.py`

- 구조화된 `UrlDiscoveryRequest`와 최대 3개의 `UrlCandidate` 결과 계약을 만든다.
- 검색 전용 호출에서만 웹 검색을 활성화하고 `web_search` 이벤트만 허용한다.
- command, file change, MCP 호출은 기존과 동일하게 거부한다.

### 3. 후보 URL 검증과 별칭 재사용

**생성:** `src/personal_monitor/control/url_discovery.py`  
**수정:** `control/planner.py`  
**테스트:** `tests/personal_monitor/control/test_url_discovery.py`, `control/test_planner.py`

- 저장된 사용자 별칭을 먼저 조회하되 매번 재검증한다.
- 검색 후보마다 URL 정책, redirect 정책, robots 판단, Scrapling 접속을 적용한다.
- 검증된 후보가 0개면 설명 요청, 1개면 기존 미리보기, 2~3개면 선택 단계로 보낸다.

### 4. 사용자별 SQLite URL 별칭

**수정:** `storage/schema.py`, `storage/registry.py`  
**테스트:** `tests/personal_monitor/storage/test_registry.py`

- 새 마이그레이션으로 `url_aliases(owner_id, normalized_name, url, updated_at)`를 추가한다.
- 사용자가 후보를 확정했을 때만 별칭을 upsert한다.
- 별칭은 사용자별로 격리하고 조회·저장 길이를 제한한다.

### 5. Telegram 후보 선택과 기존 승인 연결

**수정:** `control/actions.py`, `control/service.py`  
**테스트:** `tests/personal_monitor/control/test_service.py`

- 복수 후보를 URL이 노출되지 않는 짧은 라벨과 불투명 callback token으로 표시한다.
- 선택 token은 소유자, 후보 URL, 원 요청과 결합하고 만료·재사용을 차단한다.
- 선택 후 URL을 다시 검증하고 기존 `등록·수정·취소` 미리보기로 연결한다.

### 6. 통합 검증과 문서화

**수정:** `PROJECT_HANDOFF.md` 및 관련 운영 문서  
**테스트:** 신규 Telegram 통합 테스트와 전체 회귀 테스트

- URL 제공, 별칭 재사용, 단일 후보, 복수 후보, 검색 실패, 악성 URL, stale callback을 검증한다.
- 실행:

```bash
uv run pytest tests/personal_monitor/ai tests/personal_monitor/control tests/personal_monitor/storage tests/personal_monitor/telegram
uv run pytest
uv run ruff check .
git diff --check
```

## 완료 조건

- URL이 있는 기존 등록 동작에 변화가 없다.
- URL 없는 요청에서만 Codex 웹 검색이 발생한다.
- 검증되지 않은 URL은 미리보기·별칭·모니터에 저장되지 않는다.
- 단일 후보와 복수 후보 흐름이 Telegram 승인 단계까지 이어진다.
- 전체 테스트와 Ruff가 통과한다.
