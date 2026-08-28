from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

import structlog
import uvicorn
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Mount, Route

from calltool.api.auth import APIKeyAuthMiddleware, principal_for_api_key
from calltool.api.mcp import ServiceSlot, build_mcp_server
from calltool.api.rest import build_rest_routes
from calltool.calls.dispatcher import CallDispatcher, LiveKitCallDispatcher, NullCallDispatcher
from calltool.calls.repository import CallRepository
from calltool.calls.service import CallService
from calltool.config import Settings, get_settings
from calltool.observability.logging import configure_logging
from calltool.observability.tracing import configure_tracing
from calltool.policy.engine import PolicyEngine
from calltool.realtime.events import build_event_dispatcher
from calltool.storage.memory import MemoryCallRepository
from calltool.storage.postgres import PostgresCallRepository

logger = structlog.get_logger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    repository: CallRepository | None = None,
    dispatcher: CallDispatcher | None = None,
) -> Starlette:
    settings = settings or get_settings()
    slot = ServiceSlot()
    mcp_server = build_mcp_server(
        slot,
        principal_id=principal_for_api_key(settings.CALLTOOL_API_KEY.get_secret_value()),
    )
    mcp_app = mcp_server.streamable_http_app(
        streamable_http_path="/",
        json_response=False,
        stateless_http=False,
        host=settings.config.server.host,
    )

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        configure_logging(settings.CALLTOOL_LOG_LEVEL)
        configure_tracing(
            "calltool-api", settings.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT
        )
        settings.validate_production()
        selected_repository = repository
        selected_dispatcher = dispatcher
        if selected_repository is None:
            if settings.CALLTOOL_ENV == "test":
                selected_repository = MemoryCallRepository()
            else:
                events = build_event_dispatcher(
                    redis_url=settings.REDIS_URL,
                    webhook_url=settings.WEBHOOK_URL,
                    webhook_signing_secret=settings.WEBHOOK_SIGNING_SECRET.get_secret_value(),
                    queue_size=settings.config.performance.event_queue_size,
                )
                selected_repository = await PostgresCallRepository.connect(
                    settings.DATABASE_URL, event_publisher=events
                )
        if selected_dispatcher is None:
            selected_dispatcher = (
                NullCallDispatcher()
                if settings.CALLTOOL_ENV == "test"
                else LiveKitCallDispatcher(settings)
            )
        slot.service = CallService(
            selected_repository,
            selected_dispatcher,
            PolicyEngine(settings.config.policy),
            settings,
        )
        scheduler_task = asyncio.create_task(
            _run_dispatch_scheduler(slot.service), name="calltool-dispatch-scheduler"
        )
        app.state.ready = True
        async with mcp_server.session_manager.run():
            try:
                yield
            finally:
                app.state.ready = False
                scheduler_task.cancel()
                with suppress(asyncio.CancelledError):
                    await scheduler_task
                await slot.service.close()
                slot.service = None

    async def health(_: Request) -> Response:
        return JSONResponse({"status": "ok"})

    async def ready(request: Request) -> Response:
        is_ready = bool(getattr(request.app.state, "ready", False))
        return JSONResponse(
            {"status": "ready" if is_ready else "not_ready"},
            status_code=200 if is_ready else 503,
        )

    async def metrics(_: Request) -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    routes: list[BaseRoute] = [
        Route("/health", health, methods=["GET"]),
        Route("/ready", ready, methods=["GET"]),
        Route("/metrics", metrics, methods=["GET"]),
        *build_rest_routes(slot, settings.config.rest.base_path),
    ]
    if settings.config.mcp.enabled:
        routes.append(Mount(settings.config.mcp.path, app=mcp_app))

    return Starlette(
        debug=settings.CALLTOOL_ENV == "development",
        routes=routes,
        middleware=[Middleware(APIKeyAuthMiddleware, settings=settings)],
        lifespan=lifespan,
    )


async def _run_dispatch_scheduler(service: CallService) -> None:
    while True:
        try:
            await service.dispatch_queued()
        except Exception:
            logger.exception("dispatch scheduler iteration failed")
        await asyncio.sleep(0.5)


def run() -> None:
    settings = get_settings()
    uvicorn.run(
        create_app(settings),
        host=settings.config.server.host,
        port=settings.config.server.port,
        log_config=None,
    )
