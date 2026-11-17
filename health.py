from redis.exceptions import RedisError
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, Router

from observability import error_response

HEALTH_PATHS = frozenset({"/health/live", "/health/ready"})


async def liveness(request: Request):
    return JSONResponse({"status": "live"})


async def readiness(request: Request):
    if not request.app.state.ready:
        return error_response(request.scope, 503, "The service is not ready yet.")
    try:
        await run_in_threadpool(request.app.state.rate_limit_storage.storage.ping)
    except RedisError:
        return error_response(request.scope, 503, "The service is temporarily unavailable.")
    # Firebase/Firestore clients initialize at startup. Remote Firebase verification
    # and Firestore policy/resource checks happen on access, not in this Redis probe.
    return JSONResponse({"status": "ready"})


# A plain operational router avoids inheriting user authentication dependencies.
# It uses the parent application's state, Redis client, and outer middleware.
router = Router(routes=[Route("/live", liveness), Route("/ready", readiness)])
