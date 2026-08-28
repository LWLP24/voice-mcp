from __future__ import annotations

from google.protobuf.duration_pb2 import Duration
from livekit import api
from livekit.agents import JobContext

from calltool.calls.models import CallRecord
from calltool.config import Settings


async def dial_call(ctx: JobContext, call: CallRecord, settings: Settings) -> api.SIPParticipantInfo:
    trunk_id = settings.LIVEKIT_SIP_TRUNK_ID
    if not trunk_id:
        raise RuntimeError("LIVEKIT_SIP_TRUNK_ID is not configured")
    participant_identity = f"callee-{call.id.removeprefix('call_').lower()}"
    return await ctx.api.sip.create_sip_participant(
        api.CreateSIPParticipantRequest(
            sip_trunk_id=trunk_id,
            sip_call_to=call.target_number,
            sip_number=settings.TELNYX_FROM_NUMBER,
            room_name=ctx.room.name,
            participant_identity=participant_identity,
            participant_name=call.request.target.name or "Call recipient",
            wait_until_answered=True,
            ringing_timeout=Duration(
                seconds=settings.config.calls.ring_timeout_seconds
            ),
            max_call_duration=Duration(
                seconds=settings.config.calls.max_duration_seconds
            ),
        )
    )


def sip_error(error: api.SipCallError) -> tuple[str, bool]:
    status_code = error.sip_status_code
    if status_code == 486:
        return "busy", True
    if status_code == 603:
        return "rejected", False
    if status_code == 408:
        return "no_answer", True
    if status_code == 480:
        return "unavailable", True
    if status_code is not None and status_code >= 500:
        return "provider_failure", True
    return "sip_failure", False
