import logging
from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import select
from app.db.engine import AsyncSessionLocal

logger = logging.getLogger("health")

router = APIRouter(tags=["Health"])

@router.get("/health")
async def health_check():
    """Liveness check. Always returns ok if the process can handle a request."""
    return {"status": "ok", "service": "MigrationBot Enterprise API"}


@router.get("/ready")
async def readiness_check(request: Request):
    """
    Readiness check. Probes Postgres (SELECT 1) and Redis (PING) directly rather
    than trusting the process being up — /health returns a static literal and
    would report healthy even if the app crash-looped on a config error, or if
    Postgres/Redis became unreachable after boot.
    """
    checks: dict[str, str] = {}
    ok = True

    # main.py:lifespan swallows init_db() failures so the process can still boot for
    # debugging — but that means a live SELECT 1 below can't tell "Postgres is down"
    # apart from "Postgres is up, schema was never created" (TDD §16.23). This flag
    # closes that gap.
    if not getattr(request.app.state, "db_initialized", False):
        checks["db_schema"] = "init_db() failed or has not completed yet — see startup logs"
        ok = False

    try:
        async with AsyncSessionLocal() as db:
            await db.execute(select(1))
        checks["postgres"] = "ok"
    except Exception as e:
        logger.warning(f"Readiness check: Postgres unreachable: {e}")
        checks["postgres"] = f"unreachable: {e}"
        ok = False

    try:
        from app.queue.producer import redis_client
        await redis_client.ping()
        checks["redis"] = "ok"
    except Exception as e:
        logger.warning(f"Readiness check: Redis unreachable: {e}")
        checks["redis"] = f"unreachable: {e}"
        ok = False

    if not ok:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=checks)

    return {"status": "ready", "checks": checks}
