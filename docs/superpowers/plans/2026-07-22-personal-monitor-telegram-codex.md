# Personal Monitor Telegram and Codex Control Plane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the single allowed Telegram user create and manage monitors in natural Korean language while keeping every state change previewed, confirmed, validated, and isolated from scheduled execution.

**Architecture:** A long-polling Telegram gateway authenticates updates and stores expiring actions. A separate Unix-socket Codex worker accepts only bounded structured requests and returns schema-constrained JSON; the application fetches and sanitizes pages itself and never gives Codex application storage, Docker, Git, or Telegram access.

**Tech Stack:** Python 3.12+, httpx AsyncClient, Pydantic 2, Codex CLI `exec`, Unix domain sockets, Telegram Bot API, pytest.

## Global Constraints

- Accept commands only when both the configured Telegram `user_id` and command `chat_id` match; store delivery `chat_id` separately.
- Use long polling and run only one `getUpdates` consumer for the bot token.
- Prefer a dedicated bot token; reuse an existing outgoing-alert token only after `getWebhookInfo` shows no webhook and the operator confirms no other `getUpdates` consumer.
- Natural language is primary; slash commands remain deterministic fallbacks.
- Require a ten-minute, single-use, requester-bound confirmation for create, update, schedule change, repair activation, and delete.
- Default unspecified schedules to every six hours and reject less than 15 minutes.
- Fetch and sanitize target pages in the application; Codex receives no URL-fetching, Telegram, DB, Git, Docker, or application filesystem capability.
- Force ChatGPT login; reject startup if `OPENAI_API_KEY` or `CODEX_API_KEY` is set.
- Invoke GPT-5.6 Terra with `medium` effort twice at most; after two invalid results invoke GPT-5.6 Sol with `high` effort once.
- Use `codex exec --ephemeral --output-schema`; disable web search, ignore user configuration/rules, and use a read-only sandbox in an isolated worker directory.
- Reject a Codex result stream containing command execution, file change, MCP, or web-search events even though the worker container has no application volume.
- Never invoke Codex from Scheduler, MonitorRunner, SourceAdapter, RuleEngine, or OutboxWorker.
- Never persist Telegram message text, full target HTML, reasoning text, or Codex transcript.

---

### Task 1: Implement Telegram long polling and message delivery

**Files:**
- Create: `rental-housing-monitor/src/personal_monitor/telegram/__init__.py`
- Create: `rental-housing-monitor/src/personal_monitor/telegram/types.py`
- Create: `rental-housing-monitor/src/personal_monitor/telegram/api.py`
- Create: `rental-housing-monitor/tests/personal_monitor/telegram/test_api.py`

**Interfaces:**
- Consumes: bot token, httpx `AsyncClient`, Telegram JSON responses.
- Produces: `TelegramUpdate`, `TelegramMessage`, `CallbackQuery`, `InlineButton`, `TelegramApi.get_updates()`, `send_message()`, `answer_callback()`, and `edit_message()`.

- [ ] **Step 1: Write failing polling, send, and token-redaction tests**

```python
async def test_get_updates_advances_offset(telegram_api, transport) -> None:
    transport.queue_json({"ok": True, "result": [{"update_id": 17, "message": message_json("감시해줘")} ]})
    updates = await telegram_api.get_updates(offset=10, timeout=30)
    assert updates[0].update_id == 17
    assert transport.last_query["offset"] == "10"


async def test_send_message_returns_message_id(telegram_api, transport) -> None:
    transport.queue_json({"ok": True, "result": {"message_id": 88}})
    assert await telegram_api.send_message("42", "등록 미리보기") == "88"


async def test_errors_never_contain_bot_token(telegram_api, transport) -> None:
    transport.raise_error(httpx.ConnectError("offline"))
    with pytest.raises(TelegramApiError) as caught:
        await telegram_api.get_updates(offset=0, timeout=30)
    assert "secret-token" not in str(caught.value)
```

- [ ] **Step 2: Run focused tests and verify missing Telegram modules**

Run: `cd rental-housing-monitor && ../.venv/bin/python -m pytest tests/personal_monitor/telegram/test_api.py -q`

Expected: FAIL importing `personal_monitor.telegram.api`.

- [ ] **Step 3: Define only the Telegram fields the application uses**

```python
@dataclass(frozen=True, slots=True)
class TelegramMessage:
    message_id: int
    chat_id: int
    from_user_id: int
    text: str


@dataclass(frozen=True, slots=True)
class CallbackQuery:
    id: str
    from_user_id: int
    chat_id: int
    message_id: int
    data: str


@dataclass(frozen=True, slots=True)
class TelegramUpdate:
    update_id: int
    message: TelegramMessage | None
    callback_query: CallbackQuery | None


@dataclass(frozen=True, slots=True)
class InlineButton:
    text: str
    callback_data: str
```

Ignore unsupported update types but still advance the offset. Reject malformed supported updates as `TelegramApiError("invalid Telegram response shape")` without embedding the response body.

- [ ] **Step 4: Implement async Bot API methods**

Use endpoints below, with the token held only in the private base URL field:

```python
GET  /getUpdates         # offset, timeout, allowed_updates=["message","callback_query"]
POST /sendMessage        # chat_id, text, disable_web_page_preview, reply_markup
POST /editMessageText    # chat_id, message_id, text, reply_markup
POST /answerCallbackQuery # callback_query_id, text, show_alert
GET  /getWebhookInfo      # deployment preflight only
```

Use a 35-second client timeout for 30-second long polling and 20 seconds for other calls. Parse `ok`, `result`, and `message_id`; error text may include Telegram's description but never request URLs or headers. `getWebhookInfo` must report an empty URL before the service starts polling. Reuse the existing 4096-character `split_message` behavior through a shared helper without changing `rental_monitor.telegram`.

- [ ] **Step 5: Run tests and commit**

Run: `cd rental-housing-monitor && ../.venv/bin/python -m pytest tests/personal_monitor/telegram/test_api.py -q`

Expected: all polling, callback, split, malformed-response, and secret-redaction tests pass.

```bash
git add rental-housing-monitor/src/personal_monitor/telegram rental-housing-monitor/tests/personal_monitor/telegram/test_api.py
git commit -m "feat: add Telegram long polling client"
```

### Task 2: Authorize users and persist expiring confirmations

**Files:**
- Create: `rental-housing-monitor/src/personal_monitor/control/__init__.py`
- Create: `rental-housing-monitor/src/personal_monitor/control/actions.py`
- Create: `rental-housing-monitor/src/personal_monitor/telegram/gateway.py`
- Modify: `rental-housing-monitor/src/personal_monitor/storage/registry.py`
- Create: `rental-housing-monitor/tests/personal_monitor/control/test_actions.py`
- Create: `rental-housing-monitor/tests/personal_monitor/telegram/test_gateway.py`

**Interfaces:**
- Consumes: update user ID/chat ID, pending action repository, clock, cryptographic randomness.
- Produces: `PendingActionService.create()`, `consume()`, callback data shaped like `confirm:Q2hhbmdlVG9rZW4`/`cancel:Q2hhbmdlVG9rZW4`, and `TelegramGateway.handle_update()`.

- [ ] **Step 1: Write failing authorization and token tests**

```python
async def test_unapproved_user_cannot_reach_router(gateway, router) -> None:
    await gateway.handle_update(message_update(user_id=999, text="모니터해줘"))
    assert router.calls == []


async def test_message_from_wrong_command_chat_cannot_reach_router(gateway, router) -> None:
    await gateway.handle_update(message_update(user_id=7, chat_id=999, text="모니터해줘"))
    assert router.calls == []


def test_confirmation_is_requester_bound_single_use_and_expires(actions) -> None:
    pending = actions.create("telegram-user:7", "create", {"version_id": "v1"}, now=NOW)
    with pytest.raises(ActionDenied):
        actions.consume(pending.token, "telegram-user:8", now=NOW)
    assert actions.consume(pending.token, "telegram-user:7", now=NOW).payload == {"version_id": "v1"}
    with pytest.raises(ActionDenied):
        actions.consume(pending.token, "telegram-user:7", now=NOW)
```

- [ ] **Step 2: Run tests and verify missing action service**

Run: `cd rental-housing-monitor && ../.venv/bin/python -m pytest tests/personal_monitor/control/test_actions.py tests/personal_monitor/telegram/test_gateway.py -q`

Expected: FAIL importing `personal_monitor.control.actions`.

- [ ] **Step 3: Implement opaque one-time actions**

Generate `secrets.token_urlsafe(24)`, store only `sha256(token.encode()).hexdigest()`, set `expires_at=now+timedelta(minutes=10)`, and return the plaintext token only for callback construction. `consume()` uses `BEGIN IMMEDIATE`, checks owner, expiry, and null `consumed_at`, then marks consumed before returning the parsed action. Callback data remains below Telegram's 64-byte limit.

- [ ] **Step 4: Implement the allowlist gate**

`TelegramGateway` receives `allowed_user_id: int` and `command_chat_id: int`. It drops message and callback updates when either value differs, logs only a value shaped like `unauthorized_telegram_user_id=999`, and never sends configuration details to that chat. Authorized message updates become `ControlRequest(owner_id=f"telegram-user:{user_id}", chat_id=str(chat_id), text=text)`. Callback owner and chat identity are checked again before action consumption.

- [ ] **Step 5: Run tests and commit**

Run: `cd rental-housing-monitor && ../.venv/bin/python -m pytest tests/personal_monitor/control/test_actions.py tests/personal_monitor/telegram/test_gateway.py -q`

Expected: authorization, expiry, replay, wrong-user, cancel, and callback-length tests pass.

```bash
git add rental-housing-monitor/src/personal_monitor/control rental-housing-monitor/src/personal_monitor/telegram/gateway.py rental-housing-monitor/src/personal_monitor/storage/registry.py rental-housing-monitor/tests/personal_monitor
git commit -m "feat: confirm Telegram monitor changes"
```

### Task 3: Isolate schema-constrained Codex execution

**Files:**
- Create: `rental-housing-monitor/src/personal_monitor/ai/__init__.py`
- Create: `rental-housing-monitor/src/personal_monitor/ai/contracts.py`
- Create: `rental-housing-monitor/src/personal_monitor/ai/auth.py`
- Create: `rental-housing-monitor/src/personal_monitor/ai/codex_cli.py`
- Create: `rental-housing-monitor/src/personal_monitor/ai/worker.py`
- Create: `rental-housing-monitor/src/personal_monitor/ai/prompts.py`
- Create: `rental-housing-monitor/tests/personal_monitor/ai/test_auth.py`
- Create: `rental-housing-monitor/tests/personal_monitor/ai/test_codex_cli.py`
- Create: `rental-housing-monitor/tests/personal_monitor/ai/test_worker.py`

**Interfaces:**
- Consumes: bounded `IntentRequest`, `PlanRequest`, `RepairRequest`, dedicated `CODEX_HOME`, Unix socket.
- Produces: `CodexAuthGuard.check()`, `CodexCli.run(request, schema, model, effort)`, `CodexWorkerServer`, and `CodexWorkerClient`.

- [ ] **Step 1: Write failing authentication and forbidden-event tests**

```python
@pytest.mark.parametrize("name", ["OPENAI_API_KEY", "CODEX_API_KEY"])
async def test_api_key_environment_fails_closed(name, monkeypatch, auth_guard) -> None:
    monkeypatch.setenv(name, "secret")
    with pytest.raises(CodexAuthError, match=name):
        await auth_guard.check()


async def test_command_execution_event_rejects_entire_result(codex_cli, fake_process) -> None:
    fake_process.stdout_lines = [
        json.dumps({"type": "item.started", "item": {"type": "command_execution"}}),
        json.dumps({"type": "turn.completed"}),
    ]
    with pytest.raises(CodexProtocolError, match="forbidden event"):
        await codex_cli.run(intent_request(), IntentResult.model_json_schema(), "gpt-5.6-terra", "medium")
```

- [ ] **Step 2: Define bounded worker contracts**

```python
class IntentKind(StrEnum):
    CREATE = "create"
    LIST = "list"
    UPDATE = "update"
    PAUSE = "pause"
    RESUME = "resume"
    DELETE = "delete"
    STATUS = "status"
    UNKNOWN = "unknown"


class IntentRequest(StrictModel):
    request_id: str
    owner_id: str
    message: Annotated[str, Field(min_length=1, max_length=2000)]
    monitor_summaries: list[str] = Field(max_length=100)


class IntentResult(StrictModel):
    kind: IntentKind
    target_monitor_ids: list[str] = Field(max_length=10)
    target_url: str | None = None
    condition_text: str | None = None
    schedule_text: str | None = None
    clarification: str | None = None
    confidence: Annotated[float, Field(ge=0, le=1)]
```

`PlanRequest` contains intent fields plus at most 40,000 sanitized document characters and observed preview values. `PlanResult(StrictModel)` contains `spec: MonitorSpec` and `explanation: Annotated[str, Field(max_length=1000)]`. `RepairRequest` contains the current spec, safe validation failures, and sanitized fragment; `RepairResult(StrictModel)` contains the same two fields plus `changed_fields: list[str]` capped at 50 names.

- [ ] **Step 3: Force ChatGPT authentication**

`CodexAuthGuard.check()` first rejects non-empty `OPENAI_API_KEY`/`CODEX_API_KEY`, then runs `codex login status` with `CODEX_HOME` set to the dedicated directory. Require exit code 0 and exact trimmed stdout `Logged in using ChatGPT`; otherwise raise a safe Korean operator message directing the IAP administrator to run `codex login --device-auth`. Never return or log stdout from login status.

- [ ] **Step 4: Invoke Codex with argv, not a shell**

Build this argument vector and pass the JSON request on stdin:

```python
argv = [
    codex_binary,
    "--sandbox",
    "read-only",
    "--ask-for-approval",
    "never",
    "--model",
    model,
    "--strict-config",
    "-c",
    f'model_reasoning_effort="{effort}"',
    "-c",
    'web_search="disabled"',
    "-c",
    'forced_login_method="chatgpt"',
    "-c",
    'shell_environment_policy.inherit="none"',
    "--cd",
    str(isolated_empty_workdir),
    "exec",
    "--ephemeral",
    "--json",
    "--ignore-user-config",
    "--ignore-rules",
    "--skip-git-repo-check",
    "--output-schema",
    str(schema_path),
    "--output-last-message",
    str(result_path),
    prompt_for(type(request)),
]
```

Use `asyncio.create_subprocess_exec(*argv, stdin=PIPE, stdout=PIPE, stderr=PIPE, env=scrubbed_env)`. Set a 120-second timeout, cap stdout/stderr at 1 MiB, validate every JSONL event, reject `command_execution`, `file_change`, `mcp_tool_call`, and `web_search` item types, then parse `result_path` through the expected Pydantic model. Recursively reject returned strings matching API-key, bearer-token, JWT, cookie, or authorization-header patterns before returning the result. Delete the request/schema/result directory in `finally`. Do not use shell interpolation or persist rollout files.

- [ ] **Step 5: Implement the Unix socket worker boundary**

Use a dedicated service-UID-owned directory with mode `0o700` and socket mode `0o600`; both `monitor` and `codex-worker` run as that UID, while no other UID can connect. Messages are four-byte big-endian length followed by UTF-8 JSON, capped at 256 KiB. The server validates the outer request before invoking Codex and returns the same framing with either `{"ok":true,"result":...}` or `{"ok":false,"error_code":"..."}`. It never returns stderr, auth output, or model reasoning. The client enforces a 130-second timeout and validates the response again.

- [ ] **Step 6: Run AI boundary tests and commit**

Run: `cd rental-housing-monitor && ../.venv/bin/python -m pytest tests/personal_monitor/ai -q`

Expected: API-key rejection, missing ChatGPT login, timeout, output-schema failure, forbidden event, oversized message, socket mode, and happy-path tests pass using fake processes.

```bash
git add rental-housing-monitor/src/personal_monitor/ai rental-housing-monitor/tests/personal_monitor/ai
git commit -m "feat: isolate Codex planning worker"
```

### Task 4: Route Korean natural-language intents with deterministic fallbacks

**Files:**
- Create: `rental-housing-monitor/src/personal_monitor/control/intents.py`
- Create: `rental-housing-monitor/tests/personal_monitor/control/test_intents.py`
- Create: `rental-housing-monitor/tests/fixtures/personal_monitor/intent_cases.json`

**Interfaces:**
- Consumes: `ControlRequest`, monitor summaries, `CodexWorkerClient`.
- Produces: `IntentRouter.route() -> IntentResult` and exact slash-command fallbacks.

- [ ] **Step 1: Create a fixed Korean intent evaluation set**

```json
[
  {"text":"지금 모니터링 중인 거 보여줘","kind":"list"},
  {"text":"아까 등록한 상품 감시 잠깐 꺼줘","kind":"pause"},
  {"text":"확인 주기를 하루에 한 번으로 바꿔줘","kind":"update"},
  {"text":"임대주택 알림은 두고 가격 알림만 삭제해줘","kind":"delete"},
  {"text":"https://example.com/p/7 이게 10만 원 아래면 알려줘","kind":"create"},
  {"text":"요즘 어때?","kind":"unknown"}
]
```

Test that ambiguous references produce one clarification and no guessed monitor ID.

- [ ] **Step 2: Implement deterministic commands before Codex**

Support exact forms:

```text
/monitors
/status rental-housing-seoul-gyeonggi
/pause rental-housing-seoul-gyeonggi
/resume rental-housing-seoul-gyeonggi
/delete rental-housing-seoul-gyeonggi
/cancel
```

These parse without Codex and still require confirmation for pause/resume/delete. All other text goes to the worker. If confidence is below 0.75, target count conflicts with intent, or target monitor ID is not owned by the requester, return one clarification rather than an action.

For schema-invalid worker output, use the same closed attempt sequence as planning: Terra/medium, Terra/medium, then Sol/high. A valid `unknown` result or a low-confidence valid result does not escalate; it produces one clarification. Three invalid results return a safe “요청을 이해하지 못했습니다” message and perform no write.

- [ ] **Step 3: Run intent evaluations**

Run: `cd rental-housing-monitor && ../.venv/bin/python -m pytest tests/personal_monitor/control/test_intents.py -q`

Expected: every fixed case has the expected structured kind; ambiguous and ownership cases ask one question.

- [ ] **Step 4: Commit intent routing**

```bash
git add rental-housing-monitor/src/personal_monitor/control/intents.py rental-housing-monitor/tests/personal_monitor/control/test_intents.py rental-housing-monitor/tests/fixtures/personal_monitor/intent_cases.json
git commit -m "feat: route Korean monitor requests"
```

### Task 5: Probe, plan, validate, and preview new monitors

**Files:**
- Create: `rental-housing-monitor/src/personal_monitor/control/planner.py`
- Create: `rental-housing-monitor/src/personal_monitor/control/preview.py`
- Create: `rental-housing-monitor/tests/personal_monitor/control/test_planner.py`
- Create: `rental-housing-monitor/tests/personal_monitor/control/test_preview.py`

**Interfaces:**
- Consumes: create intent, policy-safe Scrapling probe, sanitizer, Codex worker, `MonitorSpec` validator, extractor/validator.
- Produces: `MonitorPlanner.propose() -> ProposedMonitor` and Korean preview with confirm/edit/cancel buttons.

- [ ] **Step 1: Write failing attempt-routing and preview tests**

```python
async def test_planner_uses_terra_twice_then_sol_once(planner, worker) -> None:
    worker.results = [invalid_schema(), invalid_selector(), valid_plan()]
    proposal = await planner.propose(create_intent())
    assert proposal.spec.name == "상품 가격 감시"
    assert worker.calls == [
        ("gpt-5.6-terra", "medium"),
        ("gpt-5.6-terra", "medium"),
        ("gpt-5.6-sol", "high"),
    ]


def test_preview_contains_extracted_value_schedule_and_strategy(proposal) -> None:
    text = render_preview(proposal)
    assert "현재 가격: 99,000원" in text
    assert "확인 주기: 6시간마다" in text
    assert "수집 방식: Scrapling HTTP" in text
```

- [ ] **Step 2: Implement the closed planning loop**

`propose()` performs safe probe once, sanitizes the result, then invokes Terra/medium. Each result must pass: Pydantic schema → ownership/URL equality → URL policy → extractor on the original source document → observation validator → rule field/operator compatibility → schedule minimum. Feed only safe validation messages into the next attempt. Attempt order is exactly Terra, Terra, Sol. A third failure returns `PlanningFailed` and creates no monitor/version/pending action.

- [ ] **Step 3: Build a deterministic preview**

`ProposedMonitor` contains the validated spec, up to three preview items, resolved strategy, robots decision, and safe warnings. Render name, redacted URL without query, observed fields, condition, timezone/schedule, fetch strategy, and whether a login profile is required. Buttons are `등록`, `수정`, and `취소`; `등록` token payload contains the complete validated spec JSON hash and candidate version ID so the callback cannot approve different content.

- [ ] **Step 4: Run planner/preview tests and commit**

Run: `cd rental-housing-monitor && ../.venv/bin/python -m pytest tests/personal_monitor/control/test_planner.py tests/personal_monitor/control/test_preview.py -q`

Expected: attempt routing, validation feedback, no-write-on-failure, preview content, and token-binding tests pass.

```bash
git add rental-housing-monitor/src/personal_monitor/control rental-housing-monitor/tests/personal_monitor/control
git commit -m "feat: preview validated monitor proposals"
```

### Task 6: Manage monitor lifecycle through natural language

**Files:**
- Create: `rental-housing-monitor/src/personal_monitor/control/service.py`
- Create: `rental-housing-monitor/src/personal_monitor/control/messages.py`
- Create: `rental-housing-monitor/tests/personal_monitor/control/test_service.py`

**Interfaces:**
- Consumes: intents, registry, planner, pending actions, Telegram gateway.
- Produces: create/list/status/update/pause/resume/delete/repair flows and callback execution.

- [ ] **Step 1: Write failing state-change confirmation tests**

```python
async def test_create_is_not_active_before_confirmation(service, registry) -> None:
    reply = await service.handle(request("https://example.com/p/7 10만원 아래면 알려줘"))
    assert reply.buttons[0].text == "등록"
    assert registry.list_monitors(OWNER) == []


async def test_confirmed_delete_is_soft_delete(service, registry, active_monitor) -> None:
    preview = await service.handle(request(f"{active_monitor} 삭제해줘"))
    await service.handle_callback(confirm_callback(preview))
    assert registry.get_monitor(active_monitor).status is MonitorStatus.DISABLED
```

- [ ] **Step 2: Implement lifecycle operations and ownership checks**

List/status are read-only and immediate. Create stores nothing active until confirmation; update/repair creates an unapproved version and confirmation approves then atomically activates it. Pause/resume uses expected-state transitions. Delete is soft-delete with 30-day retention. Every operation resolves monitors only within `owner_id`; ambiguous natural references return one numbered choice question.

- [ ] **Step 3: Render concise Korean outcomes**

Use fixed messages containing monitor name, status, last success, next run, and user action. Never include query parameters, cookies, Codex error text, full HTML, or internal stack traces. `needs_review` says ordinary alerts are paused and offers the validated repair preview when available.

- [ ] **Step 4: Run lifecycle tests and commit**

Run: `cd rental-housing-monitor && ../.venv/bin/python -m pytest tests/personal_monitor/control/test_service.py -q`

Expected: create/update/pause/resume/delete/status/repair and wrong-owner tests pass.

```bash
git add rental-housing-monitor/src/personal_monitor/control rental-housing-monitor/tests/personal_monitor/control/test_service.py
git commit -m "feat: manage monitors through Telegram"
```

### Task 7: Run the bot, scheduler, and outbox as one personal service

**Files:**
- Create: `rental-housing-monitor/src/personal_monitor/config.py`
- Create: `rental-housing-monitor/src/personal_monitor/service.py`
- Create: `rental-housing-monitor/src/personal_monitor/observability.py`
- Modify: `rental-housing-monitor/src/personal_monitor/cli.py`
- Create: `rental-housing-monitor/tests/personal_monitor/test_config.py`
- Create: `rental-housing-monitor/tests/personal_monitor/test_service.py`
- Create: `rental-housing-monitor/tests/personal_monitor/test_observability.py`
- Modify: `rental-housing-monitor/.env.example`

**Interfaces:**
- Consumes: environment, Telegram/Codex endpoints, registry/runtime/adapters.
- Produces: `Settings.from_env()`, `PersonalMonitorService.run()`, `personal-monitor serve`, and clean shutdown.

- [ ] **Step 1: Write failing configuration and concurrency tests**

```python
def test_settings_requires_numeric_allowed_user_without_leaking_values(monkeypatch) -> None:
    monkeypatch.setenv("PERSONAL_MONITOR_TELEGRAM_USER_ID", "not-a-number")
    with pytest.raises(ConfigurationError, match="PERSONAL_MONITOR_TELEGRAM_USER_ID") as caught:
        Settings.from_env()
    assert "not-a-number" not in str(caught.value)


async def test_service_runs_one_poller_scheduler_and_outbox(service) -> None:
    await service.run_until(StopAfterTicks(telegram=2, scheduler=2, outbox=2))
    assert service.telegram_poller.max_concurrency == 1
```

- [ ] **Step 2: Define explicit settings**

Required: `PERSONAL_MONITOR_TELEGRAM_BOT_TOKEN`, `PERSONAL_MONITOR_TELEGRAM_USER_ID`, `PERSONAL_MONITOR_TELEGRAM_COMMAND_CHAT_ID`, `PERSONAL_MONITOR_TELEGRAM_DELIVERY_CHAT_ID`, `PERSONAL_MONITOR_MASTER_KEY_PATH`, `PERSONAL_MONITOR_CODEX_SOCKET`, and `PERSONAL_MONITOR_EGRESS_PROXY`. Optional defaults: DB `/srv/personal-monitor/db/monitor.db`, profiles tmpfs `/run/personal-monitor-profiles`, diagnostics `/srv/personal-monitor/diagnostics`, adaptive `/srv/personal-monitor/adaptive`, log `/srv/personal-monitor/logs/monitor.jsonl`, timezone `Asia/Seoul`.

Append names and safe defaults to `.env.example`; leave all secret values empty.

- [ ] **Step 3: Implement structured concurrency and shutdown**

`PersonalMonitorService.run()` uses `asyncio.TaskGroup` for one Telegram poll loop, a scheduler tick every 15 seconds, outbox drain every 5 seconds, maintenance daily at 03:30 Asia/Seoul, and a heartbeat every 60 seconds. SIGTERM/SIGINT stops polling, prevents new leases, allows the active run 90 seconds to finish, closes HTTP/SQLite, and exits nonzero only for an unrecovered service-level failure.

- [ ] **Step 4: Add secret-safe JSON logs and health state**

`JsonLogFormatter` emits exactly `timestamp`, `level`, `logger`, `event`, and allowlisted context fields `monitor_id`, `run_id`, `stage`, `fetch_strategy`, `duration_ms`, `retry_count`, and `error_class`. A filter removes keys containing `token`, `cookie`, `authorization`, `secret`, `query`, `html`, or `message_text`; safe errors use class/code only. Rotate at 10 MiB with five files. The heartbeat records DB write success, scheduler loop time, disk free bytes, Telegram last-update time, outbox backlog, and Codex login health separately; failed backup state is read from the backup status file. Health failures enqueue an operator event with a six-hour dedupe window.

- [ ] **Step 5: Add service, worker, and one-shot CLI commands and run tests**

Add `serve`, `ai-worker --socket`, and `run-once --database --monitor --delivery {enabled,disabled}`. `run-once` defaults to `disabled` and constructs `NullDeliverySender`; enabled delivery must be passed explicitly. `ai-worker` starts only the Unix-socket server and refuses Telegram/DB environment variables.

Run: `cd rental-housing-monitor && ../.venv/bin/python -m pytest tests/personal_monitor/test_config.py tests/personal_monitor/test_service.py tests/personal_monitor/test_observability.py -q`

Expected: required settings, secret redaction, JSON field allowlist, single poller, tick cadence, CLI safety defaults, and graceful shutdown tests pass.

- [ ] **Step 6: Commit the service process**

```bash
git add rental-housing-monitor/src/personal_monitor rental-housing-monitor/tests/personal_monitor rental-housing-monitor/.env.example
git commit -m "feat: run personal monitor service"
```

### Task 8: Add AI, prompt-injection, and end-to-end control evaluations

**Files:**
- Create: `rental-housing-monitor/tests/personal_monitor/evals/test_intent_eval.py`
- Create: `rental-housing-monitor/tests/personal_monitor/evals/test_planner_eval.py`
- Create: `rental-housing-monitor/tests/personal_monitor/evals/test_prompt_injection.py`
- Create: `rental-housing-monitor/tests/personal_monitor/integration/test_telegram_onboarding.py`
- Create: `rental-housing-monitor/tests/fixtures/personal_monitor/planner_cases.json`
- Create: `rental-housing-monitor/tests/fixtures/personal_monitor/injection_pages.html`

**Interfaces:**
- Consumes: frozen request/output fixtures and fake Codex/Telegram transports.
- Produces: regression evidence for natural language, schema safety, model escalation, confirmation, and no-AI scheduled execution.

- [ ] **Step 1: Add planner and injection cases**

Cases cover price threshold, stock status, keyword notice, repeated listing, one ambiguous request, one login-required page, hidden “ignore previous instructions” content, HTML comments containing tool requests, a fake JSON instruction, and a query string containing a token. Expected results assert structure and validator outcome, not prose equality.

- [ ] **Step 2: Test full Telegram onboarding**

Feed one allowed update containing a URL and Korean condition; fake the probe and Codex schema result; assert preview message; feed the exact callback; assert active version 1; run the monitor; assert one outbox delivery; run again; assert no duplicate. Repeat with an unauthorized user and assert no Codex/probe/DB call.

- [ ] **Step 3: Re-run the hard AI boundary**

Run: `cd rental-housing-monitor && ../.venv/bin/python -m pytest tests/personal_monitor/test_ai_boundary.py tests/personal_monitor/evals tests/personal_monitor/integration/test_telegram_onboarding.py -q`

Expected: all tests pass; scheduled execution completes with an AI object that raises on access.

- [ ] **Step 4: Run full verification and commit**

Run: `cd rental-housing-monitor && ../.venv/bin/python -m pytest -q && ../.venv/bin/python -m ruff check . && ../.venv/bin/python -m compileall -q src && cd .. && git diff --check`

Expected: all commands exit 0.

```bash
git add rental-housing-monitor/tests/personal_monitor
git commit -m "test: verify Telegram and Codex control plane"
```

## Control-plane completion gate

Run: `cd rental-housing-monitor && env -u OPENAI_API_KEY -u CODEX_API_KEY CODEX_HOME=/srv/personal-monitor/codex-home codex login status`

Expected on the deployment host after device login: exit code 0 and ChatGPT authentication. Do not paste the command output into an issue or log. Then run the full offline test suite; no live Telegram message or live Codex call is part of CI.
