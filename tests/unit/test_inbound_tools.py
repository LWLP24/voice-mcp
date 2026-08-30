import asyncio
from unittest.mock import MagicMock

from calltool.calls.models import CallDirection
from calltool.voice.tools import ToolRuntime, build_tools


def test_inbound_call_only_exposes_safe_tools() -> None:
    runtime = ToolRuntime(
        call_id="call_test",
        context=MagicMock(),
        service=MagicMock(),
        policy=MagicMock(),
        room=MagicMock(),
        finish_event=asyncio.Event(),
    )

    tools = build_tools(runtime, direction=CallDirection.INBOUND)

    assert [tool.info.name for tool in tools] == ["record_fact", "finish_call"]
