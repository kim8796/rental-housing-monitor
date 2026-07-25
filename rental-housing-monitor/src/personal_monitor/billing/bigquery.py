from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Final
from zoneinfo import ZoneInfo

import httpx

from .models import BillingAggregate, ProjectSpend

_METADATA_TOKEN_URL: Final = (
    "http://169.254.169.254/computeMetadata/v1/instance/"
    "service-accounts/default/token"
)
_BIGQUERY_ROOT: Final = "https://bigquery.googleapis.com/bigquery/v2"
_SEOUL: Final = ZoneInfo("Asia/Seoul")
_PROJECT_RE: Final = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]\Z")
_DATASET_RE: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,1023}\Z")
_TOKEN_RE: Final = re.compile(r"[\x21-\x7e]{20,8192}\Z")
_MAX_RESPONSE_BYTES: Final = 1024 * 1024
_FIELDS: Final = (
    ("project_id", "STRING"),
    ("project_name", "STRING"),
    ("month_cost", "FLOAT"),
    ("promotion_consumed", "FLOAT"),
    ("recent_7d_consumed", "FLOAT"),
)
_QUERY: Final = """
WITH usage AS (
  SELECT
    project.id AS project_id,
    COALESCE(project.name, project.id) AS project_name,
    DATE(usage_start_time, "Asia/Seoul") AS usage_day,
    cost,
    (
      SELECT COALESCE(SUM(IF(credit.type = "PROMOTION", credit.amount, 0)), 0)
      FROM UNNEST(credits) AS credit
    ) AS promotion_credit
  FROM `{table}`
  WHERE usage_start_time >= TIMESTAMP(@credit_start, "Asia/Seoul")
    AND usage_start_time < TIMESTAMP(DATE_ADD(@as_of, INTERVAL 1 DAY), "Asia/Seoul")
),
credit_total AS (
  SELECT
    COALESCE(-SUM(promotion_credit), 0) AS promotion_consumed,
    COALESCE(
      -SUM(
        IF(
          usage_day >= DATE_SUB(@as_of, INTERVAL 6 DAY),
          promotion_credit,
          0
        )
      ),
      0
    ) AS recent_7d_consumed
  FROM usage
),
project_month AS (
  SELECT
    project_id,
    project_name,
    GREATEST(SUM(cost), 0) AS month_cost
  FROM usage
  WHERE project_id IS NOT NULL
    AND FORMAT_DATE("%Y%m", usage_day) = @invoice_month
  GROUP BY project_id, project_name
  HAVING month_cost > 0
)
SELECT
  project_month.project_id,
  project_month.project_name,
  project_month.month_cost,
  credit_total.promotion_consumed,
  credit_total.recent_7d_consumed
FROM credit_total
LEFT JOIN project_month ON TRUE
ORDER BY month_cost DESC, project_id
""".strip()


class BigQueryBillingError(RuntimeError):
    __slots__ = ()

    def __init__(self) -> None:
        super().__init__("billing export query failed")

    def __repr__(self) -> str:
        return "BigQueryBillingError(<redacted>)"


class MetadataTokenProvider:
    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: object = lambda: datetime.now(UTC),
    ) -> None:
        if not callable(clock):
            raise TypeError("invalid metadata token clock")
        self._clock = clock
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(10.0),
            trust_env=False,
            follow_redirects=False,
        )
        self._token: str | None = None
        self._expires_at: datetime | None = None

    def __repr__(self) -> str:
        return "<MetadataTokenProvider redacted>"

    async def token(self) -> str:
        now = self._now()
        if (
            self._token is not None
            and self._expires_at is not None
            and now < self._expires_at - timedelta(minutes=5)
        ):
            return self._token
        try:
            response = await self._client.get(
                _METADATA_TOKEN_URL,
                headers={"Metadata-Flavor": "Google", "Accept": "application/json"},
            )
            if (
                response.status_code != 200
                or len(response.content) > 16 * 1024
                or response.headers.get("metadata-flavor") not in {None, "Google"}
            ):
                raise ValueError
            payload = response.json()
            token = payload.get("access_token") if type(payload) is dict else None
            expires_in = payload.get("expires_in") if type(payload) is dict else None
            if (
                type(token) is not str
                or _TOKEN_RE.fullmatch(token) is None
                or type(expires_in) is not int
                or not 600 <= expires_in <= 86_400
            ):
                raise ValueError
        except Exception:
            raise BigQueryBillingError from None
        self._token = token
        self._expires_at = now + timedelta(seconds=expires_in)
        return token

    async def aclose(self) -> None:
        self._token = None
        self._expires_at = None
        await self._client.aclose()

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise BigQueryBillingError
        return value.astimezone(UTC)


class BigQueryBillingSource:
    def __init__(
        self,
        project_id: str,
        dataset_id: str,
        token_provider: object,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        maximum_bytes_billed: int = 100_000_000,
    ) -> None:
        token = getattr(token_provider, "token", None)
        if (
            type(project_id) is not str
            or _PROJECT_RE.fullmatch(project_id) is None
            or type(dataset_id) is not str
            or _DATASET_RE.fullmatch(dataset_id) is None
            or not callable(token)
            or type(maximum_bytes_billed) is not int
            or not 1 <= maximum_bytes_billed <= 1_000_000_000
        ):
            raise ValueError("invalid billing export configuration")
        self._project_id = project_id
        self._dataset_id = dataset_id
        self._token_provider = token_provider
        self._token = token
        self._maximum_bytes_billed = maximum_bytes_billed
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(40.0),
            trust_env=False,
            follow_redirects=False,
        )

    def __repr__(self) -> str:
        return "<BigQueryBillingSource redacted>"

    async def fetch(self, *, start_on: date, now: datetime) -> BillingAggregate:
        if (
            type(start_on) is not date
            or not isinstance(now, datetime)
            or now.tzinfo is None
            or now.utcoffset() is None
        ):
            raise ValueError("invalid billing export query window")
        observed_at = now.astimezone(UTC)
        local_day = observed_at.astimezone(_SEOUL).date()
        query = _QUERY.format(
            table=f"{self._project_id}.{self._dataset_id}.gcp_billing_export_v1_*"
        )
        body = {
            "query": query,
            "useLegacySql": False,
            "maximumBytesBilled": str(self._maximum_bytes_billed),
            "timeoutMs": 30_000,
            "parameterMode": "NAMED",
            "queryParameters": [
                _date_parameter("as_of", local_day),
                _date_parameter("credit_start", start_on),
                _string_parameter("invoice_month", local_day.strftime("%Y%m")),
            ],
        }
        try:
            access_token = await self._token()
            if type(access_token) is not str or _TOKEN_RE.fullmatch(access_token) is None:
                raise ValueError
            response = await self._client.post(
                f"{_BIGQUERY_ROOT}/projects/{self._project_id}/queries",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                json=body,
            )
            if response.status_code != 200 or len(response.content) > _MAX_RESPONSE_BYTES:
                raise ValueError
            payload = response.json()
            return _parse_result(payload, observed_at=observed_at)
        except BigQueryBillingError:
            raise
        except Exception:
            raise BigQueryBillingError from None

    async def aclose(self) -> None:
        await self._client.aclose()


def _date_parameter(name: str, value: date) -> dict[str, object]:
    return {
        "name": name,
        "parameterType": {"type": "DATE"},
        "parameterValue": {"value": value.isoformat()},
    }


def _string_parameter(name: str, value: str) -> dict[str, object]:
    return {
        "name": name,
        "parameterType": {"type": "STRING"},
        "parameterValue": {"value": value},
    }


def _parse_result(payload: object, *, observed_at: datetime) -> BillingAggregate:
    if type(payload) is not dict or payload.get("jobComplete") is not True:
        raise BigQueryBillingError
    schema = payload.get("schema")
    fields = schema.get("fields") if type(schema) is dict else None
    if type(fields) is not list or len(fields) != len(_FIELDS):
        raise BigQueryBillingError
    actual_fields: list[tuple[object, object]] = []
    for field in fields:
        if type(field) is not dict:
            raise BigQueryBillingError
        actual_fields.append((field.get("name"), field.get("type")))
    if tuple(actual_fields) != _FIELDS:
        raise BigQueryBillingError
    rows = payload.get("rows")
    if type(rows) is not list or not 1 <= len(rows) <= 1_000:
        raise BigQueryBillingError
    projects: list[ProjectSpend] = []
    promotion_consumed: int | None = None
    recent_consumed: int | None = None
    for row in rows:
        values = _row_values(row)
        row_promotion = _money_micros(values[3])
        row_recent = _money_micros(values[4])
        if (
            promotion_consumed is not None
            and (promotion_consumed != row_promotion or recent_consumed != row_recent)
        ):
            raise BigQueryBillingError
        promotion_consumed = row_promotion
        recent_consumed = row_recent
        if values[0] is None and values[1] is None and values[2] is None:
            continue
        if not all(type(values[index]) is str for index in range(3)):
            raise BigQueryBillingError
        projects.append(
            ProjectSpend(
                project_id=values[0],
                project_name=values[1],
                cost_micros=_money_micros(values[2]),
            )
        )
    if promotion_consumed is None or recent_consumed is None:
        raise BigQueryBillingError
    return BillingAggregate(
        observed_at=observed_at,
        promotion_consumed_micros=promotion_consumed,
        recent_7d_consumed_micros=recent_consumed,
        projects=tuple(projects),
    )


def _row_values(row: object) -> tuple[object, ...]:
    cells = row.get("f") if type(row) is dict else None
    if type(cells) is not list or len(cells) != len(_FIELDS):
        raise BigQueryBillingError
    values: list[object] = []
    for cell in cells:
        if type(cell) is not dict or set(cell) != {"v"}:
            raise BigQueryBillingError
        value = cell["v"]
        if value is not None and type(value) is not str:
            raise BigQueryBillingError
        values.append(value)
    return tuple(values)


def _money_micros(value: object) -> int:
    if type(value) is not str or not 1 <= len(value) <= 64:
        raise BigQueryBillingError
    try:
        decimal = Decimal(value)
        micros = decimal * 1_000_000
    except InvalidOperation:
        raise BigQueryBillingError from None
    if not decimal.is_finite() or micros != micros.to_integral_value():
        raise BigQueryBillingError
    result = int(micros)
    if not 0 <= result <= 2**63 - 1:
        raise BigQueryBillingError
    return result
