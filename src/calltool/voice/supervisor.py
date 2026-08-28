from __future__ import annotations

import json

from google import genai
from google.genai import types

from calltool.calls.models import CallOutcome, CallRecord
from calltool.config import Settings


class GeminiSupervisor:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = genai.Client(api_key=settings.GOOGLE_API_KEY.get_secret_value())

    async def enrich_outcome(self, call: CallRecord, outcome: CallOutcome) -> CallOutcome:
        if not self._settings.config.voice.supervisor.enabled:
            return outcome
        prompt = {
            "task": "Formuliere ausschließlich eine knappe deutsche Zusammenfassung.",
            "rule": "Erfinde keine Fakten oder Commitments.",
            "objective": call.request.objective,
            "structured_outcome": outcome.model_dump(mode="json"),
        }
        try:
            response = await self._client.aio.models.generate_content(
                model=self._settings.config.voice.supervisor.model,
                contents=json.dumps(prompt, ensure_ascii=False),
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
        await self._client.aio.aclose()
