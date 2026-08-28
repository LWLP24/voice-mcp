from __future__ import annotations

import asyncio
import json
from typing import Any

from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from calltool.api.mcp import ServiceSlot
from calltool.api.schemas import call_response
from calltool.calls.errors import (
    CallNotFoundError,
    CallToolError,
    ConcurrencyLimitError,
    DispatchError,
    InputRequestNotFoundError,
    InvalidStateTransitionError,
    PolicyDeniedError,
)
from calltool.calls.models import CallCreateRequest, InputResponse


def build_rest_routes(slot: ServiceSlot, base_path: str) -> list[Route]:
    async def create(request: Request) -> Response:
        try:
            payload = await request.json()
            call_request = CallCreateRequest.model_validate(payload)
            call = await slot.get().create_call(
                call_request,
                principal_id=_principal(request),
            )
            return JSONResponse(call_response(call), status_code=202)
        except Exception as exc:
            return error_response(exc)

    async def status(request: Request) -> Response:
        try:
            call = await slot.get().get_call(
                request.path_params["call_id"], principal_id=_principal(request)
            )
            return JSONResponse(call_response(call))
        except Exception as exc:
            return error_response(exc)

    async def respond(request: Request) -> Response:
        try:
            payload = InputResponse.model_validate(await request.json())
            call = await slot.get().respond(
                request.path_params["call_id"],
                payload.input_request_id,
                payload.response,
                principal_id=_principal(request),
            )
            return JSONResponse(call_response(call))
        except Exception as exc:
            return error_response(exc)

    async def cancel(request: Request) -> Response:
        try:
            call = await slot.get().cancel_call(
                request.path_params["call_id"], principal_id=_principal(request)
            )
            return JSONResponse(call_response(call))
        except Exception as exc:
            return error_response(exc)

    async def events(request: Request) -> Response:
        call_id = request.path_params["call_id"]
        principal = _principal(request)
        accepts_sse = "text/event-stream" in request.headers.get("accept", "")
        after = int(request.query_params.get("after", "0"))
        if not accepts_sse:
            try:
                call_events = await slot.get().list_events(
                    call_id,
                    principal_id=principal,
                    after_sequence=after,
                )
                return JSONResponse(
                    {"events": [event.model_dump(mode="json") for event in call_events]}
                )
            except Exception as exc:
                return error_response(exc)

        async def event_stream() -> Any:
            sequence = after
            while not await request.is_disconnected():
                call_events = await slot.get().list_events(
                    call_id,
                    principal_id=principal,
                    after_sequence=sequence,
                )
                for event in call_events:
                    sequence = event.sequence
                    data = json.dumps(event.model_dump(mode="json"), ensure_ascii=False)
                    yield f"id: {event.sequence}\nevent: {event.type}\ndata: {data}\n\n"
                call = await slot.get().get_call(call_id, principal_id=principal)
                if call.status.terminal:
                    return
                yield ": keepalive\n\n"
                await asyncio.sleep(1)

        try:
            await slot.get().get_call(call_id, principal_id=principal)
        except Exception as exc:
            return error_response(exc)
        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return [
        Route(f"{base_path}/calls", create, methods=["POST"]),
        Route(f"{base_path}/calls/{{call_id}}", status, methods=["GET"]),
        Route(f"{base_path}/calls/{{call_id}}/respond", respond, methods=["POST"]),
        Route(f"{base_path}/calls/{{call_id}}/cancel", cancel, methods=["POST"]),
        Route(f"{base_path}/calls/{{call_id}}/events", events, methods=["GET"]),
    ]


def _principal(request: Request) -> str:
    return str(request.state.principal_id)


def error_response(exc: Exception) -> JSONResponse:
    status_code = 500
    code = "internal_error"
    message = "Internal server error"

    if isinstance(exc, ValidationError):
        status_code, code, message = 422, "validation_error", str(exc)
    elif isinstance(exc, (CallNotFoundError, InputRequestNotFoundError)):
        status_code, code, message = 404, "not_found", str(exc)
    elif isinstance(exc, PolicyDeniedError):
        status_code, code, message = 422, "policy_denied", exc.reason
    elif isinstance(exc, ConcurrencyLimitError):
        status_code, code, message = 429, "concurrency_limit", str(exc)
    elif isinstance(exc, InvalidStateTransitionError):
        status_code, code, message = 409, "invalid_state", str(exc)
    elif isinstance(exc, DispatchError):
        status_code, code, message = 503, "dispatch_failed", str(exc)
    elif isinstance(exc, (CallToolError, json.JSONDecodeError)):
        status_code, code, message = 400, "bad_request", str(exc)

    return JSONResponse(
        {"error": {"code": code, "message": message}},
        status_code=status_code,
    )
