from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, cast

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.types import ExceptionHandler

from .api import router
from .config import Settings, get_settings
from .db import Base, build_engine, build_session_factory
from .documents.errors import DocumentError
from .errors import ErrorCode, ShioriError, shiori_error_handler
from .security import APIKeyCipher, CSRFManager, RequestSecurityMiddleware


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or get_settings()
    engine = build_engine(resolved)
    session_factory = build_session_factory(engine)
    csrf = CSRFManager(resolved.load_csrf_secret())

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> Any:
        resolved.storage_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if resolved.auto_create_tables:
            Base.metadata.create_all(engine)
        yield
        engine.dispose()

    app = FastAPI(
        title="栞译台 Shiori API",
        version="0.1.0",
        description="Private Japanese book translation and publishing workbench.",
        docs_url=None if resolved.environment == "production" else "/docs",
        redoc_url=None,
        openapi_url="/openapi.json" if resolved.environment != "production" else None,
        lifespan=lifespan,
    )
    app.state.settings = resolved
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.csrf = csrf
    if resolved.master_key is not None or resolved.master_key_file is not None:
        app.state.key_cipher = APIKeyCipher(resolved.load_master_key())

    app.add_exception_handler(ShioriError, cast(ExceptionHandler, shiori_error_handler))

    @app.exception_handler(DocumentError)
    async def document_error(_: Request, exc: DocumentError) -> JSONResponse:
        status_code = (
            409
            if exc.code
            in {
                ErrorCode.OCR_REQUIRED,
                ErrorCode.ENCODING_CONFIRMATION_REQUIRED,
                ErrorCode.UNSUPPORTED_DRM,
            }
            else 422
        )
        return JSONResponse(
            status_code=status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    **({"details": exc.details} if exc.details is not None else {}),
                }
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        details = [
            {
                "location": list(error["loc"]),
                "message": error["msg"],
                "type": error["type"],
            }
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": ErrorCode.VALIDATION_ERROR,
                    "message": "Request validation failed",
                    "details": details,
                }
            },
        )

    @app.get("/healthz", include_in_schema=False)
    def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(router)
    app.add_middleware(RequestSecurityMiddleware, settings=resolved, csrf=csrf)
    return app


def run() -> None:
    uvicorn.run(
        create_app(),
        host="127.0.0.1",
        port=18740,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
    )
