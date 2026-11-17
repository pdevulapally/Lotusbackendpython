import asyncio
from urllib.parse import urlsplit

from fastapi import HTTPException
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from config import Settings
from health import HEALTH_PATHS
from observability import error_response, logger


class RequestLimitsMiddleware:
    def __init__(self, app: ASGIApp, settings: Settings):
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_started = False
        received_bytes = 0
        timeout = asyncio.timeout(self.settings.request_timeout_seconds)

        async def limited_receive() -> Message:
            nonlocal received_bytes
            message = await receive()
            if message["type"] == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > self.settings.request_max_body_bytes:
                    raise HTTPException(413, "This request is too large. Please send less data.")
            return message

        async def tracked_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        async def reject(status: int, message: str) -> None:
            await error_response(scope, status, message)(scope, receive, send)

        try:
            headers = Headers(scope=scope)
            hosts = headers.getlist("host")
            if len(hosts) != 1:
                await reject(400, "The request host is invalid.")
                return
            try:
                host = urlsplit(f"//{hosts[0]}")
                port = host.port
            except ValueError:
                await reject(400, "The request host is invalid.")
                return
            if (
                host.hostname not in self.settings.allowed_hosts
                or port == 0
                or host.username is not None
                or host.password is not None
                or host.path
                or host.query
                or host.fragment
            ):
                await reject(400, "The request host is invalid.")
                return
            health_probe = scope["method"] in {"GET", "HEAD"} and scope["path"] in HEALTH_PATHS
            if not health_probe and self.settings.require_https and scope["scheme"] != "https":
                await reject(400, "Please use HTTPS to connect to this service.")
                return
            origin = headers.get("origin")
            if origin is not None and origin not in self.settings.cors_allowed_origins:
                await reject(403, "This website is not allowed to access the service.")
                return
            lengths = headers.getlist("content-length")
            if lengths:
                if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
                    await reject(400, "The request size header is invalid.")
                    return
                digits = lengths[0].lstrip("0")
                if len(digits) > len(str(self.settings.request_max_body_bytes)) or (
                    digits and int(digits) > self.settings.request_max_body_bytes
                ):
                    await reject(413, "This request is too large. Please send less data.")
                    return
            async with timeout:
                await self.app(scope, limited_receive, tracked_send)
        except Exception as error:
            timed_out = isinstance(error, TimeoutError) and timeout.expired()
            status = 504 if timed_out else 500
            logger.error(
                "request_timeout" if timed_out else "request_failed",
                extra={
                    "request_id": scope["state"]["request_id"],
                    "exception_type": type(error).__name__,
                    "status": status,
                },
            )
            if response_started:
                # A started stream cannot be replaced with a second response.
                raise RuntimeError(
                    "Response interrupted; check application request logs."
                ) from None
            message = (
                "This request took too long. Please try again."
                if timed_out
                else "We couldn't complete your request. Please try again later."
            )
            await reject(status, message)
