from __future__ import annotations

import re
from datetime import datetime, time
from typing import Any

import phonenumbers
from phonenumbers import PhoneNumberType

from calltool.calls.errors import PolicyDeniedError
from calltool.calls.ids import new_id
from calltool.calls.models import ActiveCallState, CommitDecision
from calltool.config import PolicyConfig

_WEEKDAYS = {
    "montag": 0,
    "dienstag": 1,
    "mittwoch": 2,
    "donnerstag": 3,
    "freitag": 4,
    "samstag": 5,
    "sonntag": 6,
}


class PolicyEngine:
    def __init__(self, config: PolicyConfig) -> None:
        self._config = config

    def validate_target(self, raw_number: str) -> str:
        try:
            number = phonenumbers.parse(raw_number, None)
        except phonenumbers.NumberParseException as exc:
            raise PolicyDeniedError("invalid_phone_number") from exc
        if not phonenumbers.is_valid_number(number):
            raise PolicyDeniedError("invalid_phone_number")
        region = phonenumbers.region_code_for_number(number)
        if self._config.allowed_country_codes and region not in self._config.allowed_country_codes:
            raise PolicyDeniedError("country_not_allowed")
        number_type = phonenumbers.number_type(number)
        if self._config.block_premium_numbers and number_type in {
            PhoneNumberType.PREMIUM_RATE,
            PhoneNumberType.SHARED_COST,
        }:
            raise PolicyDeniedError("premium_number_blocked")
        normalized = phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)
        if (
            self._config.block_emergency_numbers
            and region is not None
            and phonenumbers.is_emergency_number(
                phonenumbers.national_significant_number(number), region
            )
        ):
            raise PolicyDeniedError("emergency_number_blocked")
        return normalized

    def authorize_commit(
        self,
        state: ActiveCallState,
        action: str,
        payload: dict[str, Any],
        *,
        commit_id: str | None = None,
    ) -> CommitDecision:
        commit_id = commit_id or new_id("commit")
        if not state.permissions.may_commit:
            return CommitDecision(
                allowed=False,
                commit_id=commit_id,
                reason="commit_permission_missing",
            )
        if self._contains_cost(payload) and not state.permissions.may_accept_costs:
            return CommitDecision(
                allowed=False,
                commit_id=commit_id,
                reason="cost_not_authorized",
            )
        if action in {"book_appointment", "accept_appointment"}:
            reason = self._check_appointment_constraints(state.constraints, payload)
            if reason:
                return CommitDecision(allowed=False, commit_id=commit_id, reason=reason)
        return CommitDecision(allowed=True, commit_id=commit_id)

    @staticmethod
    def _contains_cost(payload: dict[str, Any]) -> bool:
        return any(key in payload for key in {"cost", "price", "amount", "fee"})

    def _check_appointment_constraints(
        self, constraints: list[str], payload: dict[str, Any]
    ) -> str | None:
        raw_datetime = payload.get("datetime") or payload.get("appointment_at")
        if not isinstance(raw_datetime, str):
            return "appointment_datetime_missing"
        try:
            appointment = datetime.fromisoformat(raw_datetime)
        except ValueError:
            return "appointment_datetime_invalid"

        for constraint in constraints:
            normalized = constraint.casefold().strip()
            earliest = re.search(r"frühestens\s+(\d{1,2})(?::(\d{2}))?\s*(?:uhr)?", normalized)
            if earliest:
                minimum = time(int(earliest.group(1)), int(earliest.group(2) or 0))
                if appointment.time() < minimum:
                    return "before_allowed_time"
            latest = re.search(r"spätestens\s+(\d{1,2})(?::(\d{2}))?\s*(?:uhr)?", normalized)
            if latest:
                maximum = time(int(latest.group(1)), int(latest.group(2) or 0))
                if appointment.time() > maximum:
                    return "after_allowed_time"
            for weekday, weekday_index in _WEEKDAYS.items():
                if f"nicht {weekday}" in normalized and appointment.weekday() == weekday_index:
                    return "weekday_not_allowed"
        return None
