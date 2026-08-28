from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterable

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from calltool.config import Settings


def principal_for_api_key(api_key: str) -> str:
    return "key_" + hashlib.sha256(api_key.encode()).hexdigest()[:16]


class APIKeyAuthMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        settings: Settings,
        excluded_paths: Iterable[str] = ("/health", "/ready", "/metrics"),
    ) -> None:
        self.app = app
        self._api_key = settings.CALLTOOL_API_KEY.get_secret_value()
        self._excluded_paths = frozenset(excluded_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in self._excluded_paths:
            await self.app(scope, receive, send)
            return

        authorization = Headers(scope=scope).get("authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme.casefold() != "bearer" or not token or not hmac.compare_digest(
            token, self._api_key
        ):
            response = JSONResponse(
                {"error": {"code": "unauthorized", "message": "Valid bearer token required"}},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return

        principal = principal_for_api_key(token)
        scope.setdefault("state", {})["principal_id"] = principal
        await self.app(scope, receive, send)
