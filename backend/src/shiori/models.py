from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(UTC)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class ProviderConfig(TimestampMixin, Base):
    __tablename__ = "provider_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    base_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    api_key_encrypted: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    api_key_tail: Mapped[str] = mapped_column(String(8), nullable=False)
    translation_model: Mapped[str] = mapped_column(String(255), nullable=False)
    review_model: Mapped[str] = mapped_column(String(255), nullable=False)
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    last_validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Upload(TimestampMixin, Base):
    __tablename__ = "uploads"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    purpose: Mapped[str] = mapped_column(String(16), default="book", nullable=False)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    media_type: Mapped[str] = mapped_column(String(255), nullable=False)
    expected_size: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    chunk_size: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="created", nullable=False)
    bytes_received: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    storage_path: Mapped[str | None] = mapped_column(String(2048))
    file_sha256: Mapped[str | None] = mapped_column(String(64))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    chunks: Mapped[list[UploadChunk]] = relationship(
        back_populates="upload", cascade="all, delete-orphan", order_by="UploadChunk.index"
    )
    book: Mapped[BookDocument | None] = relationship(back_populates="source_upload")


class UploadChunk(TimestampMixin, Base):
    __tablename__ = "upload_chunks"
    __table_args__ = (
        UniqueConstraint("upload_id", "index", name="uq_upload_chunk_index"),
        UniqueConstraint("upload_id", "idempotency_key", name="uq_upload_chunk_idempotency"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    upload_id: Mapped[str] = mapped_column(
        ForeignKey("uploads.id", ondelete="CASCADE"), nullable=False, index=True
    )
    index: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_path: Mapped[str] = mapped_column(String(2048), nullable=False)

    upload: Mapped[Upload] = relationship(back_populates="chunks")


class BookDocument(TimestampMixin, Base):
    __tablename__ = "book_documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_upload_id: Mapped[str | None] = mapped_column(
        ForeignKey("uploads.id", ondelete="SET NULL"), unique=True
    )
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    media_type: Mapped[str] = mapped_column(String(255), nullable=False)
    original_format: Mapped[str] = mapped_column(String(16), nullable=False)
    source_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    title: Mapped[str | None] = mapped_column(String(512))
    language: Mapped[str] = mapped_column(String(32), default="ja", nullable=False)
    parse_status: Mapped[str] = mapped_column(String(32), default="uploaded", nullable=False)
    document_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    source_upload: Mapped[Upload | None] = relationship(back_populates="book")
    sections: Mapped[list[Section]] = relationship(
        back_populates="book", cascade="all, delete-orphan", order_by="Section.ordinal"
    )
    projects: Mapped[list[TranslationProject]] = relationship(back_populates="book")


class Section(TimestampMixin, Base):
    __tablename__ = "sections"
    __table_args__ = (UniqueConstraint("book_id", "ordinal", name="uq_section_ordinal"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    book_id: Mapped[str] = mapped_column(
        ForeignKey("book_documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str | None] = mapped_column(String(512))
    locator: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    book: Mapped[BookDocument] = relationship(back_populates="sections")
    blocks: Mapped[list[Block]] = relationship(
        back_populates="section", cascade="all, delete-orphan", order_by="Block.ordinal"
    )


class Block(TimestampMixin, Base):
    __tablename__ = "blocks"
    __table_args__ = (
        UniqueConstraint("section_id", "ordinal", name="uq_block_ordinal"),
        Index("ix_blocks_stable_id", "stable_id", unique=True),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    section_id: Mapped[str] = mapped_column(
        ForeignKey("sections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    stable_id: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), default="paragraph", nullable=False)
    source_text: Mapped[str] = mapped_column(Text, nullable=False)
    block_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    section: Mapped[Section] = relationship(back_populates="blocks")
    revisions: Mapped[list[SegmentRevision]] = relationship(
        back_populates="block", cascade="all, delete-orphan"
    )


class ReferenceLibrary(TimestampMixin, Base):
    __tablename__ = "reference_libraries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    target_locale: Mapped[str] = mapped_column(String(16), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=20, nullable=False)
    rights_confirmed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    allows_external_snippets: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="created", nullable=False)
    profile: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    source_upload_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    pairings: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    alignment_review: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, nullable=False
    )


class TranslationProject(TimestampMixin, Base):
    __tablename__ = "translation_projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    book_id: Mapped[str] = mapped_column(
        ForeignKey("book_documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    target_locale: Mapped[str] = mapped_column(String(16), nullable=False)
    cover_policy: Mapped[str] = mapped_column(String(32), default="preserve", nullable=False)
    replacement_cover_upload_id: Mapped[str | None] = mapped_column(
        ForeignKey("uploads.id", ondelete="SET NULL")
    )
    selected_library_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    glossary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    translation_model: Mapped[str] = mapped_column(String(255), nullable=False)
    review_model: Mapped[str] = mapped_column(String(255), nullable=False)
    quality_mode: Mapped[str] = mapped_column(String(32), default="two_pass", nullable=False)
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    context_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False)

    book: Mapped[BookDocument] = relationship(back_populates="projects")
    jobs: Mapped[list[TranslationJob]] = relationship(back_populates="project")
    revisions: Mapped[list[SegmentRevision]] = relationship(back_populates="project")
    exports: Mapped[list[ExportArtifact]] = relationship(back_populates="project")


class TranslationJob(TimestampMixin, Base):
    __tablename__ = "translation_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("translation_projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(32), default="translate", nullable=False)
    state: Mapped[str] = mapped_column(String(32), default="queued", nullable=False, index=True)
    progress: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    current_stage: Mapped[str] = mapped_column(String(64), default="queued", nullable=False)
    checkpoint: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_detail: Mapped[str | None] = mapped_column(Text)
    lease_owner: Mapped[str | None] = mapped_column(String(128), index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    cancellation_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    project: Mapped[TranslationProject] = relationship(back_populates="jobs")


class SegmentRevision(TimestampMixin, Base):
    __tablename__ = "segment_revisions"
    __table_args__ = (
        UniqueConstraint("project_id", "block_id", "revision_no", name="uq_segment_revision_no"),
        Index("ix_segment_revision_lookup", "project_id", "block_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("translation_projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    block_id: Mapped[str] = mapped_column(
        ForeignKey("blocks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    revision_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    context_version: Mapped[int] = mapped_column(Integer, nullable=False)

    project: Mapped[TranslationProject] = relationship(back_populates="revisions")
    block: Mapped[Block] = relationship(back_populates="revisions")


class ExportArtifact(TimestampMixin, Base):
    __tablename__ = "export_artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("translation_projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    format: Mapped[str] = mapped_column(String(16), nullable=False)
    locale: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
    storage_path: Mapped[str | None] = mapped_column(String(2048))
    sha256: Mapped[str | None] = mapped_column(String(64))
    source_version: Mapped[int] = mapped_column(Integer, nullable=False)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    validation: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    project: Mapped[TranslationProject] = relationship(back_populates="exports")
