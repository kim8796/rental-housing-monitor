from __future__ import annotations

import inspect
import json
import os
import shutil
import stat
import tempfile
import threading
import unicodedata
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path, PurePosixPath

from personal_monitor.security.egress import EgressProxyPolicy
from personal_monitor.security.url_policy import ResolvedTarget
from personal_monitor.security.vault import CredentialVault, validate_logical_key

_ARCHIVE_MAGIC = b"PMPA1"
_MANIFEST_LENGTH_BYTES = 4
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_ENTRIES = 2048
MAX_TOTAL_UNPACKED_BYTES = 24 * 1024 * 1024
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_PATH_BYTES = 512
MAX_PATH_DEPTH = 32
_ERROR = "browser profile is unavailable"

_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[tuple[tuple[int, int], str], threading.RLock] = {}

BootstrapRunner = Callable[..., object]
PageAction = Callable[[object], object]


class ProfileUnavailableError(RuntimeError):
    """Fixed, redacted profile lifecycle failure."""

    def __init__(self) -> None:
        super().__init__(_ERROR)


class BrowserProfileStore:
    """Encrypted browser archives materialized only in isolated workspaces."""

    __slots__ = ("_expected_uid", "_root_identity", "_vault", "_workspace_root")

    def __init__(
        self,
        vault: CredentialVault,
        *,
        materialization_root: Path,
        require_memory_backed: bool = False,
        expected_uid: int | None = None,
    ) -> None:
        if not isinstance(vault, CredentialVault):
            raise TypeError("vault must be a CredentialVault")
        self._vault = vault
        self._expected_uid = os.geteuid() if expected_uid is None else expected_uid
        self._workspace_root = Path(materialization_root)
        try:
            with suppress(FileExistsError):
                os.mkdir(self._workspace_root, 0o700)
            metadata = os.stat(self._workspace_root, follow_symlinks=False)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != self._expected_uid
                or stat.S_IMODE(metadata.st_mode) != 0o700
                or require_memory_backed
                and not _is_memory_backed(self._workspace_root)
            ):
                raise ProfileUnavailableError
            self._root_identity = (metadata.st_dev, metadata.st_ino)
        except ProfileUnavailableError:
            raise
        except Exception:
            raise ProfileUnavailableError from None

    def __repr__(self) -> str:
        return "<BrowserProfileStore>"

    def path_for(self, profile_id: str) -> Path:
        profile = validate_logical_key(profile_id)
        return self._workspace_root / profile

    def archive(self, profile_id: str, source: Path) -> None:
        profile = validate_logical_key(profile_id)
        try:
            with self._lock_for(profile):
                archive = _encode_profile_archive(Path(source), expected_uid=self._expected_uid)
                self._vault.put(profile, archive)
        except ProfileUnavailableError:
            raise
        except Exception:
            raise ProfileUnavailableError from None

    def materialize(self, profile_id: str) -> _MaterializedProfile:
        profile = validate_logical_key(profile_id)
        return _MaterializedProfile(self, profile, self._lock_for(profile))

    def _lock_for(self, profile_id: str) -> threading.RLock:
        identity = (self._vault._lock_identity, profile_id)
        with _LOCKS_GUARD:
            return _LOCKS.setdefault(identity, threading.RLock())

    def _new_workspace(self) -> Path:
        self._require_root()
        try:
            workspace = Path(tempfile.mkdtemp(prefix=".profile-", dir=self._workspace_root))
            workspace.chmod(0o700)
            metadata = workspace.stat(follow_symlinks=False)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != self._expected_uid
                or stat.S_IMODE(metadata.st_mode) != 0o700
            ):
                raise ProfileUnavailableError
            return workspace
        except ProfileUnavailableError:
            raise
        except Exception:
            raise ProfileUnavailableError from None

    def _require_root(self) -> None:
        try:
            metadata = os.stat(self._workspace_root, follow_symlinks=False)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != self._expected_uid
                or stat.S_IMODE(metadata.st_mode) != 0o700
                or (metadata.st_dev, metadata.st_ino) != self._root_identity
            ):
                raise ProfileUnavailableError
        except ProfileUnavailableError:
            raise
        except Exception:
            raise ProfileUnavailableError from None


class _MaterializedProfile:
    __slots__ = ("_lock", "_profile", "_store", "_workspace")

    def __init__(
        self,
        store: BrowserProfileStore,
        profile: str,
        lock: threading.RLock,
    ) -> None:
        self._store = store
        self._profile = profile
        self._lock = lock
        self._workspace: Path | None = None

    def __enter__(self) -> Path:
        self._lock.acquire()
        workspace: Path | None = None
        try:
            workspace = self._store._new_workspace()
            archive = self._store._vault.get(self._profile)
            _extract_profile_archive(archive, workspace)
            self._workspace = workspace
            return workspace
        except BaseException as error:
            cleanup_error: BaseException | None = None
            if workspace is not None:
                try:
                    _remove_workspace(workspace)
                except BaseException as cleanup:
                    cleanup_error = cleanup
            self._lock.release()
            if not isinstance(error, Exception):
                if cleanup_error is not None:
                    error.add_note("browser profile cleanup failed")
                raise
            if cleanup_error is not None and not isinstance(cleanup_error, Exception):
                raise cleanup_error from error
            failure = ProfileUnavailableError()
            if cleanup_error is not None:
                failure.add_note("browser profile cleanup failed")
            raise failure from None

    def __exit__(self, exc_type, exc, traceback) -> bool:
        workspace = self._workspace
        persistence_error: BaseException | None = None
        cleanup_error: BaseException | None = None
        try:
            if workspace is None:
                persistence_error = ProfileUnavailableError()
            else:
                try:
                    archive = _encode_profile_archive(
                        workspace,
                        expected_uid=self._store._expected_uid,
                    )
                    self._store._vault.put(self._profile, archive)
                except BaseException as error:
                    persistence_error = error
                try:
                    _remove_workspace(workspace)
                except BaseException as error:
                    cleanup_error = error
        finally:
            self._workspace = None
            self._lock.release()

        if exc is not None:
            if not isinstance(exc, Exception):
                if persistence_error is not None:
                    exc.add_note("browser profile persistence failed")
                if cleanup_error is not None:
                    exc.add_note("browser profile cleanup failed")
                return False
            if persistence_error is not None:
                exc.add_note("browser profile persistence failed")
            if cleanup_error is not None:
                exc.add_note("browser profile cleanup failed")
            if cleanup_error is not None and not isinstance(cleanup_error, Exception):
                raise cleanup_error
            if persistence_error is not None and not isinstance(persistence_error, Exception):
                raise persistence_error
            return False
        if persistence_error is not None and not isinstance(persistence_error, Exception):
            if cleanup_error is not None:
                persistence_error.add_note("browser profile cleanup failed")
            raise persistence_error
        if cleanup_error is not None:
            if not isinstance(cleanup_error, Exception):
                raise cleanup_error
            failure = ProfileUnavailableError()
            failure.add_note("browser profile cleanup failed")
            if persistence_error is not None:
                failure.add_note("browser profile persistence failed")
            raise failure from None
        if persistence_error is not None:
            raise ProfileUnavailableError from None
        return False


def bootstrap_profile(
    store: BrowserProfileStore,
    profile_id: str,
    target: ResolvedTarget,
    *,
    runner: BootstrapRunner,
    egress_proxy_url: str,
    page_action: PageAction,
) -> None:
    """Run one real headful Scrapling session and archive it only on success."""

    if not isinstance(store, BrowserProfileStore) or not isinstance(target, ResolvedTarget):
        raise ProfileUnavailableError
    profile = validate_logical_key(profile_id)
    if not callable(runner) or not callable(page_action):
        raise ProfileUnavailableError
    try:
        proxy = EgressProxyPolicy.from_url(egress_proxy_url).url
    except Exception:
        raise ProfileUnavailableError from None

    lock = store._lock_for(profile)
    with lock:
        workspace: Path | None = None
        active: BaseException | None = None
        action_completed = False

        def verified_page_action(page: object) -> object:
            nonlocal action_completed
            result = page_action(page)
            if inspect.isawaitable(result):
                raise TypeError("bootstrap page action must be synchronous")
            action_completed = True
            return result

        try:
            workspace = store._new_workspace()
            runner(
                target.normalized_url,
                headless=False,
                user_data_dir=str(workspace),
                timeout=900_000,
                page_action=verified_page_action,
                proxy=proxy,
                dns_over_https=False,
                google_search=False,
                retries=1,
            )
            if not action_completed:
                raise ProfileUnavailableError
            archive = _encode_profile_archive(workspace, expected_uid=store._expected_uid)
            store._vault.put(profile, archive)
        except BaseException as error:
            active = error
        cleanup_error: BaseException | None = None
        if workspace is not None:
            try:
                _remove_workspace(workspace)
            except BaseException as error:
                cleanup_error = error
        if active is not None:
            if not isinstance(active, Exception):
                if cleanup_error is not None:
                    active.add_note("browser profile cleanup failed")
                raise active
            if cleanup_error is not None and not isinstance(cleanup_error, Exception):
                raise cleanup_error
            raise ProfileUnavailableError from None
        if cleanup_error is not None:
            if not isinstance(cleanup_error, Exception):
                raise cleanup_error
            raise ProfileUnavailableError from None


def _encode_profile_archive(root: Path, *, expected_uid: int | None = None) -> bytes:
    uid = os.geteuid() if expected_uid is None else expected_uid
    root_fd = -1
    try:
        root_fd = os.open(
            root,
            os.O_RDONLY
            | os.O_CLOEXEC
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_DIRECTORY", 0),
        )
        root_before = os.fstat(root_fd)
        if (
            not stat.S_ISDIR(root_before.st_mode)
            or root_before.st_uid != uid
            or stat.S_IMODE(root_before.st_mode) != 0o700
        ):
            raise ProfileUnavailableError
        entries: list[tuple[str, str, bytes]] = []
        seen_inodes: set[tuple[int, int]] = set()
        _scan_directory(root_fd, PurePosixPath(), entries, seen_inodes, uid)
        root_after = os.fstat(root_fd)
        if not _same_directory_snapshot(root_before, root_after):
            raise ProfileUnavailableError
        entries.sort(key=lambda item: item[0].encode("utf-8"))
        return _build_archive(entries)
    except ProfileUnavailableError:
        raise
    except Exception:
        raise ProfileUnavailableError from None
    finally:
        if root_fd >= 0:
            os.close(root_fd)


def _scan_directory(
    directory_fd: int,
    relative: PurePosixPath,
    entries: list[tuple[str, str, bytes]],
    seen_inodes: set[tuple[int, int]],
    expected_uid: int,
) -> None:
    for name in sorted(os.listdir(directory_fd), key=lambda value: value.encode("utf-8")):
        path = relative / name
        normalized = _validate_archive_path(path.as_posix())
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            if metadata.st_uid != expected_uid:
                raise ProfileUnavailableError
            child_fd = os.open(
                name,
                os.O_RDONLY
                | os.O_CLOEXEC
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_DIRECTORY", 0),
                dir_fd=directory_fd,
            )
            try:
                opened = os.fstat(child_fd)
                if (
                    not stat.S_ISDIR(opened.st_mode)
                    or opened.st_uid != expected_uid
                    or (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
                ):
                    raise ProfileUnavailableError
                entries.append((normalized, "d", b""))
                _require_entry_capacity(entries)
                _scan_directory(child_fd, path, entries, seen_inodes, expected_uid)
                final = os.fstat(child_fd)
                named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if not _same_directory_snapshot(opened, final) or (
                    named.st_dev,
                    named.st_ino,
                ) != (opened.st_dev, opened.st_ino):
                    raise ProfileUnavailableError
            finally:
                os.close(child_fd)
            continue
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != expected_uid
            or metadata.st_nlink != 1
        ):
            raise ProfileUnavailableError
        identity = (metadata.st_dev, metadata.st_ino)
        if identity in seen_inodes or metadata.st_size > MAX_FILE_BYTES:
            raise ProfileUnavailableError
        seen_inodes.add(identity)
        fd = os.open(
            name,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_fd,
        )
        try:
            opened = os.fstat(fd)
            if (
                (opened.st_dev, opened.st_ino) != identity
                or not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != expected_uid
                or opened.st_nlink != 1
                or opened.st_size > MAX_FILE_BYTES
            ):
                raise ProfileUnavailableError
            content = _read_limited(fd, MAX_FILE_BYTES + 1)
            final = os.fstat(fd)
            if len(content) != opened.st_size or (
                opened.st_size,
                opened.st_mtime_ns,
                opened.st_ctime_ns,
            ) != (final.st_size, final.st_mtime_ns, final.st_ctime_ns):
                raise ProfileUnavailableError
        finally:
            os.close(fd)
        named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (named.st_dev, named.st_ino) != identity or not stat.S_ISREG(named.st_mode):
            raise ProfileUnavailableError
        entries.append((normalized, "f", content))
        _require_entry_capacity(entries)


def _build_archive(entries: list[tuple[str, str, bytes]]) -> bytes:
    if len(entries) > MAX_ENTRIES:
        raise ProfileUnavailableError
    total = sum(len(content) for _, kind, content in entries if kind == "f")
    if total > MAX_TOTAL_UNPACKED_BYTES:
        raise ProfileUnavailableError
    manifest = [
        {"kind": kind, "path": path, "size": len(content)} for path, kind, content in entries
    ]
    manifest_bytes = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(manifest_bytes) > MAX_MANIFEST_BYTES:
        raise ProfileUnavailableError
    archive = (
        _ARCHIVE_MAGIC
        + len(manifest_bytes).to_bytes(_MANIFEST_LENGTH_BYTES, "big")
        + manifest_bytes
        + b"".join(content for _, kind, content in entries if kind == "f")
    )
    if len(archive) > MAX_ARCHIVE_BYTES:
        raise ProfileUnavailableError
    return archive


def _decode_profile_archive(archive: bytes) -> list[tuple[str, str, bytes]]:
    if not isinstance(archive, bytes) or len(archive) > MAX_ARCHIVE_BYTES:
        raise ProfileUnavailableError
    prefix = len(_ARCHIVE_MAGIC) + _MANIFEST_LENGTH_BYTES
    if len(archive) < prefix or not archive.startswith(_ARCHIVE_MAGIC):
        raise ProfileUnavailableError
    manifest_size = int.from_bytes(archive[len(_ARCHIVE_MAGIC) : prefix], "big")
    if manifest_size > MAX_MANIFEST_BYTES or prefix + manifest_size > len(archive):
        raise ProfileUnavailableError
    manifest_bytes = archive[prefix : prefix + manifest_size]
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise ProfileUnavailableError from None
    canonical = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if canonical != manifest_bytes or not isinstance(manifest, list) or len(manifest) > MAX_ENTRIES:
        raise ProfileUnavailableError

    payload = memoryview(archive)[prefix + manifest_size :]
    offset = 0
    total = 0
    entries: list[tuple[str, str, bytes]] = []
    seen: set[str] = set()
    types: dict[str, str] = {}
    previous: bytes | None = None
    for raw in manifest:
        if not isinstance(raw, dict) or set(raw) != {"kind", "path", "size"}:
            raise ProfileUnavailableError
        kind, raw_path, size = raw["kind"], raw["path"], raw["size"]
        if kind not in {"d", "f"} or isinstance(size, bool) or not isinstance(size, int):
            raise ProfileUnavailableError
        path = _validate_archive_path(raw_path)
        order_key = path.encode("utf-8")
        collision = unicodedata.normalize("NFC", path).casefold()
        if collision in seen or previous is not None and order_key <= previous:
            raise ProfileUnavailableError
        seen.add(collision)
        previous = order_key
        parent = PurePosixPath(path).parent
        if parent != PurePosixPath(".") and types.get(parent.as_posix()) != "d":
            raise ProfileUnavailableError
        if kind == "d":
            if size != 0:
                raise ProfileUnavailableError
            content = b""
        else:
            if size < 0 or size > MAX_FILE_BYTES or offset + size > len(payload):
                raise ProfileUnavailableError
            content = bytes(payload[offset : offset + size])
            offset += size
            total += size
            if total > MAX_TOTAL_UNPACKED_BYTES:
                raise ProfileUnavailableError
        types[path] = kind
        entries.append((path, kind, content))
    if offset != len(payload) or _build_archive(entries) != archive:
        raise ProfileUnavailableError
    return entries


def _extract_profile_archive(archive: bytes, workspace: Path) -> None:
    entries = _decode_profile_archive(archive)
    try:
        metadata = os.stat(workspace, follow_symlinks=False)
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise ProfileUnavailableError
        for path, kind, content in entries:
            destination = workspace.joinpath(*PurePosixPath(path).parts)
            if kind == "d":
                os.mkdir(destination, 0o700)
                continue
            fd = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                _write_all(fd, content)
                os.fsync(fd)
            finally:
                os.close(fd)
    except ProfileUnavailableError:
        raise
    except Exception:
        raise ProfileUnavailableError from None


def _validate_archive_path(value: object) -> str:
    if not isinstance(value, str) or not value or value != unicodedata.normalize("NFC", value):
        raise ProfileUnavailableError
    if (
        value.startswith("/")
        or "\\" in value
        or len(value.encode("utf-8")) > MAX_PATH_BYTES
        or any(unicodedata.category(character).startswith("C") for character in value)
    ):
        raise ProfileUnavailableError
    path = PurePosixPath(value)
    parts = path.parts
    if (
        not parts
        or len(parts) > MAX_PATH_DEPTH
        or any(
            part in {"", ".", ".."} or len(part.encode("utf-8")) > 255 or part.endswith((" ", "."))
            for part in parts
        )
        or path.as_posix() != value
    ):
        raise ProfileUnavailableError
    return value


def _require_entry_capacity(entries: list[tuple[str, str, bytes]]) -> None:
    if len(entries) > MAX_ENTRIES:
        raise ProfileUnavailableError
    if sum(len(content) for _, kind, content in entries if kind == "f") > MAX_TOTAL_UNPACKED_BYTES:
        raise ProfileUnavailableError


def _read_limited(fd: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    remaining = limit
    while remaining:
        chunk = os.read(fd, min(remaining, 1024 * 1024))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _write_all(fd: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _same_directory_snapshot(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        first.st_dev,
        first.st_ino,
        first.st_mode,
        first.st_uid,
        first.st_mtime_ns,
        first.st_ctime_ns,
    ) == (
        second.st_dev,
        second.st_ino,
        second.st_mode,
        second.st_uid,
        second.st_mtime_ns,
        second.st_ctime_ns,
    )


def _remove_workspace(path: Path) -> None:
    shutil.rmtree(path)


def _is_memory_backed(path: Path) -> bool:
    resolved = path.resolve()
    try:
        mounts = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
    except OSError:
        return False
    best_mount = Path("/")
    best_type = ""
    for line in mounts.splitlines():
        fields = line.split()
        try:
            separator = fields.index("-")
            mount = Path(fields[4].replace("\\040", " "))
            filesystem = fields[separator + 1]
        except (ValueError, IndexError):
            continue
        if (mount == resolved or mount in resolved.parents) and len(mount.parts) >= len(
            best_mount.parts
        ):
            best_mount = mount
            best_type = filesystem
    return best_type in {"tmpfs", "ramfs"}
