from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

from livekit.agents import AgentSession

WatchdogEventHandler = Callable[[str, dict[str, object]], Awaitable[None]]
UnrecoverableHandler = Callable[[], Awaitable[None]]


class VoiceWatchdog:
    """Detect a silent agent after a completed user turn and recover deterministically."""

    def __init__(
        self,
        session: AgentSession[Any],
        *,
        silence_seconds: float,
        recovery_instruction: str,
        fallback_phrase: str,
        on_event: WatchdogEventHandler,
        on_unrecoverable: UnrecoverableHandler,
    ) -> None:
        self._session = session
        self._silence_seconds = silence_seconds
        self._recovery_instruction = recovery_instruction
        self._fallback_phrase = fallback_phrase
        self._on_event = on_event
        self._on_unrecoverable = on_unrecoverable
        self._tool_depth = 0
        self._watch_task: asyncio.Task[None] | None = None
        self._agent_spoke = asyncio.Event()

    def user_state_changed(self, old_state: str, new_state: str) -> None:
        if old_state == "speaking" and new_state == "listening":
            self._arm()
        elif new_state == "speaking":
            self._cancel_watch()

    def agent_state_changed(self, new_state: str) -> None:
        if new_state == "speaking":
            self._agent_spoke.set()
            self._cancel_watch()

    def tool_execution_updated(self, update_type: str) -> None:
        if update_type == "tool_call_started":
            self._tool_depth += 1
            self._cancel_watch()
        elif update_type == "tool_call_ended":
            self._tool_depth = max(0, self._tool_depth - 1)

    def _arm(self) -> None:
        self._cancel_watch()
        self._watch_task = asyncio.create_task(self._watch(), name="calltool-voice-watchdog")

    def _cancel_watch(self) -> None:
        if self._watch_task is not None and self._watch_task is not asyncio.current_task():
            self._watch_task.cancel()
        self._watch_task = None

    async def _watch(self) -> None:
        try:
            await asyncio.sleep(self._silence_seconds)
            if self._tool_depth or self._session.user_state != "listening":
                return
            if self._session.agent_state not in {"idle", "listening"}:
                return

            await self._on_event(
                "call.watchdog_triggered",
                {
                    "silence_seconds": self._silence_seconds,
                    "agent_state": self._session.agent_state,
                },
            )
            self._agent_spoke.clear()
            try:
                self._session.generate_reply(
                    instructions=self._recovery_instruction,
                    allow_interruptions=True,
                )
                async with asyncio.timeout(2.0):
                    await self._agent_spoke.wait()
                await self._on_event("call.watchdog_recovered", {"method": "model_reply"})
                return
            except (RuntimeError, TimeoutError):
                pass

            try:
                self._session.say(
                    self._fallback_phrase,
                    allow_interruptions=True,
                )
                await self._on_event("call.watchdog_recovered", {"method": "scripted_phrase"})
            except RuntimeError:
                await self._on_event(
                    "call.watchdog_unrecoverable", {"reason": "session_unavailable"}
                )
                await self._on_unrecoverable()
        except asyncio.CancelledError:
            raise
        finally:
            if self._watch_task is asyncio.current_task():
                self._watch_task = None

    async def close(self) -> None:
        task = self._watch_task
        self._cancel_watch()
        if task is not None:
            with suppress(asyncio.CancelledError):
                await task
