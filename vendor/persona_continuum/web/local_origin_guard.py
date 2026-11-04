"""Keep the local Web API reachable only from its own pages.

The API has no login: it trusts whoever can reach it.  Binding to 127.0.0.1
keeps other machines out, but not other websites open in the user's browser,
which can send requests to localhost.  This guard rejects:

* any HTTP or WebSocket request whose ``Origin`` is not the server's own
  origin (cross-site fetch, form posts and WebSocket hijacking), and
* when serving on a loopback address, any ``Host`` header that is not a
  loopback name (DNS rebinding, where an attacker's domain resolves to
  127.0.0.1 and becomes "same-origin").

Requests without an ``Origin`` header (CLI tools, MCP clients, top-level
navigation) are not cross-site requests and pass through.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, MutableMapping
from typing import Any
from urllib.parse import urlsplit

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


def is_loopback_bind(host: str) -> bool:
    return host.strip().strip("[]").casefold() in {"localhost", "127.0.0.1", "::1"}


def _hostname(value: str) -> str:
    """Hostname part of a ``Host`` header value (port and brackets removed)."""

    value = value.strip().casefold()
    if value.startswith("["):
        return value[1 : value.find("]")] if "]" in value else value
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


def _is_loopback_name(hostname: str) -> bool:
    return hostname in {"localhost", "127.0.0.1", "::1"} or hostname.endswith(".localhost")


class LocalOriginGuard:
    """ASGI middleware enforcing same-origin access (HTTP and WebSocket)."""

    def __init__(self, app: ASGIApp, *, restrict_host_to_loopback: bool = False) -> None:
        self.app = app
        self.restrict_host_to_loopback = restrict_host_to_loopback

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        headers = _headers(scope.get("headers") or [])
        host = headers.get("host", "")
        reason = self._rejection(host, headers.get("origin"))
        if reason is None:
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            # Closing before accept makes the server answer the handshake 403.
            await receive()
            await send({"type": "websocket.close", "code": 4403})
            return
        body = reason.encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    def _rejection(self, host: str, origin: str | None) -> str | None:
        if self.restrict_host_to_loopback and not _is_loopback_name(_hostname(host)):
            return "forbidden_host"
        if origin is None:
            return None
        parsed = urlsplit(origin)
        if origin == "null" or not parsed.netloc:
            return "forbidden_origin"
        if parsed.netloc.casefold() != host.strip().casefold():
            return "forbidden_origin"
        return None


def _headers(raw: Iterable[tuple[bytes, bytes]]) -> dict[str, str]:
    return {key.decode("latin-1").casefold(): value.decode("latin-1") for key, value in raw}


__all__ = ["LOOPBACK_HOSTS", "LocalOriginGuard", "is_loopback_bind"]
