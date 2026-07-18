from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request, status
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .config import Settings
from .db import check_database
from .errors import ErrorCode, ShioriError
from .models import (
    Block,
    BookDocument,
    ExportArtifact,
    ProviderConfig,
    ReferenceLibrary,
    Section,
    SegmentRevision,
    TranslationJob,
    TranslationProject,
    Upload,
    UploadChunk,
    utcnow,
)
from .provider import probe_provider
from .schemas import (
    ExportCreate,
    ExportView,
    JobCreate,
    JobView,
    LibraryCreate,
    LibraryPatch,
    LibraryView,
    ProjectCreate,
    ProjectPatch,
    ProjectView,
    ProviderState,
    ProviderUpdate,
    RetranslateRequest,
    SegmentEdit,
    SegmentPage,
    UploadCompleteView,
    UploadCreate,
    UploadView,
)
from .security import APIKeyCipher, CSRFManager, Principal
from .ssrf import validate_public_https_base_url

router = APIRouter(prefix="/api")

COVER_MEDIA_BY_EXTENSION = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}
BOOK_MEDIA_BY_EXTENSION = {
    ".epub": "application/epub+zip",
    ".txt": "text/plain",
    ".pdf": "application/pdf",
}


def get_db(request: Request) -> Generator[Session, None, None]:
    with request.app.state.session_factory() as session:
        yield session


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_cipher(request: Request) -> APIKeyCipher:
    try:
        return request.app.state.key_cipher
    except AttributeError as exc:
        raise ShioriError(
            ErrorCode.PROVIDER_NOT_CONFIGURED,
            "Provider encryption key is not configured",
            status_code=503,
        ) from exc


DB = Annotated[Session, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]
Cipher = Annotated[APIKeyCipher, Depends(get_cipher)]


def _get_or_404(db: Session, model: type[Any], object_id: str, label: str) -> Any:
    instance = db.get(model, object_id)
    if instance is None:
        raise ShioriError(ErrorCode.NOT_FOUND, f"{label} was not found", status_code=404)
    return instance


def _provider_state(config: ProviderConfig | None) -> ProviderState:
    if config is None:
        return ProviderState(configured=False)
    return ProviderState(
        configured=True,
        base_url=config.base_url,
        api_key_tail=config.api_key_tail,
        translation_model=config.translation_model,
        review_model=config.review_model,
        capabilities=config.capabilities,
        last_validated_at=config.last_validated_at,
    )


@router.get("/session")
def session_info(request: Request, db: DB) -> dict[str, Any]:
    principal: Principal = request.state.principal
    csrf: CSRFManager = request.app.state.csrf
    provider = db.get(ProviderConfig, 1)
    return {
        "owner": {
            "email": principal.email,
            "identity_provider": principal.identity_provider,
        },
        "csrf_token": csrf.issue(principal),
        "service": {
            "status": "ready",
            "database": "ready" if check_database(request.app.state.engine) else "unavailable",
            "provider_configured": provider is not None,
        },
    }


@router.get("/settings/provider", response_model=ProviderState)
def read_provider(db: DB) -> ProviderState:
    return _provider_state(db.get(ProviderConfig, 1))


@router.put("/settings/provider", response_model=ProviderState)
async def update_provider(payload: ProviderUpdate, db: DB, cipher: Cipher) -> ProviderState:
    validated = await validate_public_https_base_url(payload.base_url)
    config = db.get(ProviderConfig, 1)
    supplied_key = payload.api_key.get_secret_value().strip() if payload.api_key else ""
    if config is None and not supplied_key:
        raise ShioriError(
            ErrorCode.VALIDATION_ERROR,
            "An API key is required for the initial provider configuration",
            status_code=422,
        )
    if supplied_key and len(supplied_key) < 8:
        raise ShioriError(ErrorCode.VALIDATION_ERROR, "API key is too short", status_code=422)
    if config is None:
        config = ProviderConfig(
            id=1,
            base_url=validated.base_url,
            api_key_encrypted=cipher.encrypt(supplied_key),
            api_key_tail=supplied_key[-4:],
            translation_model=payload.translation_model,
            review_model=payload.review_model,
        )
        db.add(config)
    else:
        config.base_url = validated.base_url
        config.translation_model = payload.translation_model
        config.review_model = payload.review_model
        if supplied_key:
            config.api_key_encrypted = cipher.encrypt(supplied_key)
            config.api_key_tail = supplied_key[-4:]
        config.capabilities = {}
        config.last_validated_at = None
    db.commit()
    db.refresh(config)
    return _provider_state(config)


@router.post("/settings/provider/test", response_model=ProviderState)
async def test_provider(
    db: DB,
    settings: AppSettings,
    cipher: Cipher,
) -> ProviderState:
    config = db.get(ProviderConfig, 1)
    if config is None:
        raise ShioriError(
            ErrorCode.PROVIDER_NOT_CONFIGURED,
            "Provider is not configured",
            status_code=409,
        )
    capabilities = await probe_provider(
        base_url=config.base_url,
        api_key=cipher.decrypt(config.api_key_encrypted),
        translation_model=config.translation_model,
        timeout_seconds=settings.provider_timeout_seconds,
    )
    config.capabilities = capabilities
    config.last_validated_at = datetime.now(UTC)
    db.commit()
    db.refresh(config)
    return _provider_state(config)


@router.post("/uploads", response_model=UploadView, status_code=status.HTTP_201_CREATED)
def create_upload(payload: UploadCreate, db: DB, settings: AppSettings) -> Upload:
    if payload.size > settings.upload_max_bytes:
        raise ShioriError(
            ErrorCode.UPLOAD_TOO_LARGE,
            f"Upload exceeds the {settings.upload_max_bytes}-byte limit",
            status_code=413,
        )
    extension = Path(payload.filename).suffix.lower()
    if payload.purpose == "cover":
        expected_media_type = COVER_MEDIA_BY_EXTENSION.get(extension)
        if expected_media_type is None or payload.media_type.lower() != expected_media_type:
            raise ShioriError(
                ErrorCode.INVALID_COVER,
                "Replacement covers must be matching JPEG or PNG files",
                status_code=422,
            )
    else:
        expected_media_type = BOOK_MEDIA_BY_EXTENSION.get(extension)
        if expected_media_type is None:
            raise ShioriError(
                ErrorCode.VALIDATION_ERROR,
                "Only EPUB, TXT, and PDF are supported",
                status_code=422,
            )
        if payload.media_type.lower() != expected_media_type:
            raise ShioriError(
                ErrorCode.VALIDATION_ERROR,
                "Book media type does not match its filename extension",
                status_code=422,
            )
    upload = Upload(
        purpose=payload.purpose,
        filename=payload.filename,
        media_type=payload.media_type.lower(),
        expected_size=payload.size,
        expected_sha256=payload.sha256.lower(),
        chunk_size=settings.upload_chunk_size,
    )
    db.add(upload)
    db.commit()
    db.refresh(upload)
    return upload


@router.get("/uploads", response_model=list[UploadView])
def list_uploads(db: DB) -> list[Upload]:
    return list(db.scalars(select(Upload).order_by(Upload.created_at.desc())).all())


@router.get("/uploads/{upload_id}", response_model=UploadView)
def read_upload(upload_id: str, db: DB) -> Upload:
    return _get_or_404(db, Upload, upload_id, "Upload")


@router.put("/uploads/{upload_id}/chunks/{chunk_index}")
async def write_upload_chunk(
    upload_id: str,
    chunk_index: int,
    request: Request,
    db: DB,
    settings: AppSettings,
    chunk_sha256: Annotated[str, Header(alias="X-Chunk-Sha256", pattern=r"^[0-9a-fA-F]{64}$")],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)],
) -> dict[str, Any]:
    upload: Upload = _get_or_404(db, Upload, upload_id, "Upload")
    if upload.status == "completed":
        existing = db.scalar(
            select(UploadChunk).where(
                UploadChunk.upload_id == upload.id, UploadChunk.index == chunk_index
            )
        )
        if existing and existing.sha256 == chunk_sha256.lower():
            return {"accepted": True, "idempotent": True, "bytes_received": upload.bytes_received}
        raise ShioriError(ErrorCode.CONFLICT, "Upload is already complete", status_code=409)
    expected_chunks = math.ceil(upload.expected_size / upload.chunk_size)
    if chunk_index < 0 or chunk_index >= expected_chunks:
        raise ShioriError(
            ErrorCode.VALIDATION_ERROR, "Chunk index is outside the upload", status_code=422
        )

    existing = db.scalar(
        select(UploadChunk).where(
            UploadChunk.upload_id == upload.id,
            or_(UploadChunk.index == chunk_index, UploadChunk.idempotency_key == idempotency_key),
        )
    )
    if existing is not None:
        if (
            existing.index == chunk_index
            and existing.idempotency_key == idempotency_key
            and existing.sha256 == chunk_sha256.lower()
        ):
            return {"accepted": True, "idempotent": True, "bytes_received": upload.bytes_received}
        raise ShioriError(
            ErrorCode.CONFLICT, "Chunk identity conflicts with existing data", status_code=409
        )

    chunk_dir = settings.storage_root / "uploads" / "chunks" / upload.id
    chunk_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = chunk_dir / f"{chunk_index:08d}.partial"
    final = chunk_dir / f"{chunk_index:08d}.chunk"
    digest = hashlib.sha256()
    byte_count = 0
    try:
        with temporary.open("wb") as output:
            async for piece in request.stream():
                byte_count += len(piece)
                if byte_count > upload.chunk_size:
                    raise ShioriError(
                        ErrorCode.UPLOAD_TOO_LARGE, "Chunk exceeds configured size", status_code=413
                    )
                digest.update(piece)
                output.write(piece)
        expected_size = min(
            upload.chunk_size, upload.expected_size - chunk_index * upload.chunk_size
        )
        if byte_count != expected_size:
            raise ShioriError(
                ErrorCode.VALIDATION_ERROR,
                "Chunk size does not match its expected range",
                status_code=422,
                details={"expected": expected_size, "received": byte_count},
            )
        if digest.hexdigest() != chunk_sha256.lower():
            raise ShioriError(
                ErrorCode.HASH_MISMATCH, "Chunk SHA-256 does not match", status_code=422
            )
        os.replace(temporary, final)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise

    chunk = UploadChunk(
        upload_id=upload.id,
        index=chunk_index,
        idempotency_key=idempotency_key,
        sha256=digest.hexdigest(),
        size=byte_count,
        storage_path=str(final),
    )
    db.add(chunk)
    db.flush()
    upload.status = "uploading"
    upload.bytes_received = int(
        db.scalar(
            select(func.coalesce(func.sum(UploadChunk.size), 0)).where(
                UploadChunk.upload_id == upload.id
            )
        )
        or 0
    )
    db.commit()
    return {"accepted": True, "idempotent": False, "bytes_received": upload.bytes_received}


@router.post("/uploads/{upload_id}/complete", response_model=UploadCompleteView)
def complete_upload(upload_id: str, db: DB, settings: AppSettings) -> dict[str, Any]:
    upload: Upload = _get_or_404(db, Upload, upload_id, "Upload")
    if upload.status == "completed":
        if upload.purpose == "cover":
            return {"upload": upload, "book_id": None}
        if upload.book is not None:
            return {"upload": upload, "book_id": upload.book.id}
    chunks = list(
        db.scalars(
            select(UploadChunk)
            .where(UploadChunk.upload_id == upload.id)
            .order_by(UploadChunk.index)
        ).all()
    )
    expected_count = math.ceil(upload.expected_size / upload.chunk_size)
    if len(chunks) != expected_count or [chunk.index for chunk in chunks] != list(
        range(expected_count)
    ):
        raise ShioriError(
            ErrorCode.UPLOAD_INCOMPLETE,
            "Not all upload chunks are present",
            status_code=409,
            details={"expected_chunks": expected_count, "received_chunks": len(chunks)},
        )

    complete_dir = settings.storage_root / "uploads" / "complete"
    complete_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    extension = Path(upload.filename).suffix.lower()
    temporary = complete_dir / f"{upload.id}.partial"
    final = complete_dir / f"{upload.id}{extension}"
    digest = hashlib.sha256()
    byte_count = 0
    try:
        with temporary.open("wb") as output:
            for chunk in chunks:
                chunk_path = Path(chunk.storage_path)
                with chunk_path.open("rb") as source:
                    while piece := source.read(1024 * 1024):
                        digest.update(piece)
                        byte_count += len(piece)
                        output.write(piece)
        if byte_count != upload.expected_size or digest.hexdigest() != upload.expected_sha256:
            raise ShioriError(
                ErrorCode.HASH_MISMATCH,
                "Completed upload does not match the declared file",
                status_code=422,
            )
        if upload.purpose == "cover":
            with temporary.open("rb") as cover_file:
                signature = cover_file.read(16)
            valid_signature = (
                upload.media_type == "image/jpeg" and signature.startswith(b"\xff\xd8\xff")
            ) or (upload.media_type == "image/png" and signature.startswith(b"\x89PNG\r\n\x1a\n"))
            if not valid_signature:
                raise ShioriError(
                    ErrorCode.INVALID_COVER,
                    "Replacement cover content does not match its declared image type",
                    status_code=422,
                )
        os.replace(temporary, final)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise

    upload.status = "completed"
    upload.storage_path = str(final)
    upload.file_sha256 = digest.hexdigest()
    upload.bytes_received = byte_count
    upload.completed_at = utcnow()
    if upload.purpose == "cover":
        db.commit()
        db.refresh(upload)
        return {"upload": upload, "book_id": None}
    book = BookDocument(
        source_upload_id=upload.id,
        source_hash=digest.hexdigest(),
        filename=upload.filename,
        media_type=upload.media_type,
        original_format=extension.lstrip("."),
        title=Path(upload.filename).stem,
        parse_status="uploaded",
    )
    db.add(book)
    db.commit()
    db.refresh(upload)
    db.refresh(book)
    return {"upload": upload, "book_id": book.id}


@router.get("/books")
def list_books(db: DB) -> list[dict[str, Any]]:
    books = db.scalars(select(BookDocument).order_by(BookDocument.created_at.desc())).all()
    return [
        {
            "id": book.id,
            "title": book.title,
            "filename": book.filename,
            "format": book.original_format,
            "language": book.language,
            "parse_status": book.parse_status,
            "source_version": book.source_version,
            "created_at": book.created_at,
        }
        for book in books
    ]


@router.get("/books/{book_id}")
def read_book(book_id: str, db: DB) -> dict[str, Any]:
    book: BookDocument = _get_or_404(db, BookDocument, book_id, "Book")
    return {
        "id": book.id,
        "title": book.title,
        "filename": book.filename,
        "format": book.original_format,
        "language": book.language,
        "parse_status": book.parse_status,
        "source_version": book.source_version,
        "metadata": book.document_metadata,
        "section_count": db.scalar(
            select(func.count()).select_from(Section).where(Section.book_id == book.id)
        ),
        "created_at": book.created_at,
        "updated_at": book.updated_at,
    }


@router.post("/libraries", response_model=LibraryView, status_code=status.HTTP_201_CREATED)
def create_library(payload: LibraryCreate, db: DB) -> ReferenceLibrary:
    if payload.source_upload_ids:
        found = set(
            db.scalars(select(Upload.id).where(Upload.id.in_(payload.source_upload_ids))).all()
        )
        missing = set(payload.source_upload_ids) - found
        if missing:
            raise ShioriError(
                ErrorCode.NOT_FOUND, "One or more source uploads were not found", status_code=404
            )
    library = ReferenceLibrary(
        **payload.model_dump(),
        confirmed_at=utcnow() if payload.rights_confirmed else None,
    )
    db.add(library)
    db.commit()
    db.refresh(library)
    return library


@router.get("/libraries", response_model=list[LibraryView])
def list_libraries(db: DB) -> list[ReferenceLibrary]:
    return list(
        db.scalars(
            select(ReferenceLibrary).order_by(
                ReferenceLibrary.priority.desc(), ReferenceLibrary.created_at
            )
        ).all()
    )


@router.get("/libraries/{library_id}", response_model=LibraryView)
def read_library(library_id: str, db: DB) -> ReferenceLibrary:
    return _get_or_404(db, ReferenceLibrary, library_id, "Library")


@router.patch("/libraries/{library_id}", response_model=LibraryView)
def patch_library(library_id: str, payload: LibraryPatch, db: DB) -> ReferenceLibrary:
    library: ReferenceLibrary = _get_or_404(db, ReferenceLibrary, library_id, "Library")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(library, field, value)
    db.commit()
    db.refresh(library)
    return library


@router.post("/libraries/{library_id}/build-profile", response_model=LibraryView, status_code=202)
def build_library_profile(library_id: str, db: DB) -> ReferenceLibrary:
    library: ReferenceLibrary = _get_or_404(db, ReferenceLibrary, library_id, "Library")
    if not library.rights_confirmed:
        raise ShioriError(
            ErrorCode.INVALID_STATE,
            "Processing rights must be confirmed before building a library",
            status_code=409,
        )
    library.status = "building"
    db.commit()
    db.refresh(library)
    return library


def _validate_library_selection(db: Session, ids: list[str], locale: str) -> list[ReferenceLibrary]:
    if not ids:
        return []
    libraries = list(db.scalars(select(ReferenceLibrary).where(ReferenceLibrary.id.in_(ids))).all())
    if len(libraries) != len(set(ids)):
        raise ShioriError(
            ErrorCode.NOT_FOUND, "One or more libraries were not found", status_code=404
        )
    incompatible = [library.id for library in libraries if library.target_locale != locale]
    if incompatible:
        raise ShioriError(
            ErrorCode.VALIDATION_ERROR,
            "Library locale does not match project locale",
            status_code=422,
            details={"library_ids": incompatible},
        )
    return libraries


def _validate_replacement_cover(db: Session, upload_id: str | None) -> Upload:
    if not upload_id:
        raise ShioriError(
            ErrorCode.INVALID_COVER,
            "A completed replacement cover upload is required",
            status_code=422,
        )
    upload: Upload = _get_or_404(db, Upload, upload_id, "Cover upload")
    if upload.purpose != "cover":
        raise ShioriError(
            ErrorCode.INVALID_COVER,
            "The selected upload is not a replacement cover",
            status_code=422,
        )
    if upload.status != "completed" or not upload.storage_path:
        raise ShioriError(
            ErrorCode.INVALID_STATE,
            "Replacement cover upload is not complete",
            status_code=409,
        )
    return upload


@router.post("/projects", response_model=ProjectView, status_code=status.HTTP_201_CREATED)
def create_project(payload: ProjectCreate, db: DB) -> TranslationProject:
    book: BookDocument = _get_or_404(db, BookDocument, payload.book_id, "Book")
    libraries = _validate_library_selection(db, payload.selected_library_ids, payload.target_locale)
    provider = db.get(ProviderConfig, 1)
    translation_model = payload.translation_model or (
        provider.translation_model if provider else "pending"
    )
    review_model = payload.review_model or (provider.review_model if provider else "pending")
    if payload.cover_policy == "replace":
        _validate_replacement_cover(db, payload.replacement_cover_upload_id)
    elif payload.replacement_cover_upload_id is not None:
        raise ShioriError(
            ErrorCode.INVALID_COVER,
            "A replacement cover can only be selected with cover_policy=replace",
            status_code=422,
        )
    project = TranslationProject(
        book_id=book.id,
        name=payload.name,
        target_locale=payload.target_locale,
        cover_policy=payload.cover_policy,
        replacement_cover_upload_id=payload.replacement_cover_upload_id,
        selected_library_ids=payload.selected_library_ids,
        glossary=payload.glossary,
        translation_model=translation_model,
        review_model=review_model,
        quality_mode=payload.quality_mode,
        config_snapshot={
            "book_hash": book.source_hash,
            "book_source_version": book.source_version,
            "target_locale": payload.target_locale,
            "cover_policy": payload.cover_policy,
            "replacement_cover_upload_id": payload.replacement_cover_upload_id,
            "library_versions": [
                {
                    "id": item.id,
                    "updated_at": item.updated_at.isoformat(),
                    "priority": item.priority,
                }
                for item in libraries
            ],
            "translation_model": translation_model,
            "review_model": review_model,
            "quality_mode": payload.quality_mode,
        },
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


@router.get("/projects", response_model=list[ProjectView])
def list_projects(db: DB) -> list[TranslationProject]:
    return list(
        db.scalars(select(TranslationProject).order_by(TranslationProject.updated_at.desc())).all()
    )


@router.get("/projects/{project_id}", response_model=ProjectView)
def read_project(project_id: str, db: DB) -> TranslationProject:
    return _get_or_404(db, TranslationProject, project_id, "Project")


@router.patch("/projects/{project_id}", response_model=ProjectView)
def patch_project(project_id: str, payload: ProjectPatch, db: DB) -> TranslationProject:
    project: TranslationProject = _get_or_404(db, TranslationProject, project_id, "Project")
    values = payload.model_dump(exclude_unset=True)
    if "selected_library_ids" in values:
        _validate_library_selection(db, values["selected_library_ids"], project.target_locale)
    resulting_cover_policy = values.get("cover_policy", project.cover_policy)
    resulting_cover_upload_id = values.get(
        "replacement_cover_upload_id", project.replacement_cover_upload_id
    )
    if resulting_cover_policy == "replace":
        _validate_replacement_cover(db, resulting_cover_upload_id)
    elif "replacement_cover_upload_id" in values and resulting_cover_upload_id is not None:
        raise ShioriError(
            ErrorCode.INVALID_COVER,
            "A replacement cover can only be selected with cover_policy=replace",
            status_code=422,
        )
    elif resulting_cover_policy != "replace":
        values["replacement_cover_upload_id"] = None
    context_changed = any(field in values for field in ("selected_library_ids", "glossary"))
    for field, value in values.items():
        setattr(project, field, value)
    if context_changed:
        project.context_version += 1
        project.status = "context_changed"
    db.commit()
    db.refresh(project)
    return project


ACTIVE_JOB_STATES = {
    "queued",
    "validating",
    "parsing",
    "awaiting_review",
    "translating",
    "reviewing",
    "assembling",
    "validating_output",
    "paused",
}
TERMINAL_JOB_STATES = {"completed", "failed", "cancelled"}


def _create_job(
    db: Session, project: TranslationProject, kind: str, block_id: str | None = None
) -> TranslationJob:
    if kind in {"translate", "retranslate"}:
        provider = db.get(ProviderConfig, 1)
        if (
            provider is None
            or project.translation_model == "pending"
            or project.review_model == "pending"
        ):
            raise ShioriError(
                ErrorCode.PROVIDER_NOT_CONFIGURED,
                "Configure the provider and project models before starting translation",
                status_code=409,
            )
    existing = db.scalar(
        select(TranslationJob).where(
            TranslationJob.project_id == project.id,
            TranslationJob.kind == kind,
            TranslationJob.state.in_(ACTIVE_JOB_STATES - {"paused"}),
        )
    )
    if existing is not None:
        raise ShioriError(
            ErrorCode.CONFLICT, "An active job of this type already exists", status_code=409
        )
    checkpoint: dict[str, Any] = {"context_version": project.context_version}
    if block_id:
        checkpoint["block_id"] = block_id
    job = TranslationJob(project_id=project.id, kind=kind, checkpoint=checkpoint)
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


@router.post("/jobs", response_model=JobView, status_code=status.HTTP_201_CREATED)
def create_job(payload: JobCreate, db: DB) -> TranslationJob:
    project: TranslationProject = _get_or_404(db, TranslationProject, payload.project_id, "Project")
    return _create_job(db, project, payload.kind, payload.block_id)


@router.get("/jobs", response_model=list[JobView])
def list_jobs(db: DB, project_id: str | None = None) -> list[TranslationJob]:
    statement = select(TranslationJob).order_by(TranslationJob.created_at.desc())
    if project_id:
        statement = statement.where(TranslationJob.project_id == project_id)
    return list(db.scalars(statement).all())


@router.get("/jobs/{job_id}", response_model=JobView)
def read_job(job_id: str, db: DB) -> TranslationJob:
    return _get_or_404(db, TranslationJob, job_id, "Job")


def _job_action(db: Session, job_id: str, action: str) -> TranslationJob:
    job: TranslationJob = _get_or_404(db, TranslationJob, job_id, "Job")
    if action == "pause":
        if job.state not in ACTIVE_JOB_STATES or job.state == "paused":
            raise ShioriError(
                ErrorCode.INVALID_STATE, "Only active jobs can be paused", status_code=409
            )
        job.state = "paused"
        job.current_stage = "paused"
        job.lease_owner = None
        job.lease_expires_at = None
    elif action == "resume":
        if job.state not in {"paused", "awaiting_review"}:
            raise ShioriError(
                ErrorCode.INVALID_STATE,
                "Only paused or awaiting-review jobs can be resumed",
                status_code=409,
            )
        job.state = "queued"
        job.current_stage = "queued"
        job.lease_owner = None
        job.lease_expires_at = None
    elif action == "cancel":
        if job.state in TERMINAL_JOB_STATES:
            raise ShioriError(
                ErrorCode.INVALID_STATE, "Terminal jobs cannot be cancelled", status_code=409
            )
        if job.lease_owner:
            job.cancellation_requested = True
        else:
            job.state = "cancelled"
            job.current_stage = "cancelled"
    elif action == "retry":
        if job.state != "failed":
            raise ShioriError(
                ErrorCode.INVALID_STATE, "Only failed jobs can be retried", status_code=409
            )
        job.state = "queued"
        job.current_stage = "queued"
        job.error_code = None
        job.error_detail = None
        job.cancellation_requested = False
        job.lease_owner = None
        job.lease_expires_at = None
    else:
        raise AssertionError(action)
    db.commit()
    db.refresh(job)
    return job


@router.post("/jobs/{job_id}/pause", response_model=JobView)
def pause_job(job_id: str, db: DB) -> TranslationJob:
    return _job_action(db, job_id, "pause")


@router.post("/jobs/{job_id}/resume", response_model=JobView)
def resume_job(job_id: str, db: DB) -> TranslationJob:
    return _job_action(db, job_id, "resume")


@router.post("/jobs/{job_id}/cancel", response_model=JobView)
def cancel_job(job_id: str, db: DB) -> TranslationJob:
    return _job_action(db, job_id, "cancel")


@router.post("/jobs/{job_id}/retry", response_model=JobView)
def retry_job(job_id: str, db: DB) -> TranslationJob:
    return _job_action(db, job_id, "retry")


@router.get("/jobs/{job_id}/events")
def job_events(job_id: str, request: Request) -> StreamingResponse:
    factory = request.app.state.session_factory

    async def stream() -> Any:
        last_payload = ""
        while True:
            if await request.is_disconnected():
                return
            with factory() as session:
                job = session.get(TranslationJob, job_id)
                if job is None:
                    payload = json.dumps(
                        {"error": {"code": ErrorCode.NOT_FOUND}}, separators=(",", ":")
                    )
                    yield f"event: error\ndata: {payload}\n\n"
                    return
                payload = JobView.model_validate(job).model_dump_json()
                terminal = job.state in TERMINAL_JOB_STATES
            if payload != last_payload:
                yield f"event: job\ndata: {payload}\n\n"
                last_payload = payload
            else:
                yield ": keep-alive\n\n"
            if terminal:
                return
            await asyncio.sleep(2)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


def _project_for_book(db: Session, project_id: str, book_id: str) -> TranslationProject:
    project: TranslationProject = _get_or_404(db, TranslationProject, project_id, "Project")
    if project.book_id != book_id:
        raise ShioriError(
            ErrorCode.NOT_FOUND, "Project does not belong to this book", status_code=404
        )
    return project


@router.get("/books/{book_id}/segments", response_model=SegmentPage)
def list_segments(
    book_id: str,
    db: DB,
    project_id: str,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    _get_or_404(db, BookDocument, book_id, "Book")
    _project_for_book(db, project_id, book_id)
    total = int(
        db.scalar(
            select(func.count())
            .select_from(Block)
            .join(Section, Block.section_id == Section.id)
            .where(Section.book_id == book_id)
        )
        or 0
    )
    rows = db.execute(
        select(Block, Section)
        .join(Section, Block.section_id == Section.id)
        .where(Section.book_id == book_id)
        .order_by(Section.ordinal, Block.ordinal)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    items: list[dict[str, Any]] = []
    for block, section in rows:
        revisions = list(
            db.scalars(
                select(SegmentRevision)
                .where(
                    SegmentRevision.project_id == project_id, SegmentRevision.block_id == block.id
                )
                .order_by(SegmentRevision.revision_no.desc())
            ).all()
        )
        human_revision = next(
            (item for item in revisions if item.revision_kind == "human" and item.locked),
            None,
        ) or next((item for item in revisions if item.revision_kind == "human"), None)
        current_revision = human_revision or next(
            (item for item in revisions if item.revision_kind != "model_candidate"),
            None,
        )
        candidates = [
            {
                "id": item.id,
                "text": item.text,
                "kind": item.revision_kind,
                "source": item.source,
                "revision_no": item.revision_no,
                "context_version": item.context_version,
            }
            for item in revisions
            if item.revision_kind == "model_candidate"
        ]
        items.append(
            {
                "block_id": block.id,
                "stable_id": block.stable_id,
                "section_id": section.id,
                "section_title": section.title,
                "kind": block.kind,
                "source_text": block.source_text,
                "translation": current_revision.text if current_revision else None,
                "revision_kind": current_revision.revision_kind if current_revision else None,
                "locked": current_revision.locked if current_revision else False,
                "context_version": current_revision.context_version if current_revision else None,
                "candidates": candidates,
            }
        )
    return {"items": items, "page": page, "page_size": page_size, "total": total}


def _find_block(db: Session, book_id: str, identifier: str) -> Block:
    block = db.scalar(
        select(Block)
        .join(Section, Block.section_id == Section.id)
        .where(
            Section.book_id == book_id, or_(Block.id == identifier, Block.stable_id == identifier)
        )
    )
    if block is None:
        raise ShioriError(ErrorCode.NOT_FOUND, "Segment was not found", status_code=404)
    return block


@router.patch("/books/{book_id}/segments/{segment_id}")
def edit_segment(book_id: str, segment_id: str, payload: SegmentEdit, db: DB) -> dict[str, Any]:
    project = _project_for_book(db, payload.project_id, book_id)
    block = _find_block(db, book_id, segment_id)
    revision_no = (
        int(
            db.scalar(
                select(func.coalesce(func.max(SegmentRevision.revision_no), 0)).where(
                    SegmentRevision.project_id == project.id, SegmentRevision.block_id == block.id
                )
            )
            or 0
        )
        + 1
    )
    revision = SegmentRevision(
        project_id=project.id,
        block_id=block.id,
        revision_no=revision_no,
        revision_kind="human",
        text=payload.text,
        locked=payload.locked,
        source="owner",
        context_version=project.context_version,
    )
    db.add(revision)
    db.commit()
    db.refresh(revision)
    return {
        "id": revision.id,
        "block_id": block.id,
        "revision_no": revision.revision_no,
        "text": revision.text,
        "locked": revision.locked,
        "context_version": revision.context_version,
    }


@router.post(
    "/books/{book_id}/segments/{segment_id}/retranslate", response_model=JobView, status_code=202
)
def retranslate_segment(
    book_id: str,
    segment_id: str,
    payload: RetranslateRequest,
    db: DB,
) -> TranslationJob:
    project = _project_for_book(db, payload.project_id, book_id)
    block = _find_block(db, book_id, segment_id)
    return _create_job(db, project, "retranslate", block.id)


@router.post("/exports", response_model=ExportView, status_code=status.HTTP_201_CREATED)
def create_export(payload: ExportCreate, db: DB) -> ExportArtifact:
    project: TranslationProject = _get_or_404(db, TranslationProject, payload.project_id, "Project")
    artifact = ExportArtifact(
        project_id=project.id,
        format=payload.format,
        locale=project.target_locale,
        source_version=project.book.source_version,
        parameters=payload.parameters,
    )
    db.add(artifact)
    db.flush()
    job = TranslationJob(
        project_id=project.id,
        kind="export",
        checkpoint={"export_id": artifact.id, "context_version": project.context_version},
    )
    db.add(job)
    db.commit()
    db.refresh(artifact)
    return artifact


@router.get("/exports", response_model=list[ExportView])
def list_exports(db: DB, project_id: str | None = None) -> list[ExportArtifact]:
    statement = select(ExportArtifact).order_by(ExportArtifact.created_at.desc())
    if project_id:
        statement = statement.where(ExportArtifact.project_id == project_id)
    return list(db.scalars(statement).all())


@router.get("/exports/{export_id}/download", response_class=FileResponse)
def download_export(export_id: str, db: DB, settings: AppSettings) -> FileResponse:
    artifact: ExportArtifact = _get_or_404(db, ExportArtifact, export_id, "Export")
    if artifact.status != "completed" or not artifact.storage_path:
        raise ShioriError(ErrorCode.INVALID_STATE, "Export is not ready", status_code=409)
    target = Path(artifact.storage_path).resolve()
    allowed_root = (settings.storage_root / "exports").resolve()
    if not target.is_relative_to(allowed_root) or not target.is_file():
        raise ShioriError(
            ErrorCode.EXPORT_VALIDATION_FAILED, "Export file is unavailable", status_code=409
        )
    media_types = {
        "epub": "application/epub+zip",
        "txt": "text/plain; charset=utf-8",
        "pdf": "application/pdf",
    }
    return FileResponse(
        target,
        media_type=media_types[artifact.format],
        filename=f"{artifact.project.name}-{artifact.locale}.{artifact.format}",
    )
