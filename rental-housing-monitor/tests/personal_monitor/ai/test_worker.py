from __future__ import annotations

import asyncio
import functools
import json
import shutil
import tempfile
from contextlib import suppress
from pathlib import Path

import pytest

from personal_monitor.ai.auth import CodexAuthGuard
from personal_monitor.ai.codex_cli import CodexCli
from personal_monitor.ai.contracts import IntentKind, IntentRequest, IntentResult
from personal_monitor.ai.worker import CodexWorkerClient, CodexWorkerError, CodexWorkerServer


def async_test(function):
    @functools.wraps(function)
    def run(*args: object, **kwargs: object):
        return asyncio.run(function(*args, **kwargs))

    return run


def short_socket_path() -> tuple[Path, Path]:
    directory = Path(tempfile.mkdtemp(prefix="pm-ai-", dir="/tmp")).resolve()
    directory.chmod(0o700)
    return directory, directory / "codex.sock"


class FakeStdin:
    def write(self, _value: bytes) -> None:
        return None

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None

    async def wait_closed(self) -> None:
        return None


class FakeStream:
    def __init__(self, data: bytes) -> None:
        self.data = data

    async def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self.data)
        value, self.data = self.data[:size], self.data[size:]
        return value


class FakeProcess:
    def __init__(
        self,
        stdout: bytes,
        *,
        wait_delay: float = 0,
        finished=None,
    ) -> None:
        self.stdin = FakeStdin()
        self.stdout = FakeStream(stdout)
        self.stderr = FakeStream(b"")
        self.returncode = 0
        self.pid = 999_999
        self._wait_delay = wait_delay
        self._finished = finished
        self._waited = False

    async def wait(self) -> int:
        if not self._waited:
            self._waited = True
            await asyncio.sleep(self._wait_delay)
            if self._finished is not None:
                self._finished()
        return 0


class SecureHarness:
    def __init__(
        self,
        directory: Path,
        *,
        authenticated: bool = True,
        auth_wait_delay: float = 0,
    ) -> None:
        self.auth_calls = 0
        self.exec_calls = 0
        self.active = 0
        self.max_active = 0
        home = directory / "home"
        root = directory / "tasks"
        home.mkdir(mode=0o700)
        root.mkdir(mode=0o700)

        async def auth_spawn(*_argv: str, **_kwargs: object) -> FakeProcess:
            self.auth_calls += 1
            output = b"Logged in using ChatGPT\n" if authenticated else b"Not logged in\n"
            return FakeProcess(output, wait_delay=auth_wait_delay)

        async def exec_spawn(*argv: str, **_kwargs: object) -> FakeProcess:
            self.exec_calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            result_path = Path(argv[argv.index("--output-last-message") + 1])
            result_path.write_text(
                IntentResult(
                    kind=IntentKind.CREATE,
                    target_monitor_ids=[],
                    target_url="https://example.com",
                    confidence=1.0,
                ).model_dump_json(),
                encoding="utf-8",
            )
            events = (
                b'{"type":"thread.started","thread_id":"thread-1"}\n'
                b'{"type":"turn.started"}\n'
                b'{"type":"turn.completed","usage":'
                b'{"input_tokens":1,"cached_input_tokens":0,"output_tokens":1}}\n'
            )
            return FakeProcess(
                events,
                wait_delay=0.05,
                finished=lambda: setattr(self, "active", self.active - 1),
            )

        guard = CodexAuthGuard("/usr/bin/true", home, process_factory=auth_spawn)
        self.guard = guard
        self.cli = CodexCli(
            "/usr/bin/true",
            home,
            root,
            auth_guard=guard,
            process_factory=exec_spawn,
        )


def intent(index: int = 1) -> IntentRequest:
    return IntentRequest(
        request_id=f"r-{index}",
        owner_id="telegram-user:7",
        message="모니터해줘",
        monitor_summaries=[],
    )


@async_test
async def test_worker_socket_happy_path_permissions_and_busy_rejection() -> None:
    directory, socket_path = short_socket_path()
    harness = SecureHarness(directory)
    server = CodexWorkerServer(
        socket_path,
        harness.cli,
        auth_check=harness.guard.check,
    )
    await server.start()
    try:
        assert socket_path.stat().st_mode & 0o777 == 0o600
        client = CodexWorkerClient(socket_path)
        results = await asyncio.gather(
            *[
                client.run(intent(index), model="gpt-5.6-terra", effort="medium")
                for index in range(3)
            ],
            return_exceptions=True,
        )
        assert (
            sum(
                isinstance(result, IntentResult) and result.kind is IntentKind.CREATE
                for result in results
            )
            == 1
        )
        assert sum(isinstance(result, CodexWorkerError) for result in results) == 2
        assert harness.max_active == 1
        assert harness.exec_calls == 1
    finally:
        await server.close()
        shutil.rmtree(directory)
    assert not socket_path.exists()


@async_test
@pytest.mark.parametrize("length", [0, 256 * 1024 + 1])
async def test_worker_rejects_invalid_frame_without_codex(length: int) -> None:
    directory, socket_path = short_socket_path()
    harness = SecureHarness(directory)
    server = CodexWorkerServer(
        socket_path,
        harness.cli,
        auth_check=harness.guard.check,
    )
    await server.start()
    try:
        reader, writer = await asyncio.open_unix_connection(socket_path)
        writer.write(length.to_bytes(4, "big"))
        await writer.drain()
        writer.write_eof()
        await reader.read()
        assert harness.exec_calls == 0
        writer.close()
        await writer.wait_closed()
    finally:
        await server.close()
        shutil.rmtree(directory)


@async_test
async def test_delayed_second_frame_never_invokes_codex() -> None:
    directory, socket_path = short_socket_path()
    harness = SecureHarness(directory)
    server = CodexWorkerServer(
        socket_path,
        harness.cli,
        auth_check=harness.guard.check,
    )
    await server.start()
    try:
        reader, writer = await asyncio.open_unix_connection(socket_path)
        payload = json.dumps(
            {
                "kind": "intent",
                "request": intent().model_dump(mode="json"),
                "model": "gpt-5.6-terra",
                "effort": "medium",
            }
        ).encode()
        frame = len(payload).to_bytes(4, "big") + payload
        writer.write(frame)
        await writer.drain()
        await asyncio.sleep(0.05)
        writer.write(frame)
        writer.write_eof()
        await writer.drain()
        await reader.read()
        assert harness.exec_calls == 0
        writer.close()
        await writer.wait_closed()
    finally:
        await server.close()
        shutil.rmtree(directory)


@async_test
async def test_client_rejects_unsafe_socket_directory(tmp_path: Path) -> None:
    directory = tmp_path / "worker"
    directory.mkdir(mode=0o755)
    socket_path = directory / "codex.sock"
    with pytest.raises(CodexWorkerError):
        CodexWorkerClient(socket_path)


def test_client_requires_existing_legitimate_socket() -> None:
    directory, socket_path = short_socket_path()
    try:
        with pytest.raises(CodexWorkerError):
            CodexWorkerClient(socket_path)
    finally:
        shutil.rmtree(directory)


@async_test
async def test_client_recursively_rejects_secret_in_safe_shaped_result() -> None:
    directory, socket_path = short_socket_path()

    async def malicious(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read()
        result = {
            "ok": True,
            "result": {
                "kind": "unknown",
                "target_monitor_ids": [],
                "target_url": None,
                "condition_text": None,
                "schedule_text": None,
                "clarification": "Bearer abcdefghijklmnopqrstuvwxyz",
                "confidence": 0.0,
            },
        }
        payload = json.dumps(result).encode()
        writer.write(len(payload).to_bytes(4, "big") + payload)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_unix_server(malicious, path=socket_path)
    socket_path.chmod(0o600)
    try:
        client = CodexWorkerClient(socket_path)
        with pytest.raises(CodexWorkerError):
            await client.run(intent())
    finally:
        server.close()
        await server.wait_closed()
        socket_path.unlink(missing_ok=True)
        shutil.rmtree(directory)


@async_test
async def test_server_and_client_reject_parent_rename_replacement() -> None:
    directory, socket_path = short_socket_path()
    harness = SecureHarness(directory)
    server = CodexWorkerServer(
        socket_path,
        harness.cli,
        auth_check=harness.guard.check,
    )
    await server.start()
    client = CodexWorkerClient(socket_path)
    moved = directory.with_name(directory.name + "-moved")
    directory.rename(moved)
    directory.mkdir(mode=0o700)
    try:
        with pytest.raises(CodexWorkerError):
            await server.close()
        with pytest.raises(CodexWorkerError):
            await client.run(intent())
    finally:
        directory.rmdir()
        moved.rename(directory)
        shutil.rmtree(directory)


@async_test
async def test_client_rejects_same_mode_socket_identity_replacement() -> None:
    directory, socket_path = short_socket_path()
    harness = SecureHarness(directory)
    secured_server = CodexWorkerServer(
        socket_path,
        harness.cli,
        auth_check=harness.guard.check,
    )
    await secured_server.start()
    client = CodexWorkerClient(socket_path)
    socket_path.unlink()

    async def malicious(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        payload = json.dumps(
            {
                "ok": True,
                "result": {
                    "kind": "unknown",
                    "target_monitor_ids": [],
                    "target_url": None,
                    "condition_text": None,
                    "schedule_text": None,
                    "clarification": None,
                    "confidence": 0.0,
                },
            }
        ).encode()
        writer.write(len(payload).to_bytes(4, "big") + payload)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    malicious_server = await asyncio.start_unix_server(malicious, path=socket_path)
    socket_path.chmod(0o600)
    try:
        with pytest.raises(CodexWorkerError):
            await client.run(intent())
        with pytest.raises(CodexWorkerError):
            await client.check()
        assert harness.exec_calls == 0
    finally:
        malicious_server.close()
        await malicious_server.wait_closed()
        socket_path.unlink(missing_ok=True)
        await secured_server.close()
        shutil.rmtree(directory)


@async_test
async def test_deep_frame_never_invokes() -> None:
    directory, socket_path = short_socket_path()
    harness = SecureHarness(directory)
    server = CodexWorkerServer(
        socket_path,
        harness.cli,
        auth_check=harness.guard.check,
    )
    await server.start()
    try:
        reader, writer = await asyncio.open_unix_connection(socket_path)
        deep = ("[" * 80 + "0" + "]" * 80).encode()
        writer.write(len(deep).to_bytes(4, "big") + deep)
        writer.write_eof()
        await writer.drain()
        await reader.read()
        assert harness.exec_calls == 0
        writer.close()
        await writer.wait_closed()

        await server.close()
        assert not server._tasks
        assert not server._connections
        assert not socket_path.exists()
    finally:
        with suppress(FileNotFoundError):
            await server.close()
        shutil.rmtree(directory)


@async_test
async def test_start_failure_removes_only_created_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory, socket_path = short_socket_path()
    harness = SecureHarness(directory)
    server = CodexWorkerServer(
        socket_path,
        harness.cli,
        auth_check=harness.guard.check,
    )

    async def fail_start(*_args: object, **_kwargs: object):
        raise RuntimeError("private-start-failure")

    monkeypatch.setattr("personal_monitor.ai.worker.asyncio.start_unix_server", fail_start)
    try:
        with pytest.raises(CodexWorkerError) as caught:
            await server.start()
        assert "private-start-failure" not in f"{caught.value!s} {caught.value!r}"
        assert not socket_path.exists()
    finally:
        shutil.rmtree(directory)


@async_test
async def test_slowloris_header_times_out_without_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory, socket_path = short_socket_path()
    harness = SecureHarness(directory)
    server = CodexWorkerServer(
        socket_path,
        harness.cli,
        auth_check=harness.guard.check,
    )
    monkeypatch.setattr("personal_monitor.ai.worker._READ_TIMEOUT", 0.02)
    await server.start()
    try:
        reader, writer = await asyncio.open_unix_connection(socket_path)
        writer.write(b"\x00\x00")
        await writer.drain()
        response = await asyncio.wait_for(reader.read(), 1)
        assert response
        assert harness.exec_calls == 0
        writer.close()
        await writer.wait_closed()
    finally:
        await server.close()
        shutil.rmtree(directory)


@async_test
async def test_close_cancels_active_handler_and_clears_bookkeeping() -> None:
    directory, socket_path = short_socket_path()
    harness = SecureHarness(directory)
    server = CodexWorkerServer(
        socket_path,
        harness.cli,
        auth_check=harness.guard.check,
    )
    await server.start()
    _reader, writer = await asyncio.open_unix_connection(socket_path)
    writer.write((100).to_bytes(4, "big"))
    await writer.drain()
    for _ in range(20):
        if server._tasks:
            break
        await asyncio.sleep(0)
    assert server._tasks
    close_task = asyncio.create_task(server.close())
    await asyncio.sleep(0)
    writer.close()
    with suppress(Exception):
        await writer.wait_closed()
    await close_task
    assert not server._tasks
    assert not server._connections
    assert not socket_path.exists()
    shutil.rmtree(directory)


async def _raw_exchange(socket_path: Path, value: object) -> object:
    reader, writer = await asyncio.open_unix_connection(socket_path)
    payload = json.dumps(value, separators=(",", ":")).encode()
    writer.write(len(payload).to_bytes(4, "big") + payload)
    writer.write_eof()
    await writer.drain()
    data = await reader.read()
    writer.close()
    await writer.wait_closed()
    size = int.from_bytes(data[:4], "big")
    assert len(data) == size + 4
    return json.loads(data[4:])


@async_test
async def test_auth_status_uses_guard_without_model_invocation() -> None:
    directory, socket_path = short_socket_path()
    harness = SecureHarness(directory)
    server = CodexWorkerServer(
        socket_path,
        harness.cli,
        auth_check=harness.guard.check,
    )
    await server.start()
    try:
        client = CodexWorkerClient(socket_path)
        await server._active.acquire()
        try:
            assert await client.check() is None
            assert server._active.locked()
        finally:
            server._active.release()
        assert harness.auth_calls == 1
        assert harness.exec_calls == 0
    finally:
        await server.close()
        shutil.rmtree(directory)


@async_test
async def test_auth_status_returns_only_fixed_failure_without_model_invocation() -> None:
    directory, socket_path = short_socket_path()
    harness = SecureHarness(directory, authenticated=False)
    server = CodexWorkerServer(
        socket_path,
        harness.cli,
        auth_check=harness.guard.check,
    )
    await server.start()
    try:
        response = await _raw_exchange(socket_path, {"kind": "auth_status"})
        assert response == {"ok": False, "error_code": "auth_failed"}
        client = CodexWorkerClient(socket_path)
        with pytest.raises(CodexWorkerError):
            await client.check()
        assert harness.auth_calls == 2
        assert harness.exec_calls == 0
    finally:
        await server.close()
        shutil.rmtree(directory)


@async_test
@pytest.mark.parametrize(
    "payload_value",
    [
        {"kind": "auth_status", "extra": True},
        {"kind": "auth_status", "token": "Bearer abcdefghijklmnopqrstuvwxyz"},
        {"kind": "auth_status", "request": {}},
        {"kind": "auth-status"},
    ],
)
async def test_auth_status_rejects_malformed_extra_and_secret_requests(
    payload_value: object,
) -> None:
    directory, socket_path = short_socket_path()
    harness = SecureHarness(directory)
    server = CodexWorkerServer(
        socket_path,
        harness.cli,
        auth_check=harness.guard.check,
    )
    await server.start()
    try:
        response = await _raw_exchange(socket_path, payload_value)
        assert response == {"ok": False, "error_code": "invalid_request"}
        assert harness.auth_calls == 0
        assert harness.exec_calls == 0
    finally:
        await server.close()
        shutil.rmtree(directory)


@async_test
async def test_auth_client_rejects_extra_secret_response() -> None:
    directory, socket_path = short_socket_path()

    async def malicious(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read()
        payload = json.dumps(
            {
                "ok": True,
                "authenticated": True,
                "detail": "Bearer abcdefghijklmnopqrstuvwxyz",
            }
        ).encode()
        writer.write(len(payload).to_bytes(4, "big") + payload)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_unix_server(malicious, path=socket_path)
    socket_path.chmod(0o600)
    try:
        client = CodexWorkerClient(socket_path)
        with pytest.raises(CodexWorkerError):
            await client.check()
    finally:
        server.close()
        await server.wait_closed()
        socket_path.unlink(missing_ok=True)
        shutil.rmtree(directory)


@async_test
async def test_auth_status_cancellation_propagates_without_model_invocation() -> None:
    directory, socket_path = short_socket_path()
    harness = SecureHarness(directory, auth_wait_delay=30)
    server = CodexWorkerServer(
        socket_path,
        harness.cli,
        auth_check=harness.guard.check,
    )
    reader = asyncio.StreamReader()
    payload = b'{"kind":"auth_status"}'
    reader.feed_data(len(payload).to_bytes(4, "big") + payload)
    reader.feed_eof()
    task = asyncio.create_task(server._response_for(reader))
    for _ in range(100):
        if harness.auth_calls:
            break
        await asyncio.sleep(0)
    assert harness.auth_calls == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert harness.exec_calls == 0
    shutil.rmtree(directory)


def test_worker_rejects_unsealed_auth_capability() -> None:
    directory, socket_path = short_socket_path()
    harness = SecureHarness(directory)

    class Arbitrary:
        async def check(self) -> None:
            raise AssertionError

    try:
        with pytest.raises(CodexWorkerError):
            CodexWorkerServer(
                socket_path,
                harness.cli,
                auth_check=Arbitrary().check,
            )
    finally:
        shutil.rmtree(directory)


def test_worker_rejects_auth_capability_from_different_guard() -> None:
    directory, socket_path = short_socket_path()
    harness = SecureHarness(directory)
    other_home = directory / "other-home"
    other_home.mkdir(mode=0o700)

    async def auth_spawn(*_argv: str, **_kwargs: object) -> FakeProcess:
        return FakeProcess(b"Logged in using ChatGPT\n")

    other_guard = CodexAuthGuard(
        "/usr/bin/true",
        other_home,
        process_factory=auth_spawn,
    )
    try:
        with pytest.raises(CodexWorkerError):
            CodexWorkerServer(
                socket_path,
                harness.cli,
                auth_check=other_guard.check,
            )
    finally:
        shutil.rmtree(directory)
