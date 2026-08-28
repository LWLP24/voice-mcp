from __future__ import annotations


class CallToolError(Exception):
    """Base class for expected application errors."""


class CallNotFoundError(CallToolError):
    def __init__(self, call_id: str) -> None:
        super().__init__(f"Call not found: {call_id}")
        self.call_id = call_id


class InputRequestNotFoundError(CallToolError):
    def __init__(self, request_id: str) -> None:
        super().__init__(f"Input request not found: {request_id}")
        self.request_id = request_id


class InvalidStateTransitionError(CallToolError):
    pass


class PolicyDeniedError(CallToolError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ConcurrencyLimitError(CallToolError):
    pass


class DispatchError(CallToolError):
    pass
