from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from livekit import rtc
from livekit.agents import llm

from calltool.calls.errors import PolicyDeniedError
from calltool.calls.ids import new_id
from calltool.calls.models import (
    ActiveCallState,
    CallDirection,
    CallOutcome,
    CallStatus,
    Candidate,
    ColdTransferState,
    Commitment,
    TransferStatus,
    utc_now,
)
from calltool.calls.service import CallService
from calltool.config import IVRConfig
from calltool.observability.metrics import TOOL_LATENCY
from calltool.policy.engine import PolicyEngine
from calltool.realtime.active_calls import ActiveCallContext


@dataclass(frozen=True)
class TransferResult:
    status: TransferStatus
    transfer_id: str | None = None
    reason: str | None = None
    sip_status: str | None = None


TransferHandler = Callable[[str], Awaitable[TransferResult]]


@dataclass
class ToolRuntime:
    call_id: str
    context: ActiveCallContext
    service: CallService
    policy: PolicyEngine
    room: rtc.Room
    finish_event: asyncio.Event
    farewell_required: bool = False
    ivr_config: IVRConfig = field(default_factory=IVRConfig)
    ivr_enabled: bool = False
    cold_transfer_enabled: bool = False
    transfer_handler: TransferHandler | None = None

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
            normalized = digits.upper()
            config = runtime.ivr_config
            if (
                not normalized
                or len(normalized) > config.max_digits_per_action
                or any(digit not in config.allowed_digits for digit in normalized)
            ):
                return {"sent": False, "reason": "invalid_dtmf"}
            for digit in normalized:
                if digit.isdigit():
                    code = int(digit)
                elif digit == "*":
                    code = 10
                elif digit == "#":
                    code = 11
                else:
                    code = ord(digit) - ord("A") + 12
                await runtime.room.local_participant.publish_dtmf(code=code, digit=digit)
                if config.inter_digit_delay_seconds:
                    await asyncio.sleep(config.inter_digit_delay_seconds)
            payload: dict[str, object] = {"digit_count": len(normalized)}
            if config.audit_digits:
                payload["digits"] = normalized
            runtime.context.persist_event("call.dtmf_sent", payload)
            return {"sent": True, "digit_count": len(normalized)}
        finally:
            TOOL_LATENCY.labels(tool="send_dtmf").observe(time.perf_counter() - started)

    @llm.function_tool(
        description=(
            "Übergibt den aktuellen Anruf per Cold Transfer an eine erlaubte Telefonnummer."
        )
    )
    async def cold_transfer(target_number: str) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            if not runtime.cold_transfer_enabled or runtime.transfer_handler is None:
                return {"transferred": False, "reason": "cold_transfer_disabled"}
            if not runtime.context.permissions.may_transfer:
                return {"transferred": False, "reason": "transfer_permission_missing"}
            try:
                normalized = runtime.policy.validate_target(target_number)
            except PolicyDeniedError as exc:
                return {"transferred": False, "reason": exc.reason}

            transfer = ColdTransferState(
                target_number=normalized,
                status=TransferStatus.REQUESTED,
            )
            async with runtime.context.state_transaction():
                runtime.context.record_transfer(transfer)
                await runtime.context.persist_state_durable(
                    "call.transfer_requested",
                    {"target_number": normalized},
                    expected_statuses={CallStatus.ACTIVE, CallStatus.INPUT_REQUIRED},
                )

            try:
                result = await runtime.transfer_handler(normalized)
            except Exception as exc:
                result = TransferResult(
                    status=TransferStatus.FAILED,
                    reason=f"transfer_api_error:{type(exc).__name__}",
                )
            transfer.status = result.status
            transfer.transfer_id = result.transfer_id
            transfer.reason = result.reason
            transfer.sip_status = result.sip_status
            transfer.updated_at = utc_now()
            async with runtime.context.state_transaction():
                runtime.context.record_transfer(transfer)
                event_type = {
                    TransferStatus.SUCCESSFUL: "call.transfer_succeeded",
                    TransferStatus.ONGOING: "call.transfer_ongoing",
                    TransferStatus.FAILED: "call.transfer_failed",
                    TransferStatus.REQUESTED: "call.transfer_requested",
                }[result.status]
                await runtime.context.persist_state_durable(
                    event_type,
                    transfer.model_dump(mode="json"),
                    expected_statuses={CallStatus.ACTIVE, CallStatus.INPUT_REQUIRED},
                )
            if result.status is TransferStatus.SUCCESSFUL:
                state = runtime.context.snapshot()
                await runtime.context.persist_completion(
                    CallOutcome(
                        success=True,
                        reason="cold_transfer_successful",
                        summary=f"Gespräch wurde an {normalized} übergeben.",
                        facts=state.facts,
                        commitments=state.commitments,
                    )
                )
                runtime.finish_event.set()
            return {
                "transferred": result.status is TransferStatus.SUCCESSFUL,
                **transfer.model_dump(mode="json"),
            }
        finally:
            TOOL_LATENCY.labels(tool="cold_transfer").observe(time.perf_counter() - started)

    @llm.function_tool(
        description=(
            "Markiert das Gespräch als abgeschlossen und baut das Ergebnis. "
            "Nach diesem Tool spielt CallTool die konfigurierte Verabschiedung genau einmal "
            "ab und legt danach auf; sprich keine eigene Verabschiedung vor dem Tool-Aufruf."
        )
    )
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
                runtime.farewell_required = True
                runtime.finish_event.set()
                return {"accepted": True, "farewell_scheduled": True, "hangup_after_goodbye": True}
        finally:
            TOOL_LATENCY.labels(tool="finish_call").observe(time.perf_counter() - started)

    if direction is CallDirection.INBOUND:
        return [record_fact, finish_call]

    outbound_tools: list[llm.Tool | llm.Toolset] = [
        record_fact,
        propose_candidate,
        authorize_commit,
        request_user_input,
    ]
    if runtime.ivr_enabled:
        outbound_tools.append(send_dtmf)
    if runtime.cold_transfer_enabled:
        outbound_tools.append(cold_transfer)
    outbound_tools.append(finish_call)
    return outbound_tools
