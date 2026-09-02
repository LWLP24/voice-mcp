from __future__ import annotations

import json

from google.protobuf.duration_pb2 import Duration
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
        headers_to_attributes={"X-Telnyx-Username": settings.TELNYX_SIP_USERNAME},
    )


def build_telnyx_inbound_trunk(settings: Settings) -> api.SIPInboundTrunkInfo:
    inbound = settings.config.calls.inbound
    return api.SIPInboundTrunkInfo(
        name="CallTool Telnyx Inbound",
        numbers=[settings.TELNYX_FROM_NUMBER],
        allowed_addresses=inbound.allowed_addresses,
        max_call_duration=Duration(seconds=settings.config.calls.max_duration_seconds),
    )


def build_inbound_dispatch_rule(settings: Settings, trunk_id: str) -> api.SIPDispatchRuleInfo:
    return api.SIPDispatchRuleInfo(
        name="CallTool Telnyx Inbound",
        trunk_ids=[trunk_id],
        rule=api.SIPDispatchRule(
            dispatch_rule_individual=api.SIPDispatchRuleIndividual(
                room_prefix=settings.config.calls.inbound.room_prefix
            )
        ),
        room_config=api.RoomConfiguration(
            agents=[
                api.RoomAgentDispatch(
                    agent_name="calltool",
                    metadata=json.dumps({"direction": "inbound"}, separators=(",", ":")),
                )
            ]
        ),
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
        outbound_response = await client.sip.list_sip_outbound_trunk(
            api.ListSIPOutboundTrunkRequest(numbers=[settings.TELNYX_FROM_NUMBER])
        )
        outbound = next(
            (
                trunk
                for trunk in outbound_response.items
                if trunk.address == settings.TELNYX_SIP_ADDRESS
                and settings.TELNYX_FROM_NUMBER in trunk.numbers
            ),
            None,
        )
        if outbound is None:
            outbound = await client.sip.create_sip_outbound_trunk(
                api.CreateSIPOutboundTrunkRequest(trunk=build_telnyx_trunk(settings))
            )
            print(f"Created Telnyx outbound trunk: {outbound.sip_trunk_id}")
        else:
            outbound = await client.sip.update_sip_outbound_trunk(
                outbound.sip_trunk_id,
                build_telnyx_trunk(settings),
            )
            print(f"Updated Telnyx outbound trunk: {outbound.sip_trunk_id}")
        print(f"Set LIVEKIT_SIP_TRUNK_ID={outbound.sip_trunk_id}")

        if settings.config.calls.inbound.enabled:
            inbound_response = await client.sip.list_sip_inbound_trunk(
                api.ListSIPInboundTrunkRequest(numbers=[settings.TELNYX_FROM_NUMBER])
            )
            inbound = next(
                (
                    trunk
                    for trunk in inbound_response.items
                    if trunk.name == "CallTool Telnyx Inbound"
                    and settings.TELNYX_FROM_NUMBER in trunk.numbers
                ),
                None,
            )
            if inbound is None:
                inbound = await client.sip.create_sip_inbound_trunk(
                    api.CreateSIPInboundTrunkRequest(trunk=build_telnyx_inbound_trunk(settings))
                )
                print(f"Created Telnyx inbound trunk: {inbound.sip_trunk_id}")
            else:
                inbound = await client.sip.update_sip_inbound_trunk(
                    inbound.sip_trunk_id,
                    build_telnyx_inbound_trunk(settings),
                )
                print(f"Updated Telnyx inbound trunk: {inbound.sip_trunk_id}")

            dispatch_response = await client.sip.list_sip_dispatch_rule(
                api.ListSIPDispatchRuleRequest(trunk_ids=[inbound.sip_trunk_id])
            )
            dispatch = next(
                (
                    item
                    for item in dispatch_response.items
                    if item.name == "CallTool Telnyx Inbound"
                ),
                None,
            )
            if dispatch is None:
                dispatch = await client.sip.create_sip_dispatch_rule(
                    api.CreateSIPDispatchRuleRequest(
                        dispatch_rule=build_inbound_dispatch_rule(settings, inbound.sip_trunk_id)
                    )
                )
                print(f"Created inbound dispatch rule: {dispatch.sip_dispatch_rule_id}")
            else:
                dispatch = await client.sip.update_sip_dispatch_rule(
                    dispatch.sip_dispatch_rule_id,
                    build_inbound_dispatch_rule(settings, inbound.sip_trunk_id),
                )
                print(f"Updated inbound dispatch rule: {dispatch.sip_dispatch_rule_id}")
        return 0
    finally:
        await client.aclose()
