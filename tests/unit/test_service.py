from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from calltool.calls.dispatcher import NullCallDispatcher
from calltool.calls.errors import InvalidCursorError
from calltool.calls.models import (
    CallCreateRequest,
    CallDirection,
    CallListRequest,
    CallOutcome,
    CallPermissions,
    CallStatus,
    CallTarget,
)
from calltool.calls.service import CallService
from calltool.config import CallsConfig, FileConfig, Settings
from calltool.policy.engine import PolicyEngine
from calltool.storage.memory import MemoryCallRepository
from calltool.voice.prompts import PromptProfile

DEFAULT_PROMPT_DIR = Path(__file__).parents[2] / "config" / "prompts" / "default"


def make_settings(max_concurrent: int = 2) -> Settings:
    return Settings(
        CALLTOOL_ENV="test",
        CALLTOOL_PROMPT_DIR=str(DEFAULT_PROMPT_DIR),
        config=FileConfig(calls=CallsConfig(max_concurrent=max_concurrent)),
    )


def make_request(client_request_id: str | None = None) -> CallCreateRequest:
    return CallCreateRequest(
        target=CallTarget(phone_number="+49301234567", name="Test"),
        objective="Vereinbare einen Termin",
        constraints=["frühestens 15 Uhr"],
        permissions=CallPermissions(may_commit=True),
        client_request_id=client_request_id,
    )


@pytest.mark.asyncio
async def test_create_is_asynchronous_and_idempotent() -> None:
    settings = make_settings()
    repository = MemoryCallRepository()
    service = CallService(
        repository,
        NullCallDispatcher(),
        PolicyEngine(settings.config.policy),
        settings,
    )

    first = await service.create_call(make_request("same-request"), principal_id="user")
    second = await service.create_call(make_request("same-request"), principal_id="user")

    assert first.id == second.id
    assert first.status is CallStatus.QUEUED
    assert first.state.room_name is not None
    assert [event.type for event in await repository.list_events(first.id)] == [
        "call.created",
        "call.validating",
        "call.queued",
        "call.dispatched",
    ]


@pytest.mark.asyncio
async def test_concurrency_limit_queues_and_cancel_releases_slot() -> None:
    settings = make_settings(max_concurrent=1)
    repository = MemoryCallRepository()
    service = CallService(
        repository,
        NullCallDispatcher(),
        PolicyEngine(settings.config.policy),
        settings,
    )
    call = await service.create_call(make_request(), principal_id="user")
    queued = await service.create_call(make_request(), principal_id="other")

    assert call.state.dispatch_id is not None
    assert queued.status is CallStatus.QUEUED
    assert queued.state.dispatch_id is None

    cancelled = await service.cancel_call(call.id, principal_id="user")
    cancelled_again = await service.cancel_call(call.id, principal_id="user")
    assert await service.dispatch_queued() == 1
    dispatched = await repository.get_call(queued.id)

    assert cancelled.status is CallStatus.CANCELLED
    assert cancelled_again.status is CallStatus.CANCELLED
    assert dispatched is not None
    assert dispatched.state.dispatch_id is not None


@pytest.mark.asyncio
async def test_human_input_roundtrip() -> None:
    settings = make_settings()
    repository = MemoryCallRepository()
    service = CallService(
        repository,
        NullCallDispatcher(),
        PolicyEngine(settings.config.policy),
        settings,
    )
    call = await service.create_call(make_request(), principal_id="user")
    call = await repository.update_call(
        call.id,
        status=CallStatus.ACTIVE,
        event_type="test.active",
        expected_statuses={CallStatus.QUEUED},
    )
    input_request = await service.request_input(call.id, "14:30 akzeptieren?", ["accept", "reject"])

    waiting = await repository.get_call(call.id)
    assert waiting is not None
    assert waiting.status is CallStatus.INPUT_REQUIRED

    resumed = await service.respond(
        call.id,
        input_request.id,
        {"choice": "accept"},
        principal_id="user",
    )

    assert resumed.status is CallStatus.ACTIVE
    resolved = await repository.get_input_request(input_request.id)
    assert resolved is not None
    assert resolved.response == {"choice": "accept"}


@pytest.mark.asyncio
async def test_inbound_call_is_connected_safe_and_idempotent() -> None:
    settings = make_settings()
    repository = MemoryCallRepository()
    service = CallService(
        repository,
        NullCallDispatcher(),
        PolicyEngine(settings.config.policy),
        settings,
    )

    first = await service.create_inbound_call(
        caller_number="+491701234567",
        called_number="+49301234567",
        sip_participant_identity="sip-caller",
        room_name="calltool-inbound-test",
        sip_call_id="telnyx-call-id",
        principal_id="user",
    )
    second = await service.create_inbound_call(
        caller_number="+491701234567",
        called_number="+49301234567",
        sip_participant_identity="sip-caller",
        room_name="calltool-inbound-test",
        sip_call_id="telnyx-call-id",
        principal_id="user",
    )

    assert second.id == first.id
    assert first.direction is CallDirection.INBOUND
    assert first.status is CallStatus.CONNECTED
    assert first.connected_at is not None
    assert first.request.permissions.may_commit is False
    assert first.request.permissions.may_accept_costs is False
    assert first.state.room_name == "calltool-inbound-test"
    prompt_profile = PromptProfile.load(settings)
    assert prompt_profile.greeting(first, "de") == (
        "Guten Tag, hier ist der KI-Assistent von LWLP. Wie kann ich Ihnen helfen?"
    )
    prompt = prompt_profile.system_prompt(first, "de")
    assert "nimmst einen eingehenden Anruf für" in prompt
    assert "LWLP entgegen" in prompt
    assert "authorize_commit" not in prompt
    assert "send_dtmf" not in prompt
    assert [event.type for event in await repository.list_events(first.id)] == [
        "call.created",
        "call.connected",
    ]


@pytest.mark.asyncio
async def test_inbound_history_is_time_filtered_paginated_and_has_transcript() -> None:
    settings = make_settings()
    repository = MemoryCallRepository()
    service = CallService(
        repository,
        NullCallDispatcher(),
        PolicyEngine(settings.config.policy),
        settings,
    )
    calls = []
    for number in ("+491701111111", "+491702222222", "+491703333333"):
        calls.append(
            await service.create_inbound_call(
                caller_number=number,
                called_number="+49301234567",
                sip_participant_identity=f"sip-{number}",
                room_name=f"room-{number}",
                sip_call_id=f"telnyx-{number}",
                principal_id="user",
            )
        )

    await service.create_inbound_call(
        caller_number="+491709999999",
        called_number="+49301234567",
        sip_participant_identity="sip-other",
        room_name="room-other",
        sip_call_id="telnyx-other",
        principal_id="other",
    )
    await repository.append_transcript_turn(
        calls[-1].id,
        role="user",
        text="Ich brauche einen Rückruf.",
    )
    await repository.append_transcript_turn(
        calls[-1].id,
        role="assistant",
        text="Ich habe das aufgenommen.",
    )

    window_start = min(call.started_at for call in calls) - timedelta(seconds=1)
    window_end = max(call.started_at for call in calls) + timedelta(seconds=1)
    first_page = await service.list_calls(
        CallListRequest(
            started_after=window_start,
            started_before=window_end,
            limit=2,
        ),
        principal_id="user",
    )
    second_page = await service.list_calls(
        CallListRequest(
            started_after=window_start,
            started_before=window_end,
            limit=2,
            cursor=first_page.next_cursor,
        ),
        principal_id="user",
    )

    assert len(first_page.calls) == 2
    assert first_page.next_cursor is not None
    assert len(second_page.calls) == 1
    assert second_page.next_cursor is None
    assert {call.id for call in [*first_page.calls, *second_page.calls]} == {
        call.id for call in calls
    }

    conversation = await service.get_conversation(calls[-1].id, principal_id="user")
    assert [turn.role for turn in conversation.transcript] == ["user", "assistant"]
    assert [turn.text for turn in conversation.transcript] == [
        "Ich brauche einen Rückruf.",
        "Ich habe das aufgenommen.",
    ]


@pytest.mark.asyncio
async def test_call_history_rejects_invalid_cursor_and_naive_time() -> None:
    settings = make_settings()
    service = CallService(
        MemoryCallRepository(),
        NullCallDispatcher(),
        PolicyEngine(settings.config.policy),
        settings,
    )

    with pytest.raises(InvalidCursorError):
        await service.list_calls(
            CallListRequest(cursor="invalid"),
            principal_id="user",
        )
    with pytest.raises(ValueError, match="timezone"):
        CallListRequest(started_after=datetime(2026, 8, 29))

    query = CallListRequest(started_after=datetime(2026, 8, 29, tzinfo=UTC))
    assert query.started_after is not None


@pytest.mark.asyncio
async def test_call_history_searches_all_directions_by_target_name() -> None:
    settings = make_settings()
    repository = MemoryCallRepository()
    service = CallService(
        repository,
        NullCallDispatcher(),
        PolicyEngine(settings.config.policy),
        settings,
    )
    outbound = await service.create_call(
        CallCreateRequest(
            target=CallTarget(phone_number="+49309876543", name="Hausarzt Dr. Müller"),
            objective="Laborergebnis erfragen",
        ),
        principal_id="user",
    )
    outcome = CallOutcome(
        success=True,
        reason="information_received",
        summary="Die Laborwerte sind unauffällig.",
    )
    await repository.update_call(
        outbound.id,
        status=CallStatus.COMPLETED,
        outcome=outcome,
        event_type="call.completed",
        expected_statuses={CallStatus.QUEUED},
    )

    page = await service.list_calls(
        CallListRequest(target_name="arzt", limit=1),
        principal_id="user",
    )

    assert len(page.calls) == 1
    assert page.calls[0].direction is CallDirection.OUTBOUND
    assert page.calls[0].outcome is not None
    assert page.calls[0].outcome.summary == "Die Laborwerte sind unauffällig."
