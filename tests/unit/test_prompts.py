from __future__ import annotations

from pathlib import Path

import pytest

from calltool.calls.models import (
    ActiveCallState,
    CallCreateRequest,
    CallOutcome,
    CallRecord,
    CallStatus,
    CallTarget,
)
from calltool.config import Settings
from calltool.voice.prompts import PromptProfile, PromptTemplateError


def _write_profile(directory: Path) -> None:
    templates = {
        "system-inbound.md": "INBOUND {{ organization_name }} {{ language }}",
        "system-outbound.md": "OUTBOUND {{ objective }} {{ language }}",
        "greeting-inbound.txt": "Hallo bei {{ organization_name }}.",
        "greeting-outbound.txt": "Hallo wegen {{ objective }}.",
        "farewell-inbound.txt": "Auf Wiederhören bei {{ organization_name }}.",
        "farewell-outbound.txt": "Auf Wiederhören wegen {{ objective }}.",
        "greeting-instruction.md": "Sprich {{ greeting_json }} auf {{ language }}.",
        "voicemail-instruction.md": "Mailbox für {{ objective }} auf {{ language }}.",
        "ivr-instruction.md": "IVR für {{ objective }} auf {{ language }}.",
        "watchdog-instruction.md": "Antworte jetzt auf {{ language }}.",
        "watchdog-fallback.txt": "Die Verbindung hakt.",
        "supervisor.md": "Fasse {{ outcome_json }} auf {{ language }} zusammen.",
    }
    for filename, content in templates.items():
        (directory / filename).write_text(content, encoding="utf-8")


def _call() -> CallRecord:
    request = CallCreateRequest(
        target=CallTarget(phone_number="+49301234567", name="Praxis"),
        objective="eines Termins",
    )
    return CallRecord(
        id="call_prompt_test",
        principal_id="test",
        status=CallStatus.ACTIVE,
        target_number=request.target.phone_number,
        request=request,
        state=ActiveCallState(objective=request.objective),
    )


def test_environment_selects_external_prompt_profile(tmp_path: Path) -> None:
    _write_profile(tmp_path)
    settings = Settings(CALLTOOL_ENV="test", CALLTOOL_PROMPT_DIR=str(tmp_path))

    profile = PromptProfile.load(settings)

    assert profile.system_prompt(_call(), "en-US") == "OUTBOUND eines Termins en-US"
    assert profile.greeting(_call(), "en-US") == "Hallo wegen eines Termins."
    assert profile.farewell(_call(), "en-US") == "Auf Wiederhören wegen eines Termins."
    assert profile.greeting_instruction(_call(), "en-US") == (
        'Sprich "Hallo wegen eines Termins." auf en-US.'
    )
    assert profile.watchdog_instruction(_call(), "en-US") == "Antworte jetzt auf en-US."
    supervisor = profile.supervisor_prompt(
        _call(),
        CallOutcome(success=True, reason="done", summary="Erledigt"),
        "en-US",
    )
    assert '"summary": "Erledigt"' in supervisor


def test_prompt_profile_is_reloaded_for_the_next_call(tmp_path: Path) -> None:
    _write_profile(tmp_path)
    settings = Settings(CALLTOOL_ENV="test", CALLTOOL_PROMPT_DIR=str(tmp_path))
    first = PromptProfile.load(settings)
    (tmp_path / "greeting-outbound.txt").write_text(
        "Neue Begrüßung für {{ objective }}.", encoding="utf-8"
    )

    second = PromptProfile.load(settings)

    assert first.greeting(_call(), "de") == "Hallo wegen eines Termins."
    assert second.greeting(_call(), "de") == "Neue Begrüßung für eines Termins."


def test_unknown_prompt_placeholder_is_rejected(tmp_path: Path) -> None:
    _write_profile(tmp_path)
    (tmp_path / "system-outbound.md").write_text(
        "Nicht erlaubt: {{ arbitrary_code }}", encoding="utf-8"
    )
    settings = Settings(CALLTOOL_ENV="test", CALLTOOL_PROMPT_DIR=str(tmp_path))

    with pytest.raises(PromptTemplateError, match="unknown placeholder"):
        PromptProfile.load(settings)


def test_missing_prompt_file_is_rejected(tmp_path: Path) -> None:
    _write_profile(tmp_path)
    (tmp_path / "greeting-inbound.txt").unlink()
    settings = Settings(CALLTOOL_ENV="test", CALLTOOL_PROMPT_DIR=str(tmp_path))

    with pytest.raises(PromptTemplateError, match="not readable"):
        PromptProfile.load(settings)
