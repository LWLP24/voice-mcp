from __future__ import annotations

import asyncio
from typing import Any

import pytest

from calltool.calls.models import (
    ActiveCallState,
    AMDCategory,
    AMDResult,
    CallCreateRequest,
    CallRecord,
    CallStatus,
    CallTarget,
    ColdTransferState,
    Commitment,
    TransferStatus,
)
from calltool.realtime.active_calls import ActiveCallContext
from calltool.storage.memory import MemoryCallRepository


class BlockingMemoryCallRepository(MemoryCallRepository):
    def __init__(self) -> None:
        super().__init__()
        self.write_started = asyncio.Event()
        self.allow_write = asyncio.Event()
        self.get_call_count = 0

    async def get_call(self, call_id: str) -> CallRecord | None:
        self.get_call_count += 1
        return await super().get_call(call_id)

    async def update_call(self, call_id: str, **kwargs: Any) -> CallRecord:
        self.write_started.set()
        await self.allow_write.wait()
        return await super().update_call(call_id, **kwargs)


class FailingMemoryCallRepository(MemoryCallRepository):
    async def update_call(self, call_id: str, **kwargs: Any) -> CallRecord:
        raise RuntimeError("postgres unavailable")


def make_call() -> CallRecord:
    request = CallCreateRequest(
        target=CallTarget(phone_number="+49301234567", name="Praxis"),
        objective="Termin vereinbaren",
    )
    return CallRecord(
        id="call_context_test",
        principal_id="test",
        status=CallStatus.ACTIVE,
        target_number=request.target.phone_number,
        request=request,
        state=ActiveCallState(objective=request.objective),
    )


@pytest.mark.asyncio
async def test_hot_state_changes_without_waiting_for_postgres() -> None:
    repository = BlockingMemoryCallRepository()
    call = make_call()
    await repository.create_call(call)
    context = ActiveCallContext.from_call(call, repository)

    context.record_fact("appointment", "Montag 15 Uhr")
    context.persist_state(
        "call.fact_recorded",
        {"key": "appointment", "value": "Montag 15 Uhr"},
        expected_statuses={CallStatus.ACTIVE},
    )

    assert context.facts == {"appointment": "Montag 15 Uhr"}
    assert repository.get_call_count == 0
    async with asyncio.timeout(1):
        await repository.write_started.wait()
    assert context.persistence_backlog == 0

    repository.allow_write.set()
    await context.close()
    stored = await repository.get_call(call.id)

    assert stored is not None
    assert stored.state.facts == {"appointment": "Montag 15 Uhr"}


@pytest.mark.asyncio
async def test_durable_write_waits_for_persistence_acknowledgement() -> None:
    repository = BlockingMemoryCallRepository()
    call = make_call()
    await repository.create_call(call)
    context = ActiveCallContext.from_call(call, repository)
    context.record_fact("confirmed", True)

    persistence = asyncio.create_task(
        context.persist_state_durable(
            "call.commit_allowed",
            {"confirmed": True},
            expected_statuses={CallStatus.ACTIVE},
        )
    )
    async with asyncio.timeout(1):
        await repository.write_started.wait()
    assert persistence.done() is False

    repository.allow_write.set()
    await persistence
    await context.close()


@pytest.mark.asyncio
async def test_failed_commitment_is_not_kept_or_reported_as_durable() -> None:
    repository = FailingMemoryCallRepository()
    call = make_call()
    await repository.create_call(call)
    context = ActiveCallContext.from_call(call, repository)
    commitment = Commitment(
        id="commit_test",
        action="book_appointment",
        payload={"when": "Montag 15 Uhr"},
        allowed=True,
        confirmed=True,
    )

    with pytest.raises(RuntimeError, match="postgres unavailable"):
        await context.persist_commitment(commitment, "call.commit_allowed")

    assert context.find_commitment(commitment.id) is None
    stored = await repository.get_call(call.id)
    assert stored is not None
    assert stored.state.commitments == []
    with pytest.raises(RuntimeError, match="persistence skipped"):
        await context.close()


@pytest.mark.asyncio
async def test_state_mutations_wait_behind_a_durable_commitment() -> None:
    repository = BlockingMemoryCallRepository()
    call = make_call()
    await repository.create_call(call)
    context = ActiveCallContext.from_call(call, repository)
    commitment = Commitment(
        id="commit_serialized",
        action="book_appointment",
        payload={"when": "Dienstag 16 Uhr"},
        allowed=True,
        confirmed=True,
    )

    async def persist_commitment() -> None:
        async with context.state_transaction():
            await context.persist_commitment(commitment, "call.commit_allowed")

    async def record_follow_up_fact() -> None:
        async with context.state_transaction():
            context.record_fact("location", "Praxis")
            context.persist_state(
                "call.fact_recorded",
                {"key": "location", "value": "Praxis"},
                expected_statuses={CallStatus.ACTIVE},
            )

    commitment_write = asyncio.create_task(persist_commitment())
    async with asyncio.timeout(1):
        await repository.write_started.wait()
    fact_write = asyncio.create_task(record_follow_up_fact())
    await asyncio.sleep(0)
    assert fact_write.done() is False

    repository.allow_write.set()
    await asyncio.gather(commitment_write, fact_write)
    await context.close()

    stored = await repository.get_call(call.id)
    assert stored is not None
    assert [item.id for item in stored.state.commitments] == [commitment.id]
    assert stored.state.facts == {"location": "Praxis"}


@pytest.mark.asyncio
async def test_transcript_turns_are_serialized_with_hot_state() -> None:
    repository = MemoryCallRepository()
    call = make_call()
    await repository.create_call(call)
    context = ActiveCallContext.from_call(call, repository)

    context.persist_transcript(role="user", text="Ich brauche einen Termin.")
    context.persist_transcript(role="assistant", text="Wann passt es Ihnen?")
    await context.close()

    transcript = await repository.list_transcript(call.id)
    stored = await repository.get_call(call.id)
    assert [turn.role for turn in transcript] == ["user", "assistant"]
    assert stored is not None
    assert stored.state.last_remote_utterance == "Ich brauche einen Termin."


@pytest.mark.asyncio
async def test_voice_telephony_state_is_hot_and_persisted_as_one_snapshot() -> None:
    repository = MemoryCallRepository()
    call = make_call()
    await repository.create_call(call)
    context = ActiveCallContext.from_call(call, repository)

    context.configure_voice_session(
        turn_detection_mode="livekit_v1_mini",
        interruption_mode="vad",
        ivr_detection_enabled=True,
    )
    context.record_amd_result(
        AMDResult(
            category=AMDCategory.HUMAN,
            reason="short_speech_then_silence",
            transcript="Hallo?",
            speech_duration_seconds=1.2,
            detection_delay_seconds=0.4,
        )
    )
    context.record_transfer(
        ColdTransferState(
            target_number="+49309876543",
            status=TransferStatus.SUCCESSFUL,
            transfer_id="transfer_123",
        )
    )
    context.record_model_usage([{"model": "gpt-realtime-2.1-mini", "input_tokens": 10}])
    context.record_session_report({"sdk_version": "1.7.1", "event_counts": {"close": 1}})
    await context.persist_state_durable(
        "call.voice_session_reported",
        {"sdk_version": "1.7.1"},
        expected_statuses={CallStatus.ACTIVE},
    )
    await context.close()

    stored = await repository.get_call(call.id)
    assert stored is not None
    assert stored.state.voice_session.turn_detection_mode == "livekit_v1_mini"
    assert stored.state.voice_session.amd is not None
    assert stored.state.voice_session.amd.category is AMDCategory.HUMAN
    assert stored.state.voice_session.transfer is not None
    assert stored.state.voice_session.transfer.status is TransferStatus.SUCCESSFUL
    assert stored.state.voice_session.model_usage[0]["input_tokens"] == 10
