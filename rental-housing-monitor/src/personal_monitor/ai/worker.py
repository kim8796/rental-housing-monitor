from __future__ import annotations

import asyncio
import json
import os
import stat
from contextlib import suppress
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from .auth import CodexAuthError
from .codex_cli import MAX_FRAME_BYTES, CodexProtocolError, _bounded_json, _scan_secrets
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
_CLIENT_TIMEOUT: Final = 130.0


class CodexWorkerError(RuntimeError):
    __slots__ = ()

    def __init__(self) -> None:
        super().__init__("Codex worker unavailable")

    def __repr__(self) -> str:
        return "CodexWorkerError(<redacted>)"


def _safe_socket_parent(path: Path) -> Path:
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
        return resolved
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


async def _read_frame(reader: asyncio.StreamReader) -> object:
    try:
        header = await asyncio.wait_for(reader.readexactly(4), _READ_TIMEOUT)
        size = int.from_bytes(header, "big")
        if size < 1 or size > MAX_FRAME_BYTES:
            raise CodexWorkerError
        payload = await asyncio.wait_for(reader.readexactly(size), _READ_TIMEOUT)
        try:
            trailing = await asyncio.wait_for(reader.read(1), 0.02)
        except TimeoutError:
            trailing = b""
        if trailing:
            raise CodexWorkerError
        return _bounded_json(payload)
    except asyncio.CancelledError:
        raise
    except CodexWorkerError:
        raise
    except Exception:
        raise CodexWorkerError from None


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


class CodexWorkerServer:
    __slots__ = (
        "_active",
        "_connections",
        "_identity",
        "_run",
        "_server",
        "_socket_path",
        "_tasks",
    )

    def __init__(self, socket_path: Path, cli: object, *, concurrency: int = 1) -> None:
        if concurrency != 1:
            raise CodexWorkerError
        parent = _safe_socket_parent(Path(socket_path))
        path = parent / Path(socket_path).name
        if len(os.fsencode(path)) > 100 or path.exists() or path.is_symlink():
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
        object.__setattr__(self, "_run", run)
        object.__setattr__(self, "_active", asyncio.Semaphore(1))
        object.__setattr__(self, "_connections", set())
        object.__setattr__(self, "_tasks", set())
        object.__setattr__(self, "_server", None)
        object.__setattr__(self, "_identity", None)

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("CodexWorkerServer composition is sealed")

    async def start(self) -> None:
        if self._server is not None or self._socket_path.exists():
            raise CodexWorkerError
        try:
            server = await asyncio.start_unix_server(self._handle, path=self._socket_path)
            os.chmod(self._socket_path, 0o600)
            metadata = self._socket_path.lstat()
            if (
                not stat.S_ISSOCK(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                server.close()
                await server.wait_closed()
                raise CodexWorkerError
            object.__setattr__(self, "_server", server)
            object.__setattr__(self, "_identity", (metadata.st_dev, metadata.st_ino))
        except CodexWorkerError:
            raise
        except Exception:
            raise CodexWorkerError from None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        self._connections.add(writer)
        response: object
        try:
            value = await _read_frame(reader)
            request = _parse_worker_request(value)
            result_type = result_type_for(request.request)
            async with self._active:
                result = await asyncio.wait_for(
                    self._run(
                        request.request,
                        result_type.model_json_schema(),
                        request.model,
                        request.effort,
                    ),
                    125.0,
                )
            validated = result_type.model_validate(result.model_dump(mode="python"))
            _scan_secrets(validated.model_dump(mode="python"))
            response = {"ok": True, "result": validated.model_dump(mode="json")}
        except asyncio.CancelledError:
            raise
        except CodexAuthError:
            response = WorkerFailure(ok=False, error_code="auth_failed").model_dump()
        except CodexProtocolError:
            response = WorkerFailure(ok=False, error_code="protocol_failed").model_dump()
        except CodexWorkerError:
            response = WorkerFailure(ok=False, error_code="invalid_request").model_dump()
        except Exception:
            response = WorkerFailure(ok=False, error_code="worker_failed").model_dump()
        try:
            writer.write(_encode(response))
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()
            self._connections.discard(writer)
            if task is not None:
                self._tasks.discard(task)

    async def close(self) -> None:
        server = self._server
        if server is not None:
            server.close()
            await server.wait_closed()
            object.__setattr__(self, "_server", None)
        for writer in tuple(self._connections):
            writer.close()
        for writer in tuple(self._connections):
            with suppress(Exception):
                await writer.wait_closed()
        current = asyncio.current_task()
        tasks = tuple(task for task in self._tasks if task is not current)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        identity = self._identity
        try:
            metadata = self._socket_path.lstat()
            if (
                identity is not None
                and stat.S_ISSOCK(metadata.st_mode)
                and (metadata.st_dev, metadata.st_ino) == identity
            ):
                self._socket_path.unlink()
        except FileNotFoundError:
            pass


class CodexWorkerClient:
    __slots__ = ("_socket_path",)

    def __init__(self, socket_path: Path) -> None:
        parent = _safe_socket_parent(Path(socket_path))
        object.__setattr__(self, "_socket_path", parent / Path(socket_path).name)

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("CodexWorkerClient composition is sealed")

    async def run(
        self,
        request: RequestModel,
        *,
        model: str = "gpt-5.6-terra",
        effort: str = "medium",
    ):
        try:
            metadata = self._socket_path.lstat()
            if (
                not stat.S_ISSOCK(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise CodexWorkerError
            socket_identity = (metadata.st_dev, metadata.st_ino)
            envelope = WorkerRequest(
                kind=request_kind(request),
                request=request,
                model=model,
                effort=effort,
            )
        except (ValidationError, OSError, TypeError):
            raise CodexWorkerError from None

        async def exchange():
            reader: asyncio.StreamReader
            writer: asyncio.StreamWriter
            reader, writer = await asyncio.open_unix_connection(self._socket_path)
            try:
                connected_metadata = self._socket_path.lstat()
                if (
                    not stat.S_ISSOCK(connected_metadata.st_mode)
                    or (connected_metadata.st_dev, connected_metadata.st_ino) != socket_identity
                ):
                    raise CodexWorkerError
                writer.write(_encode(envelope.model_dump(mode="json")))
                await writer.drain()
                value = await _read_frame(reader)
            finally:
                writer.close()
                with suppress(Exception):
                    await writer.wait_closed()
            if type(value) is not dict or type(value.get("ok")) is not bool:
                raise CodexWorkerError
            if value["ok"] is not True or set(value) != {"ok", "result"}:
                raise CodexWorkerError
            result_type = result_type_for(request)
            try:
                return result_type.model_validate(value["result"])
            except ValidationError:
                raise CodexWorkerError from None

        try:
            return await asyncio.wait_for(exchange(), _CLIENT_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except CodexWorkerError:
            raise
        except Exception:
            raise CodexWorkerError from None
