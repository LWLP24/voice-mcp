from __future__ import annotations

import asyncio
from pathlib import Path

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
from calltool.voice.prompts import PromptProfile
from calltool.voice.realtime import (
    build_amd_detector,
    build_voice_runtime,
    resolve_voice_selection,
)

DEFAULT_PROMPT_DIR = Path(__file__).parents[2] / "config" / "prompts" / "default"


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
    selection = resolve_voice_selection(
        Settings(
            CALLTOOL_ENV="test",
            CALLTOOL_VOICE_PROVIDER="gemini",
            CALLTOOL_VOICE_MODEL="gemini-3.1-flash-live-preview",
            CALLTOOL_VOICE_LANGUAGE="de",
            CALLTOOL_VOICE_NAME="Puck",
        )
    )

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
        CALLTOOL_PROMPT_DIR=str(DEFAULT_PROMPT_DIR),
    )

    runtime = build_voice_runtime(make_call(), settings, [])
    model_options = runtime.session.llm._opts

    assert runtime.selection.provider == "openai"
    assert runtime.scripted_tts is None
    assert model_options.model == "gpt-realtime-2.1-mini"
    assert model_options.voice == "marin"
    assert model_options.input_audio_transcription.language == "en"
    assert model_options.reasoning.effort == "minimal"
    assert model_options.turn_detection.type == "server_vad"
    assert model_options.turn_detection.silence_duration_ms == 300


@pytest.mark.asyncio
async def test_gemini_scripted_tts_is_available_for_non_german_language() -> None:
    await asyncio.sleep(0)
    settings = Settings(
        CALLTOOL_ENV="test",
        GOOGLE_API_KEY=SecretStr("test-google-key"),
        CALLTOOL_VOICE_PROVIDER="gemini",
        CALLTOOL_VOICE_MODEL="gemini-3.1-flash-live-preview",
        CALLTOOL_VOICE_LANGUAGE="en",
        CALLTOOL_VOICE_NAME="Puck",
        CALLTOOL_PROMPT_DIR=str(DEFAULT_PROMPT_DIR),
    )

    runtime = build_voice_runtime(make_call(), settings, [])

    assert runtime.scripted_tts is not None


@pytest.mark.asyncio
async def test_livekit_v1_mini_disables_provider_turn_detection_and_adds_local_vad() -> None:
    await asyncio.sleep(0)
    settings = Settings(
        CALLTOOL_ENV="test",
        OPENAI_API_KEY=SecretStr("test-openai-key"),
        CALLTOOL_VOICE_PROVIDER="openai",
        CALLTOOL_VOICE_MODEL="gpt-realtime-2.1-mini",
        CALLTOOL_VOICE_NAME="marin",
        CALLTOOL_PROMPT_DIR=str(DEFAULT_PROMPT_DIR),
        CALLTOOL_TURN_DETECTION_MODE="livekit_v1_mini",
        CALLTOOL_INTERRUPTION_MODE="vad",
    )

    runtime = build_voice_runtime(make_call(), settings, [])

    assert runtime.turn_detection_mode == "livekit_v1_mini"
    assert runtime.interruption_mode == "vad"
    assert runtime.session.turn_detection.model == "turn-detector-v1-mini"
    assert runtime.session.vad.model == "silero"
    assert runtime.session.llm._opts.turn_detection is None


@pytest.mark.asyncio
async def test_livekit_v1_mini_accepts_language_specific_threshold_overrides() -> None:
    await asyncio.sleep(0)
    settings = Settings(
        CALLTOOL_ENV="test",
        OPENAI_API_KEY=SecretStr("test-openai-key"),
        CALLTOOL_VOICE_PROVIDER="openai",
        CALLTOOL_VOICE_MODEL="gpt-realtime-2.1-mini",
        CALLTOOL_VOICE_LANGUAGE="de",
        CALLTOOL_VOICE_NAME="marin",
        CALLTOOL_PROMPT_DIR=str(DEFAULT_PROMPT_DIR),
        CALLTOOL_TURN_DETECTION_MODE="livekit_v1_mini",
        CALLTOOL_TURN_UNLIKELY_THRESHOLD="0.61",
        CALLTOOL_TURN_BACKCHANNEL_THRESHOLD="0.42",
    )

    runtime = build_voice_runtime(make_call(), settings, [])

    assert runtime.turn_unlikely_threshold == 0.61
    assert runtime.turn_backchannel_threshold == 0.42
    thresholds = runtime.session.turn_detection._opts.thresholds
    assert thresholds._thresholds["de"] == 0.61
    assert thresholds._bc_overrides["de"] == 0.42


def test_endpointing_overrides_are_validated_and_exposed() -> None:
    settings = Settings(
        CALLTOOL_ENV="test",
        CALLTOOL_ENDPOINTING_MIN_DELAY="0.2",
        CALLTOOL_ENDPOINTING_MAX_DELAY="0.9",
    )

    assert settings.endpointing_min_delay() == 0.2
    assert settings.endpointing_max_delay() == 0.9


@pytest.mark.asyncio
async def test_runtime_passes_endpointing_bounds_to_livekit() -> None:
    await asyncio.sleep(0)
    settings = Settings(
        CALLTOOL_ENV="test",
        OPENAI_API_KEY=SecretStr("test-openai-key"),
        CALLTOOL_VOICE_PROVIDER="openai",
        CALLTOOL_VOICE_MODEL="gpt-realtime-2.1-mini",
        CALLTOOL_PROMPT_DIR=str(DEFAULT_PROMPT_DIR),
        CALLTOOL_ENDPOINTING_MIN_DELAY="0.2",
        CALLTOOL_ENDPOINTING_MAX_DELAY="0.9",
    )

    runtime = build_voice_runtime(make_call(), settings, [])

    assert runtime.session.options.endpointing["min_delay"] == 0.2
    assert runtime.session.options.endpointing["max_delay"] == 0.9


def test_endpointing_maximum_must_not_be_shorter_than_minimum() -> None:
    settings = Settings(
        CALLTOOL_ENV="test",
        CALLTOOL_ENDPOINTING_MIN_DELAY="1.1",
        CALLTOOL_ENDPOINTING_MAX_DELAY="0.9",
    )

    with pytest.raises(ValueError, match="MAX_DELAY"):
        settings.endpointing_max_delay()


def test_turn_threshold_environment_overrides_are_bounded() -> None:
    settings = Settings(CALLTOOL_TURN_UNLIKELY_THRESHOLD="1.1")

    with pytest.raises(ValueError, match="CALLTOOL_TURN_UNLIKELY_THRESHOLD"):
        settings.turn_unlikely_threshold()


def test_feature_env_boolean_overrides_are_strict() -> None:
    settings = Settings(
        CALLTOOL_ENV="test",
        CALLTOOL_IVR_ENABLED="yes",
        CALLTOOL_AMD_ENABLED="0",
        CALLTOOL_COLD_TRANSFER_ENABLED="true",
        CALLTOOL_KRISP_ENABLED="off",
    )

    assert settings.ivr_enabled() is True
    assert settings.amd_enabled() is False
    assert settings.cold_transfer_enabled() is True
    assert settings.krisp_enabled() is False

    with pytest.raises(ValueError, match="CALLTOOL_IVR_ENABLED"):
        Settings(CALLTOOL_IVR_ENABLED="sometimes").ivr_enabled()

    with pytest.raises(ValueError, match="self-hosted build"):
        Settings(CALLTOOL_INTERRUPTION_MODE="adaptive").interruption_mode()


@pytest.mark.asyncio
async def test_amd_uses_provider_native_text_classifier_without_cloud_inference() -> None:
    await asyncio.sleep(0)
    settings = Settings(
        CALLTOOL_ENV="test",
        OPENAI_API_KEY=SecretStr("test-openai-key"),
        CALLTOOL_VOICE_PROVIDER="openai",
        CALLTOOL_VOICE_MODEL="gpt-realtime-2.1-mini",
        CALLTOOL_VOICE_NAME="marin",
        CALLTOOL_PROMPT_DIR=str(DEFAULT_PROMPT_DIR),
        CALLTOOL_AMD_ENABLED="true",
    )
    runtime = build_voice_runtime(make_call(), settings, [])

    detector = build_amd_detector(runtime, settings, participant_identity="callee-test")

    assert detector._llm_config.model == "gpt-4.1-mini"
    assert detector._participant_identity == "callee-test"
    assert detector._wait_until_finished is True


def test_prompt_and_native_greeting_receive_selected_language() -> None:
    call = make_call(CallVoiceOptions(language="en"))
    profile = PromptProfile.load(
        Settings(CALLTOOL_ENV="test", CALLTOOL_PROMPT_DIR=str(DEFAULT_PROMPT_DIR))
    )

    prompt = profile.system_prompt(call, "en")
    greeting_instruction = profile.greeting_instruction(call, "en")

    assert "BCP-47-Code en" in prompt
    assert "BCP-47-Code en" in greeting_instruction
    assert "KI-Assistent" in greeting_instruction
