from __future__ import annotations

import asyncio
import inspect
import json
import math
import os
import shutil
import stat
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from personal_monitor.security.secret_text import contains_sensitive_text

from .auth import CodexAuthError, CodexAuthGuard, _safe_directory, _stop_process
from .contracts import (
    IntentRequest,
    PlanRequest,
    RepairRequest,
    RequestModel,
    ResultModel,
    UrlDiscoveryRequest,
    result_type_for,
)
from .launcher import LauncherError, resolve_launcher
from .prompts import prompt_for

MAX_STREAM_BYTES: Final = 1024 * 1024
MAX_LINE_BYTES: Final = 128 * 1024
MAX_EVENTS: Final = 4_096
MAX_RESULT_BYTES: Final = 256 * 1024
MAX_FRAME_BYTES: Final = 256 * 1024
MAX_JSON_DEPTH: Final = 64
MAX_JSON_NODES: Final = 10_000
MAX_JSON_STRING_CHARS: Final = 40_000
MAX_JSON_ARRAY_ITEMS: Final = 1_000
_RUN_TIMEOUT: Final = 120.0
_STDIN_CLOSE_TIMEOUT: Final = 1.0
_SPAWN = asyncio.create_subprocess_exec
_ALLOWED_MODELS: Final = {
    ("gpt-5.6-terra", "medium"),
    ("gpt-5.6-sol", "high"),
}
_ALLOWED_ITEM_TYPES: Final = {"agent_message", "reasoning"}
ProcessFactory = Callable[..., Awaitable[object]]


def _web_search_mode(request: RequestModel) -> str:
    return "live" if type(request) is UrlDiscoveryRequest else "disabled"


class CodexProtocolError(RuntimeError):
    __slots__ = ()

    def __init__(self) -> None:
        super().__init__("Codex protocol failure")

    def __repr__(self) -> str:
        return "CodexProtocolError(<redacted>)"


def _safe_owned_directory(path: Path) -> Path:
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
        if (
            path != resolved
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise CodexProtocolError
        return resolved
    except CodexProtocolError:
        raise
    except Exception:
        raise CodexProtocolError from None


def _write_exclusive(path: Path, data: bytes) -> tuple[int, int]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
        try:
            written = 0
            while written < len(data):
                count = os.write(descriptor, data[written:])
                if count <= 0:
                    raise CodexProtocolError
                written += count
            os.fsync(descriptor)
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
                raise CodexProtocolError
            return metadata.st_dev, metadata.st_ino
        finally:
            os.close(descriptor)
    except CodexProtocolError:
        raise
    except Exception:
        raise CodexProtocolError from None


def _read_pinned(path: Path, identity: tuple[int, int], limit: int) -> bytes:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or (metadata.st_dev, metadata.st_ino) != identity
            or metadata.st_uid != os.geteuid()
            or metadata.st_size <= 0
            or metadata.st_size > limit
        ):
            raise CodexProtocolError
        data = bytearray()
        while True:
            chunk = os.read(descriptor, min(8192, limit + 1 - len(data)))
            if not chunk:
                return bytes(data)
            data.extend(chunk)
            if len(data) > limit:
                raise CodexProtocolError
    except CodexProtocolError:
        raise
    except Exception:
        raise CodexProtocolError from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _remove_workspace_sync(path: Path, identity: tuple[int, int]) -> None:
    try:
        metadata = path.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or (metadata.st_dev, metadata.st_ino) != identity:
            raise CodexProtocolError
        shutil.rmtree(path)
        if path.exists() or path.is_symlink():
            raise CodexProtocolError
    except CodexProtocolError:
        raise
    except Exception:
        raise CodexProtocolError from None


async def _remove_workspace(path: Path, identity: tuple[int, int]) -> None:
    task = asyncio.create_task(
        asyncio.wait_for(asyncio.to_thread(_remove_workspace_sync, path, identity), 5.0)
    )
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError as cancellation:
        try:
            await asyncio.shield(task)
        except BaseException:
            cancellation.add_note("Codex workspace cleanup failed")
        raise
    except CodexProtocolError:
        raise
    except Exception:
        raise CodexProtocolError from None


def _bounded_json(
    data: bytes,
    *,
    object_pairs_hook: Callable[
        [list[tuple[str, object]]],
        dict[str, object],
    ]
    | None = None,
) -> object:
    if not data or len(data) > MAX_FRAME_BYTES:
        raise CodexProtocolError
    try:
        text = data.decode("utf-8", "strict")
        value = json.loads(
            text,
            parse_int=lambda raw: _bounded_int(raw),
            parse_constant=lambda _raw: (_ for _ in ()).throw(CodexProtocolError()),
            object_pairs_hook=object_pairs_hook or _unique_object,
        )
    except CodexProtocolError:
        raise
    except Exception:
        raise CodexProtocolError from None
    _walk_json(value)
    return value


def _bounded_int(raw: str) -> int:
    if len(raw.lstrip("-")) > 19:
        raise CodexProtocolError
    return int(raw)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise CodexProtocolError
        value[key] = item
    return value


def _codex_event_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    duplicate_id = False
    for key, item in pairs:
        if key in value:
            if (
                key != "id"
                or type(value[key]) is not str
                or type(item) is not str
                or not 1 <= len(value[key]) <= 128
                or not 1 <= len(item) <= 128
            ):
                raise CodexProtocolError
            duplicate_id = True
            continue
        value[key] = item
    if duplicate_id and value.get("type") != "web_search":
        raise CodexProtocolError
    return value


def _walk_json(value: object) -> None:
    nodes = 0
    stack: list[tuple[object, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            raise CodexProtocolError
        if isinstance(current, str):
            try:
                encoded = current.encode("utf-8", "strict")
            except UnicodeError:
                raise CodexProtocolError from None
            if len(current) > MAX_JSON_STRING_CHARS or len(encoded) > MAX_FRAME_BYTES:
                raise CodexProtocolError
        elif type(current) is list:
            if len(current) > MAX_JSON_ARRAY_ITEMS:
                raise CodexProtocolError
            stack.extend((item, depth + 1) for item in current)
        elif type(current) is dict:
            if len(current) > MAX_JSON_ARRAY_ITEMS:
                raise CodexProtocolError
            for key, item in current.items():
                if type(key) is not str or len(key) > 256:
                    raise CodexProtocolError
                stack.append((key, depth + 1))
                stack.append((item, depth + 1))
        elif current is not None:
            if type(current) not in {bool, int, float}:
                raise CodexProtocolError
            if type(current) is float and not math.isfinite(current):
                raise CodexProtocolError


def _validate_event(line: bytes, state: int, *, allow_web_search: bool = False) -> int:
    value = _bounded_json(line, object_pairs_hook=_codex_event_object)
    if type(value) is not dict:
        raise CodexProtocolError
    event_type = value.get("type")
    if state == 0:
        if (
            event_type != "thread.started"
            or set(value) != {"type", "thread_id"}
            or type(value["thread_id"]) is not str
            or not 1 <= len(value["thread_id"]) <= 128
        ):
            raise CodexProtocolError
        return 1
    if state == 1:
        if event_type != "turn.started" or set(value) != {"type"}:
            raise CodexProtocolError
        return 2
    if state != 2:
        raise CodexProtocolError
    if event_type in {"item.started", "item.updated", "item.completed"}:
        item = value.get("item")
        if (
            allow_web_search
            and set(value) == {"type", "item"}
            and type(item) is dict
            and item.get("type") == "web_search"
            and type(item.get("id")) is str
            and 1 <= len(item["id"]) <= 128
        ):
            return 2
    if event_type == "item.completed":
        if set(value) != {"type", "item"}:
            raise CodexProtocolError
        item = value.get("item")
        if (
            type(item) is not dict
            or set(item) != {"id", "type", "text"}
            or type(item.get("id")) is not str
            or not 1 <= len(item["id"]) <= 128
            or item["type"] not in _ALLOWED_ITEM_TYPES
            or type(item.get("text")) is not str
            or len(item["text"]) > MAX_JSON_STRING_CHARS
        ):
            raise CodexProtocolError
        return 2
    if event_type == "turn.completed":
        if set(value) != {"type", "usage"} or type(value["usage"]) is not dict:
            raise CodexProtocolError
        usage = value["usage"]
        required_usage = {"input_tokens", "cached_input_tokens", "output_tokens"}
        if (
            not required_usage.issubset(usage)
            or bool(set(usage) - required_usage - {"reasoning_output_tokens"})
            or any(
                type(usage[name]) is not int or not 0 <= usage[name] <= 2**63 - 1 for name in usage
            )
        ):
            raise CodexProtocolError
        return 3
    raise CodexProtocolError


async def _read_stream(
    stream: object,
    *,
    events: bool,
    allow_web_search: bool = False,
) -> bytes:
    data = bytearray()
    pending = bytearray()
    event_count = 0
    state = 0
    while True:
        chunk = await stream.read(8192)  # type: ignore[attr-defined]
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > MAX_STREAM_BYTES:
            raise CodexProtocolError
        pending.extend(chunk)
        if len(pending) > MAX_LINE_BYTES and b"\n" not in pending:
            raise CodexProtocolError
        while b"\n" in pending:
            raw_line, _, remainder = pending.partition(b"\n")
            pending = bytearray(remainder)
            if raw_line.endswith(b"\r"):
                raw_line = raw_line[:-1]
            if not raw_line or len(raw_line) > MAX_LINE_BYTES:
                raise CodexProtocolError
            event_count += 1
            if event_count > MAX_EVENTS:
                raise CodexProtocolError
            if events:
                state = _validate_event(
                    bytes(raw_line),
                    state,
                    allow_web_search=allow_web_search,
                )
    if pending:
        if len(pending) > MAX_LINE_BYTES:
            raise CodexProtocolError
        event_count += 1
        if event_count > MAX_EVENTS:
            raise CodexProtocolError
        if events:
            state = _validate_event(
                bytes(pending),
                state,
                allow_web_search=allow_web_search,
            )
    if events and state != 3:
        raise CodexProtocolError
    return bytes(data)


def _scan_secrets(value: object) -> None:
    stack = [value]
    while stack:
        current = stack.pop()
        if type(current) is dict:
            stack.extend(current.keys())
            stack.extend(current.values())
        elif type(current) in {list, tuple}:
            stack.extend(current)
        elif isinstance(current, str):
            if contains_sensitive_text(current):
                raise CodexProtocolError
        elif current is not None and type(current) not in {bool, int, float}:
            raise CodexProtocolError


async def _wait_process_streams(
    process: object,
    *,
    allow_web_search: bool = False,
) -> tuple[bytes, bytes, int]:
    tasks = (
        asyncio.create_task(  # type: ignore[attr-defined]
            _read_stream(
                process.stdout,
                events=True,
                allow_web_search=allow_web_search,
            )
        ),
        asyncio.create_task(_read_stream(process.stderr, events=False)),  # type: ignore[attr-defined]
        asyncio.create_task(process.wait()),  # type: ignore[attr-defined]
    )
    try:
        return await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _interact_with_process(
    process: object,
    request_data: bytes,
    *,
    allow_web_search: bool = False,
) -> tuple[bytes, bytes, int]:
    written = process.stdin.write(request_data)  # type: ignore[attr-defined]
    if inspect.isawaitable(written):
        await written
    await process.stdin.drain()  # type: ignore[attr-defined]
    process.stdin.close()  # type: ignore[attr-defined]
    with suppress(AttributeError):
        await process.stdin.wait_closed()  # type: ignore[attr-defined]
    return await _wait_process_streams(process, allow_web_search=allow_web_search)


async def _cleanup_process(process: object) -> None:
    with suppress(BaseException):
        process.stdin.close()  # type: ignore[attr-defined]
    with suppress(BaseException):
        await asyncio.wait_for(  # type: ignore[attr-defined]
            process.stdin.wait_closed(), _STDIN_CLOSE_TIMEOUT
        )
    await _stop_process(process)


async def _shielded_process_cleanup(process: object) -> None:
    task = asyncio.create_task(_cleanup_process(process))
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.shield(task)
        raise


class CodexCli:
    __slots__ = (
        "_auth_guard",
        "_auth_guard_anchor",
        "_codex_home",
        "_codex_home_identity",
        "_launcher",
        "_launcher_anchor",
        "_process_factory",
        "_process_factory_anchor",
        "_task_root",
        "_task_root_identity",
    )

    def __init__(
        self,
        codex_binary: str,
        codex_home: Path,
        task_root: Path,
        *,
        auth_guard: CodexAuthGuard,
        node_binary: str | None = None,
        process_factory: ProcessFactory = _SPAWN,
    ) -> None:
        if type(self) is not CodexCli:
            raise CodexProtocolError
        if type(auth_guard) is not CodexAuthGuard:
            raise TypeError("auth_guard must be an exact CodexAuthGuard")
        try:
            launcher = resolve_launcher(codex_binary, node_binary=node_binary)
            guard_launcher, guard_home, guard_home_identity = auth_guard._trusted_snapshot()
        except (LauncherError, CodexAuthError):
            raise CodexProtocolError from None
        home = _safe_directory(Path(codex_home))
        if (
            launcher != guard_launcher
            or home != guard_home
            or (home.stat().st_dev, home.stat().st_ino) != guard_home_identity
        ):
            raise CodexProtocolError
        root = _safe_owned_directory(Path(task_root))
        home_metadata = home.stat()
        root_metadata = root.stat()
        object.__setattr__(self, "_auth_guard", auth_guard)
        object.__setattr__(self, "_auth_guard_anchor", auth_guard)
        object.__setattr__(self, "_launcher", launcher)
        object.__setattr__(self, "_launcher_anchor", launcher)
        object.__setattr__(self, "_codex_home", home)
        object.__setattr__(
            self, "_codex_home_identity", (home_metadata.st_dev, home_metadata.st_ino)
        )
        object.__setattr__(self, "_task_root", root)
        object.__setattr__(
            self, "_task_root_identity", (root_metadata.st_dev, root_metadata.st_ino)
        )
        object.__setattr__(self, "_process_factory", process_factory)
        object.__setattr__(self, "_process_factory_anchor", process_factory)

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("CodexCli composition is sealed")

    def __repr__(self) -> str:
        return "<CodexCli redacted>"

    async def run(
        self,
        request: RequestModel,
        schema: Mapping[str, object],
        model: str,
        effort: str,
    ) -> ResultModel:
        if (
            (model, effort) not in _ALLOWED_MODELS
            or type(request)
            not in {IntentRequest, UrlDiscoveryRequest, PlanRequest, RepairRequest}
            or type(schema) is not dict
            or self._process_factory is not self._process_factory_anchor
            or self._auth_guard is not self._auth_guard_anchor
            or self._launcher is not self._launcher_anchor
        ):
            raise CodexProtocolError
        _scan_secrets(request.model_dump(mode="python"))
        await self._auth_guard_anchor.check()
        expected_type = result_type_for(request)
        expected_schema = expected_type.model_json_schema()
        _walk_json(schema)
        if schema != expected_schema:
            raise CodexProtocolError
        request_data = request.model_dump_json().encode("utf-8")
        if len(request_data) > MAX_FRAME_BYTES:
            raise CodexProtocolError
        schema_data = json.dumps(
            expected_schema, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        workspace: Path | None = None
        workspace_identity: tuple[int, int] | None = None
        process: object | None = None
        result: ResultModel | None = None
        try:
            root = _safe_owned_directory(self._task_root)
            home = _safe_directory(self._codex_home)
            self._launcher_anchor.verify()
            guard_launcher, guard_home, guard_home_identity = (
                self._auth_guard_anchor._trusted_snapshot()
            )
            if (
                guard_launcher != self._launcher_anchor
                or guard_home != home
                or guard_home_identity != self._codex_home_identity
                or (root.stat().st_dev, root.stat().st_ino) != self._task_root_identity
                or (home.stat().st_dev, home.stat().st_ino) != self._codex_home_identity
            ):
                raise CodexProtocolError
            workspace = Path(tempfile.mkdtemp(prefix=".codex-", dir=self._task_root))
            workspace_metadata = workspace.lstat()
            workspace_identity = (workspace_metadata.st_dev, workspace_metadata.st_ino)
            if (
                not stat.S_ISDIR(workspace_metadata.st_mode)
                or workspace_metadata.st_uid != os.geteuid()
            ):
                raise CodexProtocolError
            os.chmod(workspace, 0o700, follow_symlinks=False)
            checked_workspace = workspace.lstat()
            if (
                not stat.S_ISDIR(checked_workspace.st_mode)
                or (checked_workspace.st_dev, checked_workspace.st_ino) != workspace_identity
                or checked_workspace.st_uid != os.geteuid()
                or stat.S_IMODE(checked_workspace.st_mode) != 0o700
            ):
                raise CodexProtocolError
            isolated = workspace / "work"
            isolated.mkdir(mode=0o700)
            schema_path = workspace / "schema.json"
            result_path = workspace / "result.json"
            _write_exclusive(schema_path, schema_data)
            result_identity = _write_exclusive(result_path, b"")
            argv = (
                *self._launcher_anchor.argv_prefix,
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
                f'web_search="{_web_search_mode(request)}"',
                "-c",
                'forced_login_method="chatgpt"',
                "-c",
                'shell_environment_policy.inherit="none"',
                "--cd",
                str(isolated),
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
            )
            env = {
                "CODEX_HOME": str(self._codex_home),
                "HOME": str(self._codex_home),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
            }
            async with asyncio.timeout(_RUN_TIMEOUT):
                process = await self._process_factory_anchor(
                    *argv,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                    start_new_session=True,
                )
                _stdout, _stderr, returncode = await _interact_with_process(
                    process,
                    request_data,
                    allow_web_search=type(request) is UrlDiscoveryRequest,
                )
            if returncode != 0:
                raise CodexProtocolError
            raw = _read_pinned(result_path, result_identity, MAX_RESULT_BYTES)
            parsed = _bounded_json(raw)
            if type(parsed) is not dict:
                raise CodexProtocolError
            _scan_secrets(parsed)
            try:
                validated_result = expected_type.model_validate(parsed)
            except ValidationError:
                raise CodexProtocolError from None
            _scan_secrets(validated_result.model_dump(mode="python"))
            result = validated_result
        except asyncio.CancelledError as cancellation:
            if process is not None:
                try:
                    await _shielded_process_cleanup(process)
                except BaseException:
                    cancellation.add_note("Codex process cleanup failed")
            if workspace is not None:
                try:
                    if workspace_identity is None:
                        raise CodexProtocolError
                    await _remove_workspace(workspace, workspace_identity)
                except BaseException:
                    cancellation.add_note("Codex workspace cleanup failed")
            raise cancellation
        except CodexProtocolError:
            if process is not None:
                with suppress(BaseException):
                    await _shielded_process_cleanup(process)
            if workspace is not None:
                if workspace_identity is None:
                    raise CodexProtocolError from None
                await _remove_workspace(workspace, workspace_identity)
            raise CodexProtocolError from None
        except BaseException as error:
            if process is not None:
                with suppress(BaseException):
                    await _shielded_process_cleanup(process)
            if workspace is not None:
                try:
                    if workspace_identity is None:
                        raise CodexProtocolError
                    await _remove_workspace(workspace, workspace_identity)
                except BaseException:
                    if not isinstance(error, Exception):
                        error.add_note("Codex workspace cleanup failed")
                    else:
                        raise CodexProtocolError from None
            if not isinstance(error, Exception):
                raise
            raise CodexProtocolError from None
        if workspace is None or workspace_identity is None or result is None:
            raise CodexProtocolError
        await _remove_workspace(workspace, workspace_identity)
        return result
