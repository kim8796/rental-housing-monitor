from __future__ import annotations

from enum import StrEnum


class ErrorClass(StrEnum):
    TRANSIENT_NETWORK = "transient_network"
    AUTHENTICATION = "authentication"
    STRUCTURE = "structure"
    VALIDATION = "validation"
    POLICY = "policy"
    DELIVERY = "delivery"
    INTERNAL = "internal"


class FailureCode(StrEnum):
    REQUIRED_CONTENT_ABSENT = "required_content_absent"


class MonitorError(RuntimeError):
    def __init__(
        self,
        error_class: ErrorClass,
        stage: str,
        safe_detail: str,
        *,
        code: FailureCode | None = None,
    ) -> None:
        super().__init__(safe_detail)
        self.error_class = error_class
        self.stage = stage
        self.safe_detail = safe_detail
        self.code = code


class FetchError(MonitorError):
    """A safe structured fetch failure without response or credential material."""

    def __init__(
        self,
        error_class: ErrorClass,
        safe_detail: str,
        *,
        status: int,
        retry_after_seconds: float | None = None,
        detected_interstitial: bool = False,
    ) -> None:
        super().__init__(error_class, "fetch", safe_detail)
        self.status = status
        self.retry_after_seconds = retry_after_seconds
        self.detected_interstitial = detected_interstitial
