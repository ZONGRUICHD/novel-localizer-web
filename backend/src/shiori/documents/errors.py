from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class DocumentError(Exception):
    """A stable, API-safe document pipeline error."""

    code: str
    message: str
    details: dict[str, Any] | None = None

    def __str__(self) -> str:
        return self.message


UNSUPPORTED_DRM = "UNSUPPORTED_DRM"
OCR_REQUIRED = "OCR_REQUIRED"
ENCODING_CONFIRMATION_REQUIRED = "ENCODING_CONFIRMATION_REQUIRED"
EXPORT_VALIDATION_FAILED = "EXPORT_VALIDATION_FAILED"
UNSAFE_ARCHIVE = "UNSAFE_ARCHIVE"
UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"
API_INCOMPATIBLE = "API_INCOMPATIBLE"
RATE_LIMITED = "RATE_LIMITED"
