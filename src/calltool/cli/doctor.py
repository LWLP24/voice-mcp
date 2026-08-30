from __future__ import annotations

import asyncio
import importlib.metadata
from dataclasses import dataclass

import asyncpg
from livekit import api
from redis.asyncio import Redis

from calltool.calls.dispatcher import LiveKitCallDispatcher
from calltool.calls.models import CallCreateRequest, CallPermissions, CallTarget
from calltool.calls.service import CallService
from calltool.config import get_settings
from calltool.policy.engine import PolicyEngine
from calltool.storage.postgres import PostgresCallRepository
from calltool.voice.prompts import PromptProfile
from calltool.voice.realtime import resolve_voice_selection


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str = ""
    informational: bool = False

    def render(self) -> str:
        status = "INFO" if self.informational else "OK" if self.ok else "FAIL"
        suffix = f" — {self.detail}" if self.detail else ""
        return f"[{status}] {self.name}{suffix}"


async def run(call_number: str | None = None) -> int:
    settings = get_settings()
    checks: list[Check] = []

    try:
        connection = await asyncpg.connect(settings.DATABASE_URL, timeout=5)
        await connection.fetchval("SELECT 1")
        await connection.close()
        checks.append(Check("PostgreSQL", True))
    except Exception as exc:
        checks.append(Check("PostgreSQL", False, str(exc)))

    redis_client: Redis = Redis.from_url(settings.REDIS_URL)
    try:
        await redis_client.ping()
        checks.append(Check("Redis", True))
    except Exception as exc:
        checks.append(Check("Redis", False, str(exc)))
    finally:
        await redis_client.aclose()

    livekit: api.LiveKitAPI | None = None
    try:
        livekit = api.LiveKitAPI(
            url=settings.LIVEKIT_URL,
            api_key=settings.LIVEKIT_API_KEY,
            api_secret=settings.LIVEKIT_API_SECRET.get_secret_value(),
        )
        async with asyncio.timeout(5):
            await livekit.room.list_rooms(api.ListRoomsRequest())
        checks.append(Check("LiveKit", True, settings.LIVEKIT_URL))
    except Exception as exc:
        checks.append(Check("LiveKit", False, str(exc)))
    finally:
        if livekit is not None:
            await livekit.aclose()

    checks.append(
        Check(
            "LiveKit SIP trunk",
            bool(settings.LIVEKIT_SIP_TRUNK_ID),
            settings.LIVEKIT_SIP_TRUNK_ID or "LIVEKIT_SIP_TRUNK_ID missing",
        )
    )
    telnyx_configured = all(
        (
            settings.TELNYX_SIP_ADDRESS,
            settings.TELNYX_SIP_USERNAME,
            settings.TELNYX_SIP_PASSWORD.get_secret_value(),
            settings.TELNYX_FROM_NUMBER,
        )
    )
    checks.append(
        Check(
            "Telnyx configuration",
            telnyx_configured,
            settings.TELNYX_SIP_ADDRESS
            if telnyx_configured
            else "address, credentials, or caller number missing",
        )
    )
    try:
        prompt_profile = PromptProfile.load(settings)
        checks.append(
            Check(
                "Prompt profile",
                True,
                f"{prompt_profile.directory} ({len(prompt_profile.source_files)} files)",
            )
        )
    except ValueError as exc:
        checks.append(Check("Prompt profile", False, str(exc)))
    if settings.config.calls.inbound.enabled:
        inbound_client: api.LiveKitAPI | None = None
        try:
            inbound_client = api.LiveKitAPI(
                url=settings.LIVEKIT_URL,
                api_key=settings.LIVEKIT_API_KEY,
                api_secret=settings.LIVEKIT_API_SECRET.get_secret_value(),
            )
            async with asyncio.timeout(5):
                trunks = await inbound_client.sip.list_sip_inbound_trunk(
                    api.ListSIPInboundTrunkRequest(numbers=[settings.TELNYX_FROM_NUMBER])
                )
                trunk = next(
                    (
                        item
                        for item in trunks.items
                        if item.name == "CallTool Telnyx Inbound"
                        and settings.TELNYX_FROM_NUMBER in item.numbers
                    ),
                    None,
                )
                if trunk is None:
                    checks.append(
                        Check(
                            "LiveKit inbound routing",
                            False,
                            "inbound trunk missing; run calltool sip bootstrap",
                        )
                    )
                else:
                    rules = await inbound_client.sip.list_sip_dispatch_rule(
                        api.ListSIPDispatchRuleRequest(trunk_ids=[trunk.sip_trunk_id])
                    )
                    dispatch = next(
                        (item for item in rules.items if item.name == "CallTool Telnyx Inbound"),
                        None,
                    )
                    checks.append(
                        Check(
                            "LiveKit inbound routing",
                            dispatch is not None,
                            (
                                f"trunk {trunk.sip_trunk_id}, dispatch {dispatch.sip_dispatch_rule_id}"
                                if dispatch is not None
                                else f"trunk {trunk.sip_trunk_id}, dispatch rule missing"
                            ),
                        )
                    )
        except Exception as exc:
            checks.append(Check("LiveKit inbound routing", False, str(exc)))
        finally:
            if inbound_client is not None:
                await inbound_client.aclose()
    try:
        voice_selection = resolve_voice_selection(settings)
        provider_key = (
            settings.OPENAI_API_KEY.get_secret_value()
            if voice_selection.provider == "openai"
            else settings.GOOGLE_API_KEY.get_secret_value()
        )
        checks.append(
            Check(
                f"{voice_selection.provider.title()} Realtime",
                bool(provider_key),
                (
                    f"{voice_selection.model}, language={voice_selection.language}, "
                    f"voice={voice_selection.voice}"
                ),
            )
        )
    except ValueError as exc:
        voice_selection = None
        checks.append(Check("Realtime voice configuration", False, str(exc)))

    supervisor_enabled = settings.config.voice.supervisor.enabled
    checks.append(
        Check(
            "Gemini supervisor",
            not supervisor_enabled or bool(settings.GOOGLE_API_KEY.get_secret_value()),
            settings.config.voice.supervisor.model if supervisor_enabled else "disabled",
            informational=not supervisor_enabled,
        )
    )
    scripted_tts_enabled = bool(
        voice_selection is not None
        and voice_selection.provider == "gemini"
        and settings.config.voice.scripted_tts.enabled
    )
    checks.append(
        Check(
            "Gemini scripted TTS",
            not scripted_tts_enabled or bool(settings.GOOGLE_API_KEY.get_secret_value()),
            (
                settings.config.voice.scripted_tts.model
                if scripted_tts_enabled
                else "provider-native realtime greeting"
            ),
            informational=not scripted_tts_enabled,
        )
    )
    shadow_stt_enabled = settings.config.voice.shadow_stt.enabled
    checks.append(
        Check(
            "Shadow STT",
            not shadow_stt_enabled or bool(settings.GOOGLE_API_KEY.get_secret_value()),
            settings.config.voice.shadow_stt.model if shadow_stt_enabled else "disabled",
            informational=not shadow_stt_enabled,
        )
    )
    checks.append(
        Check(
            "MCP",
            importlib.metadata.version("mcp") == "2.1.1",
            f"2026-07-28 / SDK {importlib.metadata.version('mcp')}",
        )
    )

    if call_number:
        try:
            repository = await PostgresCallRepository.connect(settings.DATABASE_URL)
            dispatcher = LiveKitCallDispatcher(settings)
            service = CallService(
                repository,
                dispatcher,
                PolicyEngine(settings.config.policy),
                settings,
            )
            call = await service.create_call(
                CallCreateRequest(
                    target=CallTarget(phone_number=call_number, name="Diagnostic target"),
                    objective="Kurzer CallTool-Diagnosetest; begrüßen und höflich auflegen.",
                    permissions=CallPermissions(may_commit=False),
                ),
                principal_id="doctor",
            )
            checks.append(Check("Diagnostic call", True, call.id))
            await service.close()
        except Exception as exc:
            checks.append(Check("Diagnostic call", False, str(exc)))

    for check in checks:
        print(check.render())
    failed = any(not check.ok and not check.informational for check in checks)
    print("NOT READY" if failed else "READY")
    return 1 if failed else 0
