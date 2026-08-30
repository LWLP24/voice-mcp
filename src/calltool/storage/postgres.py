from __future__ import annotations

import json
from collections.abc import Collection
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import asyncpg

from calltool.calls.errors import (
    CallNotFoundError,
    InputRequestNotFoundError,
    InvalidStateTransitionError,
)
from calltool.calls.models import (
    ActiveCallState,
    CallDirection,
    CallError,
    CallEvent,
    CallOutcome,
    CallPhase,
    CallRecord,
    CallStatus,
    InputRequest,
    TranscriptTurn,
    utc_now,
)
from calltool.realtime.events import EventPublisher

_IN_PROGRESS = tuple(status.value for status in CallStatus if not status.terminal)


def _load_migrations() -> list[str]:
    migration_dir = Path(__file__).resolve().parents[3] / "migrations"
    return [
        migration.read_text(encoding="utf-8") for migration in sorted(migration_dir.glob("*.sql"))
    ]


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=str)


def _decoded(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


class PostgresCallRepository:
    def __init__(self, pool: asyncpg.Pool, event_publisher: EventPublisher | None = None) -> None:
        self._pool = pool
        self._event_publisher = event_publisher

    @classmethod
    async def connect(
        cls,
        database_url: str,
        *,
        event_publisher: EventPublisher | None = None,
    ) -> PostgresCallRepository:
        pool = await asyncpg.create_pool(database_url, min_size=1, max_size=5, command_timeout=10)
        if pool is None:
            raise RuntimeError("asyncpg did not create a pool")
        repository = cls(pool, event_publisher)
        await repository.migrate()
        return repository

    async def migrate(self) -> None:
        for migration_sql in _load_migrations():
            await self._pool.execute(migration_sql)

    async def create_call(self, call: CallRecord) -> CallRecord:
        async with self._pool.acquire() as connection, connection.transaction():
            if call.client_request_id:
                row = await connection.fetchrow(
                    """
                    SELECT * FROM calls
                    WHERE principal_id = $1 AND client_request_id = $2
                    """,
                    call.principal_id,
                    call.client_request_id,
                )
                if row is not None:
                    return self._row_to_call(row)
            await connection.execute(
                """
                INSERT INTO calls (
                  id, principal_id, direction, client_request_id, status, phase, target_number,
                  request, state, outcome, error, created_at, updated_at, connected_at, ended_at
                ) VALUES (
                  $1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9::jsonb, $10::jsonb,
                  $11::jsonb, $12, $13, $14, $15
                )
                """,
                call.id,
                call.principal_id,
                call.direction.value,
                call.client_request_id,
                call.status.value,
                call.phase.value if call.phase else None,
                call.target_number,
                _json(call.request.model_dump(mode="json")),
                _json(call.state.model_dump(mode="json")),
                _json(call.outcome.model_dump(mode="json")) if call.outcome else None,
                _json(call.error.model_dump(mode="json")) if call.error else None,
                call.created_at,
                call.updated_at,
                call.connected_at,
                call.ended_at,
            )
            await connection.execute(
                """
                INSERT INTO call_events (call_id, sequence, type, payload, created_at)
                VALUES ($1, 1, 'call.created', '{}'::jsonb, $2)
                """,
                call.id,
                call.created_at,
            )
        if self._event_publisher is not None:
            await self._event_publisher.publish(
                CallEvent(
                    call_id=call.id,
                    sequence=1,
                    type="call.created",
                    payload={},
                    created_at=call.created_at,
                )
            )
        return call

    async def get_call(self, call_id: str) -> CallRecord | None:
        row = await self._pool.fetchrow("SELECT * FROM calls WHERE id = $1", call_id)
        return self._row_to_call(row) if row else None

    async def get_by_idempotency(
        self, principal_id: str, client_request_id: str
    ) -> CallRecord | None:
        row = await self._pool.fetchrow(
            """
            SELECT * FROM calls
            WHERE principal_id = $1 AND client_request_id = $2
            """,
            principal_id,
            client_request_id,
        )
        return self._row_to_call(row) if row else None

    async def update_call(
        self,
        call_id: str,
        *,
        status: CallStatus | None = None,
        phase: CallPhase | None = None,
        state: ActiveCallState | None = None,
        outcome: CallOutcome | None = None,
        error: CallError | None = None,
        event_type: str,
        event_payload: dict[str, object] | None = None,
        expected_statuses: Collection[CallStatus] | None = None,
    ) -> CallRecord:
        async with self._pool.acquire() as connection, connection.transaction():
            current = await connection.fetchrow(
                "SELECT * FROM calls WHERE id = $1 FOR UPDATE", call_id
            )
            if current is None:
                raise CallNotFoundError(call_id)
            current_status = CallStatus(current["status"])
            if expected_statuses is not None and current_status not in expected_statuses:
                raise InvalidStateTransitionError(
                    f"Expected one of {sorted(expected_statuses)}, got {current_status}"
                )

            now = utc_now()
            next_status = status or current_status
            next_phase = phase.value if phase else current["phase"]
            connected_at = current["connected_at"]
            ended_at = current["ended_at"]
            if next_status is CallStatus.CONNECTED and connected_at is None:
                connected_at = now
            if next_status.terminal:
                ended_at = now
                next_phase = None

            row = await connection.fetchrow(
                """
                UPDATE calls SET
                  status = $2,
                  phase = $3,
                  state = COALESCE($4::jsonb, state),
                  outcome = COALESCE($5::jsonb, outcome),
                  error = COALESCE($6::jsonb, error),
                  updated_at = $7,
                  connected_at = $8,
                  ended_at = $9
                WHERE id = $1
                RETURNING *
                """,
                call_id,
                next_status.value,
                next_phase,
                _json(state.model_dump(mode="json")) if state else None,
                _json(outcome.model_dump(mode="json")) if outcome else None,
                _json(error.model_dump(mode="json")) if error else None,
                now,
                connected_at,
                ended_at,
            )
            sequence = await connection.fetchval(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM call_events WHERE call_id = $1",
                call_id,
            )
            await connection.execute(
                """
                INSERT INTO call_events (call_id, sequence, type, payload, created_at)
                VALUES ($1, $2, $3, $4::jsonb, $5)
                """,
                call_id,
                sequence,
                event_type,
                _json(event_payload or {}),
                now,
            )
        if row is None:
            raise CallNotFoundError(call_id)
        if self._event_publisher is not None:
            await self._event_publisher.publish(
                CallEvent(
                    call_id=call_id,
                    sequence=sequence,
                    type=event_type,
                    payload=event_payload or {},
                    created_at=now,
                )
            )
        return self._row_to_call(row)

    async def list_events(self, call_id: str, *, after_sequence: int = 0) -> list[CallEvent]:
        rows = await self._pool.fetch(
            """
            SELECT call_id, sequence, type, payload, created_at
            FROM call_events
            WHERE call_id = $1 AND sequence > $2
            ORDER BY sequence
            """,
            call_id,
            after_sequence,
        )
        return [
            CallEvent(
                call_id=row["call_id"],
                sequence=row["sequence"],
                type=row["type"],
                payload=_decoded(row["payload"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    async def list_calls(
        self,
        *,
        principal_id: str,
        direction: CallDirection | None,
        status: CallStatus | None,
        phone_number: str | None,
        target_name: str | None,
        started_after: datetime | None,
        started_before: datetime | None,
        cursor_started_at: datetime | None,
        cursor_call_id: str | None,
        limit: int,
    ) -> list[CallRecord]:
        arguments: list[object] = [principal_id]
        conditions = ["principal_id = $1"]

        def parameter(value: object) -> str:
            arguments.append(value)
            return f"${len(arguments)}"

        if direction is not None:
            conditions.append(f"direction = {parameter(direction.value)}")
        if status is not None:
            conditions.append(f"status = {parameter(status.value)}")
        if phone_number is not None:
            conditions.append(f"target_number = {parameter(phone_number)}")
        if target_name is not None:
            conditions.append(
                f"request->'target'->>'name' ILIKE '%' || {parameter(target_name)} || '%'"
            )
        if started_after is not None:
            conditions.append(f"COALESCE(connected_at, created_at) >= {parameter(started_after)}")
        if started_before is not None:
            conditions.append(f"COALESCE(connected_at, created_at) < {parameter(started_before)}")
        if cursor_started_at is not None and cursor_call_id is not None:
            started_parameter = parameter(cursor_started_at)
            id_parameter = parameter(cursor_call_id)
            conditions.append(
                f"(COALESCE(connected_at, created_at), id) < ({started_parameter}, {id_parameter})"
            )
        limit_parameter = parameter(limit)
        rows = await self._pool.fetch(
            f"""
            SELECT * FROM calls
            WHERE {" AND ".join(conditions)}
            ORDER BY COALESCE(connected_at, created_at) DESC, id DESC
            LIMIT {limit_parameter}
            """,
            *arguments,
        )
        return [self._row_to_call(row) for row in rows]

    async def append_transcript_turn(
        self,
        call_id: str,
        *,
        role: Literal["user", "assistant"],
        text: str,
        interrupted: bool = False,
        state: ActiveCallState | None = None,
    ) -> TranscriptTurn:
        now = utc_now()
        transcript = text.strip()
        if not transcript:
            raise ValueError("transcript text must not be empty")
        async with self._pool.acquire() as connection, connection.transaction():
            current = await connection.fetchrow(
                "SELECT id FROM calls WHERE id = $1 FOR UPDATE", call_id
            )
            if current is None:
                raise CallNotFoundError(call_id)
            sequence = int(
                await connection.fetchval(
                    """
                    SELECT COALESCE(MAX(sequence), 0) + 1
                    FROM call_transcript_turns
                    WHERE call_id = $1
                    """,
                    call_id,
                )
            )
            await connection.execute(
                """
                INSERT INTO call_transcript_turns (
                  call_id, sequence, role, text, interrupted, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6)
                """,
                call_id,
                sequence,
                role,
                transcript,
                interrupted,
                now,
            )
            if state is not None:
                await connection.execute(
                    "UPDATE calls SET state = $2::jsonb, updated_at = $3 WHERE id = $1",
                    call_id,
                    _json(state.model_dump(mode="json")),
                    now,
                )
            event_sequence = int(
                await connection.fetchval(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM call_events WHERE call_id = $1",
                    call_id,
                )
            )
            event_type = f"call.{role}_transcript_final"
            event_payload: dict[str, object] = {
                "transcript": transcript,
                "interrupted": interrupted,
            }
            await connection.execute(
                """
                INSERT INTO call_events (call_id, sequence, type, payload, created_at)
                VALUES ($1, $2, $3, $4::jsonb, $5)
                """,
                call_id,
                event_sequence,
                event_type,
                _json(event_payload),
                now,
            )
        turn = TranscriptTurn(
            call_id=call_id,
            sequence=sequence,
            role=role,
            text=transcript,
            interrupted=interrupted,
            created_at=now,
        )
        if self._event_publisher is not None:
            await self._event_publisher.publish(
                CallEvent(
                    call_id=call_id,
                    sequence=event_sequence,
                    type=event_type,
                    payload=event_payload,
                    created_at=now,
                )
            )
        return turn

    async def list_transcript(self, call_id: str) -> list[TranscriptTurn]:
        rows = await self._pool.fetch(
            """
            SELECT call_id, sequence, role, text, interrupted, created_at
            FROM call_transcript_turns
            WHERE call_id = $1
            ORDER BY sequence
            """,
            call_id,
        )
        return [TranscriptTurn.model_validate(dict(row)) for row in rows]

    async def count_in_progress(self, principal_id: str | None = None) -> int:
        if principal_id is None:
            return int(
                await self._pool.fetchval(
                    "SELECT COUNT(*) FROM calls WHERE status = ANY($1::text[])",
                    list(_IN_PROGRESS),
                )
            )
        return int(
            await self._pool.fetchval(
                """
                SELECT COUNT(*) FROM calls
                WHERE principal_id = $1 AND status = ANY($2::text[])
                """,
                principal_id,
                list(_IN_PROGRESS),
            )
        )

    async def count_dispatched(self) -> int:
        return int(
            await self._pool.fetchval(
                """
                SELECT COUNT(*) FROM calls
                WHERE status = ANY($1::text[])
                  AND (
                    status NOT IN ('created', 'validating', 'queued')
                    OR state->>'dispatch_id' IS NOT NULL
                  )
                """,
                list(_IN_PROGRESS),
            )
        )

    async def list_queued(self, *, limit: int) -> list[CallRecord]:
        rows = await self._pool.fetch(
            """
            SELECT * FROM calls
            WHERE status = 'queued' AND state->>'dispatch_id' IS NULL
            ORDER BY created_at
            LIMIT $1
            """,
            limit,
        )
        return [self._row_to_call(row) for row in rows]

    async def create_input_request(self, request: InputRequest) -> InputRequest:
        await self._pool.execute(
            """
            INSERT INTO input_requests (
              id, call_id, status, request, response, created_at, expires_at, resolved_at
            ) VALUES ($1, $2, $3, $4::jsonb, NULL, $5, $6, NULL)
            """,
            request.id,
            request.call_id,
            request.status,
            _json({"question": request.question, "options": request.options}),
            request.created_at,
            request.expires_at,
        )
        return request

    async def get_input_request(self, request_id: str) -> InputRequest | None:
        row = await self._pool.fetchrow("SELECT * FROM input_requests WHERE id = $1", request_id)
        return self._row_to_input_request(row) if row else None

    async def resolve_input_request(
        self, call_id: str, request_id: str, response: dict[str, object]
    ) -> InputRequest:
        row = await self._pool.fetchrow(
            """
            UPDATE input_requests SET
              status = 'resolved', response = $3::jsonb, resolved_at = $4
            WHERE id = $1 AND call_id = $2 AND status = 'pending'
            RETURNING *
            """,
            request_id,
            call_id,
            _json(response),
            utc_now(),
        )
        if row is None:
            existing = await self.get_input_request(request_id)
            if existing is None or existing.call_id != call_id:
                raise InputRequestNotFoundError(request_id)
            return existing
        return self._row_to_input_request(row)

    async def expire_input_request(self, call_id: str, request_id: str) -> InputRequest:
        row = await self._pool.fetchrow(
            """
            UPDATE input_requests SET status = 'expired', resolved_at = $3
            WHERE id = $1 AND call_id = $2 AND status = 'pending'
            RETURNING *
            """,
            request_id,
            call_id,
            utc_now(),
        )
        if row is None:
            existing = await self.get_input_request(request_id)
            if existing is None or existing.call_id != call_id:
                raise InputRequestNotFoundError(request_id)
            return existing
        return self._row_to_input_request(row)

    async def close(self) -> None:
        await self._pool.close()
        if self._event_publisher is not None:
            await self._event_publisher.close()

    @staticmethod
    def _row_to_call(row: asyncpg.Record) -> CallRecord:
        return CallRecord.model_validate(
            {
                "id": row["id"],
                "principal_id": row["principal_id"],
                "direction": row["direction"],
                "client_request_id": row["client_request_id"],
                "status": row["status"],
                "phase": row["phase"],
                "target_number": row["target_number"],
                "request": _decoded(row["request"]),
                "state": _decoded(row["state"]),
                "outcome": _decoded(row["outcome"]) if row["outcome"] else None,
                "error": _decoded(row["error"]) if row["error"] else None,
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "connected_at": row["connected_at"],
                "ended_at": row["ended_at"],
            }
        )

    @staticmethod
    def _row_to_input_request(row: asyncpg.Record) -> InputRequest:
        body = _decoded(row["request"])
        return InputRequest(
            id=row["id"],
            call_id=row["call_id"],
            status=row["status"],
            question=body["question"],
            options=body.get("options", []),
            response=_decoded(row["response"]) if row["response"] else None,
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            resolved_at=row["resolved_at"],
        )
