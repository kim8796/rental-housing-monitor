from __future__ import annotations

import asyncio
import functools
import shutil
import tempfile
from pathlib import Path

import pytest

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


class FakeCli:
    def __init__(self) -> None:
        self.calls = 0
        self.active = 0
        self.max_active = 0

    async def run(
        self, request: IntentRequest, schema: dict[str, object], model: str, effort: str
    ) -> IntentResult:
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.01)
        self.active -= 1
        return IntentResult(
            kind=IntentKind.CREATE,
            target_monitor_ids=[],
            target_url="https://example.com",
            condition_text=None,
            schedule_text=None,
            clarification=None,
            confidence=1.0,
        )


@async_test
async def test_worker_socket_happy_path_permissions_and_concurrency() -> None:
    directory, socket_path = short_socket_path()
    cli = FakeCli()
    server = CodexWorkerServer(socket_path, cli)
    await server.start()
    try:
        assert socket_path.stat().st_mode & 0o777 == 0o600
        client = CodexWorkerClient(socket_path)
        requests = [
            IntentRequest(
                request_id=f"r-{index}",
                owner_id="telegram-user:7",
                message="모니터해줘",
                monitor_summaries=[],
            )
            for index in range(3)
        ]
        results = await asyncio.gather(
            *[client.run(item, model="gpt-5.6-terra", effort="medium") for item in requests]
        )
        assert all(result.kind is IntentKind.CREATE for result in results)
        assert cli.max_active == 1
    finally:
        await server.close()
        shutil.rmtree(directory)
    assert not socket_path.exists()


@async_test
@pytest.mark.parametrize("length", [0, 256 * 1024 + 1])
async def test_worker_rejects_invalid_frame_without_codex(length: int) -> None:
    directory, socket_path = short_socket_path()
    cli = FakeCli()
    server = CodexWorkerServer(socket_path, cli)
    await server.start()
    try:
        reader, writer = await asyncio.open_unix_connection(socket_path)
        writer.write(length.to_bytes(4, "big"))
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        await reader.read()
        assert cli.calls == 0
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
