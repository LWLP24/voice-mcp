from __future__ import annotations

from google.protobuf.duration_pb2 import Duration
from livekit import api
from livekit.agents import JobContext
from livekit.protocol import sip

from calltool.calls.models import CallRecord, TransferStatus
from calltool.config import Settings
from calltool.voice.tools import TransferResult


async def dial_call(
    ctx: JobContext, call: CallRecord, settings: Settings
) -> api.SIPParticipantInfo:
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
            participant_attributes={
                "calltool.call_id": call.id,
                "calltool.direction": call.direction.value,
            },
            wait_until_answered=True,
            ringing_timeout=Duration(seconds=settings.config.calls.ring_timeout_seconds),
            max_call_duration=Duration(seconds=settings.config.calls.max_duration_seconds),
            krisp_enabled=settings.krisp_enabled(),
        )
    )


async def transfer_call(
    ctx: JobContext,
    *,
    participant_identity: str,
    target_number: str,
    settings: Settings,
) -> TransferResult:
    config = settings.config.telephony.cold_transfer
    response = await ctx.api.sip.transfer_sip_participant(
        api.TransferSIPParticipantRequest(
            participant_identity=participant_identity,
            room_name=ctx.room.name,
            transfer_to=f"tel:{target_number}",
            play_dialtone=config.play_dialtone,
            ringing_timeout=Duration(seconds=config.ringing_timeout_seconds),
        )
    )
    statuses = {
        sip.SIPTransferStatus.STS_TRANSFER_ONGOING: TransferStatus.ONGOING,
        sip.SIPTransferStatus.STS_TRANSFER_FAILED: TransferStatus.FAILED,
        sip.SIPTransferStatus.STS_TRANSFER_SUCCESSFUL: TransferStatus.SUCCESSFUL,
    }
    reason = sip.SIPTransferReason.Name(response.reason).removeprefix("STR_").lower()
    sip_status = None
    if response.sip_status.code or response.sip_status.status:
        sip_status = f"{response.sip_status.code}:{response.sip_status.status}"
    return TransferResult(
        status=statuses.get(response.status, TransferStatus.FAILED),
        transfer_id=response.transfer_id or None,
        reason=reason,
        sip_status=sip_status,
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
