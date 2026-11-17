import json
import logging
import sys
import time
from collections.abc import Mapping
from contextvars import ContextVar
from datetime import UTC, datetime
from http import HTTPStatus
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("lotusbuild.api")
request_context: ContextVar[str | None] = ContextVar("request_id", default=None)


class AgentJSONResponse(JSONResponse):
    """Keep HTML delimiters out of the wire representation while preserving the decoded text."""

    def render(self, content) -> bytes:
        return (
            super()
            .render(content)
            .replace(b"<", rb"\u003c")
            .replace(b">", rb"\u003e")
            .replace(b"&", rb"\u0026")
        )


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # Only explicitly selected fields are logged. No raw URLs, headers,
        # bodies, claims, exception messages, or validation input values.
        event = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "event": record.getMessage(),
        }
        for field in ("request_id", "method", "route", "status", "duration_ms", "exception_type"):
            if hasattr(record, field):
                event[field] = getattr(record, field)
        context_id = request_context.get()
        if "request_id" not in event and context_id is not None:
            event["request_id"] = context_id
        return json.dumps(event)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logger.handlers = [handler]
    logger.setLevel(level)
    logger.propagate = False


def error_response(
    scope: Scope,
    status: int,
    message: str,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "error": {"code": HTTPStatus(status).name.lower(), "message": message},
            "request_id": scope["state"]["request_id"],
        },
        headers=headers,
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException):
        return error_response(request.scope, error.status_code, str(error.detail), error.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        return error_response(
            request.scope, 422, "Some request details are invalid. Please check them and try again."
        )


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = uuid4().hex
        context_token = request_context.set(request_id)
        scope.setdefault("state", {})["request_id"] = request_id
        started = time.monotonic()
        status = 500

        async def send_with_context(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
                headers["Cache-Control"] = "no-store"
                headers["X-Content-Type-Options"] = "nosniff"
                if headers.get("content-type", "").split(";", 1)[0] == "application/json":
                    headers["Content-Security-Policy"] = (
                        "default-src 'none'; frame-ancestors 'none'; sandbox"
                    )
                headers["X-Frame-Options"] = "DENY"
                headers["Referrer-Policy"] = "no-referrer"
            await send(message)

        try:
            await self.app(scope, receive, send_with_context)
        finally:
            route = scope.get("route")
            logger.info(
                "request_completed",
                extra={
                    "request_id": request_id,
                    "method": scope["method"],
                    "route": route.path if route is not None else "<unmatched>",
                    "status": status,
                    "duration_ms": round((time.monotonic() - started) * 1000, 2),
                },
            )
            request_context.reset(context_token)
