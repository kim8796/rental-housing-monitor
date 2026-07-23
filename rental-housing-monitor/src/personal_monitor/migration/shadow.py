from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from zoneinfo import ZoneInfo

from personal_monitor.domain.observation import ObservationBatch, ObservedItem, content_hash
from personal_monitor.domain.rules import RuleMatch, evaluate_rules
from personal_monitor.domain.spec import RuleKind
from personal_monitor.domain.validator import validate_batch
from personal_monitor.engine.runner import render_payload
from personal_monitor.migration.import_rental import (
    IMPORT_MARKER_ID,
    RENTAL_MONITOR_ID,
    RENTAL_VERSION_ID,
    _outbox_id,
    _read_legacy_shadow_snapshot,
    _rental_spec,
)
from personal_monitor.storage.registry import RegistryRepository
from personal_monitor.storage.schema import canonical_json, transaction, utc_now

_AGENCIES = ("LH", "SH", "GH")
_AGENCY_SET = frozenset(_AGENCIES)
_STATUS_SET = frozenset({"ok", "failed"})
_ITEM_ID = re.compile(r"announcement:(LH|SH|GH):[^\s:][^\s]{0,508}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MAX_ITEMS = 10_000
_MAX_ID_BYTES = 512
_MAX_STORED_ROWS = 4_000
_SEOUL = ZoneInfo("Asia/Seoul")


def _redacted_repr(owner: object) -> str:
    return f"<{type(owner).__name__} redacted>"


def _date(value: object, *, name: str) -> date:
    if type(value) is not date or value.isoformat() != str(value):
        raise ValueError(f"{name} is invalid")
    return value


def _aware_time(value: object, *, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} is invalid")
    return value


def _hash(value: object, *, name: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{name} is invalid")
    return value


def _boolean(value: object, *, name: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{name} is invalid")
    return value


def _bounded_count(value: object, *, name: str) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_ITEMS * 3:
        raise ValueError(f"{name} is invalid")
    return value


def _item_id(value: object, *, agency: str | None = None) -> str:
    if type(value) is not str:
        raise ValueError("item identity is invalid")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("item identity is invalid") from None
    match = _ITEM_ID.fullmatch(value)
    if (
        match is None
        or len(encoded) > _MAX_ID_BYTES
        or "\x00" in value
        or (agency is not None and match.group(1) != agency)
    ):
        raise ValueError("item identity is invalid")
    return value


def _status_map(value: object) -> Mapping[str, str]:
    if type(value) is not dict and not isinstance(value, MappingProxyType):
        raise ValueError("source status is invalid")
    values = dict(value)
    if (
        set(values) != _AGENCY_SET
        or any(type(key) is not str for key in values)
        or any(type(item) is not str or item not in _STATUS_SET for item in values.values())
    ):
        raise ValueError("source status is invalid")
    return MappingProxyType({agency: values[agency] for agency in _AGENCIES})


def _ids(value: object, *, agency: str) -> tuple[str, ...]:
    if type(value) is not tuple or len(value) > _MAX_ITEMS:
        raise ValueError("item identities are invalid")
    result = tuple(_item_id(item, agency=agency) for item in value)
    if result != tuple(sorted(result)) or len(result) != len(set(result)):
        raise ValueError("item identities are invalid")
    return result


@dataclass(frozen=True, slots=True, repr=False)
class ShadowItem:
    agency: str
    item_id: str
    filter_outcome: str = "included"

    def __post_init__(self) -> None:
        if type(self.agency) is not str or self.agency not in _AGENCY_SET:
            raise ValueError("agency is invalid")
        _item_id(self.item_id, agency=self.agency)
        if type(self.filter_outcome) is not str or self.filter_outcome != "included":
            raise ValueError("filter outcome is invalid")

    __repr__ = _redacted_repr


@dataclass(frozen=True, slots=True, repr=False)
class ShadowSnapshot:
    items: tuple[ShadowItem, ...] = field(repr=False)
    source_status: Mapping[str, str] = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.items) is not tuple or len(self.items) > _MAX_ITEMS:
            raise ValueError("shadow items are invalid")
        if any(type(item) is not ShadowItem for item in self.items):
            raise ValueError("shadow items are invalid")
        identities = [item.item_id for item in self.items]
        if len(identities) != len(set(identities)):
            raise ValueError("shadow item identities are invalid")
        ordered = tuple(sorted(self.items, key=lambda item: (item.agency, item.item_id)))
        object.__setattr__(self, "items", ordered)
        object.__setattr__(self, "source_status", _status_map(self.source_status))

    __repr__ = _redacted_repr


@dataclass(frozen=True, slots=True, repr=False)
class ShadowDifference:
    agency: str
    missing_ids: tuple[str, ...] = field(repr=False)
    extra_ids: tuple[str, ...] = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.agency) is not str or self.agency not in _AGENCY_SET:
            raise ValueError("agency is invalid")
        missing = _ids(self.missing_ids, agency=self.agency)
        extra = _ids(self.extra_ids, agency=self.agency)
        if not missing and not extra:
            raise ValueError("shadow difference is empty")
        if set(missing) & set(extra):
            raise ValueError("shadow difference is invalid")

    __repr__ = _redacted_repr


@dataclass(frozen=True, slots=True, repr=False)
class ShadowResult:
    run_date: date
    old_hash: str
    new_hash: str
    matched: bool
    differences: tuple[ShadowDifference, ...] = field(repr=False)
    old_status: Mapping[str, str] = field(repr=False)
    new_status: Mapping[str, str] = field(repr=False)
    recorded_at: datetime

    def __post_init__(self) -> None:
        _date(self.run_date, name="run date")
        _hash(self.old_hash, name="old hash")
        _hash(self.new_hash, name="new hash")
        _boolean(self.matched, name="matched")
        if (
            type(self.differences) is not tuple
            or len(self.differences) > len(_AGENCIES)
            or any(type(item) is not ShadowDifference for item in self.differences)
            or tuple(item.agency for item in self.differences)
            != tuple(sorted(item.agency for item in self.differences))
        ):
            raise ValueError("shadow differences are invalid")
        old_status = _status_map(self.old_status)
        new_status = _status_map(self.new_status)
        recorded_at = _aware_time(self.recorded_at, name="recorded time")
        actually_matched = (
            self.old_hash == self.new_hash
            and not self.differences
            and dict(old_status) == dict(new_status)
        )
        if self.matched != actually_matched:
            raise ValueError("matched flag is invalid")
        object.__setattr__(self, "old_status", old_status)
        object.__setattr__(self, "new_status", new_status)
        object.__setattr__(self, "recorded_at", recorded_at)

    __repr__ = _redacted_repr


@dataclass(frozen=True, slots=True, repr=False)
class DuplicateProbeResult:
    monitor_id: str
    run_date: date
    current_hash: str
    passed: bool
    missing_ids: tuple[str, ...] = field(repr=False)
    conflicting_ids: tuple[str, ...] = field(repr=False)
    recorded_at: datetime

    def __post_init__(self) -> None:
        if type(self.monitor_id) is not str or self.monitor_id != RENTAL_MONITOR_ID:
            raise ValueError("monitor identity is invalid")
        _date(self.run_date, name="run date")
        _hash(self.current_hash, name="current hash")
        _boolean(self.passed, name="passed")
        missing = _all_agency_ids(self.missing_ids)
        conflicting = _all_agency_ids(self.conflicting_ids)
        if set(missing) & set(conflicting) or (self.passed and (missing or conflicting)):
            raise ValueError("probe differences are invalid")
        _aware_time(self.recorded_at, name="recorded time")

    __repr__ = _redacted_repr


def _all_agency_ids(value: object) -> tuple[str, ...]:
    if type(value) is not tuple or len(value) > _MAX_ITEMS:
        raise ValueError("item identities are invalid")
    result = tuple(_item_id(item) for item in value)
    if result != tuple(sorted(result)) or len(result) != len(set(result)):
        raise ValueError("item identities are invalid")
    return result


@dataclass(frozen=True, slots=True, repr=False)
class MigrationStatus:
    consecutive_matches: int
    last_match_date: date | None
    unresolved_differences: int
    state_imported: bool
    duplicate_probe_passed: bool
    cutover_ready: bool

    def __post_init__(self) -> None:
        _bounded_count(self.consecutive_matches, name="consecutive matches")
        if self.last_match_date is not None:
            _date(self.last_match_date, name="last match date")
        _bounded_count(self.unresolved_differences, name="unresolved differences")
        _boolean(self.state_imported, name="state imported")
        _boolean(self.duplicate_probe_passed, name="duplicate probe passed")
        _boolean(self.cutover_ready, name="cutover ready")

    __repr__ = _redacted_repr


class ShadowComparator:
    __slots__ = ("_clock",)

    def __init__(self, *, clock: Callable[[], datetime] = utc_now) -> None:
        if not callable(clock):
            raise ValueError("clock is invalid")
        self._clock = clock

    def __repr__(self) -> str:
        return "<ShadowComparator redacted>"

    def compare(
        self,
        old: ShadowSnapshot,
        new: ShadowSnapshot,
        run_date: date,
    ) -> ShadowResult:
        if type(old) is not ShadowSnapshot or type(new) is not ShadowSnapshot:
            raise ValueError("shadow snapshot is invalid")
        _date(run_date, name="run date")
        old_safe = _safe_snapshot(old)
        new_safe = _safe_snapshot(new)
        old_by_agency = {
            agency: {item.item_id for item in old.items if item.agency == agency}
            for agency in _AGENCIES
        }
        new_by_agency = {
            agency: {item.item_id for item in new.items if item.agency == agency}
            for agency in _AGENCIES
        }
        differences = tuple(
            ShadowDifference(
                agency=agency,
                missing_ids=tuple(sorted(old_by_agency[agency] - new_by_agency[agency])),
                extra_ids=tuple(sorted(new_by_agency[agency] - old_by_agency[agency])),
            )
            for agency in sorted(_AGENCIES)
            if old_by_agency[agency] != new_by_agency[agency]
        )
        old_hash = content_hash(old_safe)
        new_hash = content_hash(new_safe)
        statuses_match = dict(old.source_status) == dict(new.source_status)
        return ShadowResult(
            run_date=run_date,
            old_hash=old_hash,
            new_hash=new_hash,
            matched=old_hash == new_hash and not differences and statuses_match,
            differences=differences,
            old_status=old.source_status,
            new_status=new.source_status,
            recorded_at=_aware_time(self._clock(), name="clock"),
        )


def _safe_snapshot(value: ShadowSnapshot) -> dict[str, object]:
    return {
        "items": [
            [item.agency, item.item_id, item.filter_outcome]
            for item in sorted(value.items, key=lambda item: (item.agency, item.item_id))
        ],
        "source_status": dict(value.source_status),
    }


def load_legacy_shadow_snapshot(
    source_path: str | Path,
    run_date: date,
    *,
    now: datetime | None = None,
) -> ShadowSnapshot:
    """Load the fixed legacy run through Task 2's strict read-only source contract."""
    observed, statuses = _read_legacy_shadow_snapshot(
        source_path,
        run_date,
        now=utc_now() if now is None else now,
    )
    return ShadowSnapshot(
        items=tuple(
            ShadowItem(
                agency=str(row.fields["agency"]),
                item_id=f"announcement:{row.key}",
            )
            for row in observed
        ),
        source_status=statuses,
    )


class RentalShadowError(RuntimeError):
    """A deliberately fixed and redacted shadow execution failure."""

    __slots__ = ()

    def __init__(self) -> None:
        super().__init__("rental shadow failed")

    def __repr__(self) -> str:
        return "<RentalShadowError redacted>"


async def run_shadow_fetch(
    source_path: str | Path,
    repository: ShadowRepository,
    run_date: date,
    *,
    adapter: object,
    sender: object,
    now: datetime | None = None,
) -> ShadowResult:
    """Fetch once and record parity without entering any persistence/delivery runner path."""
    try:
        if type(repository) is not ShadowRepository:
            raise ValueError("shadow repository is invalid")
        fetch = getattr(adapter, "fetch", None)
        send = getattr(sender, "send", None)
        if not callable(fetch) or not callable(send):
            raise ValueError("shadow composition is invalid")
        requested = _date(run_date, name="run date")
        evidence_time = utc_now() if now is None else _aware_time(now, name="clock")
        active = _active_rental(repository)
        old = load_legacy_shadow_snapshot(
            source_path,
            requested,
            now=evidence_time,
        )
        batch = await fetch(RENTAL_MONITOR_ID, active.spec)
        new = _snapshot_from_batch(batch, active.spec, requested)
        result = ShadowComparator().compare(old, new, requested)
        repository.record(result)
        return result
    except Exception:
        raise RentalShadowError from None


def _active_rental(repository: ShadowRepository):
    active = RegistryRepository(repository.connection).get_active_monitor(RENTAL_MONITOR_ID)
    monitor = repository.connection.execute(
        "SELECT status, active_version_id FROM monitors WHERE id = ?",
        (RENTAL_MONITOR_ID,),
    ).fetchone()
    if (
        monitor is None
        or monitor["status"] != "active"
        or monitor["active_version_id"] != RENTAL_VERSION_ID
        or active.version_id != RENTAL_VERSION_ID
        or active.spec != _rental_spec(active.owner_id)
    ):
        raise ValueError("rental monitor is incompatible")
    return active


def _snapshot_from_batch(
    batch: object,
    spec: object,
    requested: date,
) -> ShadowSnapshot:
    if type(batch) is not ObservationBatch:
        raise ValueError("adapter batch is invalid")
    if batch.monitor_id != RENTAL_MONITOR_ID:
        raise ValueError("adapter batch is invalid")
    observed_at = _aware_time(batch.observed_at, name="observed time")
    if observed_at.astimezone(_SEOUL).date() != requested:
        raise ValueError("adapter batch date is invalid")
    _hash(batch.source_hash, name="source hash")
    statuses = _status_map(batch.source_status)
    failed = {agency for agency in _AGENCIES if statuses[agency] == "failed"}
    warning_sources = {warning.source for warning in batch.warnings}
    if (
        any(
            type(warning.source) is not str
            or warning.source not in _AGENCY_SET
            or type(warning.stage) is not str
            or type(warning.detail) is not str
            for warning in batch.warnings
        )
        or warning_sources != failed
    ):
        raise ValueError("adapter batch status is invalid")
    validate_batch(spec, batch)  # type: ignore[arg-type]
    items: list[ShadowItem] = []
    for item in batch.items:
        agency = item.fields.get("agency")
        if type(agency) is not str:
            raise ValueError("adapter item agency is invalid")
        items.append(ShadowItem(agency=agency, item_id=item.item_id))
    return ShadowSnapshot(items=tuple(items), source_status=statuses)


class RentalDuplicateProbeError(RuntimeError):
    """A deliberately fixed and redacted duplicate-probe failure."""

    __slots__ = ()

    def __init__(self) -> None:
        super().__init__("rental duplicate probe failed")

    def __repr__(self) -> str:
        return "<RentalDuplicateProbeError redacted>"


async def run_duplicate_probe(
    repository: ShadowRepository,
    monitor_id: str,
    *,
    adapter: object,
    sender: object,
    now: datetime | None = None,
) -> DuplicateProbeResult:
    """Probe imported state through the fetch-only boundary and record no delivery work."""
    try:
        if type(repository) is not ShadowRepository or monitor_id != RENTAL_MONITOR_ID:
            raise ValueError("duplicate probe monitor is invalid")
        fetch = getattr(adapter, "fetch", None)
        send = getattr(sender, "send", None)
        if not callable(fetch) or not callable(send):
            raise ValueError("duplicate probe composition is invalid")
        evidence_time = _aware_time(utc_now() if now is None else now, name="clock")
        active = _active_rental(repository)
        batch = await fetch(RENTAL_MONITOR_ID, active.spec)
        if type(batch) is not ObservationBatch:
            raise ValueError("adapter batch is invalid")
        observed_at = _aware_time(batch.observed_at, name="observed time")
        run_date = observed_at.astimezone(_SEOUL).date()
        local_today = evidence_time.astimezone(_SEOUL).date()
        if run_date not in {local_today, local_today - timedelta(days=1)}:
            raise ValueError("adapter batch date is invalid")
        current = _snapshot_from_batch(batch, active.spec, run_date)
        missing, conflicting, aggregate_ok = _probe_imported_state(
            repository.connection,
            batch,
            active.spec,
        )
        complete = not batch.warnings and all(
            current.source_status[agency] == "ok" for agency in _AGENCIES
        )
        result = DuplicateProbeResult(
            monitor_id=RENTAL_MONITOR_ID,
            run_date=run_date,
            current_hash=content_hash(_safe_snapshot(current)),
            passed=complete and aggregate_ok and not missing and not conflicting,
            missing_ids=missing,
            conflicting_ids=conflicting,
            recorded_at=evidence_time,
        )
        repository.record_duplicate_probe(result)
        return result
    except Exception:
        raise RentalDuplicateProbeError from None


def _probe_imported_state(
    connection: sqlite3.Connection,
    batch: ObservationBatch,
    spec: object,
) -> tuple[tuple[str, ...], tuple[str, ...], bool]:
    try:
        imported = _state_imported(connection)
    except RuntimeError:
        imported = False
    rows = connection.execute(
        "SELECT item_id, fields_json, content_hash FROM observations "
        "WHERE monitor_id = ? ORDER BY item_id LIMIT ?",
        (RENTAL_MONITOR_ID, _MAX_ITEMS + 1),
    ).fetchall()
    if len(rows) > _MAX_ITEMS:
        return (), (), False
    stored: dict[str, tuple[str, str]] = {}
    aggregate_ok = imported
    for row in rows:
        try:
            item_id = _item_id(row["item_id"])
        except (KeyError, TypeError, ValueError, UnicodeError):
            aggregate_ok = False
            continue
        try:
            fields = _unique_json(row["fields_json"])
            if type(fields) is not dict or canonical_json(fields) != row["fields_json"]:
                raise ValueError
            item = ObservedItem(item_id=item_id, fields=fields)
            if content_hash(item.fields) != row["content_hash"]:
                raise ValueError
            if evaluate_rules(
                spec.rules,  # type: ignore[attr-defined]
                previous=item,
                current=item,
                is_new=False,
            ):
                raise ValueError
            stored[item_id] = (row["fields_json"], row["content_hash"])
        except (KeyError, TypeError, ValueError, UnicodeError):
            aggregate_ok = False
            stored[item_id] = ("", "")
    missing: list[str] = []
    conflicting: list[str] = []
    for item in batch.items:
        expected = (canonical_json(item.fields), content_hash(item.fields))
        actual = stored.get(item.item_id)
        if actual is None:
            missing.append(item.item_id)
        elif actual != expected:
            conflicting.append(item.item_id)
    if not _delivered_aggregate_consistent(connection, spec):
        aggregate_ok = False
    return tuple(sorted(missing)), tuple(sorted(conflicting)), aggregate_ok


def _delivered_aggregate_consistent(connection: sqlite3.Connection, spec: object) -> bool:
    rows = connection.execute(
        "SELECT o.id, o.dedupe_key, o.target_id, o.payload_json, o.status, "
        "o.attempt_count, o.available_at, o.last_error, o.lease_owner, "
        "o.lease_expires_at, o.created_at, d.target_id AS delivery_target, "
        "d.external_message_id, d.delivered_at, ob.item_id, ob.fields_json, "
        "m.owner_id AS monitor_owner, t.owner_id AS target_owner "
        "FROM outbox AS o "
        "LEFT JOIN deliveries AS d ON d.outbox_id = o.id "
        "LEFT JOIN observations AS ob ON ob.monitor_id = o.monitor_id "
        "AND o.dedupe_key = o.monitor_id || ':' || ob.item_id || ':new_item' "
        "LEFT JOIN monitors AS m ON m.id = o.monitor_id "
        "LEFT JOIN delivery_targets AS t ON t.id = o.target_id "
        "WHERE o.monitor_id = ? ORDER BY o.id LIMIT ?",
        (RENTAL_MONITOR_ID, _MAX_ITEMS + 1),
    ).fetchall()
    if len(rows) > _MAX_ITEMS:
        return False
    for row in rows:
        try:
            item_id = _item_id(row["item_id"])
            dedupe_key = f"{RENTAL_MONITOR_ID}:{item_id}:new_item"
            fields = _unique_json(row["fields_json"])
            if type(fields) is not dict or canonical_json(fields) != row["fields_json"]:
                return False
            payload = render_payload(
                spec,  # type: ignore[arg-type]
                ObservedItem(item_id=item_id, fields=fields),
                RuleMatch(RuleKind.NEW_ITEM, None, None, None),
            )
            if (
                row["dedupe_key"] != dedupe_key
                or row["id"] != _outbox_id(dedupe_key)
                or row["payload_json"] != canonical_json(payload)
                or row["status"] != "delivered"
                or type(row["attempt_count"]) is not int
                or row["attempt_count"] != 0
                or row["available_at"] != row["created_at"]
                or row["created_at"] != row["delivered_at"]
                or row["last_error"] is not None
                or row["lease_owner"] is not None
                or row["lease_expires_at"] is not None
                or row["target_id"] != row["delivery_target"]
                or type(row["external_message_id"]) is not str
                or not row["external_message_id"]
                or row["monitor_owner"] != row["target_owner"]
            ):
                return False
        except (KeyError, TypeError, ValueError, UnicodeError):
            return False
    return True


class ShadowRepository:
    __slots__ = ("_connection", "_clock")

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if type(connection) is not sqlite3.Connection or not callable(clock):
            raise ValueError("shadow storage is invalid")
        self._connection = connection
        self._clock = clock

    def __repr__(self) -> str:
        return "<ShadowRepository redacted>"

    @property
    def connection(self) -> sqlite3.Connection:
        return self._connection

    def record(self, result: ShadowResult) -> None:
        values = _result_values(result)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO rental_shadow_results("
                    "run_date, old_hash, new_hash, matched, differences_json, "
                    "old_status_json, new_status_json, recorded_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(run_date) DO UPDATE SET "
                    "old_hash=excluded.old_hash,new_hash=excluded.new_hash,"
                    "matched=excluded.matched,differences_json=excluded.differences_json,"
                    "old_status_json=excluded.old_status_json,"
                    "new_status_json=excluded.new_status_json,"
                    "recorded_at=excluded.recorded_at",
                    values,
                )
        except sqlite3.Error:
            raise RuntimeError("shadow result storage failed") from None

    def record_duplicate_probe(self, result: DuplicateProbeResult) -> None:
        values = _probe_values(result)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO rental_duplicate_probe_results("
                    "monitor_id, run_date, current_hash, passed, differences_json, recorded_at"
                    ") VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(monitor_id) DO UPDATE SET "
                    "run_date=excluded.run_date,current_hash=excluded.current_hash,"
                    "passed=excluded.passed,differences_json=excluded.differences_json,"
                    "recorded_at=excluded.recorded_at",
                    values,
                )
        except sqlite3.Error:
            raise RuntimeError("duplicate probe storage failed") from None

    def status(self, as_of: date) -> MigrationStatus:
        requested = _date(as_of, name="as-of date")
        now = _aware_time(self._clock(), name="clock").astimezone(_SEOUL).date()
        if requested > now:
            raise ValueError("as-of date is invalid")
        rows = self.connection.execute(
            "SELECT run_date, old_hash, new_hash, matched, differences_json, "
            "old_status_json, new_status_json, recorded_at "
            "FROM rental_shadow_results ORDER BY run_date DESC LIMIT ?",
            (_MAX_STORED_ROWS + 1,),
        ).fetchall()
        if len(rows) > _MAX_STORED_ROWS:
            raise RuntimeError("shadow storage is invalid")
        results = tuple(_result_from_row(row) for row in rows)
        if any(result.run_date > now for result in results):
            raise RuntimeError("shadow storage is invalid")
        eligible = tuple(result for result in results if result.run_date <= requested)
        newest = eligible[0] if eligible else None
        consecutive = 0
        if newest is not None:
            expected = newest.run_date
            for result in eligible:
                if result.run_date != expected or not result.matched:
                    break
                consecutive += 1
                expected -= timedelta(days=1)
        unresolved = _unresolved(newest) if newest is not None else 0
        imported = _state_imported(self.connection)
        probe = _load_probe(self.connection)
        if probe is not None and probe.run_date > now:
            raise RuntimeError("duplicate probe storage is invalid")
        probe_passed = probe is not None and probe.run_date <= requested and probe.passed
        recent_shadow = newest is not None and newest.run_date in {
            requested,
            requested - timedelta(days=1),
        }
        probe_fresh = (
            probe is not None
            and newest is not None
            and probe.run_date >= newest.run_date
            and probe.run_date in {requested, requested - timedelta(days=1)}
        )
        ready = (
            recent_shadow
            and consecutive >= 7
            and unresolved == 0
            and imported
            and probe_passed
            and probe_fresh
        )
        return MigrationStatus(
            consecutive_matches=consecutive,
            last_match_date=newest.run_date if newest is not None and newest.matched else None,
            unresolved_differences=unresolved,
            state_imported=imported,
            duplicate_probe_passed=probe_passed,
            cutover_ready=ready,
        )

    def cutover_ready(self, as_of: date) -> bool:
        return self.status(as_of).cutover_ready


def _result_values(result: object) -> tuple[object, ...]:
    if type(result) is not ShadowResult:
        raise ValueError("shadow result is invalid")
    validated = ShadowResult(
        run_date=result.run_date,
        old_hash=result.old_hash,
        new_hash=result.new_hash,
        matched=result.matched,
        differences=result.differences,
        old_status=result.old_status,
        new_status=result.new_status,
        recorded_at=result.recorded_at,
    )
    differences = [
        {
            "agency": item.agency,
            "missing_ids": list(item.missing_ids),
            "extra_ids": list(item.extra_ids),
        }
        for item in validated.differences
    ]
    return (
        validated.run_date.isoformat(),
        validated.old_hash,
        validated.new_hash,
        int(validated.matched),
        canonical_json(differences),
        canonical_json(validated.old_status),
        canonical_json(validated.new_status),
        validated.recorded_at.astimezone(UTC).isoformat(),
    )


def _probe_values(result: object) -> tuple[object, ...]:
    if type(result) is not DuplicateProbeResult:
        raise ValueError("duplicate probe result is invalid")
    validated = DuplicateProbeResult(
        monitor_id=result.monitor_id,
        run_date=result.run_date,
        current_hash=result.current_hash,
        passed=result.passed,
        missing_ids=result.missing_ids,
        conflicting_ids=result.conflicting_ids,
        recorded_at=result.recorded_at,
    )
    differences = {
        "conflicting_ids": list(validated.conflicting_ids),
        "missing_ids": list(validated.missing_ids),
    }
    return (
        validated.monitor_id,
        validated.run_date.isoformat(),
        validated.current_hash,
        int(validated.passed),
        canonical_json(differences),
        validated.recorded_at.astimezone(UTC).isoformat(),
    )


def _unique_json(text: object) -> object:
    if type(text) is not str or len(text.encode("utf-8")) > 1_000_000:
        raise RuntimeError("shadow storage is invalid")

    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    try:
        return json.loads(
            text,
            object_pairs_hook=unique,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (TypeError, ValueError, UnicodeError):
        raise RuntimeError("shadow storage is invalid") from None


def _result_from_row(row: sqlite3.Row) -> ShadowResult:
    try:
        differences_raw = _unique_json(row["differences_json"])
        if type(differences_raw) is not list:
            raise ValueError
        differences: list[ShadowDifference] = []
        for item in differences_raw:
            if type(item) is not dict or set(item) != {"agency", "missing_ids", "extra_ids"}:
                raise ValueError
            missing = item["missing_ids"]
            extra = item["extra_ids"]
            if type(missing) is not list or type(extra) is not list:
                raise ValueError
            differences.append(ShadowDifference(item["agency"], tuple(missing), tuple(extra)))
        old_status = _unique_json(row["old_status_json"])
        new_status = _unique_json(row["new_status_json"])
        if type(old_status) is not dict or type(new_status) is not dict:
            raise ValueError
        matched = row["matched"]
        if type(matched) is not int or matched not in {0, 1}:
            raise ValueError
        run_date = date.fromisoformat(row["run_date"])
        if run_date.isoformat() != row["run_date"]:
            raise ValueError
        recorded_at = datetime.fromisoformat(row["recorded_at"])
        result = ShadowResult(
            run_date=run_date,
            old_hash=row["old_hash"],
            new_hash=row["new_hash"],
            matched=bool(matched),
            differences=tuple(differences),
            old_status=old_status,
            new_status=new_status,
            recorded_at=recorded_at,
        )
        if _result_values(result) != tuple(row):
            raise ValueError
        return result
    except (KeyError, TypeError, ValueError, UnicodeError):
        raise RuntimeError("shadow storage is invalid") from None


def _probe_from_row(row: sqlite3.Row) -> DuplicateProbeResult:
    try:
        raw = _unique_json(row["differences_json"])
        if type(raw) is not dict or set(raw) != {"missing_ids", "conflicting_ids"}:
            raise ValueError
        if type(raw["missing_ids"]) is not list or type(raw["conflicting_ids"]) is not list:
            raise ValueError
        passed = row["passed"]
        if type(passed) is not int or passed not in {0, 1}:
            raise ValueError
        run_date = date.fromisoformat(row["run_date"])
        if run_date.isoformat() != row["run_date"]:
            raise ValueError
        result = DuplicateProbeResult(
            monitor_id=row["monitor_id"],
            run_date=run_date,
            current_hash=row["current_hash"],
            passed=bool(passed),
            missing_ids=tuple(raw["missing_ids"]),
            conflicting_ids=tuple(raw["conflicting_ids"]),
            recorded_at=datetime.fromisoformat(row["recorded_at"]),
        )
        if _probe_values(result) != tuple(row):
            raise ValueError
        return result
    except (KeyError, TypeError, ValueError, UnicodeError):
        raise RuntimeError("duplicate probe storage is invalid") from None


def _load_probe(connection: sqlite3.Connection) -> DuplicateProbeResult | None:
    rows = connection.execute(
        "SELECT monitor_id, run_date, current_hash, passed, differences_json, recorded_at "
        "FROM rental_duplicate_probe_results"
    ).fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        raise RuntimeError("duplicate probe storage is invalid")
    return _probe_from_row(rows[0])


def _state_imported(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        "SELECT monitor_id, version_id, lease_generation, stage, fetch_strategy, status, "
        "started_at, finished_at, error_class, error_detail FROM runs WHERE id = ?",
        (IMPORT_MARKER_ID,),
    ).fetchone()
    if row is None:
        return False
    values = tuple(row)
    expected_fixed = (
        RENTAL_MONITOR_ID,
        RENTAL_VERSION_ID,
        0,
        "migration_import",
        None,
        "success",
    )
    try:
        started = datetime.fromisoformat(values[6])
        finished = datetime.fromisoformat(values[7])
        valid_time = (
            started.tzinfo is not None
            and started.utcoffset() is not None
            and finished.tzinfo is not None
            and finished.utcoffset() is not None
            and started.astimezone(UTC).isoformat() == values[6]
            and values[6] == values[7]
        )
    except (TypeError, ValueError):
        valid_time = False
    if values[:6] != expected_fixed or values[8:] != (None, None) or not valid_time:
        raise RuntimeError("import marker is invalid")
    return True


def _unresolved(result: ShadowResult) -> int:
    item_count = sum(len(item.missing_ids) + len(item.extra_ids) for item in result.differences)
    status_count = sum(
        result.old_status[agency] != result.new_status[agency] for agency in _AGENCIES
    )
    return item_count + status_count
