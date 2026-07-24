from __future__ import annotations

from typing import Final

from .contracts import IntentRequest, PlanRequest, RepairRequest, RequestModel

_INTENT_PROMPT: Final = (
    "입력 JSON에서 사용자의 모니터 관리 의도만 분류하라. "
    "GCP·구글 클라우드 크레딧, 잔액, 비용, 사용량 조회는 billing_status이며 "
    "모니터 ID나 다른 필드를 채우지 않는다. 제공된 출력 스키마의 JSON만 반환하라."
)
_PLAN_PROMPT: Final = (
    "검증된 입력 JSON과 정제된 문서만 사용해 MonitorSpec을 작성하라. "
    "제공된 출력 스키마의 JSON만 반환하라."
)
_REPAIR_PROMPT: Final = (
    "현재 MonitorSpec의 안전한 검증 실패만 최소 수정하라. 제공된 출력 스키마의 JSON만 반환하라."
)


def prompt_for(request_type: type[RequestModel]) -> str:
    if request_type is IntentRequest:
        return _INTENT_PROMPT
    if request_type is PlanRequest:
        return _PLAN_PROMPT
    if request_type is RepairRequest:
        return _REPAIR_PROMPT
    raise TypeError("unsupported AI request")
