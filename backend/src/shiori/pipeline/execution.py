from __future__ import annotations

import asyncio
import hashlib
import os
import sqlite3
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, cast

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from shiori.config import Settings
from shiori.documents import (
    Block as PipelineBlock,
)
from shiori.documents import (
    BookDocument as PipelineDocument,
)
from shiori.documents import export_epub, export_pdf, export_txt, parse_epub, parse_pdf, parse_txt
from shiori.documents.errors import API_INCOMPATIBLE, DocumentError
from shiori.documents.model import BlockKind
from shiori.models import (
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
)
from shiori.security import APIKeyCipher
from shiori.ssrf import ValidatedEndpoint, validate_public_https_base_url

from .alignment import align_paragraphs
from .core_jobs import SQLAlchemyJobRepository
from .lease import LeasedJob
from .retrieval import ReferenceIndex, ReferencePair, build_style_profile
from .translation import (
    ChatCompletionConfig,
    OpenAICompatibleChatClient,
    TwoPassTranslator,
)

EndpointLoader = Callable[[str], ValidatedEndpoint]
ClientFactory = Callable[[ChatCompletionConfig], OpenAICompatibleChatClient]


def _default_endpoint_loader(base_url: str) -> ValidatedEndpoint:
    return asyncio.run(validate_public_https_base_url(base_url))


def _document_parser(path: Path) -> PipelineDocument:
    extension = path.suffix.lower()
    if extension == ".epub":
        return parse_epub(path)
    if extension == ".txt":
        return parse_txt(path)
    if extension == ".pdf":
        return parse_pdf(path)
    raise DocumentError("UNSUPPORTED_FORMAT", "Only EPUB, TXT, and PDF are supported.")


def select_effective_revision(
    revisions: Iterable[SegmentRevision],
) -> SegmentRevision | None:
    """Select displayed/exported text without promoting model candidates.

    Candidates remain durable and comparable, but they are never an active
    edition merely because their revision number is newer.
    """

    eligible = [revision for revision in revisions if revision.revision_kind != "model_candidate"]
    if not eligible:
        return None

    def priority(revision: SegmentRevision) -> tuple[int, int]:
        if revision.revision_kind == "human" and revision.locked:
            return (3, revision.revision_no)
        if revision.revision_kind == "human":
            return (2, revision.revision_no)
        return (1, revision.revision_no)

    return max(eligible, key=priority)


class CorePipelineRuntime:
    """Concrete parsing, translation and export handlers for production jobs."""

    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: sessionmaker[Session],
        jobs: SQLAlchemyJobRepository,
        endpoint_loader: EndpointLoader = _default_endpoint_loader,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.jobs = jobs
        self.endpoint_loader = endpoint_loader
        self.client_factory = client_factory or (lambda config: OpenAICompatibleChatClient(config))

    @property
    def handlers(
        self,
    ) -> dict[str, Callable[[LeasedJob, Callable[[dict[str, Any]], None]], dict[str, Any]]]:
        return {
            "parse": self.parse_job,
            "translate": self.translate_job,
            "retranslate": self.translate_job,
            "export": self.export_job,
        }

    def _check_cancelled(self, job: LeasedJob) -> None:
        if self.jobs.cancellation_requested(job.id, owner=job.lease_owner):
            raise JobCancellationRequested

    def _source_path(self, book_id: str) -> Path:
        with self.session_factory() as session:
            book = session.get(BookDocument, book_id)
            upload = (
                session.get(Upload, book.source_upload_id)
                if book and book.source_upload_id
                else None
            )
            if book is None or upload is None or not upload.storage_path:
                raise DocumentError("NOT_FOUND", "The source upload is unavailable.")
            path = Path(upload.storage_path).resolve()
            allowed = (self.settings.storage_root / "uploads" / "complete").resolve()
            if not path.is_relative_to(allowed) or not path.is_file():
                raise DocumentError("NOT_FOUND", "The source upload path is outside storage.")
            return path

    def ensure_parsed(self, book_id: str, *, job: LeasedJob | None = None) -> PipelineDocument:
        if job is not None:
            self._check_cancelled(job)
            self.jobs.set_stage(
                job.id,
                owner=job.lease_owner,
                stage="parsing",
                progress=0.08,
                checkpoint=job.checkpoint,
            )
        source_path = self._source_path(book_id)
        document = _document_parser(source_path)
        with self.session_factory() as session, session.begin():
            book = session.get(BookDocument, book_id)
            if book is None:
                raise DocumentError("NOT_FOUND", "The source book no longer exists.")
            existing_sections = int(
                session.scalar(
                    select(func.count()).select_from(Section).where(Section.book_id == book.id)
                )
                or 0
            )
            if existing_sections == 0:
                for section_ordinal, parsed_section in enumerate(document.sections):
                    section = Section(
                        book_id=book.id,
                        ordinal=section_ordinal,
                        title=parsed_section.title or None,
                        locator={
                            "source": parsed_section.locator,
                            "href": parsed_section.href,
                            "metadata": parsed_section.metadata,
                        },
                    )
                    session.add(section)
                    session.flush()
                    for block_ordinal, parsed_block in enumerate(parsed_section.blocks):
                        session.add(
                            Block(
                                section_id=section.id,
                                ordinal=block_ordinal,
                                stable_id=parsed_block.id,
                                kind=parsed_block.kind,
                                source_text=parsed_block.source_text,
                                block_metadata={
                                    "locator": parsed_block.locator,
                                    **parsed_block.metadata,
                                },
                            )
                        )
            book.title = document.title or book.title
            book.language = document.language
            book.parse_status = "parsed"
            book.document_metadata = {
                **document.metadata,
                "pipeline_document_id": document.id,
                "cover_asset_id": document.cover_asset_id,
                "assets": [
                    {
                        "path": asset.path,
                        "media_type": asset.media_type,
                        "sha256": asset.sha256,
                        "is_cover": asset.is_cover,
                    }
                    for asset in document.assets
                ],
            }
        return document

    def parse_job(
        self,
        job: LeasedJob,
        checkpoint: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        del checkpoint
        with self.session_factory() as session:
            project = session.get(TranslationProject, job.payload["project_id"])
            if project is None:
                raise DocumentError("NOT_FOUND", "The translation project no longer exists.")
            book_id = project.book_id
        document = self.ensure_parsed(book_id, job=job)
        return {
            "book_id": book_id,
            "sections": len(document.sections),
            "blocks": sum(len(section.blocks) for section in document.sections),
        }

    def _translation_context(
        self,
        session: Session,
        project: TranslationProject,
    ) -> tuple[ReferenceIndex | None, dict[str, Any]]:
        if not project.selected_library_ids:
            return None, {}
        libraries = list(
            session.scalars(
                select(ReferenceLibrary).where(
                    ReferenceLibrary.id.in_(project.selected_library_ids)
                )
            ).all()
        )
        memory_index = ReferenceIndex(sqlite3.connect(":memory:"))
        indexed_pairs = 0
        profiles: list[dict[str, Any]] = []
        for library in libraries:
            profiles.append(
                {
                    "name": library.name,
                    "priority": library.priority,
                    "mode": library.mode,
                    "profile": library.profile,
                }
            )
            if not (library.rights_confirmed and library.allows_external_snippets):
                continue
            for raw_pair in library.pairings or []:
                if not isinstance(raw_pair, dict):
                    continue
                source = str(raw_pair.get("source_text") or "")
                target = str(raw_pair.get("target_text") or "")
                if not source and not target:
                    continue
                _, inserted = memory_index.add(
                    ReferencePair(
                        library_id=library.id,
                        target_locale=project.target_locale,
                        source_text=source[:300],
                        target_text=target[:300],
                        priority=library.priority,
                        mode=library.mode,
                        external_allowed=True,
                    )
                )
                indexed_pairs += int(inserted)
        if indexed_pairs == 0:
            memory_index.connection.close()
            return None, {"libraries": profiles}
        return memory_index, {"libraries": profiles}

    def _persist_results(
        self,
        *,
        job: LeasedJob,
        project_id: str,
        context_version: int,
        result_by_stable_id: dict[str, tuple[str, str, str]],
        locked_candidates: dict[str, str],
        candidate_model: str,
    ) -> None:
        with self.session_factory() as session, session.begin():
            requested_ids = set(result_by_stable_id) | set(locked_candidates)
            blocks = list(
                session.scalars(select(Block).where(Block.stable_id.in_(requested_ids))).all()
            )
            for block in blocks:
                if block.stable_id in locked_candidates:
                    candidate_source = f"worker:{job.id}:model={candidate_model}"[:64]
                    already_saved = session.scalar(
                        select(SegmentRevision.id).where(
                            SegmentRevision.project_id == project_id,
                            SegmentRevision.block_id == block.id,
                            SegmentRevision.revision_kind == "model_candidate",
                            SegmentRevision.source == candidate_source,
                        )
                    )
                    if already_saved:
                        continue
                    maximum = int(
                        session.scalar(
                            select(func.coalesce(func.max(SegmentRevision.revision_no), 0)).where(
                                SegmentRevision.project_id == project_id,
                                SegmentRevision.block_id == block.id,
                            )
                        )
                        or 0
                    )
                    session.add(
                        SegmentRevision(
                            project_id=project_id,
                            block_id=block.id,
                            revision_no=maximum + 1,
                            revision_kind="model_candidate",
                            text=locked_candidates[block.stable_id],
                            locked=False,
                            source=candidate_source,
                            context_version=context_version,
                        )
                    )
                    continue
                draft, reviewed, final_kind = result_by_stable_id[block.stable_id]
                existing_source = f"job:{job.id}:review"
                already_saved = session.scalar(
                    select(SegmentRevision.id).where(
                        SegmentRevision.project_id == project_id,
                        SegmentRevision.block_id == block.id,
                        SegmentRevision.source == existing_source,
                    )
                )
                if already_saved:
                    continue
                maximum = int(
                    session.scalar(
                        select(func.coalesce(func.max(SegmentRevision.revision_no), 0)).where(
                            SegmentRevision.project_id == project_id,
                            SegmentRevision.block_id == block.id,
                        )
                    )
                    or 0
                )
                session.add(
                    SegmentRevision(
                        project_id=project_id,
                        block_id=block.id,
                        revision_no=maximum + 1,
                        revision_kind="model_draft",
                        text=draft,
                        locked=False,
                        source=f"job:{job.id}:draft",
                        context_version=context_version,
                    )
                )
                session.add(
                    SegmentRevision(
                        project_id=project_id,
                        block_id=block.id,
                        revision_no=maximum + 2,
                        revision_kind=final_kind,
                        text=reviewed,
                        locked=False,
                        source=existing_source,
                        context_version=context_version,
                    )
                )
            db_job = session.get(TranslationJob, job.id)
            if db_job is not None and locked_candidates:
                merged = dict(db_job.checkpoint or {})
                merged["locked_candidates"] = locked_candidates
                db_job.checkpoint = merged

    def translate_job(
        self,
        job: LeasedJob,
        checkpoint: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        del checkpoint
        with self.session_factory() as session:
            project = session.get(TranslationProject, job.payload["project_id"])
            if project is None:
                raise DocumentError("NOT_FOUND", "The translation project no longer exists.")
            if job.checkpoint.get("context_version") != project.context_version:
                raise DocumentError(
                    "INVALID_STATE", "Project context changed after this job was created."
                )
            book_id = project.book_id
        self.ensure_parsed(book_id, job=job)

        with self.session_factory() as session:
            project = session.get(TranslationProject, job.payload["project_id"])
            provider = session.get(ProviderConfig, 1)
            if project is None or provider is None:
                raise DocumentError(
                    API_INCOMPATIBLE, "Provider or project configuration is unavailable."
                )
            sections = list(
                session.scalars(
                    select(Section).where(Section.book_id == book_id).order_by(Section.ordinal)
                ).all()
            )
            block_rows = list(
                session.scalars(
                    select(Block)
                    .join(Section, Block.section_id == Section.id)
                    .where(Section.book_id == book_id)
                    .order_by(Section.ordinal, Block.ordinal)
                ).all()
            )
            reference_index, style_profile = self._translation_context(session, project)
            endpoint = self.endpoint_loader(provider.base_url)
            cipher = APIKeyCipher(self.settings.load_master_key())
            api_key = cipher.decrypt(provider.api_key_encrypted)
            supports_json = bool(provider.capabilities.get("json_response_format", True))
            translation_model = project.translation_model
            review_model = project.review_model
            locale = project.target_locale
            glossary = dict(project.glossary or {})
            context_version = project.context_version

            locked_human_block_ids: set[str] = set()
            revisions = session.scalars(
                select(SegmentRevision)
                .where(SegmentRevision.project_id == project.id)
                .order_by(SegmentRevision.block_id, SegmentRevision.revision_no.desc())
            )
            for revision in revisions:
                if revision.revision_kind == "human" and revision.locked:
                    locked_human_block_ids.add(revision.block_id)

        client = self.client_factory(
            ChatCompletionConfig(
                endpoint=endpoint,
                api_key=api_key,
                timeout_seconds=self.settings.provider_timeout_seconds,
                supports_json_response_format=supports_json,
            )
        )
        translator = TwoPassTranslator(
            client,
            translation_model=translation_model,
            review_model=review_model,
        )
        blocks_by_section: dict[str, list[Block]] = {section.id: [] for section in sections}
        for block in block_rows:
            blocks_by_section.setdefault(block.section_id, []).append(block)
        selected_block_id = job.checkpoint.get("block_id") if job.kind == "retranslate" else None
        completed_sections = set(job.checkpoint.get("completed_section_ids") or [])
        locked_candidates: dict[str, str] = {}
        translated_count = 0
        awaiting_review = False
        try:
            for section_index, section in enumerate(sections):
                if section.id in completed_sections:
                    continue
                self._check_cancelled(job)
                rows = blocks_by_section.get(section.id, [])
                if selected_block_id:
                    rows = [row for row in rows if row.id == selected_block_id]
                unlocked: list[Block] = []
                locked: set[str] = set()
                for row in rows:
                    if row.id in locked_human_block_ids:
                        locked.add(row.stable_id)
                    unlocked.append(row)
                pipeline_blocks = [
                    PipelineBlock(
                        id=row.stable_id,
                        kind=cast(BlockKind, row.kind),
                        source_text=row.source_text,
                        locator=str(row.block_metadata.get("locator", row.ordinal)),
                        metadata=dict(row.block_metadata or {}),
                    )
                    for row in unlocked
                ]
                if not pipeline_blocks:
                    completed_sections.add(section.id)
                    continue
                references = (
                    reference_index.search(
                        "\n".join(block.source_text for block in pipeline_blocks),
                        target_locale=locale,
                        for_external_api=True,
                    )
                    if reference_index is not None
                    else []
                )
                progress_base = 0.15 + (section_index / max(len(sections), 1)) * 0.72
                self.jobs.set_stage(
                    job.id,
                    owner=job.lease_owner,
                    stage="translating",
                    progress=progress_base,
                    checkpoint={
                        **job.checkpoint,
                        "completed_section_ids": sorted(completed_sections),
                        "section_id": section.id,
                    },
                )

                def on_translation_checkpoint(
                    stage: str,
                    payload: dict[str, Any],
                    progress: float = progress_base,
                    section_id: str = section.id,
                ) -> None:
                    next_stage = (
                        "reviewing"
                        if stage in {"translated", "reviewed", "rewritten"}
                        else "translating"
                    )
                    self.jobs.set_stage(
                        job.id,
                        owner=job.lease_owner,
                        stage=next_stage,
                        progress=min(progress + 0.05, 0.94),
                        checkpoint={
                            **job.checkpoint,
                            "completed_section_ids": sorted(completed_sections),
                            "section_id": section_id,
                            "batch": payload.get("batch"),
                            "pass": stage,
                        },
                    )

                chapter = translator.translate_chapter(
                    pipeline_blocks,
                    locale=locale,
                    references=references,
                    terminology=glossary,
                    style_profile=style_profile,
                    checkpoint=on_translation_checkpoint,
                )
                result_by_id: dict[str, tuple[str, str, str]] = {}
                for result in chapter.results:
                    if result.block_id in locked:
                        locked_candidates[result.block_id] = result.final_text
                        continue
                    result_by_id[result.block_id] = (
                        result.draft_text,
                        result.final_text,
                        "model_candidate" if job.kind == "retranslate" else "model_review",
                    )
                self._persist_results(
                    job=job,
                    project_id=project.id,
                    context_version=context_version,
                    result_by_stable_id=result_by_id,
                    locked_candidates=locked_candidates,
                    candidate_model=review_model,
                )
                translated_count += len(result_by_id)
                awaiting_review = awaiting_review or chapter.awaiting_review
                completed_sections.add(section.id)
                self.jobs.set_stage(
                    job.id,
                    owner=job.lease_owner,
                    stage="reviewing",
                    progress=min(progress_base + 0.1, 0.95),
                    checkpoint={
                        **job.checkpoint,
                        "completed_section_ids": sorted(completed_sections),
                        "locked_candidates": locked_candidates,
                    },
                )
                if selected_block_id:
                    break
        finally:
            client.close()
            if reference_index is not None:
                reference_index.connection.close()
            api_key = ""  # release the decrypted value as soon as practical

        with self.session_factory() as session, session.begin():
            project = session.get(TranslationProject, job.payload["project_id"])
            if project is not None:
                project.status = "awaiting_review" if awaiting_review else "translated"
        return {
            "translated_blocks": translated_count,
            "locked_candidates": locked_candidates,
            "awaiting_review": awaiting_review,
        }

    def _apply_latest_translations(
        self,
        document: PipelineDocument,
        *,
        session: Session,
        project: TranslationProject,
    ) -> None:
        rows = session.execute(
            select(Block, SegmentRevision)
            .join(Section, Block.section_id == Section.id)
            .join(
                SegmentRevision,
                (SegmentRevision.block_id == Block.id) & (SegmentRevision.project_id == project.id),
            )
            .where(Section.book_id == project.book_id)
            .order_by(Block.stable_id, SegmentRevision.revision_no.desc())
        ).all()
        revisions_by_block: dict[str, list[SegmentRevision]] = {}
        for block, revision in rows:
            revisions_by_block.setdefault(block.stable_id, []).append(revision)
        effective = {
            stable_id: selected.text
            for stable_id, revisions in revisions_by_block.items()
            if (selected := select_effective_revision(revisions)) is not None
        }
        for block in document.iter_blocks():
            translated = effective.get(block.id)
            if translated is not None:
                block.translations[project.target_locale] = translated

    def export_job(
        self,
        job: LeasedJob,
        checkpoint: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        del checkpoint
        export_id = job.checkpoint.get("export_id")
        with self.session_factory() as session, session.begin():
            artifact = session.get(ExportArtifact, export_id) if export_id else None
            project = session.get(TranslationProject, job.payload["project_id"])
            if artifact is None or project is None or artifact.project_id != project.id:
                raise DocumentError("NOT_FOUND", "The export request no longer exists.")
            if artifact.source_version != project.book.source_version:
                raise DocumentError(
                    "INVALID_STATE", "The source book changed after export was queued."
                )
            artifact.status = "processing"
            book_id = project.book_id

        document = self.ensure_parsed(book_id, job=job)
        self._check_cancelled(job)
        self.jobs.set_stage(
            job.id,
            owner=job.lease_owner,
            stage="assembling",
            progress=0.62,
            checkpoint=job.checkpoint,
        )
        with self.session_factory() as session:
            project = session.get(TranslationProject, job.payload["project_id"])
            artifact = session.get(ExportArtifact, export_id)
            if project is None or artifact is None:
                raise DocumentError("NOT_FOUND", "The export request no longer exists.")
            self._apply_latest_translations(document, session=session, project=project)
            cover_policy = project.cover_policy
            replacement: bytes | None = None
            replacement_media = "image/jpeg"
            if cover_policy == "replace" and project.replacement_cover_upload_id:
                upload = session.get(Upload, project.replacement_cover_upload_id)
                if upload is None:
                    raise DocumentError("NOT_FOUND", "The replacement cover is unavailable.")
                if upload.purpose != "cover":
                    raise DocumentError(
                        "INVALID_COVER", "The selected upload is not a replacement cover."
                    )
                if upload.status != "completed" or not upload.storage_path:
                    raise DocumentError(
                        "INVALID_STATE", "The replacement cover upload is not complete."
                    )
                cover_path = Path(upload.storage_path).resolve()
                allowed_uploads = (self.settings.storage_root / "uploads" / "complete").resolve()
                if not cover_path.is_relative_to(allowed_uploads) or not cover_path.is_file():
                    raise DocumentError("NOT_FOUND", "The replacement cover file is unavailable.")
                replacement = cover_path.read_bytes()
                replacement_media = upload.media_type
            locale = project.target_locale
            output_format = artifact.format

        if output_format == "epub":
            payload = export_epub(
                document,
                target_locale=locale,
                cover_policy=cover_policy,
                replacement_cover=replacement,
                replacement_cover_media_type=replacement_media,
            )
        elif output_format == "txt":
            payload = export_txt(document, target_locale=locale)
        elif output_format == "pdf":
            payload = export_pdf(document, target_locale=locale)
        else:
            raise DocumentError("UNSUPPORTED_FORMAT", "The export format is not supported.")

        self._check_cancelled(job)
        self.jobs.set_stage(
            job.id,
            owner=job.lease_owner,
            stage="validating_output",
            progress=0.92,
            checkpoint=job.checkpoint,
        )
        export_root = (self.settings.storage_root / "exports" / project.id).resolve()
        export_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        final = (export_root / f"{artifact.id}.{output_format}").resolve()
        if not final.is_relative_to((self.settings.storage_root / "exports").resolve()):
            raise DocumentError("EXPORT_VALIDATION_FAILED", "Export path escaped storage.")
        temporary = final.with_suffix(final.suffix + ".partial")
        try:
            temporary.write_bytes(payload)
            os.replace(temporary, final)
        finally:
            temporary.unlink(missing_ok=True)
        digest = hashlib.sha256(payload).hexdigest()
        with self.session_factory() as session, session.begin():
            artifact = session.get(ExportArtifact, export_id)
            if artifact is None:
                final.unlink(missing_ok=True)
                raise DocumentError("NOT_FOUND", "The export request no longer exists.")
            artifact.status = "completed"
            artifact.storage_path = str(final)
            artifact.sha256 = digest
            artifact.validation = {
                "valid": True,
                "bytes": len(payload),
                "sha256": digest,
                "omitted_non_text_assets": len(document.assets) if output_format == "txt" else 0,
            }
        return {"export_id": export_id, "format": output_format, "sha256": digest}

    def process_one_library_profile(self) -> bool:
        """Consume the API's ``status=building`` library maintenance queue."""

        with self.session_factory() as session:
            library = session.scalar(
                select(ReferenceLibrary)
                .where(ReferenceLibrary.status == "building")
                .order_by(ReferenceLibrary.updated_at.asc())
                .limit(1)
            )
            if library is None:
                return False
            library_id = library.id
            upload_ids = list(library.source_upload_ids or [])
            pairing_specs = list(library.pairings or [])

        texts: list[str] = []
        source_documents: dict[str, PipelineDocument] = {}
        with self.session_factory() as session:
            uploads = {
                upload.id: upload
                for upload in session.scalars(select(Upload).where(Upload.id.in_(upload_ids))).all()
            }
            for upload_id, upload in uploads.items():
                if not upload.storage_path:
                    continue
                document = _document_parser(Path(upload.storage_path))
                source_documents[upload_id] = document
                texts.extend(block.source_text for block in document.iter_blocks())

        alignment_review: list[dict[str, Any]] = []
        enriched_pairings: list[dict[str, Any]] = []
        for spec in pairing_specs:
            if not isinstance(spec, dict):
                continue
            source_id = str(spec.get("source_upload_id") or "")
            target_id = str(spec.get("target_upload_id") or "")
            source_document = source_documents.get(source_id)
            target_document = source_documents.get(target_id)
            if source_document is None or target_document is None:
                enriched_pairings.append(spec)
                continue
            aligned = align_paragraphs(
                [block.source_text for block in source_document.iter_blocks()],
                [block.source_text for block in target_document.iter_blocks()],
            )
            for pair in aligned:
                record = {
                    "source_text": pair.source_text,
                    "target_text": pair.target_text,
                    "confidence": pair.confidence,
                    "content_hash": pair.content_hash,
                }
                if pair.needs_review:
                    alignment_review.append(record)
                else:
                    enriched_pairings.append(record)

        unique_texts = list(dict.fromkeys(texts))
        profile = build_style_profile(unique_texts)
        profile["content_hashes"] = [
            hashlib.sha256(text.encode("utf-8")).hexdigest() for text in unique_texts
        ]
        profile["deduplicated_blocks"] = len(texts) - len(unique_texts)
        with self.session_factory() as session, session.begin():
            library = session.get(ReferenceLibrary, library_id)
            if library is None:
                return True
            library.profile = profile
            library.alignment_review = alignment_review
            if enriched_pairings:
                library.pairings = enriched_pairings
            library.status = "review_required" if alignment_review else "ready"
        return True


class JobCancellationRequested(Exception):
    pass
