from __future__ import annotations

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

    try:
        livekit = api.LiveKitAPI(
            url=settings.LIVEKIT_URL,
            api_key=settings.LIVEKIT_API_KEY,
            api_secret=settings.LIVEKIT_API_SECRET.get_secret_value(),
        )
        await livekit.room.list_rooms(api.ListRoomsRequest())
        checks.append(Check("LiveKit", True, settings.LIVEKIT_URL))
        await livekit.aclose()
    except Exception as exc:
        checks.append(Check("LiveKit", False, str(exc)))

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
    if settings.config.calls.inbound.enabled:
        inbound_client: api.LiveKitAPI | None = None
        try:
            inbound_client = api.LiveKitAPI(
                url=settings.LIVEKIT_URL,
                api_key=settings.LIVEKIT_API_KEY,
                api_secret=settings.LIVEKIT_API_SECRET.get_secret_value(),
            )
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
    checks.append(
        Check(
            "Gemini Live",
            bool(settings.GOOGLE_API_KEY.get_secret_value()),
            settings.config.voice.realtime.model,
        )
    )
    checks.append(Check("Gemini supervisor", True, settings.config.voice.supervisor.model))
    checks.append(Check("Gemini TTS", True, settings.config.voice.scripted_tts.model))
    checks.append(
        Check(
            "Shadow STT",
            True,
            settings.config.voice.shadow_stt.model
            if settings.config.voice.shadow_stt.enabled
            else "disabled",
            informational=not settings.config.voice.shadow_stt.enabled,
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
