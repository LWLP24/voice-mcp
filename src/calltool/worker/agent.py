from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from typing import Any

import structlog
from livekit import api, rtc
from livekit.agents import AutoSubscribe, JobContext

from calltool.api.auth import principal_for_api_key
from calltool.calls.dispatcher import NullCallDispatcher
from calltool.calls.models import (
    CallDirection,
    CallError,
    CallOutcome,
    CallPhase,
    CallStatus,
)
from calltool.calls.service import CallService
from calltool.config import Settings
from calltool.policy.engine import PolicyEngine
from calltool.realtime.events import build_event_dispatcher
from calltool.storage.postgres import PostgresCallRepository
from calltool.voice.realtime import build_voice_runtime
from calltool.voice.scripted_speech import frame_stream, pre_synthesize
from calltool.voice.supervisor import GeminiSupervisor
from calltool.voice.tools import ToolRuntime, build_tools
from calltool.worker.dialer import dial_call, sip_error
from calltool.worker.session import SessionObserver

logger = structlog.get_logger(__name__)


async def handle_call(ctx: JobContext, settings: Settings) -> None:
    metadata = _job_metadata(ctx.job.metadata)
    inbound = metadata.get("direction") == CallDirection.INBOUND.value
    call_id = metadata.get("call_id")
    if not inbound and not isinstance(call_id, str):
        ctx.shutdown("missing call_id in dispatch metadata")
        return
    if inbound and not settings.config.calls.inbound.enabled:
        ctx.shutdown("inbound calls disabled")
        return

    events = build_event_dispatcher(
        redis_url=settings.REDIS_URL,
        webhook_url=settings.WEBHOOK_URL,
        webhook_signing_secret=settings.WEBHOOK_SIGNING_SECRET.get_secret_value(),
        queue_size=settings.config.performance.event_queue_size,
    )
    repository = await PostgresCallRepository.connect(settings.DATABASE_URL, event_publisher=events)
    service = CallService(
        repository,
        NullCallDispatcher(),
        PolicyEngine(settings.config.policy),
        settings,
    )
    supervisor: GeminiSupervisor | None = None
    observer: SessionObserver | None = None
    greeting_task: asyncio.Task[rtc.AudioFrame] | None = None
    try:
        if inbound:
            await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
            participant = await ctx.wait_for_participant()
            if participant.kind != rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
                ctx.shutdown("inbound participant is not SIP")
                return
            attributes = participant.attributes
            call = await service.create_inbound_call(
                caller_number=attributes.get("sip.phoneNumber", "anonymous"),
                called_number=attributes.get("sip.trunkPhoneNumber", settings.TELNYX_FROM_NUMBER),
                sip_participant_identity=participant.identity,
                room_name=ctx.room.name,
                sip_call_id=attributes.get("sip.callIDFull") or attributes.get("sip.callID"),
                principal_id=principal_for_api_key(settings.CALLTOOL_API_KEY.get_secret_value()),
            )
            call_id = call.id
            if call.status is not CallStatus.CONNECTED:
                ctx.shutdown("inbound call already handled")
                return
            logger.info(
                "inbound call accepted",
                call_id=call.id,
                room_name=ctx.room.name,
                sip_call_id=attributes.get("sip.callID"),
            )
        else:
            assert isinstance(call_id, str)
            stored_call = await repository.get_call(call_id)
            if stored_call is None or stored_call.status.terminal:
                ctx.shutdown("call no longer active")
                return
            call = await repository.update_call(
                stored_call.id,
                status=CallStatus.PREWARMING,
                event_type="call.prewarming",
                expected_statuses={CallStatus.QUEUED},
            )
            await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)

        finish_event = asyncio.Event()
        tool_runtime = ToolRuntime(
            call_id=call.id,
            repository=repository,
            service=service,
            policy=PolicyEngine(settings.config.policy),
            room=ctx.room,
            finish_event=finish_event,
        )
        tools = build_tools(tool_runtime, direction=call.direction)
        voice = build_voice_runtime(call, settings, tools)
        supervisor = GeminiSupervisor(settings, voice.prompt_profile)
        greeting = voice.prompt_profile.greeting(call, voice.selection.language)
        if voice.scripted_tts is not None:
            greeting_task = asyncio.create_task(pre_synthesize(voice.scripted_tts, greeting))

        if not inbound:
            participant_identity = f"callee-{call.id.removeprefix('call_').lower()}"
            state = call.state.model_copy(update={"sip_participant_identity": participant_identity})
            call = await repository.update_call(
                call.id,
                status=CallStatus.DIALING,
                state=state,
                event_type="call.dialing",
                expected_statuses={CallStatus.PREWARMING},
            )
            dial_task = asyncio.create_task(dial_call(ctx, call, settings))
            call = await repository.update_call(
                call.id,
                status=CallStatus.RINGING,
                event_type="call.ringing",
                expected_statuses={CallStatus.DIALING},
            )
            try:
                await dial_task
            except api.SipCallError as exc:
                code, retryable = sip_error(exc)
                await repository.update_call(
                    call.id,
                    status=CallStatus.FAILED,
                    error=CallError(
                        code=code,
                        message=f"SIP {exc.sip_status_code}: {exc.sip_status}",
                        retryable=retryable,
                    ),
                    event_type="call.failed",
                    event_payload={
                        "code": code,
                        "sip_status_code": exc.sip_status_code,
                    },
                    expected_statuses={CallStatus.RINGING},
                )
                if greeting_task is not None:
                    greeting_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await greeting_task
                ctx.shutdown(code)
                return

            participant = await ctx.wait_for_participant(identity=participant_identity)
            call = await repository.update_call(
                call.id,
                status=CallStatus.CONNECTED,
                phase=CallPhase.OPENING,
                event_type="call.connected",
                event_payload={"direction": CallDirection.OUTBOUND.value},
                expected_statuses={CallStatus.RINGING},
            )

        disconnected = asyncio.Event()

        @ctx.room.on("participant_disconnected")
        def on_participant_disconnected(remote: Any) -> None:
            if remote.identity == participant.identity:
                disconnected.set()

        @ctx.room.on("disconnected")
        def on_room_disconnected(*_: Any) -> None:
            disconnected.set()

        if participant.identity not in ctx.room.remote_participants:
            disconnected.set()

        await voice.session.start(
            agent=voice.agent,
            room=ctx.room,
            record={
                "audio": settings.config.storage.audio,
                "transcript": settings.config.storage.transcript,
                "traces": True,
                "logs": True,
            },
        )
        watchdog_unrecoverable = asyncio.Event()

        async def on_watchdog_unrecoverable() -> None:
            watchdog_unrecoverable.set()

        observer = SessionObserver(
            voice.session,
            repository,
            call.id,
            watchdog_silence_seconds=settings.config.performance.watchdog_silence_seconds,
            watchdog_recovery_instruction=voice.prompt_profile.watchdog_instruction(
                call, voice.selection.language
            ),
            watchdog_fallback_phrase=voice.prompt_profile.watchdog_fallback(
                call, voice.selection.language
            ),
            on_unrecoverable=on_watchdog_unrecoverable,
        )
        observer.attach()
        call = await repository.update_call(
            call.id,
            status=CallStatus.ACTIVE,
            phase=CallPhase.OPENING,
            event_type="call.active",
            expected_statuses={CallStatus.CONNECTED},
        )

        if greeting_task is not None:
            greeting_audio = await greeting_task
            await voice.session.say(
                greeting,
                audio=frame_stream(greeting_audio),
                allow_interruptions=True,
            )
        else:
            voice.session.generate_reply(
                instructions=voice.prompt_profile.greeting_instruction(
                    call, voice.selection.language
                ),
                allow_interruptions=True,
            )

        end_reason = await _wait_for_call_end(disconnected, finish_event, watchdog_unrecoverable)
        if end_reason == "finish":
            with suppress(TimeoutError):
                async with asyncio.timeout(15):
                    await voice.session.wait_for_idle()
        elif end_reason == "watchdog":
            current = await repository.get_call(call.id)
            if current is not None and not current.status.terminal:
                await repository.update_call(
                    current.id,
                    status=CallStatus.FAILED,
                    error=CallError(
                        code="voice_watchdog_unrecoverable",
                        message="Voice session did not recover after a silent turn",
                        retryable=False,
                    ),
                    event_type="call.failed",
                    event_payload={"code": "voice_watchdog_unrecoverable"},
                    expected_statuses={current.status},
                )
            voice.session.shutdown(drain=False)
            ctx.shutdown("voice watchdog unrecoverable")
            return

        voice.session.shutdown(drain=True)
        await observer.close()
        observer = None
        await _complete_call(
            repository,
            supervisor,
            call.id,
            language=voice.selection.language,
        )
        ctx.shutdown("call completed")
    except Exception as exc:
        logger.exception("call worker failed", call_id=call_id)
        if isinstance(call_id, str):
            current = await repository.get_call(call_id)
            if current is not None and not current.status.terminal:
                with suppress(Exception):
                    await repository.update_call(
                        call_id,
                        status=CallStatus.FAILED,
                        error=CallError(
                            code="worker_failure",
                            message=str(exc),
                            retryable=False,
                        ),
                        event_type="call.failed",
                        event_payload={"code": "worker_failure"},
                        expected_statuses={current.status},
                    )
        ctx.shutdown("worker failure")
    finally:
        if greeting_task is not None and not greeting_task.done():
            greeting_task.cancel()
            with suppress(asyncio.CancelledError):
                await greeting_task
        if observer is not None:
            await observer.close()
        if supervisor is not None:
            await supervisor.close()
        await service.close()


def _job_metadata(raw_metadata: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(raw_metadata or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def _wait_for_call_end(
    disconnected: asyncio.Event,
    finish: asyncio.Event,
    watchdog: asyncio.Event,
) -> str:
    waiters = {
        asyncio.create_task(disconnected.wait()): "disconnected",
        asyncio.create_task(finish.wait()): "finish",
        asyncio.create_task(watchdog.wait()): "watchdog",
    }
    done, pending = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    return waiters[next(iter(done))]


async def _complete_call(
    repository: PostgresCallRepository,
    supervisor: GeminiSupervisor,
    call_id: str,
    *,
    language: str,
) -> None:
    call = await repository.get_call(call_id)
    if call is None or call.status.terminal:
        return
    if call.outcome is None:
        confirmed = [item for item in call.state.commitments if item.allowed and item.confirmed]
        inbound = call.direction is CallDirection.INBOUND
        outcome = CallOutcome(
            success=inbound or bool(confirmed),
            reason="remote_hangup",
            summary=(
                "Eingehendes Gespräch wurde beendet."
                if inbound
                else (
                    "Gespräch beendet; verbindliche Zusage wurde erfasst."
                    if confirmed
                    else "Gespräch wurde ohne verbindliche Zusage beendet."
                )
            ),
            facts=call.state.facts,
            commitments=call.state.commitments,
        )
        if call.status is not CallStatus.COMPLETING:
            call = await repository.update_call(
                call.id,
                status=CallStatus.COMPLETING,
                outcome=outcome,
                event_type="call.completing",
                expected_statuses={CallStatus.ACTIVE, CallStatus.INPUT_REQUIRED},
            )
    else:
        outcome = call.outcome
    outcome = await supervisor.enrich_outcome(call, outcome, language)
    await repository.update_call(
        call.id,
        status=CallStatus.COMPLETED,
        outcome=outcome,
        event_type="call.completed",
        event_payload={"success": outcome.success, "reason": outcome.reason},
        expected_statuses={CallStatus.COMPLETING},
    )
