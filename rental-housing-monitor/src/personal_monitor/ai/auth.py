from __future__ import annotations

import asyncio
import os
import signal
import stat
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import Final

from .launcher import LauncherError, PinnedLauncher, resolve_launcher

_AUTH_ERROR: Final = (
    "Codex ChatGPT 로그인이 필요합니다. 관리자가 codex login --device-auth를 실행하세요."
)
_MAX_STATUS_BYTES: Final = 16 * 1024
_AUTH_TIMEOUT: Final = 10.0
_PROCESS_STOP_TIMEOUT: Final = 1.0
_SPAWN = asyncio.create_subprocess_exec

ProcessFactory = Callable[..., Awaitable[object]]


class CodexAuthError(RuntimeError):
    __slots__ = ()

    def __repr__(self) -> str:
        return "CodexAuthError(<redacted>)"


def _safe_directory(path: Path) -> Path:
    try:
        original = Path(path)
        metadata = original.lstat()
        resolved = original.resolve(strict=True)
        resolved_metadata = resolved.stat()
        if (
            original != resolved
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or (metadata.st_dev, metadata.st_ino)
            != (resolved_metadata.st_dev, resolved_metadata.st_ino)
        ):
            raise CodexAuthError(_AUTH_ERROR)
        return resolved
    except CodexAuthError:
        raise
    except Exception:
        raise CodexAuthError(_AUTH_ERROR) from None


async def _read_bounded(stream: object, limit: int) -> bytes:
    data = bytearray()
    while True:
        chunk = await stream.read(min(4096, limit + 1 - len(data)))  # type: ignore[attr-defined]
        if not chunk:
            return bytes(data)
        data.extend(chunk)
        if len(data) > limit:
            raise CodexAuthError(_AUTH_ERROR)


async def _stop_process(process: object) -> None:
    if getattr(process, "returncode", None) is not None:
        with suppress(BaseException):
            await process.wait()  # type: ignore[attr-defined]
        return
    pid = getattr(process, "pid", None)
    if type(pid) is not int or pid <= 1:
        return
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(  # type: ignore[attr-defined]
            process.wait(), _PROCESS_STOP_TIMEOUT
        )
        return
    except BaseException:
        pass
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)
    with suppress(BaseException):
        await asyncio.wait_for(  # type: ignore[attr-defined]
            process.wait(), _PROCESS_STOP_TIMEOUT
        )


async def _wait_status(process: object) -> tuple[bytes, bytes, int]:
    tasks = (
        asyncio.create_task(_read_bounded(process.stdout, _MAX_STATUS_BYTES)),  # type: ignore[attr-defined]
        asyncio.create_task(_read_bounded(process.stderr, _MAX_STATUS_BYTES)),  # type: ignore[attr-defined]
        asyncio.create_task(process.wait()),  # type: ignore[attr-defined]
    )
    try:
        return await asyncio.wait_for(asyncio.gather(*tasks), _AUTH_TIMEOUT)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class CodexAuthGuard:
    __slots__ = (
        "_home",
        "_home_identity",
        "_launcher",
        "_launcher_anchor",
        "_process_factory",
        "_process_factory_anchor",
        "_sealed",
    )

    def __init__(
        self,
        codex_binary: str,
        codex_home: Path,
        *,
        node_binary: str | None = None,
        process_factory: ProcessFactory = _SPAWN,
    ) -> None:
        if type(self) is not CodexAuthGuard:
            raise CodexAuthError(_AUTH_ERROR)
        try:
            launcher = resolve_launcher(codex_binary, node_binary=node_binary)
        except LauncherError:
            raise CodexAuthError(_AUTH_ERROR) from None
        home = _safe_directory(Path(codex_home))
        home_metadata = home.stat()
        object.__setattr__(self, "_home", home)
        object.__setattr__(self, "_home_identity", (home_metadata.st_dev, home_metadata.st_ino))
        object.__setattr__(self, "_launcher", launcher)
        object.__setattr__(self, "_launcher_anchor", launcher)
        object.__setattr__(self, "_process_factory", process_factory)
        object.__setattr__(self, "_process_factory_anchor", process_factory)
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("CodexAuthGuard composition is sealed")

    def __repr__(self) -> str:
        return "<CodexAuthGuard redacted>"

    def _trusted_snapshot(self) -> tuple[PinnedLauncher, Path, tuple[int, int]]:
        if (
            type(self) is not CodexAuthGuard
            or self._launcher is not self._launcher_anchor
            or self._process_factory is not self._process_factory_anchor
        ):
            raise CodexAuthError(_AUTH_ERROR)
        try:
            self._launcher_anchor.verify()
            home = _safe_directory(self._home)
            metadata = home.stat()
            if (metadata.st_dev, metadata.st_ino) != self._home_identity:
                raise CodexAuthError(_AUTH_ERROR)
            return self._launcher_anchor, home, self._home_identity
        except (CodexAuthError, LauncherError):
            raise CodexAuthError(_AUTH_ERROR) from None
        except Exception:
            raise CodexAuthError(_AUTH_ERROR) from None

    async def check(self) -> None:
        for name in ("OPENAI_API_KEY", "CODEX_API_KEY"):
            if name in os.environ and os.environ[name] != "":
                raise CodexAuthError(f"{name} 환경 변수는 허용되지 않습니다.")
        launcher, home, _identity = self._trusted_snapshot()
        env = {
            "CODEX_HOME": str(home),
            "HOME": str(home),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }
        process: object | None = None
        try:
            process = await self._process_factory_anchor(
                *launcher.argv_prefix,
                "login",
                "status",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                start_new_session=True,
            )
            stdout, _stderr, returncode = await _wait_status(process)
            if returncode != 0 or stdout.decode("utf-8", "strict").strip() != (
                "Logged in using ChatGPT"
            ):
                raise CodexAuthError(_AUTH_ERROR)
        except asyncio.CancelledError:
            if process is not None:
                await asyncio.shield(_stop_process(process))
            raise
        except CodexAuthError:
            if process is not None:
                await _stop_process(process)
            raise
        except BaseException as error:
            if process is not None:
                await _stop_process(process)
            if not isinstance(error, Exception):
                raise
            raise CodexAuthError(_AUTH_ERROR) from None
