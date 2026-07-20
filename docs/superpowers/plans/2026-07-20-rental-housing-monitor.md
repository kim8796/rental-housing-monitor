# Rental Housing Monitor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a daily Python monitor that finds new Seoul/Gyeonggi LH, SH, and GH rental-housing notices from official sources and reports them through Telegram without duplicate delivery.

**Architecture:** Three institution-specific collectors normalize official JSON/HTML into one immutable `Announcement` model. A runner filters results, uses SQLite delivery records for idempotency, isolates institution failures, and sends either new notices, an explicit no-new-notice message, or an institution-specific failure summary. GitHub Actions restores and persists the SQLite database on a dedicated `data` branch.

**Tech Stack:** Python 3.12, httpx, BeautifulSoup4, tenacity, python-dotenv, SQLite, pytest, pytest-httpx, Ruff, GitHub Actions, Telegram Bot API.

## Global Constraints

- Use only official LH, SH, GH pages or official APIs.
- Monitor only Seoul and Gyeonggi happiness housing, national rental housing, and newlywed-targeted purchased rental housing.
- Store secrets only in environment variables; never log or commit their values.
- Treat the first run's matching results as new notices.
- Record Telegram delivery only after the API confirms success.
- Continue processing healthy institutions when another institution fails.
- Run at UTC 03:00, which is Korea Standard Time 12:00.
- Keep application files under `rental-housing-monitor/`; only the workflow lives at repository-root `.github/workflows/`.

---

### Task 1: Package skeleton, domain model, and filtering

**Files:**
- Create: `rental-housing-monitor/pyproject.toml`
- Create: `rental-housing-monitor/src/rental_monitor/__init__.py`
- Create: `rental-housing-monitor/src/rental_monitor/models.py`
- Create: `rental-housing-monitor/src/rental_monitor/filters.py`
- Create: `rental-housing-monitor/tests/test_models.py`
- Create: `rental-housing-monitor/tests/test_filters.py`

**Interfaces:**
- Produces: `Announcement`, `Agency`, `HousingType`, `canonical_key(announcement)`, `classify_housing_type(title, raw_type) -> HousingType | None`, `normalize_region(raw_region) -> str | None`, and `is_recruitment_title(title) -> bool`.
- Consumes: only Python standard-library types.

- [ ] **Step 1: Write failing model and filter tests**

```python
def test_source_id_makes_stable_agency_scoped_key(sample_announcement):
    assert canonical_key(sample_announcement) == "LH:2016122300001530"

def test_newlywed_purchase_requires_purchase_and_newlywed_terms():
    result = classify_housing_type(title="신혼·신생아 매입임대 입주자 모집", raw_type="매입임대")
    assert result is HousingType.NEWLYWED_PURCHASE

def test_follow_up_result_post_is_excluded():
    assert is_recruitment_title("행복주택 당첨자 발표") is False
```

- [ ] **Step 2: Run tests and verify they fail because the package does not exist**

Run: `cd rental-housing-monitor && python -m pytest tests/test_models.py tests/test_filters.py -q`

Expected: import errors for `rental_monitor`.

- [ ] **Step 3: Implement immutable enums/model, URL normalization, canonical key, and allow-list filtering**

```python
@dataclass(frozen=True, slots=True)
class Announcement:
    source_id: str | None
    title: str
    agency: Agency
    region: str
    housing_type: HousingType
    target: str
    announcement_date: date
    application_start_date: date | None
    application_end_date: date | None
    url: str
```

The filter recognizes `서울/서울특별시` and `경기/경기도`, requires a recruitment term, rejects follow-up terms unless the title also identifies a corrected recruitment notice, and maps only the three approved housing types.

- [ ] **Step 4: Run the focused tests and then all current tests**

Run: `cd rental-housing-monitor && python -m pytest tests/test_models.py tests/test_filters.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit the domain slice**

```bash
git add rental-housing-monitor/pyproject.toml rental-housing-monitor/src rental-housing-monitor/tests
git commit -m "feat: add rental notice domain model"
```

### Task 2: SQLite repository and idempotent delivery state

**Files:**
- Create: `rental-housing-monitor/src/rental_monitor/repository.py`
- Create: `rental-housing-monitor/tests/test_repository.py`

**Interfaces:**
- Consumes: `Announcement`, `canonical_key`.
- Produces: `AnnouncementRepository(path)`, `initialize()`, `upsert_seen()`, `pending_for_chat()`, `mark_delivered()`, `start_run()`, and `finish_run()`.

- [ ] **Step 1: Write tests for first-seen, already-delivered, and failed-delivery retry behavior**

```python
def test_seen_but_undelivered_notice_remains_pending(repository, notice):
    repository.upsert_seen([notice])
    assert repository.pending_for_chat([notice], "42") == [notice]

def test_successful_delivery_is_not_pending_again(repository, notice):
    repository.upsert_seen([notice])
    repository.mark_delivered(notice, "42", 123)
    assert repository.pending_for_chat([notice], "42") == []
```

- [ ] **Step 2: Run the repository tests and verify missing implementation failure**

Run: `cd rental-housing-monitor && python -m pytest tests/test_repository.py -q`

Expected: import failure for `repository`.

- [ ] **Step 3: Implement schema creation and transactional repository methods**

Use `announcements`, `deliveries`, and `runs` tables with foreign keys, ISO-8601 UTC timestamps, `INSERT ... ON CONFLICT DO UPDATE`, and a `(announcement_key, chat_id)` delivery primary key.

- [ ] **Step 4: Run repository and complete current suite**

Run: `cd rental-housing-monitor && python -m pytest -q`

Expected: all tests pass.

- [ ] **Step 5: Commit persistence**

```bash
git add rental-housing-monitor/src/rental_monitor/repository.py rental-housing-monitor/tests/test_repository.py
git commit -m "feat: persist notice and delivery state"
```

### Task 3: Shared HTTP policy and LH official API collector

**Files:**
- Create: `rental-housing-monitor/src/rental_monitor/collectors/__init__.py`
- Create: `rental-housing-monitor/src/rental_monitor/collectors/base.py`
- Create: `rental-housing-monitor/src/rental_monitor/collectors/lh.py`
- Create: `rental-housing-monitor/tests/fixtures/lh_notices.json`
- Create: `rental-housing-monitor/tests/test_lh_collector.py`
- Create: `rental-housing-monitor/tests/test_http_retry.py`

**Interfaces:**
- Produces: `Collector` protocol, `CollectorError`, `ParserStructureError`, `request_with_retry(client, ...)`, and `LHCollector(client, service_key).collect()`.
- Consumes: official API fields including `PAN_ID`, `PAN_NM`, `AIS_TP_CD_NM`, `CNP_CD_NM`, `PAN_NT_ST_DT`, `PAN_NT_TO_DT`, `DTL_URL`, and `ALL_CNT`.

- [ ] **Step 1: Add failing fixture-based parser, paging, schema-error, and retry tests**

```python
def test_lh_collector_normalizes_official_response(httpx_mock):
    httpx_mock.add_response(json=load_fixture("lh_notices.json"))
    notices = LHCollector(httpx.Client(), "encoded-key").collect()
    assert notices[0].agency is Agency.LH
    assert notices[0].housing_type is HousingType.HAPPY

def test_lh_schema_change_names_lh_in_error(httpx_mock):
    httpx_mock.add_response(json={"unexpected": []})
    with pytest.raises(ParserStructureError, match="LH"):
        LHCollector(httpx.Client(), "key").collect()
```

- [ ] **Step 2: Run focused tests and verify missing collector failure**

Run: `cd rental-housing-monitor && python -m pytest tests/test_lh_collector.py tests/test_http_retry.py -q`

Expected: import failures for collector modules.

- [ ] **Step 3: Implement bounded paging, secret-safe requests, schema validation, and three-attempt exponential retry**

Call the official LH `분양임대공고문 조회 서비스` over HTTPS, send the key only as `ServiceKey`, request rental and housing-welfare notice categories, and deduplicate results by canonical key.

- [ ] **Step 4: Run focused and complete tests**

Run: `cd rental-housing-monitor && python -m pytest -q`

Expected: all tests pass.

- [ ] **Step 5: Commit LH collection**

```bash
git add rental-housing-monitor/src/rental_monitor/collectors rental-housing-monitor/tests
git commit -m "feat: collect official LH rental notices"
```

### Task 4: SH and GH official HTML collectors

**Files:**
- Create: `rental-housing-monitor/src/rental_monitor/collectors/sh.py`
- Create: `rental-housing-monitor/src/rental_monitor/collectors/gh.py`
- Create: `rental-housing-monitor/tests/fixtures/sh_list.html`
- Create: `rental-housing-monitor/tests/fixtures/sh_detail.html`
- Create: `rental-housing-monitor/tests/fixtures/gh_rental_list.html`
- Create: `rental-housing-monitor/tests/fixtures/gh_purchase_list.html`
- Create: `rental-housing-monitor/tests/fixtures/gh_detail.html`
- Create: `rental-housing-monitor/tests/test_sh_collector.py`
- Create: `rental-housing-monitor/tests/test_gh_collector.py`

**Interfaces:**
- Produces: `SHCollector(client).collect()` and `GHCollector(client).collect()`.
- Consumes: SH official housing-rental list/detail pages and GH official rental/purchased-rental list/detail pages.

- [ ] **Step 1: Capture minimal sanitized official response fixtures and write failing list/detail parser tests**

```python
def test_sh_missing_rows_without_empty_marker_is_structure_error():
    with pytest.raises(ParserStructureError, match="SH"):
        parse_sh_list("<html><body>changed</body></html>")

def test_gh_merges_duplicate_notice_from_two_official_lists(httpx_mock):
    notices = GHCollector(configured_client).collect()
    assert [notice.source_id for notice in notices].count("792") == 1
```

- [ ] **Step 2: Run focused tests and verify missing collectors fail**

Run: `cd rental-housing-monitor && python -m pytest tests/test_sh_collector.py tests/test_gh_collector.py -q`

Expected: imports fail for SH and GH collectors.

- [ ] **Step 3: Implement selectors anchored to semantic table headings and stable query IDs**

Resolve relative official links with `urljoin`, extract SH `seq` and GH `pbancNo` as source IDs, fetch details only for candidate recruitment posts, parse application periods when present, and explicitly distinguish official empty lists from changed markup.

- [ ] **Step 4: Run focused and complete tests**

Run: `cd rental-housing-monitor && python -m pytest -q`

Expected: all tests pass.

- [ ] **Step 5: Commit SH/GH collection**

```bash
git add rental-housing-monitor/src/rental_monitor/collectors rental-housing-monitor/tests
git commit -m "feat: collect official SH and GH notices"
```

### Task 5: Telegram client and orchestration

**Files:**
- Create: `rental-housing-monitor/src/rental_monitor/telegram.py`
- Create: `rental-housing-monitor/src/rental_monitor/runner.py`
- Create: `rental-housing-monitor/tests/test_telegram.py`
- Create: `rental-housing-monitor/tests/test_runner.py`

**Interfaces:**
- Produces: `TelegramClient.send(text) -> int`, `format_announcement()`, `split_messages()`, and `MonitorRunner.run() -> RunResult`.
- Consumes: collectors, repository, chat ID, and logger.

- [ ] **Step 1: Write failing tests for required fields, message splitting, no-new notice, partial failure, and delivery ordering**

```python
def test_runner_marks_delivery_only_after_telegram_success(...):
    telegram.send.return_value = 777
    runner.run()
    repository.mark_delivered.assert_called_once_with(notice, "42", 777)

def test_no_new_notice_is_sent_when_all_collectors_succeed(...):
    runner.run()
    telegram.send.assert_called_once_with("오늘은 신규 공고가 없습니다.")

def test_partial_failure_names_agency_instead_of_claiming_no_new(...):
    runner.run()
    assert "기관: SH" in telegram.sent_text
```

- [ ] **Step 2: Run focused tests and verify missing modules fail**

Run: `cd rental-housing-monitor && python -m pytest tests/test_telegram.py tests/test_runner.py -q`

Expected: import failures for Telegram and runner modules.

- [ ] **Step 3: Implement HTML-safe messages, 4096-character splitting, success-only delivery recording, and institution isolation**

The runner upserts all filtered observations, sends each pending notice, records only confirmed message IDs, sends the exact Korean no-new text only after three healthy collectors, and sends a structured failure summary for any collector error.

- [ ] **Step 4: Run focused and complete tests**

Run: `cd rental-housing-monitor && python -m pytest -q`

Expected: all tests pass.

- [ ] **Step 5: Commit delivery orchestration**

```bash
git add rental-housing-monitor/src/rental_monitor/telegram.py rental-housing-monitor/src/rental_monitor/runner.py rental-housing-monitor/tests
git commit -m "feat: send idempotent Telegram alerts"
```

### Task 6: Configuration, CLI, logging, workflow, and documentation

**Files:**
- Create: `rental-housing-monitor/src/rental_monitor/config.py`
- Create: `rental-housing-monitor/src/rental_monitor/logging_config.py`
- Create: `rental-housing-monitor/src/rental_monitor/__main__.py`
- Create: `rental-housing-monitor/tests/test_config.py`
- Create: `rental-housing-monitor/tests/test_workflow.py`
- Create: `rental-housing-monitor/.env.example`
- Create: `rental-housing-monitor/.gitignore`
- Create: `rental-housing-monitor/README.md`
- Create: `.github/workflows/rental-housing-monitor.yml`

**Interfaces:**
- Produces: `Settings.from_env()`, `configure_logging()`, `python -m rental_monitor`, and the daily workflow.
- Consumes: `DATA_GO_KR_SERVICE_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, optional `DATABASE_PATH`, and optional `LOG_PATH`.

- [ ] **Step 1: Write failing environment-validation and workflow static tests**

```python
def test_missing_secret_names_are_reported_without_values(monkeypatch):
    with pytest.raises(ConfigurationError, match="TELEGRAM_BOT_TOKEN"):
        Settings.from_env()

def test_workflow_runs_at_kst_noon_and_prevents_overlap(workflow):
    assert workflow["on"]["schedule"][0]["cron"] == "0 3 * * *"
    assert workflow["permissions"]["contents"] == "write"
    assert "concurrency" in workflow
```

- [ ] **Step 2: Run focused tests and verify missing implementation/workflow failures**

Run: `cd rental-housing-monitor && python -m pytest tests/test_config.py tests/test_workflow.py -q`

Expected: missing modules and workflow file.

- [ ] **Step 3: Implement settings, console/file logs, CLI wiring, secret-safe workflow, and data-branch persistence**

The workflow checks out code, restores `rental-housing-monitor/data/announcements.db` from `data` if present, runs the CLI, uploads `logs/monitor.log` with `if: always()`, and updates only the DB on `data` using a temporary worktree.

- [ ] **Step 4: Document local setup, official API application, Telegram configuration, GitHub Secrets, data branch initialization, execution, testing, and parser alerts**

`.env.example` contains names only:

```dotenv
DATA_GO_KR_SERVICE_KEY=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
DATABASE_PATH=data/announcements.db
LOG_PATH=logs/monitor.log
```

- [ ] **Step 5: Run full verification**

Run: `cd rental-housing-monitor && python -m pytest -q && python -m ruff check . && python -m compileall -q src`

Expected: zero failing tests, zero Ruff diagnostics, and exit code 0.

- [ ] **Step 6: Commit operational files**

```bash
git add rental-housing-monitor .github/workflows/rental-housing-monitor.yml
git commit -m "feat: schedule rental housing monitoring"
```

### Task 7: Requirements audit and live-source smoke checks

**Files:**
- Modify only if verification reveals a concrete defect in the files above.

**Interfaces:**
- Consumes: complete project and design specification.
- Produces: verified test/lint/compile results and documented limitations for unauthenticated live checks.

- [ ] **Step 1: Audit every design requirement against code, tests, README, and workflow**

Check official-source URLs, all eight output fields, stable identity, first-run behavior, no-new message, retry/logging, duplicate prevention, agency-specific parser alerts, KST schedule, tests, README, and `.env.example`.

- [ ] **Step 2: Run read-only live HTTP smoke checks where credentials are not required**

Verify SH/GH official list pages return expected structural markers. Do not send Telegram messages and do not expose API or bot secrets.

- [ ] **Step 3: Run fresh full verification after any fixes**

Run: `cd rental-housing-monitor && python -m pytest -q && python -m ruff check . && python -m compileall -q src && git diff --check`

Expected: all commands exit 0.

- [ ] **Step 4: Review Git status and report only project-related changes**

Run: `git status --short`

Expected: no unrelated tracked files modified; pre-existing `uyi-pet-run/` remains untouched.
