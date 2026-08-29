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
