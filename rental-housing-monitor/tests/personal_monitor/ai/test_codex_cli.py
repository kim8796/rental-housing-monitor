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
from personal_monitor.ai.codex_cli import (
    CodexCli,
    CodexProtocolError,
    _validate_event,
    _web_search_mode,
)
from personal_monitor.ai.contracts import (
    IntentKind,
    IntentRequest,
    IntentResult,
    PlanRequest,
    PlanResult,
    RepairRequest,
    RepairResult,
    UrlCandidate,
    UrlDiscoveryRequest,
    UrlDiscoveryResult,
    WorkerRequest,
    request_kind,
    result_type_for,
)
from personal_monitor.ai.prompts import prompt_for
from personal_monitor.domain.spec import MonitorSpec
from tests.credential_alias_cases import PUNCTUATED_ASSIGNMENTS, SENSITIVE_ASSIGNMENTS

TRUE_BINARY = (
    str(Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Git/usr/bin/true.exe")
    if os.name == "nt"
    else "/usr/bin/true"
)


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


def discovery_request() -> UrlDiscoveryRequest:
    return UrlDiscoveryRequest(
        request_id="discovery-1",
        query="서울주택도시공사 임대주택 모집공고 게시판",
    )


QUOTED_ASSIGNMENTS = (
    'authorization: "supersecretvalue"',
    "password='supersecretvalue'",
    '"authorization": "supersecretvalue"',
    "'api_key': 'supersecretvalue'",
    "session_id=supersecretvalue",
    'session-id="supersecretvalue"',
    "auth: supersecretvalue",
    "credentials=supersecretvalue",
    "signature: supersecretvalue",
    "key=supersecretvalue",
    "`authorization`: `supersecretvalue`",
    *SENSITIVE_ASSIGNMENTS,
    *PUNCTUATED_ASSIGNMENTS,
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

    return CodexAuthGuard(TRUE_BINARY, home, process_factory=spawn)


def make_cli(
    home: Path,
    root: Path,
    *,
    process_factory=None,
) -> CodexCli:
    kwargs: dict[str, object] = {"auth_guard": auth_guard(home)}
    if process_factory is not None:
        kwargs["process_factory"] = process_factory
    return CodexCli(TRUE_BINARY, home, root, **kwargs)  # type: ignore[arg-type]


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
        TRUE_BINARY,
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
@pytest.mark.skipif(os.name == "nt", reason="Codex launcher permission checks require POSIX")
async def test_url_discovery_enables_live_search_and_accepts_only_search_event(
    tmp_path: Path,
) -> None:
    home, task_root = cli_paths(tmp_path)
    observed_argv: tuple[str, ...] | None = None

    async def spawn(*argv: str, **_kwargs: object) -> FakeProcess:
        nonlocal observed_argv
        observed_argv = argv
        result_path = Path(argv[argv.index("--output-last-message") + 1])
        result_path.write_text(
            UrlDiscoveryResult(
                candidates=[
                    UrlCandidate(
                        name="서울주택도시공사 임대주택 공고",
                        url="https://www.i-sh.co.kr/main/lay2/program/S1T294C295/www/brd/m_247/list.do",
                    )
                ],
                clarification=None,
            ).model_dump_json(),
            encoding="utf-8",
        )
        events = [
            {"type": "thread.started", "thread_id": "thread-1"},
            {"type": "turn.started"},
            {
                "type": "item.completed",
                "item": {
                    "id": "search-1",
                    "type": "web_search",
                    "query": "서울주택도시공사 임대주택 모집공고 게시판",
                },
            },
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1},
            },
        ]
        stdout = b"".join(
            json.dumps(event, ensure_ascii=False).encode("utf-8") + b"\n" for event in events
        )
        return FakeProcess(stdout)

    cli = make_cli(home, task_root, process_factory=spawn)

    result = await cli.run(
        discovery_request(),
        UrlDiscoveryResult.model_json_schema(),
        "gpt-5.6-terra",
        "medium",
    )

    assert result.candidates[0].name == "서울주택도시공사 임대주택 공고"
    assert observed_argv is not None
    assert 'web_search="live"' in observed_argv
    assert 'web_search="disabled"' not in observed_argv


def test_only_url_discovery_request_selects_live_web_search() -> None:
    assert _web_search_mode(discovery_request()) == "live"
    assert _web_search_mode(request()) == "disabled"


def test_web_search_stream_item_is_allowed_only_for_discovery_run() -> None:
    started = json.dumps({"type": "thread.started", "thread_id": "thread-1"}).encode()
    turn = json.dumps({"type": "turn.started"}).encode()
    search = json.dumps(
        {
            "type": "item.completed",
            "item": {
                "id": "search-1",
                "type": "web_search",
                "query": "서울주택도시공사 공고",
            },
        },
        ensure_ascii=False,
    ).encode()
    state = _validate_event(started, 0)
    state = _validate_event(turn, state)

    with pytest.raises(CodexProtocolError):
        _validate_event(search, state, allow_web_search=False)
    assert _validate_event(search, state, allow_web_search=True) == state


def test_codex_web_search_event_accepts_only_bounded_string_duplicate_ids() -> None:
    event = (
        b'{"type":"item.started","item":{"id":"search-1","type":"web_search",'
        b'"action":"search","query":"official housing notices","id":"action-2"}}'
    )
    wrong_type = event.replace(
        b'"id":"action-2"}}',
        b'"id":2}}',
    )
    wrong_event = event.replace(
        b'"type":"web_search"',
        b'"type":"agent_message"',
    )

    assert _validate_event(event, 2, allow_web_search=True) == 2
    with pytest.raises(CodexProtocolError):
        _validate_event(wrong_type, 2, allow_web_search=True)
    with pytest.raises(CodexProtocolError):
        _validate_event(wrong_event, 2, allow_web_search=True)


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
            '"discovery_query":null,"condition_text":null,"schedule_text":null,'
            '"clarification":null,'
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
            "discovery_query": None,
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


@async_test
@pytest.mark.parametrize("secret", QUOTED_ASSIGNMENTS)
async def test_request_secret_is_rejected_before_codex_process_spawn(
    tmp_path: Path,
    secret: str,
) -> None:
    home, root = cli_paths(tmp_path)
    spawn_calls = 0

    async def spawn(*_argv: str, **_kwargs: object) -> FakeProcess:
        nonlocal spawn_calls
        spawn_calls += 1
        return FakeProcess(valid_events())

    unsafe = IntentRequest(
        request_id="req-1",
        owner_id="telegram-user:7",
        message=secret,
        monitor_summaries=[],
    )
    cli = make_cli(home, root, process_factory=spawn)

    with pytest.raises(CodexProtocolError):
        await cli.run(
            unsafe,
            IntentResult.model_json_schema(),
            "gpt-5.6-terra",
            "medium",
        )

    assert spawn_calls == 0


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


def test_intent_output_schema_is_strict_structured_output_compatible() -> None:
    schema = IntentResult.model_json_schema()
    properties = schema["properties"]

    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(properties)
    for name in (
        "target_url",
        "discovery_query",
        "condition_text",
        "schedule_text",
        "clarification",
    ):
        assert {"type": "null"} in properties[name]["anyOf"]


def test_url_discovery_contract_is_strict_and_bounded_to_three_candidates() -> None:
    result = UrlDiscoveryResult(
        candidates=[
            UrlCandidate(
                name=f"공식 게시판 {index}",
                url=f"https://example.com/notices/{index}",
            )
            for index in range(3)
        ],
        clarification=None,
    )
    schema = UrlDiscoveryResult.model_json_schema()

    assert len(result.candidates) == 3
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    with pytest.raises(ValidationError):
        UrlDiscoveryResult(
            candidates=[
                UrlCandidate(name=str(index), url=f"https://example.com/{index}")
                for index in range(4)
            ],
            clarification=None,
        )


def test_url_discovery_worker_discriminator_result_type_and_prompt_are_fixed() -> None:
    value = discovery_request()
    packet = WorkerRequest(kind="url_discovery", request=value)

    assert type(packet.request) is UrlDiscoveryRequest
    assert request_kind(value) == "url_discovery"
    assert result_type_for(value) is UrlDiscoveryResult
    assert "공식" in prompt_for(UrlDiscoveryRequest)
    assert "최대 3개" in prompt_for(UrlDiscoveryRequest)


@pytest.mark.parametrize("result_type", [PlanResult, RepairResult])
def test_spec_result_transports_named_fields_as_an_array_and_restores_monitor_spec(
    result_type: type[PlanResult] | type[RepairResult],
) -> None:
    current = spec()
    kwargs: dict[str, object] = {
        "spec": current,
        "explanation": "가격 필드를 기준으로 구성했습니다.",
    }
    if result_type is RepairResult:
        kwargs["changed_fields"] = []
    encoded = result_type(**kwargs).model_dump(mode="json")

    assert encoded["spec"]["extract"]["fields"] == [
        {
            "name": "price",
            "selector": ".price",
            "type": "krw",
            "required": True,
            "attribute": None,
            "pattern": None,
        }
    ]
    assert result_type.model_validate(encoded).spec == current


@pytest.mark.parametrize("result_type", [PlanResult, RepairResult])
def test_spec_output_schema_is_strict_structured_output_compatible(
    result_type: type[PlanResult] | type[RepairResult],
) -> None:
    schema = result_type.model_json_schema()
    definitions = schema["$defs"]

    def resolved(value: dict[str, object]) -> dict[str, object]:
        reference = value.get("$ref")
        if isinstance(reference, str):
            return definitions[reference.rsplit("/", 1)[-1]]
        return value

    spec_schema = resolved(schema["properties"]["spec"])
    extract_schema = resolved(spec_schema["properties"]["extract"])
    fields_schema = resolved(extract_schema["properties"]["fields"])
    item_schema = resolved(fields_schema["items"])

    assert fields_schema["type"] == "array"
    assert item_schema["additionalProperties"] is False
    assert set(item_schema["required"]) == set(item_schema["properties"])

    stack: list[object] = [schema]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            assert "default" not in current
            properties = current.get("properties")
            if isinstance(properties, dict):
                assert current["additionalProperties"] is False
                assert set(current["required"]) == set(properties)
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)


@async_test
@pytest.mark.parametrize("kind", ["intent", "url_discovery", "plan", "repair"])
async def test_all_four_request_result_pairs_use_fixed_prompts(tmp_path: Path, kind: str) -> None:
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
        "url_discovery": (
            discovery_request(),
            UrlDiscoveryResult(
                candidates=[
                    UrlCandidate(
                        name="공식 임대 공고",
                        url="https://example.com/housing",
                    )
                ],
                clarification=None,
            ),
            UrlDiscoveryResult,
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
        CodexCli(TRUE_BINARY, home, root)


def test_worker_cannot_capture_an_arbitrary_run_callable(tmp_path: Path) -> None:
    from personal_monitor.ai.worker import CodexWorkerError, CodexWorkerServer

    class Arbitrary:
        async def run(self, *_args: object) -> IntentResult:
            raise AssertionError

        async def check(self) -> None:
            raise AssertionError

    directory = tmp_path / "socket"
    directory.mkdir(mode=0o700)
    with pytest.raises(CodexWorkerError):
        arbitrary = Arbitrary()
        CodexWorkerServer(
            directory / "worker.sock",
            arbitrary,
            auth_check=arbitrary.check,
        )


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
        TRUE_BINARY,
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
        TRUE_BINARY,
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
