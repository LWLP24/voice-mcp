from __future__ import annotations

import asyncio
from collections.abc import Collection
from datetime import datetime
from typing import Literal

from calltool.calls.errors import (
    CallNotFoundError,
    InputRequestNotFoundError,
    InvalidStateTransitionError,
)
from calltool.calls.models import (
    ActiveCallState,
    CallDirection,
    CallError,
    CallEvent,
    CallOutcome,
    CallPhase,
    CallRecord,
    CallStatus,
    InputRequest,
    TranscriptTurn,
    utc_now,
)


class MemoryCallRepository:
    def __init__(self) -> None:
        self._calls: dict[str, CallRecord] = {}
        self._events: dict[str, list[CallEvent]] = {}
        self._transcripts: dict[str, list[TranscriptTurn]] = {}
        self._input_requests: dict[str, InputRequest] = {}
        self._lock = asyncio.Lock()

    async def create_call(self, call: CallRecord) -> CallRecord:
        async with self._lock:
            if call.client_request_id:
                existing = self._find_idempotent(call.principal_id, call.client_request_id)
                if existing is not None:
                    return existing.model_copy(deep=True)
            self._calls[call.id] = call.model_copy(deep=True)
            self._events[call.id] = [
                CallEvent(call_id=call.id, sequence=1, type="call.created", payload={})
            ]
            self._transcripts[call.id] = []
            return call.model_copy(deep=True)

    async def get_call(self, call_id: str) -> CallRecord | None:
        call = self._calls.get(call_id)
        return call.model_copy(deep=True) if call else None

    async def get_by_idempotency(
        self, principal_id: str, client_request_id: str
    ) -> CallRecord | None:
        call = self._find_idempotent(principal_id, client_request_id)
        return call.model_copy(deep=True) if call else None

    def _find_idempotent(self, principal_id: str, client_request_id: str) -> CallRecord | None:
        return next(
            (
                call
                for call in self._calls.values()
                if call.principal_id == principal_id and call.client_request_id == client_request_id
            ),
            None,
        )

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
    ) -> CallRecord:
        async with self._lock:
            call = self._calls.get(call_id)
            if call is None:
                raise CallNotFoundError(call_id)
            if expected_statuses is not None and call.status not in expected_statuses:
                raise InvalidStateTransitionError(
                    f"Expected one of {sorted(expected_statuses)}, got {call.status}"
                )
            now = utc_now()
            updates: dict[str, object] = {"updated_at": now}
            if status is not None:
                updates["status"] = status
                if status is CallStatus.CONNECTED and call.connected_at is None:
                    updates["connected_at"] = now
                if status.terminal:
                    updates["ended_at"] = now
                    updates["phase"] = None
            if phase is not None:
                updates["phase"] = phase
            if state is not None:
                updates["state"] = state
            if outcome is not None:
                updates["outcome"] = outcome
            if error is not None:
                updates["error"] = error
            updated = call.model_copy(update=updates, deep=True)
            self._calls[call_id] = updated
            sequence = len(self._events[call_id]) + 1
            self._events[call_id].append(
                CallEvent(
                    call_id=call_id,
                    sequence=sequence,
                    type=event_type,
                    payload=event_payload or {},
                    created_at=now,
                )
            )
            return updated.model_copy(deep=True)

    async def list_events(self, call_id: str, *, after_sequence: int = 0) -> list[CallEvent]:
        if call_id not in self._calls:
            raise CallNotFoundError(call_id)
        return [
            event.model_copy(deep=True)
            for event in self._events[call_id]
            if event.sequence > after_sequence
        ]

    async def list_calls(
        self,
        *,
        principal_id: str,
        direction: CallDirection | None,
        status: CallStatus | None,
        phone_number: str | None,
        target_name: str | None,
        started_after: datetime | None,
        started_before: datetime | None,
        cursor_started_at: datetime | None,
        cursor_call_id: str | None,
        limit: int,
    ) -> list[CallRecord]:
        calls = [
            call
            for call in self._calls.values()
            if call.principal_id == principal_id
            and (direction is None or call.direction is direction)
            and (status is None or call.status is status)
            and (phone_number is None or call.target_number == phone_number)
            and (
                target_name is None
                or (
                    call.request.target.name is not None
                    and target_name.casefold() in call.request.target.name.casefold()
                )
            )
            and (started_after is None or call.started_at >= started_after)
            and (started_before is None or call.started_at < started_before)
            and (
                cursor_started_at is None
                or cursor_call_id is None
                or (call.started_at, call.id) < (cursor_started_at, cursor_call_id)
            )
        ]
        calls.sort(key=lambda call: (call.started_at, call.id), reverse=True)
        return [call.model_copy(deep=True) for call in calls[:limit]]

    async def append_transcript_turn(
        self,
        call_id: str,
        *,
        role: Literal["user", "assistant"],
        text: str,
        interrupted: bool = False,
        last_remote_utterance: str | None = None,
    ) -> TranscriptTurn:
        transcript = text.strip()
        if not transcript:
            raise ValueError("transcript text must not be empty")
        async with self._lock:
            call = self._calls.get(call_id)
            if call is None:
                raise CallNotFoundError(call_id)
            now = utc_now()
            sequence = len(self._transcripts[call_id]) + 1
            turn = TranscriptTurn(
                call_id=call_id,
                sequence=sequence,
                role=role,
                text=transcript,
                interrupted=interrupted,
                created_at=now,
            )
            self._transcripts[call_id].append(turn)
            if last_remote_utterance is not None:
                state = call.state.model_copy(
                    update={"last_remote_utterance": last_remote_utterance},
                    deep=True,
                )
                self._calls[call_id] = call.model_copy(
                    update={"state": state, "updated_at": now}, deep=True
                )
            event_sequence = len(self._events[call_id]) + 1
            self._events[call_id].append(
                CallEvent(
                    call_id=call_id,
                    sequence=event_sequence,
                    type=f"call.{role}_transcript_final",
                    payload={"transcript": transcript, "interrupted": interrupted},
                    created_at=now,
                )
            )
            return turn.model_copy(deep=True)

    async def list_transcript(self, call_id: str) -> list[TranscriptTurn]:
        if call_id not in self._calls:
            raise CallNotFoundError(call_id)
        return [turn.model_copy(deep=True) for turn in self._transcripts[call_id]]

    async def count_in_progress(self, principal_id: str | None = None) -> int:
        return sum(
            1
            for call in self._calls.values()
            if not call.status.terminal
            and (principal_id is None or call.principal_id == principal_id)
        )

    async def count_dispatched(self) -> int:
        return sum(
            1
            for call in self._calls.values()
            if not call.status.terminal
            and (
                call.status not in {CallStatus.CREATED, CallStatus.VALIDATING, CallStatus.QUEUED}
                or call.state.dispatch_id is not None
            )
        )

    async def list_queued(self, *, limit: int) -> list[CallRecord]:
        queued = sorted(
            (
                call
                for call in self._calls.values()
                if call.status is CallStatus.QUEUED and call.state.dispatch_id is None
            ),
            key=lambda call: call.created_at,
        )
        return [call.model_copy(deep=True) for call in queued[:limit]]

    async def create_input_request(self, request: InputRequest) -> InputRequest:
        async with self._lock:
            self._input_requests[request.id] = request.model_copy(deep=True)
            return request.model_copy(deep=True)

    async def get_input_request(self, request_id: str) -> InputRequest | None:
        request = self._input_requests.get(request_id)
        return request.model_copy(deep=True) if request else None

    async def resolve_input_request(
        self, call_id: str, request_id: str, response: dict[str, object]
    ) -> InputRequest:
        async with self._lock:
            request = self._input_requests.get(request_id)
            if request is None or request.call_id != call_id:
                raise InputRequestNotFoundError(request_id)
            if request.status != "pending":
                return request.model_copy(deep=True)
            request = request.model_copy(
                update={"status": "resolved", "response": response, "resolved_at": utc_now()},
                deep=True,
            )
            self._input_requests[request_id] = request
            return request.model_copy(deep=True)

    async def expire_input_request(self, call_id: str, request_id: str) -> InputRequest:
        async with self._lock:
            request = self._input_requests.get(request_id)
            if request is None or request.call_id != call_id:
                raise InputRequestNotFoundError(request_id)
            if request.status != "pending":
                return request.model_copy(deep=True)
            request = request.model_copy(
                update={"status": "expired", "resolved_at": utc_now()}, deep=True
            )
            self._input_requests[request_id] = request
            return request.model_copy(deep=True)

    async def close(self) -> None:
        return None
