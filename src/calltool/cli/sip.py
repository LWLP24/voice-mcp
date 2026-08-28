from __future__ import annotations

from livekit import api

from calltool.config import Settings, get_settings


def build_telnyx_trunk(settings: Settings) -> api.SIPOutboundTrunkInfo:
    return api.SIPOutboundTrunkInfo(
        name="CallTool Telnyx Europe",
        address=settings.TELNYX_SIP_ADDRESS,
        destination_country="DE",
        numbers=[settings.TELNYX_FROM_NUMBER],
        auth_username=settings.TELNYX_SIP_USERNAME,
        auth_password=settings.TELNYX_SIP_PASSWORD.get_secret_value(),
        transport=api.SIP_TRANSPORT_TCP,
        headers_to_attributes={
            "X-Telnyx-Username": settings.TELNYX_SIP_USERNAME
        },
    )


async def bootstrap() -> int:
    settings = get_settings()
    required = {
        "TELNYX_SIP_USERNAME": settings.TELNYX_SIP_USERNAME,
        "TELNYX_SIP_PASSWORD": settings.TELNYX_SIP_PASSWORD.get_secret_value(),
        "TELNYX_FROM_NUMBER": settings.TELNYX_FROM_NUMBER,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        print(f"Missing Telnyx settings: {', '.join(missing)}")
        return 1

    client = api.LiveKitAPI(
        url=settings.LIVEKIT_URL,
        api_key=settings.LIVEKIT_API_KEY,
        api_secret=settings.LIVEKIT_API_SECRET.get_secret_value(),
    )
    try:
        response = await client.sip.list_sip_outbound_trunk(
            api.ListSIPOutboundTrunkRequest(numbers=[settings.TELNYX_FROM_NUMBER])
        )
        existing = next(
            (
                trunk
                for trunk in response.items
                if trunk.address == settings.TELNYX_SIP_ADDRESS
                and settings.TELNYX_FROM_NUMBER in trunk.numbers
            ),
            None,
        )
        if existing is not None:
            print(f"Telnyx outbound trunk already exists: {existing.sip_trunk_id}")
            print(f"Set LIVEKIT_SIP_TRUNK_ID={existing.sip_trunk_id}")
            return 0

        trunk = await client.sip.create_sip_outbound_trunk(
            api.CreateSIPOutboundTrunkRequest(
                trunk=build_telnyx_trunk(settings)
            )
        )
        print(f"Created Telnyx outbound trunk: {trunk.sip_trunk_id}")
        print(f"Set LIVEKIT_SIP_TRUNK_ID={trunk.sip_trunk_id}")
        return 0
    finally:
        await client.aclose()
