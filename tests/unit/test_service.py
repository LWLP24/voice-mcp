import pytest

from calltool.calls.dispatcher import NullCallDispatcher
from calltool.calls.models import (
    CallCreateRequest,
    CallPermissions,
    CallStatus,
    CallTarget,
)
from calltool.calls.service import CallService
from calltool.config import CallsConfig, FileConfig, Settings
from calltool.policy.engine import PolicyEngine
from calltool.storage.memory import MemoryCallRepository


def make_settings(max_concurrent: int = 2) -> Settings:
    return Settings(
        CALLTOOL_ENV="test",
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
