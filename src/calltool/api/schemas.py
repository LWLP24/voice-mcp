from __future__ import annotations

from typing import Any

from calltool.calls.models import CallRecord


def call_response(call: CallRecord) -> dict[str, Any]:
    payload: dict[str, Any] = call.model_dump(mode="json", exclude_none=True)
    payload["call_id"] = payload.pop("id")
    return payload
