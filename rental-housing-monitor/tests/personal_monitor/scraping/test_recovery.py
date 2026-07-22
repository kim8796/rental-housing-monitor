from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path

import pytest
from scrapling.core.storage import SQLiteStorageSystem

from personal_monitor.domain.spec import FetchStrategy, MonitorSpec, MonitorStatus
from personal_monitor.engine.errors import ErrorClass, MonitorError
from personal_monitor.scraping.document import SourceDocument
from personal_monitor.scraping.extractor import DeclarativeExtractor
from personal_monitor.scraping.validator import ObservationValidator
from personal_monitor.security.encryption import AesGcmCipher, EncryptedBlob
from personal_monitor.security.sanitize import sanitize_for_ai
from personal_monitor.storage import RecoveryRepository, RegistryRepository, open_database

pytestmark = [
    pytest.mark.filterwarnings("ignore:The 'strip_cdata' option.*:DeprecationWarning"),
    pytest.mark.filterwarnings(
        r"ignore:codecs.open\(\) is deprecated.*:DeprecationWarning:tld.utils"
    ),
]


class IsolatedSQLiteStorage(SQLiteStorageSystem.__wrapped__):
    def __del__(self) -> None:
        """The 0.4.11 base deallocator double-closes after an explicit fixture close."""


def make_spec(
    *,
    selector_kind: str = "css",
    max_items: int = 1,
    source_adapter: str = "scrapling",
) -> MonitorSpec:
    if selector_kind == "xpath":
        item_scope = "//main[@id='catalog']"
        title_selector = ".//span[@data-role='title']"
        price_selector = ".//span[@data-role='price']"
    else:
        item_scope = "main.catalog"
        title_selector = ".title"
        price_selector = ".price"
    payload: dict[str, object] = {
        "schema_version": 1,
        "owner_id": "owner",
        "name": "가격 감시",
        "target_url": "https://example.com/catalog",
        "source_adapter": source_adapter,
        "schedule": "0 */6 * * *",
        "timezone": "Asia/Seoul",
        "extract": {
            "item_scope": item_scope,
            "fields": {
                "title": {"selector": title_selector, "type": "text"},
                "price": {"selector": price_selector, "type": "krw"},
            },
        },
        "validators": {"min_items": 1, "max_items": max_items},
        "rules": [{"kind": "new_item"}],
    }
    if source_adapter == "official_api":
        payload["adapter_ref"] = "official_catalog"
    return MonitorSpec.model_validate(payload)


def document(body: bytes, *, content_type: str = "text/html") -> SourceDocument:
    return SourceDocument(
        final_url="https://example.com/catalog",
        status=200,
        content_type=content_type,
        headers={"content-type": content_type},
        body=body,
        strategy=FetchStrategy.HTTP,
    )


def baseline_html(selector_kind: str = "css") -> bytes:
    if selector_kind == "xpath":
        return (
            b'<main id="catalog"><article><span data-role="title">Keyboard</span>'
            b'<span data-role="price">1000</span></article></main>'
        )
    return (
        b'<main class="catalog"><article><span class="title">Keyboard</span>'
        b'<span class="price">1000</span></article></main>'
    )


def changed_html(selector_kind: str = "css", *, price: bytes = b"900") -> bytes:
    if selector_kind == "xpath":
        return (
            b'<section><div id="catalog-new"><article><h2 data-role="heading">Keyboard</h2>'
            b'<strong data-role="amount">' + price + b"</strong></article></div></section>"
        )
    return (
        b'<section><div class="catalog-v2"><article><h2 class="heading">Keyboard</h2>'
        b'<strong class="amount">' + price + b"</strong></article></div></section>"
    )


@pytest.fixture
def connection() -> Iterator[sqlite3.Connection]:
    value = open_database(":memory:")
    yield value
    value.close()


@pytest.fixture
def adaptive_storage(tmp_path: Path):
    storage = IsolatedSQLiteStorage(
        str(tmp_path / "isolated-adaptive.db"), url="https://example.com"
    )
    yield storage
    storage.close()


def build_recovery(
    connection: sqlite3.Connection,
    adaptive_storage: object,
    *,
    spec: MonitorSpec | None = None,
):
    from personal_monitor.scraping.recovery import AdaptiveRecovery

    registry = RegistryRepository(connection)
    registry.create_user("owner", 1)
    registry.create_user("other", 2)
    monitor_spec = spec or make_spec()
    monitor_id = registry.create_monitor(monitor_spec, created_by="owner")
    repository = RecoveryRepository(
        connection, clock=lambda: datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
    )
    cipher = AesGcmCipher(b"k" * 32)
    recovery = AdaptiveRecovery(
        registry=registry,
        repository=repository,
        cipher=cipher,
        adaptive_storage=adaptive_storage,
        extractor=DeclarativeExtractor(),
        validator=ObservationValidator(),
    )
    return recovery, registry, repository, cipher, monitor_id


@pytest.mark.parametrize("selector_kind", ["css", "xpath"])
def test_real_scrapling_baseline_and_relocation_share_injected_storage_and_namespaces(
    connection: sqlite3.Connection,
    adaptive_storage,
    selector_kind: str,
) -> None:
    recovery, registry, _, _, monitor_id = build_recovery(
        connection, adaptive_storage, spec=make_spec(selector_kind=selector_kind)
    )
    active_version_id = registry.get_active_monitor(monitor_id).version_id

    recovery.save_success_baseline(
        monitor_id, owner_id="owner", document=document(baseline_html(selector_kind))
    )
    candidate = recovery.propose_adaptive(
        monitor_id,
        owner_id="owner",
        document=document(changed_html(selector_kind)),
        failure_class=ErrorClass.STRUCTURE,
    )

    assert candidate is not None
    assert candidate.validation_passed is True
    rows = adaptive_storage.connection.execute(
        "SELECT identifier FROM storage ORDER BY identifier"
    ).fetchall()
    assert [row[0] for row in rows] == [
        f"pm:{monitor_id}:{active_version_id}:field:price",
        f"pm:{monitor_id}:{active_version_id}:field:title",
        f"pm:{monitor_id}:{active_version_id}:item_scope",
    ]
    stored = connection.execute(
        "SELECT spec_json, approved_at FROM monitor_versions WHERE id = ?",
        (candidate.version_id,),
    ).fetchone()
    proposed = MonitorSpec.model_validate_json(stored["spec_json"])
    extracted = DeclarativeExtractor().extract(
        document(changed_html(selector_kind)), proposed.extract
    )
    validated = ObservationValidator().validate(extracted, proposed.extract, proposed.validators)
    assert validated[0].fields == {"title": "Keyboard", "price": 900}
    assert stored["approved_at"] is None
    assert registry.get_active_monitor(monitor_id).version_id == active_version_id
    assert registry.list_monitors("owner")[0].status is MonitorStatus.NEEDS_REVIEW
    assert set(candidate.field_changes) == {"item_scope", "field:title", "field:price"}


def test_generated_relocated_selectors_must_match_exactly_one_element(
    connection: sqlite3.Connection,
    adaptive_storage,
) -> None:
    recovery, _, _, _, monitor_id = build_recovery(connection, adaptive_storage)
    recovery.save_success_baseline(monitor_id, owner_id="owner", document=document(baseline_html()))
    ambiguous = document(
        b'<main class="catalog"><article><span class="title">Keyboard</span>'
        b'<strong class="amount">900</strong><strong class="amount">900</strong>'
        b"</article></main>"
    )

    candidate = recovery.propose_adaptive(
        monitor_id,
        owner_id="owner",
        document=ambiguous,
        failure_class=ErrorClass.STRUCTURE,
    )

    assert candidate is None
    assert connection.execute("SELECT count(*) FROM monitor_versions").fetchone()[0] == 1
    assert connection.execute("SELECT count(*) FROM diagnostic_snapshots").fetchone()[0] == 1


def test_scoped_selector_generation_rejects_a_mismatched_root_prefix() -> None:
    from scrapling import Selector

    from personal_monitor.scraping.recovery import _generated_scoped_selector

    root = Selector(b'<main><span id="same-id">inside</span></main>').css("main")[0]
    outside = Selector(b'<section><span id="same-id">outside</span></section>').css("#same-id")[0]

    with pytest.raises(ValueError, match="outside"):
        _generated_scoped_selector(root, outside, xpath=False)


def test_unexpected_adaptive_storage_error_still_stores_a_redacted_diagnostic(
    connection: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    class ExplodingStorage(IsolatedSQLiteStorage):
        explode = False

        def retrieve(self, identifier: str):
            if self.explode:
                raise RuntimeError("private-source-fragment /private/storage/path")
            return super().retrieve(identifier)

    storage = ExplodingStorage(str(tmp_path / "exploding.db"), url="https://example.com")
    try:
        recovery, _, _, _, monitor_id = build_recovery(connection, storage)
        recovery.save_success_baseline(
            monitor_id, owner_id="owner", document=document(baseline_html())
        )
        storage.explode = True

        candidate = recovery.propose_adaptive(
            monitor_id,
            owner_id="owner",
            document=document(changed_html()),
            failure_class=ErrorClass.STRUCTURE,
        )

        assert candidate is None
        assert connection.execute("SELECT count(*) FROM monitor_versions").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM diagnostic_snapshots").fetchone()[0] == 1
    finally:
        storage.close()


def test_generated_selector_containing_a_supplied_secret_fails_closed(
    connection: sqlite3.Connection,
    adaptive_storage,
) -> None:
    recovery, _, _, _, monitor_id = build_recovery(connection, adaptive_storage)
    recovery.save_success_baseline(monitor_id, owner_id="owner", document=document(baseline_html()))
    changed = document(
        b'<main class="catalog"><article><h2 class="heading">Keyboard</h2>'
        b'<strong id="credential-secret" class="amount">900</strong></article></main>'
    )

    candidate = recovery.propose_adaptive(
        monitor_id,
        owner_id="owner",
        document=changed,
        failure_class=ErrorClass.STRUCTURE,
        secret_values={"credential-secret"},
    )

    assert candidate is None
    assert connection.execute("SELECT count(*) FROM monitor_versions").fetchone()[0] == 1
    assert connection.execute("SELECT count(*) FROM diagnostic_snapshots").fetchone()[0] == 1


@pytest.mark.parametrize(
    ("title", "secrets"),
    [
        (b"credential-secret", {"credential-secret"}),
        (b"https://user:password@example.com/private", set()),
        (b"https://example.com/private?token=credential", set()),
    ],
)
def test_unsafe_preview_value_fails_closed_instead_of_returning_a_candidate(
    connection: sqlite3.Connection,
    adaptive_storage,
    title: bytes,
    secrets: set[str],
) -> None:
    recovery, _, _, _, monitor_id = build_recovery(connection, adaptive_storage)
    recovery.save_success_baseline(monitor_id, owner_id="owner", document=document(baseline_html()))
    changed = document(
        b'<section><div class="catalog-v2"><article><h2 class="heading">'
        + title
        + b'</h2><strong class="amount">900</strong></article></div></section>'
    )

    candidate = recovery.propose_adaptive(
        monitor_id,
        owner_id="owner",
        document=changed,
        failure_class=ErrorClass.STRUCTURE,
        secret_values=secrets,
    )

    assert candidate is None
    assert connection.execute("SELECT count(*) FROM monitor_versions").fetchone()[0] == 1
    assert connection.execute("SELECT count(*) FROM diagnostic_snapshots").fetchone()[0] == 1


@pytest.mark.parametrize("failure_mode", ["normalization", "item_range"])
def test_failed_full_extraction_or_independent_validation_stores_only_the_diagnostic(
    connection: sqlite3.Connection,
    adaptive_storage,
    failure_mode: str,
) -> None:
    max_items = 1
    recovery, _, _, cipher, monitor_id = build_recovery(
        connection, adaptive_storage, spec=make_spec(max_items=max_items)
    )
    recovery.save_success_baseline(monitor_id, owner_id="owner", document=document(baseline_html()))
    if failure_mode == "normalization":
        failed = document(changed_html(price=b"not-a-price"))
    else:
        failed = document(
            b'<main class="catalog"><article><h2 class="heading">A</h2>'
            b'<strong class="amount">900</strong></article></main>'
            b'<main class="catalog"><article><h2 class="heading">B</h2>'
            b'<strong class="amount">800</strong></article></main>'
        )

    candidate = recovery.propose_adaptive(
        monitor_id,
        owner_id="owner",
        document=failed,
        failure_class=ErrorClass.VALIDATION,
        secret_values={"not-a-price"},
    )

    assert candidate is None
    assert connection.execute("SELECT count(*) FROM monitor_versions").fetchone()[0] == 1
    row = connection.execute("SELECT nonce, ciphertext FROM diagnostic_snapshots").fetchone()
    plaintext = cipher.decrypt(
        EncryptedBlob(nonce=row["nonce"], ciphertext=row["ciphertext"]),
        monitor_id.encode(),
    )
    assert b"not-a-price" not in plaintext
    assert (
        plaintext == sanitize_for_ai(failed.body.decode(), secret_values={"not-a-price"}).encode()
    )
    assert failed.body not in bytes(row["ciphertext"])


def test_baseline_runs_full_extraction_and_validation_before_saving_features(
    connection: sqlite3.Connection,
    adaptive_storage,
) -> None:
    recovery, _, _, _, monitor_id = build_recovery(connection, adaptive_storage)

    with pytest.raises(MonitorError):
        recovery.save_success_baseline(
            monitor_id,
            owner_id="owner",
            document=document(b'<main class="catalog"><span class="price">invalid</span></main>'),
        )

    assert adaptive_storage.connection.execute("SELECT count(*) FROM storage").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("failure_class", "content_type"),
    [
        (ErrorClass.POLICY, "text/html"),
        (ErrorClass.AUTHENTICATION, "text/html"),
        (ErrorClass.TRANSIENT_NETWORK, "text/html"),
        (ErrorClass.STRUCTURE, "application/json"),
    ],
)
def test_non_adaptive_failure_inputs_are_rejected_without_a_snapshot(
    connection: sqlite3.Connection,
    adaptive_storage,
    failure_class: ErrorClass,
    content_type: str,
) -> None:
    recovery, _, _, _, monitor_id = build_recovery(connection, adaptive_storage)

    with pytest.raises(ValueError, match="eligible"):
        recovery.propose_adaptive(
            monitor_id,
            owner_id="owner",
            document=document(b"{}", content_type=content_type),
            failure_class=failure_class,
        )

    assert connection.execute("SELECT count(*) FROM diagnostic_snapshots").fetchone()[0] == 0


def test_official_adapter_and_wrong_owner_cannot_enter_adaptive_recovery(
    connection: sqlite3.Connection,
    adaptive_storage,
) -> None:
    recovery, _, _, _, monitor_id = build_recovery(
        connection, adaptive_storage, spec=make_spec(source_adapter="official_api")
    )

    for owner_id in ("owner", "other"):
        with pytest.raises(ValueError):
            recovery.propose_adaptive(
                monitor_id,
                owner_id=owner_id,
                document=document(changed_html()),
                failure_class=ErrorClass.STRUCTURE,
            )

    assert connection.execute("SELECT count(*) FROM diagnostic_snapshots").fetchone()[0] == 0


def test_recovery_candidate_freezes_caps_and_redacts_nested_values() -> None:
    from personal_monitor.scraping.recovery import RecoveryCandidate

    candidate = RecoveryCandidate(
        version_id="secret-version-id",
        validation_passed=True,
        field_changes={f"field-{index}": "x" * 700 for index in range(30)},
        preview_items=[
            {
                **{f"field_{index}": "v" * 300 for index in range(12)},
                "password": "credential-secret",
                "token": "token-secret",
            }
            for _ in range(9)
        ],
    )

    assert len(candidate.field_changes) == 20
    assert max(map(len, candidate.field_changes.values())) == 500
    assert len(candidate.preview_items) == 5
    assert all(len(item) == 8 for item in candidate.preview_items)
    assert all(len(value) <= 160 for item in candidate.preview_items for value in item.values())
    assert all("password" not in item and "token" not in item for item in candidate.preview_items)
    with pytest.raises(TypeError):
        candidate.field_changes["new"] = "value"  # type: ignore[index]
    with pytest.raises(TypeError):
        candidate.preview_items[0]["new"] = "value"  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        candidate.version_id = "other"  # type: ignore[misc]
    representation = repr(candidate)
    assert "secret-version-id" not in representation
    assert "credential-secret" not in representation
    assert "token-secret" not in representation
    assert "x" * 100 not in representation


def test_recovery_requires_an_explicit_scrapling_storage_dependency(
    connection: sqlite3.Connection,
) -> None:
    from personal_monitor.scraping.recovery import AdaptiveRecovery

    registry = RegistryRepository(connection)
    repository = RecoveryRepository(connection)
    with pytest.raises(TypeError):
        AdaptiveRecovery(  # type: ignore[call-arg]
            registry=registry,
            repository=repository,
            cipher=AesGcmCipher(b"k" * 32),
            extractor=DeclarativeExtractor(),
            validator=ObservationValidator(),
        )


def test_recovery_does_not_touch_scrapling_global_site_packages_database(
    connection: sqlite3.Connection,
    adaptive_storage,
) -> None:
    import scrapling.parser as parser_module

    global_path = Path(parser_module.__DEFAULT_DB_FILE__)
    before = global_path.stat() if global_path.exists() else None
    recovery, _, _, _, monitor_id = build_recovery(connection, adaptive_storage)
    recovery.save_success_baseline(monitor_id, owner_id="owner", document=document(baseline_html()))
    recovery.propose_adaptive(
        monitor_id,
        owner_id="owner",
        document=document(changed_html()),
        failure_class=ErrorClass.STRUCTURE,
    )

    after = global_path.stat() if global_path.exists() else None
    assert before == after
    assert Path(adaptive_storage.storage_file) != global_path
