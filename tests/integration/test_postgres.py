import os
from datetime import UTC, datetime, timedelta

import pytest

from calltool.calls.ids import new_id
from calltool.calls.models import (
    ActiveCallState,
    CallCreateRequest,
    CallDirection,
    CallRecord,
    CallStatus,
    CallTarget,
)
from calltool.storage.postgres import PostgresCallRepository

DATABASE_URL = os.environ.get("CALLTOOL_TEST_DATABASE_URL")


@pytest.mark.skipif(DATABASE_URL is None, reason="temporary PostgreSQL not configured")
@pytest.mark.asyncio
async def test_postgres_call_history_migration_and_queries() -> None:
    assert DATABASE_URL is not None
    repository = await PostgresCallRepository.connect(DATABASE_URL)
    principal_id = new_id("principal")
    call_id = new_id("call")
    started_at = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
    request = CallCreateRequest(
        target=CallTarget(phone_number="+491701234567", name="Eingehender Anrufer"),
        objective="Anliegen aufnehmen",
        context={"called_number": "+49301234567"},
    )
    call = CallRecord(
        id=call_id,
        principal_id=principal_id,
        direction=CallDirection.INBOUND,
        status=CallStatus.ACTIVE,
        target_number=request.target.phone_number,
        request=request,
        state=ActiveCallState(objective=request.objective),
        created_at=started_at - timedelta(seconds=1),
        updated_at=started_at,
        connected_at=started_at,
    )
    outbound_request = CallCreateRequest(
        target=CallTarget(phone_number="+49309876543", name="Hausarzt Dr. Müller"),
        objective="Laborergebnis erfragen",
    )
    outbound = CallRecord(
        id=new_id("call"),
        principal_id=principal_id,
        direction=CallDirection.OUTBOUND,
        status=CallStatus.COMPLETED,
        target_number=outbound_request.target.phone_number,
        request=outbound_request,
        state=ActiveCallState(objective=outbound_request.objective),
        created_at=started_at - timedelta(minutes=1),
        updated_at=started_at,
        connected_at=started_at - timedelta(seconds=30),
        ended_at=started_at,
    )

    try:
        await repository.create_call(call)
        await repository.create_call(outbound)
        await repository.append_transcript_turn(
            call.id,
            role="user",
            text="Ich brauche einen Rückruf.",
        )
        await repository.append_transcript_turn(
            call.id,
            role="assistant",
            text="Ich habe das aufgenommen.",
        )
        await repository.migrate()

        listed = await repository.list_calls(
            principal_id=principal_id,
            direction=CallDirection.INBOUND,
            status=None,
            phone_number="+491701234567",
            target_name="eingehender",
            started_after=started_at,
            started_before=started_at + timedelta(seconds=1),
            cursor_started_at=None,
            cursor_call_id=None,
            limit=10,
        )
        transcript = await repository.list_transcript(call.id)
        outbound_calls = await repository.list_calls(
            principal_id=principal_id,
            direction=None,
            status=None,
            phone_number=None,
            target_name="arzt",
            started_after=None,
            started_before=None,
            cursor_started_at=None,
            cursor_call_id=None,
            limit=1,
        )

        assert [item.id for item in listed] == [call.id]
        assert [turn.role for turn in transcript] == ["user", "assistant"]
        assert [turn.sequence for turn in transcript] == [1, 2]
        assert [item.id for item in outbound_calls] == [outbound.id]
        assert outbound_calls[0].direction is CallDirection.OUTBOUND
    finally:
        await repository.close()
