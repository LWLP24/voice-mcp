import hashlib
import hmac
import json

import httpx
import pytest

from calltool.calls.models import CallEvent
from calltool.realtime.events import WebhookEventSink


@pytest.mark.asyncio
async def test_webhook_is_filtered_and_hmac_signed() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    sink = WebhookEventSink("https://example.test/events", "secret", client=client)
    ignored = CallEvent(call_id="call_1", sequence=1, type="call.created")
    delivered = CallEvent(
        call_id="call_1",
        sequence=2,
        type="call.completed",
        payload={"success": True},
    )

    await sink.send(ignored)
    await sink.send(delivered)

    assert len(requests) == 1
    request = requests[0]
    timestamp = request.headers["X-CallTool-Timestamp"]
    expected = hmac.new(
        b"secret",
        timestamp.encode("ascii") + b"." + request.content,
        hashlib.sha256,
    ).hexdigest()
    assert request.headers["X-CallTool-Event"] == "call.completed"
    assert request.headers["X-CallTool-Signature"] == f"sha256={expected}"
    assert json.loads(request.content)["call_id"] == "call_1"
    await client.aclose()
