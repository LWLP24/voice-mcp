from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Collection
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

import structlog

from calltool.calls.models import (
    ActiveCallState,
    AMDResult,
    CallDirection,
    CallOutcome,
    CallPermissions,
    CallPhase,
    CallRecord,
    CallStatus,
    Candidate,
    ColdTransferState,
    Commitment,
    VoiceSessionState,
)
from calltool.calls.repository import CallRepository

logger = structlog.get_logger(__name__)

PersistenceOperation = Callable[[], Awaitable[None]]


@dataclass(frozen=True)
class _PersistenceItem:
    operation: PersistenceOperation
    description: str
    required: bool
    completion: asyncio.Future[None] | None = None


@dataclass
class ActiveCallContext:
    """Mutable per-call hot state owned by exactly one LiveKit worker job."""

    call_id: str
    direction: CallDirection
    objective: str
    constraints: list[str]
    permissions: CallPermissions
    facts: dict[str, Any]
    candidates: list[Candidate]
    commitments: list[Commitment]
    pending_input_request_id: str | None
    phase: CallPhase | None
    status: CallStatus
    connected_at: datetime | None
    room_name: str | None
    dispatch_id: str | None
    sip_participant_identity: str | None
    last_remote_utterance: str | None
    voice_session: VoiceSessionState
    _repository: CallRepository = field(repr=False)
    _queue: asyncio.Queue[_PersistenceItem | None] = field(init=False, repr=False)
    _writer_task: asyncio.Task[None] = field(init=False, repr=False)
    _state_lock: asyncio.Lock = field(init=False, repr=False)
    _required_failure: Exception | None = field(default=None, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        self._state_lock = asyncio.Lock()
        self._queue = asyncio.Queue()
        self._writer_task = asyncio.create_task(
            self._run_persistence(),
            name=f"calltool-state-writer-{self.call_id}",
        )

    @classmethod
    def from_call(
        cls,
        call: CallRecord,
        repository: CallRepository,
    ) -> ActiveCallContext:
        state = call.state
        return cls(
            call_id=call.id,
            direction=call.direction,
            objective=state.objective,
            constraints=list(state.constraints),
            permissions=state.permissions.model_copy(deep=True),
            facts=deepcopy(state.facts),
            candidates=[candidate.model_copy(deep=True) for candidate in state.candidates],
            commitments=[commitment.model_copy(deep=True) for commitment in state.commitments],
            pending_input_request_id=state.pending_input_request_id,
            phase=call.phase,
            status=call.status,
            connected_at=call.connected_at,
            room_name=state.room_name,
            dispatch_id=state.dispatch_id,
            sip_participant_identity=state.sip_participant_identity,
            last_remote_utterance=state.last_remote_utterance,
            voice_session=state.voice_session.model_copy(deep=True),
            _repository=repository,
        )

    def snapshot(self) -> ActiveCallState:
        return ActiveCallState(
            objective=self.objective,
            constraints=list(self.constraints),
            permissions=self.permissions.model_copy(deep=True),
            facts=deepcopy(self.facts),
            candidates=[candidate.model_copy(deep=True) for candidate in self.candidates],
            commitments=[commitment.model_copy(deep=True) for commitment in self.commitments],
            pending_input_request_id=self.pending_input_request_id,
            room_name=self.room_name,
            dispatch_id=self.dispatch_id,
            sip_participant_identity=self.sip_participant_identity,
            last_remote_utterance=self.last_remote_utterance,
            voice_session=self.voice_session.model_copy(deep=True),
        )

    def apply_call(self, call: CallRecord) -> None:
        if call.id != self.call_id:
            raise ValueError("cannot apply a different call to ActiveCallContext")
        self.status = call.status
        self.phase = call.phase
        self.connected_at = call.connected_at
        self.pending_input_request_id = call.state.pending_input_request_id
        self.room_name = call.state.room_name
        self.dispatch_id = call.state.dispatch_id
        self.sip_participant_identity = call.state.sip_participant_identity
        self.voice_session = call.state.voice_session.model_copy(deep=True)

    def record_fact(self, key: str, value: Any) -> None:
        self.facts[key] = deepcopy(value)

    def add_candidate(self, candidate: Candidate) -> None:
        self.candidates.append(candidate.model_copy(deep=True))

    def find_commitment(self, commit_id: str) -> Commitment | None:
        return next((item for item in self.commitments if item.id == commit_id), None)

    def add_commitment(self, commitment: Commitment) -> None:
        self.commitments.append(commitment.model_copy(deep=True))

    def configure_voice_session(
        self,
        *,
        turn_detection_mode: Literal["realtime_llm", "livekit_v1_mini"],
        interruption_mode: Literal["vad"],
        ivr_detection_enabled: bool,
        turn_unlikely_threshold: float | None = None,
        turn_backchannel_threshold: float | None = None,
    ) -> None:
        self.voice_session.turn_detection_mode = turn_detection_mode
        self.voice_session.interruption_mode = interruption_mode
        self.voice_session.ivr_detection_enabled = ivr_detection_enabled
        self.voice_session.turn_unlikely_threshold = turn_unlikely_threshold
        self.voice_session.turn_backchannel_threshold = turn_backchannel_threshold

    def record_amd_result(self, result: AMDResult) -> None:
        self.voice_session.amd = result.model_copy(deep=True)

    def record_transfer(self, transfer: ColdTransferState) -> None:
        self.voice_session.transfer = transfer.model_copy(deep=True)

    def record_model_usage(self, usage: list[dict[str, Any]]) -> None:
        self.voice_session.model_usage = deepcopy(usage)

    def record_session_report(self, report: dict[str, Any]) -> None:
        self.voice_session.session_report = deepcopy(report)

    @asynccontextmanager
    async def state_transaction(self) -> AsyncIterator[None]:
        async with self._state_lock:
            yield

    async def persist_commitment(self, commitment: Commitment, event_type: str) -> None:
        state = self.snapshot()
        state.commitments.append(commitment.model_copy(deep=True))

        async def operation() -> None:
            await self._repository.update_call(
                self.call_id,
                state=state,
                event_type=event_type,
                event_payload=commitment.model_dump(mode="json"),
                expected_statuses={CallStatus.ACTIVE, CallStatus.INPUT_REQUIRED},
            )

        await self._submit_and_wait(operation, description=event_type, required=True)
        self.add_commitment(commitment)

    def persist_state(
        self,
        event_type: str,
        event_payload: dict[str, object] | None = None,
        *,
        expected_statuses: Collection[CallStatus] | None = None,
    ) -> None:
        state = self.snapshot()
        statuses = set(expected_statuses) if expected_statuses is not None else None

        async def operation() -> None:
            await self._repository.update_call(
                self.call_id,
                state=state,
                event_type=event_type,
                event_payload=event_payload,
                expected_statuses=statuses,
            )

        self._submit(operation, description=event_type, required=True)

    async def persist_state_durable(
        self,
        event_type: str,
        event_payload: dict[str, object] | None = None,
        *,
        expected_statuses: Collection[CallStatus] | None = None,
    ) -> None:
        state = self.snapshot()
        statuses = set(expected_statuses) if expected_statuses is not None else None

        async def operation() -> None:
            await self._repository.update_call(
                self.call_id,
                state=state,
                event_type=event_type,
                event_payload=event_payload,
                expected_statuses=statuses,
            )

        await self._submit_and_wait(operation, description=event_type, required=True)

    async def persist_completion(self, outcome: CallOutcome) -> None:
        self.phase = CallPhase.CLOSING
        state = self.snapshot()

        async def operation() -> None:
            await self._repository.update_call(
                self.call_id,
                status=CallStatus.COMPLETING,
                phase=CallPhase.CLOSING,
                state=state,
                outcome=outcome,
                event_type="call.completing",
                event_payload={"success": outcome.success, "reason": outcome.reason},
                expected_statuses={CallStatus.ACTIVE, CallStatus.INPUT_REQUIRED},
            )

        await self._submit_and_wait(
            operation,
            description="call.completing",
            required=True,
        )
        self.status = CallStatus.COMPLETING

    def persist_event(
        self,
        event_type: str,
        payload: dict[str, object] | None = None,
    ) -> None:
        async def operation() -> None:
            await self._repository.update_call(
                self.call_id,
                event_type=event_type,
                event_payload=payload,
            )

        self._submit(operation, description=event_type, required=False)

    def persist_transcript(
        self,
        *,
        role: Literal["user", "assistant"],
        text: str,
        interrupted: bool = False,
    ) -> None:
        transcript = text.strip()
        if not transcript:
            return
        last_remote_utterance = None
        if role == "user":
            self.last_remote_utterance = transcript
            last_remote_utterance = transcript

        async def operation() -> None:
            await self._repository.append_transcript_turn(
                self.call_id,
                role=role,
                text=transcript,
                interrupted=interrupted,
                last_remote_utterance=last_remote_utterance,
            )

        self._submit(
            operation,
            description=f"call.{role}_transcript_final",
            required=True,
        )

    async def flush(self) -> None:
        async def barrier() -> None:
            return None

        await self._submit_and_wait(barrier, description="state.flush", required=True)
        if self._required_failure is not None:
            raise RuntimeError("ActiveCallContext could not persist durable state") from (
                self._required_failure
            )

    async def close(self) -> None:
        if self._closed:
            return
        flush_error: Exception | None = None
        try:
            await self.flush()
        except Exception as exc:
            flush_error = exc
        self._closed = True
        self._queue.put_nowait(None)
        await self._writer_task
        if flush_error is not None:
            raise flush_error

    @property
    def persistence_backlog(self) -> int:
        return self._queue.qsize()

    def _submit(
        self,
        operation: PersistenceOperation,
        *,
        description: str,
        required: bool,
    ) -> None:
        if self._closed:
            raise RuntimeError("ActiveCallContext is closed")
        self._queue.put_nowait(
            _PersistenceItem(
                operation=operation,
                description=description,
                required=required,
            )
        )

    async def _submit_and_wait(
        self,
        operation: PersistenceOperation,
        *,
        description: str,
        required: bool,
    ) -> None:
        if self._closed:
            raise RuntimeError("ActiveCallContext is closed")
        completion = asyncio.get_running_loop().create_future()
        self._queue.put_nowait(
            _PersistenceItem(
                operation=operation,
                description=description,
                required=required,
                completion=completion,
            )
        )
        await completion

    async def _run_persistence(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                if item is None:
                    return
                if item.required and self._required_failure is not None:
                    error = RuntimeError(
                        "required persistence skipped after an earlier durable write failed"
                    )
                    if item.completion is not None and not item.completion.done():
                        item.completion.set_exception(error)
                    continue
                try:
                    await item.operation()
                except Exception as exc:
                    if item.required and self._required_failure is None:
                        self._required_failure = exc
                    if item.completion is not None and not item.completion.done():
                        item.completion.set_exception(exc)
                    logger.exception(
                        "active call persistence failed",
                        call_id=self.call_id,
                        operation=item.description,
                    )
                else:
                    if item.completion is not None and not item.completion.done():
                        item.completion.set_result(None)
            finally:
                self._queue.task_done()
