# Personal Monitor Core Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the deterministic domain, persistence, scheduler, rules, run orchestration, and delivery outbox used by every personal monitor.

**Architecture:** Add a new `personal_monitor` package beside the unchanged `rental_monitor` package. Pydantic owns the versioned configuration boundary; focused SQLite repositories own persistence; async ports isolate fetch and delivery implementations from the run engine.

**Tech Stack:** Python 3.12+, Pydantic 2, croniter, SQLite WAL, asyncio, pytest, Ruff.

## Global Constraints

- Do not modify existing `rental_monitor` behavior, fixtures, workflow, or QStash integration in this plan.
- Use `owner_id` on every user-owned aggregate and keep `MonitorSpec.schema_version=1`.
- Accept only `official_api`, `scrapling`, and `python_plugin` source adapter kinds.
- Accept only `auto`, `http`, `dynamic`, and `stealthy` fetch strategies.
- Default schedules to `0 */6 * * *` in `Asia/Seoul`; inspect 512 consecutive future occurrence gaps and reject any gap below 15 minutes.
- Add zero to 120 seconds of stable SHA-256-based jitter, except the rental monitor at 12:13 Asia/Seoul.
- Permit no Python expression, shell string, callable, or arbitrary plugin path in `MonitorSpec`.
- Record notifications in the outbox transaction before delivery and mark delivery only after a sender returns a message ID.
- Keep regular execution free of AI dependencies and imports.
- Use the existing root virtual environment through `../.venv/bin/python` from `rental-housing-monitor/`.

---

### Task 1: Add the package and dependency boundary

**Files:**
- Modify: `rental-housing-monitor/pyproject.toml`
- Create: `rental-housing-monitor/src/personal_monitor/__init__.py`
- Create: `rental-housing-monitor/src/personal_monitor/domain/__init__.py`
- Create: `rental-housing-monitor/tests/personal_monitor/test_package.py`

**Interfaces:**
- Consumes: the existing Hatch project and Python 3.12 floor.
- Produces: importable `personal_monitor`, version `0.1.0`, Pydantic, and croniter dependencies while retaining `rental_monitor` in the wheel.

- [ ] **Step 1: Write the failing package test**

```python
def test_personal_monitor_package_has_version() -> None:
    import personal_monitor

    assert personal_monitor.__version__ == "0.1.0"
```

- [ ] **Step 2: Run the focused test and verify the missing package failure**

Run: `cd rental-housing-monitor && ../.venv/bin/python -m pytest tests/personal_monitor/test_package.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'personal_monitor'`.

- [ ] **Step 3: Update project metadata and create the package**

Add these dependency entries without removing the existing ones:

```toml
"croniter>=6.0,<7",
"pydantic>=2.11,<3",
```

Replace the Hatch package list with:

```toml
[tool.hatch.build.targets.wheel]
packages = ["src/rental_monitor", "src/personal_monitor"]
```

Create `src/personal_monitor/__init__.py` with:

```python
__version__ = "0.1.0"
```

Create an empty `src/personal_monitor/domain/__init__.py`.

- [ ] **Step 4: Install the editable package and run the complete baseline**

Run: `cd rental-housing-monitor && ../.venv/bin/python -m pip install -e '.[dev]' && ../.venv/bin/python -m pytest -q`

Expected: the new package test and all 52 existing tests pass.

- [ ] **Step 5: Commit the package boundary**

```bash
git add rental-housing-monitor/pyproject.toml rental-housing-monitor/src/personal_monitor rental-housing-monitor/tests/personal_monitor/test_package.py
git commit -m "feat: add personal monitor package"
```

### Task 2: Define and validate `MonitorSpec`

**Files:**
- Create: `rental-housing-monitor/src/personal_monitor/domain/spec.py`
- Create: `rental-housing-monitor/tests/personal_monitor/domain/test_spec.py`

**Interfaces:**
- Consumes: Pydantic `BaseModel`, `ConfigDict`, `field_validator`, `model_validator`; croniter; `zoneinfo.ZoneInfo`.
- Produces: `MonitorSpec`, `ExtractSpec`, `FieldSpec`, `ValidatorSpec`, `RuleSpec`, `SourceAdapterKind`, `FetchStrategy`, `FieldType`, `RuleKind`, and `MonitorSpec.model_json_schema()`.

- [ ] **Step 1: Write failing strict-schema tests**

```python
from pydantic import ValidationError
import pytest

from personal_monitor.domain.spec import MonitorSpec


def valid_spec() -> dict[str, object]:
    return {
        "schema_version": 1,
        "owner_id": "telegram-user:123456789",
        "name": "상품 가격 감시",
        "target_url": "https://example.com/product/123",
        "source_adapter": "scrapling",
        "adapter_ref": None,
        "fetch_strategy": "auto",
        "schedule": "0 */6 * * *",
        "timezone": "Asia/Seoul",
        "extract": {
            "item_scope": "main",
            "fields": {
                "title": {"selector": "h1", "type": "text", "required": True},
                "price": {"selector": ".price", "type": "krw", "required": True},
            },
        },
        "validators": {
            "min_items": 1,
            "max_items": 1,
            "allowed_link_domains": ["example.com"],
        },
        "rules": [
            {
                "kind": "numeric_threshold",
                "field": "price",
                "operator": "lte",
                "value": 100000,
            }
        ],
        "notify_on_no_change": False,
        "auth_profile_ref": None,
    }


def test_monitor_spec_round_trips() -> None:
    spec = MonitorSpec.model_validate(valid_spec())
    assert spec.model_dump(mode="json", exclude_unset=True) == valid_spec()


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("schema_version",), 2),
        (("target_url",), "file:///etc/passwd"),
        (("schedule",), "*/5 * * * *"),
        (("extract", "fields", "price", "selector"), "__import__('os').system('id')"),
    ],
)
def test_monitor_spec_rejects_unsafe_values(path: tuple[str, ...], value: object) -> None:
    payload = valid_spec()
    target = payload
    for key in path[:-1]:
        target = target[key]  # type: ignore[index,assignment]
    target[path[-1]] = value  # type: ignore[index]
    with pytest.raises(ValidationError):
        MonitorSpec.model_validate(payload)


def test_monitor_spec_rejects_unknown_fields() -> None:
    payload = valid_spec() | {"python_code": "print('unsafe')"}
    with pytest.raises(ValidationError, match="extra_forbidden"):
        MonitorSpec.model_validate(payload)
```

- [ ] **Step 2: Run the schema tests and verify the missing module failure**

Run: `cd rental-housing-monitor && ../.venv/bin/python -m pytest tests/personal_monitor/domain/test_spec.py -q`

Expected: FAIL importing `personal_monitor.domain.spec`.

- [ ] **Step 3: Implement the closed configuration model**

Use one strict base class and these exact enums and fields:

```python
from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from urllib.parse import parse_qsl, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class SourceAdapterKind(StrEnum):
    OFFICIAL_API = "official_api"
    SCRAPLING = "scrapling"
    PYTHON_PLUGIN = "python_plugin"


class FetchStrategy(StrEnum):
    AUTO = "auto"
    HTTP = "http"
    DYNAMIC = "dynamic"
    STEALTHY = "stealthy"


class FieldType(StrEnum):
    TEXT = "text"
    INTEGER = "integer"
    DECIMAL = "decimal"
    KRW = "krw"
    DATE = "date"
    DATETIME = "datetime"
    BOOLEAN = "boolean"
    URL = "url"


class RuleKind(StrEnum):
    NEW_ITEM = "new_item"
    FIELD_CHANGED = "field_changed"
    NUMERIC_THRESHOLD = "numeric_threshold"
    STATUS_EQUALS = "status_equals"
    KEYWORD_MATCH = "keyword_match"


class MonitorStatus(StrEnum):
    ACTIVE = "active"
    PAUSED_USER = "paused_user"
    PAUSED_AUTH = "paused_auth"
    NEEDS_REVIEW = "needs_review"
    DISABLED = "disabled"


SAFE_SELECTOR = re.compile(r"^[\w\s.#>*+~:\-\[\]=\"'()/@|]+$")
SAFE_FIELD = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
MIN_SCHEDULE_INTERVAL_SECONDS = 900
SCHEDULE_GAP_SAMPLE_COUNT = 512
SENSITIVE_QUERY_PARAMETER_NAMES = frozenset(
    {
        "access_token", "api_key", "apikey", "auth", "authorization", "client_secret",
        "credentials", "key", "passwd", "password", "secret", "session", "signature", "token",
    }
)


class FieldSpec(StrictModel):
    selector: Annotated[str, Field(min_length=1, max_length=500)]
    type: FieldType
    required: bool = True
    attribute: Annotated[str | None, Field(max_length=80)] = None
    pattern: Annotated[str | None, Field(max_length=300)] = None

    @field_validator("selector")
    @classmethod
    def selector_is_declarative(cls, value: str) -> str:
        if not SAFE_SELECTOR.fullmatch(value) or any(
            token in value for token in ("__", "import", "lambda", ";", "`", "${")
        ):
            raise ValueError("selector must be declarative CSS/XPath")
        return value


class ExtractSpec(StrictModel):
    item_scope: Annotated[str, Field(min_length=1, max_length=500)]
    fields: dict[Annotated[str, Field(pattern=SAFE_FIELD.pattern)], FieldSpec]

    @field_validator("item_scope")
    @classmethod
    def item_scope_is_declarative(cls, value: str) -> str:
        return FieldSpec.selector_is_declarative(value)


class ValidatorSpec(StrictModel):
    min_items: Annotated[int, Field(ge=0, le=10_000)] = 1
    max_items: Annotated[int, Field(ge=1, le=10_000)] = 1
    allowed_link_domains: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("allowed_link_domains")
    @classmethod
    def domains_are_exact_hosts(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            host = value.rstrip(".").casefold()
            if not re.fullmatch(r"[a-z0-9.-]+", host) or ".." in host or host.startswith("."):
                raise ValueError("allowed link domains must be exact ASCII hostnames")
            normalized.append(host)
        return sorted(set(normalized))

    @model_validator(mode="after")
    def item_range_is_ordered(self) -> ValidatorSpec:
        if self.min_items > self.max_items:
            raise ValueError("min_items must not exceed max_items")
        return self


class RuleSpec(StrictModel):
    kind: RuleKind
    field: str | None = None
    operator: Literal["lt", "lte", "eq", "gte", "gt"] | None = None
    value: str | int | float | bool | None = None
    keywords: list[str] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def required_arguments_match_kind(self) -> RuleSpec:
        if self.kind is RuleKind.NEW_ITEM:
            if self.field is not None or self.operator is not None or self.value is not None or self.keywords:
                raise ValueError("new_item takes no arguments")
        elif self.kind is RuleKind.FIELD_CHANGED:
            if self.field is None or self.operator is not None or self.value is not None or self.keywords:
                raise ValueError("field_changed requires only field")
        elif self.kind is RuleKind.NUMERIC_THRESHOLD:
            if (
                self.field is None
                or self.operator is None
                or isinstance(self.value, bool)
                or not isinstance(self.value, int | float)
                or self.keywords
            ):
                raise ValueError("numeric_threshold requires field, operator, and numeric value")
        elif self.kind is RuleKind.KEYWORD_MATCH:
            if self.field is None or not self.keywords or self.operator is not None or self.value is not None:
                raise ValueError("keyword_match requires field and keywords")
        elif self.kind is RuleKind.STATUS_EQUALS:
            if self.field is None or self.value is None or self.operator is not None or self.keywords:
                raise ValueError("status_equals requires field and value")
        return self


class MonitorSpec(StrictModel):
    schema_version: Literal[1]
    owner_id: Annotated[str, Field(min_length=1, max_length=120)]
    name: Annotated[str, Field(min_length=1, max_length=120)]
    target_url: Annotated[str, Field(min_length=8, max_length=2048)]
    source_adapter: SourceAdapterKind
    adapter_ref: Annotated[str | None, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")] = None
    fetch_strategy: FetchStrategy = FetchStrategy.AUTO
    schedule: str = "0 */6 * * *"
    timezone: str = "Asia/Seoul"
    extract: ExtractSpec
    validators: ValidatorSpec
    rules: list[RuleSpec] = Field(min_length=1, max_length=20)
    notify_on_no_change: bool = False
    auth_profile_ref: Annotated[str | None, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")] = None

    @field_validator("target_url")
    @classmethod
    def url_has_public_web_scheme(cls, value: str) -> str:
        parts = urlsplit(value)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
        ):
            raise ValueError("target_url must be an http(s) URL without userinfo")
        if any(name.casefold() in SENSITIVE_QUERY_PARAMETER_NAMES for name, _ in parse_qsl(parts.query, keep_blank_values=True)):
            raise ValueError("target_url query contains a credential-like parameter")
        return value

    @model_validator(mode="after")
    def schedule_is_valid_and_bounded(self) -> MonitorSpec:
        try:
            zone = ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as error:
            raise ValueError("unknown timezone") from error
        base = datetime(2026, 1, 1, tzinfo=zone)
        iterator = croniter(self.schedule, base)
        previous = iterator.get_next(datetime)
        for _ in range(SCHEDULE_GAP_SAMPLE_COUNT):
            current = iterator.get_next(datetime)
            if (current - previous).total_seconds() < MIN_SCHEDULE_INTERVAL_SECONDS:
                raise ValueError("schedule interval must be at least 15 minutes")
            previous = current
        if self.source_adapter is SourceAdapterKind.SCRAPLING and self.adapter_ref is not None:
            raise ValueError("scrapling does not accept adapter_ref")
        if self.source_adapter is not SourceAdapterKind.SCRAPLING and self.adapter_ref is None:
            raise ValueError("official_api and python_plugin require adapter_ref")
        return self
```

- [ ] **Step 4: Run the schema test and export a schema fixture**

Run: `cd rental-housing-monitor && ../.venv/bin/python -m pytest tests/personal_monitor/domain/test_spec.py -q`

Expected: all tests pass, including strict unknown-field rejection.

- [ ] **Step 5: Commit the configuration contract**

```bash
git add rental-housing-monitor/src/personal_monitor/domain/spec.py rental-housing-monitor/tests/personal_monitor/domain/test_spec.py
git commit -m "feat: define monitor specification contract"
```

### Task 3: Normalize observations and evaluate rules

**Files:**
- Create: `rental-housing-monitor/src/personal_monitor/domain/observation.py`
- Create: `rental-housing-monitor/src/personal_monitor/domain/rules.py`
- Create: `rental-housing-monitor/tests/personal_monitor/domain/test_observation.py`
- Create: `rental-housing-monitor/tests/personal_monitor/domain/test_rules.py`

**Interfaces:**
- Consumes: `MonitorSpec`, `RuleSpec`, normalized field dictionaries.
- Produces: `ObservedItem`, `ObservationBatch`, `Change`, `RuleMatch`, `stable_item_id()`, `content_hash()`, `diff_items()`, and `evaluate_rules()`.

- [ ] **Step 1: Write failing identity, diff, and rule tests**

```python
from personal_monitor.domain.observation import ObservedItem, diff_items, stable_item_id
from personal_monitor.domain.rules import evaluate_rules
from personal_monitor.domain.spec import RuleSpec


def test_item_id_prefers_source_id_then_normalized_url() -> None:
    assert stable_item_id({"source_id": "A-7", "url": "https://example.com/a"}) == "source:A-7"
    assert stable_item_id({"url": "https://EXAMPLE.com/a?utm_source=x"}) == stable_item_id(
        {"url": "https://example.com/a"}
    )


def test_diff_reports_changed_field() -> None:
    previous = [ObservedItem(item_id="p1", fields={"price": 120000})]
    current = [ObservedItem(item_id="p1", fields={"price": 99000})]
    assert diff_items(previous, current)[0].changed_fields == {"price": (120000, 99000)}


def test_threshold_matches_only_on_crossing() -> None:
    rule = RuleSpec(kind="numeric_threshold", field="price", operator="lte", value=100000)
    matches = evaluate_rules(
        [rule],
        previous=ObservedItem(item_id="p1", fields={"price": 120000}),
        current=ObservedItem(item_id="p1", fields={"price": 99000}),
        is_new=False,
    )
    assert [match.kind for match in matches] == ["numeric_threshold"]
```

- [ ] **Step 2: Run tests and verify missing modules**

Run: `cd rental-housing-monitor && ../.venv/bin/python -m pytest tests/personal_monitor/domain/test_observation.py tests/personal_monitor/domain/test_rules.py -q`

Expected: FAIL importing the new modules.

- [ ] **Step 3: Implement immutable observation and change types**

```python
@dataclass(frozen=True, slots=True)
class ObservedItem:
    item_id: str
    fields: dict[str, Scalar]


@dataclass(frozen=True, slots=True)
class SourceWarning:
    source: str
    stage: str
    detail: str


@dataclass(frozen=True, slots=True)
class ObservationBatch:
    monitor_id: str
    items: tuple[ObservedItem, ...]
    observed_at: datetime
    source_hash: str
    source_status: dict[str, str] = field(default_factory=dict)
    warnings: tuple[SourceWarning, ...] = ()


@dataclass(frozen=True, slots=True)
class Change:
    item_id: str
    is_new: bool
    removed: bool
    changed_fields: dict[str, tuple[Scalar | None, Scalar | None]]
```

Define `Scalar = str | int | float | bool | None`. `stable_item_id()` returns `f"source:{source_id}"` when present, otherwise SHA-256 of the normalized URL, otherwise SHA-256 of canonical JSON for sorted core fields. `content_hash()` hashes canonical UTF-8 JSON with `sort_keys=True` and compact separators. `diff_items()` emits new, removed, and changed items in sorted `item_id` order.

- [ ] **Step 4: Implement closed rule evaluation**

```python
@dataclass(frozen=True, slots=True)
class RuleMatch:
    kind: RuleKind
    field: str | None
    previous: Scalar
    current: Scalar


def evaluate_rules(
    rules: Sequence[RuleSpec],
    *,
    previous: ObservedItem | None,
    current: ObservedItem,
    is_new: bool,
) -> list[RuleMatch]:
    matches: list[RuleMatch] = []
    for rule in rules:
        old = previous.fields.get(rule.field) if previous and rule.field else None
        new = current.fields.get(rule.field) if rule.field else None
        if rule.kind is RuleKind.NEW_ITEM and is_new:
            matches.append(RuleMatch(rule.kind, None, None, None))
        elif rule.kind is RuleKind.FIELD_CHANGED and old != new:
            matches.append(RuleMatch(rule.kind, rule.field, old, new))
        elif rule.kind is RuleKind.NUMERIC_THRESHOLD and _crossed(old, new, rule.operator, rule.value):
            matches.append(RuleMatch(rule.kind, rule.field, old, new))
        elif rule.kind is RuleKind.STATUS_EQUALS and old != rule.value and new == rule.value:
            matches.append(RuleMatch(rule.kind, rule.field, old, new))
        elif rule.kind is RuleKind.KEYWORD_MATCH and _new_keyword(old, new, rule.keywords):
            matches.append(RuleMatch(rule.kind, rule.field, old, new))
    return matches
```

`_crossed()` returns true only when the current value satisfies the comparator and the previous value was absent or did not satisfy it. `_new_keyword()` uses Unicode `casefold()` and matches when at least one configured keyword appears now but none appeared previously.

- [ ] **Step 5: Run domain tests and commit**

Run: `cd rental-housing-monitor && ../.venv/bin/python -m pytest tests/personal_monitor/domain -q`

Expected: all domain tests pass.

```bash
git add rental-housing-monitor/src/personal_monitor/domain rental-housing-monitor/tests/personal_monitor/domain
git commit -m "feat: evaluate deterministic monitor rules"
```

### Task 4: Create the versioned SQLite registry and runtime store

**Files:**
- Create: `rental-housing-monitor/src/personal_monitor/storage/__init__.py`
- Create: `rental-housing-monitor/src/personal_monitor/storage/schema.py`
- Create: `rental-housing-monitor/src/personal_monitor/storage/registry.py`
- Create: `rental-housing-monitor/src/personal_monitor/storage/runtime.py`
- Create: `rental-housing-monitor/tests/personal_monitor/storage/test_registry.py`
- Create: `rental-housing-monitor/tests/personal_monitor/storage/test_runtime.py`

**Interfaces:**
- Consumes: `MonitorSpec`, `ObservationBatch`, `RuleMatch`, UTC datetimes.
- Produces: `open_database(path)`, `RegistryRepository`, `RuntimeRepository`, transactional monitor version activation, leases, runs, observations, outbox, deliveries, and pending actions.

- [ ] **Step 1: Write failing transaction and idempotency tests**

```python
def test_unapproved_version_cannot_become_active(registry, monitor_spec) -> None:
    monitor_id = registry.create_monitor(monitor_spec, created_by="telegram-user:123456789")
    candidate = registry.add_version(monitor_id, monitor_spec, created_by="codex", approved=False)
    with pytest.raises(ValueError, match="approved"):
        registry.activate_version(monitor_id, candidate)


def test_delivery_key_is_idempotent(runtime) -> None:
    first = runtime.enqueue_delivery(
        dedupe_key="m1:p1:numeric_threshold:price:99000",
        monitor_id="m1",
        target_id="t1",
        payload={"text": "가격이 99,000원입니다"},
    )
    second = runtime.enqueue_delivery(
        dedupe_key="m1:p1:numeric_threshold:price:99000",
        monitor_id="m1",
        target_id="t1",
        payload={"text": "가격이 99,000원입니다"},
    )
    assert first == second
    assert runtime.connection.execute("SELECT count(*) FROM outbox").fetchone()[0] == 1


def test_owner_scoped_lists_never_return_another_users_monitor(registry, two_owner_specs) -> None:
    first = registry.create_monitor(two_owner_specs[0], created_by="telegram-user:1")
    registry.create_monitor(two_owner_specs[1], created_by="telegram-user:2")
    assert [row.id for row in registry.list_monitors("telegram-user:1")] == [first]
```

- [ ] **Step 2: Run storage tests and verify missing modules**

Run: `cd rental-housing-monitor && ../.venv/bin/python -m pytest tests/personal_monitor/storage -q`

Expected: FAIL importing `personal_monitor.storage`.

- [ ] **Step 3: Implement connection policy and schema migration 1**

`open_database()` must create the parent directory, set `row_factory=sqlite3.Row`, and execute:

```sql
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;

CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE users(
  id TEXT PRIMARY KEY, telegram_user_id INTEGER NOT NULL UNIQUE,
  status TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE delivery_targets(
  id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, kind TEXT NOT NULL,
  address TEXT NOT NULL, created_at TEXT NOT NULL,
  UNIQUE(owner_id, kind, address), FOREIGN KEY(owner_id) REFERENCES users(id)
);
CREATE TABLE monitors(
  id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, name TEXT NOT NULL,
  status TEXT NOT NULL, active_version_id TEXT, next_run_at TEXT,
  lease_owner TEXT, lease_expires_at TEXT, disabled_at TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  FOREIGN KEY(owner_id) REFERENCES users(id)
);
CREATE TABLE monitor_versions(
  id TEXT PRIMARY KEY, monitor_id TEXT NOT NULL, version_number INTEGER NOT NULL,
  spec_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at TEXT NOT NULL,
  approved_by TEXT, approved_at TEXT, UNIQUE(monitor_id, version_number),
  FOREIGN KEY(monitor_id) REFERENCES monitors(id)
);
CREATE TABLE observations(
  monitor_id TEXT NOT NULL, item_id TEXT NOT NULL, fields_json TEXT NOT NULL,
  content_hash TEXT NOT NULL, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
  PRIMARY KEY(monitor_id, item_id), FOREIGN KEY(monitor_id) REFERENCES monitors(id)
);
CREATE TABLE runs(
  id TEXT PRIMARY KEY, monitor_id TEXT NOT NULL, version_id TEXT NOT NULL,
  stage TEXT NOT NULL, fetch_strategy TEXT, status TEXT NOT NULL,
  started_at TEXT NOT NULL, finished_at TEXT, error_class TEXT, error_detail TEXT,
  FOREIGN KEY(monitor_id) REFERENCES monitors(id)
);
CREATE TABLE outbox(
  id TEXT PRIMARY KEY, dedupe_key TEXT NOT NULL UNIQUE, monitor_id TEXT NOT NULL,
  target_id TEXT NOT NULL, payload_json TEXT NOT NULL, status TEXT NOT NULL,
  attempt_count INTEGER NOT NULL DEFAULT 0, available_at TEXT NOT NULL,
  last_error TEXT, created_at TEXT NOT NULL,
  FOREIGN KEY(monitor_id) REFERENCES monitors(id),
  FOREIGN KEY(target_id) REFERENCES delivery_targets(id)
);
CREATE TABLE deliveries(
  outbox_id TEXT PRIMARY KEY, target_id TEXT NOT NULL, external_message_id TEXT NOT NULL,
  delivered_at TEXT NOT NULL, FOREIGN KEY(outbox_id) REFERENCES outbox(id)
);
CREATE TABLE pending_actions(
  token_hash TEXT PRIMARY KEY, owner_id TEXT NOT NULL, action TEXT NOT NULL,
  payload_json TEXT NOT NULL, expires_at TEXT NOT NULL, consumed_at TEXT,
  FOREIGN KEY(owner_id) REFERENCES users(id)
);
CREATE TABLE credential_refs(
  id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, kind TEXT NOT NULL,
  vault_key TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL,
  FOREIGN KEY(owner_id) REFERENCES users(id)
);
CREATE INDEX monitors_due_idx ON monitors(status, next_run_at);
CREATE INDEX outbox_due_idx ON outbox(status, available_at);
CREATE INDEX runs_monitor_started_idx ON runs(monitor_id, started_at);
```

Insert migration version 1 only after the transaction succeeds.

- [ ] **Step 4: Implement registry operations**

`RegistryRepository` exposes these exact methods:

```python
@dataclass(frozen=True, slots=True)
class ActiveMonitor:
    id: str
    owner_id: str
    version_id: str
    spec: MonitorSpec


@dataclass(frozen=True, slots=True)
class DeliveryTargetRow:
    id: str
    owner_id: str
    kind: str
    address: str


@dataclass(frozen=True, slots=True)
class MonitorRow:
    id: str
    owner_id: str
    name: str
    status: MonitorStatus
    next_run_at: datetime | None


def create_user(self, user_id: str, telegram_user_id: int) -> None: ...
def create_delivery_target(self, target_id: str, owner_id: str, address: str) -> None: ...
def create_monitor(self, spec: MonitorSpec, *, created_by: str) -> str: ...
def add_version(
    self, monitor_id: str, spec: MonitorSpec, *, created_by: str, approved: bool
) -> str: ...
def approve_version(self, version_id: str, *, approved_by: str) -> None: ...
def activate_version(self, monitor_id: str, version_id: str) -> None: ...
def get_active_spec(self, monitor_id: str) -> MonitorSpec: ...
def get_active_monitor(self, monitor_id: str) -> ActiveMonitor: ...
def get_primary_target(self, owner_id: str) -> DeliveryTargetRow: ...
def list_monitors(self, owner_id: str, *, include_disabled: bool = False) -> list[MonitorRow]: ...
def transition_status(self, monitor_id: str, expected: MonitorStatus, target: MonitorStatus) -> None: ...
def soft_delete(self, monitor_id: str, *, disabled_at: datetime) -> None: ...
```

Use `BEGIN IMMEDIATE` for version numbering and activation. An immediate operation must reject an already-active caller-owned transaction instead of degrading to a savepoint; non-immediate operations may use a savepoint without committing the caller's transaction. `create_monitor()` creates version 1 already approved by `created_by`, then sets it active in the same transaction. `activate_version()` verifies that version belongs to the monitor and has non-null `approved_at`.

- [ ] **Step 5: Implement runtime operations**

`RuntimeRepository` exposes these exact methods:

```python
@dataclass(frozen=True, slots=True)
class OutboxRow:
    id: str
    target_id: str
    payload: dict[str, object]
    attempt_count: int


def claim_due(self, *, worker_id: str, now: datetime, lease_seconds: int = 300) -> list[str]: ...
def release_lease(self, monitor_id: str, *, worker_id: str, next_run_at: datetime) -> None: ...
def start_run(self, monitor_id: str, version_id: str, *, started_at: datetime) -> str: ...
def finish_run(self, run_id: str, *, status: str, stage: str, error_class: str | None = None, error_detail: str | None = None) -> None: ...
def load_items(self, monitor_id: str) -> list[ObservedItem]: ...
def upsert_items(self, batch: ObservationBatch) -> None: ...
def enqueue_delivery(self, *, dedupe_key: str, monitor_id: str, target_id: str, payload: dict[str, object]) -> str: ...
def due_outbox(self, *, now: datetime, limit: int = 50) -> list[OutboxRow]: ...
def mark_delivered(self, outbox_id: str, *, message_id: str, delivered_at: datetime) -> None: ...
def reschedule_outbox(self, outbox_id: str, *, available_at: datetime, error: str) -> None: ...
```

All JSON uses `ensure_ascii=False`, `sort_keys=True`, and compact separators. IDs use `uuid.uuid4().hex`; timestamps are timezone-aware UTC ISO-8601 strings. Stored `error_detail` and `last_error` values are closed safe diagnostic codes, never arbitrary text. `error_detail` may be `None`; otherwise `finish_run(error_detail=...)` and `reschedule_outbox(error=...)` accept only `required_field_missing`, `validation_failed`, `connection_timeout`, `network_error`, `authentication_failed`, `structure_changed`, `policy_rejected`, `delivery_failed`, `internal_error`, `timeout`, or `offline`. Never persist URL queries, cookies, response bodies, exception reprs, HTML, identifiers, or raw exception messages in those columns.

- [ ] **Step 6: Run storage and full tests, then commit**

Run: `cd rental-housing-monitor && ../.venv/bin/python -m pytest tests/personal_monitor/storage -q && ../.venv/bin/python -m pytest -q`

Expected: all tests pass and the existing 52 tests remain unchanged.

```bash
git add rental-housing-monitor/src/personal_monitor/storage rental-housing-monitor/tests/personal_monitor/storage
git commit -m "feat: persist versioned monitor state"
```

### Task 5: Add deterministic scheduling and lease recovery

**Files:**
- Create: `rental-housing-monitor/src/personal_monitor/engine/__init__.py`
- Create: `rental-housing-monitor/src/personal_monitor/engine/scheduler.py`
- Create: `rental-housing-monitor/tests/personal_monitor/engine/test_scheduler.py`

**Interfaces:**
- Consumes: `MonitorSpec.schedule`, `MonitorSpec.timezone`, monitor ID, UTC time, `RuntimeRepository.claim_due()`.
- Produces: `next_run_at(spec, monitor_id, after) -> datetime`, `stable_jitter_seconds(monitor_id) -> int`, and `Scheduler.tick(now) -> list[str]`.

- [ ] **Step 1: Write failing timezone, jitter, and lease tests**

```python
def test_next_run_uses_monitor_timezone_and_stable_jitter(monitor_spec) -> None:
    result = next_run_at(
        monitor_spec,
        "monitor-7",
        datetime(2026, 7, 22, 0, 1, tzinfo=UTC),
    )
    assert result == datetime(2026, 7, 22, 3, 0, tzinfo=UTC) + timedelta(
        seconds=stable_jitter_seconds("monitor-7")
    )


def test_expired_lease_can_be_reclaimed(runtime, active_monitor) -> None:
    first = runtime.claim_due(worker_id="worker-a", now=NOW)
    second = runtime.claim_due(worker_id="worker-b", now=NOW + timedelta(seconds=301))
    assert first == [active_monitor]
    assert second == [active_monitor]
```

- [ ] **Step 2: Run focused tests and verify missing scheduler**

Run: `cd rental-housing-monitor && ../.venv/bin/python -m pytest tests/personal_monitor/engine/test_scheduler.py -q`

Expected: FAIL importing `personal_monitor.engine.scheduler`.

- [ ] **Step 3: Implement cron calculation and stable jitter**

```python
def stable_jitter_seconds(monitor_id: str) -> int:
    return int.from_bytes(hashlib.sha256(monitor_id.encode()).digest()[:2], "big") % 121


def next_run_at(spec: MonitorSpec, monitor_id: str, after: datetime) -> datetime:
    zone = ZoneInfo(spec.timezone)
    local_after = after.astimezone(zone)
    scheduled = croniter(spec.schedule, local_after).get_next(datetime)
    is_rental_exact = (
        monitor_id == "rental-housing-seoul-gyeonggi" and spec.schedule == "13 12 * * *"
    )
    jitter = 0 if is_rental_exact else stable_jitter_seconds(monitor_id)
    return (scheduled + timedelta(seconds=jitter)).astimezone(UTC)
```

`Scheduler.tick()` claims due rows once, returns their IDs for the worker loop, and never executes a monitor itself. Lease duration is 300 seconds and each release checks `lease_owner` to prevent another worker from clearing the lease.

- [ ] **Step 4: Run scheduler and storage tests, then commit**

Run: `cd rental-housing-monitor && ../.venv/bin/python -m pytest tests/personal_monitor/engine/test_scheduler.py tests/personal_monitor/storage -q`

Expected: all tests pass.

```bash
git add rental-housing-monitor/src/personal_monitor/engine rental-housing-monitor/tests/personal_monitor/engine/test_scheduler.py
git commit -m "feat: schedule monitor runs with leases"
```

### Task 6: Orchestrate runs and idempotent delivery

**Files:**
- Create: `rental-housing-monitor/src/personal_monitor/ports.py`
- Create: `rental-housing-monitor/src/personal_monitor/engine/errors.py`
- Create: `rental-housing-monitor/src/personal_monitor/engine/runner.py`
- Create: `rental-housing-monitor/src/personal_monitor/engine/outbox.py`
- Create: `rental-housing-monitor/tests/personal_monitor/engine/test_runner.py`
- Create: `rental-housing-monitor/tests/personal_monitor/engine/test_outbox.py`
- Create: `rental-housing-monitor/tests/personal_monitor/test_ai_boundary.py`

**Interfaces:**
- Consumes: adapters, validators, observation diff/rules, registry/runtime repositories, delivery sender.
- Produces: async `SourceAdapter.fetch(monitor_id, spec)`, `AdapterRegistry.resolve(kind, adapter_ref)`, `DeliverySender.send(address, payload)`, `OperatorHealthSink.emit_once(dedupe_key, payload)`, `RegistryRepository.get_delivery_target(target_id)`, `MonitorRunner.run(monitor_id)`, and `OutboxWorker.drain_once()`.

- [ ] **Step 1: Write failing execution-boundary tests**

```python
async def test_regular_run_never_imports_ai(runtime_fixture, fake_adapter, fake_sender) -> None:
    import inspect
    import personal_monitor.engine.runner as runner_module

    assert "personal_monitor.ai" not in inspect.getsource(runner_module)
    runner = runtime_fixture.runner(adapter=fake_adapter, sender=fake_sender)
    result = await runner.run(runtime_fixture.monitor_id)
    assert result.status == "success"


async def test_send_failure_keeps_outbox_pending(runtime_fixture) -> None:
    worker = runtime_fixture.outbox_worker(sender=FailingSender("offline"))
    await worker.drain_once(now=NOW)
    row = runtime_fixture.runtime.connection.execute("SELECT status, attempt_count FROM outbox").fetchone()
    assert tuple(row) == ("pending", 1)
```

- [ ] **Step 2: Run engine tests and verify missing ports/runner**

Run: `cd rental-housing-monitor && ../.venv/bin/python -m pytest tests/personal_monitor/engine/test_runner.py tests/personal_monitor/engine/test_outbox.py tests/personal_monitor/test_ai_boundary.py -q`

Expected: FAIL importing the new interfaces.

- [ ] **Step 3: Define async ports and closed errors**

```python
class SourceAdapter(Protocol):
    async def fetch(self, monitor_id: str, spec: MonitorSpec) -> ObservationBatch: ...


class AdapterRegistry(Protocol):
    def resolve(self, kind: SourceAdapterKind, adapter_ref: str | None) -> SourceAdapter: ...


class DeliverySender(Protocol):
    async def send(self, address: str, payload: dict[str, object]) -> str: ...


class OperatorHealthSink(Protocol):
    async def emit_once(self, dedupe_key: str, payload: dict[str, object]) -> None: ...


class Clock(Protocol):
    def now(self) -> datetime: ...


class ErrorClass(StrEnum):
    TRANSIENT_NETWORK = "transient_network"
    AUTHENTICATION = "authentication"
    STRUCTURE = "structure"
    VALIDATION = "validation"
    POLICY = "policy"
    DELIVERY = "delivery"
    INTERNAL = "internal"


class MonitorError(RuntimeError):
    def __init__(self, error_class: ErrorClass, stage: str, safe_detail: str) -> None:
        super().__init__(safe_detail)
        self.error_class = error_class
        self.stage = stage
        self.safe_detail = safe_detail
```

- [ ] **Step 4: Implement the run transaction sequence**

`MonitorRunner.run()` must perform this exact order. `RunResult` is a frozen dataclass with `status: Literal["success", "partial_failure", "failed"]`, `matched_count: int`, and `warning_count: int`; `render_payload(spec, item, match)`, `render_warning(spec, warning, source_status)`, and `delivery_key(monitor_id, item, match)` are pure functions in `runner.py`:

```python
active = registry.get_active_monitor(monitor_id)
spec = active.spec
target = registry.get_primary_target(active.owner_id)
run_id = runtime.start_run(monitor_id, active.version_id, started_at=clock.now())
batch = await adapters.resolve(spec.source_adapter, spec.adapter_ref).fetch(monitor_id, spec)
previous = runtime.load_items(monitor_id)
changes = diff_items(previous, list(batch.items))
runtime.upsert_items(batch)
matched_count = 0
for change in changes:
    current = current_by_id.get(change.item_id)
    if current is None:
        continue
    matches = evaluate_rules(
        spec.rules,
        previous=previous_by_id.get(change.item_id),
        current=current,
        is_new=change.is_new,
    )
    for match in matches:
        matched_count += 1
        runtime.enqueue_delivery(
            dedupe_key=delivery_key(monitor_id, current, match),
            monitor_id=monitor_id,
            target_id=target.id,
            payload=render_payload(spec, current, match),
        )
if batch.warnings:
    for warning in batch.warnings:
        runtime.enqueue_delivery(
            dedupe_key=f"{monitor_id}:warning:{batch.observed_at.date()}:{warning.source}:{warning.stage}",
            monitor_id=monitor_id,
            target_id=target.id,
            payload=render_warning(spec, warning, batch.source_status),
        )
    run_status = "partial_failure"
elif matched_count == 0 and spec.notify_on_no_change:
    local_date = batch.observed_at.astimezone(ZoneInfo(spec.timezone)).date()
    runtime.enqueue_delivery(
        dedupe_key=f"{monitor_id}:no-change:{local_date}",
        monitor_id=monitor_id,
        target_id=target.id,
        payload={"text": "오늘은 신규 공고가 없습니다."},
    )
    run_status = "success"
else:
    run_status = "success"
runtime.finish_run(run_id, status=run_status, stage="complete")
runtime.release_lease(
    monitor_id,
    worker_id=worker_id,
    next_run_at=next_run_at(spec, monitor_id, clock.now()),
)
```

Map `MonitorError` to state transitions: authentication → `paused_auth`; structure/validation → `needs_review`; policy → `needs_review`; transient network → keep active and schedule retry; internal → `needs_review`. Persist only the closed diagnostic code derived from `ErrorClass` (`network_error`, `authentication_failed`, `structure_changed`, `validation_failed`, `policy_rejected`, `delivery_failed`, or `internal_error`); never persist `MonitorError.safe_detail`. Transient-network failures release the lease with `next_run_at=clock.now()+timedelta(minutes=5)`; every other failed run uses the next regular cron time after its status transition. A failed run must finish its run record and release the lease. No AI type appears in the runner constructor or module imports.

`delivery_key()` is exact and stable: new-item events use `f"{monitor_id}:{item.item_id}:new_item"`; all other rule events append rule kind, field name, and SHA-256 of canonical previous/current values. This lets the rental importer preseed old new-item deliveries while allowing a future threshold to alert again only after a real crossing. A partial-source warning suppresses the no-change message.

- [ ] **Step 5: Implement outbox retry and success ordering**

Add `RegistryRepository.get_delivery_target(target_id) -> DeliveryTargetRow`, which resolves the internal foreign-key ID and raises when absent; `OutboxWorker` uses its address and never treats `target_id` itself as a delivery address. `OutboxWorker` uses delays `(60, 300, 1800, 7200, 21600)` seconds. It selects pending rows whose `available_at <= now`, resolves the target, calls `DeliverySender.send()`, and executes `mark_delivered()` only after a message ID returns. It maps arbitrary sender exceptions to the closed `delivery_failed` storage code. After the fifth failed attempt it retains status `pending`, schedules the next attempt 21,600 seconds later, computes the UTC six-hour window start, and calls `OperatorHealthSink.emit_once()` with dedupe key `outbox-stuck:{outbox_id}:{window_start_iso}` rather than dropping the message. The sink owns persistence of that dedupe key, so restarts cannot duplicate an operator event within the window.

- [ ] **Step 6: Run the engine and full suites, then commit**

Run: `cd rental-housing-monitor && ../.venv/bin/python -m pytest tests/personal_monitor/engine tests/personal_monitor/test_ai_boundary.py -q && ../.venv/bin/python -m pytest -q`

Expected: all tests pass and `test_regular_run_never_imports_ai` proves the boundary.

```bash
git add rental-housing-monitor/src/personal_monitor rental-housing-monitor/tests/personal_monitor
git commit -m "feat: run monitors with idempotent outbox"
```

### Task 7: Add retention maintenance and an operator CLI

**Files:**
- Create: `rental-housing-monitor/src/personal_monitor/maintenance.py`
- Create: `rental-housing-monitor/src/personal_monitor/cli.py`
- Create: `rental-housing-monitor/src/personal_monitor/__main__.py`
- Create: `rental-housing-monitor/tests/personal_monitor/test_maintenance.py`
- Create: `rental-housing-monitor/tests/personal_monitor/test_cli.py`
- Modify: `rental-housing-monitor/pyproject.toml`

**Interfaces:**
- Consumes: database path, JSON `MonitorSpec`, clock, registry/runtime repositories.
- Produces: `personal-monitor validate-spec`, `personal-monitor database init`, `personal-monitor maintenance run`, and `python -m personal_monitor`.

- [ ] **Step 1: Write failing CLI and retention tests**

```python
def test_validate_spec_prints_canonical_json(tmp_path, capsys) -> None:
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(valid_spec()), encoding="utf-8")
    assert main(["validate-spec", str(spec_path)]) == 0
    assert json.loads(capsys.readouterr().out)["schema_version"] == 1


def test_maintenance_applies_exact_retention_windows(repository, now) -> None:
    seed_old_rows(repository, now)
    Maintenance(repository).run(now=now)
    assert count(repository, "runs") == 1
    assert count(repository, "deliveries") == 1
    assert count(repository, "disabled old monitor") == 0
```

- [ ] **Step 2: Run focused tests and verify missing CLI**

Run: `cd rental-housing-monitor && ../.venv/bin/python -m pytest tests/personal_monitor/test_maintenance.py tests/personal_monitor/test_cli.py -q`

Expected: FAIL importing `personal_monitor.cli` and `personal_monitor.maintenance`.

- [ ] **Step 3: Implement exact retention transactions**

`Maintenance.run(now)` deletes completed `runs` older than 90 days, successful `deliveries` and their delivered outbox rows older than 180 days, consumed/expired `pending_actions` older than one day, diagnostic snapshots older than seven days when that table is added by the scraping plan, and disabled monitors older than 30 days with all dependent rows. Run `PRAGMA optimize`; do not run `VACUUM` on every service tick. Expose a separate monthly `database vacuum` command.

- [ ] **Step 4: Implement argparse commands and entry point**

```python
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="personal-monitor")
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate-spec")
    validate.add_argument("path", type=Path)
    database = sub.add_parser("database")
    database.add_argument("action", choices=("init", "integrity-check", "vacuum"))
    database.add_argument("--path", type=Path, required=True)
    maintenance = sub.add_parser("maintenance")
    maintenance.add_argument("action", choices=("run",))
    maintenance.add_argument("--database", type=Path, required=True)
    return parser
```

`validate-spec` loads UTF-8 JSON with `MonitorSpec.model_validate_json()` and prints canonical JSON. `database init` applies migrations, `integrity-check` requires `PRAGMA integrity_check` to return `ok`, and `maintenance run` executes retention once. Add:

```toml
[project.scripts]
personal-monitor = "personal_monitor.cli:main"
```

`__main__.py` calls `raise SystemExit(main())`.

- [ ] **Step 5: Run full verification**

Run: `cd rental-housing-monitor && ../.venv/bin/python -m pytest -q && ../.venv/bin/python -m ruff check . && ../.venv/bin/python -m compileall -q src && ../.venv/bin/personal-monitor --help`

Expected: all tests pass, Ruff and compileall exit 0, and CLI help lists `validate-spec`, `database`, and `maintenance`.

- [ ] **Step 6: Commit the executable core**

```bash
git add rental-housing-monitor/pyproject.toml rental-housing-monitor/src/personal_monitor rental-housing-monitor/tests/personal_monitor
git commit -m "feat: add personal monitor runtime CLI"
```

## Core plan completion gate

Run: `cd rental-housing-monitor && ../.venv/bin/python -m pytest -q && ../.venv/bin/python -m ruff check . && ../.venv/bin/python -m compileall -q src && cd .. && git diff --check && git status --short`

Expected: every test passes; static checks exit 0; no uncommitted core-plan files remain; `.github/workflows/rental-housing-monitor.yml` is byte-for-byte unchanged.
