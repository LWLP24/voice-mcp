from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from calltool.calls.models import (
    ActiveCallState,
    CallCreateRequest,
    CallRecord,
    CallStatus,
    CallTarget,
)
from calltool.realtime.active_calls import ActiveCallContext
from calltool.storage.memory import MemoryCallRepository
from calltool.worker.session import SessionObserver


class FakeSession:
    def __init__(self) -> None:
        self.agent_state = "speaking"
        self.llm = None
        self.stt = None
        self.tts = None
        self.vad = None
        self.turn_detection = "realtime_llm"
        self.handlers: dict[str, list[Callable[[Any], None]]] = defaultdict(list)

    def on(self, event: str, callback: Callable[[Any], None]) -> None:
        self.handlers[event].append(callback)

    def emit(self, event: str, value: Any) -> None:
        for callback in self.handlers[event]:
            callback(value)

    def generate_reply(self, **_: Any) -> None:
        return None

    async def say(self, *_: Any, **__: Any) -> None:
        return None


class FakeUsage:
    def model_dump(self, *, mode: str) -> dict[str, object]:
        assert mode == "json"
        return {"model": "gpt-realtime-2.1-mini", "input_tokens": 12}


def make_call() -> CallRecord:
    request = CallCreateRequest(
        target=CallTarget(phone_number="+49301234567"),
        objective="Test",
    )
    return CallRecord(
        id="call_observer",
        principal_id="test",
        status=CallStatus.ACTIVE,
        target_number=request.target.phone_number,
        request=request,
        state=ActiveCallState(objective=request.objective),
    )


@pytest.mark.asyncio
async def test_native_interruption_false_interruption_and_usage_are_collected() -> None:
    repository = MemoryCallRepository()
    call = make_call()
    await repository.create_call(call)
    context = ActiveCallContext.from_call(call, repository)
    session = FakeSession()

    async def on_unrecoverable() -> None:
        return None

    observer = SessionObserver(
        session,
        context,
        persist_transcript=False,
        watchdog_silence_seconds=2.5,
        watchdog_recovery_instruction="recover",
        watchdog_fallback_phrase="fallback",
        on_unrecoverable=on_unrecoverable,
        interruption_mode="vad",
    )
    observer.attach()
    session.emit(
        "overlapping_speech",
        SimpleNamespace(
            is_interruption=True,
            overlap_started_at=10.0,
            detected_at=10.2,
            probability=0.93,
            detection_delay=0.2,
            prediction_duration=0.03,
            total_duration=0.04,
            num_requests=2,
        ),
    )
    session.emit(
        "agent_state_changed",
        SimpleNamespace(old_state="speaking", new_state="listening"),
    )
    session.emit("agent_false_interruption", SimpleNamespace(resumed=True))
    session.emit(
        "session_usage_updated",
        SimpleNamespace(usage=SimpleNamespace(model_usage=[FakeUsage()])),
    )

    await observer.close()
    await context.close()

    events = await repository.list_events(call.id)
    event_types = [event.type for event in events]
    assert "call.user_interruption_detected" in event_types
    assert "call.false_interruption_detected" in event_types
    assert "call.barge_in_metrics" in event_types
    assert context.voice_session.model_usage == [
        {"model": "gpt-realtime-2.1-mini", "input_tokens": 12}
    ]
