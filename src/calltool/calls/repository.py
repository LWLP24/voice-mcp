from __future__ import annotations

from collections.abc import Collection
from typing import Protocol

from calltool.calls.models import (
    ActiveCallState,
    CallError,
    CallEvent,
    CallOutcome,
    CallPhase,
    CallRecord,
    CallStatus,
    InputRequest,
)


class CallRepository(Protocol):
    async def create_call(self, call: CallRecord) -> CallRecord: ...

    async def get_call(self, call_id: str) -> CallRecord | None: ...

    async def get_by_idempotency(self, principal_id: str, client_request_id: str) -> CallRecord | None: ...

    async def update_call(
        self,
        call_id: str,
        *,
        status: CallStatus | None = None,
        phase: CallPhase | None = None,
        state: ActiveCallState | None = None,
        outcome: CallOutcome | None = None,
        error: CallError | None = None,
        event_type: str,
        event_payload: dict[str, object] | None = None,
        expected_statuses: Collection[CallStatus] | None = None,
    ) -> CallRecord: ...

    async def list_events(self, call_id: str, *, after_sequence: int = 0) -> list[CallEvent]: ...

    async def count_in_progress(self, principal_id: str | None = None) -> int: ...

    async def count_dispatched(self) -> int: ...

    async def list_queued(self, *, limit: int) -> list[CallRecord]: ...

    async def create_input_request(self, request: InputRequest) -> InputRequest: ...

    async def get_input_request(self, request_id: str) -> InputRequest | None: ...

    async def resolve_input_request(
        self, call_id: str, request_id: str, response: dict[str, object]
    ) -> InputRequest: ...

    async def expire_input_request(
        self, call_id: str, request_id: str
    ) -> InputRequest: ...

    async def close(self) -> None: ...
