from __future__ import annotations

import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


def vault_types():
    from personal_monitor.security.vault import CredentialVault, VaultError, load_master_key

    return CredentialVault, VaultError, load_master_key


def make_key_file(path: Path, value: bytes = b"k" * 32) -> Path:
    path.write_bytes(value)
    path.chmod(0o600)
    return path


@pytest.mark.parametrize(
    "logical_key",
    [
        "../escape",
        "/absolute",
        "a/b",
        "a\\b",
        ".",
        "..",
        "a..b",
        "UPPER",
        "рrofile",
        "nul\x00suffix",
        "x" * 65,
        "",
    ],
)
def test_vault_rejects_unsafe_logical_keys_before_filesystem_access(
    tmp_path: Path, logical_key: str
) -> None:
    CredentialVault, _, _ = vault_types()
    vault = CredentialVault(tmp_path / "vault", key=b"k" * 32)

    with pytest.raises(ValueError) as caught:
        vault.get(logical_key)

    if logical_key:
        assert logical_key not in str(caught.value)
    assert list((tmp_path / "vault").iterdir()) == []


def test_vault_round_trip_is_versioned_and_never_stores_plaintext(tmp_path: Path) -> None:
    CredentialVault, _, _ = vault_types()
    vault = CredentialVault(tmp_path / "vault", key=b"k" * 32)
    secret = b"session-cookie=private-value"

    vault.put("profile-7", secret)

    record = (tmp_path / "vault" / "profile-7.bin").read_bytes()
    assert vault.get("profile-7") == secret
    assert record.startswith(b"PMV1")
    assert secret not in record
    assert b"private-value" not in record
    assert stat.S_IMODE((tmp_path / "vault").stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "vault" / "profile-7.bin").stat().st_mode) == 0o600
    assert list((tmp_path / "vault").glob(".*.tmp")) == []


def test_vault_delete_removes_only_the_record_and_missing_is_redacted(tmp_path: Path) -> None:
    CredentialVault, VaultError, _ = vault_types()
    vault = CredentialVault(tmp_path / "vault", key=b"k" * 32)
    vault.put("profile-7", b"secret")

    vault.delete("profile-7")

    assert not (tmp_path / "vault" / "profile-7.bin").exists()
    with pytest.raises(VaultError) as caught:
        vault.get("profile-7")
    assert "profile-7" not in str(caught.value)
    assert "profile-7" not in repr(caught.value)


def test_delete_unlinks_a_record_symlink_without_following_its_target(tmp_path: Path) -> None:
    CredentialVault, VaultError, _ = vault_types()
    vault = CredentialVault(tmp_path / "vault", key=b"k" * 32)
    target = tmp_path / "outside"
    target.write_bytes(b"must-survive")
    record = tmp_path / "vault" / "profile-7.bin"
    record.symlink_to(target)

    with pytest.raises(VaultError):
        vault.delete("profile-7")

    assert target.read_bytes() == b"must-survive"
    assert record.is_symlink()


@pytest.mark.parametrize("mutation", ["header", "nonce", "ciphertext", "aad", "truncate"])
def test_vault_rejects_tampered_or_misbound_records_with_one_redacted_error(
    tmp_path: Path, mutation: str
) -> None:
    CredentialVault, VaultError, _ = vault_types()
    vault = CredentialVault(tmp_path / "vault", key=b"k" * 32)
    vault.put("profile-7", b"highly-private-payload")
    source = tmp_path / "vault" / "profile-7.bin"
    data = bytearray(source.read_bytes())
    target = source
    if mutation == "header":
        data[0] ^= 1
    elif mutation == "nonce":
        data[4] ^= 1
    elif mutation == "ciphertext":
        data[-1] ^= 1
    elif mutation == "aad":
        target = tmp_path / "vault" / "profile-8.bin"
    else:
        data = data[:15]
    target.write_bytes(data)
    target.chmod(0o600)

    with pytest.raises(VaultError) as caught:
        vault.get("profile-8" if mutation == "aad" else "profile-7")

    assert str(caught.value) == "credential vault operation failed"
    assert "profile" not in repr(caught.value)
    assert "private" not in repr(caught.value)


def test_vault_rejects_oversized_symlink_and_nonregular_records(tmp_path: Path) -> None:
    CredentialVault, VaultError, _ = vault_types()
    from personal_monitor.security.vault import MAX_VAULT_RECORD_BYTES

    cases = ("oversized", "symlink", "directory")
    for case in cases:
        root = tmp_path / case
        vault = CredentialVault(root, key=b"k" * 32)
        record = root / "profile.bin"
        if case == "oversized":
            with record.open("wb") as stream:
                stream.truncate(MAX_VAULT_RECORD_BYTES + 1)
            record.chmod(0o600)
        elif case == "symlink":
            outside = tmp_path / "outside-record"
            outside.write_bytes(b"PMV1" + b"x" * 64)
            record.symlink_to(outside)
        else:
            record.mkdir()

        with pytest.raises(VaultError):
            vault.get("profile")


@pytest.mark.parametrize("failure_point", ["replace", "directory_fsync"])
def test_failed_atomic_put_preserves_old_record_and_cleans_temporary_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_point: str
) -> None:
    CredentialVault, VaultError, _ = vault_types()
    import personal_monitor.security.vault as vault_module

    vault = CredentialVault(tmp_path / "vault", key=b"k" * 32)
    vault.put("profile", b"old-secret")
    real_fsync = os.fsync
    real_replace = os.replace
    if failure_point == "replace":

        def fail_replace(*args, **kwargs):
            raise OSError("attacker-controlled replace detail")

        monkeypatch.setattr(vault_module.os, "replace", fail_replace)
    else:
        calls = 0

        def fail_parent_fsync(fd: int) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("attacker-controlled fsync detail")
            real_fsync(fd)

        monkeypatch.setattr(vault_module.os, "fsync", fail_parent_fsync)

    with pytest.raises(VaultError) as caught:
        vault.put("profile", b"new-secret")

    monkeypatch.setattr(vault_module.os, "fsync", real_fsync)
    monkeypatch.setattr(vault_module.os, "replace", real_replace)
    assert vault.get("profile") == b"old-secret"
    assert "attacker" not in str(caught.value)
    assert sorted(path.name for path in (tmp_path / "vault").iterdir()) == ["profile.bin"]


def test_concurrent_get_and_put_observe_only_complete_authenticated_values(tmp_path: Path) -> None:
    CredentialVault, _, _ = vault_types()
    vault = CredentialVault(tmp_path / "vault", key=b"k" * 32)
    values = [bytes([index]) * 8192 for index in range(1, 17)]
    vault.put("profile", values[0])

    def writer() -> None:
        for value in values[1:]:
            vault.put("profile", value)

    def reader() -> list[bytes]:
        return [vault.get("profile") for _ in range(40)]

    with ThreadPoolExecutor(max_workers=5) as executor:
        writer_future = executor.submit(writer)
        readers = [executor.submit(reader) for _ in range(4)]
        writer_future.result()
        observed = [value for future in readers for value in future.result()]

    assert observed
    assert all(value in values for value in observed)


def test_master_key_loader_uses_the_opened_descriptor_even_if_path_is_swapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, load_master_key = vault_types()
    import personal_monitor.security.vault as vault_module

    key_path = make_key_file(tmp_path / "master.key", b"a" * 32)
    replacement = make_key_file(tmp_path / "replacement", b"b" * 32)
    real_open = os.open

    def swapping_open(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        if Path(path) == key_path:
            os.replace(replacement, key_path)
        return fd

    monkeypatch.setattr(vault_module.os, "open", swapping_open)

    assert load_master_key(key_path) == b"a" * 32


@pytest.mark.parametrize("case", ["symlink", "mode", "length", "directory", "owner"])
def test_master_key_loader_rejects_unsafe_files_with_a_fixed_error(
    tmp_path: Path, case: str
) -> None:
    _, VaultError, load_master_key = vault_types()
    target = make_key_file(tmp_path / "target", b"k" * 32)
    key_path = tmp_path / "master.key"
    expected_uid = os.geteuid()
    if case == "symlink":
        key_path.symlink_to(target)
    elif case == "mode":
        make_key_file(key_path).chmod(0o640)
    elif case == "length":
        make_key_file(key_path, b"x" * 31)
    elif case == "directory":
        key_path.mkdir()
        key_path.chmod(0o600)
    else:
        make_key_file(key_path)
        expected_uid += 1

    with pytest.raises(VaultError) as caught:
        load_master_key(key_path, expected_uid=expected_uid)

    assert str(caught.value) == "credential vault operation failed"
    assert str(key_path) not in repr(caught.value)


def test_vault_rejects_untrusted_root_objects(tmp_path: Path) -> None:
    CredentialVault, VaultError, _ = vault_types()
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    symlink = tmp_path / "vault-link"
    symlink.symlink_to(real, target_is_directory=True)
    open_root = tmp_path / "open-root"
    open_root.mkdir(mode=0o755)

    with pytest.raises(VaultError):
        CredentialVault(symlink, key=b"k" * 32)
    with pytest.raises(VaultError):
        CredentialVault(open_root, key=b"k" * 32)
