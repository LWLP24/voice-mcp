from __future__ import annotations

from datetime import datetime
from typing import Any

from calltool.calls.models import CallConversation, CallListPage, CallRecord


def call_response(call: CallRecord) -> dict[str, Any]:
    payload: dict[str, Any] = call.model_dump(mode="json", exclude_none=True)
    payload["call_id"] = payload.pop("id")
    return payload


def call_list_response(page: CallListPage) -> dict[str, Any]:
    return {
        "calls": [_call_list_item(call) for call in page.calls],
        "next_cursor": page.next_cursor,
    }


def conversation_response(conversation: CallConversation) -> dict[str, Any]:
    call = conversation.call
    facts = call.outcome.facts if call.outcome is not None else call.state.facts
    return {
        "call": call_response(call),
        "timing": _timing(call),
        "summary": call.outcome.summary if call.outcome is not None else None,
        "facts": facts,
        "transcript": [
            turn.model_dump(mode="json", exclude={"call_id"}) for turn in conversation.transcript
        ],
    }


def _call_list_item(call: CallRecord) -> dict[str, Any]:
    item: dict[str, Any] = {
        "call_id": call.id,
        "direction": call.direction.value,
        "status": call.status.value,
        "remote_number": call.target_number,
        "target_name": call.request.target.name,
        **_timing(call),
        "summary": call.outcome.summary if call.outcome is not None else None,
        "facts": call.outcome.facts if call.outcome is not None else call.state.facts,
    }
    if call.direction.value == "inbound":
        item["caller_number"] = call.target_number
        item["called_number"] = call.request.context.get("called_number")
    else:
        item["callee_number"] = call.target_number
    return item


def _timing(call: CallRecord) -> dict[str, Any]:
    duration_seconds = None
    if call.connected_at is not None and call.ended_at is not None:
        duration_seconds = max(0.0, (call.ended_at - call.connected_at).total_seconds())
    return {
        "created_at": _timestamp(call.created_at),
        "started_at": _timestamp(call.started_at),
        "connected_at": _timestamp(call.connected_at) if call.connected_at else None,
        "ended_at": _timestamp(call.ended_at) if call.ended_at else None,
        "duration_seconds": duration_seconds,
    }


def _timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")
