from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from google.genai import types
from livekit.agents import Agent, AgentSession, llm, tts
from livekit.agents.types import NOT_GIVEN
from livekit.plugins import google

from calltool.calls.models import CallRecord
from calltool.config import Settings
from calltool.voice.prompts import compile_call_prompt


@dataclass(frozen=True)
class VoiceRuntime:
    session: AgentSession[None]
    agent: Agent
    scripted_tts: tts.TTS[Any]


def build_voice_runtime(
    call: CallRecord,
    settings: Settings,
    tools: list[llm.Tool | llm.Toolset],
) -> VoiceRuntime:
    voice = settings.config.voice
    api_key = settings.GOOGLE_API_KEY.get_secret_value()
    realtime = google.realtime.RealtimeModel(
        model=voice.realtime.model,
        api_key=api_key,
        voice=voice.realtime.voice,
        instructions=compile_call_prompt(call),
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
    )
    scripted_tts = google.beta.GeminiTTS(
        model=voice.scripted_tts.model,
        voice_name=voice.scripted_tts.voice,
        api_key=api_key,
    )
    session: AgentSession[None] = AgentSession(
        llm=realtime,
        tts=scripted_tts,
        tools=tools,
        turn_detection="realtime_llm",
        allow_interruptions=True,
        min_interruption_duration=0.15,
        false_interruption_timeout=1.0,
        max_tool_steps=5,
    )
    agent = Agent(instructions=compile_call_prompt(call), tools=tools)
    return VoiceRuntime(session=session, agent=agent, scripted_tts=scripted_tts)
