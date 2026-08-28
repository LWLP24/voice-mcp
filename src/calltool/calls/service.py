from __future__ import annotations

import asyncio
from datetime import timedelta

from calltool.calls.dispatcher import CallDispatcher
from calltool.calls.errors import (
    CallNotFoundError,
    DispatchError,
    InputRequestNotFoundError,
    InvalidStateTransitionError,
)
from calltool.calls.ids import new_id
from calltool.calls.models import (
    ActiveCallState,
    CallCreateRequest,
    CallError,
    CallEvent,
    CallPhase,
    CallRecord,
    CallStatus,
    InputRequest,
    utc_now,
)
from calltool.calls.repository import CallRepository
from calltool.calls.state import can_transition
from calltool.config import Settings
from calltool.policy.engine import PolicyEngine


class CallService:
    def __init__(
        self,
        repository: CallRepository,
        dispatcher: CallDispatcher,
        policy: PolicyEngine,
        settings: Settings,
    ) -> None:
        self._repository = repository
        self._dispatcher = dispatcher
        self._policy = policy
        self._settings = settings
        self._dispatch_lock = asyncio.Lock()

    async def create_call(self, request: CallCreateRequest, *, principal_id: str) -> CallRecord:
        if request.client_request_id:
            existing = await self._repository.get_by_idempotency(
                principal_id, request.client_request_id
            )
            if existing is not None:
                return existing

        normalized_number = self._policy.validate_target(request.target.phone_number)
        request = request.model_copy(
            update={"target": request.target.model_copy(update={"phone_number": normalized_number})}
        )
        now = utc_now()
        call = CallRecord(
            id=new_id("call"),
            principal_id=principal_id,
            client_request_id=request.client_request_id,
            status=CallStatus.CREATED,
            target_number=normalized_number,
            request=request,
            state=ActiveCallState(
                objective=request.objective,
                constraints=request.constraints,
                permissions=request.permissions,
            ),
            created_at=now,
            updated_at=now,
        )
        created_id = call.id
        call = await self._repository.create_call(call)
        if call.id != created_id:
            return call
        call = await self.transition(
            call.id,
            CallStatus.VALIDATING,
            event_type="call.validating",
        )
        call = await self.transition(call.id, CallStatus.QUEUED, event_type="call.queued")

        await self.dispatch_queued()
        return await self.get_call(call.id, principal_id=principal_id)

    async def dispatch_queued(self) -> int:
        """Fill free call slots while leaving excess requests durably queued."""
        dispatched = 0
        async with self._dispatch_lock:
            active = await self._repository.count_dispatched()
            capacity = max(0, self._settings.config.calls.max_concurrent - active)
            if capacity == 0:
                return 0
            for call in await self._repository.list_queued(limit=capacity):
                try:
                    result = await self._dispatcher.dispatch(call)
                    state = call.state.model_copy(
                        update={"room_name": result.room_name, "dispatch_id": result.dispatch_id}
                    )
                    await self._repository.update_call(
                        call.id,
                        state=state,
                        event_type="call.dispatched",
                        event_payload={
                            "room_name": result.room_name,
                            "dispatch_id": result.dispatch_id,
                        },
                        expected_statuses={CallStatus.QUEUED},
                    )
                    dispatched += 1
                except DispatchError as exc:
                    await self._repository.update_call(
                        call.id,
                        status=CallStatus.FAILED,
                        error=CallError(
                            code="dispatch_failed", message=str(exc), retryable=True
                        ),
                        event_type="call.failed",
                        event_payload={"code": "dispatch_failed"},
                        expected_statuses={CallStatus.QUEUED},
                    )
        return dispatched

    async def get_call(self, call_id: str, *, principal_id: str | None = None) -> CallRecord:
        call = await self._repository.get_call(call_id)
        if call is None or (principal_id is not None and call.principal_id != principal_id):
            raise CallNotFoundError(call_id)
        return call

    async def list_events(
        self, call_id: str, *, principal_id: str, after_sequence: int = 0
    ) -> list[CallEvent]:
        await self.get_call(call_id, principal_id=principal_id)
        return await self._repository.list_events(call_id, after_sequence=after_sequence)

    async def cancel_call(self, call_id: str, *, principal_id: str) -> CallRecord:
        call = await self.get_call(call_id, principal_id=principal_id)
        if call.status.terminal:
            return call
        await self._dispatcher.cancel(call)
        return await self._repository.update_call(
            call.id,
            status=CallStatus.CANCELLED,
            event_type="call.cancelled",
            expected_statuses={call.status},
        )

    async def respond(
        self,
        call_id: str,
        input_request_id: str,
        response: dict[str, object],
        *,
        principal_id: str,
    ) -> CallRecord:
        call = await self.get_call(call_id, principal_id=principal_id)
        input_request = await self._repository.get_input_request(input_request_id)
        if input_request is None or input_request.call_id != call.id:
            raise InputRequestNotFoundError(input_request_id)
        if input_request.status != "pending":
            return call
        await self._repository.resolve_input_request(call.id, input_request_id, response)
        state = call.state.model_copy(update={"pending_input_request_id": None})
        return await self._repository.update_call(
            call.id,
            status=CallStatus.ACTIVE,
            state=state,
            event_type="call.input_received",
            event_payload={"input_request_id": input_request_id},
            expected_statuses={CallStatus.INPUT_REQUIRED},
        )

    async def request_input(
        self, call_id: str, question: str, options: list[str]
    ) -> InputRequest:
        call = await self.get_call(call_id)
        request = InputRequest(
            id=new_id("input"),
            call_id=call.id,
            question=question,
            options=options,
            expires_at=utc_now()
            + timedelta(seconds=self._settings.config.calls.user_input_timeout_seconds),
        )
        await self._repository.create_input_request(request)
        state = call.state.model_copy(update={"pending_input_request_id": request.id})
        await self._repository.update_call(
            call.id,
            status=CallStatus.INPUT_REQUIRED,
            state=state,
            event_type="call.input_required",
            event_payload={
                "input_request_id": request.id,
                "question": question,
                "options": options,
            },
            expected_statuses={CallStatus.ACTIVE},
        )
        return request

    async def expire_input(self, call_id: str, input_request_id: str) -> CallRecord:
        call = await self.get_call(call_id)
        request = await self._repository.expire_input_request(call.id, input_request_id)
        if request.status != "expired" or call.status is not CallStatus.INPUT_REQUIRED:
            return call
        state = call.state.model_copy(update={"pending_input_request_id": None})
        return await self._repository.update_call(
            call.id,
            status=CallStatus.ACTIVE,
            state=state,
            event_type="call.input_timeout",
            event_payload={"input_request_id": input_request_id},
            expected_statuses={CallStatus.INPUT_REQUIRED},
        )

    async def transition(
        self,
        call_id: str,
        status: CallStatus,
        *,
        phase: CallPhase | None = None,
        event_type: str,
        event_payload: dict[str, object] | None = None,
    ) -> CallRecord:
        call = await self.get_call(call_id)
        if not can_transition(call.status, status):
            raise InvalidStateTransitionError(f"Cannot transition {call.status} to {status}")
        return await self._repository.update_call(
            call.id,
            status=status,
            phase=phase,
            event_type=event_type,
            event_payload=event_payload,
            expected_statuses={call.status},
        )

    async def close(self) -> None:
        await self._dispatcher.close()
        await self._repository.close()
