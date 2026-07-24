from __future__ import annotations

import asyncio
import json
import os
import socket
import stat
from contextlib import suppress
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from .auth import CodexAuthError, CodexAuthGuard
from .codex_cli import (
    MAX_FRAME_BYTES,
    CodexCli,
    CodexProtocolError,
    _bounded_json,
    _scan_secrets,
)
from .contracts import (
    IntentRequest,
    PlanRequest,
    RepairRequest,
    RequestModel,
    WorkerFailure,
    WorkerRequest,
    request_kind,
    result_type_for,
)

_READ_TIMEOUT: Final = 5.0
_EOF_TIMEOUT: Final = 1.0
_CLOSE_TIMEOUT: Final = 1.0
_RUN_TIMEOUT: Final = 125.0
_AUTH_CHECK_TIMEOUT: Final = 12.0
_HANDLER_TIMEOUT: Final = 128.0
_CLIENT_TIMEOUT: Final = 130.0


class CodexWorkerError(RuntimeError):
    __slots__ = ()

    def __init__(self) -> None:
        super().__init__("Codex worker unavailable")

    def __repr__(self) -> str:
        return "CodexWorkerError(<redacted>)"


def _safe_socket_parent(path: Path) -> tuple[Path, tuple[int, int]]:
    try:
        if not path.is_absolute() or path.name in {"", ".", ".."}:
            raise CodexWorkerError
        parent = path.parent
        metadata = parent.lstat()
        resolved = parent.resolve(strict=True)
        if (
            parent != resolved
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or path != resolved / path.name
        ):
            raise CodexWorkerError
        return resolved, (metadata.st_dev, metadata.st_ino)
    except CodexWorkerError:
        raise
    except Exception:
        raise CodexWorkerError from None


def _require_parent(path: Path, identity: tuple[int, int]) -> None:
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
        if (
            path != resolved
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or (metadata.st_dev, metadata.st_ino) != identity
        ):
            raise CodexWorkerError
    except CodexWorkerError:
        raise
    except Exception:
        raise CodexWorkerError from None


def _encode(value: object) -> bytes:
    try:
        payload = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    except Exception:
        raise CodexWorkerError from None
    if not payload or len(payload) > MAX_FRAME_BYTES:
        raise CodexWorkerError
    return len(payload).to_bytes(4, "big") + payload


async def _read_frame(
    reader: asyncio.StreamReader,
    *,
    header_timeout: float | None = None,
) -> object:
    try:
        timeout = _READ_TIMEOUT if header_timeout is None else header_timeout
        header = await asyncio.wait_for(reader.readexactly(4), timeout)
        size = int.from_bytes(header, "big")
        if size < 1 or size > MAX_FRAME_BYTES:
            raise CodexWorkerError
        payload = await asyncio.wait_for(reader.readexactly(size), _READ_TIMEOUT)
        trailing = await asyncio.wait_for(reader.read(1), _EOF_TIMEOUT)
        if trailing != b"":
            raise CodexWorkerError
        return _bounded_json(payload)
    except asyncio.CancelledError:
        raise
    except CodexWorkerError:
        raise
    except Exception:
        raise CodexWorkerError from None


async def _close_writer(writer: asyncio.StreamWriter) -> None:
    writer.close()
    with suppress(BaseException):
        await asyncio.wait_for(writer.wait_closed(), _CLOSE_TIMEOUT)


async def _shielded_close_writer(writer: asyncio.StreamWriter) -> None:
    task = asyncio.create_task(_close_writer(writer))
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.shield(task)
        raise


def _parse_worker_request(value: object) -> WorkerRequest:
    if type(value) is not dict:
        raise CodexWorkerError
    kind = value.get("kind")
    model_type: type[RequestModel]
    if kind == "intent":
        model_type = IntentRequest
    elif kind == "plan":
        model_type = PlanRequest
    elif kind == "repair":
        model_type = RepairRequest
    else:
        raise CodexWorkerError
    try:
        normalized = dict(value)
        normalized["request"] = model_type.model_validate(value.get("request"))
        return WorkerRequest.model_validate(normalized)
    except ValidationError:
        raise CodexWorkerError from None


def _is_auth_status_request(value: object) -> bool:
    return type(value) is dict and set(value) == {"kind"} and value.get("kind") == "auth_status"


class CodexWorkerServer:
    __slots__ = (
        "_active",
        "_auth_check",
        "_auth_check_anchor",
        "_connections",
        "_handle_anchor",
        "_identity",
        "_parent",
        "_parent_identity",
        "_run",
        "_run_anchor",
        "_server",
        "_socket_path",
        "_tasks",
    )

    def __init__(
        self,
        socket_path: Path,
        cli: CodexCli,
        *,
        auth_check: object,
        concurrency: int = 1,
    ) -> None:
        if type(self) is not CodexWorkerServer or concurrency != 1 or type(cli) is not CodexCli:
            raise CodexWorkerError
        if (
            not callable(auth_check)
            or getattr(auth_check, "__self__", None) is None
            or type(auth_check.__self__) is not CodexAuthGuard
            or getattr(auth_check, "__func__", None) is not CodexAuthGuard.check
            or auth_check.__self__ is not cli._auth_guard_anchor
        ):
            raise CodexWorkerError
        parent, parent_identity = _safe_socket_parent(Path(socket_path))
        path = parent / Path(socket_path).name
        if len(os.fsencode(path)) > 100:
            raise CodexWorkerError
        try:
            path.lstat()
        except FileNotFoundError:
            pass
        except Exception:
            raise CodexWorkerError from None
        else:
            raise CodexWorkerError
        try:
            run = cli.run
            if not callable(run):
                raise CodexWorkerError
        except CodexWorkerError:
            raise
        except Exception:
            raise CodexWorkerError from None
        object.__setattr__(self, "_socket_path", path)
        object.__setattr__(self, "_parent", parent)
        object.__setattr__(self, "_parent_identity", parent_identity)
        object.__setattr__(self, "_run", run)
        object.__setattr__(self, "_run_anchor", run)
        object.__setattr__(self, "_auth_check", auth_check)
        object.__setattr__(self, "_auth_check_anchor", auth_check)
        object.__setattr__(self, "_active", asyncio.Semaphore(1))
        object.__setattr__(self, "_connections", set())
        object.__setattr__(self, "_tasks", set())
        object.__setattr__(self, "_server", None)
        object.__setattr__(self, "_identity", None)
        object.__setattr__(self, "_handle_anchor", self._handle)

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("CodexWorkerServer composition is sealed")

    def _integrity_ok(self) -> bool:
        return (
            type(self) is CodexWorkerServer
            and self._run is self._run_anchor
            and self._auth_check is self._auth_check_anchor
            and self._auth_check_anchor.__func__ is CodexAuthGuard.check
            and type(self._auth_check_anchor.__self__) is CodexAuthGuard
            and self._handle_anchor.__self__ is self
            and self._handle_anchor.__func__ is CodexWorkerServer._handle
        )

    async def start(self) -> None:
        if not self._integrity_ok() or self._server is not None:
            raise CodexWorkerError
        _require_parent(self._parent, self._parent_identity)
        try:
            self._socket_path.lstat()
        except FileNotFoundError:
            pass
        except Exception:
            raise CodexWorkerError from None
        else:
            raise CodexWorkerError

        server: asyncio.AbstractServer | None = None
        raw_socket: socket.socket | None = None
        socket_identity: tuple[int, int] | None = None
        try:
            raw_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            raw_socket.setblocking(False)
            raw_socket.bind(str(self._socket_path))
            metadata = self._socket_path.lstat()
            if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.geteuid():
                raise CodexWorkerError
            socket_identity = (metadata.st_dev, metadata.st_ino)
            os.chmod(self._socket_path, 0o600, follow_symlinks=False)
            checked = self._socket_path.lstat()
            if (
                not stat.S_ISSOCK(checked.st_mode)
                or (checked.st_dev, checked.st_ino) != socket_identity
                or checked.st_uid != os.geteuid()
                or stat.S_IMODE(checked.st_mode) != 0o600
            ):
                raise CodexWorkerError
            _require_parent(self._parent, self._parent_identity)
            server = await asyncio.start_unix_server(self._handle_anchor, sock=raw_socket)
            raw_socket = None
            object.__setattr__(self, "_server", server)
            object.__setattr__(self, "_identity", socket_identity)
        except BaseException as error:
            if server is not None:
                server.close()
                with suppress(BaseException):
                    await asyncio.wait_for(server.wait_closed(), _CLOSE_TIMEOUT)
            if raw_socket is not None:
                raw_socket.close()
            if socket_identity is not None:
                self._unlink_exact(socket_identity)
            if not isinstance(error, Exception):
                raise
            raise CodexWorkerError from None

    def _unlink_exact(self, identity: tuple[int, int]) -> None:
        try:
            metadata = self._socket_path.lstat()
            if stat.S_ISSOCK(metadata.st_mode) and (metadata.st_dev, metadata.st_ino) == identity:
                self._socket_path.unlink()
        except FileNotFoundError:
            pass
        except Exception:
            raise CodexWorkerError from None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        self._connections.add(writer)
        try:
            async with asyncio.timeout(_HANDLER_TIMEOUT):
                response = await self._response_for(reader)
                writer.write(_encode(response))
                await writer.drain()
        except asyncio.CancelledError:
            raise
        except Exception:
            with suppress(Exception):
                writer.write(
                    _encode(WorkerFailure(ok=False, error_code="worker_failed").model_dump())
                )
                await writer.drain()
        finally:
            try:
                await _shielded_close_writer(writer)
            finally:
                self._connections.discard(writer)
                if task is not None:
                    self._tasks.discard(task)

    async def _response_for(self, reader: asyncio.StreamReader) -> object:
        try:
            value = await _read_frame(reader)
            if _is_auth_status_request(value):
                await asyncio.wait_for(
                    self._auth_check_anchor(),
                    _AUTH_CHECK_TIMEOUT,
                )
                return {"ok": True, "authenticated": True}
            request = _parse_worker_request(value)
            if self._active.locked():
                return WorkerFailure(ok=False, error_code="busy").model_dump()
            await self._active.acquire()
            try:
                result_type = result_type_for(request.request)
                result = await asyncio.wait_for(
                    self._run_anchor(
                        request.request,
                        result_type.model_json_schema(),
                        request.model,
                        request.effort,
                    ),
                    _RUN_TIMEOUT,
                )
            finally:
                self._active.release()
            validated = result_type.model_validate(result.model_dump(mode="python"))
            _scan_secrets(validated.model_dump(mode="python"))
            return {"ok": True, "result": validated.model_dump(mode="json")}
        except asyncio.CancelledError:
            raise
        except CodexAuthError:
            return WorkerFailure(ok=False, error_code="auth_failed").model_dump()
        except CodexProtocolError:
            return WorkerFailure(ok=False, error_code="protocol_failed").model_dump()
        except CodexWorkerError:
            return WorkerFailure(ok=False, error_code="invalid_request").model_dump()
        except Exception:
            return WorkerFailure(ok=False, error_code="worker_failed").model_dump()

    async def close(self) -> None:
        parent_error: CodexWorkerError | None = None
        try:
            _require_parent(self._parent, self._parent_identity)
        except CodexWorkerError as error:
            parent_error = error
        server = self._server
        if server is not None:
            server.close()
            with suppress(BaseException):
                await asyncio.wait_for(server.wait_closed(), _CLOSE_TIMEOUT)
            object.__setattr__(self, "_server", None)
        current = asyncio.current_task()
        tasks = tuple(task for task in self._tasks if task is not current)
        for task in tasks:
            task.cancel()
        if tasks:
            with suppress(BaseException):
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True), _CLOSE_TIMEOUT
                )
        writers = tuple(self._connections)
        if writers:
            with suppress(BaseException):
                await asyncio.wait_for(
                    asyncio.gather(
                        *(_close_writer(writer) for writer in writers),
                        return_exceptions=True,
                    ),
                    _CLOSE_TIMEOUT,
                )
        identity = self._identity
        if parent_error is None and identity is not None:
            self._unlink_exact(identity)
        object.__setattr__(self, "_identity", None)
        if parent_error is not None:
            raise parent_error


class CodexWorkerClient:
    __slots__ = ("_parent", "_parent_identity", "_socket_path")

    def __init__(self, socket_path: Path) -> None:
        if type(self) is not CodexWorkerClient:
            raise CodexWorkerError
        if Path(socket_path).name != "worker.sock":
            raise CodexWorkerError
        parent, parent_identity = _safe_socket_parent(Path(socket_path))
        resolved_socket = parent / Path(socket_path).name
        object.__setattr__(self, "_parent", parent)
        object.__setattr__(self, "_parent_identity", parent_identity)
        object.__setattr__(self, "_socket_path", resolved_socket)

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("CodexWorkerClient composition is sealed")

    def _current_socket_identity(self) -> tuple[int, int]:
        try:
            _require_parent(self._parent, self._parent_identity)
            metadata = self._socket_path.lstat()
            if (
                not stat.S_ISSOCK(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise CodexWorkerError
            return metadata.st_dev, metadata.st_ino
        except CodexWorkerError:
            raise
        except Exception:
            raise CodexWorkerError from None

    async def _exchange(self, envelope: object) -> object:
        socket_identity = self._current_socket_identity()
        reader: asyncio.StreamReader
        writer: asyncio.StreamWriter
        reader, writer = await asyncio.open_unix_connection(self._socket_path)
        try:
            if self._current_socket_identity() != socket_identity:
                raise CodexWorkerError
            writer.write(_encode(envelope))
            await writer.drain()
            if not writer.can_write_eof():
                raise CodexWorkerError
            writer.write_eof()
            value = await _read_frame(reader, header_timeout=_CLIENT_TIMEOUT)
        finally:
            await _shielded_close_writer(writer)
        _scan_secrets(value)
        return value

    async def check(self) -> None:
        try:
            value = await asyncio.wait_for(
                self._exchange({"kind": "auth_status"}),
                _CLIENT_TIMEOUT,
            )
            if type(value) is not dict or type(value.get("ok")) is not bool:
                raise CodexWorkerError
            if (
                set(value) != {"ok", "authenticated"}
                or value["ok"] is not True
                or type(value.get("authenticated")) is not bool
                or value["authenticated"] is not True
            ):
                raise CodexWorkerError
        except asyncio.CancelledError:
            raise
        except CodexWorkerError:
            raise
        except Exception:
            raise CodexWorkerError from None

    async def run(
        self,
        request: RequestModel,
        *,
        model: str = "gpt-5.6-terra",
        effort: str = "medium",
    ):
        try:
            envelope = WorkerRequest(
                kind=request_kind(request),
                request=request,
                model=model,
                effort=effort,
            )
            value = await asyncio.wait_for(
                self._exchange(envelope.model_dump(mode="json")),
                _CLIENT_TIMEOUT,
            )
            if type(value) is not dict or type(value.get("ok")) is not bool:
                raise CodexWorkerError
            if value["ok"] is not True or set(value) != {"ok", "result"}:
                raise CodexWorkerError
            result_type = result_type_for(request)
            try:
                result = result_type.model_validate(value["result"])
            except ValidationError:
                raise CodexWorkerError from None
            _scan_secrets(result.model_dump(mode="python"))
            return result
        except asyncio.CancelledError:
            raise
        except CodexWorkerError:
            raise
        except Exception:
            raise CodexWorkerError from None
