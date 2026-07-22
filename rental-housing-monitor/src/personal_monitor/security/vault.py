from __future__ import annotations

import os
import re
import secrets
import stat
import threading
import weakref
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from personal_monitor.security.encryption import AesGcmCipher, EncryptedBlob

_MAGIC = b"PMV1"
_NONCE_BYTES = 12
_TAG_BYTES = 16
_KEY_BYTES = 32
_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z", re.ASCII)
MAX_VAULT_RECORD_BYTES = 64 * 1024 * 1024
_ERROR = "credential vault operation failed"
_OPEN_FILE_FLAGS = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
_OPEN_DIRECTORY_FLAGS = _OPEN_FILE_FLAGS | getattr(os, "O_DIRECTORY", 0)

_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[tuple[int, int], threading.RLock] = {}


def _private_vault_pins():
    pins: weakref.WeakKeyDictionary[object, tuple[object, ...]] = weakref.WeakKeyDictionary()
    lock = threading.RLock()

    def pin(owner: object, values: tuple[object, ...]) -> None:
        with lock:
            if owner in pins:
                raise VaultError
            pins[owner] = values

    def acquire(owner: object) -> tuple[object, ...]:
        with lock:
            values = pins.get(owner)
        if values is None:
            raise VaultError
        return values

    def release(owner: object) -> tuple[object, ...] | None:
        with lock:
            return pins.pop(owner, None)

    return pin, acquire, release


_pin_vault, _acquire_vault, _release_vault = _private_vault_pins()


class VaultError(RuntimeError):
    """Fixed, redacted failure at the encrypted-record boundary."""

    def __init__(self) -> None:
        super().__init__(_ERROR)


def validate_logical_key(value: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ValueError("logical key is invalid")
    return value


def load_master_key(path: Path, *, expected_uid: int | None = None) -> bytes:
    """Read an exact service-owned key through one no-follow file descriptor."""

    uid = os.geteuid() if expected_uid is None else expected_uid
    fd = -1
    try:
        fd = os.open(path, _OPEN_FILE_FLAGS)
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != uid
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size != _KEY_BYTES
        ):
            raise VaultError
        key = _read_exact(fd, _KEY_BYTES + 1)
        final = os.fstat(fd)
        if len(key) != _KEY_BYTES or not _same_file_snapshot(metadata, final):
            raise VaultError
        return key
    except VaultError:
        raise
    except Exception:
        raise VaultError from None
    finally:
        if fd >= 0:
            os.close(fd)


class CredentialVault:
    """A descriptor-pinned directory of authenticated, atomically replaced records."""

    __slots__ = (
        "_cipher",
        "_composition",
        "_dir_fd",
        "_expected_uid",
        "_identity",
        "_lock",
        "_root",
        "_sealed",
        "__weakref__",
    )

    def __init__(
        self,
        root: Path,
        *,
        key: bytes | None = None,
        key_path: Path | None = None,
        expected_uid: int | None = None,
    ) -> None:
        if (key is None) == (key_path is None):
            raise ValueError("provide exactly one master key source")
        uid = os.geteuid() if expected_uid is None else expected_uid
        cipher_key = load_master_key(key_path, expected_uid=uid) if key_path is not None else key
        self._cipher = AesGcmCipher(cipher_key)  # type: ignore[arg-type]
        self._expected_uid = uid
        self._root = Path(root)
        self._dir_fd = -1
        try:
            with suppress(FileExistsError):
                os.mkdir(self._root, 0o700)
            self._dir_fd = os.open(self._root, _OPEN_DIRECTORY_FLAGS)
            metadata = os.fstat(self._dir_fd)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != uid
                or stat.S_IMODE(metadata.st_mode) != 0o700
            ):
                raise VaultError
            identity = (metadata.st_dev, metadata.st_ino)
            self._identity = identity
            with _LOCKS_GUARD:
                self._lock = _LOCKS.setdefault(identity, threading.RLock())
            self._composition = (
                self._cipher,
                self._dir_fd,
                self._expected_uid,
                self._identity,
                self._lock,
            )
            self._sealed = True
            _pin_vault(self, self._composition)
        except VaultError:
            self.close()
            raise
        except Exception:
            self.close()
            raise VaultError from None

    def __repr__(self) -> str:
        return "<CredentialVault>"

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("credential vault is sealed")
        object.__setattr__(self, name, value)

    @property
    def _lock_identity(self) -> tuple[int, int]:
        _cipher, _dir_fd, _uid, identity, _lock = self._trusted_snapshot()
        return identity

    def close(self) -> None:
        pinned = _release_vault(self)
        fd = pinned[1] if pinned is not None else getattr(self, "_dir_fd", -1)
        if fd >= 0:
            os.close(fd)  # type: ignore[arg-type]
        object.__setattr__(self, "_dir_fd", -1)

    def __del__(self) -> None:
        with suppress(Exception):
            self.close()

    def put(self, logical_key: str, value: bytes | bytearray | memoryview) -> None:
        key = validate_logical_key(logical_key)
        try:
            cipher, directory_fd, uid, _identity, lock = self._trusted_snapshot()
            blob = cipher.encrypt(value, key.encode("ascii"))
            record = _MAGIC + blob.nonce + blob.ciphertext
            if len(record) > MAX_VAULT_RECORD_BYTES:
                raise VaultError
            with lock:
                self._write_record(_record_name(key), record, directory_fd, uid)
        except VaultError:
            raise
        except Exception as error:
            failure = VaultError()
            if "credential vault cleanup required" in getattr(error, "__notes__", ()):
                failure.add_note("credential vault cleanup required")
            raise failure from None

    def get(self, logical_key: str) -> bytes:
        key = validate_logical_key(logical_key)
        try:
            cipher, directory_fd, uid, _identity, lock = self._trusted_snapshot()
            with lock:
                record = self._read_record(_record_name(key), directory_fd, uid)
            if len(record) < len(_MAGIC) + _NONCE_BYTES + _TAG_BYTES or not record.startswith(
                _MAGIC
            ):
                raise VaultError
            blob = EncryptedBlob(
                nonce=record[len(_MAGIC) : len(_MAGIC) + _NONCE_BYTES],
                ciphertext=record[len(_MAGIC) + _NONCE_BYTES :],
            )
            return cipher.decrypt(blob, key.encode("ascii"))
        except VaultError:
            raise
        except Exception:
            raise VaultError from None

    def delete(self, logical_key: str) -> None:
        key = validate_logical_key(logical_key)
        name = _record_name(key)
        try:
            _cipher, directory_fd, uid, _identity, lock = self._trusted_snapshot()
            with lock:
                metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                self._validate_record_metadata(metadata, uid)
                os.unlink(name, dir_fd=directory_fd)
                os.fsync(directory_fd)
        except VaultError:
            raise
        except Exception:
            raise VaultError from None

    def _read_record(self, name: str, directory_fd: int, expected_uid: int) -> bytes:
        fd = -1
        try:
            fd = os.open(name, _OPEN_FILE_FLAGS, dir_fd=directory_fd)
            before = os.fstat(fd)
            self._validate_record_metadata(before, expected_uid)
            if before.st_size > MAX_VAULT_RECORD_BYTES:
                raise VaultError
            value = _read_exact(fd, MAX_VAULT_RECORD_BYTES + 1)
            after = os.fstat(fd)
            if len(value) != before.st_size or not _same_file_snapshot(before, after):
                raise VaultError
            return value
        finally:
            if fd >= 0:
                os.close(fd)

    def _write_record(
        self,
        name: str,
        record: bytes,
        directory_fd: int,
        expected_uid: int,
    ) -> None:
        token = secrets.token_hex(16)
        temporary = f".{token}.tmp"
        backup = f".{token}.bak"
        fd = -1
        backup_created = False
        replaced = False
        committed = False
        active: BaseException | None = None
        cleanup_error: BaseException | None = None
        try:
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=directory_fd,
            )
            _write_all(fd, record)
            os.fsync(fd)
            written = os.fstat(fd)
            self._validate_record_metadata(written, expected_uid)
            os.close(fd)
            fd = -1

            try:
                current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                current = None
            if current is not None:
                self._validate_record_metadata(current, expected_uid)
                os.link(
                    name,
                    backup,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                backup_created = True

            os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            replaced = True
            os.fsync(directory_fd)
            committed = True
            if backup_created:
                os.unlink(backup, dir_fd=directory_fd)
                backup_created = False
                os.fsync(directory_fd)
        except BaseException as error:
            active = error
            if fd >= 0:
                try:
                    os.close(fd)
                except BaseException as cleanup:
                    cleanup_error = cleanup
                fd = -1
            if replaced and not committed:
                try:
                    if backup_created:
                        os.replace(
                            backup,
                            name,
                            src_dir_fd=directory_fd,
                            dst_dir_fd=directory_fd,
                        )
                        backup_created = False
                    else:
                        os.unlink(name, dir_fd=directory_fd)
                    os.fsync(directory_fd)
                except BaseException as cleanup:
                    cleanup_error = cleanup
        finally:
            if fd >= 0:
                try:
                    os.close(fd)
                except BaseException as cleanup:
                    cleanup_error = cleanup_error or cleanup
            temporary_error = _unlink_error(directory_fd, temporary)
            cleanup_error = cleanup_error or temporary_error
            if backup_created and not (replaced and not committed and cleanup_error is not None):
                backup_error = _unlink_error(directory_fd, backup)
                cleanup_error = cleanup_error or backup_error

        if active is not None:
            if cleanup_error is not None:
                if isinstance(active, Exception) and not isinstance(cleanup_error, Exception):
                    raise cleanup_error from active
                active.add_note("credential vault cleanup required")
            raise active
        if cleanup_error is not None:
            if not isinstance(cleanup_error, Exception):
                raise cleanup_error
            failure = VaultError()
            failure.add_note("credential vault cleanup required")
            raise failure

    @staticmethod
    def _validate_record_metadata(metadata: os.stat_result, expected_uid: int) -> None:
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != expected_uid
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise VaultError

    def _trusted_snapshot(
        self,
        _acquire: Callable[[object], tuple[object, ...]] = _acquire_vault,
    ) -> tuple[AesGcmCipher, int, int, tuple[int, int], threading.RLock]:
        try:
            pinned = _acquire(self)
            cipher, directory_fd, uid, identity, lock = pinned
            metadata = os.fstat(directory_fd)
            valid = (
                type(self) is CredentialVault
                and type(cipher) is AesGcmCipher
                and type(directory_fd) is int
                and type(uid) is int
                and isinstance(identity, tuple)
                and self._cipher is cipher
                and self._dir_fd == directory_fd
                and self._expected_uid == uid
                and self._identity == identity
                and self._lock is lock
                and self._composition is pinned
                and stat.S_ISDIR(metadata.st_mode)
                and metadata.st_uid == uid
                and stat.S_IMODE(metadata.st_mode) == 0o700
                and (metadata.st_dev, metadata.st_ino) == identity
            )
        except Exception:
            valid = False
        if not valid:
            raise VaultError
        return cipher, directory_fd, uid, identity, lock  # type: ignore[return-value]


def _record_name(logical_key: str) -> str:
    return f"{logical_key}.bin"


def _read_exact(fd: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    remaining = limit
    while remaining:
        chunk = os.read(fd, min(remaining, 1024 * 1024))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _write_all(fd: int, value: bytes) -> None:
    view = memoryview(value)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _same_file_snapshot(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        first.st_dev,
        first.st_ino,
        first.st_mode,
        first.st_uid,
        first.st_size,
        first.st_mtime_ns,
        first.st_ctime_ns,
    ) == (
        second.st_dev,
        second.st_ino,
        second.st_mode,
        second.st_uid,
        second.st_size,
        second.st_mtime_ns,
        second.st_ctime_ns,
    )


def _unlink_error(directory_fd: int, name: str) -> BaseException | None:
    try:
        os.unlink(name, dir_fd=directory_fd)
    except FileNotFoundError:
        return None
    except BaseException as error:
        return error
    return None
