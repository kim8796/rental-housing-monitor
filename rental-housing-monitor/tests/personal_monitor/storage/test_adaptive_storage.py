from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from lxml.html import fromstring
from scrapling import Selector

from personal_monitor.security.encryption import AesGcmCipher
from personal_monitor.storage import open_database


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    value = open_database(tmp_path / "adaptive-state.db")
    yield value
    value.close()


def adaptive_store(connection: sqlite3.Connection, key: bytes = b"k" * 32):
    from personal_monitor.scraping.adaptive_storage import EncryptedAdaptiveStorage

    return EncryptedAdaptiveStorage(
        connection,
        cipher=AesGcmCipher(key),
        clock=lambda: datetime(2026, 7, 23, 12, 0, tzinfo=UTC),
    )


def namespace(store, *, owner: str = "owner-secret", version: str = "version-secret"):
    return store.for_namespace(
        owner_id=owner,
        monitor_id="monitor-secret",
        version_id=version,
    )


def element():
    return fromstring(
        '<article class="private-card" data-token="credential-secret">'
        "private page text credential-secret<span>child</span></article>"
    )


def authenticated_aad(connection: sqlite3.Connection, identifier: str) -> bytes:
    expires_at = connection.execute("SELECT expires_at FROM adaptive_features LIMIT 1").fetchone()[
        "expires_at"
    ]
    base = (
        b"personal-monitor/adaptive-feature/v1\0"
        b"owner-secret\0monitor-secret\0version-secret\0" + identifier.encode()
    )
    return base + b"\0expires_at\0" + expires_at.encode()


def test_encrypted_store_persists_only_hashes_ciphertext_and_redacted_metadata(
    connection: sqlite3.Connection,
) -> None:
    store = adaptive_store(connection)
    scoped = namespace(store)

    scoped.save(element(), "field:price:0")

    row = connection.execute("SELECT * FROM adaptive_features").fetchone()
    assert set(row.keys()) == {
        "key_hash",
        "namespace_hash",
        "nonce",
        "ciphertext",
        "created_at",
        "updated_at",
        "expires_at",
    }
    assert len(row["key_hash"]) == 64
    assert len(row["namespace_hash"]) == 64
    assert len(row["nonce"]) == 12
    assert len(row["ciphertext"]) > 16
    assert row["expires_at"] > row["updated_at"]
    serialized_row = b"|".join(
        bytes(value) if isinstance(value, bytes) else str(value).encode() for value in row
    )
    for forbidden in (
        b"owner-secret",
        b"monitor-secret",
        b"version-secret",
        b"field:price:0",
        b"private-card",
        b"credential-secret",
        b"private page text",
    ):
        assert forbidden not in serialized_row
    assert repr(store) == "EncryptedAdaptiveStorage()"
    assert repr(scoped) == "EncryptedAdaptiveNamespace()"


def test_real_scrapling_auto_save_and_adaptive_relocation_round_trip_encrypted(
    connection: sqlite3.Connection,
) -> None:
    store = adaptive_store(connection)
    baseline_storage = namespace(store)
    baseline = Selector(
        b'<main><span class="price" data-token="credential-secret">1000</span></main>',
        adaptive=True,
        _storage=baseline_storage,
    )
    assert baseline.css(".price", identifier="field:price:0", auto_save=True)

    recovery_storage = namespace(store)
    changed = Selector(
        b'<section><strong class="amount" data-token="credential-secret">900</strong></section>',
        adaptive=True,
        _storage=recovery_storage,
    )
    relocated = changed.css(".price", identifier="field:price:0", adaptive=True)

    assert len(relocated) == 1
    assert relocated[0].tag == "strong"
    restored = recovery_storage.retrieve("field:price:0")
    assert restored is not None
    assert restored["tag"] == "span"
    assert restored["attributes"]["data-token"] == "credential-secret"


def test_database_files_never_contain_feature_plaintext_or_namespace_values(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "encrypted-features.db"
    connection = open_database(database_path)
    scoped = namespace(adaptive_store(connection))
    scoped.save(element(), "field:price:0")
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.close()

    stored_bytes = b"".join(
        path.read_bytes()
        for path in (database_path, Path(f"{database_path}-wal"), Path(f"{database_path}-shm"))
        if path.exists()
    )
    for forbidden in (
        b"owner-secret",
        b"monitor-secret",
        b"version-secret",
        b"field:price:0",
        b"private-card",
        b"credential-secret",
        b"private page text",
    ):
        assert forbidden not in stored_bytes


def test_namespace_and_identifier_are_authenticated_and_isolated(
    connection: sqlite3.Connection,
) -> None:
    store = adaptive_store(connection)
    namespace(store).save(element(), "field:price:0")

    assert namespace(store, owner="other-owner").retrieve("field:price:0") is None
    assert namespace(store, version="other-version").retrieve("field:price:0") is None
    assert namespace(store).retrieve("field:title:0") is None
    with pytest.raises(ValueError, match="authentication") as caught:
        namespace(adaptive_store(connection, b"z" * 32)).retrieve("field:price:0")
    assert "owner-secret" not in str(caught.value)
    assert "field:price:0" not in str(caught.value)


def test_tampering_and_malformed_ciphertext_fail_with_one_redacted_error(
    connection: sqlite3.Connection,
) -> None:
    scoped = namespace(adaptive_store(connection))
    scoped.save(element(), "field:price:0")
    row = connection.execute("SELECT key_hash, ciphertext FROM adaptive_features").fetchone()
    tampered = bytes(row["ciphertext"])
    connection.execute(
        "UPDATE adaptive_features SET ciphertext = ? WHERE key_hash = ?",
        (tampered[:-1] + bytes([tampered[-1] ^ 1]), row["key_hash"]),
    )

    with pytest.raises(ValueError, match="authentication") as caught:
        scoped.retrieve("field:price:0")

    assert "credential-secret" not in str(caught.value)
    assert row["key_hash"] not in str(caught.value)


@pytest.mark.parametrize(
    "identifier",
    ["", "price", "../field:price:0", "field:Price:0", "field:price:-1", "x" * 300],
)
def test_identifier_contract_is_closed_and_errors_are_redacted(
    connection: sqlite3.Connection,
    identifier: str,
) -> None:
    scoped = namespace(adaptive_store(connection))

    with pytest.raises(ValueError) as caught:
        scoped.save(element(), identifier)

    if identifier:
        assert identifier not in str(caught.value)
    assert connection.execute("SELECT count(*) FROM adaptive_features").fetchone()[0] == 0


def test_feature_size_and_shape_limits_fail_before_writes(
    connection: sqlite3.Connection,
) -> None:
    scoped = namespace(adaptive_store(connection))
    oversized = fromstring(f'<div data-private="{"x" * 40_000}">text</div>')

    with pytest.raises(ValueError, match="bounded"):
        scoped.save(oversized, "item_scope:0")

    assert connection.execute("SELECT count(*) FROM adaptive_features").fetchone()[0] == 0


def test_sql_failure_rolls_back_without_partial_adaptive_feature(
    connection: sqlite3.Connection,
) -> None:
    scoped = namespace(adaptive_store(connection))
    connection.execute(
        "CREATE TRIGGER fail_adaptive BEFORE INSERT ON adaptive_features "
        "BEGIN SELECT RAISE(ABORT, 'private path and secret'); END"
    )

    with pytest.raises(RuntimeError, match="storage failed") as caught:
        scoped.save(element(), "item_scope:0")

    assert "private path" not in str(caught.value)
    assert connection.execute("SELECT count(*) FROM adaptive_features").fetchone()[0] == 0


def test_repeated_save_updates_one_key_with_fresh_ciphertext(
    connection: sqlite3.Connection,
) -> None:
    scoped = namespace(adaptive_store(connection))
    scoped.save(element(), "item_scope:0")
    first = connection.execute("SELECT nonce, ciphertext FROM adaptive_features").fetchone()

    scoped.save(fromstring("<article>updated</article>"), "item_scope:0")

    second = connection.execute("SELECT nonce, ciphertext FROM adaptive_features").fetchone()
    assert connection.execute("SELECT count(*) FROM adaptive_features").fetchone()[0] == 1
    assert bytes(first["nonce"]) != bytes(second["nonce"])
    assert bytes(first["ciphertext"]) != bytes(second["ciphertext"])
    assert scoped.retrieve("item_scope:0")["text"] == "updated"


def test_expired_rows_and_namespace_cleanup_are_bounded_and_scoped(
    connection: sqlite3.Connection,
) -> None:
    now = [datetime(2026, 7, 23, 12, 0, tzinfo=UTC)]
    from personal_monitor.scraping.adaptive_storage import EncryptedAdaptiveStorage

    store = EncryptedAdaptiveStorage(
        connection,
        cipher=AesGcmCipher(b"k" * 32),
        clock=lambda: now[0],
    )
    first = namespace(store)
    second = namespace(store, version="other-version")
    first.save(element(), "item_scope:0")
    first.save(element(), "field:title:0")
    second.save(element(), "item_scope:0")

    assert first.delete("field:title:0") is True
    assert first.delete("field:title:0") is False
    assert first.delete_all() == 1
    assert connection.execute("SELECT count(*) FROM adaptive_features").fetchone()[0] == 1
    now[0] += timedelta(days=91)
    assert second.retrieve("item_scope:0") is None
    assert connection.execute("SELECT count(*) FROM adaptive_features").fetchone()[0] == 0


def test_purge_expired_uses_bound_timestamp_and_rolls_back_on_sql_failure(
    connection: sqlite3.Connection,
) -> None:
    store = adaptive_store(connection)
    namespace(store).save(element(), "item_scope:0")
    connection.execute(
        "CREATE TRIGGER fail_feature_delete BEFORE DELETE ON adaptive_features "
        "BEGIN SELECT RAISE(ABORT, 'private expired feature'); END"
    )

    with pytest.raises(RuntimeError, match="storage failed") as caught:
        store.purge_expired(now=datetime(2027, 1, 1, tzinfo=UTC))

    assert "private expired feature" not in str(caught.value)
    assert connection.execute("SELECT count(*) FROM adaptive_features").fetchone()[0] == 1


def test_authenticated_but_invalid_feature_json_is_revalidated_before_relocation(
    connection: sqlite3.Connection,
) -> None:
    cipher = AesGcmCipher(b"k" * 32)
    store = adaptive_store(connection)
    scoped = namespace(store)
    scoped.save(element(), "item_scope:0")
    aad = authenticated_aad(connection, "item_scope:0")
    crafted = cipher.encrypt(
        b'{"feature":{"tag":"div"},"format":"personal-monitor-adaptive-feature-v1"}',
        aad,
    )
    connection.execute(
        "UPDATE adaptive_features SET nonce = ?, ciphertext = ?",
        (crafted.nonce, crafted.ciphertext),
    )

    with pytest.raises(ValueError, match="payload is invalid"):
        scoped.retrieve("item_scope:0")


def test_authenticated_deep_json_is_rejected_with_a_fixed_payload_error(
    connection: sqlite3.Connection,
) -> None:
    cipher = AesGcmCipher(b"k" * 32)
    scoped = namespace(adaptive_store(connection))
    scoped.save(element(), "item_scope:0")
    aad = authenticated_aad(connection, "item_scope:0")
    crafted = cipher.encrypt((b'{"feature":' + b"[" * 100 + b"]" * 100 + b"}"), aad)
    connection.execute(
        "UPDATE adaptive_features SET nonce = ?, ciphertext = ?",
        (crafted.nonce, crafted.ciphertext),
    )

    with pytest.raises(ValueError, match="payload is invalid") as caught:
        scoped.retrieve("item_scope:0")

    assert "item_scope" not in str(caught.value)


def test_authenticated_noncanonical_feature_cannot_bypass_inner_limits(
    connection: sqlite3.Connection,
) -> None:
    cipher = AesGcmCipher(b"k" * 32)
    scoped = namespace(adaptive_store(connection))
    scoped.save(element(), "item_scope:0")
    aad = authenticated_aad(connection, "item_scope:0")
    payload = {
        "format": "personal-monitor-adaptive-feature-v1",
        "feature": {
            "tag": "div",
            "attributes": {"class": " " * 5_000},
            "text": None,
            "path": ["div"],
        },
    }
    crafted = cipher.encrypt(json.dumps(payload).encode(), aad)
    connection.execute(
        "UPDATE adaptive_features SET nonce = ?, ciphertext = ?",
        (crafted.nonce, crafted.ciphertext),
    )

    with pytest.raises(ValueError, match="payload is invalid"):
        scoped.retrieve("item_scope:0")


@pytest.mark.parametrize(
    "payload",
    [
        b'{"format":"x","format":"y","feature":{}}',
        (
            b'{"format":"personal-monitor-adaptive-feature-v1","feature":'
            b'{"tag":"div","attributes":{},"text":NaN,"path":["div"]}}'
        ),
        b"\xff\xfe",
        b'{"format":"personal-monitor-adaptive-feature-v1","feature":{}} trailing',
    ],
)
def test_authenticated_malformed_json_variants_have_one_safe_payload_error(
    connection: sqlite3.Connection,
    payload: bytes,
) -> None:
    cipher = AesGcmCipher(b"k" * 32)
    scoped = namespace(adaptive_store(connection))
    scoped.save(element(), "item_scope:0")
    aad = authenticated_aad(connection, "item_scope:0")
    crafted = cipher.encrypt(payload, aad)
    connection.execute(
        "UPDATE adaptive_features SET nonce = ?, ciphertext = ?",
        (crafted.nonce, crafted.ciphertext),
    )

    with pytest.raises(ValueError, match="payload is invalid") as caught:
        scoped.retrieve("item_scope:0")

    assert "item_scope" not in str(caught.value)


def test_store_composition_and_namespace_are_sealed_before_any_sql(
    connection: sqlite3.Connection,
) -> None:
    store = adaptive_store(connection)
    scoped = namespace(store)
    with pytest.raises(AttributeError):
        scoped._namespace = b"swapped"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        store._clock = lambda: datetime.now(UTC)  # type: ignore[misc]

    object.__setattr__(scoped, "_namespace", b"low-level-swap")
    statements: list[str] = []
    connection.set_trace_callback(statements.append)
    with pytest.raises(RuntimeError, match="integrity"):
        scoped.save(element(), "item_scope:0")

    assert not any("adaptive_features" in statement for statement in statements)
    assert connection.execute("SELECT count(*) FROM adaptive_features").fetchone()[0] == 0


def test_coordinated_low_level_store_and_namespace_snapshot_replacement_is_rejected(
    connection: sqlite3.Connection,
) -> None:
    store = adaptive_store(connection)
    replacement_cipher = AesGcmCipher(b"z" * 32)
    original_clock = store._clock
    object.__setattr__(store, "_cipher", replacement_cipher)
    object.__setattr__(
        store,
        "_composition",
        (connection, replacement_cipher, original_clock),
    )
    statements: list[str] = []
    connection.set_trace_callback(statements.append)
    with pytest.raises(RuntimeError, match="integrity"):
        namespace(store)
    assert not any("adaptive_features" in statement for statement in statements)

    clean_store = adaptive_store(connection)
    scoped = namespace(clean_store)
    swapped = b"other\0monitor\0version"
    swapped_hash = __import__("hashlib").sha256(swapped).hexdigest()
    object.__setattr__(scoped, "_namespace", swapped)
    object.__setattr__(scoped, "_namespace_hash", swapped_hash)
    object.__setattr__(
        scoped,
        "_composition",
        (connection, clean_store._cipher, clean_store._clock, swapped, swapped_hash),
    )
    with pytest.raises(RuntimeError, match="integrity"):
        scoped.save(element(), "item_scope:0")
    assert connection.execute("SELECT count(*) FROM adaptive_features").fetchone()[0] == 0


def test_authenticated_semantically_valid_noncanonical_json_is_rejected(
    connection: sqlite3.Connection,
) -> None:
    cipher = AesGcmCipher(b"k" * 32)
    scoped = namespace(adaptive_store(connection))
    scoped.save(element(), "item_scope:0")
    restored = scoped.retrieve("item_scope:0")
    assert restored is not None
    aad = authenticated_aad(connection, "item_scope:0")
    noncanonical = (
        json.dumps(
            {"feature": restored, "format": "personal-monitor-adaptive-feature-v1"},
            indent=2,
        ).encode()
        + b"\n"
    )
    crafted = cipher.encrypt(noncanonical, aad)
    connection.execute(
        "UPDATE adaptive_features SET nonce = ?, ciphertext = ?",
        (crafted.nonce, crafted.ciphertext),
    )
    with pytest.raises(ValueError, match="payload is invalid"):
        scoped.retrieve("item_scope:0")


@pytest.mark.parametrize(
    "replacement",
    [
        "2027-01-01T00:00:00+00:00",
        "2026-07-23T11:59:59+00:00",
        "not-a-time",
    ],
)
def test_expiry_metadata_is_authenticated_before_it_affects_usability(
    connection: sqlite3.Connection,
    replacement: str,
) -> None:
    scoped = namespace(adaptive_store(connection))
    scoped.save(element(), "item_scope:0")
    connection.execute("UPDATE adaptive_features SET expires_at = ?", (replacement,))

    with pytest.raises(ValueError, match="authentication"):
        scoped.retrieve("item_scope:0")

    assert connection.execute("SELECT count(*) FROM adaptive_features").fetchone()[0] == 1


def test_concurrent_connections_upsert_one_authenticated_feature(tmp_path: Path) -> None:
    database_path = tmp_path / "concurrent-adaptive.db"
    initial = open_database(database_path)
    initial.close()

    def save_and_read(index: int) -> str:
        connection = open_database(database_path)
        try:
            scoped = namespace(adaptive_store(connection))
            scoped.save(fromstring(f"<article>value-{index}</article>"), "item_scope:0")
            restored = scoped.retrieve("item_scope:0")
            assert restored is not None
            return str(restored["text"])
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=4) as executor:
        values = list(executor.map(save_and_read, range(12)))

    final = open_database(database_path)
    try:
        assert len(values) == 12
        assert final.execute("SELECT count(*) FROM adaptive_features").fetchone()[0] == 1
        restored = namespace(adaptive_store(final)).retrieve("item_scope:0")
        assert restored is not None
        assert str(restored["text"]).startswith("value-")
    finally:
        final.close()


@pytest.mark.parametrize(
    ("clock", "error"),
    [
        (lambda: datetime(2026, 7, 23), ValueError),
        (lambda: "not-a-time", TypeError),
    ],
)
def test_storage_construction_requires_an_aware_clock(
    connection: sqlite3.Connection,
    clock,
    error: type[Exception],
) -> None:
    from personal_monitor.scraping.adaptive_storage import EncryptedAdaptiveStorage

    with pytest.raises(error):
        EncryptedAdaptiveStorage(
            connection,
            cipher=AesGcmCipher(b"k" * 32),
            clock=clock,
        )


def test_storage_rejects_an_unconfigured_sqlite_connection() -> None:
    from personal_monitor.scraping.adaptive_storage import EncryptedAdaptiveStorage

    raw = sqlite3.connect(":memory:")
    try:
        raw.execute(
            "CREATE TABLE adaptive_features("
            "key_hash TEXT, namespace_hash TEXT, nonce BLOB, ciphertext BLOB, "
            "created_at TEXT, updated_at TEXT, expires_at TEXT)"
        )
        with pytest.raises(RuntimeError, match="unavailable"):
            EncryptedAdaptiveStorage(
                raw,
                cipher=AesGcmCipher(b"k" * 32),
                clock=lambda: datetime(2026, 7, 23, tzinfo=UTC),
            )
    finally:
        raw.close()
