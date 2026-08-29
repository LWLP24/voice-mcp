from __future__ import annotations

import os
import sys

from livekit.agents import AgentServer, JobContext, cli

from calltool.config import get_settings
from calltool.observability.logging import configure_logging
from calltool.observability.tracing import configure_tracing
from calltool.voice.prompts import PromptProfile
from calltool.worker.agent import handle_call

settings = get_settings()
server = AgentServer(
    drain_timeout=settings.config.calls.max_duration_seconds + 300,
    num_idle_processes=settings.config.performance.prewarm_workers,
    host=settings.config.server.host,
    port=settings.config.server.health_port,
    prometheus_port=9090,
    ws_url=settings.LIVEKIT_URL,
    api_key=settings.LIVEKIT_API_KEY,
    api_secret=settings.LIVEKIT_API_SECRET.get_secret_value(),
    log_level=settings.CALLTOOL_LOG_LEVEL.upper(),
)


@server.rtc_session(agent_name="calltool")
async def calltool_agent(ctx: JobContext) -> None:
    await handle_call(ctx, settings)


def run() -> None:
    configure_logging(settings.CALLTOOL_LOG_LEVEL)
    configure_tracing("calltool-worker", settings.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT)
    PromptProfile.load(settings)
    os.environ.setdefault("LIVEKIT_URL", settings.LIVEKIT_URL)
    os.environ.setdefault("LIVEKIT_API_KEY", settings.LIVEKIT_API_KEY)
    os.environ.setdefault("LIVEKIT_API_SECRET", settings.LIVEKIT_API_SECRET.get_secret_value())
    sys.argv = [sys.argv[0], "start"]
    cli.run_app(server)
