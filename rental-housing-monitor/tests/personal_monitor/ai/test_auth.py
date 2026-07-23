from __future__ import annotations

import asyncio
import functools
import os
from pathlib import Path

import pytest

from personal_monitor.ai.auth import CodexAuthError, CodexAuthGuard


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
