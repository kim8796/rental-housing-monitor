from __future__ import annotations

import asyncio
import functools
import os
import signal
from pathlib import Path

import pytest

from personal_monitor.ai.auth import CodexAuthError, CodexAuthGuard
from personal_monitor.ai.launcher import LauncherError, resolve_launcher


def async_test(function):
    @functools.wraps(function)
    def run(*args: object, **kwargs: object):
        return asyncio.run(function(*args, **kwargs))

    return run


class FakeStream:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def read(self, size: int = -1) -> bytes:
        if not self._data:
            return b""
        if size < 0:
            size = len(self._data)
        value, self._data = self._data[:size], self._data[size:]
        return value


class FakeProcess:
    def __init__(self, stdout: bytes, stderr: bytes = b"", returncode: int = 0) -> None:
        self.stdout = FakeStream(stdout)
        self.stderr = FakeStream(stderr)
        self.returncode = returncode
        self.pid = os.getpid()

    async def wait(self) -> int:
        return self.returncode


@async_test
@pytest.mark.parametrize("name", ["OPENAI_API_KEY", "CODEX_API_KEY"])
@pytest.mark.parametrize("value", ["secret", " "])
async def test_api_key_environment_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    home = tmp_path / "codex"
    home.mkdir(mode=0o700)
    called = False

    async def spawn(*_argv: str, **_kwargs: object) -> FakeProcess:
        nonlocal called
        called = True
        return FakeProcess(b"Logged in using ChatGPT\n")

    monkeypatch.setenv(name, value)
    guard = CodexAuthGuard("/usr/bin/true", home, process_factory=spawn)
    with pytest.raises(CodexAuthError, match=name):
        await guard.check()
    assert called is False
    if value.strip():
        assert value not in repr(guard)


@async_test
async def test_auth_uses_exact_argv_and_scrubbed_environment(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    home.mkdir(mode=0o700)
    observed: tuple[tuple[str, ...], dict[str, object]] | None = None

    async def spawn(*argv: str, **kwargs: object) -> FakeProcess:
        nonlocal observed
        observed = (argv, kwargs)
        return FakeProcess(b"Logged in using ChatGPT\n")

    guard = CodexAuthGuard("/usr/bin/true", home, process_factory=spawn)
    await guard.check()
    assert observed is not None
    argv, kwargs = observed
    assert argv == ("/usr/bin/true", "login", "status")
    assert kwargs["start_new_session"] is True
    env = kwargs["env"]
    assert isinstance(env, dict)
    assert env["CODEX_HOME"] == str(home.resolve())
    assert "OPENAI_API_KEY" not in env
    assert "CODEX_API_KEY" not in env


@async_test
async def test_auth_accepts_exact_chatgpt_status_on_stderr(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    home.mkdir(mode=0o700)

    async def spawn(*_argv: str, **_kwargs: object) -> FakeProcess:
        return FakeProcess(b"", b"Logged in using ChatGPT\n")

    guard = CodexAuthGuard("/usr/bin/true", home, process_factory=spawn)
    await guard.check()


@async_test
@pytest.mark.parametrize(
    ("stdout", "returncode"),
    [
        (b"Logged in using API key\n", 0),
        (b"Logged in using ChatGPT extra\n", 0),
        (b"private-status", 1),
    ],
)
async def test_non_chatgpt_status_is_fixed_and_redacted(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, stdout: bytes, returncode: int
) -> None:
    home = tmp_path / "codex"
    home.mkdir(mode=0o700)

    async def spawn(*_argv: str, **_kwargs: object) -> FakeProcess:
        return FakeProcess(stdout, b"private-stderr", returncode)

    guard = CodexAuthGuard("/usr/bin/true", home, process_factory=spawn)
    with pytest.raises(CodexAuthError) as caught:
        await guard.check()
    exposed = f"{caught.value!s} {caught.value!r} {caplog.text}"
    assert "private" not in exposed
    assert "codex login --device-auth" in exposed


def test_auth_rejects_missing_symlink_and_open_home(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(CodexAuthError):
        CodexAuthGuard("/usr/bin/true", missing)
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(CodexAuthError):
        CodexAuthGuard("/usr/bin/true", link)
    real.chmod(0o755)
    with pytest.raises(CodexAuthError):
        CodexAuthGuard("/usr/bin/true", real)


@async_test
async def test_auth_cancellation_is_preserved(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    home.mkdir(mode=0o700)

    async def spawn(*_argv: str, **_kwargs: object) -> FakeProcess:
        raise asyncio.CancelledError

    guard = CodexAuthGuard("/usr/bin/true", home, process_factory=spawn)
    with pytest.raises(asyncio.CancelledError):
        await guard.check()


def test_env_node_wrapper_uses_pinned_absolute_interpreter(tmp_path: Path) -> None:
    node = tmp_path / "node"
    node.write_bytes(b"native-placeholder")
    node.chmod(0o755)
    script = tmp_path / "codex"
    script.write_text("#!/usr/bin/env node\n// safe fixture\n", encoding="utf-8")
    script.chmod(0o755)

    launcher = resolve_launcher(str(script), node_binary=str(node))

    assert launcher.argv_prefix == (str(node.resolve()), str(script.resolve()))
    launcher.verify()


def test_env_node_wrapper_rejects_writable_or_unknown_runtime(tmp_path: Path) -> None:
    node = tmp_path / "node"
    node.write_bytes(b"native-placeholder")
    node.chmod(0o777)
    script = tmp_path / "codex"
    script.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    script.chmod(0o755)
    with pytest.raises(LauncherError):
        resolve_launcher(str(script), node_binary=str(node))
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    with pytest.raises(LauncherError):
        resolve_launcher(str(script), node_binary="/usr/bin/true")


@async_test
async def test_auth_output_overflow_terminates_and_reaps_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "codex"
    home.mkdir(mode=0o700)

    class HangingProcess(FakeProcess):
        def __init__(self) -> None:
            super().__init__(b"x" * (16 * 1024 + 1))
            self.returncode = None  # type: ignore[assignment]
            self.pid = 424_242
            self.waits = 0

        async def wait(self) -> int:
            self.waits += 1
            while self.returncode is None:
                await asyncio.sleep(0)
            return self.returncode

    process = HangingProcess()
    signals: list[int] = []

    def killpg(pid: int, sent: int) -> None:
        assert pid == process.pid
        signals.append(sent)
        process.returncode = -sent  # type: ignore[assignment]

    async def spawn(*_argv: str, **_kwargs: object) -> HangingProcess:
        return process

    monkeypatch.setattr("personal_monitor.ai.auth.os.killpg", killpg)
    guard = CodexAuthGuard("/usr/bin/true", home, process_factory=spawn)
    with pytest.raises(CodexAuthError):
        await guard.check()
    assert signals == [signal.SIGTERM]
    assert process.waits >= 1


@async_test
async def test_auth_timeout_is_fixed_and_terminates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "codex"
    home.mkdir(mode=0o700)

    class BlockingStream:
        async def read(self, _size: int) -> bytes:
            await asyncio.Future()
            return b""

    process = FakeProcess(b"")
    process.stdout = BlockingStream()
    process.stderr = BlockingStream()
    process.returncode = None  # type: ignore[assignment]
    process.pid = 515_151
    signals: list[int] = []

    async def wait() -> int:
        while process.returncode is None:
            await asyncio.sleep(0)
        return process.returncode

    process.wait = wait  # type: ignore[method-assign]

    def killpg(_pid: int, sent: int) -> None:
        signals.append(sent)
        process.returncode = -sent  # type: ignore[assignment]

    async def spawn(*_argv: str, **_kwargs: object) -> FakeProcess:
        return process

    monkeypatch.setattr("personal_monitor.ai.auth._AUTH_TIMEOUT", 0.01)
    monkeypatch.setattr("personal_monitor.ai.auth.os.killpg", killpg)
    guard = CodexAuthGuard("/usr/bin/true", home, process_factory=spawn)
    with pytest.raises(CodexAuthError):
        await guard.check()
    assert signals == [signal.SIGTERM]


@async_test
async def test_auth_escalates_to_kill_and_preserves_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "codex"
    home.mkdir(mode=0o700)

    class BlockingStream:
        async def read(self, _size: int) -> bytes:
            await asyncio.Future()
            return b""

    process = FakeProcess(b"")
    process.stdout = BlockingStream()
    process.stderr = BlockingStream()
    process.returncode = None  # type: ignore[assignment]
    process.pid = 616_161
    signals: list[int] = []

    async def wait() -> int:
        while process.returncode is None:
            await asyncio.sleep(0)
        return process.returncode

    process.wait = wait  # type: ignore[method-assign]

    def killpg(_pid: int, sent: int) -> None:
        signals.append(sent)
        if sent == signal.SIGKILL:
            process.returncode = -sent  # type: ignore[assignment]

    async def spawn(*_argv: str, **_kwargs: object) -> FakeProcess:
        return process

    monkeypatch.setattr("personal_monitor.ai.auth._PROCESS_STOP_TIMEOUT", 0.01)
    monkeypatch.setattr("personal_monitor.ai.auth.os.killpg", killpg)
    guard = CodexAuthGuard("/usr/bin/true", home, process_factory=spawn)
    task = asyncio.create_task(guard.check())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert process.returncode == -signal.SIGKILL
