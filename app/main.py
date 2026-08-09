import logging
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.logging import configure as configure_logging
from app.core.observability import configure as configure_error_tracking
from app.database import SessionLocal

configure_logging(settings.LOG_LEVEL, settings.LOG_FORMAT)
configure_error_tracking()

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Apsara Assistant API",
    description="AI Sales Assistant for Cambodian Businesses",
    version="0.1.0",
)

# Wildcard only where nothing real is served from. Credentialed requests from
# any origin is not something to ship, so a configured list is required outside
# development.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    """Tag every request so its logs can be found together.

    Honours an inbound X-Request-ID when there is one, so a trace started by a
    proxy or the web app carries through rather than restarting here.
    """
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
    started = time.perf_counter()

    response = await call_next(request)

    duration_ms = round((time.perf_counter() - started) * 1000, 1)
    response.headers["X-Request-ID"] = request_id
    logger.info(
        "%s %s %s", request.method, request.url.path, response.status_code,
        extra={"request_id": request_id, "status": response.status_code,
               "duration_ms": duration_ms, "path": request.url.path},
    )
    return response


app.include_router(api_router, prefix="/api/v1")


@app.get("/health")
def health_check():
    """Liveness only — does not touch the database.

    Kept trivial so a load balancer can restart a wedged process without a
    database blip taking every instance out with it.
    """
    return {"status": "ok"}


@app.get("/health/ready")
def readiness_check():
    """Readiness — reports whether this instance can actually serve.

    A process that is up but cannot reach Postgres should be taken out of
    rotation, which /health alone cannot express.
    """
    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
        return {"status": "ok", "database": "ok"}
    except Exception:
        logger.exception("Readiness check failed")
        # 503 rather than an exception, so the body still says what is wrong.
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=503,
                            content={"status": "degraded", "database": "unreachable"})
    finally:
        db.close()
