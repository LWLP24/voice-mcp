from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from typing import Any

import structlog
from livekit import api, rtc
from livekit.agents import AgentSession, AutoSubscribe, JobContext
from livekit.agents.voice import amd

from calltool.api.auth import principal_for_api_key
from calltool.calls.dispatcher import NullCallDispatcher
from calltool.calls.models import (
    AMDCategory,
    AMDResult,
    CallDirection,
    CallError,
    CallOutcome,
    CallPhase,
    CallRecord,
    CallStatus,
    TransferStatus,
    utc_now,
)
from calltool.calls.service import CallService
from calltool.config import Settings
from calltool.policy.engine import PolicyEngine
from calltool.realtime.active_calls import ActiveCallContext
from calltool.realtime.events import build_event_dispatcher
from calltool.storage.postgres import PostgresCallRepository
from calltool.voice.realtime import VoiceRuntime, build_amd_detector, build_voice_runtime
from calltool.voice.scripted_speech import frame_stream, pre_synthesize
from calltool.voice.supervisor import GeminiSupervisor
from calltool.voice.tools import ToolRuntime, TransferResult, build_tools
from calltool.worker.dialer import dial_call, hangup_sip_participant, sip_error, transfer_call
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
    active_context: ActiveCallContext | None = None
    greeting_task: asyncio.Task[rtc.AudioFrame] | None = None
    voice: VoiceRuntime | None = None
    amd_detector: amd.AMD | None = None
    ivr_timeout_handle: asyncio.TimerHandle | None = None
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
        participant_identity = (
            participant.identity if inbound else f"callee-{call.id.removeprefix('call_').lower()}"
        )
        active_context = ActiveCallContext.from_call(call, repository)
        active_context.sip_participant_identity = participant_identity

        async def handle_transfer(target_number: str) -> TransferResult:
            return await transfer_call(
                ctx,
                participant_identity=participant_identity,
                target_number=target_number,
                settings=settings,
            )

        tool_runtime = ToolRuntime(
            call_id=call.id,
            context=active_context,
            service=service,
            policy=PolicyEngine(settings.config.policy),
            room=ctx.room,
            finish_event=finish_event,
            ivr_config=settings.config.telephony.ivr,
            ivr_enabled=settings.ivr_enabled(),
            cold_transfer_enabled=(
                not inbound
                and settings.cold_transfer_enabled()
                and call.request.permissions.may_transfer
            ),
            transfer_handler=handle_transfer,
        )
        tools = build_tools(tool_runtime, direction=call.direction)
        voice = build_voice_runtime(call, settings, tools)
        active_context.configure_voice_session(
            turn_detection_mode=voice.turn_detection_mode,
            interruption_mode=voice.interruption_mode,
            ivr_detection_enabled=voice.ivr_detection_enabled,
            turn_unlikely_threshold=voice.turn_unlikely_threshold,
            turn_backchannel_threshold=voice.turn_backchannel_threshold,
        )
        supervisor = GeminiSupervisor(settings, voice.prompt_profile)
        greeting = voice.prompt_profile.greeting(call, voice.selection.language)
        if voice.scripted_tts is not None:
            greeting_task = asyncio.create_task(pre_synthesize(voice.scripted_tts, greeting))

        watchdog_unrecoverable = asyncio.Event()

        async def on_watchdog_unrecoverable() -> None:
            watchdog_unrecoverable.set()

        observer = SessionObserver(
            voice.session,
            active_context,
            persist_transcript=settings.config.storage.transcript,
            watchdog_silence_seconds=settings.config.performance.watchdog_silence_seconds,
            watchdog_recovery_instruction=voice.prompt_profile.watchdog_instruction(
                call, voice.selection.language
            ),
            watchdog_fallback_phrase=voice.prompt_profile.watchdog_fallback(
                call, voice.selection.language
            ),
            on_unrecoverable=on_watchdog_unrecoverable,
            interruption_mode=voice.interruption_mode,
        )
        observer.attach()
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

        if not inbound:
            if settings.amd_enabled():
                amd_detector = build_amd_detector(
                    voice,
                    settings,
                    participant_identity=participant_identity,
                )
                await amd_detector.__aenter__()
            call = await repository.update_call(
                call.id,
                status=CallStatus.DIALING,
                state=active_context.snapshot(),
                event_type="call.dialing",
                expected_statuses={CallStatus.PREWARMING},
            )
            active_context.apply_call(call)
            dial_task = asyncio.create_task(dial_call(ctx, call, settings))
            call = await repository.update_call(
                call.id,
                status=CallStatus.RINGING,
                event_type="call.ringing",
                expected_statuses={CallStatus.DIALING},
            )
            active_context.apply_call(call)
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
                    event_payload={"code": code, "sip_status_code": exc.sip_status_code},
                    expected_statuses={CallStatus.RINGING},
                )
                await _close_amd(amd_detector)
                amd_detector = None
                await _close_voice_session(voice.session, drain=False)
                voice = None
                await observer.close()
                observer = None
                await active_context.close()
                active_context = None
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
            active_context.apply_call(call)

        disconnected = asyncio.Event()

        @ctx.room.on("participant_disconnected")
        def on_participant_disconnected(remote: Any) -> None:
            if remote.identity == participant.identity:
                disconnected.set()

        @ctx.room.on("disconnected")
        def on_room_disconnected(*_: Any) -> None:
            disconnected.set()

        @ctx.room.on("sip_dtmf_received")
        def on_dtmf_received(event: Any) -> None:
            payload: dict[str, object] = {"code": int(event.code)}
            if settings.config.telephony.ivr.audit_digits:
                payload["digit"] = str(event.digit)
            if active_context is not None:
                active_context.persist_event("call.dtmf_received", payload)

        if participant.identity not in ctx.room.remote_participants:
            disconnected.set()

        detected_amd = await _run_amd(amd_detector, active_context)
        amd_detector = None
        call = await repository.update_call(
            call.id,
            status=CallStatus.ACTIVE,
            phase=CallPhase.OPENING,
            state=active_context.snapshot(),
            event_type="call.active",
            expected_statuses={CallStatus.CONNECTED},
        )
        active_context.apply_call(call)

        await _start_initial_voice_action(
            voice,
            call,
            active_context,
            greeting,
            greeting_task,
            detected_amd,
            settings,
            finish_event,
            service,
            disconnected,
        )
        greeting_task = None

        ivr_timeout = asyncio.Event()
        if (
            detected_amd is not None
            and detected_amd.category is AMDCategory.MACHINE_IVR
            and settings.ivr_enabled()
        ):
            ivr_timeout_handle = asyncio.get_running_loop().call_later(
                settings.config.telephony.ivr.navigation_timeout_seconds,
                ivr_timeout.set,
            )

        end_reason = await _wait_for_call_end(
            disconnected,
            finish_event,
            watchdog_unrecoverable,
            ivr_timeout,
        )
        if ivr_timeout_handle is not None:
            ivr_timeout_handle.cancel()
            ivr_timeout_handle = None
        if end_reason == "ivr_timeout":
            await _finish_amd_call(
                active_context,
                finish_event,
                reason="ivr_navigation_timeout",
                summary="Die IVR-Navigation wurde nach dem konfigurierten Timeout beendet.",
            )
            end_reason = "finish"
        if end_reason == "finish":
            with suppress(TimeoutError):
                async with asyncio.timeout(15):
                    await voice.session.wait_for_idle()
            transfer = active_context.snapshot().voice_session.transfer
            if transfer is None or transfer.status is not TransferStatus.SUCCESSFUL:
                try:
                    await hangup_sip_participant(
                        ctx,
                        participant_identity=participant_identity,
                    )
                except Exception as exc:
                    logger.warning(
                        "could not explicitly hang up SIP participant",
                        call_id=call.id,
                        participant_identity=participant_identity,
                        error=str(exc),
                    )
        elif end_reason == "watchdog":
            closed_session = voice.session
            await _close_voice_session(closed_session, drain=False)
            voice = None
            await observer.close()
            observer = None
            await _persist_session_report(ctx, closed_session, active_context)
            with suppress(Exception):
                await active_context.close()
            active_context = None
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
            ctx.shutdown("voice watchdog unrecoverable")
            return

        closed_session = voice.session
        language = voice.selection.language
        await _close_voice_session(closed_session, drain=True)
        voice = None
        await observer.close()
        observer = None
        await _persist_session_report(ctx, closed_session, active_context)
        await active_context.close()
        active_context = None
        await _complete_call(
            repository,
            supervisor,
            call.id,
            language=language,
        )
        ctx.shutdown("call completed")
    except Exception as exc:
        logger.exception("call worker failed", call_id=call_id)
        if amd_detector is not None:
            with suppress(Exception):
                await _close_amd(amd_detector)
            amd_detector = None
        if voice is not None:
            with suppress(Exception):
                await voice.session.aclose()
            voice = None
        if observer is not None:
            with suppress(Exception):
                await observer.close()
            observer = None
        if active_context is not None:
            with suppress(Exception):
                await active_context.close()
            active_context = None
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
        if ivr_timeout_handle is not None:
            ivr_timeout_handle.cancel()
        if amd_detector is not None:
            with suppress(Exception):
                await _close_amd(amd_detector)
        if voice is not None:
            with suppress(Exception):
                await voice.session.aclose()
        if greeting_task is not None and not greeting_task.done():
            greeting_task.cancel()
            with suppress(asyncio.CancelledError):
                await greeting_task
        if observer is not None:
            await observer.close()
        if active_context is not None:
            await active_context.close()
        if supervisor is not None:
            await supervisor.close()
        await service.close()


async def _close_amd(detector: amd.AMD | None) -> None:
    if detector is not None:
        await detector.aclose()


async def _run_amd(
    detector: amd.AMD | None,
    context: ActiveCallContext,
) -> AMDResult | None:
    if detector is None:
        return None
    event_type = "call.amd_detected"
    try:
        prediction = await detector.execute()
        result = AMDResult(
            category=AMDCategory(prediction.category.value),
            reason=prediction.reason,
            transcript=prediction.transcript,
            speech_duration_seconds=prediction.speech_duration,
            detection_delay_seconds=prediction.delay,
        )
    except Exception as exc:
        event_type = "call.amd_failed"
        result = AMDResult(
            category=AMDCategory.UNCERTAIN,
            reason=f"amd_error:{type(exc).__name__}",
        )
    finally:
        await detector.aclose()

    async with context.state_transaction():
        context.record_amd_result(result)
        await context.persist_state_durable(
            event_type,
            result.model_dump(mode="json"),
            expected_statuses={CallStatus.CONNECTED},
        )
    return result


async def _start_initial_voice_action(
    voice: VoiceRuntime,
    call: CallRecord,
    context: ActiveCallContext,
    greeting: str,
    greeting_task: asyncio.Task[rtc.AudioFrame] | None,
    detected_amd: AMDResult | None,
    settings: Settings,
    finish_event: asyncio.Event,
    service: CallService,
    disconnected: asyncio.Event,
) -> None:
    category = detected_amd.category.value if detected_amd is not None else "human"
    config = settings.config.telephony.amd

    if category == "machine-ivr":
        await _cancel_audio_task(greeting_task)
        if settings.ivr_enabled():
            await _speak_control_message(
                voice,
                voice.prompt_profile.ivr_instruction(call, voice.selection.language),
                allow_interruptions=False,
            )
            return
        await _finish_amd_call(
            context,
            finish_event,
            reason="ivr_detected_but_disabled",
            summary="Automatisches Telefonmenü erkannt; IVR-Navigation ist deaktiviert.",
        )
        return

    action = "continue"
    if category == "machine-vm":
        action = config.voicemail_action
    elif category == "machine-unavailable":
        action = config.unavailable_action
    elif category == "uncertain":
        action = config.uncertain_action

    if action == "request_user":
        action = await _request_amd_decision(service, context, disconnected)

    if action == "hangup":
        await _cancel_audio_task(greeting_task)
        await _finish_amd_call(
            context,
            finish_event,
            reason=f"amd_{category}",
            summary=f"Anruf nach AMD-Ergebnis {category} beendet.",
        )
        return
    if action == "leave_message":
        await _cancel_audio_task(greeting_task)
        await _speak_control_message(
            voice,
            voice.prompt_profile.voicemail_instruction(call, voice.selection.language),
            allow_interruptions=False,
        )
        with suppress(TimeoutError):
            async with asyncio.timeout(30):
                await voice.session.wait_for_idle()
        await _finish_amd_call(
            context,
            finish_event,
            reason="voicemail_message_left",
            summary="Eine konfigurierte Nachricht wurde auf der Mailbox hinterlassen.",
            success=True,
        )
        return

    if greeting_task is not None:
        greeting_audio = await greeting_task
        await voice.session.say(
            greeting,
            audio=frame_stream(greeting_audio),
            allow_interruptions=True,
        )
    else:
        voice.session.generate_reply(
            instructions=voice.prompt_profile.greeting_instruction(call, voice.selection.language),
            allow_interruptions=True,
        )


async def _speak_control_message(
    voice: VoiceRuntime,
    text: str,
    *,
    allow_interruptions: bool,
) -> None:
    """Speak a deterministic control prompt without Gemini 3.1 generate_reply()."""
    if voice.scripted_tts is not None:
        voice.session.say(text, allow_interruptions=allow_interruptions)
    else:
        voice.session.generate_reply(
            instructions=text,
            allow_interruptions=allow_interruptions,
        )


async def _request_amd_decision(
    service: CallService,
    context: ActiveCallContext,
    disconnected: asyncio.Event,
) -> str:
    request = await service.request_input(
        context.call_id,
        "Mailbox erkannt: auflegen, Nachricht hinterlassen oder normal fortfahren?",
        ["hangup", "leave_message", "continue"],
    )
    context.pending_input_request_id = request.id
    context.status = CallStatus.INPUT_REQUIRED
    while not disconnected.is_set():
        current = await service.get_input_request(request.id)
        if current is None:
            break
        if current.status == "resolved":
            context.pending_input_request_id = None
            context.status = CallStatus.ACTIVE
            response = current.response or {}
            choice = response.get("choice") or response.get("action")
            return str(choice) if choice in {"hangup", "leave_message", "continue"} else "hangup"
        if current.expires_at and current.expires_at <= utc_now():
            await service.expire_input(context.call_id, request.id)
            break
        await asyncio.sleep(0.25)
    current = await service.get_input_request(request.id)
    if current is not None and current.status == "pending":
        await service.expire_input(context.call_id, request.id)
    context.pending_input_request_id = None
    context.status = CallStatus.ACTIVE
    return "hangup"


async def _cancel_audio_task(task: asyncio.Task[rtc.AudioFrame] | None) -> None:
    if task is None or task.done():
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def _finish_amd_call(
    context: ActiveCallContext,
    finish_event: asyncio.Event,
    *,
    reason: str,
    summary: str,
    success: bool = False,
) -> None:
    state = context.snapshot()
    await context.persist_completion(
        CallOutcome(
            success=success,
            reason=reason,
            summary=summary,
            facts=state.facts,
            commitments=state.commitments,
        )
    )
    finish_event.set()


async def _close_voice_session(session: AgentSession[Any], *, drain: bool) -> None:
    closed = asyncio.Event()

    def on_close(_: Any) -> None:
        closed.set()

    session.on("close", on_close)
    session.shutdown(drain=drain)
    await closed.wait()


async def _persist_session_report(
    ctx: JobContext,
    session: AgentSession[Any],
    context: ActiveCallContext,
) -> None:
    session_report = ctx.make_session_report(session)
    report = session_report.to_dict()
    usage = report.get("usage")
    model_usage = usage if isinstance(usage, list) else []
    events = report.get("events")
    event_types: dict[str, int] = {}
    if isinstance(events, list):
        for item in events:
            if isinstance(item, dict):
                event_type = str(item.get("type", "unknown"))
                event_types[event_type] = event_types.get(event_type, 0) + 1
    started_at = session_report.started_at
    timestamp = session_report.timestamp
    duration_seconds = None
    if isinstance(started_at, int | float) and isinstance(timestamp, int | float):
        duration_seconds = max(0.0, float(timestamp) - float(started_at))
    summary: dict[str, Any] = {
        "job_id": report.get("job_id"),
        "room_id": report.get("room_id"),
        "room": report.get("room"),
        "sdk_version": report.get("sdk_version"),
        "started_at": started_at,
        "ended_at": timestamp,
        "duration_seconds": duration_seconds,
        "options": report.get("options"),
        "event_counts": event_types,
        "usage": model_usage,
    }
    async with context.state_transaction():
        context.record_model_usage(model_usage)
        context.record_session_report(summary)
        await context.persist_state_durable(
            "call.voice_session_reported",
            summary,
            expected_statuses={
                CallStatus.ACTIVE,
                CallStatus.INPUT_REQUIRED,
                CallStatus.COMPLETING,
            },
        )


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
    ivr_timeout: asyncio.Event,
) -> str:
    waiters = {
        asyncio.create_task(disconnected.wait()): "disconnected",
        asyncio.create_task(finish.wait()): "finish",
        asyncio.create_task(watchdog.wait()): "watchdog",
        asyncio.create_task(ivr_timeout.wait()): "ivr_timeout",
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
