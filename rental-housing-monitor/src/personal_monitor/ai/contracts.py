from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from personal_monitor.domain.spec import (
    FetchStrategy,
    FieldType,
    MonitorSpec,
    RuleKind,
    SourceAdapterKind,
)

BoundId = Annotated[str, Field(min_length=1, max_length=128)]
BoundText = Annotated[str, Field(min_length=1, max_length=2_000)]
BoundOptionalText = Annotated[str | None, Field(max_length=2_000)]
BoundSummary = Annotated[str, Field(min_length=1, max_length=1_000)]
BoundMonitorId = Annotated[str, Field(min_length=1, max_length=128)]
BoundUrl = Annotated[str | None, Field(max_length=2_048)]


def _require_all_properties(schema: dict[str, object]) -> None:
    properties = schema.get("properties")
    if isinstance(properties, dict):
        schema["required"] = list(properties)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class RedactedModel(StrictModel):
    def __repr__(self) -> str:
        return f"<{type(self).__name__} redacted>"

    def __str__(self) -> str:
        return self.__repr__()


class IntentKind(StrEnum):
    CREATE = "create"
    LIST = "list"
    UPDATE = "update"
    PAUSE = "pause"
    RESUME = "resume"
    DELETE = "delete"
    STATUS = "status"
    BILLING_STATUS = "billing_status"
    UNKNOWN = "unknown"


class IntentRequest(RedactedModel):
    request_id: BoundId
    owner_id: BoundId
    message: BoundText
    monitor_summaries: list[BoundSummary] = Field(max_length=100)


class IntentResult(RedactedModel):
    model_config = ConfigDict(json_schema_extra=_require_all_properties)

    kind: IntentKind
    target_monitor_ids: list[BoundMonitorId] = Field(max_length=10)
    target_url: BoundUrl = None
    discovery_query: Annotated[str | None, Field(max_length=300)] = None
    condition_text: BoundOptionalText = None
    schedule_text: Annotated[str | None, Field(max_length=500)] = None
    clarification: BoundOptionalText = None
    confidence: Annotated[float, Field(ge=0, le=1)]

    @field_validator("kind", mode="before")
    @classmethod
    def parse_kind(cls, value: object) -> object:
        return IntentKind(value) if isinstance(value, str) else value


class UrlDiscoveryRequest(RedactedModel):
    request_id: BoundId
    query: Annotated[str, Field(min_length=3, max_length=300)]


class UrlCandidate(StrictModel):
    name: Annotated[str, Field(min_length=1, max_length=120)]
    url: Annotated[str, Field(min_length=1, max_length=2_048)]


class UrlDiscoveryResult(RedactedModel):
    model_config = ConfigDict(json_schema_extra=_require_all_properties)

    candidates: list[UrlCandidate] = Field(max_length=3)
    clarification: Annotated[str | None, Field(max_length=500)] = None


class PlanRequest(RedactedModel):
    request_id: BoundId
    owner_id: BoundId
    message: BoundText
    intent: IntentResult
    sanitized_document: Annotated[str, Field(max_length=40_000)]
    observed_preview_values: list[BoundSummary] = Field(default_factory=list, max_length=100)


class _PlanFieldSpec(StrictModel):
    name: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
    selector: Annotated[str, Field(min_length=1, max_length=500)]
    type: FieldType
    required: bool
    attribute: Annotated[str | None, Field(max_length=80)]
    pattern: Annotated[str | None, Field(max_length=300)]

    @field_validator("type", mode="before")
    @classmethod
    def parse_field_type(cls, value: object) -> object:
        return FieldType(value) if isinstance(value, str) else value


class _PlanExtractSpec(StrictModel):
    item_scope: Annotated[str, Field(min_length=1, max_length=500)]
    fields: list[_PlanFieldSpec] = Field(min_length=1, max_length=50)


class _PlanValidatorSpec(StrictModel):
    min_items: Annotated[int, Field(ge=0, le=10_000)]
    max_items: Annotated[int, Field(ge=1, le=10_000)]
    allowed_link_domains: list[str] = Field(max_length=50)


class _PlanRuleSpec(StrictModel):
    kind: RuleKind
    field: str | None
    operator: Literal["lt", "lte", "eq", "gte", "gt"] | None
    value: str | int | float | bool | None
    keywords: list[str] = Field(max_length=50)

    @field_validator("kind", mode="before")
    @classmethod
    def parse_rule_kind(cls, value: object) -> object:
        return RuleKind(value) if isinstance(value, str) else value


class _PlanMonitorSpec(StrictModel):
    schema_version: Literal[1]
    owner_id: Annotated[str, Field(min_length=1, max_length=120)]
    name: Annotated[str, Field(min_length=1, max_length=120)]
    target_url: Annotated[str, Field(min_length=8, max_length=2_048)]
    source_adapter: SourceAdapterKind
    adapter_ref: Annotated[str | None, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
    fetch_strategy: FetchStrategy
    schedule: str
    timezone: str
    extract: _PlanExtractSpec
    validators: _PlanValidatorSpec
    rules: list[_PlanRuleSpec] = Field(min_length=1, max_length=20)
    notify_on_no_change: bool
    auth_profile_ref: Annotated[
        str | None,
        Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$"),
    ]

    @field_validator("source_adapter", mode="before")
    @classmethod
    def parse_source_adapter(cls, value: object) -> object:
        return SourceAdapterKind(value) if isinstance(value, str) else value

    @field_validator("fetch_strategy", mode="before")
    @classmethod
    def parse_fetch_strategy(cls, value: object) -> object:
        return FetchStrategy(value) if isinstance(value, str) else value

    @classmethod
    def from_monitor_spec(cls, value: MonitorSpec) -> _PlanMonitorSpec:
        return cls.model_validate(cls.transport_payload(value.model_dump(mode="json")))

    @staticmethod
    def transport_payload(value: Mapping[str, object]) -> dict[str, object]:
        payload = dict(value)
        extract_value = payload.get("extract")
        if not isinstance(extract_value, Mapping):
            return payload
        extract = dict(extract_value)
        fields_value = extract.get("fields")
        if not isinstance(fields_value, Mapping):
            return payload
        fields: list[dict[str, object]] = []
        for name, field_value in sorted(fields_value.items()):
            if type(name) is not str or not isinstance(field_value, Mapping):
                return payload
            field = dict(field_value)
            if "name" in field:
                return payload
            fields.append({"name": name, **field})
        extract["fields"] = fields
        payload["extract"] = extract
        return payload

    def to_monitor_spec(self) -> MonitorSpec:
        payload = self.model_dump(mode="json")
        extract = payload["extract"]
        mapped: dict[str, object] = {}
        for field in extract["fields"]:
            item = dict(field)
            name = item.pop("name")
            if name in mapped:
                raise ValueError("duplicate field name")
            mapped[name] = item
        extract["fields"] = mapped
        return MonitorSpec.model_validate(payload)


class _SpecResult(RedactedModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        serialize_by_alias=True,
    )

    transport_spec: _PlanMonitorSpec = Field(alias="spec")

    @field_validator("transport_spec", mode="before")
    @classmethod
    def encode_monitor_spec(cls, value: object) -> object:
        if type(value) is MonitorSpec:
            return _PlanMonitorSpec.from_monitor_spec(value)
        if isinstance(value, Mapping):
            return _PlanMonitorSpec.transport_payload(value)
        return value

    @property
    def spec(self) -> MonitorSpec:
        return self.transport_spec.to_monitor_spec()

    @spec.setter
    def spec(self, value: MonitorSpec) -> None:
        object.__setattr__(
            self,
            "transport_spec",
            _PlanMonitorSpec.from_monitor_spec(value),
        )


class PlanResult(_SpecResult):
    explanation: Annotated[str, Field(min_length=1, max_length=1_000)]


class RepairRequest(RedactedModel):
    request_id: BoundId
    owner_id: BoundId
    current_spec: MonitorSpec
    validation_failures: list[BoundSummary] = Field(min_length=1, max_length=100)
    sanitized_fragment: Annotated[str, Field(max_length=40_000)]


class RepairResult(_SpecResult):
    explanation: Annotated[str, Field(min_length=1, max_length=1_000)]
    changed_fields: list[
        Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_.]{0,127}$")]
    ] = Field(max_length=50)


RequestModel = IntentRequest | UrlDiscoveryRequest | PlanRequest | RepairRequest
ResultModel = IntentResult | UrlDiscoveryResult | PlanResult | RepairResult


class WorkerRequest(RedactedModel):
    kind: Literal["intent", "url_discovery", "plan", "repair"]
    request: RequestModel
    model: Literal["gpt-5.6-terra", "gpt-5.6-sol"] = "gpt-5.6-terra"
    effort: Literal["medium", "high"] = "medium"

    @model_validator(mode="after")
    def discriminator_matches_request(self) -> WorkerRequest:
        expected = {
            "intent": IntentRequest,
            "url_discovery": UrlDiscoveryRequest,
            "plan": PlanRequest,
            "repair": RepairRequest,
        }[self.kind]
        if type(self.request) is not expected:
            raise ValueError("worker request type mismatch")
        if (self.model, self.effort) not in {
            ("gpt-5.6-terra", "medium"),
            ("gpt-5.6-sol", "high"),
        }:
            raise ValueError("worker model configuration mismatch")
        return self


class WorkerFailure(StrictModel):
    ok: Literal[False]
    error_code: Literal[
        "invalid_request",
        "auth_failed",
        "protocol_failed",
        "busy",
        "worker_failed",
    ]


def request_kind(value: RequestModel) -> str:
    if type(value) is IntentRequest:
        return "intent"
    if type(value) is UrlDiscoveryRequest:
        return "url_discovery"
    if type(value) is PlanRequest:
        return "plan"
    if type(value) is RepairRequest:
        return "repair"
    raise TypeError("unsupported AI request")


def result_type_for(value: RequestModel) -> type[ResultModel]:
    if type(value) is IntentRequest:
        return IntentResult
    if type(value) is UrlDiscoveryRequest:
        return UrlDiscoveryResult
    if type(value) is PlanRequest:
        return PlanResult
    if type(value) is RepairRequest:
        return RepairResult
    raise TypeError("unsupported AI request")
