import math
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from hashlib import sha256
from ipaddress import IPv6Address, ip_address

from fastapi import FastAPI, HTTPException, Request
from fastapi.routing import APIRoute
from limits import RateLimitItemPerSecond
from limits.storage import RedisStorage
from limits.strategies import MovingWindowRateLimiter
from redis import ConnectionPool
from redis.backoff import NoBackoff
from redis.exceptions import RedisError
from redis.retry import Retry
from starlette.concurrency import run_in_threadpool
from starlette.types import ASGIApp, Receive, Scope, Send

from auth import CurrentUser
from config import Settings
from health import HEALTH_PATHS
from observability import error_response


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    pool = ConnectionPool.from_url(
        settings.rate_limit_redis_url.get_secret_value(),
        socket_timeout=settings.rate_limit_timeout_seconds,
        socket_connect_timeout=settings.rate_limit_timeout_seconds,
        retry=Retry(NoBackoff(), 0),
        retry_on_timeout=False,
    )
    storage = RedisStorage(
        settings.rate_limit_redis_url.get_secret_value(),
        connection_pool=pool,
        key_prefix=settings.rate_limit_key_prefix,
    )
    try:
        try:
            await run_in_threadpool(storage.storage.ping)
        except RedisError:
            raise RuntimeError(
                "Cannot connect to the configured rate-limit Redis server."
            ) from None

        app.state.rate_limiter = MovingWindowRateLimiter(storage)
        app.state.rate_limit_storage = storage
        app.state.rate_limit_policies = {
            name: RateLimitItemPerSecond(policy.requests, policy.window_seconds)
            for name, policy in settings.rate_limit_policies.items()
        }
        app.state.pre_auth_rate_limit = RateLimitItemPerSecond(
            settings.pre_auth_rate_limit.requests, settings.pre_auth_rate_limit.window_seconds
        )
        pending = [route.dependant for route in app.routes if isinstance(route, APIRoute)]
        while pending:
            dependency = pending.pop()
            if isinstance(dependency.call, RateLimit):
                if dependency.call.policy not in settings.rate_limit_policies:
                    raise RuntimeError(
                        f"Rate-limit policy is not configured: {dependency.call.policy}"
                    )
            pending.extend(dependency.dependencies)
        yield
    finally:
        await run_in_threadpool(storage.storage.close)
        await run_in_threadpool(pool.disconnect)


def consume_limit(app: FastAPI, policy: RateLimitItemPerSecond, group: str, identity: str) -> None:
    limiter = app.state.rate_limiter
    identity_key = sha256(identity.encode()).hexdigest()
    try:
        if limiter.hit(policy, group, identity_key):
            return
        window = limiter.get_window_stats(policy, group, identity_key)
    except RedisError:
        raise HTTPException(
            status_code=503,
            detail="We couldn't process your request. Please try again later.",
        ) from None

    retry_after = max(1, math.ceil(window.reset_time - time.time()))
    raise HTTPException(
        status_code=429,
        detail="You're making requests too quickly. Please wait and try again.",
        headers={"Retry-After": str(retry_after)},
    )


@dataclass(frozen=True)
class RateLimit:
    policy: str

    def __call__(self, request: Request, user: CurrentUser) -> None:
        # Policy names come from server route declarations, never request input.
        enforce_user_limit(request, user, self.policy)


def enforce_user_limit(request: Request, user: dict, policy_name: str) -> None:
    if policy_name not in request.app.state.rate_limit_policies:
        raise RuntimeError(f"Rate-limit policy is not configured: {policy_name}")
    policy = request.app.state.rate_limit_policies[policy_name]
    consume_limit(request.app, policy, f"user:{policy_name}", user["uid"])


class PreAuthRateLimitMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or (
            scope["method"] in {"GET", "HEAD"} and scope["path"] in HEALTH_PATHS
        ):
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        try:
            if request.client is None:
                raise ValueError("Missing peer address")
            address = ip_address(request.client.host)
            if isinstance(address, IPv6Address) and address.ipv4_mapped is not None:
                address = address.ipv4_mapped
        except ValueError:
            response = error_response(scope, 503, "We couldn't identify this connection.")
            await response(scope, receive, send)
            return

        # Uvicorn resolves trusted Caddy headers. Never read forwarded headers here.
        try:
            await run_in_threadpool(
                consume_limit,
                request.app,
                request.app.state.pre_auth_rate_limit,
                "ip",
                str(address),
            )
        except HTTPException as error:
            response = error_response(scope, error.status_code, str(error.detail), error.headers)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def default_rate_limit(request: Request, user: CurrentUser) -> None:
    enforce_user_limit(request, user, request.app.state.settings.rate_limit_default_policy)
