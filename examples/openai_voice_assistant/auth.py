"""Optional password protection for running the assistant on a public URL.

Set ``APP_PASSWORD`` to turn it on. Browsers sign in once on ``/login`` and get a long-lived
HttpOnly cookie (so an installed phone app stays signed in); scripts can instead send
``Authorization: Bearer <APP_PASSWORD>``. Changing the password signs everyone out.

Leave ``APP_PASSWORD`` unset on AgentCore Runtime, which already authenticates callers.
"""

import hashlib
import hmac
import os
from http.cookies import SimpleCookie
from typing import Any, Callable, Iterable

COOKIE_NAME = "assistant_session"
COOKIE_MAX_AGE = 60 * 60 * 24 * 90  # 90 days

# Paths reachable without signing in: health check, the login page, and what an
# unauthenticated phone needs to show the app icon and install prompt.
PUBLIC_PATHS = {"/ping", "/login", "/manifest.webmanifest", "/sw.js", "/offline.html", "/favicon.ico"}
PUBLIC_PREFIXES = ("/icons/",)


def password() -> str:
    """The configured password, or an empty string when protection is off."""
    return os.environ.get("APP_PASSWORD", "")


def session_token(secret: str) -> str:
    """Cookie value derived from the password, so no server-side session store is needed."""
    return hmac.new(secret.encode(), b"voice-assistant-session", hashlib.sha256).hexdigest()


def check_password(candidate: str) -> bool:
    """Constant-time comparison against the configured password."""
    secret = password()
    return bool(secret) and hmac.compare_digest(candidate.encode(), secret.encode())


def _header(headers: Iterable[tuple], name: bytes) -> str:
    for key, value in headers:
        if key.lower() == name:
            return value.decode("latin-1")
    return ""


def is_authorized(scope: dict) -> bool:
    """Whether an HTTP or WebSocket request carries a valid cookie or bearer token."""
    secret = password()
    if not secret:
        return True
    headers = scope.get("headers", [])
    bearer = _header(headers, b"authorization")
    if bearer.lower().startswith("bearer ") and hmac.compare_digest(bearer[7:].encode(), secret.encode()):
        return True
    cookie = SimpleCookie(_header(headers, b"cookie")).get(COOKIE_NAME)
    return cookie is not None and hmac.compare_digest(cookie.value.encode(), session_token(secret).encode())


class PasswordMiddleware:
    """Pure ASGI middleware so it guards WebSocket handshakes as well as HTTP requests."""

    def __init__(self, app: Callable) -> None:
        """Wrap the ASGI ``app``."""
        self.app = app

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> Any:
        """Let public paths and signed-in requests through; turn everything else away."""
        path = scope.get("path", "")
        if (
            scope["type"] not in ("http", "websocket")
            or path in PUBLIC_PATHS
            or path.startswith(PUBLIC_PREFIXES)
            or is_authorized(scope)
        ):
            return await self.app(scope, receive, send)

        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4401})
        elif path == "/" and scope.get("method") == "GET":
            await send({"type": "http.response.start", "status": 303, "headers": [(b"location", b"/login")]})
            await send({"type": "http.response.body", "body": b""})
        else:
            await send(
                {"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"application/json")]}
            )
            await send({"type": "http.response.body", "body": b'{"error": "unauthorized"}'})
