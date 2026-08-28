from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from collections.abc import Iterable
from typing import Protocol

import httpx
import structlog
from redis.asyncio import Redis

from calltool.calls.models import CallEvent

logger = structlog.get_logger(__name__)

_CRITICAL_EVENTS = frozenset(
    {
        "call.commit_allowed",
        "call.commit_denied",
        "call.input_required",
        "call.input_received",
        "call.input_timeout",
        "call.completed",
        "call.failed",
        "call.cancelled",
    }
)
_WEBHOOK_EVENTS = frozenset(
    {"call.input_required", "call.completed", "call.failed"}
)


class EventPublisher(Protocol):
    async def publish(self, event: CallEvent) -> None: ...

    async def close(self) -> None: ...


class EventSink(Protocol):
    async def send(self, event: CallEvent) -> None: ...

    async def close(self) -> None: ...


class RedisEventSink:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    @classmethod
    def from_url(cls, redis_url: str) -> RedisEventSink:
        return cls(Redis.from_url(redis_url, decode_responses=True))

    async def send(self, event: CallEvent) -> None:
        body = event.model_dump_json()
        async with self._redis.pipeline(transaction=False) as pipeline:
            pipeline.publish("calltool:events", body)
            pipeline.publish(f"calltool:events:{event.call_id}", body)
            await pipeline.execute()

    async def close(self) -> None:
        await self._redis.aclose()


class WebhookEventSink:
    def __init__(
        self,
        url: str,
        signing_secret: str,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._url = url
        self._secret = signing_secret.encode("utf-8")
        self._client = client or httpx.AsyncClient(timeout=5.0)
        self._owns_client = client is None

    async def send(self, event: CallEvent) -> None:
        if event.type not in _WEBHOOK_EVENTS:
            return
        body = json.dumps(
            event.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        timestamp = str(int(time.time()))
        signature = hmac.new(
            self._secret,
            timestamp.encode("ascii") + b"." + body,
            hashlib.sha256,
        ).hexdigest()
        response = await self._client.post(
            self._url,
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-CallTool-Event": event.type,
                "X-CallTool-Timestamp": timestamp,
                "X-CallTool-Signature": f"sha256={signature}",
            },
        )
        response.raise_for_status()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class AsyncEventDispatcher:
    """Bounded, non-blocking event fan-out for Redis and webhooks."""

    def __init__(self, sinks: Iterable[EventSink], *, queue_size: int = 1024) -> None:
        self._sinks = tuple(sinks)
        self._queue: asyncio.Queue[CallEvent | None] = asyncio.Queue(maxsize=queue_size)
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    def start(self) -> AsyncEventDispatcher:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="calltool-event-dispatcher")
        return self

    async def publish(self, event: CallEvent) -> None:
        if self._closed:
            return
        if event.type in _CRITICAL_EVENTS:
            await self._queue.put(event)
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning(
                "dropping non-critical event",
                call_id=event.call_id,
                event_type=event.type,
            )

    async def _run(self) -> None:
        while True:
            event = await self._queue.get()
            try:
                if event is None:
                    return
                for sink in self._sinks:
                    try:
                        await sink.send(event)
                    except Exception:
                        logger.exception(
                            "event sink failed",
                            call_id=event.call_id,
                            event_type=event.type,
                            sink=type(sink).__name__,
                        )
            finally:
                self._queue.task_done()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._task is not None:
            await self._queue.put(None)
            try:
                async with asyncio.timeout(5):
                    await self._task
            except TimeoutError:
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)
        for sink in self._sinks:
            await sink.close()


def build_event_dispatcher(
    *,
    redis_url: str,
    webhook_url: str,
    webhook_signing_secret: str,
    queue_size: int,
) -> AsyncEventDispatcher:
    sinks: list[EventSink] = [RedisEventSink.from_url(redis_url)]
    if webhook_url:
        sinks.append(WebhookEventSink(webhook_url, webhook_signing_secret))
    return AsyncEventDispatcher(sinks, queue_size=queue_size).start()
