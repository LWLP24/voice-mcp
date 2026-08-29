from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class CallStatus(StrEnum):
    CREATED = "created"
    VALIDATING = "validating"
    QUEUED = "queued"
    PREWARMING = "prewarming"
    DIALING = "dialing"
    RINGING = "ringing"
    CONNECTED = "connected"
    ACTIVE = "active"
    INPUT_REQUIRED = "input_required"
    COMPLETING = "completing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {self.COMPLETED, self.FAILED, self.CANCELLED}


class CallDirection(StrEnum):
    OUTBOUND = "outbound"
    INBOUND = "inbound"


class CallPhase(StrEnum):
    OPENING = "opening"
    IDENTIFYING = "identifying"
    REQUESTING = "requesting"
    NEGOTIATING = "negotiating"
    CONFIRMING = "confirming"
    CLOSING = "closing"


class CallTarget(BaseModel):
    phone_number: str
    name: str | None = None


class CallPermissions(BaseModel):
    may_commit: bool = False
    may_accept_costs: bool = False
    may_disclose: list[str] = Field(default_factory=list)


class CallCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: CallTarget
    objective: str = Field(min_length=1, max_length=4_000)
    constraints: list[str] = Field(default_factory=list, max_length=100)
    context: dict[str, Any] = Field(default_factory=dict)
    permissions: CallPermissions = Field(default_factory=CallPermissions)
    client_request_id: str | None = Field(default=None, min_length=1, max_length=200)


class Candidate(BaseModel):
    id: str
    kind: str
    value: dict[str, Any]
    allowed: bool
    denial_reason: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class Commitment(BaseModel):
    id: str
    action: str
    payload: dict[str, Any]
    allowed: bool
    reason: str | None = None
    confirmed: bool = False
    created_at: datetime = Field(default_factory=utc_now)


class ActiveCallState(BaseModel):
    objective: str
    constraints: list[str] = Field(default_factory=list)
    permissions: CallPermissions = Field(default_factory=CallPermissions)
    facts: dict[str, Any] = Field(default_factory=dict)
    candidates: list[Candidate] = Field(default_factory=list)
    commitments: list[Commitment] = Field(default_factory=list)
    pending_input_request_id: str | None = None
    room_name: str | None = None
    dispatch_id: str | None = None
    sip_participant_identity: str | None = None
    last_remote_utterance: str | None = None


class CallOutcome(BaseModel):
    success: bool
    reason: str
    summary: str
    facts: dict[str, Any] = Field(default_factory=dict)
    commitments: list[Commitment] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class CallError(BaseModel):
    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class CallRecord(BaseModel):
    id: str
    principal_id: str
    direction: CallDirection = CallDirection.OUTBOUND
    client_request_id: str | None = None
    status: CallStatus
    phase: CallPhase | None = None
    target_number: str
    request: CallCreateRequest
    state: ActiveCallState
    outcome: CallOutcome | None = None
    error: CallError | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    connected_at: datetime | None = None
    ended_at: datetime | None = None

    @field_validator("created_at", "updated_at", "connected_at", "ended_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


class CallEvent(BaseModel):
    call_id: str
    sequence: int
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class InputRequest(BaseModel):
    id: str
    call_id: str
    status: Literal["pending", "resolved", "expired", "cancelled"] = "pending"
    question: str
    options: list[str] = Field(default_factory=list)
    response: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime | None = None
    resolved_at: datetime | None = None


class InputResponse(BaseModel):
    input_request_id: str
    response: dict[str, Any]


class DispatchResult(BaseModel):
    room_name: str
    dispatch_id: str


class CommitDecision(BaseModel):
    allowed: bool
    commit_id: str
    reason: str | None = None
