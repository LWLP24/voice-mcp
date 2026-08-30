from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from livekit import api
from livekit.protocol import sip

from calltool.calls.models import (
    ActiveCallState,
    CallCreateRequest,
    CallRecord,
    CallStatus,
    CallTarget,
    TransferStatus,
)
from calltool.config import Settings
from calltool.worker.dialer import dial_call, transfer_call


def make_call() -> CallRecord:
    request = CallCreateRequest(
        target=CallTarget(phone_number="+49301234567", name="Praxis"),
        objective="Termin vereinbaren",
    )
    return CallRecord(
        id="call_dialer",
        principal_id="test",
        status=CallStatus.DIALING,
        target_number=request.target.phone_number,
        request=request,
        state=ActiveCallState(objective=request.objective),
    )


@pytest.mark.asyncio
async def test_dialer_maps_call_attributes_and_keeps_krisp_opt_in() -> None:
    ctx = MagicMock()
    ctx.room.name = "calltool-call-dialer"
    ctx.api.sip.create_sip_participant = AsyncMock(return_value=api.SIPParticipantInfo())
    settings = Settings(
        CALLTOOL_ENV="test",
        LIVEKIT_SIP_TRUNK_ID="ST_test",
        TELNYX_FROM_NUMBER="+49309999999",
        CALLTOOL_KRISP_ENABLED="false",
    )

    await dial_call(ctx, make_call(), settings)

    request = ctx.api.sip.create_sip_participant.await_args.args[0]
    assert request.krisp_enabled is False
    assert request.participant_attributes == {
        "calltool.call_id": "call_dialer",
        "calltool.direction": "outbound",
    }
    assert request.sip_call_to == "+49301234567"


@pytest.mark.asyncio
async def test_cold_transfer_uses_tel_uri_and_maps_livekit_result() -> None:
    response = api.TransferSIPParticipantResponse(
        transfer_id="transfer_123",
        status=sip.SIPTransferStatus.STS_TRANSFER_SUCCESSFUL,
        reason=sip.SIPTransferReason.STR_COMPLETED,
        sip_status=api.SIPStatus(code=sip.SIPStatusCode.SIP_STATUS_OK, status="OK"),
    )
    ctx = MagicMock()
    ctx.room.name = "calltool-call-dialer"
    ctx.api.sip.transfer_sip_participant = AsyncMock(return_value=response)
    settings = Settings(CALLTOOL_ENV="test")

    result = await transfer_call(
        ctx,
        participant_identity="callee-dialer",
        target_number="+49309876543",
        settings=settings,
    )

    request = ctx.api.sip.transfer_sip_participant.await_args.args[0]
    assert request.transfer_to == "tel:+49309876543"
    assert request.participant_identity == "callee-dialer"
    assert request.ringing_timeout.seconds == 30
    assert result.status is TransferStatus.SUCCESSFUL
    assert result.transfer_id == "transfer_123"
    assert result.reason == "completed"
    assert result.sip_status == "200:OK"
