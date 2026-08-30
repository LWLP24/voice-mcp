from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from calltool.language import normalize_language_code


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


class CallVoiceOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["gemini", "openai"] | None = None
    model: str | None = Field(default=None, min_length=1, max_length=200)
    language: str | None = None
    voice: str | None = Field(default=None, min_length=1, max_length=100)

    @field_validator("language")
    @classmethod
    def validate_language(cls, value: str | None) -> str | None:
        return normalize_language_code(value) if value is not None else None


class CallCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: CallTarget
    objective: str = Field(min_length=1, max_length=4_000)
    constraints: list[str] = Field(default_factory=list, max_length=100)
    context: dict[str, Any] = Field(default_factory=dict)
    permissions: CallPermissions = Field(default_factory=CallPermissions)
    voice: CallVoiceOptions = Field(default_factory=CallVoiceOptions)
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

    @property
    def started_at(self) -> datetime:
        return self.connected_at or self.created_at


class CallListRequest(BaseModel):
    direction: CallDirection | None = None
    status: CallStatus | None = None
    phone_number: str | None = Field(default=None, min_length=1, max_length=64)
    target_name: str | None = Field(default=None, min_length=1, max_length=200)
    started_after: datetime | None = None
    started_before: datetime | None = None
    limit: int = Field(default=50, ge=1, le=100)
    cursor: str | None = Field(default=None, min_length=1, max_length=1_000)

    @field_validator("started_after", "started_before")
    @classmethod
    def require_query_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("call time filters must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_time_range(self) -> CallListRequest:
        if (
            self.started_after is not None
            and self.started_before is not None
            and self.started_after >= self.started_before
        ):
            raise ValueError("started_after must be earlier than started_before")
        return self


class TranscriptTurn(BaseModel):
    call_id: str
    sequence: int = Field(ge=1)
    role: Literal["user", "assistant"]
    text: str = Field(min_length=1)
    interrupted: bool = False
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("created_at")
    @classmethod
    def require_created_timezone(cls, value: datetime) -> datetime:
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class CallListPage(BaseModel):
    calls: list[CallRecord]
    next_cursor: str | None = None


class CallConversation(BaseModel):
    call: CallRecord
    transcript: list[TranscriptTurn]


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
