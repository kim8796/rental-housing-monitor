from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from collections.abc import Iterator
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote_plus

import pytest

from personal_monitor.domain.spec import FetchStrategy, MonitorSpec, MonitorStatus
from personal_monitor.engine.errors import ErrorClass, MonitorError
from personal_monitor.scraping.adaptive_storage import EncryptedAdaptiveStorage
from personal_monitor.scraping.document import SourceDocument
from personal_monitor.scraping.extractor import DeclarativeExtractor
from personal_monitor.scraping.validator import ObservationValidator
from personal_monitor.security.encryption import AesGcmCipher, EncryptedBlob
from personal_monitor.security.sanitize import sanitize_for_ai
from personal_monitor.security.url_policy import UrlPolicy
from personal_monitor.storage import RecoveryRepository, RegistryRepository, open_database
from tests.credential_alias_cases import (
    BENIGN_COMPOUND_FIELD_NAMES,
    SENSITIVE_COMPOUND_FIELD_NAMES,
    SENSITIVE_KEY_VARIANTS,
)

pytestmark = [
    pytest.mark.filterwarnings("ignore:The 'strip_cdata' option.*:DeprecationWarning"),
    pytest.mark.filterwarnings(
        r"ignore:codecs.open\(\) is deprecated.*:DeprecationWarning:tld.utils"
    ),
]


class Resolver:
    def __init__(self, *, error: BaseException | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, int]] = []

    async def resolve(self, hostname: str, port: int) -> tuple[str, ...]:
        self.calls.append((hostname, port))
        if self.error is not None:
            raise self.error
        return ("93.184.216.34",)


def make_spec(
    *,
    selector_kind: str = "css",
    min_items: int = 1,
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
        "validators": {"min_items": min_items, "max_items": max_items},
        "rules": [{"kind": "new_item"}],
    }
    if source_adapter == "official_api":
        payload["adapter_ref"] = "official_catalog"
    return MonitorSpec.model_validate(payload)


def document(
    body: bytes,
    *,
    content_type: str = "text/html",
    final_url: str = "https://example.com/catalog",
    redirect_urls: tuple[str, ...] = (),
) -> SourceDocument:
    return SourceDocument(
        final_url=final_url,
        status=200,
        content_type=content_type,
        headers={"content-type": content_type},
        body=body,
        strategy=FetchStrategy.HTTP,
        redirect_urls=redirect_urls,
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


def multi_baseline_html(selector_kind: str) -> bytes:
    if selector_kind == "xpath":
        return (
            b'<main id="catalog"><span data-role="title">Keyboard</span>'
            b'<span data-role="price">1000</span></main>'
            b'<main id="catalog"><span data-role="title">Mouse</span>'
            b'<span data-role="price">2000</span></main>'
        )
    return (
        b'<main class="catalog"><span class="title">Keyboard</span>'
        b'<span class="price">1000</span></main>'
        b'<main class="catalog"><span class="title">Mouse</span>'
        b'<span class="price">2000</span></main>'
    )


def multi_changed_html(selector_kind: str) -> bytes:
    if selector_kind == "xpath":
        return (
            b'<aside data-role="unrelated"><h2 data-role="heading">Decoy</h2>'
            b'<strong data-role="amount">9999</strong></aside>'
            b'<div data-role="catalog-new"><h2 data-role="heading">Keyboard</h2>'
            b'<strong data-role="amount">900</strong></div>'
            b'<div data-role="catalog-new"><h2 data-role="heading">Mouse</h2>'
            b'<strong data-role="amount">1800</strong></div>'
        )
    return (
        b'<aside class="unrelated"><h2 class="heading">Decoy</h2>'
        b'<strong class="amount">9999</strong></aside>'
        b'<div class="catalog-v2"><h2 class="heading">Keyboard</h2>'
        b'<strong class="amount">900</strong></div>'
        b'<div class="catalog-v2"><h2 class="heading">Mouse</h2>'
        b'<strong class="amount">1800</strong></div>'
    )


@pytest.fixture
def connection() -> Iterator[sqlite3.Connection]:
    value = open_database(":memory:")
    yield value
    value.close()


@pytest.fixture
def adaptive_storage(connection: sqlite3.Connection) -> EncryptedAdaptiveStorage:
    return EncryptedAdaptiveStorage(
        connection,
        cipher=AesGcmCipher(b"a" * 32),
        clock=lambda: datetime(2026, 7, 23, 12, 0, tzinfo=UTC),
    )


def build_recovery(
    connection: sqlite3.Connection,
    adaptive_storage: object,
    *,
    spec: MonitorSpec | None = None,
    resolver: Resolver | None = None,
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
        url_policy=UrlPolicy(resolver or Resolver()),
        extractor=DeclarativeExtractor(),
        validator=ObservationValidator(),
    )
    return recovery, registry, repository, cipher, monitor_id


def save_baseline(recovery: object, *args: object, **kwargs: object) -> None:
    asyncio.run(recovery.save_success_baseline(*args, **kwargs))  # type: ignore[attr-defined]


def propose(recovery: object, *args: object, **kwargs: object):
    return asyncio.run(recovery.propose_adaptive(*args, **kwargs))  # type: ignore[attr-defined]


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

    save_baseline(
        recovery, monitor_id, owner_id="owner", document=document(baseline_html(selector_kind))
    )
    candidate = propose(
        recovery,
        monitor_id,
        owner_id="owner",
        document=document(changed_html(selector_kind)),
        failure_class=ErrorClass.STRUCTURE,
    )

    assert candidate is not None
    assert candidate.validation_passed is True
    assert connection.execute("SELECT count(*) FROM adaptive_features").fetchone()[0] == 3
    serialized_rows = b"".join(
        bytes(value) if isinstance(value, bytes) else str(value).encode()
        for row in connection.execute("SELECT * FROM adaptive_features")
        for value in row
    )
    assert monitor_id.encode() not in serialized_rows
    assert active_version_id.encode() not in serialized_rows
    assert b"Keyboard" not in serialized_rows
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
    save_baseline(recovery, monitor_id, owner_id="owner", document=document(baseline_html()))
    ambiguous = document(
        b'<main class="catalog"><article><span class="title">Keyboard</span>'
        b'<strong class="amount">900</strong><strong class="amount">900</strong>'
        b"</article></main>"
    )

    candidate = propose(
        recovery,
        monitor_id,
        owner_id="owner",
        document=ambiguous,
        failure_class=ErrorClass.STRUCTURE,
    )

    assert candidate is None
    assert connection.execute("SELECT count(*) FROM monitor_versions").fetchone()[0] == 1
    assert connection.execute("SELECT count(*) FROM diagnostic_snapshots").fetchone()[0] == 1


@pytest.mark.parametrize("selector_kind", ["css", "xpath"])
def test_multi_row_relocation_generalizes_all_exemplars_and_excludes_decoys(
    connection: sqlite3.Connection,
    adaptive_storage: EncryptedAdaptiveStorage,
    selector_kind: str,
) -> None:
    recovery, _, _, _, monitor_id = build_recovery(
        connection,
        adaptive_storage,
        spec=make_spec(selector_kind=selector_kind, min_items=2, max_items=2),
    )
    save_baseline(
        recovery,
        monitor_id,
        owner_id="owner",
        document=document(multi_baseline_html(selector_kind)),
    )

    candidate = propose(
        recovery,
        monitor_id,
        owner_id="owner",
        document=document(multi_changed_html(selector_kind)),
        failure_class=ErrorClass.STRUCTURE,
    )

    assert candidate is not None
    stored = connection.execute(
        "SELECT spec_json FROM monitor_versions WHERE id = ?", (candidate.version_id,)
    ).fetchone()
    proposed = MonitorSpec.model_validate_json(stored["spec_json"])
    extracted = DeclarativeExtractor().extract(
        document(multi_changed_html(selector_kind)), proposed.extract
    )
    validated = ObservationValidator().validate(extracted, proposed.extract, proposed.validators)
    assert [item.fields for item in validated] == [
        {"title": "Keyboard", "price": 900},
        {"title": "Mouse", "price": 1800},
    ]
    assert "Decoy" not in repr(validated)
    assert connection.execute("SELECT count(*) FROM adaptive_features").fetchone()[0] == 6


def test_scoped_selector_generation_rejects_a_mismatched_root_prefix() -> None:
    from scrapling import Selector

    from personal_monitor.scraping.recovery import _generated_scoped_selector

    root = Selector(b'<main><span id="same-id">inside</span></main>').css("main")[0]
    outside = Selector(b'<section><span id="same-id">outside</span></section>').css("#same-id")[0]

    with pytest.raises(ValueError, match="outside"):
        _generated_scoped_selector(root, outside, xpath=False)


def test_unexpected_adaptive_storage_error_still_stores_a_redacted_diagnostic(
    connection: sqlite3.Connection,
    adaptive_storage: EncryptedAdaptiveStorage,
) -> None:
    recovery, _, _, _, monitor_id = build_recovery(connection, adaptive_storage)
    save_baseline(recovery, monitor_id, owner_id="owner", document=document(baseline_html()))
    row = connection.execute(
        "SELECT key_hash, ciphertext FROM adaptive_features LIMIT 1"
    ).fetchone()
    ciphertext = bytes(row["ciphertext"])
    connection.execute(
        "UPDATE adaptive_features SET ciphertext = ? WHERE key_hash = ?",
        (ciphertext[:-1] + bytes([ciphertext[-1] ^ 1]), row["key_hash"]),
    )

    candidate = propose(
        recovery,
        monitor_id,
        owner_id="owner",
        document=document(changed_html()),
        failure_class=ErrorClass.STRUCTURE,
    )

    assert candidate is None
    assert connection.execute("SELECT count(*) FROM monitor_versions").fetchone()[0] == 1
    assert connection.execute("SELECT count(*) FROM diagnostic_snapshots").fetchone()[0] == 1


def test_generated_selector_containing_a_supplied_secret_fails_closed(
    connection: sqlite3.Connection,
    adaptive_storage,
) -> None:
    recovery, _, _, _, monitor_id = build_recovery(connection, adaptive_storage)
    save_baseline(recovery, monitor_id, owner_id="owner", document=document(baseline_html()))
    changed = document(
        b'<main class="catalog"><article><h2 class="heading">Keyboard</h2>'
        b'<strong id="credential-secret" class="amount">900</strong></article></main>'
    )

    candidate = propose(
        recovery,
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
    save_baseline(recovery, monitor_id, owner_id="owner", document=document(baseline_html()))
    changed = document(
        b'<section><div class="catalog-v2"><article><h2 class="heading">'
        + title
        + b'</h2><strong class="amount">900</strong></article></div></section>'
    )

    candidate = propose(
        recovery,
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
    save_baseline(recovery, monitor_id, owner_id="owner", document=document(baseline_html()))
    if failure_mode == "normalization":
        failed = document(changed_html(price=b"not-a-price"))
    else:
        failed = document(
            b'<main class="catalog"><article><h2 class="heading">A</h2>'
            b'<strong class="amount">900</strong></article></main>'
            b'<main class="catalog"><article><h2 class="heading">B</h2>'
            b'<strong class="amount">800</strong></article></main>'
        )

    candidate = propose(
        recovery,
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
        save_baseline(
            recovery,
            monitor_id,
            owner_id="owner",
            document=document(b'<main class="catalog"><span class="price">invalid</span></main>'),
        )

    assert connection.execute("SELECT count(*) FROM adaptive_features").fetchone()[0] == 0


def test_baseline_feature_batch_rolls_back_when_a_later_insert_fails(
    connection: sqlite3.Connection,
    adaptive_storage: EncryptedAdaptiveStorage,
) -> None:
    recovery, registry, _, _, monitor_id = build_recovery(connection, adaptive_storage)
    active = registry.get_active_monitor(monitor_id)
    base_aad = (
        b"personal-monitor/adaptive-feature/v1\0"
        + f"owner\0{monitor_id}\0{active.version_id}\0field:title:0".encode()
    )
    failing_hash = hashlib.sha256(base_aad).hexdigest()
    connection.execute(
        "CREATE TRIGGER fail_second_feature BEFORE INSERT ON adaptive_features "
        f"WHEN NEW.key_hash = '{failing_hash}' "
        "BEGIN SELECT RAISE(ABORT, 'private later feature'); END"
    )

    with pytest.raises(RuntimeError, match="storage failed"):
        save_baseline(
            recovery,
            monitor_id,
            owner_id="owner",
            document=document(baseline_html()),
        )

    assert connection.execute("SELECT count(*) FROM adaptive_features").fetchone()[0] == 0
    assert registry.list_monitors("owner")[0].status is MonitorStatus.ACTIVE


@pytest.mark.parametrize("operation", ["baseline", "diagnostic"])
@pytest.mark.parametrize("mutation", ["status", "version"])
def test_recovery_rechecks_stale_monitor_state_after_policy_await(
    tmp_path: Path,
    operation: str,
    mutation: str,
) -> None:
    database_path = tmp_path / f"race-{operation}-{mutation}.db"
    primary = open_database(database_path)
    secondary = open_database(database_path)

    async def scenario() -> None:
        started = asyncio.Event()
        resume = asyncio.Event()

        class PausingResolver(Resolver):
            paused = False

            async def resolve(self, hostname: str, port: int) -> tuple[str, ...]:
                if not self.paused:
                    self.paused = True
                    started.set()
                    await resume.wait()
                return await super().resolve(hostname, port)

        storage = EncryptedAdaptiveStorage(
            primary,
            cipher=AesGcmCipher(b"a" * 32),
            clock=lambda: datetime(2026, 7, 23, tzinfo=UTC),
        )
        recovery, registry, _, _, monitor_id = build_recovery(
            primary, storage, resolver=PausingResolver()
        )
        active = registry.get_active_monitor(monitor_id)
        if operation == "baseline":
            coroutine = recovery.save_success_baseline(
                monitor_id,
                owner_id="owner",
                document=document(baseline_html()),
            )
        else:
            coroutine = recovery.propose_adaptive(
                monitor_id,
                owner_id="owner",
                document=document(baseline_html()),
                failure_class=ErrorClass.STRUCTURE,
            )
        task = asyncio.create_task(coroutine)
        await started.wait()
        if mutation == "status":
            secondary.execute(
                "UPDATE monitors SET status = 'paused_user' WHERE id = ?", (monitor_id,)
            )
            expected_versions = 1
        else:
            row = secondary.execute(
                "SELECT spec_json FROM monitor_versions WHERE id = ?", (active.version_id,)
            ).fetchone()
            secondary.execute(
                "INSERT INTO monitor_versions(id, monitor_id, version_number, spec_json, "
                "created_by, created_at) VALUES ('replacement-version', ?, 2, ?, 'owner', ?)",
                (monitor_id, row["spec_json"], datetime.now(UTC).isoformat()),
            )
            secondary.execute(
                "UPDATE monitors SET active_version_id = 'replacement-version' WHERE id = ?",
                (monitor_id,),
            )
            expected_versions = 2
        resume.set()
        with pytest.raises(ValueError, match="precondition"):
            await task
        assert primary.execute("SELECT count(*) FROM adaptive_features").fetchone()[0] == 0
        assert primary.execute("SELECT count(*) FROM diagnostic_snapshots").fetchone()[0] == 0
        assert primary.execute("SELECT count(*) FROM monitor_versions").fetchone()[0] == (
            expected_versions
        )

    try:
        asyncio.run(scenario())
    finally:
        secondary.close()
        primary.close()


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
        propose(
            recovery,
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
            propose(
                recovery,
                monitor_id,
                owner_id=owner_id,
                document=document(changed_html()),
                failure_class=ErrorClass.STRUCTURE,
            )

    assert connection.execute("SELECT count(*) FROM diagnostic_snapshots").fetchone()[0] == 0


@pytest.mark.parametrize(
    "status",
    [
        MonitorStatus.PAUSED_USER,
        MonitorStatus.PAUSED_AUTH,
        MonitorStatus.NEEDS_REVIEW,
        MonitorStatus.DISABLED,
    ],
)
@pytest.mark.parametrize("operation", ["baseline", "proposal"])
def test_non_active_monitors_cannot_write_features_candidates_or_diagnostics(
    connection: sqlite3.Connection,
    adaptive_storage: EncryptedAdaptiveStorage,
    status: MonitorStatus,
    operation: str,
) -> None:
    recovery, _, _, _, monitor_id = build_recovery(connection, adaptive_storage)
    connection.execute("UPDATE monitors SET status = ? WHERE id = ?", (status.value, monitor_id))

    with pytest.raises(ValueError, match="eligible"):
        if operation == "baseline":
            save_baseline(
                recovery,
                monitor_id,
                owner_id="owner",
                document=document(baseline_html()),
            )
        else:
            propose(
                recovery,
                monitor_id,
                owner_id="owner",
                document=document(changed_html()),
                failure_class=ErrorClass.STRUCTURE,
            )

    assert connection.execute("SELECT count(*) FROM adaptive_features").fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM diagnostic_snapshots").fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM monitor_versions").fetchone()[0] == 1


@pytest.mark.parametrize(
    ("final_url", "redirect_urls"),
    [
        ("https://evil.example/catalog", ()),
        (
            "https://redirect.example/final",
            ("https://evil.example/start",),
        ),
        (
            "https://redirect.example/final",
            (
                "https://example.com/catalog",
                "https://r1.example/",
                "https://r2.example/",
                "https://r3.example/",
                "https://r4.example/",
                "https://r5.example/",
            ),
        ),
        (
            "https://redirect.example/final",
            (
                "https://example.com/catalog",
                "https://r1.example/",
                "https://r1.example/",
            ),
        ),
    ],
)
def test_unassociated_or_invalid_redirect_lineage_fails_before_any_write(
    connection: sqlite3.Connection,
    adaptive_storage: EncryptedAdaptiveStorage,
    final_url: str,
    redirect_urls: tuple[str, ...],
) -> None:
    recovery, _, _, _, monitor_id = build_recovery(connection, adaptive_storage)
    failed_document = document(baseline_html(), final_url=final_url, redirect_urls=redirect_urls)

    with pytest.raises(ValueError, match="eligible") as caught:
        save_baseline(
            recovery,
            monitor_id,
            owner_id="owner",
            document=failed_document,
        )

    assert "evil.example" not in str(caught.value)
    assert connection.execute("SELECT count(*) FROM adaptive_features").fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM diagnostic_snapshots").fetchone()[0] == 0


def test_valid_unique_redirect_lineage_remains_eligible(
    connection: sqlite3.Connection,
    adaptive_storage: EncryptedAdaptiveStorage,
) -> None:
    recovery, _, _, _, monitor_id = build_recovery(connection, adaptive_storage)

    save_baseline(
        recovery,
        monitor_id,
        owner_id="owner",
        document=document(
            baseline_html(),
            final_url="https://redirect.example/final",
            redirect_urls=(
                "https://example.com/catalog",
                "https://redirect.example/intermediate",
            ),
        ),
    )

    assert connection.execute("SELECT count(*) FROM adaptive_features").fetchone()[0] == 3


def test_policy_base_exception_is_preserved_and_cannot_write(
    connection: sqlite3.Connection,
    adaptive_storage: EncryptedAdaptiveStorage,
) -> None:
    recovery, _, _, _, monitor_id = build_recovery(
        connection,
        adaptive_storage,
        resolver=Resolver(error=KeyboardInterrupt()),
    )

    with pytest.raises(KeyboardInterrupt):
        save_baseline(
            recovery,
            monitor_id,
            owner_id="owner",
            document=document(baseline_html()),
        )

    assert connection.execute("SELECT count(*) FROM adaptive_features").fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM diagnostic_snapshots").fetchone()[0] == 0


def test_dns_failure_is_a_fixed_eligibility_error_before_any_write(
    connection: sqlite3.Connection,
    adaptive_storage: EncryptedAdaptiveStorage,
) -> None:
    recovery, _, _, _, monitor_id = build_recovery(
        connection,
        adaptive_storage,
        resolver=Resolver(error=OSError("private resolver path")),
    )

    with pytest.raises(ValueError, match="eligible") as caught:
        save_baseline(
            recovery,
            monitor_id,
            owner_id="owner",
            document=document(baseline_html()),
        )

    assert "private resolver path" not in str(caught.value)
    assert connection.execute("SELECT count(*) FROM adaptive_features").fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM diagnostic_snapshots").fetchone()[0] == 0


@pytest.mark.parametrize(
    "attribute",
    [
        "_registry",
        "_repository",
        "_cipher",
        "_adaptive_storage",
        "_url_policy",
        "_extractor",
        "_validator",
    ],
)
@pytest.mark.parametrize("low_level", [False, True])
def test_recovery_dependency_replacement_fails_before_any_dispatch(
    connection: sqlite3.Connection,
    adaptive_storage: EncryptedAdaptiveStorage,
    attribute: str,
    low_level: bool,
) -> None:
    original_resolver = Resolver()
    recovery, _, _, _, monitor_id = build_recovery(
        connection, adaptive_storage, resolver=original_resolver
    )
    other_resolver = Resolver()
    replacements: dict[str, object] = {
        "_registry": RegistryRepository(connection),
        "_repository": RecoveryRepository(connection),
        "_cipher": AesGcmCipher(b"z" * 32),
        "_adaptive_storage": EncryptedAdaptiveStorage(
            connection,
            cipher=AesGcmCipher(b"z" * 32),
            clock=lambda: datetime(2026, 7, 23, tzinfo=UTC),
        ),
        "_url_policy": UrlPolicy(other_resolver),
        "_extractor": DeclarativeExtractor(),
        "_validator": ObservationValidator(),
    }
    if low_level:
        object.__setattr__(recovery, attribute, replacements[attribute])
    else:
        setattr(recovery, attribute, replacements[attribute])
    statements: list[str] = []
    connection.set_trace_callback(statements.append)

    with pytest.raises(RuntimeError, match="integrity"):
        save_baseline(
            recovery,
            monitor_id,
            owner_id="owner",
            document=document(baseline_html()),
        )

    assert statements == []
    assert original_resolver.calls == []
    assert other_resolver.calls == []


@pytest.mark.parametrize(
    "dependency",
    ["registry", "repository", "cipher", "extractor", "validator"],
)
def test_recovery_rejects_dependency_subclasses_at_the_constructor_boundary(
    connection: sqlite3.Connection,
    adaptive_storage: EncryptedAdaptiveStorage,
    dependency: str,
) -> None:
    from personal_monitor.scraping.recovery import AdaptiveRecovery

    class RegistrySubclass(RegistryRepository):
        pass

    class RecoverySubclass(RecoveryRepository):
        pass

    class CipherSubclass(AesGcmCipher):
        pass

    class ExtractorSubclass(DeclarativeExtractor):
        pass

    class ValidatorSubclass(ObservationValidator):
        pass

    dependencies: dict[str, object] = {
        "registry": RegistryRepository(connection),
        "repository": RecoveryRepository(connection),
        "cipher": AesGcmCipher(b"k" * 32),
        "adaptive_storage": adaptive_storage,
        "url_policy": UrlPolicy(Resolver()),
        "extractor": DeclarativeExtractor(),
        "validator": ObservationValidator(),
    }
    replacements: dict[str, object] = {
        "registry": RegistrySubclass(connection),
        "repository": RecoverySubclass(connection),
        "cipher": CipherSubclass(b"k" * 32),
        "extractor": ExtractorSubclass(),
        "validator": ValidatorSubclass(),
    }
    dependencies[dependency] = replacements[dependency]

    with pytest.raises(TypeError):
        AdaptiveRecovery(**dependencies)  # type: ignore[arg-type]


def test_replacing_recovery_accessor_cannot_replace_private_snapshot(
    connection: sqlite3.Connection,
    adaptive_storage: EncryptedAdaptiveStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import personal_monitor.scraping.recovery as recovery_module

    recovery, registry, repository, cipher, _monitor_id = build_recovery(
        connection, adaptive_storage
    )
    replacement = DeclarativeExtractor()
    object.__setattr__(recovery, "_extractor", replacement)
    monkeypatch.setattr(
        recovery_module,
        "_acquire_recovery",
        lambda _owner: (
            registry,
            repository,
            cipher,
            adaptive_storage,
            recovery._url_policy,
            replacement,
            recovery._validator,
            connection,
        ),
    )

    with pytest.raises(RuntimeError, match="integrity"):
        recovery_module._trusted_recovery(recovery)


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


@pytest.mark.parametrize("key", SENSITIVE_KEY_VARIANTS)
def test_recovery_candidate_removes_every_credential_field_key_variant(key: str) -> None:
    from personal_monitor.scraping.recovery import RecoveryCandidate

    candidate = RecoveryCandidate(
        version_id="version-id",
        validation_passed=True,
        field_changes={key: ".safe"},
        preview_items=[{key: "supersecretvalue"}],
    )

    assert candidate.field_changes == {}
    assert candidate.preview_items == ({},)
    assert "supersecretvalue" not in repr(candidate)


@pytest.mark.parametrize("key", SENSITIVE_COMPOUND_FIELD_NAMES)
def test_recovery_candidate_removes_compound_credential_field_keys(key: str) -> None:
    from personal_monitor.scraping.recovery import RecoveryCandidate

    candidate = RecoveryCandidate(
        version_id="version-id",
        validation_passed=True,
        field_changes={key: ".safe"},
        preview_items=[{key: "supersecretvalue"}],
    )

    assert candidate.field_changes == {}
    assert candidate.preview_items == ({},)
    assert "supersecretvalue" not in repr(candidate)


@pytest.mark.parametrize("key", SENSITIVE_KEY_VARIANTS)
def test_recovery_candidate_removes_urls_with_every_credential_query_variant(key: str) -> None:
    from personal_monitor.scraping.recovery import RecoveryCandidate

    secret_url = f"https://example.com/path?{quote_plus(key)}=supersecretvalue"
    candidate = RecoveryCandidate(
        version_id="version-id",
        validation_passed=True,
        field_changes={},
        preview_items=[{"safe": secret_url}],
    )

    assert candidate.preview_items == ({},)
    assert "supersecretvalue" not in repr(candidate)


@pytest.mark.parametrize("key", SENSITIVE_COMPOUND_FIELD_NAMES)
def test_recovery_url_checks_do_not_apply_compound_field_semantics(key: str) -> None:
    from personal_monitor.scraping.recovery import RecoveryCandidate

    safe_url = f"https://example.com/path?{quote_plus(key)}=ordinary"
    candidate = RecoveryCandidate(
        version_id="version-id",
        validation_passed=True,
        field_changes={},
        preview_items=[{"safe": safe_url}],
    )

    assert candidate.preview_items == ({"safe": safe_url},)


@pytest.mark.parametrize("key", BENIGN_COMPOUND_FIELD_NAMES)
def test_recovery_candidate_preserves_noncanonical_field_keys(key: str) -> None:
    from personal_monitor.scraping.recovery import RecoveryCandidate

    candidate = RecoveryCandidate(
        version_id="version-id",
        validation_passed=True,
        field_changes={key: ".safe"},
        preview_items=[{key: "ordinary"}],
    )

    assert candidate.field_changes == {key: ".safe"}
    assert candidate.preview_items == ({key: "ordinary"},)


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

    with pytest.raises(TypeError, match="sealed encrypted"):
        AdaptiveRecovery(
            registry=registry,
            repository=repository,
            cipher=AesGcmCipher(b"k" * 32),
            adaptive_storage=object(),  # type: ignore[arg-type]
            url_policy=UrlPolicy(Resolver()),
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
    save_baseline(recovery, monitor_id, owner_id="owner", document=document(baseline_html()))
    propose(
        recovery,
        monitor_id,
        owner_id="owner",
        document=document(changed_html()),
        failure_class=ErrorClass.STRUCTURE,
    )

    after = global_path.stat() if global_path.exists() else None
    assert before == after
    assert connection.execute("SELECT count(*) FROM adaptive_features").fetchone()[0] > 0
