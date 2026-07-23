from __future__ import annotations

import asyncio
import re
import unicodedata
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, ExitStack, contextmanager, nullcontext
from datetime import UTC, date, datetime, timedelta
from types import MappingProxyType
from typing import Protocol, final
from urllib.parse import urlsplit

import httpx

from personal_monitor.domain.observation import (
    ObservationBatch,
    ObservedItem,
    SourceWarning,
    content_hash,
)
from personal_monitor.domain.spec import FieldSpec, FieldType, MonitorSpec, SourceAdapterKind
from personal_monitor.engine.errors import ErrorClass, MonitorError
from personal_monitor.security.url_policy import (
    ALLOWED_PORTS,
    canonicalize_hostname,
    has_unsafe_url_characters,
)
from rental_monitor.collectors.base import Collector, CollectorError
from rental_monitor.collectors.gh import GHCollector
from rental_monitor.collectors.lh import LHCollector
from rental_monitor.collectors.sh import SHCollector
from rental_monitor.gh_tls import build_gh_ssl_context
from rental_monitor.models import Agency, Announcement, HousingType, canonical_key

RENTAL_ITEM_SCOPE = "announcement"
RENTAL_ANNOUNCEMENT_FIELDS: Mapping[str, FieldSpec] = MappingProxyType(
    {
        "source_id": FieldSpec(selector="source_id", type=FieldType.TEXT, required=False),
        "title": FieldSpec(selector="title", type=FieldType.TEXT),
        "agency": FieldSpec(selector="agency", type=FieldType.TEXT),
        "region": FieldSpec(selector="region", type=FieldType.TEXT),
        "housing_type": FieldSpec(selector="housing_type", type=FieldType.TEXT),
        "target": FieldSpec(selector="target", type=FieldType.TEXT),
        "announcement_date": FieldSpec(selector="announcement_date", type=FieldType.DATE),
        "application_start_date": FieldSpec(
            selector="application_start_date",
            type=FieldType.DATE,
            required=False,
        ),
        "application_end_date": FieldSpec(
            selector="application_end_date",
            type=FieldType.DATE,
            required=False,
        ),
        "url": FieldSpec(selector="url", type=FieldType.URL),
    }
)

_AGENCIES = (Agency.LH, Agency.SH, Agency.GH)
_SAFE_EXCEPTION_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_USER_AGENT = "rental-housing-monitor/0.1 (+official-notice-checker)"

Clock = Callable[[], datetime]


class CollectorContextFactory(Protocol):
    def __call__(self) -> AbstractContextManager[Sequence[Collector]]: ...


@final
class RentalHousingAdapter:
    """Run the three fixed rental collectors behind one allowlisted adapter."""

    __slots__ = ("_clock", "_collector_factory")

    def __init__(
        self,
        collector_factory: CollectorContextFactory,
        *,
        clock: Clock = lambda: datetime.now(UTC),
    ) -> None:
        if not callable(collector_factory):
            raise TypeError("collector_factory must be callable")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._collector_factory = collector_factory
        self._clock = clock

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("RentalHousingAdapter cannot be subclassed")

    @classmethod
    def from_collectors(
        cls,
        collectors: Sequence[Collector],
        *,
        clock: Clock = lambda: datetime.now(UTC),
    ) -> RentalHousingAdapter:
        """Inject non-owned collectors; the caller remains responsible for closing resources."""
        fixed = tuple(collectors)

        def factory() -> AbstractContextManager[Sequence[Collector]]:
            return nullcontext(fixed)

        return cls(factory, clock=clock)

    @classmethod
    def production(
        cls,
        data_go_kr_service_key: str,
        *,
        clock: Clock = lambda: datetime.now(UTC),
    ) -> RentalHousingAdapter:
        """Create an adapter that owns fresh production HTTP clients for each fetch."""
        if (
            type(data_go_kr_service_key) is not str
            or not data_go_kr_service_key
            or len(data_go_kr_service_key) > 4096
            or any(ord(character) < 32 for character in data_go_kr_service_key)
        ):
            raise ValueError("data.go.kr service key is invalid")

        def factory() -> AbstractContextManager[Sequence[Collector]]:
            return _production_collectors(data_go_kr_service_key)

        return cls(factory, clock=clock)

    async def fetch(self, monitor_id: str, spec: MonitorSpec) -> ObservationBatch:
        _validate_spec(spec)
        observed_at = _read_clock(self._clock)

        try:
            context = self._collector_factory()
        except Exception:
            raise _validation_error("collector construction failed") from None
        try:
            with context as collectors:
                ordered = _ordered_collectors(collectors)
                results = await _collect_all(ordered)
        except asyncio.CancelledError:
            raise
        except MonitorError:
            raise
        except Exception:
            raise _validation_error("collector construction failed") from None

        statuses: dict[str, str] = {}
        warnings: list[SourceWarning] = []
        collected_items: list[ObservedItem] = []
        for agency, result in zip(_AGENCIES, results, strict=True):
            if isinstance(result, _CollectionFailure):
                statuses[agency.value] = "failed"
                warnings.append(
                    SourceWarning(
                        source=agency.value,
                        stage=result.stage,
                        detail=result.detail,
                    )
                )
            else:
                statuses[agency.value] = "ok"
                collected_items.extend(result)

        items_by_id: dict[str, ObservedItem] = {}
        for item in collected_items:
            previous = items_by_id.get(item.item_id)
            if previous is not None and previous.fields != item.fields:
                raise _validation_error("conflicting duplicate announcement")
            items_by_id[item.item_id] = item
        items = tuple(items_by_id[item_id] for item_id in sorted(items_by_id))
        hash_input = {
            "items": [{"item_id": item.item_id, "fields": dict(item.fields)} for item in items],
            "source_status": [
                {"source": agency.value, "status": statuses[agency.value]} for agency in _AGENCIES
            ],
            "warnings": [
                {
                    "source": warning.source,
                    "stage": warning.stage,
                    "detail": warning.detail,
                }
                for warning in warnings
            ],
        }
        return ObservationBatch(
            monitor_id=monitor_id,
            items=items,
            observed_at=observed_at,
            source_hash=content_hash(hash_input),
            source_status=statuses,
            warnings=tuple(warnings),
        )


def production_rental_housing_adapter(
    data_go_kr_service_key: str,
    *,
    clock: Clock = lambda: datetime.now(UTC),
) -> RentalHousingAdapter:
    return RentalHousingAdapter.production(data_go_kr_service_key, clock=clock)


@contextmanager
def _production_collectors(service_key: str) -> Iterator[Sequence[Collector]]:
    options = {
        "follow_redirects": True,
        "headers": {"User-Agent": _USER_AGENT},
    }
    with ExitStack() as stack:
        client = stack.enter_context(httpx.Client(**options))
        gh_client = stack.enter_context(
            httpx.Client(
                **options,
                verify=build_gh_ssl_context(),
            )
        )
        yield (
            LHCollector(client, service_key),
            SHCollector(client),
            GHCollector(gh_client),
        )


def _validate_spec(spec: MonitorSpec) -> None:
    if (
        type(spec) is not MonitorSpec
        or spec.source_adapter is not SourceAdapterKind.PYTHON_PLUGIN
        or spec.adapter_ref != "rental_housing"
    ):
        raise MonitorError(ErrorClass.POLICY, "adapter", "monitor adapter is incompatible")
    if (
        spec.extract.item_scope != RENTAL_ITEM_SCOPE
        or set(spec.extract.fields) != set(RENTAL_ANNOUNCEMENT_FIELDS)
        or any(
            spec.extract.fields[name] != expected
            for name, expected in RENTAL_ANNOUNCEMENT_FIELDS.items()
        )
    ):
        raise _validation_error("rental announcement schema is incompatible")


def _read_clock(clock: Clock) -> datetime:
    try:
        value = clock()
        if type(value) is not datetime or value.tzinfo is None:
            raise ValueError
        offset = value.utcoffset()
        if offset != timedelta(0):
            raise ValueError
        result = value.astimezone(UTC)
    except (Exception, BaseExceptionGroup):
        raise _validation_error("UTC clock returned an invalid value") from None
    return result


def _ordered_collectors(collectors: Sequence[Collector]) -> tuple[Collector, Collector, Collector]:
    try:
        candidates = tuple(collectors)
        if len(candidates) != len(_AGENCIES):
            raise ValueError
        by_agency: dict[Agency, Collector] = {}
        for collector in candidates:
            agency = collector.agency
            if type(agency) is not Agency or agency in by_agency:
                raise ValueError
            by_agency[agency] = collector
        return tuple(by_agency[agency] for agency in _AGENCIES)  # type: ignore[return-value]
    except Exception:
        raise _validation_error("collector set is invalid") from None


class _CollectionFailure:
    __slots__ = ("detail", "stage")

    def __init__(self, stage: str, detail: str) -> None:
        self.stage = stage
        self.detail = detail


async def _collect_all(
    collectors: tuple[Collector, Collector, Collector],
) -> tuple[list[ObservedItem] | _CollectionFailure, ...]:
    tasks = tuple(
        asyncio.create_task(
            _collect_one(agency, collector),
            name=f"rental-housing-collector:{agency.value}",
        )
        for agency, collector in zip(_AGENCIES, collectors, strict=True)
    )
    try:
        await asyncio.wait(tasks, return_when=asyncio.ALL_COMPLETED)
    except asyncio.CancelledError:
        await _join_collection_tasks(tasks)
        fatal = _fatal_collection_error(tasks)
        if fatal is not None:
            raise fatal from None
        raise
    except BaseException:
        await _join_collection_tasks(tasks)
        raise
    return _collection_results(tasks)


async def _join_collection_tasks(tasks: Sequence[asyncio.Task[object]]) -> None:
    if all(task.done() for task in tasks):
        return
    join_task = asyncio.create_task(
        asyncio.wait(tasks, return_when=asyncio.ALL_COMPLETED),
        name="rental-housing-collector:join",
    )
    while not join_task.done():
        try:
            await asyncio.shield(join_task)
        except asyncio.CancelledError:
            continue
    join_task.result()


def _fatal_collection_error(
    tasks: Sequence[asyncio.Task[object]],
) -> BaseException | None:
    errors: list[BaseException] = []
    for task in tasks:
        if task.cancelled():
            continue
        error = task.exception()
        if error is not None and not isinstance(error, Exception):
            errors.append(error)
    if not errors:
        return None
    if len(errors) == 1:
        return errors[0]
    return BaseExceptionGroup("rental collector fatal failures", errors)


def _collection_results(
    tasks: Sequence[asyncio.Task[list[ObservedItem] | _CollectionFailure]],
) -> tuple[list[ObservedItem] | _CollectionFailure, ...]:
    results: list[list[ObservedItem] | _CollectionFailure] = []
    errors: list[BaseException] = []
    for task in tasks:
        try:
            results.append(task.result())
        except BaseException as error:
            errors.append(error)
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise BaseExceptionGroup("rental collector failures", errors)
    return tuple(results)


async def _collect_one(
    agency: Agency,
    collector: Collector,
) -> list[ObservedItem] | _CollectionFailure:
    try:
        raw = await asyncio.to_thread(collector.collect)
        if type(raw) is not list:
            return _CollectionFailure("collection", "invalid collector result")
        items: list[ObservedItem] = []
        for value in raw:
            if type(value) is not Announcement or value.agency is not agency:
                return _CollectionFailure("collection", "invalid collector result")
            try:
                _validate_announcement(value)
                items.append(_translate(value))
            except Exception:
                return _CollectionFailure("collection", "invalid collector result")
        return items
    except asyncio.CancelledError:
        raise
    except CollectorError as error:
        if error.agency is not agency:
            return _CollectionFailure("collection", "invalid collector failure")
        return _CollectionFailure(error.stage, error.detail)
    except Exception as error:
        return _CollectionFailure("collection", _safe_exception_name(error))


def _safe_exception_name(error: Exception) -> str:
    name = type(error).__name__
    if _SAFE_EXCEPTION_NAME.fullmatch(name) is None:
        return "Exception"
    return name


def _validate_announcement(notice: Announcement) -> None:
    if notice.source_id is not None:
        _validate_text_scalar(notice.source_id, allow_blank=True)
    for value in (notice.title, notice.region, notice.target, notice.url):
        _validate_text_scalar(value)
    if type(notice.agency) is not Agency or type(notice.housing_type) is not HousingType:
        raise ValueError
    _validate_announcement_url(notice.url)
    for value in (
        notice.announcement_date,
        notice.application_start_date,
        notice.application_end_date,
    ):
        if value is not None and type(value) is not date:
            raise ValueError


def _validate_text_scalar(value: str, *, allow_blank: bool = False) -> None:
    if (
        type(value) is not str
        or (not allow_blank and not value.strip())
        or any(unicodedata.category(character).startswith("C") for character in value)
    ):
        raise ValueError
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ValueError from None


def _validate_announcement_url(value: str) -> None:
    try:
        if has_unsafe_url_characters(value):
            raise ValueError
        parts = urlsplit(value)
        hostname = parts.hostname
        if (
            parts.scheme.casefold() not in {"http", "https"}
            or not hostname
            or parts.username is not None
            or parts.password is not None
        ):
            raise ValueError
        canonicalize_hostname(hostname)
        port = parts.port
        if port is not None and port not in ALLOWED_PORTS:
            raise ValueError
        if parts.netloc.rsplit("@", 1)[-1].endswith(":"):
            raise ValueError
    except Exception:
        raise ValueError("announcement URL is invalid") from None


def _translate(notice: Announcement) -> ObservedItem:
    _validate_announcement(notice)
    key = canonical_key(notice)
    fields = {
        "source_id": notice.source_id,
        "title": notice.title,
        "agency": notice.agency.value,
        "region": notice.region,
        "housing_type": notice.housing_type.value,
        "target": notice.target,
        "announcement_date": notice.announcement_date.isoformat(),
        "application_start_date": (
            notice.application_start_date.isoformat()
            if notice.application_start_date is not None
            else None
        ),
        "application_end_date": (
            notice.application_end_date.isoformat()
            if notice.application_end_date is not None
            else None
        ),
        "url": notice.url,
    }
    return ObservedItem(item_id=f"announcement:{key}", fields=fields)


def _validation_error(detail: str) -> MonitorError:
    return MonitorError(ErrorClass.VALIDATION, "rental_housing", detail)
