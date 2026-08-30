from datetime import datetime

import pytest

from calltool.calls.errors import PolicyDeniedError
from calltool.calls.models import ActiveCallState, CallPermissions
from calltool.config import PolicyConfig
from calltool.policy.engine import PolicyEngine


def test_normalizes_allowed_german_number() -> None:
    policy = PolicyEngine(PolicyConfig())

    assert policy.validate_target("+49 30 1234567") == "+49301234567"


def test_rejects_invalid_number() -> None:
    policy = PolicyEngine(PolicyConfig())

    with pytest.raises(PolicyDeniedError, match="invalid_phone_number"):
        policy.validate_target("not-a-number")


def test_denies_commit_without_permission() -> None:
    policy = PolicyEngine(PolicyConfig())
    state = ActiveCallState(objective="test", permissions=CallPermissions(may_commit=False))

    decision = policy.authorize_commit(
        state, "book_appointment", {"datetime": "2026-09-03T16:30:00+02:00"}
    )

    assert decision.allowed is False
    assert decision.reason == "commit_permission_missing"


def test_enforces_earliest_time_and_excluded_weekday() -> None:
    policy = PolicyEngine(PolicyConfig())
    state = ActiveCallState(
        objective="test",
        constraints=["frühestens 15 Uhr", "nicht Dienstag"],
        permissions=CallPermissions(may_commit=True),
    )

    before = policy.authorize_commit(
        state, "book_appointment", {"datetime": "2026-09-03T14:30:00+02:00"}
    )
    tuesday = datetime(2026, 9, 1, 16, 30).astimezone().isoformat()
    excluded = policy.authorize_commit(state, "book_appointment", {"datetime": tuesday})
    allowed = policy.authorize_commit(
        state, "book_appointment", {"datetime": "2026-09-03T16:30:00+02:00"}
    )

    assert before.reason == "before_allowed_time"
    assert excluded.reason == "weekday_not_allowed"
    assert allowed.allowed is True
