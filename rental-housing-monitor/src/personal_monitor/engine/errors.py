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


class MonitorError(RuntimeError):
    def __init__(self, error_class: ErrorClass, stage: str, safe_detail: str) -> None:
        super().__init__(safe_detail)
        self.error_class = error_class
        self.stage = stage
        self.safe_detail = safe_detail
