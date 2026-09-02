from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from calltool.calls.models import (
    ActiveCallState,
    CallCreateRequest,
    CallDirection,
    CallPermissions,
    CallRecord,
    CallStatus,
    CallTarget,
    TransferStatus,
)
from calltool.config import IVRConfig, PolicyConfig
from calltool.policy.engine import PolicyEngine
from calltool.realtime.active_calls import ActiveCallContext
from calltool.storage.memory import MemoryCallRepository
from calltool.voice.tools import ToolRuntime, TransferResult, build_tools
from calltool.worker.dialer import hangup_sip_participant


def make_call(*, may_transfer: bool = False) -> CallRecord:
    permissions = CallPermissions(may_transfer=may_transfer)
    request = CallCreateRequest(
        target=CallTarget(phone_number="+49301234567", name="Praxis"),
        objective="Termin vereinbaren",
        permissions=permissions,
    )
    return CallRecord(
        id="call_telephony_tools",
        principal_id="test",
        status=CallStatus.ACTIVE,
        target_number=request.target.phone_number,
        request=request,
        state=ActiveCallState(objective=request.objective, permissions=permissions),
    )


def find_tool(runtime: ToolRuntime, name: str):
    return next(
        tool
        for tool in build_tools(runtime, direction=CallDirection.OUTBOUND)
        if tool.info.name == name
    )


@pytest.mark.asyncio
async def test_dtmf_uses_livekit_allowlist_and_audits_without_digits() -> None:
    repository = MemoryCallRepository()
    call = make_call()
    await repository.create_call(call)
    context = ActiveCallContext.from_call(call, repository)
    room = MagicMock()
    room.local_participant.publish_dtmf = AsyncMock()
    runtime = ToolRuntime(
        call_id=call.id,
        context=context,
        service=MagicMock(),
        policy=PolicyEngine(PolicyConfig()),
        room=room,
        finish_event=asyncio.Event(),
        ivr_config=IVRConfig(enabled=True, inter_digit_delay_seconds=0, audit_digits=False),
        ivr_enabled=True,
    )

    result = await find_tool(runtime, "send_dtmf")("12#")
    invalid = await find_tool(runtime, "send_dtmf")("12X")
    await context.close()

    assert result == {"sent": True, "digit_count": 3}
    assert invalid == {"sent": False, "reason": "invalid_dtmf"}
    assert room.local_participant.publish_dtmf.await_count == 3
    events = await repository.list_events(call.id)
    event = next(item for item in events if item.type == "call.dtmf_sent")
    assert event.payload == {"digit_count": 3}


@pytest.mark.asyncio
async def test_cold_transfer_validates_persists_and_completes_call() -> None:
    repository = MemoryCallRepository()
    call = make_call(may_transfer=True)
    await repository.create_call(call)
    context = ActiveCallContext.from_call(call, repository)

    async def transfer_handler(target_number: str) -> TransferResult:
        assert target_number == "+49309876543"
        return TransferResult(
            status=TransferStatus.SUCCESSFUL,
            transfer_id="transfer_123",
            reason="completed",
            sip_status="200:OK",
        )

    finish_event = asyncio.Event()
    runtime = ToolRuntime(
        call_id=call.id,
        context=context,
        service=MagicMock(),
        policy=PolicyEngine(PolicyConfig()),
        room=MagicMock(),
        finish_event=finish_event,
        cold_transfer_enabled=True,
        transfer_handler=transfer_handler,
    )

    result = await find_tool(runtime, "cold_transfer")("+49 30 9876543")
    await context.close()

    assert result["transferred"] is True
    assert finish_event.is_set()
    stored = await repository.get_call(call.id)
    assert stored is not None
    assert stored.status is CallStatus.COMPLETING
    assert stored.outcome is not None
    assert stored.outcome.reason == "cold_transfer_successful"
    assert stored.state.voice_session.transfer is not None
    assert stored.state.voice_session.transfer.status is TransferStatus.SUCCESSFUL
    assert [event.type for event in await repository.list_events(call.id)] == [
        "call.created",
        "call.transfer_requested",
        "call.transfer_succeeded",
        "call.completing",
    ]


@pytest.mark.asyncio
async def test_finish_call_requests_deterministic_farewell_before_hangup() -> None:
    repository = MemoryCallRepository()
    call = make_call()
    await repository.create_call(call)
    context = ActiveCallContext.from_call(call, repository)
    runtime = ToolRuntime(
        call_id=call.id,
        context=context,
        service=MagicMock(),
        policy=PolicyEngine(PolicyConfig()),
        room=MagicMock(),
        finish_event=asyncio.Event(),
    )

    result = await find_tool(runtime, "finish_call")(
        True,
        "test_completed",
        "Der Test wurde abgeschlossen.",
    )
    await context.close()

    assert result["farewell_scheduled"] is True
    assert runtime.farewell_required is True
    assert runtime.finish_event.is_set()


@pytest.mark.asyncio
async def test_cold_transfer_is_not_exposed_without_permission() -> None:
    call = make_call(may_transfer=False)
    runtime = ToolRuntime(
        call_id=call.id,
        context=MagicMock(),
        service=MagicMock(),
        policy=PolicyEngine(PolicyConfig()),
        room=MagicMock(),
        finish_event=asyncio.Event(),
        cold_transfer_enabled=False,
    )

    names = [tool.info.name for tool in build_tools(runtime, direction=CallDirection.OUTBOUND)]

    assert "cold_transfer" not in names


@pytest.mark.asyncio
async def test_hangup_removes_the_sip_participant() -> None:
    room_api = SimpleNamespace(
        remove_participant=AsyncMock(),
        delete_room=AsyncMock(),
    )
    ctx = SimpleNamespace(
        api=SimpleNamespace(room=room_api),
        room=SimpleNamespace(name="call-room"),
    )

    await hangup_sip_participant(ctx, participant_identity="callee-test")

    room_api.remove_participant.assert_awaited_once()
    request = room_api.remove_participant.await_args.args[0]
    assert request.room == "call-room"
    assert request.identity == "callee-test"
    room_api.delete_room.assert_not_awaited()
