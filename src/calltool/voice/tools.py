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
    CallOutcome,
    CallStatus,
    Candidate,
    Commitment,
    utc_now,
)
from calltool.calls.repository import CallRepository
from calltool.calls.service import CallService
from calltool.observability.metrics import TOOL_LATENCY
from calltool.policy.engine import PolicyEngine


@dataclass
class ToolRuntime:
    call_id: str
    repository: CallRepository
    service: CallService
    policy: PolicyEngine
    room: rtc.Room
    finish_event: asyncio.Event

    async def current_state(self) -> ActiveCallState:
        call = await self.repository.get_call(self.call_id)
        if call is None:
            raise RuntimeError("Call disappeared from durable storage")
        return call.state


def build_tools(runtime: ToolRuntime) -> list[llm.Tool | llm.Toolset]:
    @llm.function_tool(description="Speichert ein vom Gesprächspartner bestätigtes Faktum.")
    async def record_fact(key: str, value: Any) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            call = await runtime.repository.get_call(runtime.call_id)
            if call is None:
                raise RuntimeError("call_not_found")
            facts = dict(call.state.facts)
            facts[key] = value
            state = call.state.model_copy(update={"facts": facts})
            await runtime.repository.update_call(
                call.id,
                state=state,
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
            call = await runtime.repository.get_call(runtime.call_id)
            if call is None:
                raise RuntimeError("call_not_found")
            action = "book_appointment" if kind == "appointment" else kind
            decision = runtime.policy.authorize_commit(call.state, action, value)
            candidate = Candidate(
                id=new_id("candidate"),
                kind=kind,
                value=value,
                allowed=decision.allowed,
                denial_reason=decision.reason,
            )
            state = call.state.model_copy(
                update={"candidates": [*call.state.candidates, candidate]}
            )
            await runtime.repository.update_call(
                call.id,
                state=state,
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
            call = await runtime.repository.get_call(runtime.call_id)
            if call is None:
                raise RuntimeError("call_not_found")
            canonical = json.dumps(
                {"action": action, "payload": payload},
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            digest = hashlib.sha256(
                f"{call.id}:{canonical}".encode()
            ).hexdigest()[:26].upper()
            commit_id = f"commit_{digest}"
            existing = next(
                (item for item in call.state.commitments if item.id == commit_id), None
            )
            if existing is not None:
                return {
                    "allowed": existing.allowed,
                    "commit_id": existing.id,
                    "reason": existing.reason,
                    "idempotent_replay": True,
                }
            decision = runtime.policy.authorize_commit(
                call.state, action, payload, commit_id=commit_id
            )
            commitment = Commitment(
                id=decision.commit_id,
                action=action,
                payload=payload,
                allowed=decision.allowed,
                reason=decision.reason,
                confirmed=decision.allowed,
            )
            state = call.state.model_copy(
                update={"commitments": [*call.state.commitments, commitment]}
            )
            event_type = "call.commit_allowed" if decision.allowed else "call.commit_denied"
            await runtime.repository.update_call(
                call.id,
                state=state,
                event_type=event_type,
                event_payload=commitment.model_dump(mode="json"),
                expected_statuses={CallStatus.ACTIVE},
            )
            return decision.model_dump(mode="json")
        finally:
            TOOL_LATENCY.labels(tool="authorize_commit").observe(
                time.perf_counter() - started
            )

    @llm.function_tool(
        description="Fragt den Auftraggeber nach einer fehlenden Entscheidung und wartet auf Antwort."
    )
    async def request_user_input(question: str, options: list[str]) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            request = await runtime.service.request_input(runtime.call_id, question, options)
            while True:
                current = await runtime.repository.get_input_request(request.id)
                if current is None:
                    raise RuntimeError("input_request_disappeared")
                if current.status == "resolved":
                    return current.response or {}
                if current.expires_at and current.expires_at <= utc_now():
                    await runtime.service.expire_input(runtime.call_id, request.id)
                    return {"status": "timeout"}
                await asyncio.sleep(0.25)
        finally:
            TOOL_LATENCY.labels(tool="request_user_input").observe(
                time.perf_counter() - started
            )

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
            call = await runtime.repository.get_call(runtime.call_id)
            if call is None:
                raise RuntimeError("call_not_found")
            outcome = CallOutcome(
                success=success,
                reason=reason,
                summary=summary,
                facts=call.state.facts,
                commitments=call.state.commitments,
                notes=notes or [],
            )
            await runtime.repository.update_call(
                call.id,
                status=CallStatus.COMPLETING,
                outcome=outcome,
                event_type="call.completing",
                event_payload={"success": success, "reason": reason},
                expected_statuses={CallStatus.ACTIVE, CallStatus.INPUT_REQUIRED},
            )
            runtime.finish_event.set()
            return {"accepted": True, "hangup_after_goodbye": True}
        finally:
            TOOL_LATENCY.labels(tool="finish_call").observe(time.perf_counter() - started)

    return [
        record_fact,
        propose_candidate,
        authorize_commit,
        request_user_input,
        send_dtmf,
        finish_call,
    ]
