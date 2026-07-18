from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ProviderState(BaseModel):
    configured: bool
    base_url: str | None = None
    api_key_tail: str | None = None
    translation_model: str | None = None
    review_model: str | None = None
    capabilities: dict[str, Any] = Field(default_factory=dict)
    last_validated_at: datetime | None = None


class ProviderUpdate(BaseModel):
    base_url: str = Field(min_length=8, max_length=2048)
    api_key: SecretStr | None = None
    translation_model: str = Field(min_length=1, max_length=255)
    review_model: str = Field(min_length=1, max_length=255)


class UploadCreate(BaseModel):
    purpose: Literal["book", "cover"] = "book"
    filename: str = Field(min_length=1, max_length=512)
    media_type: str = Field(min_length=1, max_length=255)
    size: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")

    @field_validator("filename")
    @classmethod
    def safe_filename(cls, value: str) -> str:
        if "\x00" in value or "/" in value or "\\" in value or value in {".", ".."}:
            raise ValueError("filename must not contain path separators")
        return value


class UploadView(ORMModel):
    id: str
    purpose: str
    filename: str
    media_type: str
    expected_size: int
    expected_sha256: str
    chunk_size: int
    status: str
    bytes_received: int
    file_sha256: str | None
    created_at: datetime
    completed_at: datetime | None


class UploadCompleteView(BaseModel):
    upload: UploadView
    book_id: str | None


class LibraryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    mode: Literal["paired", "style_only"]
    target_locale: Literal["zh-CN", "zh-TW"]
    priority: int = Field(default=20, ge=0, le=1000)
    rights_confirmed: bool
    allows_external_snippets: bool = False
    notes: str | None = Field(default=None, max_length=4000)
    source_upload_ids: list[str] = Field(default_factory=list, max_length=1000)
    pairings: list[dict[str, Any]] = Field(default_factory=list, max_length=1000)


class LibraryPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    priority: int | None = Field(default=None, ge=0, le=1000)
    allows_external_snippets: bool | None = None
    notes: str | None = Field(default=None, max_length=4000)
    source_upload_ids: list[str] | None = Field(default=None, max_length=1000)
    pairings: list[dict[str, Any]] | None = Field(default=None, max_length=1000)
    alignment_review: list[dict[str, Any]] | None = Field(default=None, max_length=5000)
    status: Literal["created", "building", "awaiting_review", "ready", "failed"] | None = None


class LibraryView(ORMModel):
    id: str
    name: str
    mode: str
    target_locale: str
    priority: int
    rights_confirmed: bool
    allows_external_snippets: bool
    confirmed_at: datetime | None
    notes: str | None
    status: str
    profile: dict[str, Any]
    source_upload_ids: list[str]
    pairings: list[dict[str, Any]]
    alignment_review: list[dict[str, Any]]
    created_at: datetime
    updated_at: datetime


class ProjectCreate(BaseModel):
    book_id: str
    name: str = Field(min_length=1, max_length=512)
    target_locale: Literal["zh-CN", "zh-TW"]
    cover_policy: Literal["preserve", "replace", "none"] = "preserve"
    replacement_cover_upload_id: str | None = None
    selected_library_ids: list[str] = Field(default_factory=list, max_length=100)
    glossary: dict[str, Any] = Field(default_factory=dict)
    translation_model: str | None = Field(default=None, max_length=255)
    review_model: str | None = Field(default=None, max_length=255)
    quality_mode: Literal["two_pass", "translation_only"] = "two_pass"


class ProjectPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=512)
    selected_library_ids: list[str] | None = Field(default=None, max_length=100)
    glossary: dict[str, Any] | None = None
    cover_policy: Literal["preserve", "replace", "none"] | None = None
    replacement_cover_upload_id: str | None = None


class ProjectView(ORMModel):
    id: str
    book_id: str
    name: str
    target_locale: str
    cover_policy: str
    replacement_cover_upload_id: str | None
    selected_library_ids: list[str]
    glossary: dict[str, Any]
    translation_model: str
    review_model: str
    quality_mode: str
    context_version: int
    status: str
    created_at: datetime
    updated_at: datetime


class JobCreate(BaseModel):
    project_id: str
    kind: Literal["translate", "retranslate", "export"] = "translate"
    block_id: str | None = None


class JobView(ORMModel):
    id: str
    project_id: str
    kind: str
    state: str
    progress: float
    current_stage: str
    checkpoint: dict[str, Any]
    error_code: str | None
    error_detail: str | None
    cancellation_requested: bool
    attempts: int
    created_at: datetime
    updated_at: datetime


class SegmentEdit(BaseModel):
    project_id: str
    text: str = Field(max_length=100_000)
    locked: bool = True


class SegmentCandidateView(BaseModel):
    id: str
    text: str
    kind: str
    source: str
    revision_no: int
    context_version: int


class SegmentView(BaseModel):
    block_id: str
    stable_id: str
    section_id: str
    section_title: str | None
    kind: str
    source_text: str
    translation: str | None
    revision_kind: str | None
    locked: bool
    context_version: int | None
    candidates: list[SegmentCandidateView] = Field(default_factory=list)


class SegmentPage(BaseModel):
    items: list[SegmentView]
    page: int
    page_size: int
    total: int


class RetranslateRequest(BaseModel):
    project_id: str


class ExportCreate(BaseModel):
    project_id: str
    format: Literal["epub", "txt", "pdf"]
    parameters: dict[str, Any] = Field(default_factory=dict)


class ExportView(ORMModel):
    id: str
    project_id: str
    format: str
    locale: str
    status: str
    sha256: str | None
    source_version: int
    parameters: dict[str, Any]
    validation: dict[str, Any]
    created_at: datetime
    updated_at: datetime
