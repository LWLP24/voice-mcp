from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any

from livekit import rtc
from livekit.agents import llm

from calltool.calls.ids import new_id
from calltool.calls.models import (
    ActiveCallState,
    CallDirection,
    CallOutcome,
    CallStatus,
    Candidate,
    Commitment,
    utc_now,
)
from calltool.calls.service import CallService
from calltool.observability.metrics import TOOL_LATENCY
from calltool.policy.engine import PolicyEngine
from calltool.realtime.active_calls import ActiveCallContext


@dataclass
class ToolRuntime:
    call_id: str
    context: ActiveCallContext
    service: CallService
    policy: PolicyEngine
    room: rtc.Room
    finish_event: asyncio.Event

    async def current_state(self) -> ActiveCallState:
        return self.context.snapshot()


def build_tools(
    runtime: ToolRuntime,
    *,
    direction: CallDirection = CallDirection.OUTBOUND,
) -> list[llm.Tool | llm.Toolset]:
    @llm.function_tool(description="Speichert ein vom Gesprächspartner bestätigtes Faktum.")
    async def record_fact(key: str, value: Any) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            async with runtime.context.state_transaction():
                runtime.context.record_fact(key, value)
                runtime.context.persist_state(
                    event_type="call.fact_recorded",
                    event_payload={"key": key, "value": value},
                    expected_statuses={CallStatus.ACTIVE, CallStatus.INPUT_REQUIRED},
                )
            return {"recorded": True, "key": key}
        finally:
            TOOL_LATENCY.labels(tool="record_fact").observe(time.perf_counter() - started)

    @llm.function_tool(description="Prüft einen unverbindlichen Kandidaten gegen die Regeln.")
    async def propose_candidate(kind: str, value: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            async with runtime.context.state_transaction():
                action = "book_appointment" if kind == "appointment" else kind
                decision = runtime.policy.authorize_commit(
                    runtime.context.snapshot(), action, value
                )
                candidate = Candidate(
                    id=new_id("candidate"),
                    kind=kind,
                    value=value,
                    allowed=decision.allowed,
                    denial_reason=decision.reason,
                )
                runtime.context.add_candidate(candidate)
                runtime.context.persist_state(
                    event_type="call.candidate_detected",
                    event_payload=candidate.model_dump(mode="json"),
                    expected_statuses={CallStatus.ACTIVE},
                )
            return candidate.model_dump(mode="json")
        finally:
            TOOL_LATENCY.labels(tool="propose_candidate").observe(time.perf_counter() - started)

    @llm.function_tool(
        description="Autorisiert jede verbindliche Zusage. Vor einer Bestätigung zwingend aufrufen."
    )
    async def authorize_commit(action: str, payload: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            async with runtime.context.state_transaction():
                canonical = json.dumps(
                    {"action": action, "payload": payload},
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                )
                digest = (
                    hashlib.sha256(f"{runtime.context.call_id}:{canonical}".encode())
                    .hexdigest()[:26]
                    .upper()
                )
                commit_id = f"commit_{digest}"
                existing = runtime.context.find_commitment(commit_id)
                if existing is not None:
                    return {
                        "allowed": existing.allowed,
                        "commit_id": existing.id,
                        "reason": existing.reason,
                        "idempotent_replay": True,
                    }
                decision = runtime.policy.authorize_commit(
                    runtime.context.snapshot(), action, payload, commit_id=commit_id
                )
                commitment = Commitment(
                    id=decision.commit_id,
                    action=action,
                    payload=payload,
                    allowed=decision.allowed,
                    reason=decision.reason,
                    confirmed=decision.allowed,
                )
                event_type = "call.commit_allowed" if decision.allowed else "call.commit_denied"
                await runtime.context.persist_commitment(commitment, event_type)
                return decision.model_dump(mode="json")
        finally:
            TOOL_LATENCY.labels(tool="authorize_commit").observe(time.perf_counter() - started)

    @llm.function_tool(
        description="Fragt den Auftraggeber nach einer fehlenden Entscheidung und wartet auf Antwort."
    )
    async def request_user_input(question: str, options: list[str]) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            async with runtime.context.state_transaction():
                await runtime.context.flush()
                request = await runtime.service.request_input(runtime.call_id, question, options)
                runtime.context.pending_input_request_id = request.id
                runtime.context.status = CallStatus.INPUT_REQUIRED
            while True:
                current = await runtime.service.get_input_request(request.id)
                if current is None:
                    raise RuntimeError("input_request_disappeared")
                if current.status == "resolved":
                    async with runtime.context.state_transaction():
                        runtime.context.pending_input_request_id = None
                        runtime.context.status = CallStatus.ACTIVE
                    return current.response or {}
                if current.expires_at and current.expires_at <= utc_now():
                    await runtime.service.expire_input(runtime.call_id, request.id)
                    async with runtime.context.state_transaction():
                        runtime.context.pending_input_request_id = None
                        runtime.context.status = CallStatus.ACTIVE
                    return {"status": "timeout"}
                await asyncio.sleep(0.25)
        finally:
            TOOL_LATENCY.labels(tool="request_user_input").observe(time.perf_counter() - started)

    @llm.function_tool(description="Sendet DTMF-Ziffern an ein Telefonmenü.")
    async def send_dtmf(digits: str) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            valid = "0123456789*#ABCD"
            if not digits or any(digit not in valid for digit in digits):
                return {"sent": False, "reason": "invalid_dtmf"}
            for digit in digits:
                if digit.isdigit():
                    code = int(digit)
                elif digit == "*":
                    code = 10
                elif digit == "#":
                    code = 11
                else:
                    code = ord(digit) - ord("A") + 12
                await runtime.room.local_participant.publish_dtmf(code=code, digit=digit)
                await asyncio.sleep(0.3)
            return {"sent": True, "digits": digits}
        finally:
            TOOL_LATENCY.labels(tool="send_dtmf").observe(time.perf_counter() - started)

    @llm.function_tool(description="Markiert das Gespräch als abgeschlossen und baut das Ergebnis.")
    async def finish_call(
        success: bool,
        reason: str,
        summary: str,
        notes: list[str] | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            async with runtime.context.state_transaction():
                state = runtime.context.snapshot()
                outcome = CallOutcome(
                    success=success,
                    reason=reason,
                    summary=summary,
                    facts=state.facts,
                    commitments=state.commitments,
                    notes=notes or [],
                )
                await runtime.context.persist_completion(outcome)
                runtime.finish_event.set()
                return {"accepted": True, "hangup_after_goodbye": True}
        finally:
            TOOL_LATENCY.labels(tool="finish_call").observe(time.perf_counter() - started)

    if direction is CallDirection.INBOUND:
        return [record_fact, finish_call]

    return [
        record_fact,
        propose_candidate,
        authorize_commit,
        request_user_input,
        send_dtmf,
        finish_call,
    ]
