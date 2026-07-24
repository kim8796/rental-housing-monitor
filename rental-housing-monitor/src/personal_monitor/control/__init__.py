from .actions import ActionDenied, ConsumedAction, PendingAction, PendingActionService
from .messages import ControlReply

__all__ = [
    "ActionDenied",
    "ConsumedAction",
    "ControlReply",
    "ControlService",
    "PendingAction",
    "PendingActionService",
]


def __getattr__(name: str):
    if name == "ControlService":
        from .service import ControlService

        return ControlService
    raise AttributeError(name)
