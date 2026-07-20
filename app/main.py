import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1.router import api_router
from app.core.config import settings

logging.basicConfig(level=settings.LOG_LEVEL.upper())

app = FastAPI(
    title="Apsara Assistant API",
    description="AI Sales Assistant for Cambodian Businesses",
    version="0.1.0",
    # Hide interactive docs in production
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None if settings.is_production else "/redoc",
    openapi_url=None if settings.is_production else "/openapi.json",
)

# CORS: an explicit origin allowlist (never "*" with credentials — browsers
# reject that combination and it is unsafe).
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Give schema validation the same translatable shape as our own errors.

    FastAPI's default 422 body is a list of English pydantic messages with no
    stable identifier, so the UI could only ever render it verbatim — the one
    remaining place a Khmer seller would be shown raw English. The field name
    travels as a param; it stays untranslated on purpose, since it names an API
    field rather than anything the seller typed.

    The forms validate with Zod first, so reaching this is unusual — but "the
    error nobody expects" is exactly the one worth not leaving in English.
    """
    first = exc.errors()[0] if exc.errors() else {}
    # loc is like ("body", "email"); the last element is the field itself.
    location = [str(part) for part in first.get("loc", []) if part != "body"]
    field = location[-1] if location else ""

    return JSONResponse(
        status_code=422,
        content={
            "detail": {
                "code": "validation_error",
                "message": first.get("msg", "Validation error"),
                "params": {"field": field},
            }
        },
    )


app.include_router(api_router, prefix="/api/v1")


@app.get("/health", tags=["health"])
def health_check():
    return {"status": "ok", "environment": settings.ENVIRONMENT}
