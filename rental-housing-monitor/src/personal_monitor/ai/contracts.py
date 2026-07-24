from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from personal_monitor.domain.spec import MonitorSpec

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
    condition_text: BoundOptionalText = None
    schedule_text: Annotated[str | None, Field(max_length=500)] = None
    clarification: BoundOptionalText = None
    confidence: Annotated[float, Field(ge=0, le=1)]

    @field_validator("kind", mode="before")
    @classmethod
    def parse_kind(cls, value: object) -> object:
        return IntentKind(value) if isinstance(value, str) else value


class PlanRequest(RedactedModel):
    request_id: BoundId
    owner_id: BoundId
    message: BoundText
    intent: IntentResult
    sanitized_document: Annotated[str, Field(max_length=40_000)]
    observed_preview_values: list[BoundSummary] = Field(default_factory=list, max_length=100)


class PlanResult(RedactedModel):
    spec: MonitorSpec
    explanation: Annotated[str, Field(min_length=1, max_length=1_000)]


class RepairRequest(RedactedModel):
    request_id: BoundId
    owner_id: BoundId
    current_spec: MonitorSpec
    validation_failures: list[BoundSummary] = Field(min_length=1, max_length=100)
    sanitized_fragment: Annotated[str, Field(max_length=40_000)]


class RepairResult(RedactedModel):
    spec: MonitorSpec
    explanation: Annotated[str, Field(min_length=1, max_length=1_000)]
    changed_fields: list[
        Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_.]{0,127}$")]
    ] = Field(max_length=50)


RequestModel = IntentRequest | PlanRequest | RepairRequest
ResultModel = IntentResult | PlanResult | RepairResult


class WorkerRequest(RedactedModel):
    kind: Literal["intent", "plan", "repair"]
    request: RequestModel
    model: Literal["gpt-5.6-terra", "gpt-5.6-sol"] = "gpt-5.6-terra"
    effort: Literal["medium", "high"] = "medium"

    @model_validator(mode="after")
    def discriminator_matches_request(self) -> WorkerRequest:
        expected = {
            "intent": IntentRequest,
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
    if type(value) is PlanRequest:
        return "plan"
    if type(value) is RepairRequest:
        return "repair"
    raise TypeError("unsupported AI request")


def result_type_for(value: RequestModel) -> type[ResultModel]:
    if type(value) is IntentRequest:
        return IntentResult
    if type(value) is PlanRequest:
        return PlanResult
    if type(value) is RepairRequest:
        return RepairResult
    raise TypeError("unsupported AI request")
