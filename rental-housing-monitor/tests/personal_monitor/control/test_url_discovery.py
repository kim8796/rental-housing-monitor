from __future__ import annotations

import asyncio

from personal_monitor.ai.contracts import (
    UrlCandidate,
    UrlDiscoveryRequest,
    UrlDiscoveryResult,
)
from personal_monitor.control.planner import PlanningFailed
from personal_monitor.control.url_discovery import UrlDiscoveryService
from personal_monitor.storage import RegistryRepository, open_database

OWNER = "telegram-user:7"
QUERY = "서울주택도시공사 임대주택 모집공고"


class FakeWorker:
    def __init__(self, results: list[object]) -> None:
        self.results = results
        self.calls: list[tuple[UrlDiscoveryRequest, str, str]] = []

    async def run(
        self,
        request: UrlDiscoveryRequest,
        *,
        model: str,
        effort: str,
    ) -> object:
        self.calls.append((request, model, effort))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class FakePlanner:
    def __init__(self, results: dict[str, object]) -> None:
        self.results = results
        self.calls: list[tuple[str, str]] = []

    async def validate_discovery_url(self, owner_id: str, url: str) -> object:
        self.calls.append((owner_id, url))
        result = self.results[url]
        if isinstance(result, BaseException):
            raise result
        return result


def run(value: object):
    return asyncio.run(value)  # type: ignore[arg-type]


def registry() -> RegistryRepository:
    connection = open_database(":memory:")
    value = RegistryRepository(connection)
    value.create_user(OWNER, 7)
    return value


def test_saved_alias_is_revalidated_before_reuse_without_web_search() -> None:
    stored = "https://www.i-sh.co.kr/notices"
    storage = registry()
    storage.upsert_url_alias(OWNER, QUERY, stored)
    planner = FakePlanner({stored: stored})
    worker = FakeWorker([])
    service = UrlDiscoveryService(worker, planner, storage)

    outcome = run(service.discover(OWNER, QUERY))

    assert [(item.name, item.url) for item in outcome.candidates] == [(QUERY, stored)]
    assert planner.calls == [(OWNER, stored)]
    assert worker.calls == []
    storage.connection.close()


def test_search_candidates_are_deduplicated_and_filtered_by_real_validation() -> None:
    first = "https://official.example/notices"
    duplicate = "https://official.example/notices/"
    rejected = "https://invalid.example/notices"
    worker = FakeWorker(
        [
            UrlDiscoveryResult(
                candidates=[
                    UrlCandidate(name="공식 공고", url=first),
                    UrlCandidate(name="공식 공고 중복", url=duplicate),
                    UrlCandidate(name="검증 실패", url=rejected),
                ],
                clarification=None,
            )
        ]
    )
    planner = FakePlanner(
        {
            first: first,
            duplicate: first,
            rejected: PlanningFailed(),
        }
    )
    storage = registry()
    service = UrlDiscoveryService(worker, planner, storage)

    outcome = run(service.discover(OWNER, QUERY))

    assert [(item.name, item.url) for item in outcome.candidates] == [("공식 공고", first)]
    assert outcome.clarification is None
    assert len(worker.calls) == 1
    assert worker.calls[0][0].query == QUERY
    storage.connection.close()


def test_ambiguous_search_returns_clarification_without_inventing_candidate() -> None:
    worker = FakeWorker(
        [
            UrlDiscoveryResult(
                candidates=[],
                clarification="어느 지역의 주택공사 게시판인지 알려주세요.",
            )
        ]
    )
    planner = FakePlanner({})
    storage = registry()
    service = UrlDiscoveryService(worker, planner, storage)

    outcome = run(service.discover(OWNER, QUERY))

    assert outcome.candidates == ()
    assert outcome.clarification == "어느 지역의 주택공사 게시판인지 알려주세요."
    assert planner.calls == []
    storage.connection.close()


def test_selected_candidate_is_revalidated_again_before_preview() -> None:
    selected = "https://official.example/notices"
    planner = FakePlanner({selected: selected})
    storage = registry()
    service = UrlDiscoveryService(FakeWorker([]), planner, storage)

    result = run(service.validate_selected_url(OWNER, selected))

    assert result == selected
    assert planner.calls == [(OWNER, selected)]
    storage.connection.close()
