from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime

import httpx
import pytest

from personal_monitor.billing.bigquery import (
    BigQueryBillingError,
    BigQueryBillingSource,
    MetadataTokenProvider,
)

NOW = datetime(2026, 7, 24, 3, 10, tzinfo=UTC)


def test_source_uses_fixed_metadata_and_bounded_parameterized_query() -> None:
    metadata_requests: list[httpx.Request] = []
    query_requests: list[httpx.Request] = []

    def metadata_handler(request: httpx.Request) -> httpx.Response:
        metadata_requests.append(request)
        return httpx.Response(
            200,
            json={"access_token": "private-access-token", "expires_in": 3599},
        )

    def query_handler(request: httpx.Request) -> httpx.Response:
        query_requests.append(request)
        return httpx.Response(
            200,
            json={
                "jobComplete": True,
                "schema": {
                    "fields": [
                        {"name": "project_id", "type": "STRING"},
                        {"name": "project_name", "type": "STRING"},
                        {"name": "month_cost", "type": "FLOAT"},
                        {"name": "promotion_consumed", "type": "FLOAT"},
                        {"name": "recent_7d_consumed", "type": "FLOAT"},
                    ]
                },
                "rows": [
                    {
                        "f": [
                            {"v": "project-a"},
                            {"v": "주 프로젝트"},
                            {"v": "4000.125"},
                            {"v": "4954.74"},
                            {"v": "1400"},
                        ]
                    },
                    {
                        "f": [
                            {"v": "project-b"},
                            {"v": "보조 프로젝트"},
                            {"v": "500"},
                            {"v": "4954.74"},
                            {"v": "1400"},
                        ]
                    },
                ],
                "totalBytesProcessed": "12345",
            },
        )

    token_provider = MetadataTokenProvider(
        transport=httpx.MockTransport(metadata_handler),
        clock=lambda: NOW,
    )
    source = BigQueryBillingSource(
        "local-social-native-wlk-0720",
        "billing_monitor",
        token_provider,
        transport=httpx.MockTransport(query_handler),
    )

    result = asyncio.run(source.fetch(start_on=date(2026, 7, 8), now=NOW))
    second = asyncio.run(source.fetch(start_on=date(2026, 7, 8), now=NOW))

    assert len(metadata_requests) == 1
    assert metadata_requests[0].url == (
        "http://169.254.169.254/computeMetadata/v1/instance/"
        "service-accounts/default/token"
    )
    assert metadata_requests[0].headers["metadata-flavor"] == "Google"
    assert len(query_requests) == 2
    request = query_requests[0]
    assert request.url == (
        "https://bigquery.googleapis.com/bigquery/v2/projects/"
        "local-social-native-wlk-0720/queries"
    )
    assert request.headers["authorization"] == "Bearer private-access-token"
    body = json.loads(request.content)
    assert body["useLegacySql"] is False
    assert body["maximumBytesBilled"] == "100000000"
    assert "`local-social-native-wlk-0720.billing_monitor.gcp_billing_export_v1_*`" in body[
        "query"
    ]
    assert "private-access-token" not in body["query"]
    assert [item["name"] for item in body["queryParameters"]] == [
        "as_of",
        "credit_start",
        "invoice_month",
    ]
    assert result == second
    assert result.promotion_consumed_micros == 4_954_740_000
    assert result.recent_7d_consumed_micros == 1_400_000_000
    assert [(item.project_id, item.cost_micros) for item in result.projects] == [
        ("project-a", 4_000_125_000),
        ("project-b", 500_000_000),
    ]

    asyncio.run(source.aclose())


@pytest.mark.parametrize(
    "payload",
    (
        {"jobComplete": False},
        {"jobComplete": True, "schema": {"fields": []}, "rows": []},
        {
            "jobComplete": True,
            "schema": {
                "fields": [
                    {"name": "project_id", "type": "STRING"},
                    {"name": "project_name", "type": "STRING"},
                    {"name": "month_cost", "type": "NUMERIC"},
                    {"name": "promotion_consumed", "type": "NUMERIC"},
                    {"name": "recent_7d_consumed", "type": "NUMERIC"},
                ]
            },
            "rows": [
                {
                    "f": [
                        {"v": "project-a"},
                        {"v": "주 프로젝트"},
                        {"v": "not-money"},
                        {"v": "4954.74"},
                        {"v": "1400"},
                    ]
                }
            ],
        },
    ),
)
def test_source_rejects_incomplete_or_malformed_results_without_leaks(
    payload: dict[str, object],
) -> None:
    class TokenProvider:
        async def token(self) -> str:
            return "private-access-token"

    source = BigQueryBillingSource(
        "local-social-native-wlk-0720",
        "billing_monitor",
        TokenProvider(),
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=payload)),
    )

    with pytest.raises(BigQueryBillingError) as caught:
        asyncio.run(source.fetch(start_on=date(2026, 7, 8), now=NOW))

    assert str(caught.value) == "billing export query failed"
    assert "private-access-token" not in repr(caught.value)
