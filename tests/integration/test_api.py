import httpx
import pytest
from pydantic import SecretStr
from starlette.applications import Starlette

from calltool.api.app import create_app
from calltool.calls.dispatcher import NullCallDispatcher
from calltool.config import Settings
from calltool.storage.memory import MemoryCallRepository


def build_app() -> Starlette:
    settings = Settings(
        CALLTOOL_ENV="test",
        CALLTOOL_API_KEY=SecretStr("test-secret"),
    )
    return create_app(
        settings,
        repository=MemoryCallRepository(),
        dispatcher=NullCallDispatcher(),
    )


@pytest.mark.asyncio
async def test_health_is_public_and_calls_require_auth() -> None:
    app = build_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/health")).status_code == 200
            assert (await client.get("/ready")).json() == {"status": "ready"}
            assert (await client.post("/v1/calls", json={})).status_code == 401


@pytest.mark.asyncio
async def test_rest_call_lifecycle() -> None:
    headers = {"Authorization": "Bearer test-secret"}
    payload = {
        "target": {"phone_number": "+49301234567", "name": "Praxis"},
        "objective": "Vereinbare einen Kontrolltermin",
        "constraints": ["frühestens 15 Uhr"],
        "permissions": {"may_commit": True, "may_disclose": ["name"]},
        "voice": {
            "provider": "openai",
            "model": "gpt-realtime-2.1-mini",
            "language": "en_us",
            "voice": "cedar",
        },
        "client_request_id": "api-test-1",
    }
    app = build_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post("/v1/calls", headers=headers, json=payload)
            assert created.status_code == 202
            call_id = created.json()["call_id"]

            status = await client.get(f"/v1/calls/{call_id}", headers=headers)
            events = await client.get(f"/v1/calls/{call_id}/events", headers=headers)
            cancelled = await client.post(f"/v1/calls/{call_id}/cancel", headers=headers)

            assert status.json()["status"] == "queued"
            assert status.json()["request"]["voice"] == {
                "provider": "openai",
                "model": "gpt-realtime-2.1-mini",
                "language": "en-US",
                "voice": "cedar",
            }
            assert [event["type"] for event in events.json()["events"]] == [
                "call.created",
                "call.validating",
                "call.queued",
                "call.dispatched",
            ]
            assert cancelled.json()["status"] == "cancelled"
