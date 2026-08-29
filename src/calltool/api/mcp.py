from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mcp.server.mcpserver import MCPServer

from calltool.api.schemas import call_response
from calltool.calls.models import (
    CallCreateRequest,
    CallPermissions,
    CallTarget,
    CallVoiceOptions,
)
from calltool.calls.service import CallService


@dataclass
class ServiceSlot:
    service: CallService | None = None

    def get(self) -> CallService:
        if self.service is None:
            raise RuntimeError("CallTool is not ready")
        return self.service


def build_mcp_server(slot: ServiceSlot, *, principal_id: str = "mcp") -> MCPServer[None]:
    server: MCPServer[None] = MCPServer(
        name="calltool",
        title="CallTool",
        description="Start and control outbound telephone calls",
        version="0.1.0",
        instructions=(
            "Calls are asynchronous. Create a call, poll phone_call.status, and answer "
            "phone_call.respond when the status is input_required."
        ),
    )

    @server.tool(
        name="phone_call.create",
        description="Create an outbound phone call job and return immediately.",
        structured_output=True,
    )
    async def create_call(
        target: CallTarget,
        objective: str,
        constraints: list[str] | None = None,
        context: dict[str, Any] | None = None,
        permissions: CallPermissions | None = None,
        voice: CallVoiceOptions | None = None,
        client_request_id: str | None = None,
    ) -> dict[str, Any]:
        call = await slot.get().create_call(
            CallCreateRequest(
                target=target,
                objective=objective,
                constraints=constraints or [],
                context=context or {},
                permissions=permissions or CallPermissions(),
                voice=voice or CallVoiceOptions(),
                client_request_id=client_request_id,
            ),
            principal_id=principal_id,
        )
        return call_response(call)

    @server.tool(
        name="phone_call.status",
        description="Read current status, pending input, outcome, and error for a call.",
        structured_output=True,
    )
    async def call_status(call_id: str) -> dict[str, Any]:
        call = await slot.get().get_call(call_id, principal_id=principal_id)
        return call_response(call)

    @server.tool(
        name="phone_call.respond",
        description="Resolve a pending human-in-the-loop request for a call.",
        structured_output=True,
    )
    async def respond(
        call_id: str, input_request_id: str, response: dict[str, Any]
    ) -> dict[str, Any]:
        call = await slot.get().respond(
            call_id,
            input_request_id,
            response,
            principal_id=principal_id,
        )
        return call_response(call)

    @server.tool(
        name="phone_call.cancel",
        description="Idempotently cancel a queued, ringing, or active call.",
        structured_output=True,
    )
    async def cancel(call_id: str) -> dict[str, Any]:
        call = await slot.get().cancel_call(call_id, principal_id=principal_id)
        return call_response(call)

    return server
