"""FastAPI application factory, middleware, and exception handlers.

Note on the generated ``/openapi.json``: FastAPI produces it from the code as a
convenience for Swagger UI. It is **not** the contract. ``docs/openapi.yaml`` is
hand-written and reviewed, and where the two disagree the code is wrong.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.v1 import api_router
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.db.session import dispose_engine, get_sessionmaker

logger = logging.getLogger(__name__)

DESCRIPTION = """
Backend API for the **Account Executive (AE)** module of the HiFi AIR Sales Management
Platform.

AE activities only. GSE / DSE / DSF flows — including Open New Outlet and Gadget Store
sell-in — are explicitly out of scope.

* The AE identity always comes from the JWT. No endpoint accepts an `aeId`.
* MSISDNs are normalised to `62` form. A barcode scanned as `085882724305` is stored
  as `6285882724305`.
* The server is authoritative on time; `deviceTime` is recorded for audit, never trusted.
* Money is IDR in whole rupiah as an integer.
* Every creating `POST` accepts an `Idempotency-Key` header so the mobile offline
  outbox can retry safely.
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    settings.file_storage_dir.mkdir(parents=True, exist_ok=True)
    _warn_about_default_secrets(settings)
    yield
    await dispose_engine()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    app = FastAPI(
        title="FWA Sales Digitalization — Account Executive API",
        version="1.0.0",
        summary="Backend contract for the HiFi AIR Account Executive mobile application.",
        description=DESCRIPTION,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )
    app.state.settings = settings

    if settings.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.include_router(api_router, prefix="/v1")
    _register_exception_handlers(app)

    @app.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        """Liveness plus a database round trip — a pod that cannot reach PostgreSQL is
        not healthy, however happily it serves this route."""
        async with get_sessionmaker(settings)() as session:
            await session.execute(text("SELECT 1"))
        return {"status": "ok"}

    return app


def _register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.to_payload(),
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "VALIDATION_FAILED",
                    "message": "The request could not be processed. Check the highlighted fields.",
                    "details": _validation_details(exc.errors()),
                }
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        # Everything leaves through the same envelope, including the 404s and 405s
        # Starlette raises before any of our code runs.
        codes = {401: "UNAUTHORIZED", 403: "FORBIDDEN", 404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": codes.get(exc.status_code, "HTTP_ERROR"),
                    "message": str(exc.detail),
                }
            },
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        # Log the detail, return none of it. This system holds names, phone numbers,
        # addresses and GPS coordinates; a stack trace in a response is a leak.
        logger.exception("Unhandled error", exc_info=exc)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "Something went wrong. Try again in a moment.",
                }
            },
        )


def _validation_details(errors: Sequence[Any]) -> list[dict[str, str]]:
    details: list[dict[str, str]] = []
    for error in errors:
        location = [str(part) for part in error.get("loc", []) if part not in {"body", "query"}]
        details.append(
            {
                "field": ".".join(location) or "body",
                "issue": str(error.get("msg", "invalid")),
            }
        )
    return details


def _warn_about_default_secrets(settings: Settings) -> None:
    defaults = {
        "JWT_SECRET": settings.jwt_secret.startswith("change-me"),
        "SERVICE_API_KEY": settings.service_api_key.startswith("change-me"),
        "FILE_URL_SECRET": settings.file_url_secret.startswith("change-me"),
    }
    problems = [name for name, is_default in defaults.items() if is_default]

    # HS256 keys shorter than the digest are a real weakness, not a style point.
    if len(settings.jwt_secret) < 32:
        problems.append("JWT_SECRET (shorter than 32 characters)")

    if not problems:
        return

    message = f"Weak or placeholder secrets in use: {', '.join(problems)}."
    if settings.is_production:
        raise RuntimeError(message + " Refusing to start in production.")
    logger.warning("%s Fine for local development, never for a deployed environment.", message)


app = create_app()
