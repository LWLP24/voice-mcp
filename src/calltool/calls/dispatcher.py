from __future__ import annotations

import json
from typing import Protocol

from livekit import api

from calltool.calls.errors import DispatchError
from calltool.calls.models import CallRecord, DispatchResult
from calltool.config import Settings


class CallDispatcher(Protocol):
    async def dispatch(self, call: CallRecord) -> DispatchResult: ...

    async def cancel(self, call: CallRecord) -> None: ...

    async def close(self) -> None: ...


class LiveKitCallDispatcher:
    agent_name = "calltool"

    def __init__(self, settings: Settings) -> None:
        self._client = api.LiveKitAPI(
            url=settings.LIVEKIT_URL,
            api_key=settings.LIVEKIT_API_KEY,
            api_secret=settings.LIVEKIT_API_SECRET.get_secret_value(),
        )

    async def dispatch(self, call: CallRecord) -> DispatchResult:
        room_name = f"call-{call.id.removeprefix('call_').lower()}"
        metadata = json.dumps({"call_id": call.id}, separators=(",", ":"))
        try:
            await self._client.room.create_room(
                api.CreateRoomRequest(name=room_name, empty_timeout=300, departure_timeout=20)
            )
            dispatch = await self._client.agent_dispatch.create_dispatch(
                api.CreateAgentDispatchRequest(
                    agent_name=self.agent_name,
                    room=room_name,
                    metadata=metadata,
                )
            )
        except Exception as exc:
            raise DispatchError(f"LiveKit dispatch failed: {exc}") from exc
        return DispatchResult(room_name=room_name, dispatch_id=dispatch.id)

    async def cancel(self, call: CallRecord) -> None:
        room_name = call.state.room_name
        if not room_name:
            return
        try:
            await self._client.room.delete_room(api.DeleteRoomRequest(room=room_name))
        except Exception as exc:
            raise DispatchError(f"Could not delete LiveKit room {room_name}: {exc}") from exc

    async def close(self) -> None:
        await self._client.aclose()


class NullCallDispatcher:
    """Test dispatcher that records enough state without external services."""

    async def dispatch(self, call: CallRecord) -> DispatchResult:
        suffix = call.id.removeprefix("call_").lower()
        return DispatchResult(room_name=f"call-{suffix}", dispatch_id=f"dispatch-{suffix}")

    async def cancel(self, call: CallRecord) -> None:
        return None

    async def close(self) -> None:
        return None
