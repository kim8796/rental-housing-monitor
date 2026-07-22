from __future__ import annotations

import os
import re
import secrets
import stat
import threading
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

    __slots__ = ("_cipher", "_dir_fd", "_expected_uid", "_identity", "_lock", "_root")

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
        except VaultError:
            self.close()
            raise
        except Exception:
            self.close()
            raise VaultError from None

    def __repr__(self) -> str:
        return "<CredentialVault>"

    @property
    def _lock_identity(self) -> tuple[int, int]:
        return self._identity

    def close(self) -> None:
        fd = getattr(self, "_dir_fd", -1)
        if fd >= 0:
            os.close(fd)
            self._dir_fd = -1

    def __del__(self) -> None:
        with suppress(Exception):
            self.close()

    def put(self, logical_key: str, value: bytes | bytearray | memoryview) -> None:
        key = validate_logical_key(logical_key)
        try:
            blob = self._cipher.encrypt(value, key.encode("ascii"))
            record = _MAGIC + blob.nonce + blob.ciphertext
            if len(record) > MAX_VAULT_RECORD_BYTES:
                raise VaultError
            with self._lock:
                self._write_record(_record_name(key), record)
        except VaultError:
            raise
        except Exception:
            raise VaultError from None

    def get(self, logical_key: str) -> bytes:
        key = validate_logical_key(logical_key)
        try:
            with self._lock:
                record = self._read_record(_record_name(key))
            if len(record) < len(_MAGIC) + _NONCE_BYTES + _TAG_BYTES or not record.startswith(
                _MAGIC
            ):
                raise VaultError
            blob = EncryptedBlob(
                nonce=record[len(_MAGIC) : len(_MAGIC) + _NONCE_BYTES],
                ciphertext=record[len(_MAGIC) + _NONCE_BYTES :],
            )
            return self._cipher.decrypt(blob, key.encode("ascii"))
        except VaultError:
            raise
        except Exception:
            raise VaultError from None

    def delete(self, logical_key: str) -> None:
        key = validate_logical_key(logical_key)
        name = _record_name(key)
        try:
            with self._lock:
                metadata = os.stat(name, dir_fd=self._dir_fd, follow_symlinks=False)
                self._validate_record_metadata(metadata)
                os.unlink(name, dir_fd=self._dir_fd)
                os.fsync(self._dir_fd)
        except VaultError:
            raise
        except Exception:
            raise VaultError from None

    def _read_record(self, name: str) -> bytes:
        fd = -1
        try:
            fd = os.open(name, _OPEN_FILE_FLAGS, dir_fd=self._dir_fd)
            before = os.fstat(fd)
            self._validate_record_metadata(before)
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

    def _write_record(self, name: str, record: bytes) -> None:
        token = secrets.token_hex(16)
        temporary = f".{token}.tmp"
        backup = f".{token}.bak"
        fd = -1
        backup_created = False
        replaced = False
        try:
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=self._dir_fd,
            )
            _write_all(fd, record)
            os.fsync(fd)
            written = os.fstat(fd)
            self._validate_record_metadata(written)
            os.close(fd)
            fd = -1

            try:
                current = os.stat(name, dir_fd=self._dir_fd, follow_symlinks=False)
            except FileNotFoundError:
                current = None
            if current is not None:
                self._validate_record_metadata(current)
                os.link(
                    name,
                    backup,
                    src_dir_fd=self._dir_fd,
                    dst_dir_fd=self._dir_fd,
                    follow_symlinks=False,
                )
                backup_created = True

            os.replace(temporary, name, src_dir_fd=self._dir_fd, dst_dir_fd=self._dir_fd)
            replaced = True
            os.fsync(self._dir_fd)
            if backup_created:
                os.unlink(backup, dir_fd=self._dir_fd)
                backup_created = False
                os.fsync(self._dir_fd)
        except BaseException:
            if fd >= 0:
                os.close(fd)
                fd = -1
            if replaced:
                try:
                    if backup_created:
                        os.replace(
                            backup,
                            name,
                            src_dir_fd=self._dir_fd,
                            dst_dir_fd=self._dir_fd,
                        )
                        backup_created = False
                    else:
                        os.unlink(name, dir_fd=self._dir_fd)
                    os.fsync(self._dir_fd)
                except Exception:
                    pass
            raise
        finally:
            if fd >= 0:
                os.close(fd)
            _unlink_if_present(self._dir_fd, temporary)
            if backup_created:
                _unlink_if_present(self._dir_fd, backup)

    def _validate_record_metadata(self, metadata: os.stat_result) -> None:
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != self._expected_uid
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise VaultError


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


def _unlink_if_present(directory_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=directory_fd)
    except FileNotFoundError:
        pass
    except Exception:
        pass
