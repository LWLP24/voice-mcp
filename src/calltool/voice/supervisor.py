from __future__ import annotations

import json

from google import genai
from google.genai import types

from calltool.calls.models import CallOutcome, CallRecord
from calltool.config import Settings
from calltool.voice.prompts import PromptProfile


class GeminiSupervisor:
    def __init__(self, settings: Settings, prompt_profile: PromptProfile) -> None:
        self._settings = settings
        self._prompt_profile = prompt_profile
        api_key = settings.GOOGLE_API_KEY.get_secret_value()
        self._client = (
            genai.Client(api_key=api_key)
            if settings.config.voice.supervisor.enabled and api_key
            else None
        )

    async def enrich_outcome(
        self,
        call: CallRecord,
        outcome: CallOutcome,
        language: str,
    ) -> CallOutcome:
        if not self._settings.config.voice.supervisor.enabled or self._client is None:
            return outcome
        prompt = self._prompt_profile.supervisor_prompt(call, outcome, language)
        try:
            response = await self._client.aio.models.generate_content(
                model=self._settings.config.voice.supervisor.model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    thinking_config=types.ThinkingConfig(
                        thinking_level=types.ThinkingLevel(
                            self._settings.config.voice.supervisor.thinking_level.upper()
                        )
                    ),
                    response_mime_type="application/json",
                    response_schema={
                        "type": "object",
                        "properties": {"summary": {"type": "string"}},
                        "required": ["summary"],
                    },
                ),
            )
            parsed = json.loads(response.text or "{}")
            summary = parsed.get("summary")
            if isinstance(summary, str) and summary.strip():
                return outcome.model_copy(update={"summary": summary.strip()})
        except Exception:
            return outcome
        return outcome

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aio.aclose()
