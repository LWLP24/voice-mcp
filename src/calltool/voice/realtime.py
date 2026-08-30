from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, cast

from google.genai import types
from livekit.agents import Agent, AgentSession, TurnHandlingOptions, inference, llm, tts
from livekit.agents.types import NOT_GIVEN
from livekit.agents.voice import amd
from livekit.plugins import google
from livekit.plugins import openai as livekit_openai
from openai.types.realtime import AudioTranscription, RealtimeReasoning

from calltool.calls.models import CallRecord, CallVoiceOptions
from calltool.config import Settings
from calltool.language import normalize_language_code
from calltool.voice.prompts import PromptProfile

VoiceProvider = Literal["gemini", "openai"]

OPENAI_REALTIME_MODELS = frozenset({"gpt-realtime-2.1", "gpt-realtime-2.1-mini"})
OPENAI_REALTIME_VOICES = frozenset(
    {"alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse", "marin", "cedar"}
)

_DEFAULT_MODELS: dict[VoiceProvider, str] = {
    "gemini": "gemini-3.1-flash-live-preview",
    "openai": "gpt-realtime-2.1",
}
_DEFAULT_VOICES: dict[VoiceProvider, str] = {"gemini": "Puck", "openai": "marin"}


@dataclass(frozen=True)
class VoiceSelection:
    provider: VoiceProvider
    model: str
    language: str
    voice: str


@dataclass(frozen=True)
class VoiceRuntime:
    session: AgentSession[None]
    agent: Agent
    selection: VoiceSelection
    prompt_profile: PromptProfile
    scripted_tts: tts.TTS[Any] | None
    turn_detection_mode: Literal["realtime_llm", "livekit_v1_mini"]
    interruption_mode: Literal["vad"]
    turn_unlikely_threshold: float | None
    turn_backchannel_threshold: float | None
    ivr_detection_enabled: bool


def resolve_voice_selection(
    settings: Settings,
    options: CallVoiceOptions | None = None,
) -> VoiceSelection:
    configured = settings.config.voice.realtime
    options = options or CallVoiceOptions()

    environment_provider = settings.CALLTOOL_VOICE_PROVIDER.strip().lower()
    base_provider = environment_provider or configured.provider
    provider_raw = options.provider or base_provider
    if provider_raw not in {"gemini", "openai"}:
        raise ValueError("voice provider must be 'gemini' or 'openai'")
    provider = cast(VoiceProvider, provider_raw)

    provider_changed_per_call = options.provider is not None and options.provider != base_provider
    model = options.model
    if model is None and not provider_changed_per_call:
        model = settings.CALLTOOL_VOICE_MODEL.strip() or None
    if model is None:
        model = configured.model if configured.provider == provider else _DEFAULT_MODELS[provider]

    voice_name = options.voice
    if voice_name is None and not provider_changed_per_call:
        voice_name = settings.CALLTOOL_VOICE_NAME.strip() or None
    if voice_name is None:
        voice_name = (
            configured.voice if configured.provider == provider else _DEFAULT_VOICES[provider]
        )
    language = normalize_language_code(
        options.language or settings.CALLTOOL_VOICE_LANGUAGE.strip() or configured.language
    )

    if provider == "openai":
        if model == "gpt-realtime-2.1-flash":
            raise ValueError(
                "OpenAI has no gpt-realtime-2.1-flash model; use gpt-realtime-2.1-mini"
            )
        if model not in OPENAI_REALTIME_MODELS:
            supported = ", ".join(sorted(OPENAI_REALTIME_MODELS))
            raise ValueError(f"unsupported OpenAI Realtime model {model!r}; use {supported}")
        if voice_name not in OPENAI_REALTIME_VOICES and not voice_name.startswith("voice_"):
            supported = ", ".join(sorted(OPENAI_REALTIME_VOICES))
            raise ValueError(
                f"unsupported OpenAI Realtime voice {voice_name!r}; use {supported}, "
                "or an eligible custom voice ID beginning with 'voice_'"
            )
    elif model.startswith("gpt-realtime"):
        raise ValueError("OpenAI Realtime models require voice provider 'openai'")

    return VoiceSelection(
        provider=provider,
        model=model,
        language=language,
        voice=voice_name,
    )


def build_voice_runtime(
    call: CallRecord,
    settings: Settings,
    tools: list[llm.Tool | llm.Toolset],
) -> VoiceRuntime:
    voice = settings.config.voice
    selection = resolve_voice_selection(settings, call.request.voice)
    prompt_profile = PromptProfile.load(settings)
    prompt = prompt_profile.system_prompt(call, selection.language)
    turn_detection_mode = settings.turn_detection_mode()
    interruption_mode = settings.interruption_mode()
    turn_unlikely_threshold = settings.turn_unlikely_threshold()
    turn_backchannel_threshold = settings.turn_backchannel_threshold()
    client_side_turn_detection = turn_detection_mode == "livekit_v1_mini"
    realtime: llm.RealtimeModel

    if selection.provider == "openai":
        api_key = settings.OPENAI_API_KEY.get_secret_value()
        if not api_key:
            raise ValueError("OPENAI_API_KEY is required for the OpenAI Realtime provider")
        realtime = livekit_openai.realtime.RealtimeModel(
            model=selection.model,
            api_key=api_key,
            voice=selection.voice,
            input_audio_transcription=(
                AudioTranscription(
                    model="gpt-4o-mini-transcribe",
                    language=selection.language,
                )
                if voice.realtime.input_transcription
                else None
            ),
            reasoning=RealtimeReasoning(effort=voice.realtime.thinking_level),
            turn_detection=None if client_side_turn_detection else NOT_GIVEN,
        )
        scripted_tts: tts.TTS[Any] | None = None
    else:
        api_key = settings.GOOGLE_API_KEY.get_secret_value()
        if not api_key:
            raise ValueError("GOOGLE_API_KEY is required for the Gemini Realtime provider")
        realtime = google.realtime.RealtimeModel(
            model=selection.model,
            api_key=api_key,
            voice=selection.voice,
            language=selection.language,
            instructions=prompt,
            input_audio_transcription=(
                types.AudioTranscriptionConfig() if voice.realtime.input_transcription else None
            ),
            output_audio_transcription=(
                types.AudioTranscriptionConfig() if voice.realtime.output_transcription else None
            ),
            context_window_compression=(
                types.ContextWindowCompressionConfig(
                    trigger_tokens=25_000,
                    sliding_window=types.SlidingWindow(target_tokens=8_000),
                )
                if voice.realtime.context_compression.enabled
                else NOT_GIVEN
            ),
            session_resumption=(
                types.SessionResumptionConfig(transparent=True)
                if voice.realtime.session_resumption.enabled
                else NOT_GIVEN
            ),
            thinking_config=types.ThinkingConfig(
                thinking_level=types.ThinkingLevel(voice.realtime.thinking_level.upper()),
                include_thoughts=False,
            ),
            realtime_input_config=(
                types.RealtimeInputConfig(
                    automatic_activity_detection=types.AutomaticActivityDetection(disabled=True)
                )
                if client_side_turn_detection
                else NOT_GIVEN
            ),
        )
        scripted_tts = (
            google.beta.GeminiTTS(
                model=voice.scripted_tts.model,
                voice_name=selection.voice,
                api_key=api_key,
            )
            if voice.scripted_tts.enabled
            and voice.scripted_tts.provider == "gemini"
            and selection.provider == "gemini"
            else None
        )

    turn_detection: Any = "realtime_llm"
    if turn_detection_mode == "livekit_v1_mini":
        detector_options: dict[str, Any] = {"version": "v1-mini"}
        if turn_unlikely_threshold is not None:
            detector_options["unlikely_threshold"] = {selection.language: turn_unlikely_threshold}
        if turn_backchannel_threshold is not None:
            detector_options["backchannel_threshold"] = {
                selection.language: turn_backchannel_threshold
            }
        turn_detection = inference.TurnDetector(**detector_options)
    turn_handling: TurnHandlingOptions = {
        "turn_detection": turn_detection,
        "interruption": {
            "enabled": True,
            "mode": interruption_mode,
            "min_duration": voice.realtime.interruptions.min_duration_seconds,
            "false_interruption_timeout": (
                voice.realtime.interruptions.false_interruption_timeout_seconds
            ),
        },
    }
    ivr_detection_enabled = settings.ivr_enabled()
    session: AgentSession[None] = AgentSession(
        vad=(
            inference.VAD()
            if client_side_turn_detection or interruption_mode == "vad"
            else NOT_GIVEN
        ),
        llm=realtime,
        tts=scripted_tts if scripted_tts is not None else NOT_GIVEN,
        tools=tools,
        turn_handling=turn_handling,
        ivr_detection=ivr_detection_enabled,
        max_tool_steps=5,
    )
    agent = Agent(instructions=prompt, tools=tools)
    return VoiceRuntime(
        session=session,
        agent=agent,
        selection=selection,
        prompt_profile=prompt_profile,
        scripted_tts=scripted_tts,
        turn_detection_mode=turn_detection_mode,
        interruption_mode=interruption_mode,
        turn_unlikely_threshold=turn_unlikely_threshold,
        turn_backchannel_threshold=turn_backchannel_threshold,
        ivr_detection_enabled=ivr_detection_enabled,
    )


def build_amd_detector(
    runtime: VoiceRuntime,
    settings: Settings,
    *,
    participant_identity: str,
) -> amd.AMD:
    """Build LiveKit AMD with a provider-native text classifier for self-hosting."""
    if not settings.amd_enabled():
        raise ValueError("AMD is disabled")
    if not settings.config.voice.realtime.input_transcription:
        raise ValueError("AMD requires voice.realtime.input_transcription=true")

    config = settings.config.telephony.amd
    provider = (
        runtime.selection.provider
        if config.classifier_provider == "auto"
        else config.classifier_provider
    )
    classifier: llm.LLM[Any]
    if provider == "openai":
        api_key = settings.OPENAI_API_KEY.get_secret_value()
        if not api_key:
            raise ValueError("OPENAI_API_KEY is required for OpenAI AMD")
        classifier = livekit_openai.LLM(model=config.openai_model, api_key=api_key)
    else:
        api_key = settings.GOOGLE_API_KEY.get_secret_value()
        if not api_key:
            raise ValueError("GOOGLE_API_KEY is required for Gemini AMD")
        classifier = google.LLM(model=config.gemini_model, api_key=api_key)

    return amd.AMD(
        runtime.session,
        llm=classifier,
        participant_identity=participant_identity,
        ivr_detection=settings.ivr_enabled(),
        wait_until_finished=config.wait_until_finished,
    )
