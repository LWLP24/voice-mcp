from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from livekit import rtc
from livekit.agents import tts


async def pre_synthesize(model: tts.TTS[Any], text: str) -> rtc.AudioFrame:
    return await model.synthesize(text).collect()


async def frame_stream(frame: rtc.AudioFrame) -> AsyncIterator[rtc.AudioFrame]:
    yield frame
