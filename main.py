from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware

from auth import CurrentUser
from auth import lifespan as auth_lifespan
from billing import router as billing_router
from billing import webhooks
from config import Settings, load_settings
from database import lifespan as database_lifespan
from health import router as health_router
from observability import RequestContextMiddleware, configure_logging, install_error_handlers
from project_sessions import session_router
from projects import project_router
from rate_limits import (
    PreAuthRateLimitMiddleware,
    default_rate_limit,
)
from rate_limits import (
    lifespan as rate_limit_lifespan,
)
from request_limits import RequestLimitsMiddleware
from workspace_runs import router as run_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with auth_lifespan(app), database_lifespan(app), rate_limit_lifespan(app):
        app.state.ready = True
        try:
            yield
        finally:
            app.state.ready = False


def get_account(user: CurrentUser):
    return {
        "uid": user["uid"],
        "email": user.get("email"),
        "email_verified": user.get("email_verified", False),
    }


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is None:
        settings = load_settings()
    configure_logging(settings.log_level)
    app = FastAPI(
        lifespan=lifespan,
        dependencies=[Depends(default_rate_limit)],
        docs_url="/docs" if settings.api_docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.api_docs_enabled else None,
        middleware=[
            Middleware(RequestContextMiddleware),
            Middleware(
                CORSMiddleware,
                allow_origins=settings.cors_allowed_origins,
                allow_methods=settings.cors_allowed_methods,
                allow_headers=settings.cors_allowed_headers,
                expose_headers=settings.cors_expose_headers,
                max_age=settings.cors_max_age_seconds,
                allow_credentials=False,
            ),
            Middleware(RequestLimitsMiddleware, settings=settings),
            Middleware(PreAuthRateLimitMiddleware),
        ],
    )
    app.state.settings = settings
    app.state.ready = False
    install_error_handlers(app)
    app.mount("/health", health_router)
    app.mount("/api/v1/webhooks", webhooks)
    app.include_router(billing_router)
    app.get("/api/v1/users/account")(get_account)
    app.include_router(project_router(settings))
    app.include_router(session_router(settings))
    app.include_router(run_router)
    return app
