from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse


class ErrorCode:
    AUTH_REQUIRED = "AUTH_REQUIRED"
    AUTH_INVALID = "AUTH_INVALID"
    OWNER_ONLY = "OWNER_ONLY"
    ORIGIN_REJECTED = "ORIGIN_REJECTED"
    CSRF_INVALID = "CSRF_INVALID"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    UPLOAD_TOO_LARGE = "UPLOAD_TOO_LARGE"
    UPLOAD_INCOMPLETE = "UPLOAD_INCOMPLETE"
    HASH_MISMATCH = "HASH_MISMATCH"
    UNSUPPORTED_DRM = "UNSUPPORTED_DRM"
    OCR_REQUIRED = "OCR_REQUIRED"
    ENCODING_CONFIRMATION_REQUIRED = "ENCODING_CONFIRMATION_REQUIRED"
    ALIGNMENT_REVIEW_REQUIRED = "ALIGNMENT_REVIEW_REQUIRED"
    API_INCOMPATIBLE = "API_INCOMPATIBLE"
    RATE_LIMITED = "RATE_LIMITED"
    EXPORT_VALIDATION_FAILED = "EXPORT_VALIDATION_FAILED"
    PROVIDER_NOT_CONFIGURED = "PROVIDER_NOT_CONFIGURED"
    PROVIDER_UNREACHABLE = "PROVIDER_UNREACHABLE"
    INVALID_STATE = "INVALID_STATE"
    INVALID_COVER = "INVALID_COVER"


class ShioriError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        details: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details


async def shiori_error_handler(_: Request, exc: ShioriError) -> JSONResponse:
    error: dict[str, Any] = {"code": exc.code, "message": exc.message}
    if exc.details is not None:
        error["details"] = exc.details
    return JSONResponse(status_code=exc.status_code, content={"error": error})
