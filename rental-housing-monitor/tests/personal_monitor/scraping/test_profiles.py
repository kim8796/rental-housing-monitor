from __future__ import annotations

import asyncio
import inspect
import json
import os
import shutil
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path

import pytest

from personal_monitor.security.url_policy import ResolvedTarget
from personal_monitor.security.vault import CredentialVault


def profile_types():
    from personal_monitor.scraping.profiles import (
        BrowserProfileStore,
        ProfileUnavailableError,
        bootstrap_profile,
    )

    return BrowserProfileStore, ProfileUnavailableError, bootstrap_profile


def profile_store(tmp_path: Path):
    BrowserProfileStore, _, _ = profile_types()
    vault = CredentialVault(tmp_path / "vault", key=b"k" * 32)
    return BrowserProfileStore(vault, materialization_root=tmp_path / "workspaces"), vault


def seed_profile(store, tmp_path: Path, *, value: bytes = b"cookie=private") -> None:
    source = tmp_path / "source"
    source.mkdir(mode=0o700)
    (source / "Default").mkdir(mode=0o700)
    (source / "Default" / "Cookies").write_bytes(value)
    store.archive("profile-7", source)
    shutil.rmtree(source)


@pytest.mark.parametrize(
    "profile_id", ["../escape", "/absolute", "a/b", "a\\b", "a..b", "UPPER", "рrofile", "x" * 65]
)
def test_profile_id_cannot_escape_root(tmp_path: Path, profile_id: str) -> None:
    store, _ = profile_store(tmp_path)

    with pytest.raises(ValueError) as caught:
        store.path_for(profile_id)

    assert profile_id not in str(caught.value)


def test_profile_archive_is_deterministic_canonical_and_restores_file_modes(
    tmp_path: Path,
) -> None:
    store, vault = profile_store(tmp_path)
    source = tmp_path / "source"
    source.mkdir(mode=0o700)
    (source / "Default").mkdir(mode=0o700)
    (source / "Default" / "Cookies").write_bytes(b"private-cookie")
    (source / "Local State").write_bytes(b"private-state")

    store.archive("profile-7", source)
    first = vault.get("profile-7")
    store.archive("profile-7", source)
    second = vault.get("profile-7")

    assert first == second
    assert b"private-cookie" in first
    with store.materialize("profile-7") as workspace:
        assert (workspace / "Default" / "Cookies").read_bytes() == b"private-cookie"
        assert (workspace / "Local State").read_bytes() == b"private-state"
        assert stat.S_IMODE(workspace.stat().st_mode) == 0o700
        assert stat.S_IMODE((workspace / "Default").stat().st_mode) == 0o700
        assert stat.S_IMODE((workspace / "Local State").stat().st_mode) == 0o600

    assert list((tmp_path / "workspaces").iterdir()) == []


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
def test_archive_rejects_nonregular_or_multiply_linked_input(tmp_path: Path, kind: str) -> None:
    store, _ = profile_store(tmp_path)
    source = tmp_path / "source"
    source.mkdir(mode=0o700)
    regular = source / "regular"
    regular.write_bytes(b"private")
    unsafe = source / "unsafe"
    if kind == "symlink":
        unsafe.symlink_to(regular)
    elif kind == "hardlink":
        os.link(regular, unsafe)
    else:
        os.mkfifo(unsafe)

    with pytest.raises(Exception) as caught:
        store.archive("profile", source)

    assert str(source) not in repr(caught.value)
    assert "private" not in repr(caught.value)


def test_archive_directory_swap_cannot_redirect_scan_through_a_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, ProfileUnavailableError, _ = profile_types()
    from personal_monitor.security.vault import VaultError

    store, vault = profile_store(tmp_path)
    source = tmp_path / "source"
    nested = source / "nested"
    outside = tmp_path / "outside"
    source.mkdir(mode=0o700)
    nested.mkdir(mode=0o700)
    outside.mkdir(mode=0o700)
    (nested / "safe").write_bytes(b"safe")
    (outside / "Cookies").write_bytes(b"outside-private-cookie")
    import personal_monitor.scraping.profiles as profiles_module

    real_open = os.open
    swapped = False

    def swapping_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if not swapped and path == "nested" and kwargs.get("dir_fd") is not None:
            swapped = True
            shutil.rmtree(nested)
            nested.symlink_to(outside, target_is_directory=True)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(profiles_module.os, "open", swapping_open)

    with pytest.raises(ProfileUnavailableError):
        store.archive("profile", source)

    assert swapped
    with pytest.raises(VaultError):
        vault.get("profile")


def test_materialize_pins_original_workspace_across_rename_and_decoy_replacement(
    tmp_path: Path,
) -> None:
    store, _ = profile_store(tmp_path)
    seed_profile(store, tmp_path, value=b"original-private-cookie")
    moved: Path | None = None
    decoy: Path | None = None

    with store.materialize("profile-7") as workspace:
        moved = workspace.with_name(f"{workspace.name}-moved")
        decoy = workspace
        workspace.rename(moved)
        decoy.mkdir(mode=0o700)
        (decoy / "Default").mkdir(mode=0o700)
        (decoy / "Default" / "Cookies").write_bytes(b"decoy-private-cookie")
        (moved / "Default" / "Cookies").write_bytes(b"updated-private-cookie")

    assert moved is not None and not moved.exists()
    assert decoy is not None and not decoy.exists()
    with store.materialize("profile-7") as restored:
        assert (restored / "Default" / "Cookies").read_bytes() == b"updated-private-cookie"


def test_extract_intermediate_symlink_swap_never_writes_outside_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from personal_monitor.scraping.profiles import (
        ProfileUnavailableError,
        _extract_profile_archive,
    )

    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir(mode=0o700)
    outside.mkdir(mode=0o700)
    archive = raw_archive(
        [
            {"kind": "d", "path": "Default", "size": 0},
            {"kind": "f", "path": "Default/Cookies", "size": 19},
        ],
        b"late-private-cookie",
    )
    import personal_monitor.scraping.profiles as profiles_module

    real_open = os.open
    swapped = False

    def swapping_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if not swapped and Path(path).name == "Cookies":
            swapped = True
            shutil.rmtree(workspace / "Default")
            (workspace / "Default").symlink_to(outside, target_is_directory=True)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(profiles_module.os, "open", swapping_open)

    with pytest.raises(ProfileUnavailableError):
        _extract_profile_archive(archive, workspace)

    assert swapped
    assert not (outside / "Cookies").exists()


def raw_archive(manifest: list[dict[str, object]], payload: bytes = b"") -> bytes:
    encoded = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return b"PMPA1" + len(encoded).to_bytes(4, "big") + encoded + payload


@pytest.mark.parametrize(
    ("manifest", "payload"),
    [
        ([{"kind": "f", "path": "../escape", "size": 1}], b"x"),
        ([{"kind": "f", "path": "/absolute", "size": 1}], b"x"),
        ([{"kind": "f", "path": "a\\b", "size": 1}], b"x"),
        (
            [
                {"kind": "f", "path": "same", "size": 1},
                {"kind": "f", "path": "same", "size": 1},
            ],
            b"xx",
        ),
        (
            [
                {"kind": "f", "path": "Case", "size": 1},
                {"kind": "f", "path": "case", "size": 1},
            ],
            b"xx",
        ),
        ([{"kind": "l", "path": "link", "size": 0}], b""),
        ([{"kind": "h", "path": "hard", "size": 0}], b""),
        ([{"kind": "device", "path": "node", "size": 0}], b""),
        ([{"kind": "f", "path": "a/" * 33 + "f", "size": 1}], b"x"),
    ],
)
def test_decoder_rejects_traversal_links_special_duplicates_and_deep_trees(
    tmp_path: Path, manifest: list[dict[str, object]], payload: bytes
) -> None:
    from personal_monitor.scraping.profiles import (
        ProfileUnavailableError,
        _extract_profile_archive,
    )

    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)

    with pytest.raises(ProfileUnavailableError):
        _extract_profile_archive(raw_archive(manifest, payload), workspace)

    assert list(workspace.iterdir()) == []


def test_decoder_rejects_noncanonical_and_bounded_archive_bombs(tmp_path: Path) -> None:
    from personal_monitor.scraping.profiles import (
        MAX_ARCHIVE_BYTES,
        MAX_ENTRIES,
        MAX_FILE_BYTES,
        ProfileUnavailableError,
        _extract_profile_archive,
    )

    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    noncanonical_manifest = b'[{"size":1, "path":"file","kind":"f"}]'
    cases = [
        b"PMPA1" + len(noncanonical_manifest).to_bytes(4, "big") + noncanonical_manifest + b"x",
        raw_archive([{"kind": "f", "path": "file", "size": MAX_FILE_BYTES + 1}]),
        raw_archive([{"kind": "d", "path": f"d{i}", "size": 0} for i in range(MAX_ENTRIES + 1)]),
        b"x" * (MAX_ARCHIVE_BYTES + 1),
    ]

    for archive in cases:
        with pytest.raises(ProfileUnavailableError):
            _extract_profile_archive(archive, workspace)
        assert list(workspace.iterdir()) == []


@pytest.mark.parametrize(
    "entries",
    [
        [("Case", "f", b"one"), ("case", "f", b"two")],
        [("parent", "f", b"one"), ("parent/child", "f", b"two")],
    ],
)
def test_encoder_rejects_archives_its_decoder_would_reject(
    entries: list[tuple[str, str, bytes]],
) -> None:
    from personal_monitor.scraping.profiles import ProfileUnavailableError, _build_archive

    with pytest.raises(ProfileUnavailableError):
        _build_archive(entries)


@pytest.mark.parametrize(
    "exception_type", [RuntimeError, asyncio.CancelledError, KeyboardInterrupt, SystemExit]
)
def test_materialize_reencrypts_changes_and_removes_plaintext_on_every_exit(
    tmp_path: Path, exception_type: type[BaseException]
) -> None:
    store, _ = profile_store(tmp_path)
    seed_profile(store, tmp_path)
    workspace_path: Path | None = None

    with pytest.raises(exception_type), store.materialize("profile-7") as workspace:
        workspace_path = workspace
        (workspace / "Default" / "Cookies").write_bytes(b"cookie=changed")
        raise exception_type("secret exception detail")

    assert workspace_path is not None and not workspace_path.exists()
    with store.materialize("profile-7") as restored:
        assert (restored / "Default" / "Cookies").read_bytes() == b"cookie=changed"


def test_missing_and_corrupt_profiles_fail_closed_without_identifiers_or_bytes(
    tmp_path: Path,
) -> None:
    store, vault = profile_store(tmp_path)
    _, ProfileUnavailableError, _ = profile_types()

    with pytest.raises(ProfileUnavailableError) as missing, store.materialize("secret-profile"):
        pass
    vault.put("secret-profile", b"corrupt-private-archive")
    with pytest.raises(ProfileUnavailableError) as corrupt, store.materialize("secret-profile"):
        pass

    for caught in (missing, corrupt):
        assert str(caught.value) == "browser profile is unavailable"
        assert "secret-profile" not in repr(caught.value)
        assert "corrupt" not in repr(caught.value)


def test_profile_secrets_never_reach_logs_or_files_outside_the_active_workspace(
    tmp_path: Path, caplog
) -> None:
    store, _ = profile_store(tmp_path)
    marker = b"unique-private-cookie-marker"
    seed_profile(store, tmp_path, value=marker)

    with store.materialize("profile-7") as workspace:
        assert marker in (workspace / "Default" / "Cookies").read_bytes()

    assert marker.decode() not in caplog.text
    assert all(marker not in path.read_bytes() for path in tmp_path.rglob("*") if path.is_file())


def test_materializations_are_unique_and_same_profile_sessions_are_serialized(
    tmp_path: Path,
) -> None:
    store, _ = profile_store(tmp_path)
    seed_profile(store, tmp_path)
    first_entered = threading.Event()
    allow_first_exit = threading.Event()
    second_entered = threading.Event()
    paths: list[Path] = []

    def first() -> None:
        with store.materialize("profile-7") as workspace:
            paths.append(workspace)
            first_entered.set()
            assert allow_first_exit.wait(timeout=5)
            (workspace / "Default" / "Cookies").write_bytes(b"first")

    def second() -> None:
        assert first_entered.wait(timeout=5)
        with store.materialize("profile-7") as workspace:
            paths.append(workspace)
            second_entered.set()
            assert (workspace / "Default" / "Cookies").read_bytes() == b"first"
            (workspace / "Default" / "Cookies").write_bytes(b"second")

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(first)
        second_future = executor.submit(second)
        assert first_entered.wait(timeout=5)
        assert not second_entered.wait(timeout=0.1)
        allow_first_exit.set()
        first_future.result()
        second_future.result()

    assert len(paths) == 2 and paths[0] != paths[1]
    assert all(not path.exists() for path in paths)
    with store.materialize("profile-7") as workspace:
        assert (workspace / "Default" / "Cookies").read_bytes() == b"second"


def test_same_profile_serialization_spans_store_and_vault_instances(tmp_path: Path) -> None:
    BrowserProfileStore, _, _ = profile_types()
    first_store, _ = profile_store(tmp_path)
    seed_profile(first_store, tmp_path)
    second_vault = CredentialVault(tmp_path / "vault", key=b"k" * 32)
    second_store = BrowserProfileStore(
        second_vault,
        materialization_root=tmp_path / "workspaces",
    )
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()

    def first() -> None:
        with first_store.materialize("profile-7"):
            first_entered.set()
            assert release_first.wait(timeout=5)

    def second() -> None:
        assert first_entered.wait(timeout=5)
        with second_store.materialize("profile-7"):
            second_entered.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(first)
        second_future = executor.submit(second)
        assert first_entered.wait(timeout=5)
        assert not second_entered.wait(timeout=0.1)
        release_first.set()
        first_future.result()
        second_future.result()


def test_cleanup_failure_is_visible_but_cannot_mask_a_system_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _ = profile_store(tmp_path)
    seed_profile(store, tmp_path)
    import personal_monitor.scraping.profiles as profiles_module

    def fail_cleanup(_path: Path) -> None:
        raise RuntimeError("secret cleanup detail")

    monkeypatch.setattr(profiles_module, "_remove_workspace", fail_cleanup)
    original = KeyboardInterrupt("operator stop")

    with pytest.raises(KeyboardInterrupt) as caught, store.materialize("profile-7"):
        raise original

    assert caught.value is original
    assert caught.value.__notes__ == ["browser profile cleanup failed"]


def test_persistence_system_exception_wins_over_ordinary_cleanup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _ = profile_store(tmp_path)
    seed_profile(store, tmp_path)
    import personal_monitor.scraping.profiles as profiles_module

    with pytest.raises(KeyboardInterrupt) as caught, store.materialize("profile-7"):
        monkeypatch.setattr(
            profiles_module,
            "_encode_profile_archive_fd",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt("persist")),
        )
        monkeypatch.setattr(
            profiles_module,
            "_remove_workspace",
            lambda _path: (_ for _ in ()).throw(RuntimeError("cleanup")),
        )

    assert caught.value.__notes__ == ["browser profile cleanup failed"]


def test_enter_cleanup_system_exception_does_not_leak_the_profile_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _ = profile_store(tmp_path)
    import personal_monitor.scraping.profiles as profiles_module

    real_remove = profiles_module._remove_workspace
    monkeypatch.setattr(
        profiles_module,
        "_remove_workspace",
        lambda _path: (_ for _ in ()).throw(KeyboardInterrupt("cleanup")),
    )
    with pytest.raises(KeyboardInterrupt), store.materialize("profile-7"):
        pass
    monkeypatch.setattr(profiles_module, "_remove_workspace", real_remove)
    lock = store._lock_for("profile-7")

    def lock_is_available_to_another_thread() -> bool:
        acquired = lock.acquire(blocking=False)
        if acquired:
            lock.release()
        return acquired

    with ThreadPoolExecutor(max_workers=1) as executor:
        assert executor.submit(lock_is_available_to_another_thread).result()


def test_async_same_profile_sessions_serialize_by_task_without_blocking_loop(
    tmp_path: Path,
) -> None:
    store, _ = profile_store(tmp_path)
    seed_profile(store, tmp_path, value=b"initial")

    async def scenario() -> None:
        async with asyncio.timeout(1):
            first_entered = asyncio.Event()
            release_first = asyncio.Event()
            second_entered = asyncio.Event()

            async def first() -> None:
                async with store.materialize("profile-7") as workspace:
                    first_entered.set()
                    await release_first.wait()
                    (workspace / "Default" / "Cookies").write_bytes(b"first")

            async def second() -> None:
                await first_entered.wait()
                async with store.materialize("profile-7") as workspace:
                    second_entered.set()
                    assert (workspace / "Default" / "Cookies").read_bytes() == b"first"
                    (workspace / "Default" / "Cookies").write_bytes(b"second")

            one = asyncio.create_task(first())
            two = asyncio.create_task(second())
            await first_entered.wait()
            await asyncio.sleep(0.02)
            assert not second_entered.is_set()
            release_first.set()
            await asyncio.gather(one, two)

    asyncio.run(scenario())
    with store.materialize("profile-7") as workspace:
        assert (workspace / "Default" / "Cookies").read_bytes() == b"second"


def test_bootstrap_runner_is_offline_injectable_headful_bounded_and_archived(
    tmp_path: Path,
) -> None:
    store, _ = profile_store(tmp_path)
    _, _, bootstrap_profile = profile_types()
    calls: list[tuple[str, dict[str, object]]] = []

    def action(_page: object) -> None:
        pass

    target = ResolvedTarget(
        normalized_url="https://example.com/login?opaque=private-query",
        hostname="example.com",
        port=443,
        addresses=frozenset({"93.184.216.34"}),
    )

    def runner(url: str, **kwargs: object) -> object:
        calls.append((url, kwargs))
        workspace = Path(str(kwargs["user_data_dir"]))
        (workspace / "Cookies").write_bytes(b"bootstrap-private-cookie")
        callback = kwargs["page_action"]
        assert callable(callback)
        callback(object())
        return object()

    bootstrap_profile(
        store,
        "profile-7",
        target,
        runner=runner,
        egress_proxy_url="http://proxy.internal:8080",
        page_action=action,
    )

    assert len(calls) == 1
    url, kwargs = calls[0]
    assert url == target.normalized_url
    assert kwargs["headless"] is False
    assert kwargs["timeout"] == 900_000
    assert kwargs["page_action"] is not action
    assert kwargs["proxy"] == "http://proxy.internal:8080"
    assert not Path(str(kwargs["user_data_dir"])).exists()
    with store.materialize("profile-7") as workspace:
        assert (workspace / "Cookies").read_bytes() == b"bootstrap-private-cookie"


def test_bootstrap_fails_closed_if_scrapling_never_completes_page_action(tmp_path: Path) -> None:
    store, _ = profile_store(tmp_path)
    _, ProfileUnavailableError, bootstrap_profile = profile_types()
    target = ResolvedTarget(
        normalized_url="https://example.com/login",
        hostname="example.com",
        port=443,
        addresses=frozenset({"93.184.216.34"}),
    )

    with pytest.raises(ProfileUnavailableError):
        bootstrap_profile(
            store,
            "profile-7",
            target,
            runner=lambda _url, **_kwargs: object(),
            egress_proxy_url="http://proxy.internal:8080",
            page_action=lambda _page: None,
        )

    assert list((tmp_path / "workspaces").iterdir()) == []


def test_bootstrap_bounds_a_blocked_operator_action_and_cleans_workspace(tmp_path: Path) -> None:
    store, _ = profile_store(tmp_path)
    _, ProfileUnavailableError, bootstrap_profile = profile_types()
    target = ResolvedTarget(
        normalized_url="https://example.com/login",
        hostname="example.com",
        port=443,
        addresses=frozenset({"93.184.216.34"}),
    )
    block_forever = threading.Event()

    def runner(_url: str, **kwargs: object) -> object:
        callback = kwargs["page_action"]
        assert callable(callback)
        with suppress(Exception):
            callback(object())
        return object()

    started = time.monotonic()
    with pytest.raises(ProfileUnavailableError):
        bootstrap_profile(
            store,
            "profile-7",
            target,
            runner=runner,
            egress_proxy_url="http://proxy.internal:8080",
            page_action=lambda _page: block_forever.wait(),
            operator_timeout_seconds=0.02,
        )

    assert time.monotonic() - started < 1
    assert list((tmp_path / "workspaces").iterdir()) == []


def test_bootstrap_reports_cleanup_failure_alongside_ordinary_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _ = profile_store(tmp_path)
    _, ProfileUnavailableError, bootstrap_profile = profile_types()
    import personal_monitor.scraping.profiles as profiles_module

    target = ResolvedTarget(
        normalized_url="https://example.com/login",
        hostname="example.com",
        port=443,
        addresses=frozenset({"93.184.216.34"}),
    )
    monkeypatch.setattr(
        profiles_module,
        "_remove_workspace",
        lambda _workspace: (_ for _ in ()).throw(RuntimeError("private cleanup detail")),
    )

    with pytest.raises(ProfileUnavailableError) as caught:
        bootstrap_profile(
            store,
            "profile-7",
            target,
            runner=lambda _url, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("private runner detail")
            ),
            egress_proxy_url="http://proxy.internal:8080",
            page_action=lambda _page: None,
        )

    assert getattr(caught.value, "__notes__", []) == ["browser profile cleanup failed"]
    assert "private" not in repr(caught.value)


def test_bootstrap_failure_never_archives_or_leaves_plaintext(tmp_path: Path) -> None:
    store, _ = profile_store(tmp_path)
    _, ProfileUnavailableError, bootstrap_profile = profile_types()
    target = ResolvedTarget(
        normalized_url="https://example.com/login?token=private-query",
        hostname="example.com",
        port=443,
        addresses=frozenset({"93.184.216.34"}),
    )

    def runner(_url: str, **kwargs: object) -> object:
        Path(str(kwargs["user_data_dir"]), "Cookies").write_bytes(b"private-cookie")
        raise RuntimeError("private-query private-cookie")

    with pytest.raises(ProfileUnavailableError) as caught:
        bootstrap_profile(
            store,
            "profile-7",
            target,
            runner=runner,
            egress_proxy_url="http://proxy.internal:8080",
            page_action=lambda _page: None,
        )

    assert "private" not in repr(caught.value)
    assert list((tmp_path / "workspaces").iterdir()) == []
    with pytest.raises(ProfileUnavailableError), store.materialize("profile-7"):
        pass


def test_profile_store_rejects_vault_subclasses(tmp_path: Path) -> None:
    class VaultSubclass(CredentialVault):
        pass

    with pytest.raises(TypeError):
        VaultSubclass(tmp_path / "vault", key=b"k" * 32)


def test_profile_store_rejects_low_level_vault_swap_before_archive_dispatch(
    tmp_path: Path,
) -> None:
    store, _ = profile_store(tmp_path)
    _, ProfileUnavailableError, _ = profile_types()
    source = tmp_path / "source"
    source.mkdir(mode=0o700)
    (source / "Cookies").write_bytes(b"private-cookie")
    captured: list[bytes] = []

    class CapturingVault:
        _lock_identity = ((0, 0), "attacker")

        def put(self, _key: str, value: bytes) -> None:
            captured.append(value)

    object.__setattr__(store, "_vault", CapturingVault())

    with pytest.raises(ProfileUnavailableError):
        store.archive("profile-7", source)

    assert captured == []


def test_materialization_uses_one_pinned_vault_snapshot_across_callbacks(tmp_path: Path) -> None:
    store, vault = profile_store(tmp_path)
    seed_profile(store, tmp_path, value=b"initial")
    captured: list[bytes] = []

    class CapturingVault:
        def put(self, _key: str, value: bytes) -> None:
            captured.append(value)

    with store.materialize("profile-7") as workspace:
        object.__setattr__(store, "_vault", CapturingVault())
        (workspace / "Default" / "Cookies").write_bytes(b"changed")

    assert captured == []
    object.__setattr__(store, "_vault", vault)
    with store.materialize("profile-7") as workspace:
        assert (workspace / "Default" / "Cookies").read_bytes() == b"changed"


def test_bootstrap_rejects_profile_store_subclasses_before_runner_dispatch(
    tmp_path: Path,
) -> None:
    BrowserProfileStore, ProfileUnavailableError, bootstrap_profile = profile_types()

    class StoreSubclass(BrowserProfileStore):
        pass

    store = object.__new__(StoreSubclass)
    target = ResolvedTarget(
        normalized_url="https://example.com/login",
        hostname="example.com",
        port=443,
        addresses=frozenset({"93.184.216.34"}),
    )
    calls: list[object] = []

    with pytest.raises(ProfileUnavailableError):
        bootstrap_profile(
            store,
            "profile-7",
            target,
            runner=lambda *_args, **_kwargs: calls.append(object()),
            egress_proxy_url="http://proxy.internal:8080",
            page_action=lambda _page: None,
        )

    assert calls == []


def test_profile_store_is_sealed_after_construction(tmp_path: Path) -> None:
    store, vault = profile_store(tmp_path)

    with pytest.raises(AttributeError):
        store._vault = vault  # type: ignore[misc]


def test_vault_symbol_replacement_before_store_construction_cannot_receive_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    BrowserProfileStore, _, _ = profile_types()
    import personal_monitor.scraping.profiles as profiles_module

    captured: list[bytes] = []

    class CapturingVault:
        _lock_identity = (123, 456)

        def _trusted_snapshot(self):
            return object(), -1, os.geteuid(), self._lock_identity, threading.RLock()

        def put(self, _key: str, value: bytes) -> None:
            captured.append(value)

    source = tmp_path / "source"
    source.mkdir(mode=0o700)
    (source / "Cookies").write_bytes(b"private-cookie")
    rejected = False
    with monkeypatch.context() as patch:
        patch.setattr(profiles_module, "CredentialVault", CapturingVault)
        try:
            store = BrowserProfileStore(
                CapturingVault(),
                materialization_root=tmp_path / "workspaces",
            )
            store.archive("profile", source)
        except TypeError:
            rejected = True

    assert rejected
    assert captured == []


def test_store_symbol_replacement_before_subclass_construction_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    BrowserProfileStore, _, _ = profile_types()

    class StoreSubclass(BrowserProfileStore):
        pass

    vault = CredentialVault(tmp_path / "vault", key=b"k" * 32)
    import personal_monitor.scraping.profiles as profiles_module

    with monkeypatch.context() as patch:
        patch.setattr(profiles_module, "BrowserProfileStore", StoreSubclass)
        with pytest.raises(TypeError):
            StoreSubclass(vault, materialization_root=tmp_path / "workspaces")


def test_profile_accessor_symbol_replacement_before_construction_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    BrowserProfileStore, _, _ = profile_types()
    import personal_monitor.scraping.profiles as profiles_module

    source = tmp_path / "source"
    source.mkdir(mode=0o700)
    (source / "Cookies").write_bytes(b"private-cookie")
    vault = CredentialVault(tmp_path / "vault", key=b"k" * 32)
    calls: list[str] = []

    with monkeypatch.context() as patch:
        patch.setattr(profiles_module, "_pin_profile_store", lambda *_args: calls.append("pin"))
        patch.setattr(
            profiles_module,
            "_acquire_profile_store",
            lambda *_args: calls.append("acquire"),
        )
        patch.setattr(
            profiles_module,
            "_release_profile_store",
            lambda *_args: calls.append("release"),
        )
        store = BrowserProfileStore(vault, materialization_root=tmp_path / "workspaces")
        store.archive("profile", source)

    store.close()
    assert calls == []


def test_profile_public_api_has_no_internal_dependency_injection_parameters() -> None:
    BrowserProfileStore, _, bootstrap_profile = profile_types()

    assert tuple(inspect.signature(BrowserProfileStore).parameters) == (
        "vault",
        "materialization_root",
        "require_memory_backed",
        "expected_uid",
    )
    assert tuple(inspect.signature(BrowserProfileStore.close).parameters) == ("self",)
    assert tuple(inspect.signature(BrowserProfileStore._trusted_snapshot).parameters) == ("self",)
    assert tuple(inspect.signature(bootstrap_profile).parameters) == (
        "store",
        "profile_id",
        "target",
        "runner",
        "egress_proxy_url",
        "page_action",
        "operator_timeout_seconds",
    )
