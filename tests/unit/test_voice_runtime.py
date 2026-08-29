from __future__ import annotations

import asyncio

import pytest
from pydantic import SecretStr, ValidationError

from calltool.calls.models import (
    ActiveCallState,
    CallCreateRequest,
    CallRecord,
    CallStatus,
    CallTarget,
    CallVoiceOptions,
)
from calltool.config import Settings
from calltool.voice.prompts import compile_call_prompt, greeting_instruction_for
from calltool.voice.realtime import build_voice_runtime, resolve_voice_selection


def make_call(voice: CallVoiceOptions | None = None) -> CallRecord:
    request = CallCreateRequest(
        target=CallTarget(phone_number="+49301234567", name="Test"),
        objective="Vereinbare einen Termin",
        voice=voice or CallVoiceOptions(),
    )
    return CallRecord(
        id="call_test",
        principal_id="test",
        status=CallStatus.ACTIVE,
        target_number=request.target.phone_number,
        request=request,
        state=ActiveCallState(objective=request.objective),
    )


def test_default_voice_selection_stays_on_gemini() -> None:
    selection = resolve_voice_selection(Settings(CALLTOOL_ENV="test"))

    assert selection.provider == "gemini"
    assert selection.model == "gemini-3.1-flash-live-preview"
    assert selection.language == "de"
    assert selection.voice == "Puck"


def test_environment_can_select_openai_mini_language_and_voice() -> None:
    settings = Settings(
        CALLTOOL_ENV="test",
        CALLTOOL_VOICE_PROVIDER="openai",
        CALLTOOL_VOICE_MODEL="gpt-realtime-2.1-mini",
        CALLTOOL_VOICE_LANGUAGE="en_us",
        CALLTOOL_VOICE_NAME="cedar",
    )

    selection = resolve_voice_selection(settings)

    assert selection.provider == "openai"
    assert selection.model == "gpt-realtime-2.1-mini"
    assert selection.language == "en-US"
    assert selection.voice == "cedar"


def test_per_call_provider_switch_uses_matching_provider_defaults() -> None:
    settings = Settings(
        CALLTOOL_ENV="test",
        CALLTOOL_VOICE_PROVIDER="gemini",
        CALLTOOL_VOICE_MODEL="gemini-3.1-flash-live-preview",
        CALLTOOL_VOICE_NAME="Puck",
    )

    selection = resolve_voice_selection(settings, CallVoiceOptions(provider="openai"))

    assert selection.provider == "openai"
    assert selection.model == "gpt-realtime-2.1"
    assert selection.voice == "marin"


def test_per_call_voice_options_override_environment() -> None:
    settings = Settings(
        CALLTOOL_ENV="test",
        CALLTOOL_VOICE_PROVIDER="openai",
        CALLTOOL_VOICE_MODEL="gpt-realtime-2.1",
        CALLTOOL_VOICE_LANGUAGE="de",
        CALLTOOL_VOICE_NAME="marin",
    )
    options = CallVoiceOptions(
        provider="openai",
        model="gpt-realtime-2.1-mini",
        language="fr-FR",
        voice="coral",
    )

    selection = resolve_voice_selection(settings, options)

    assert selection.model == "gpt-realtime-2.1-mini"
    assert selection.language == "fr-FR"
    assert selection.voice == "coral"


def test_nonexistent_openai_flash_alias_is_rejected() -> None:
    settings = Settings(
        CALLTOOL_ENV="test",
        CALLTOOL_VOICE_PROVIDER="openai",
        CALLTOOL_VOICE_MODEL="gpt-realtime-2.1-flash",
    )

    with pytest.raises(ValueError, match=r"gpt-realtime-2\.1-mini"):
        resolve_voice_selection(settings)


def test_language_codes_are_validated() -> None:
    with pytest.raises(ValidationError, match="BCP-47"):
        CallVoiceOptions(language="not a language")


@pytest.mark.asyncio
async def test_openai_runtime_uses_native_voice_and_transcription_language() -> None:
    await asyncio.sleep(0)
    settings = Settings(
        CALLTOOL_ENV="test",
        OPENAI_API_KEY=SecretStr("test-openai-key"),
        CALLTOOL_VOICE_PROVIDER="openai",
        CALLTOOL_VOICE_MODEL="gpt-realtime-2.1-mini",
        CALLTOOL_VOICE_LANGUAGE="en",
        CALLTOOL_VOICE_NAME="marin",
    )

    runtime = build_voice_runtime(make_call(), settings, [])
    model_options = runtime.session.llm._opts

    assert runtime.selection.provider == "openai"
    assert runtime.scripted_tts is None
    assert model_options.model == "gpt-realtime-2.1-mini"
    assert model_options.voice == "marin"
    assert model_options.input_audio_transcription.language == "en"
    assert model_options.reasoning.effort == "minimal"


def test_prompt_and_native_greeting_receive_selected_language() -> None:
    call = make_call(CallVoiceOptions(language="en"))

    prompt = compile_call_prompt(call, "en")
    greeting_instruction = greeting_instruction_for(call, "en")

    assert "BCP-47-Code en" in prompt
    assert "BCP-47-Code en" in greeting_instruction
    assert "KI-Assistent" in greeting_instruction
