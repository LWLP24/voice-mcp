from __future__ import annotations

from calltool.calls.models import CallStatus

_TRANSITIONS: dict[CallStatus, set[CallStatus]] = {
    CallStatus.CREATED: {CallStatus.VALIDATING, CallStatus.CANCELLED, CallStatus.FAILED},
    CallStatus.VALIDATING: {CallStatus.QUEUED, CallStatus.CANCELLED, CallStatus.FAILED},
    CallStatus.QUEUED: {CallStatus.PREWARMING, CallStatus.CANCELLED, CallStatus.FAILED},
    CallStatus.PREWARMING: {CallStatus.DIALING, CallStatus.CANCELLED, CallStatus.FAILED},
    CallStatus.DIALING: {CallStatus.RINGING, CallStatus.CONNECTED, CallStatus.CANCELLED, CallStatus.FAILED},
    CallStatus.RINGING: {CallStatus.CONNECTED, CallStatus.CANCELLED, CallStatus.FAILED},
    CallStatus.CONNECTED: {CallStatus.ACTIVE, CallStatus.CANCELLED, CallStatus.FAILED},
    CallStatus.ACTIVE: {
        CallStatus.INPUT_REQUIRED,
        CallStatus.COMPLETING,
        CallStatus.CANCELLED,
        CallStatus.FAILED,
    },
    CallStatus.INPUT_REQUIRED: {
        CallStatus.ACTIVE,
        CallStatus.COMPLETING,
        CallStatus.CANCELLED,
        CallStatus.FAILED,
    },
    CallStatus.COMPLETING: {CallStatus.COMPLETED, CallStatus.CANCELLED, CallStatus.FAILED},
    CallStatus.COMPLETED: set(),
    CallStatus.FAILED: set(),
    CallStatus.CANCELLED: set(),
}


def can_transition(current: CallStatus, target: CallStatus) -> bool:
    return current == target or target in _TRANSITIONS[current]
