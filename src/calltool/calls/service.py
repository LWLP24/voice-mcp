from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
from datetime import datetime, timedelta

from calltool.calls.dispatcher import CallDispatcher
from calltool.calls.errors import (
    CallNotFoundError,
    DispatchError,
    InputRequestNotFoundError,
    InvalidCursorError,
    InvalidStateTransitionError,
)
from calltool.calls.ids import new_id
from calltool.calls.models import (
    ActiveCallState,
    CallConversation,
    CallCreateRequest,
    CallDirection,
    CallError,
    CallEvent,
    CallListPage,
    CallListRequest,
    CallPermissions,
    CallPhase,
    CallRecord,
    CallStatus,
    CallTarget,
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

    async def create_inbound_call(
        self,
        *,
        caller_number: str,
        called_number: str,
        sip_participant_identity: str,
        room_name: str,
        sip_call_id: str | None,
        principal_id: str,
    ) -> CallRecord:
        """Persist an already-connected inbound SIP participant as a normal call."""
        inbound = self._settings.config.calls.inbound
        caller_number = caller_number.strip()[:64] or "anonymous"
        called_number = called_number.strip()[:64] or "unknown"
        client_request_id = None
        if sip_call_id:
            digest = hashlib.sha256(sip_call_id.encode()).hexdigest()
            client_request_id = f"inbound:{digest}"
            existing = await self._repository.get_by_idempotency(principal_id, client_request_id)
            if existing is not None:
                return existing

        context = {
            "direction": CallDirection.INBOUND.value,
            "caller_number": caller_number,
            "called_number": called_number,
            "organization_name": inbound.organization_name,
        }
        if sip_call_id:
            context["sip_call_id"] = sip_call_id[:256]
        request = CallCreateRequest(
            target=CallTarget(phone_number=caller_number, name="Eingehender Anrufer"),
            objective=inbound.objective,
            constraints=inbound.constraints,
            context=context,
            permissions=CallPermissions(),
            client_request_id=client_request_id,
        )
        now = utc_now()
        call = CallRecord(
            id=new_id("call"),
            principal_id=principal_id,
            direction=CallDirection.INBOUND,
            client_request_id=client_request_id,
            status=CallStatus.CREATED,
            target_number=caller_number,
            request=request,
            state=ActiveCallState(
                objective=request.objective,
                constraints=request.constraints,
                permissions=request.permissions,
                facts={"called_number": called_number},
                room_name=room_name,
                sip_participant_identity=sip_participant_identity,
            ),
            created_at=now,
            updated_at=now,
        )
        created_id = call.id
        call = await self._repository.create_call(call)
        if call.id != created_id:
            return call
        return await self._repository.update_call(
            call.id,
            status=CallStatus.CONNECTED,
            phase=CallPhase.OPENING,
            event_type="call.connected",
            event_payload={
                "direction": CallDirection.INBOUND.value,
                "room_name": room_name,
                "sip_call_id": sip_call_id or "",
            },
            expected_statuses={CallStatus.CREATED},
        )

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
                        error=CallError(code="dispatch_failed", message=str(exc), retryable=True),
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

    async def list_calls(self, query: CallListRequest, *, principal_id: str) -> CallListPage:
        cursor_started_at, cursor_call_id = _decode_call_cursor(query.cursor)
        calls = await self._repository.list_calls(
            principal_id=principal_id,
            direction=query.direction,
            status=query.status,
            phone_number=query.phone_number,
            target_name=query.target_name,
            started_after=query.started_after,
            started_before=query.started_before,
            cursor_started_at=cursor_started_at,
            cursor_call_id=cursor_call_id,
            limit=query.limit + 1,
        )
        page = calls[: query.limit]
        next_cursor = None
        if len(calls) > query.limit and page:
            last_call = page[-1]
            next_cursor = _encode_call_cursor(last_call.started_at, last_call.id)
        return CallListPage(calls=page, next_cursor=next_cursor)

    async def get_conversation(self, call_id: str, *, principal_id: str) -> CallConversation:
        call = await self.get_call(call_id, principal_id=principal_id)
        transcript = await self._repository.list_transcript(call.id)
        return CallConversation(call=call, transcript=transcript)

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

    async def get_input_request(self, input_request_id: str) -> InputRequest | None:
        return await self._repository.get_input_request(input_request_id)

    async def request_input(self, call_id: str, question: str, options: list[str]) -> InputRequest:
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


def _encode_call_cursor(started_at: datetime, call_id: str) -> str:
    payload = json.dumps(
        {"started_at": started_at.isoformat(), "call_id": call_id},
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_call_cursor(cursor: str | None) -> tuple[datetime | None, str | None]:
    if cursor is None:
        return None, None
    try:
        padding = "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(cursor + padding))
        started_at = datetime.fromisoformat(payload["started_at"])
        call_id = payload["call_id"]
        if started_at.tzinfo is None or not isinstance(call_id, str) or not call_id:
            raise ValueError
    except (binascii.Error, KeyError, TypeError, UnicodeError, ValueError) as exc:
        raise InvalidCursorError("Invalid call list cursor") from exc
    return started_at, call_id
