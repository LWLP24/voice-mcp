import pytest
from mcp.types import CallToolResult

from calltool.api.mcp import ServiceSlot, build_mcp_server
from calltool.calls.dispatcher import NullCallDispatcher
from calltool.calls.service import CallService
from calltool.config import Settings
from calltool.policy.engine import PolicyEngine
from calltool.storage.memory import MemoryCallRepository


@pytest.mark.asyncio
async def test_mcp_exposes_required_tools_and_creates_call() -> None:
    settings = Settings(CALLTOOL_ENV="test")
    repository = MemoryCallRepository()
    service = CallService(
        repository,
        NullCallDispatcher(),
        PolicyEngine(settings.config.policy),
        settings,
    )
    slot = ServiceSlot(service)
    server = build_mcp_server(slot)

    tools = await server.list_tools()
    assert {tool.name for tool in tools} == {
        "phone_call.create",
        "phone_call.status",
        "phone_call.list",
        "phone_call.conversation",
        "phone_call.respond",
        "phone_call.cancel",
    }

    result = await server.call_tool(
        "phone_call.create",
        {
            "target": {"phone_number": "+49301234567", "name": "Praxis"},
            "objective": "Vereinbare einen Termin",
            "permissions": {"may_commit": True},
            "voice": {
                "provider": "openai",
                "model": "gpt-realtime-2.1-mini",
                "language": "en-US",
                "voice": "marin",
            },
        },
    )
    assert isinstance(result, CallToolResult)
    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["status"] == "queued"
    assert result.structured_content["request"]["voice"]["provider"] == "openai"

    inbound = await service.create_inbound_call(
        caller_number="+491701234567",
        called_number="+49301234567",
        sip_participant_identity="sip-caller",
        room_name="calltool-inbound-mcp-test",
        sip_call_id="telnyx-mcp-test",
        principal_id="mcp",
    )
    await repository.append_transcript_turn(
        inbound.id,
        role="user",
        text="Bitte rufen Sie mich morgen zurück.",
    )

    listed = await server.call_tool("phone_call.list", {"limit": 10})
    assert listed.is_error is False
    assert listed.structured_content is not None
    assert listed.structured_content["calls"][0]["call_id"] == inbound.id
    assert listed.structured_content["calls"][0]["caller_number"] == "+491701234567"

    conversation = await server.call_tool("phone_call.conversation", {"call_id": inbound.id})
    assert conversation.is_error is False
    assert conversation.structured_content is not None
    assert conversation.structured_content["transcript"][0]["role"] == "user"
    assert conversation.structured_content["transcript"][0]["text"] == (
        "Bitte rufen Sie mich morgen zurück."
    )
