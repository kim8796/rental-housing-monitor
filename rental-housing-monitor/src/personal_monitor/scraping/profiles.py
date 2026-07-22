from __future__ import annotations

import asyncio
import inspect
import json
import math
import os
import stat
import tempfile
import threading
import unicodedata
import weakref
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
_BOOTSTRAP_TIMEOUT_SECONDS = 900.0

_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[tuple[tuple[int, int], str], threading.Lock] = {}
_DEFERRED_FINALIZERS: set[asyncio.Task[None]] = set()

BootstrapRunner = Callable[..., object]
PageAction = Callable[[object], object]
ProfileStoreSnapshot = tuple[
    CredentialVault,
    int,
    int,
    tuple[int, int],
    Path,
    tuple[int, int],
]


def _private_profile_store_pins():
    pins: weakref.WeakKeyDictionary[object, tuple[object, ...]] = weakref.WeakKeyDictionary()
    lock = threading.RLock()

    def pin(owner: object, values: tuple[object, ...]) -> None:
        with lock:
            if owner in pins:
                raise ProfileUnavailableError
            pins[owner] = values

    def acquire(owner: object) -> tuple[object, ...]:
        with lock:
            values = pins.get(owner)
        if values is None:
            raise ProfileUnavailableError
        return values

    def release(owner: object) -> tuple[object, ...] | None:
        with lock:
            return pins.pop(owner, None)

    return pin, acquire, release


_pin_profile_store, _acquire_profile_store, _release_profile_store = _private_profile_store_pins()


def _profile_lease_registry():
    leases: dict[tuple[int, int], _PinnedWorkspace] = {}
    lock = threading.RLock()

    def register(workspace: _PinnedWorkspace) -> None:
        with lock:
            if workspace.identity in leases:
                raise ProfileUnavailableError
            leases[workspace.identity] = workspace

    def attach(path: Path, worker: asyncio.Task[object]) -> None:
        fd = -1
        try:
            fd = os.open(
                path,
                os.O_RDONLY
                | os.O_CLOEXEC
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_DIRECTORY", 0),
            )
            metadata = os.fstat(fd)
            identity = (metadata.st_dev, metadata.st_ino)
        except Exception:
            return
        finally:
            if fd >= 0:
                os.close(fd)
        with lock:
            workspace = leases.get(identity)
            if workspace is not None:
                workspace.workers.add(worker)

    def pending(workspace: _PinnedWorkspace) -> tuple[asyncio.Task[object], ...]:
        with lock:
            return tuple(worker for worker in workspace.workers if not worker.done())

    def unregister(workspace: _PinnedWorkspace) -> None:
        with lock:
            if leases.get(workspace.identity) is workspace:
                del leases[workspace.identity]

    return register, attach, pending, unregister


(
    _register_profile_lease,
    attach_profile_worker,
    _pending_profile_workers,
    _unregister_profile_lease,
) = _profile_lease_registry()


async def _acquire_lock_async(lock: threading.Lock) -> None:
    while not lock.acquire(blocking=False):
        await asyncio.sleep(0.01)


class ProfileUnavailableError(RuntimeError):
    """Fixed, redacted profile lifecycle failure."""

    def __init__(self) -> None:
        super().__init__(_ERROR)


class BrowserProfileStore:
    """Encrypted browser archives materialized only in isolated workspaces."""

    __slots__ = (
        "_composition",
        "_expected_uid",
        "_root_fd",
        "_root_identity",
        "_sealed",
        "_vault",
        "_workspace_root",
        "__weakref__",
    )

    def __init__(
        self,
        vault: CredentialVault,
        *,
        materialization_root: Path,
        require_memory_backed: bool = False,
        expected_uid: int | None = None,
    ) -> None:
        if type(vault) is not CredentialVault:
            raise TypeError("vault must be a CredentialVault")
        vault_identity = vault._lock_identity
        self._vault = vault
        self._expected_uid = os.geteuid() if expected_uid is None else expected_uid
        self._workspace_root = Path(materialization_root)
        self._root_fd = -1
        try:
            with suppress(FileExistsError):
                os.mkdir(self._workspace_root, 0o700)
            self._root_fd = os.open(
                self._workspace_root,
                os.O_RDONLY
                | os.O_CLOEXEC
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_DIRECTORY", 0),
            )
            metadata = os.fstat(self._root_fd)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != self._expected_uid
                or stat.S_IMODE(metadata.st_mode) != 0o700
                or require_memory_backed
                and not _is_memory_backed(self._workspace_root)
            ):
                raise ProfileUnavailableError
            self._root_identity = (metadata.st_dev, metadata.st_ino)
            self._composition = (
                self._vault,
                self._expected_uid,
                self._root_fd,
                self._root_identity,
                self._workspace_root,
                vault_identity,
            )
            self._sealed = True
            _pin_profile_store(self, self._composition)
        except ProfileUnavailableError:
            self.close()
            raise
        except Exception:
            self.close()
            raise ProfileUnavailableError from None

    def __repr__(self) -> str:
        return "<BrowserProfileStore>"

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("browser profile store is sealed")
        object.__setattr__(self, name, value)

    def close(self) -> None:
        pinned = _release_profile_store(self)
        fd = pinned[2] if pinned is not None else getattr(self, "_root_fd", -1)
        if fd >= 0:
            os.close(fd)  # type: ignore[arg-type]
        object.__setattr__(self, "_root_fd", -1)

    def __del__(self) -> None:
        with suppress(Exception):
            self.close()

    def path_for(self, profile_id: str) -> Path:
        profile = validate_logical_key(profile_id)
        _vault, _uid, _root_fd, _root_identity, workspace_root, _vault_identity = (
            self._trusted_snapshot()
        )
        return workspace_root / profile

    def archive(self, profile_id: str, source: Path) -> None:
        profile = validate_logical_key(profile_id)
        try:
            snapshot = self._trusted_snapshot()
            vault, uid, _root_fd, _root_identity, _workspace_root, _vault_identity = snapshot
            with self._lock_for(profile, snapshot):
                archive = _encode_profile_archive(Path(source), expected_uid=uid)
                vault.put(profile, archive)
        except ProfileUnavailableError:
            raise
        except Exception:
            raise ProfileUnavailableError from None

    def materialize(self, profile_id: str) -> _MaterializedProfile:
        profile = validate_logical_key(profile_id)
        snapshot = self._trusted_snapshot()
        return _MaterializedProfile(self, profile, self._lock_for(profile, snapshot), snapshot)

    def _lock_for(
        self,
        profile_id: str,
        snapshot: ProfileStoreSnapshot | None = None,
    ) -> threading.Lock:
        trusted = self._trusted_snapshot() if snapshot is None else snapshot
        identity = (trusted[5], profile_id)
        with _LOCKS_GUARD:
            return _LOCKS.setdefault(identity, threading.Lock())

    def _new_workspace(
        self,
        snapshot: ProfileStoreSnapshot,
    ) -> _PinnedWorkspace:
        _vault, expected_uid, root_fd, _root_identity, workspace_root, _vault_identity = snapshot
        self._require_root(snapshot)
        name = f".profile-{next(tempfile._get_candidate_names())}"
        workspace_fd = -1
        created = False
        identity: tuple[int, int] | None = None
        try:
            os.mkdir(name, 0o700, dir_fd=root_fd)
            created = True
            workspace_fd = os.open(
                name,
                os.O_RDONLY
                | os.O_CLOEXEC
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_DIRECTORY", 0),
                dir_fd=root_fd,
            )
            metadata = os.fstat(workspace_fd)
            identity = (metadata.st_dev, metadata.st_ino)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != expected_uid
                or stat.S_IMODE(metadata.st_mode) != 0o700
            ):
                raise ProfileUnavailableError
            workspace = _PinnedWorkspace(
                path=workspace_root / name,
                name=name,
                fd=workspace_fd,
                identity=identity,
                root_fd=root_fd,
                expected_uid=expected_uid,
            )
            _register_profile_lease(workspace)
            return workspace
        except BaseException as error:
            cleanup_error: BaseException | None = None
            if workspace_fd >= 0:
                try:
                    _clear_directory_fd(workspace_fd)
                except BaseException as cleanup:
                    cleanup_error = cleanup
                try:
                    os.close(workspace_fd)
                except BaseException as cleanup:
                    cleanup_error = cleanup_error or cleanup
            if created:
                try:
                    named = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                    if identity is None or (named.st_dev, named.st_ino) == identity:
                        os.rmdir(name, dir_fd=root_fd)
                except FileNotFoundError:
                    pass
                except BaseException as cleanup:
                    cleanup_error = cleanup_error or cleanup
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

    def _require_root(
        self,
        snapshot: ProfileStoreSnapshot,
    ) -> None:
        _vault, expected_uid, root_fd, root_identity, workspace_root, _vault_identity = snapshot
        try:
            metadata = os.stat(workspace_root, follow_symlinks=False)
            pinned = os.fstat(root_fd)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != expected_uid
                or stat.S_IMODE(metadata.st_mode) != 0o700
                or (metadata.st_dev, metadata.st_ino) != root_identity
                or (pinned.st_dev, pinned.st_ino) != root_identity
            ):
                raise ProfileUnavailableError
        except ProfileUnavailableError:
            raise
        except Exception:
            raise ProfileUnavailableError from None

    def _trusted_snapshot(
        self,
        _acquire: Callable[[object], tuple[object, ...]] = _acquire_profile_store,
    ) -> ProfileStoreSnapshot:
        try:
            pinned = _acquire(self)
            vault, uid, root_fd, root_identity, workspace_root, vault_identity = pinned
            vault_snapshot = vault._trusted_snapshot()
            metadata = os.fstat(root_fd)
            valid = (
                type(self) is BrowserProfileStore
                and type(vault) is CredentialVault
                and type(uid) is int
                and type(root_fd) is int
                and isinstance(root_identity, tuple)
                and isinstance(workspace_root, Path)
                and isinstance(vault_identity, tuple)
                and self._vault is vault
                and self._expected_uid == uid
                and self._root_fd == root_fd
                and self._root_identity == root_identity
                and self._workspace_root == workspace_root
                and self._composition is pinned
                and vault_snapshot[3] == vault_identity
                and stat.S_ISDIR(metadata.st_mode)
                and metadata.st_uid == uid
                and stat.S_IMODE(metadata.st_mode) == 0o700
                and (metadata.st_dev, metadata.st_ino) == root_identity
            )
        except Exception:
            valid = False
        if not valid:
            raise ProfileUnavailableError
        return (  # type: ignore[return-value]
            vault,
            uid,
            root_fd,
            root_identity,
            workspace_root,
            vault_identity,
        )


class _PinnedWorkspace:
    __slots__ = ("expected_uid", "fd", "identity", "name", "path", "root_fd", "workers")

    def __init__(
        self,
        *,
        path: Path,
        name: str,
        fd: int,
        identity: tuple[int, int],
        root_fd: int,
        expected_uid: int,
    ) -> None:
        self.path = path
        self.name = name
        self.fd = fd
        self.identity = identity
        self.root_fd = root_fd
        self.expected_uid = expected_uid
        self.workers: set[asyncio.Task[object]] = set()

    def __repr__(self) -> str:
        return "<_PinnedWorkspace>"


class _MaterializedProfile:
    __slots__ = ("_lock", "_profile", "_snapshot", "_store", "_workspace")

    def __init__(
        self,
        store: BrowserProfileStore,
        profile: str,
        lock: threading.Lock,
        snapshot: ProfileStoreSnapshot,
    ) -> None:
        self._store = store
        self._profile = profile
        self._lock = lock
        self._snapshot = snapshot
        self._workspace: _PinnedWorkspace | None = None

    def __enter__(self) -> Path:
        self._lock.acquire()
        return self._enter_after_lock()

    async def __aenter__(self) -> Path:
        await _acquire_lock_async(self._lock)
        return self._enter_after_lock()

    def _enter_after_lock(self) -> Path:
        workspace: _PinnedWorkspace | None = None
        try:
            workspace = self._store._new_workspace(self._snapshot)
            archive = self._snapshot[0].get(self._profile)
            _extract_profile_archive_fd(archive, workspace.fd, workspace.expected_uid)
            self._workspace = workspace
            return workspace.path
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
        return self._exit_after_lock(exc)

    async def __aexit__(self, exc_type, exc, traceback) -> bool:
        workspace = self._workspace
        if workspace is not None:
            pending = _pending_profile_workers(workspace)
            if pending:
                self._workspace = None
                finalizer = asyncio.create_task(
                    _finalize_after_workers(
                        pending,
                        vault=self._snapshot[0],
                        profile=self._profile,
                        workspace=workspace,
                        profile_lock=self._lock,
                    )
                )
                _DEFERRED_FINALIZERS.add(finalizer)
                finalizer.add_done_callback(_finish_deferred_finalizer)
                return False
        return self._exit_after_lock(exc)

    def _exit_after_lock(self, exc: BaseException | None) -> bool:
        workspace = self._workspace
        persistence_error: BaseException | None = None
        cleanup_error: BaseException | None = None
        try:
            if workspace is None:
                persistence_error = ProfileUnavailableError()
            else:
                try:
                    archive = _encode_profile_archive_fd(
                        workspace.fd,
                        expected_uid=workspace.expected_uid,
                    )
                    self._snapshot[0].put(self._profile, archive)
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


async def _finalize_after_workers(
    workers: tuple[asyncio.Task[object], ...],
    *,
    vault: CredentialVault,
    profile: str,
    workspace: _PinnedWorkspace,
    profile_lock: threading.Lock,
) -> None:
    cancelled = False
    for worker in workers:
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                cancelled = True
            except BaseException:
                break
        if worker.done() and not worker.cancelled():
            with suppress(BaseException):
                worker.exception()
    try:
        await asyncio.to_thread(_finalize_deferred_workspace, vault, profile, workspace)
    finally:
        profile_lock.release()
    if cancelled:
        raise asyncio.CancelledError


def _finalize_deferred_workspace(
    vault: CredentialVault,
    profile: str,
    workspace: _PinnedWorkspace,
) -> None:
    persistence_error: BaseException | None = None
    cleanup_error: BaseException | None = None
    try:
        archive = _encode_profile_archive_fd(
            workspace.fd,
            expected_uid=workspace.expected_uid,
        )
        vault.put(profile, archive)
    except BaseException as error:
        persistence_error = error
    try:
        _remove_workspace(workspace)
    except BaseException as error:
        cleanup_error = error
    if persistence_error is not None and not isinstance(persistence_error, Exception):
        if cleanup_error is not None:
            persistence_error.add_note("browser profile cleanup failed")
        raise persistence_error
    if cleanup_error is not None and not isinstance(cleanup_error, Exception):
        raise cleanup_error
    if persistence_error is not None or cleanup_error is not None:
        failure = ProfileUnavailableError()
        if persistence_error is not None:
            failure.add_note("browser profile persistence failed")
        if cleanup_error is not None:
            failure.add_note("browser profile cleanup failed")
        raise failure


def _finish_deferred_finalizer(task: asyncio.Task[None]) -> None:
    _DEFERRED_FINALIZERS.discard(task)
    if task.cancelled():
        return
    try:
        error = task.exception()
    except BaseException:
        return
    if error is not None:
        task.get_loop().call_exception_handler(
            {
                "message": "browser profile deferred cleanup failed",
                "exception": ProfileUnavailableError(),
            }
        )


def bootstrap_profile(
    store: BrowserProfileStore,
    profile_id: str,
    target: ResolvedTarget,
    *,
    runner: BootstrapRunner,
    egress_proxy_url: str,
    page_action: PageAction,
    operator_timeout_seconds: float = _BOOTSTRAP_TIMEOUT_SECONDS,
) -> None:
    """Run one real headful Scrapling session and archive it only on success."""

    if type(store) is not BrowserProfileStore or not isinstance(target, ResolvedTarget):
        raise ProfileUnavailableError
    profile = validate_logical_key(profile_id)
    if (
        not callable(runner)
        or not callable(page_action)
        or isinstance(operator_timeout_seconds, bool)
        or not isinstance(operator_timeout_seconds, (int, float))
        or not math.isfinite(operator_timeout_seconds)
        or operator_timeout_seconds <= 0
        or operator_timeout_seconds > _BOOTSTRAP_TIMEOUT_SECONDS
    ):
        raise ProfileUnavailableError
    try:
        proxy = EgressProxyPolicy.from_url(egress_proxy_url).url
        snapshot = store._trusted_snapshot()
    except Exception:
        raise ProfileUnavailableError from None

    vault, _uid, _root_fd, _root_identity, _workspace_root, _vault_identity = snapshot
    lock = store._lock_for(profile, snapshot)
    with lock:
        workspace: _PinnedWorkspace | None = None
        active: BaseException | None = None
        action_completed = False

        def verified_page_action(page: object) -> object:
            nonlocal action_completed
            completed = threading.Event()
            results: list[object] = []
            errors: list[BaseException] = []

            def invoke_action() -> None:
                try:
                    result = page_action(page)
                    if inspect.isawaitable(result):
                        raise TypeError("bootstrap page action must be synchronous")
                    results.append(result)
                except BaseException as error:
                    errors.append(error)
                finally:
                    completed.set()

            threading.Thread(target=invoke_action, daemon=True).start()
            if not completed.wait(timeout=float(operator_timeout_seconds)):
                raise ProfileUnavailableError
            if errors:
                raise errors[0]
            action_completed = True
            return results[0]

        try:
            workspace = store._new_workspace(snapshot)
            runner(
                target.normalized_url,
                headless=False,
                user_data_dir=str(workspace.path),
                timeout=900_000,
                page_action=verified_page_action,
                proxy=proxy,
                dns_over_https=False,
                google_search=False,
                retries=1,
            )
            if not action_completed:
                raise ProfileUnavailableError
            archive = _encode_profile_archive_fd(
                workspace.fd,
                expected_uid=workspace.expected_uid,
            )
            vault.put(profile, archive)
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
            failure = ProfileUnavailableError()
            if cleanup_error is not None:
                failure.add_note("browser profile cleanup failed")
            raise failure from None
        if cleanup_error is not None:
            if not isinstance(cleanup_error, Exception):
                raise cleanup_error
            failure = ProfileUnavailableError()
            failure.add_note("browser profile cleanup failed")
            raise failure from None


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
        return _encode_profile_archive_fd(root_fd, expected_uid=uid)
    except ProfileUnavailableError:
        raise
    except Exception:
        raise ProfileUnavailableError from None
    finally:
        if root_fd >= 0:
            os.close(root_fd)


def _encode_profile_archive_fd(root_fd: int, *, expected_uid: int) -> bytes:
    root_before = os.fstat(root_fd)
    if (
        not stat.S_ISDIR(root_before.st_mode)
        or root_before.st_uid != expected_uid
        or stat.S_IMODE(root_before.st_mode) != 0o700
    ):
        raise ProfileUnavailableError
    entries: list[tuple[str, str, bytes]] = []
    seen_inodes: set[tuple[int, int]] = set()
    _scan_directory(root_fd, PurePosixPath(), entries, seen_inodes, expected_uid)
    root_after = os.fstat(root_fd)
    if not _same_directory_snapshot(root_before, root_after):
        raise ProfileUnavailableError
    entries.sort(key=lambda item: item[0].encode("utf-8"))
    return _build_archive(entries)


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
    _validate_archive_entries(entries)
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


def _validate_archive_entries(entries: list[tuple[str, str, bytes]]) -> None:
    total = 0
    seen: set[str] = set()
    types: dict[str, str] = {}
    previous: bytes | None = None
    for entry in entries:
        if not isinstance(entry, tuple) or len(entry) != 3:
            raise ProfileUnavailableError
        raw_path, kind, content = entry
        path = _validate_archive_path(raw_path)
        if kind not in {"d", "f"} or not isinstance(content, bytes):
            raise ProfileUnavailableError
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
            if content:
                raise ProfileUnavailableError
        else:
            if len(content) > MAX_FILE_BYTES:
                raise ProfileUnavailableError
            total += len(content)
            if total > MAX_TOTAL_UNPACKED_BYTES:
                raise ProfileUnavailableError
        types[path] = kind


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
    workspace_fd = -1
    try:
        workspace_fd = os.open(
            workspace,
            os.O_RDONLY
            | os.O_CLOEXEC
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_DIRECTORY", 0),
        )
        metadata = os.fstat(workspace_fd)
        _extract_profile_archive_fd(archive, workspace_fd, metadata.st_uid)
    except ProfileUnavailableError:
        raise
    except Exception:
        raise ProfileUnavailableError from None
    finally:
        if workspace_fd >= 0:
            os.close(workspace_fd)


def _extract_profile_archive_fd(archive: bytes, workspace_fd: int, expected_uid: int) -> None:
    entries = _decode_profile_archive(archive)
    root = os.fstat(workspace_fd)
    if (
        not stat.S_ISDIR(root.st_mode)
        or root.st_uid != expected_uid
        or stat.S_IMODE(root.st_mode) != 0o700
    ):
        raise ProfileUnavailableError
    directories: dict[str, tuple[int, tuple[int, int], str, int]] = {
        ".": (os.dup(workspace_fd), (root.st_dev, root.st_ino), "", -1)
    }
    try:
        for path, kind, content in entries:
            parsed = PurePosixPath(path)
            parent_key = parsed.parent.as_posix()
            parent_fd = directories[parent_key][0]
            name = parsed.name
            if kind == "d":
                os.mkdir(name, 0o700, dir_fd=parent_fd)
                child_fd = os.open(
                    name,
                    os.O_RDONLY
                    | os.O_CLOEXEC
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_DIRECTORY", 0),
                    dir_fd=parent_fd,
                )
                metadata = os.fstat(child_fd)
                if (
                    not stat.S_ISDIR(metadata.st_mode)
                    or metadata.st_uid != expected_uid
                    or stat.S_IMODE(metadata.st_mode) != 0o700
                ):
                    os.close(child_fd)
                    raise ProfileUnavailableError
                directories[path] = (
                    child_fd,
                    (metadata.st_dev, metadata.st_ino),
                    name,
                    parent_fd,
                )
                continue
            fd = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=parent_fd,
            )
            try:
                _write_all(fd, content)
                os.fsync(fd)
                metadata = os.fstat(fd)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != expected_uid
                    or stat.S_IMODE(metadata.st_mode) != 0o600
                ):
                    raise ProfileUnavailableError
            finally:
                os.close(fd)
        for key, (_directory_fd, identity, name, parent_fd) in directories.items():
            if key == ".":
                continue
            named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if not stat.S_ISDIR(named.st_mode) or (named.st_dev, named.st_ino) != identity:
                raise ProfileUnavailableError
    except BaseException:
        with suppress(BaseException):
            _clear_directory_fd(workspace_fd)
        raise
    finally:
        for directory_fd, _identity, _name, _parent_fd in directories.values():
            os.close(directory_fd)


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


def _remove_workspace(workspace: _PinnedWorkspace) -> None:
    cleanup_error: BaseException | None = None
    try:
        metadata = os.fstat(workspace.fd)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != workspace.expected_uid
            or (metadata.st_dev, metadata.st_ino) != workspace.identity
        ):
            raise ProfileUnavailableError
        _clear_directory_fd(workspace.fd)
        matching_names = []
        for name in os.listdir(workspace.root_fd):
            candidate = os.stat(name, dir_fd=workspace.root_fd, follow_symlinks=False)
            if (candidate.st_dev, candidate.st_ino) == workspace.identity:
                matching_names.append(name)
        if len(matching_names) != 1:
            raise ProfileUnavailableError
        os.rmdir(matching_names[0], dir_fd=workspace.root_fd)
        _remove_workspace_decoy(workspace)
        os.fsync(workspace.root_fd)
    except BaseException as error:
        cleanup_error = error
    finally:
        _unregister_profile_lease(workspace)
        os.close(workspace.fd)
        workspace.fd = -1
    if cleanup_error is not None:
        if not isinstance(cleanup_error, Exception):
            raise cleanup_error
        raise ProfileUnavailableError from None


def _remove_workspace_decoy(workspace: _PinnedWorkspace) -> None:
    try:
        metadata = os.stat(workspace.name, dir_fd=workspace.root_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if (metadata.st_dev, metadata.st_ino) == workspace.identity:
        return
    if stat.S_ISDIR(metadata.st_mode):
        if metadata.st_uid != workspace.expected_uid or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise ProfileUnavailableError
        fd = os.open(
            workspace.name,
            os.O_RDONLY
            | os.O_CLOEXEC
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_DIRECTORY", 0),
            dir_fd=workspace.root_fd,
        )
        try:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                raise ProfileUnavailableError
            _clear_directory_fd(fd)
        finally:
            os.close(fd)
        os.rmdir(workspace.name, dir_fd=workspace.root_fd)
        return
    os.unlink(workspace.name, dir_fd=workspace.root_fd)


def _clear_directory_fd(directory_fd: int) -> None:
    for name in os.listdir(directory_fd):
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
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
                if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                    raise ProfileUnavailableError
                _clear_directory_fd(child_fd)
                named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
                    raise ProfileUnavailableError
            finally:
                os.close(child_fd)
            os.rmdir(name, dir_fd=directory_fd)
            continue
        os.unlink(name, dir_fd=directory_fd)


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
