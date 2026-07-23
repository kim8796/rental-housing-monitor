from __future__ import annotations

import asyncio
import functools
import json
import os
import signal
from pathlib import Path

import pytest
from pydantic import ValidationError

from personal_monitor.ai.auth import CodexAuthGuard
from personal_monitor.ai.codex_cli import CodexCli, CodexProtocolError
from personal_monitor.ai.contracts import (
    IntentKind,
    IntentRequest,
    IntentResult,
    PlanRequest,
    PlanResult,
    RepairRequest,
    RepairResult,
    WorkerRequest,
)
from personal_monitor.domain.spec import MonitorSpec


def async_test(function):
    @functools.wraps(function)
    def run(*args: object, **kwargs: object):
        return asyncio.run(function(*args, **kwargs))

    return run


def request() -> IntentRequest:
    return IntentRequest(
        request_id="req-1",
        owner_id="telegram-user:7",
        message="서울 임대주택을 모니터해줘",
        monitor_summaries=[],
    )


def cli_paths(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "codex-home"
    home.mkdir(mode=0o700)
    root = tmp_path / "tasks"
    root.mkdir(mode=0o700)
    return home, root


def auth_guard(home: Path) -> CodexAuthGuard:
    async def spawn(*_argv: str, **_kwargs: object) -> FakeProcess:
        return FakeProcess(b"Logged in using ChatGPT\n")

    return CodexAuthGuard("/usr/bin/true", home, process_factory=spawn)


def make_cli(
    home: Path,
    root: Path,
    *,
    process_factory=None,
) -> CodexCli:
    kwargs: dict[str, object] = {"auth_guard": auth_guard(home)}
    if process_factory is not None:
        kwargs["process_factory"] = process_factory
    return CodexCli("/usr/bin/true", home, root, **kwargs)  # type: ignore[arg-type]


def valid_events(*, text: str | None = None) -> bytes:
    events: list[dict[str, object]] = [
        {"type": "thread.started", "thread_id": "thread-1"},
        {"type": "turn.started"},
    ]
    if text is not None:
        events.append(
            {
                "type": "item.completed",
                "item": {
                    "id": "item-1",
                    "type": "agent_message",
                    "text": text,
                },
            }
        )
    events.append(
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1},
        }
    )
    return b"".join(
        json.dumps(event, ensure_ascii=False).encode("utf-8") + b"\n" for event in events
    )


ACTUAL_CODEX_0144_BENIGN_EVENTS = (
    b'{"type":"thread.started","thread_id":"019c-source-fixture"}\n'
    b'{"type":"turn.started"}\n'
    b'{"type":"item.completed","item":{"id":"item_0","type":"reasoning",'
    b'"text":"Validated the bounded request."}}\n'
    b'{"type":"item.completed","item":{"id":"item_1","type":"agent_message",'
    b'"text":"Structured result prepared."}}\n'
    b'{"type":"turn.completed","usage":{"input_tokens":42,"cached_input_tokens":7,'
    b'"output_tokens":9,"reasoning_output_tokens":3}}\n'
)


def spec() -> MonitorSpec:
    return MonitorSpec.model_validate(
        {
            "schema_version": 1,
            "owner_id": "telegram-user:7",
            "name": "가격 감시",
            "target_url": "https://example.com/product",
            "source_adapter": "scrapling",
            "adapter_ref": None,
            "fetch_strategy": "auto",
            "schedule": "0 */6 * * *",
            "timezone": "Asia/Seoul",
            "extract": {
                "item_scope": "main",
                "fields": {"price": {"selector": ".price", "type": "krw", "required": True}},
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
                    "value": 100_000,
                }
            ],
            "notify_on_no_change": False,
            "auth_profile_ref": None,
        }
    )


class FakeStdin:
    def __init__(self) -> None:
        self.data = b""

    def write(self, value: bytes) -> None:
        self.data += value

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None

    async def wait_closed(self) -> None:
        return None


class FakeStream:
    def __init__(self, data: bytes) -> None:
        self.data = data

    async def read(self, size: int) -> bytes:
        value, self.data = self.data[:size], self.data[size:]
        return value


class FakeProcess:
    def __init__(self, stdout: bytes, stderr: bytes = b"", returncode: int = 0) -> None:
        self.stdin = FakeStdin()
        self.stdout = FakeStream(stdout)
        self.stderr = FakeStream(stderr)
        self.returncode = returncode
        self.pid = os.getpid()

    async def wait(self) -> int:
        return self.returncode


@async_test
async def test_cli_exact_argv_env_stdin_and_cleanup(tmp_path: Path) -> None:
    home, task_root = cli_paths(tmp_path)
    observed: tuple[tuple[str, ...], dict[str, object], FakeProcess] | None = None

    async def spawn(*argv: str, **kwargs: object) -> FakeProcess:
        nonlocal observed
        result_path = Path(argv[argv.index("--output-last-message") + 1])
        result_path.write_text(
            IntentResult(
                kind=IntentKind.CREATE,
                target_monitor_ids=[],
                target_url="https://example.com",
                condition_text=None,
                schedule_text=None,
                clarification=None,
                confidence=0.9,
            ).model_dump_json(),
            encoding="utf-8",
        )
        process = FakeProcess(valid_events())
        observed = (argv, kwargs, process)
        return process

    cli = make_cli(home, task_root, process_factory=spawn)
    result = await cli.run(request(), IntentResult.model_json_schema(), "gpt-5.6-terra", "medium")
    assert result.kind is IntentKind.CREATE
    assert observed is not None
    argv, kwargs, process = observed
    assert argv[:15] == (
        "/usr/bin/true",
        "--sandbox",
        "read-only",
        "--ask-for-approval",
        "never",
        "--model",
        "gpt-5.6-terra",
        "--strict-config",
        "-c",
        'model_reasoning_effort="medium"',
        "-c",
        'web_search="disabled"',
        "-c",
        'forced_login_method="chatgpt"',
        "-c",
    )
    assert "exec" in argv
    assert "--ignore-user-config" in argv
    assert "--ignore-rules" in argv
    assert kwargs["start_new_session"] is True
    assert "shell" not in kwargs
    env = kwargs["env"]
    assert isinstance(env, dict)
    assert "OPENAI_API_KEY" not in env
    assert "CODEX_API_KEY" not in env
    decoded = json.loads(process.stdin.data)
    assert decoded["message"] == "서울 임대주택을 모니터해줘"
    assert list(task_root.iterdir()) == []


@async_test
@pytest.mark.parametrize(
    ("model", "effort"),
    [
        ("gpt-5.6-terra", "high"),
        ("gpt-5.6-sol", "medium"),
        ("other", "medium"),
    ],
)
async def test_only_exact_model_effort_pairs(tmp_path: Path, model: str, effort: str) -> None:
    home, root = cli_paths(tmp_path)
    cli = make_cli(home, root)
    with pytest.raises(CodexProtocolError):
        await cli.run(request(), IntentResult.model_json_schema(), model, effort)


@async_test
@pytest.mark.parametrize(
    "event",
    [
        {"type": "item.started", "item": {"type": "command_execution"}},
        {"type": "item.completed", "item": {"type": "file_change"}},
        {"type": "item.started", "item": {"type": "mcp_tool_call"}},
        {"type": "item.completed", "item": {"type": "web_search"}},
        {"type": "item.started", "item": {"type": "image_generation"}},
    ],
)
async def test_forbidden_event_rejects_entire_result(
    tmp_path: Path, event: dict[str, object]
) -> None:
    home, root = cli_paths(tmp_path)

    async def spawn(*argv: str, **_kwargs: object) -> FakeProcess:
        result_path = Path(argv[argv.index("--output-last-message") + 1])
        result_path.write_text(
            '{"kind":"unknown","target_monitor_ids":[],"target_url":null,'
            '"condition_text":null,"schedule_text":null,"clarification":null,'
            '"confidence":0}',
            encoding="utf-8",
        )
        prefix = b'{"type":"thread.started","thread_id":"thread-1"}\n{"type":"turn.started"}\n'
        return FakeProcess(prefix + json.dumps(event).encode() + b"\n" + valid_events())

    cli = make_cli(home, root, process_factory=spawn)
    with pytest.raises(CodexProtocolError, match="Codex protocol failure"):
        await cli.run(request(), IntentResult.model_json_schema(), "gpt-5.6-terra", "medium")


@async_test
@pytest.mark.parametrize(
    "stdout",
    [b"not-json\n", b"\xff\n", b"[" + b"[" * 80 + b"]" * 80 + b"]\n"],
)
async def test_invalid_stream_is_fixed(tmp_path: Path, stdout: bytes) -> None:
    home, root = cli_paths(tmp_path)

    async def spawn(*_argv: str, **_kwargs: object) -> FakeProcess:
        return FakeProcess(stdout)

    cli = make_cli(home, root, process_factory=spawn)
    with pytest.raises(CodexProtocolError) as caught:
        await cli.run(request(), IntentResult.model_json_schema(), "gpt-5.6-terra", "medium")
    assert "not-json" not in str(caught.value)


@async_test
@pytest.mark.parametrize(
    "secret",
    [
        "sk-abcdefghijklmnopqrstuvwxyz123456",
        "Bearer abcdefghijklmnopqrstuvwxyz",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signaturevalue",
        "eyJhbGciOiJIUzI1NiJ9.YWJjZGVm.signature",
        "Cookie: session=abcdefghijklmnop",
        "Authorization: private-value",
    ],
)
async def test_result_secret_scanner_is_recursive(tmp_path: Path, secret: str) -> None:
    home, root = cli_paths(tmp_path)

    async def spawn(*argv: str, **_kwargs: object) -> FakeProcess:
        result_path = Path(argv[argv.index("--output-last-message") + 1])
        payload = {
            "kind": "unknown",
            "target_monitor_ids": [],
            "target_url": None,
            "condition_text": None,
            "schedule_text": None,
            "clarification": secret,
            "confidence": 0.0,
        }
        result_path.write_text(json.dumps(payload), encoding="utf-8")
        return FakeProcess(valid_events())

    cli = make_cli(home, root, process_factory=spawn)
    with pytest.raises(CodexProtocolError) as caught:
        await cli.run(request(), IntentResult.model_json_schema(), "gpt-5.6-terra", "medium")
    assert secret not in str(caught.value)


def test_contracts_forbid_extra_and_redact_user_content() -> None:
    value = request()
    assert "서울 임대주택" not in repr(value)
    with pytest.raises(ValidationError):
        IntentRequest(
            request_id="r",
            owner_id="o",
            message="x",
            monitor_summaries=[],
            extra="bad",  # type: ignore[call-arg]
        )
    with pytest.raises(ValidationError):
        WorkerRequest.model_validate(
            {"kind": "intent", "request": value.model_dump(), "extra": "bad"}
        )


@async_test
@pytest.mark.parametrize("kind", ["intent", "plan", "repair"])
async def test_all_three_request_result_pairs_use_fixed_prompts(tmp_path: Path, kind: str) -> None:
    home, root = cli_paths(tmp_path)
    intent = IntentResult(
        kind=IntentKind.CREATE,
        target_monitor_ids=[],
        target_url="https://example.com/product",
        condition_text="10만원 이하",
        schedule_text=None,
        clarification=None,
        confidence=0.9,
    )
    current = spec()
    cases = {
        "intent": (
            request(),
            intent,
            IntentResult,
        ),
        "plan": (
            PlanRequest(
                request_id="req-2",
                owner_id="telegram-user:7",
                message="가격을 감시해줘",
                intent=intent,
                sanitized_document="<main><span class=price>90000</span></main>",
                observed_preview_values=["price=90000"],
            ),
            PlanResult(spec=current, explanation="가격 필드를 기준으로 구성했습니다."),
            PlanResult,
        ),
        "repair": (
            RepairRequest(
                request_id="req-3",
                owner_id="telegram-user:7",
                current_spec=current,
                validation_failures=["price selector missing"],
                sanitized_fragment="<span class=amount>90000</span>",
            ),
            RepairResult(
                spec=current,
                explanation="현재 구성이 유효합니다.",
                changed_fields=[],
            ),
            RepairResult,
        ),
    }
    ai_request, expected, result_type = cases[kind]
    prompts: list[str] = []

    async def spawn(*argv: str, **_kwargs: object) -> FakeProcess:
        prompts.append(argv[-1])
        result_path = Path(argv[argv.index("--output-last-message") + 1])
        result_path.write_text(expected.model_dump_json(), encoding="utf-8")
        return FakeProcess(valid_events())

    cli = make_cli(home, root, process_factory=spawn)
    actual = await cli.run(
        ai_request,
        result_type.model_json_schema(),
        "gpt-5.6-terra",
        "medium",
    )
    assert type(actual) is result_type
    assert len(prompts) == 1
    assert "JSON" in prompts[0]


@async_test
async def test_stream_overflow_and_result_type_mismatch_cleanup(tmp_path: Path) -> None:
    home, root = cli_paths(tmp_path)
    attempts = 0

    async def spawn(*argv: str, **_kwargs: object) -> FakeProcess:
        nonlocal attempts
        attempts += 1
        result_path = Path(argv[argv.index("--output-last-message") + 1])
        if attempts == 1:
            result_path.write_text("{}", encoding="utf-8")
            return FakeProcess(b"x" * (1024 * 1024 + 1))
        result_path.write_text('{"unexpected":"private"}', encoding="utf-8")
        return FakeProcess(valid_events())

    cli = make_cli(home, root, process_factory=spawn)
    for _ in range(2):
        with pytest.raises(CodexProtocolError):
            await cli.run(
                request(),
                IntentResult.model_json_schema(),
                "gpt-5.6-terra",
                "medium",
            )
        assert list(root.iterdir()) == []


def test_cli_cannot_be_composed_without_exact_auth_guard(tmp_path: Path) -> None:
    home, root = cli_paths(tmp_path)
    with pytest.raises(TypeError):
        CodexCli("/usr/bin/true", home, root)


def test_worker_cannot_capture_an_arbitrary_run_callable(tmp_path: Path) -> None:
    from personal_monitor.ai.worker import CodexWorkerError, CodexWorkerServer

    class Arbitrary:
        async def run(self, *_args: object) -> IntentResult:
            raise AssertionError

    directory = tmp_path / "socket"
    directory.mkdir(mode=0o700)
    with pytest.raises(CodexWorkerError):
        CodexWorkerServer(directory / "worker.sock", Arbitrary())


@async_test
async def test_ordinary_agent_text_containing_images_path_is_allowed(tmp_path: Path) -> None:
    home, root = cli_paths(tmp_path)

    async def spawn(*argv: str, **_kwargs: object) -> FakeProcess:
        result_path = Path(argv[argv.index("--output-last-message") + 1])
        result_path.write_text(
            IntentResult(
                kind=IntentKind.UNKNOWN,
                target_monitor_ids=[],
                confidence=0.0,
            ).model_dump_json(),
            encoding="utf-8",
        )
        events = [
            {"type": "thread.started", "thread_id": "safe-thread"},
            {"type": "turn.started"},
            {
                "type": "item.completed",
                "item": {
                    "id": "item-1",
                    "type": "agent_message",
                    "text": "문서의 /images/catalog 경로를 관찰했습니다.",
                },
            },
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1},
            },
        ]
        return FakeProcess(
            b"".join(
                json.dumps(event, ensure_ascii=False).encode("utf-8") + b"\n" for event in events
            )
        )

    guard = auth_guard(home)
    cli = CodexCli(
        "/usr/bin/true",
        home,
        root,
        auth_guard=guard,
        process_factory=spawn,
    )
    result = await cli.run(request(), IntentResult.model_json_schema(), "gpt-5.6-terra", "medium")
    assert result.kind is IntentKind.UNKNOWN


@async_test
async def test_event_after_turn_completed_is_rejected(tmp_path: Path) -> None:
    home, root = cli_paths(tmp_path)

    async def spawn(*argv: str, **_kwargs: object) -> FakeProcess:
        result_path = Path(argv[argv.index("--output-last-message") + 1])
        result_path.write_text(
            IntentResult(
                kind=IntentKind.UNKNOWN,
                target_monitor_ids=[],
                confidence=0.0,
            ).model_dump_json(),
            encoding="utf-8",
        )
        return FakeProcess(
            valid_events()
            + b'{"type":"item.completed","item":{"id":"late","type":"reasoning","text":"x"}}\n'
        )

    guard = auth_guard(home)
    cli = CodexCli(
        "/usr/bin/true",
        home,
        root,
        auth_guard=guard,
        process_factory=spawn,
    )
    with pytest.raises(CodexProtocolError):
        await cli.run(request(), IntentResult.model_json_schema(), "gpt-5.6-terra", "medium")


@async_test
async def test_api_key_added_after_composition_blocks_exec_before_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, root = cli_paths(tmp_path)
    exec_calls = 0

    async def spawn(*_argv: str, **_kwargs: object) -> FakeProcess:
        nonlocal exec_calls
        exec_calls += 1
        raise AssertionError("exec must not start")

    cli = make_cli(home, root, process_factory=spawn)
    monkeypatch.setenv("OPENAI_API_KEY", "private-api-value")
    with pytest.raises(Exception) as caught:
        await cli.run(request(), IntentResult.model_json_schema(), "gpt-5.6-terra", "medium")
    assert exec_calls == 0
    assert list(root.iterdir()) == []
    assert "private-api-value" not in f"{caught.value!s} {caught.value!r}"


@async_test
async def test_cleanup_failure_never_returns_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, root = cli_paths(tmp_path)

    async def spawn(*argv: str, **_kwargs: object) -> FakeProcess:
        result_path = Path(argv[argv.index("--output-last-message") + 1])
        result_path.write_text(
            IntentResult(
                kind=IntentKind.UNKNOWN,
                target_monitor_ids=[],
                confidence=0.0,
            ).model_dump_json(),
            encoding="utf-8",
        )
        return FakeProcess(valid_events())

    cli = make_cli(home, root, process_factory=spawn)

    def fail_cleanup(_path: Path, _identity: tuple[int, int]) -> None:
        raise OSError("private-cleanup-detail")

    monkeypatch.setattr("personal_monitor.ai.codex_cli._remove_workspace_sync", fail_cleanup)
    with pytest.raises(CodexProtocolError) as caught:
        await cli.run(request(), IntentResult.model_json_schema(), "gpt-5.6-terra", "medium")
    assert "private-cleanup-detail" not in f"{caught.value!s} {caught.value!r}"
    for leftover in root.iterdir():
        import shutil

        shutil.rmtree(leftover)


@async_test
async def test_symlinked_result_is_rejected_and_workspace_removed(tmp_path: Path) -> None:
    home, root = cli_paths(tmp_path)
    outside = tmp_path / "outside-result.json"
    outside.write_text(
        IntentResult(
            kind=IntentKind.UNKNOWN,
            target_monitor_ids=[],
            confidence=0.0,
        ).model_dump_json(),
        encoding="utf-8",
    )

    async def spawn(*argv: str, **_kwargs: object) -> FakeProcess:
        result_path = Path(argv[argv.index("--output-last-message") + 1])
        result_path.unlink()
        result_path.symlink_to(outside)
        return FakeProcess(valid_events())

    cli = make_cli(home, root, process_factory=spawn)
    with pytest.raises(CodexProtocolError):
        await cli.run(request(), IntentResult.model_json_schema(), "gpt-5.6-terra", "medium")
    assert list(root.iterdir()) == []
    assert outside.exists()


@async_test
async def test_actual_codex_0144_usage_fixture_is_accepted(tmp_path: Path) -> None:
    home, root = cli_paths(tmp_path)

    async def spawn(*argv: str, **_kwargs: object) -> FakeProcess:
        result_path = Path(argv[argv.index("--output-last-message") + 1])
        result_path.write_text(
            IntentResult(
                kind=IntentKind.UNKNOWN,
                target_monitor_ids=[],
                confidence=0.0,
            ).model_dump_json(),
            encoding="utf-8",
        )
        return FakeProcess(ACTUAL_CODEX_0144_BENIGN_EVENTS)

    cli = make_cli(home, root, process_factory=spawn)
    result = await cli.run(request(), IntentResult.model_json_schema(), "gpt-5.6-terra", "medium")
    assert result.kind is IntentKind.UNKNOWN


@pytest.mark.parametrize(
    "reasoning_value",
    [True, -1, "3", None],
)
def test_actual_usage_rejects_invalid_reasoning_counter(reasoning_value: object) -> None:
    from personal_monitor.ai.codex_cli import _validate_event

    event = {
        "type": "turn.completed",
        "usage": {
            "input_tokens": 1,
            "cached_input_tokens": 0,
            "output_tokens": 1,
            "reasoning_output_tokens": reasoning_value,
        },
    }
    with pytest.raises(CodexProtocolError):
        _validate_event(json.dumps(event).encode(), 2)


@async_test
@pytest.mark.parametrize("stage", ["write", "drain", "wait_closed"])
async def test_entire_stdin_lifecycle_is_deadlined_and_process_reaped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    home, root = cli_paths(tmp_path)
    entered = asyncio.Event()

    class HangingStdin(FakeStdin):
        async def _hang(self) -> None:
            entered.set()
            await asyncio.Future()

        def write(self, value: bytes):
            self.data += value
            return self._hang() if stage == "write" else None

        async def drain(self) -> None:
            if stage == "drain":
                await self._hang()

        async def wait_closed(self) -> None:
            if stage == "wait_closed":
                await self._hang()

    process = FakeProcess(b"")
    process.stdin = HangingStdin()
    process.returncode = None  # type: ignore[assignment]
    process.pid = 717_171
    process.waits = 0
    signals: list[int] = []

    async def wait() -> int:
        process.waits += 1
        while process.returncode is None:
            await asyncio.sleep(0)
        return process.returncode

    process.wait = wait  # type: ignore[method-assign]

    def killpg(_pid: int, sent: int) -> None:
        signals.append(sent)
        process.returncode = -sent  # type: ignore[assignment]

    async def spawn(*_argv: str, **_kwargs: object) -> FakeProcess:
        return process

    monkeypatch.setattr("personal_monitor.ai.codex_cli._RUN_TIMEOUT", 0.01)
    monkeypatch.setattr("personal_monitor.ai.codex_cli._STDIN_CLOSE_TIMEOUT", 0.01)
    monkeypatch.setattr("personal_monitor.ai.auth._PROCESS_STOP_TIMEOUT", 0.01)
    monkeypatch.setattr("personal_monitor.ai.auth.os.killpg", killpg)
    cli = make_cli(home, root, process_factory=spawn)
    with pytest.raises(CodexProtocolError):
        await cli.run(request(), IntentResult.model_json_schema(), "gpt-5.6-terra", "medium")
    assert entered.is_set()
    assert signals == [signal.SIGTERM]
    assert process.waits >= 1
    assert list(root.iterdir()) == []


@async_test
async def test_cancellation_wins_over_workspace_cleanup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, root = cli_paths(tmp_path)
    entered = asyncio.Event()

    class HangingStdin(FakeStdin):
        async def drain(self) -> None:
            entered.set()
            await asyncio.Future()

        async def wait_closed(self) -> None:
            await asyncio.Future()

    process = FakeProcess(b"")
    process.stdin = HangingStdin()
    process.returncode = None  # type: ignore[assignment]
    process.pid = 818_181

    async def wait() -> int:
        while process.returncode is None:
            await asyncio.sleep(0)
        return process.returncode

    process.wait = wait  # type: ignore[method-assign]

    def killpg(_pid: int, sent: int) -> None:
        process.returncode = -sent  # type: ignore[assignment]

    async def spawn(*_argv: str, **_kwargs: object) -> FakeProcess:
        return process

    def fail_cleanup(_path: Path, _identity: tuple[int, int]) -> None:
        raise OSError("private-cleanup-secret")

    monkeypatch.setattr("personal_monitor.ai.codex_cli._STDIN_CLOSE_TIMEOUT", 0.01)
    monkeypatch.setattr("personal_monitor.ai.auth._PROCESS_STOP_TIMEOUT", 0.01)
    monkeypatch.setattr("personal_monitor.ai.auth.os.killpg", killpg)
    monkeypatch.setattr("personal_monitor.ai.codex_cli._remove_workspace_sync", fail_cleanup)
    cli = make_cli(home, root, process_factory=spawn)
    task = asyncio.create_task(
        cli.run(request(), IntentResult.model_json_schema(), "gpt-5.6-terra", "medium")
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    assert "private-cleanup-secret" not in str(caught.value.__notes__)
    for leftover in root.iterdir():
        import shutil

        shutil.rmtree(leftover)
