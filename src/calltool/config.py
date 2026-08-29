from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from calltool.language import normalize_language_code


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=1, le=65535)
    health_port: int = Field(default=8081, ge=1, le=65535)


class MCPConfig(BaseModel):
    enabled: bool = True
    path: str = "/mcp"
    protocol: str = "2026-07-28"


class RESTConfig(BaseModel):
    enabled: bool = True
    base_path: str = "/v1"


class InboundCallsConfig(BaseModel):
    enabled: bool = True
    organization_name: str = "LWLP"
    objective: str = (
        "Nimm das Anliegen des Anrufers auf, beantworte allgemeine Fragen soweit möglich "
        "und fasse das Gespräch strukturiert zusammen."
    )
    constraints: list[str] = Field(
        default_factory=lambda: [
            "Keine verbindlichen Zusagen oder kostenpflichtigen Handlungen vornehmen.",
            "Bei fehlenden Informationen transparent bleiben und nichts erfinden.",
        ]
    )
    room_prefix: str = Field(default="calltool-inbound-", min_length=1, max_length=64)
    allowed_addresses: list[str] = Field(
        default_factory=lambda: ["185.246.41.140/32", "185.246.41.141/32"]
    )


class CallsConfig(BaseModel):
    max_concurrent: int = Field(default=2, ge=1)
    ring_timeout_seconds: int = Field(default=45, ge=1)
    max_duration_seconds: int = Field(default=1800, ge=1)
    user_input_timeout_seconds: int = Field(default=180, ge=1)
    inbound: InboundCallsConfig = Field(default_factory=InboundCallsConfig)


class ToggleConfig(BaseModel):
    enabled: bool = True


class RealtimeVoiceConfig(BaseModel):
    provider: Literal["gemini", "openai"] = "gemini"
    model: str = "gemini-3.1-flash-live-preview"
    voice: str = "Puck"
    language: str = "de"
    thinking_level: Literal["minimal", "low", "medium", "high"] = "minimal"
    input_transcription: bool = True
    output_transcription: bool = True
    context_compression: ToggleConfig = Field(default_factory=ToggleConfig)
    session_resumption: ToggleConfig = Field(default_factory=ToggleConfig)

    @field_validator("language")
    @classmethod
    def validate_language(cls, value: str) -> str:
        return normalize_language_code(value)


class ScriptedTTSConfig(BaseModel):
    provider: str = "gemini"
    model: str = "gemini-3.1-flash-tts-preview"
    voice: str = "Puck"
    enabled: bool = True


class ShadowSTTConfig(BaseModel):
    enabled: bool = False
    provider: str = "gemini"
    model: str = "gemini-3.5-transcribe-live"


class SupervisorConfig(BaseModel):
    enabled: bool = True
    model: str = "gemini-3.7-flash"
    thinking_level: Literal["low", "medium", "high"] = "low"
    mode: Literal["background", "on_demand"] = "background"


class PromptProfileConfig(BaseModel):
    directory: Path = Path("prompts/default")
    system_inbound: str = "system-inbound.md"
    system_outbound: str = "system-outbound.md"
    greeting_inbound: str = "greeting-inbound.txt"
    greeting_outbound: str = "greeting-outbound.txt"
    greeting_instruction: str = "greeting-instruction.md"
    watchdog_instruction: str = "watchdog-instruction.md"
    watchdog_fallback: str = "watchdog-fallback.txt"
    supervisor: str = "supervisor.md"

    @field_validator(
        "system_inbound",
        "system_outbound",
        "greeting_inbound",
        "greeting_outbound",
        "greeting_instruction",
        "watchdog_instruction",
        "watchdog_fallback",
        "supervisor",
    )
    @classmethod
    def validate_filename(cls, value: str) -> str:
        filename = value.strip()
        if not filename or Path(filename).name != filename:
            raise ValueError("prompt filenames must be plain filenames without directories")
        return filename


class VoiceConfig(BaseModel):
    realtime: RealtimeVoiceConfig = Field(default_factory=RealtimeVoiceConfig)
    scripted_tts: ScriptedTTSConfig = Field(default_factory=ScriptedTTSConfig)
    shadow_stt: ShadowSTTConfig = Field(default_factory=ShadowSTTConfig)
    supervisor: SupervisorConfig = Field(default_factory=SupervisorConfig)
    prompts: PromptProfileConfig = Field(default_factory=PromptProfileConfig)


class PerformanceTargets(BaseModel):
    turn_latency_p50_ms: int = 600
    turn_latency_p95_ms: int = 1200
    barge_in_stop_p95_ms: int = 250
    local_tool_p95_ms: int = 20


class PerformanceConfig(BaseModel):
    prewarm_workers: int = Field(default=1, ge=0)
    event_queue_size: int = Field(default=1024, ge=16)
    watchdog_silence_seconds: float = Field(default=2.5, ge=0.5)
    targets: PerformanceTargets = Field(default_factory=PerformanceTargets)


class PolicyConfig(BaseModel):
    require_commit_authorization: bool = True
    allowed_country_codes: list[str] = Field(default_factory=lambda: ["DE"])
    block_emergency_numbers: bool = True
    block_premium_numbers: bool = True


class StorageConfig(BaseModel):
    transcript: bool = False
    audio: bool = False


class FeaturesConfig(BaseModel):
    shadow_stt: bool = False
    supervisor: bool = True
    scripted_confirmations: bool = True
    cascade_fallback: bool = False
    semantic_turn_detector: bool = False


class FileConfig(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    mcp: MCPConfig = Field(default_factory=MCPConfig)
    rest: RESTConfig = Field(default_factory=RESTConfig)
    calls: CallsConfig = Field(default_factory=CallsConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    performance: PerformanceConfig = Field(default_factory=PerformanceConfig)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    features: FeaturesConfig = Field(default_factory=FeaturesConfig)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    CALLTOOL_ENV: Literal["development", "test", "production"] = "development"
    CALLTOOL_CONFIG: Path = Path("config/calltool.yaml")
    CALLTOOL_API_KEY: SecretStr = SecretStr("change-me")
    CALLTOOL_LOG_LEVEL: str = "INFO"
    OTEL_EXPORTER_OTLP_TRACES_ENDPOINT: str = ""

    DATABASE_URL: str = "postgresql://calltool:calltool@localhost:5432/calltool"
    REDIS_URL: str = "redis://localhost:6379/0"

    LIVEKIT_URL: str = "ws://localhost:7880"
    LIVEKIT_API_KEY: str = "devkey"
    LIVEKIT_API_SECRET: SecretStr = SecretStr("secret")
    LIVEKIT_SIP_TRUNK_ID: str = ""

    TELNYX_SIP_ADDRESS: str = "sip.telnyx.eu"
    TELNYX_SIP_USERNAME: str = ""
    TELNYX_SIP_PASSWORD: SecretStr = SecretStr("")
    TELNYX_FROM_NUMBER: str = ""

    GOOGLE_API_KEY: SecretStr = SecretStr("")
    OPENAI_API_KEY: SecretStr = SecretStr("")
    CALLTOOL_VOICE_PROVIDER: str = ""
    CALLTOOL_VOICE_MODEL: str = ""
    CALLTOOL_VOICE_LANGUAGE: str = ""
    CALLTOOL_VOICE_NAME: str = ""
    CALLTOOL_PROMPT_DIR: str = ""
    WEBHOOK_URL: str = ""
    WEBHOOK_SIGNING_SECRET: SecretStr = SecretStr("change-me")

    config: FileConfig = Field(default_factory=FileConfig)

    @classmethod
    def load(cls) -> Settings:
        environment = cls()
        config_path = environment.CALLTOOL_CONFIG
        if not config_path.exists():
            return environment
        with config_path.open(encoding="utf-8") as config_file:
            raw_config = yaml.safe_load(config_file) or {}
        return cls(config=FileConfig.model_validate(raw_config))

    def validate_production(self) -> None:
        if self.CALLTOOL_ENV != "production":
            return
        placeholders = {
            "CALLTOOL_API_KEY": self.CALLTOOL_API_KEY.get_secret_value(),
            "LIVEKIT_API_KEY": self.LIVEKIT_API_KEY,
            "LIVEKIT_API_SECRET": self.LIVEKIT_API_SECRET.get_secret_value(),
            "LIVEKIT_SIP_TRUNK_ID": self.LIVEKIT_SIP_TRUNK_ID,
            "TELNYX_SIP_USERNAME": self.TELNYX_SIP_USERNAME,
            "TELNYX_SIP_PASSWORD": self.TELNYX_SIP_PASSWORD.get_secret_value(),
            "TELNYX_FROM_NUMBER": self.TELNYX_FROM_NUMBER,
            "WEBHOOK_SIGNING_SECRET": self.WEBHOOK_SIGNING_SECRET.get_secret_value(),
        }
        provider = self.CALLTOOL_VOICE_PROVIDER or self.config.voice.realtime.provider
        if provider == "openai":
            placeholders["OPENAI_API_KEY"] = self.OPENAI_API_KEY.get_secret_value()
        else:
            placeholders["GOOGLE_API_KEY"] = self.GOOGLE_API_KEY.get_secret_value()
        if (
            self.config.voice.supervisor.enabled
            or self.config.voice.shadow_stt.enabled
            or self.config.features.shadow_stt
        ):
            placeholders["GOOGLE_API_KEY"] = self.GOOGLE_API_KEY.get_secret_value()
        invalid = [
            name
            for name, value in placeholders.items()
            if value in {"", "change-me", "secret", "devkey"}
        ]
        if invalid:
            names = ", ".join(sorted(invalid))
            raise ValueError(f"Unsafe production secrets: {names}")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.load()
