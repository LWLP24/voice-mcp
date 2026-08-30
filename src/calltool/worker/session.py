from __future__ import annotations

import asyncio
import time
from collections.abc import Coroutine
from functools import partial
from typing import Any, Literal

from livekit.agents import AgentSession

from calltool.observability.metrics import (
    BARGE_IN_LATENCY,
    END_OF_TURN_DELAY,
    INTERRUPTIONS,
    NATIVE_METRIC_DURATION,
    NATIVE_METRIC_EVENTS,
    TRANSCRIPTION_DELAY,
    TURN_LATENCY,
)
from calltool.realtime.active_calls import ActiveCallContext
from calltool.voice.watchdog import UnrecoverableHandler, VoiceWatchdog


class SessionObserver:
    def __init__(
        self,
        session: AgentSession[Any],
        context: ActiveCallContext,
        *,
        persist_transcript: bool,
        watchdog_silence_seconds: float,
        watchdog_recovery_instruction: str,
        watchdog_fallback_phrase: str,
        on_unrecoverable: UnrecoverableHandler,
        interruption_mode: Literal["vad"] = "vad",
    ) -> None:
        self._session = session
        self._context = context
        self._persist_transcript_enabled = persist_transcript
        self._tasks: set[asyncio.Task[None]] = set()
        self._transcript_lock = asyncio.Lock()
        self._barge_in_started_at: float | None = None
        self._interruption_mode = interruption_mode
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
        self._session.on("overlapping_speech", self._on_overlapping_speech)
        self._session.on("session_usage_updated", self._on_session_usage)
        self._session.on("error", self._on_error)
        self._attach_native_metrics()

    def _on_user_state(self, event: Any) -> None:
        self._watchdog.user_state_changed(event.old_state, event.new_state)
        if event.new_state == "speaking":
            if self._session.agent_state == "speaking" and self._interruption_mode == "vad":
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
        if not self._persist_transcript_enabled or not event.is_final:
            return
        self.spawn(self._persist_user_transcript(str(event.transcript)))

    def _on_conversation_item(self, event: Any) -> None:
        item = event.item
        role = getattr(item, "role", None)
        metrics = getattr(item, "metrics", {})
        if role == "user":
            end_of_turn_delay = metrics.get("end_of_turn_delay")
            if isinstance(end_of_turn_delay, int | float) and end_of_turn_delay >= 0:
                END_OF_TURN_DELAY.observe(float(end_of_turn_delay))
            transcription_delay = metrics.get("transcription_delay")
            if isinstance(transcription_delay, int | float) and transcription_delay >= 0:
                TRANSCRIPTION_DELAY.observe(float(transcription_delay))
            return
        if role != "assistant":
            return
        if self._persist_transcript_enabled:
            text = getattr(item, "text_content", None)
            if isinstance(text, str) and text.strip():
                self.spawn(
                    self._persist_assistant_transcript(
                        text,
                        interrupted=bool(getattr(item, "interrupted", False)),
                    )
                )
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
        INTERRUPTIONS.labels(result="false").inc()
        self.spawn(
            self.persist(
                "call.false_interruption_detected",
                {"resumed": bool(event.resumed)},
            )
        )

    def _on_overlapping_speech(self, event: Any) -> None:
        if not bool(event.is_interruption):
            INTERRUPTIONS.labels(result="backchannel").inc()
            return
        INTERRUPTIONS.labels(result="interruption").inc()
        overlap_started_at = getattr(event, "overlap_started_at", None)
        if isinstance(overlap_started_at, int | float):
            elapsed = max(0.0, float(event.detected_at) - float(overlap_started_at))
            self._barge_in_started_at = time.perf_counter() - elapsed
        payload: dict[str, object] = {
            "probability": float(event.probability),
            "detection_delay_seconds": float(event.detection_delay),
            "prediction_duration_seconds": float(event.prediction_duration),
            "total_duration_seconds": float(event.total_duration),
            "num_requests": int(event.num_requests),
        }
        self.spawn(self.persist("call.user_interruption_detected", payload))

    def _on_session_usage(self, event: Any) -> None:
        usage = [item.model_dump(mode="json") for item in event.usage.model_usage]
        self._context.record_model_usage(usage)

    def _attach_native_metrics(self) -> None:
        sources = {
            "llm": self._session.llm,
            "stt": self._session.stt,
            "tts": self._session.tts,
            "vad": self._session.vad,
            "turn_detector": self._session.turn_detection,
        }
        for source_name, source in sources.items():
            if source is not None and hasattr(source, "on"):
                metric_source: Any = source
                metric_source.on("metrics_collected", partial(self._on_native_metric, source_name))

    def _on_native_metric(self, source: str, metric: Any) -> None:
        kind = str(getattr(metric, "type", type(metric).__name__))
        NATIVE_METRIC_EVENTS.labels(source=source, kind=kind).inc()
        duration = getattr(metric, "duration", None)
        if duration is None:
            duration = getattr(metric, "total_duration", None)
        if isinstance(duration, int | float) and duration >= 0:
            NATIVE_METRIC_DURATION.labels(source=source, kind=kind).observe(float(duration))

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
        if self._context.status.terminal:
            return
        self._context.persist_event(event_type, payload)

    async def _persist_user_transcript(self, transcript: str) -> None:
        async with self._transcript_lock:
            if self._context.status.terminal or not transcript.strip():
                return
            self._context.persist_transcript(role="user", text=transcript)

    async def _persist_assistant_transcript(self, transcript: str, *, interrupted: bool) -> None:
        async with self._transcript_lock:
            if self._context.status.terminal or not transcript.strip():
                return
            self._context.persist_transcript(
                role="assistant",
                text=transcript,
                interrupted=interrupted,
            )

    async def close(self) -> None:
        await self._watchdog.close()
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
