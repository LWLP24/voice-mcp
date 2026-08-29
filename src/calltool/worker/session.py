from __future__ import annotations

import asyncio
import time
from collections.abc import Coroutine
from typing import Any

from livekit.agents import AgentSession

from calltool.calls.errors import InvalidStateTransitionError
from calltool.calls.models import CallStatus
from calltool.calls.repository import CallRepository
from calltool.observability.metrics import BARGE_IN_LATENCY, TURN_LATENCY
from calltool.voice.watchdog import UnrecoverableHandler, VoiceWatchdog


class SessionObserver:
    def __init__(
        self,
        session: AgentSession[Any],
        repository: CallRepository,
        call_id: str,
        *,
        watchdog_silence_seconds: float,
        watchdog_recovery_instruction: str,
        watchdog_fallback_phrase: str,
        on_unrecoverable: UnrecoverableHandler,
    ) -> None:
        self._session = session
        self._repository = repository
        self._call_id = call_id
        self._tasks: set[asyncio.Task[None]] = set()
        self._barge_in_started_at: float | None = None
        self._watchdog = VoiceWatchdog(
            session,
            silence_seconds=watchdog_silence_seconds,
            recovery_instruction=watchdog_recovery_instruction,
            fallback_phrase=watchdog_fallback_phrase,
            on_event=self.persist,
            on_unrecoverable=on_unrecoverable,
        )

    def attach(self) -> None:
        self._session.on("user_state_changed", self._on_user_state)
        self._session.on("agent_state_changed", self._on_agent_state)
        self._session.on("user_input_transcribed", self._on_transcript)
        self._session.on("conversation_item_added", self._on_conversation_item)
        self._session.on("tool_execution_updated", self._on_tool_update)
        self._session.on("agent_false_interruption", self._on_false_interruption)
        self._session.on("error", self._on_error)

    def _on_user_state(self, event: Any) -> None:
        self._watchdog.user_state_changed(event.old_state, event.new_state)
        if event.new_state == "speaking":
            if self._session.agent_state == "speaking":
                self._barge_in_started_at = time.perf_counter()
            self.spawn(self.persist("call.user_speech_started", {}))
        elif event.old_state == "speaking":
            self.spawn(self.persist("call.user_speech_ended", {}))

    def _on_agent_state(self, event: Any) -> None:
        self._watchdog.agent_state_changed(event.new_state)
        if event.old_state == "speaking" and self._barge_in_started_at is not None:
            latency = time.perf_counter() - self._barge_in_started_at
            self._barge_in_started_at = None
            BARGE_IN_LATENCY.observe(latency)
            self.spawn(
                self.persist(
                    "call.barge_in_metrics",
                    {"barge_in_stop_latency_seconds": latency},
                )
            )
        if event.new_state == "speaking":
            self.spawn(self.persist("call.agent_speech_started", {}))
        elif event.old_state == "speaking":
            self.spawn(self.persist("call.agent_speech_ended", {}))

    def _on_transcript(self, event: Any) -> None:
        if not event.is_final:
            return
        self.spawn(self._persist_transcript(str(event.transcript)))

    def _on_conversation_item(self, event: Any) -> None:
        item = event.item
        if getattr(item, "role", None) != "assistant":
            return
        metrics = getattr(item, "metrics", {})
        latency = metrics.get("e2e_latency")
        if isinstance(latency, int | float) and latency >= 0:
            TURN_LATENCY.observe(float(latency))
            self.spawn(
                self.persist(
                    "call.turn_metrics",
                    {"conversation_response_latency_seconds": float(latency)},
                )
            )

    def _on_tool_update(self, event: Any) -> None:
        update = event.update
        update_type = str(update.type)
        self._watchdog.tool_execution_updated(update_type)
        if update_type == "tool_call_started":
            function_call = update.function_call
            self.spawn(
                self.persist(
                    "call.tool_started",
                    {"tool": str(function_call.name), "tool_call_id": function_call.call_id},
                )
            )
        elif update_type == "tool_call_ended":
            self.spawn(
                self.persist(
                    "call.tool_finished",
                    {"tool_call_id": update.call_id, "status": str(update.status)},
                )
            )

    def _on_false_interruption(self, event: Any) -> None:
        self.spawn(self.persist("call.false_interruption", {"resumed": bool(event.resumed)}))

    def _on_error(self, event: Any) -> None:
        self.spawn(
            self.persist(
                "call.voice_error",
                {"error": repr(event.error), "source": type(event.source).__name__},
            )
        )

    def spawn(self, coroutine: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def persist(self, event_type: str, payload: dict[str, object]) -> None:
        call = await self._repository.get_call(self._call_id)
        if call is None or call.status.terminal:
            return
        try:
            await self._repository.update_call(
                call.id,
                event_type=event_type,
                event_payload=payload,
                expected_statuses={call.status},
            )
        except InvalidStateTransitionError:
            return

    async def _persist_transcript(self, transcript: str) -> None:
        call = await self._repository.get_call(self._call_id)
        if call is None or call.status.terminal:
            return
        state = call.state.model_copy(update={"last_remote_utterance": transcript})
        try:
            await self._repository.update_call(
                call.id,
                state=state,
                event_type="call.user_transcript_final",
                event_payload={"transcript": transcript},
                expected_statuses={CallStatus.ACTIVE, CallStatus.INPUT_REQUIRED},
            )
        except InvalidStateTransitionError:
            return

    async def close(self) -> None:
        await self._watchdog.close()
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
