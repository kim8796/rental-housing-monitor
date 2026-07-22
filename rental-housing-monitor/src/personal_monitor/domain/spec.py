from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from urllib.parse import parse_qsl, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class SourceAdapterKind(StrEnum):
    OFFICIAL_API = "official_api"
    SCRAPLING = "scrapling"
    PYTHON_PLUGIN = "python_plugin"


class FetchStrategy(StrEnum):
    AUTO = "auto"
    HTTP = "http"
    DYNAMIC = "dynamic"
    STEALTHY = "stealthy"


class FieldType(StrEnum):
    TEXT = "text"
    INTEGER = "integer"
    DECIMAL = "decimal"
    KRW = "krw"
    DATE = "date"
    DATETIME = "datetime"
    BOOLEAN = "boolean"
    URL = "url"


class RuleKind(StrEnum):
    NEW_ITEM = "new_item"
    FIELD_CHANGED = "field_changed"
    NUMERIC_THRESHOLD = "numeric_threshold"
    STATUS_EQUALS = "status_equals"
    KEYWORD_MATCH = "keyword_match"


class MonitorStatus(StrEnum):
    ACTIVE = "active"
    PAUSED_USER = "paused_user"
    PAUSED_AUTH = "paused_auth"
    NEEDS_REVIEW = "needs_review"
    DISABLED = "disabled"


SAFE_SELECTOR = re.compile(r"^[\w\s.#>*+~:\-\[\]=\"'()/@|]+$")
SAFE_FIELD = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class FieldSpec(StrictModel):
    selector: Annotated[str, Field(min_length=1, max_length=500)]
    type: FieldType
    required: bool = True
    attribute: Annotated[str | None, Field(max_length=80)] = None
    pattern: Annotated[str | None, Field(max_length=300)] = None

    @field_validator("selector")
    @classmethod
    def selector_is_declarative(cls, value: str) -> str:
        if not SAFE_SELECTOR.fullmatch(value) or any(
            token in value for token in ("__", "import", "lambda", ";", "`", "${")
        ):
            raise ValueError("selector must be declarative CSS/XPath")
        return value


class ExtractSpec(StrictModel):
    item_scope: Annotated[str, Field(min_length=1, max_length=500)]
    fields: dict[Annotated[str, Field(pattern=SAFE_FIELD.pattern)], FieldSpec]

    @field_validator("item_scope")
    @classmethod
    def item_scope_is_declarative(cls, value: str) -> str:
        return FieldSpec.selector_is_declarative(value)


class ValidatorSpec(StrictModel):
    min_items: Annotated[int, Field(ge=0, le=10_000)] = 1
    max_items: Annotated[int, Field(ge=1, le=10_000)] = 1
    allowed_link_domains: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("allowed_link_domains")
    @classmethod
    def domains_are_exact_hosts(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            host = value.rstrip(".").casefold()
            if not re.fullmatch(r"[a-z0-9.-]+", host) or ".." in host or host.startswith("."):
                raise ValueError("allowed link domains must be exact ASCII hostnames")
            normalized.append(host)
        return sorted(set(normalized))

    @model_validator(mode="after")
    def item_range_is_ordered(self) -> ValidatorSpec:
        if self.min_items > self.max_items:
            raise ValueError("min_items must not exceed max_items")
        return self


class RuleSpec(StrictModel):
    kind: RuleKind
    field: str | None = None
    operator: Literal["lt", "lte", "eq", "gte", "gt"] | None = None
    value: str | int | float | bool | None = None
    keywords: list[str] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def required_arguments_match_kind(self) -> RuleSpec:
        if self.kind is RuleKind.NEW_ITEM:
            if (
                self.field is not None
                or self.operator is not None
                or self.value is not None
                or self.keywords
            ):
                raise ValueError("new_item takes no arguments")
        elif self.kind is RuleKind.FIELD_CHANGED:
            if (
                self.field is None
                or self.operator is not None
                or self.value is not None
                or self.keywords
            ):
                raise ValueError("field_changed requires only field")
        elif self.kind is RuleKind.NUMERIC_THRESHOLD:
            if (
                self.field is None
                or self.operator is None
                or isinstance(self.value, bool)
                or not isinstance(self.value, int | float)
                or self.keywords
            ):
                raise ValueError("numeric_threshold requires field, operator, and numeric value")
        elif self.kind is RuleKind.KEYWORD_MATCH:
            if (
                self.field is None
                or not self.keywords
                or self.operator is not None
                or self.value is not None
            ):
                raise ValueError("keyword_match requires field and keywords")
        elif self.kind is RuleKind.STATUS_EQUALS and (
            self.field is None or self.value is None or self.operator is not None or self.keywords
        ):
            raise ValueError("status_equals requires field and value")
        return self


class MonitorSpec(StrictModel):
    schema_version: Literal[1]
    owner_id: Annotated[str, Field(min_length=1, max_length=120)]
    name: Annotated[str, Field(min_length=1, max_length=120)]
    target_url: Annotated[str, Field(min_length=8, max_length=2048)]
    source_adapter: SourceAdapterKind
    adapter_ref: Annotated[str | None, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")] = None
    fetch_strategy: FetchStrategy = FetchStrategy.AUTO
    schedule: str = "0 */6 * * *"
    timezone: str = "Asia/Seoul"
    extract: ExtractSpec
    validators: ValidatorSpec
    rules: list[RuleSpec] = Field(min_length=1, max_length=20)
    notify_on_no_change: bool = False
    auth_profile_ref: Annotated[str | None, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")] = None

    @field_validator("target_url")
    @classmethod
    def url_has_public_web_scheme(cls, value: str) -> str:
        parts = urlsplit(value)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
        ):
            raise ValueError("target_url must be an http(s) URL without userinfo")
        sensitive_names = {
            "access_token",
            "api_key",
            "apikey",
            "auth",
            "key",
            "session",
            "signature",
            "token",
        }
        if any(
            name.casefold() in sensitive_names
            for name, _ in parse_qsl(parts.query, keep_blank_values=True)
        ):
            raise ValueError("target_url query contains a credential-like parameter")
        return value

    @model_validator(mode="after")
    def schedule_is_valid_and_bounded(self) -> MonitorSpec:
        try:
            zone = ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as error:
            raise ValueError("unknown timezone") from error
        base = datetime(2026, 1, 1, tzinfo=zone)
        iterator = croniter(self.schedule, base)
        first = iterator.get_next(datetime)
        second = iterator.get_next(datetime)
        if (second - first).total_seconds() < 900:
            raise ValueError("schedule interval must be at least 15 minutes")
        if self.source_adapter is SourceAdapterKind.SCRAPLING and self.adapter_ref is not None:
            raise ValueError("scrapling does not accept adapter_ref")
        if self.source_adapter is not SourceAdapterKind.SCRAPLING and self.adapter_ref is None:
            raise ValueError("official_api and python_plugin require adapter_ref")
        return self
