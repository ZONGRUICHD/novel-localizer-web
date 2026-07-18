from __future__ import annotations

import base64
import json
import sqlite3
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shiori.config import Settings
from shiori.db import Base
from shiori.documents.errors import RATE_LIMITED, DocumentError
from shiori.documents.model import Block
from shiori.models import (
    Block as DatabaseBlock,
)
from shiori.models import (
    BookDocument as DatabaseBook,
)
from shiori.models import (
    ExportArtifact,
    ProviderConfig,
    SegmentRevision,
    TranslationJob,
    TranslationProject,
    Upload,
)
from shiori.models import (
    Section as DatabaseSection,
)
from shiori.pipeline import (
    ChatCompletionConfig,
    CorePipelineRuntime,
    OpenAICompatibleChatClient,
    ReferenceIndex,
    ReferencePair,
    SQLAlchemyJobRepository,
    SQLiteLeaseRepository,
    TwoPassTranslator,
    align_paragraphs,
    detect_reference_copying,
    select_effective_revision,
    validate_pinned_endpoint,
)
from shiori.security import APIKeyCipher
from shiori.ssrf import ValidatedEndpoint
from shiori.worker import ShioriWorker


def test_reference_index_deduplicates_and_enforces_external_bounds() -> None:
    index = ReferenceIndex(sqlite3.connect(":memory:"))
    high = ReferencePair(
        library_id="makenine",
        target_locale="zh-CN",
        source_text="負けヒロインが多すぎる。" * 80,
        target_text="败犬女主太多了。" * 80,
        priority=100,
        external_allowed=True,
    )
    _, inserted = index.add(high)
    _, duplicate = index.add(high)
    index.add(
        ReferencePair(
            library_id="private-only",
            target_locale="zh-CN",
            source_text="負けヒロインの秘密",
            target_text="败犬女主的秘密",
            priority=999,
            external_allowed=False,
        )
    )
    assert inserted is True
    assert duplicate is False
    assert index.count() == 2
    results = index.search("負けヒロイン", target_locale="zh-CN", for_external_api=True)
    assert [result.library_id for result in results] == ["makenine"]
    assert all(
        len(result.source_text) <= 300 and len(result.target_text) <= 300 for result in results
    )
    assert sum(result.total_characters for result in results) <= 2400


def test_alignment_flags_difficult_pairs_and_preserves_order() -> None:
    aligned = align_paragraphs(
        ["「今日はいい天気だね」", "彼女は笑った。"],
        ["“今天天气真好。”", "她笑了。"],
    )
    assert len(aligned) == 2
    assert aligned[0].source_indices == (0,)
    assert aligned[1].target_indices == (1,)
    assert all(pair.content_hash for pair in aligned)


def test_copying_detection_uses_lcs_and_ngrams() -> None:
    copied = "她推开教室的门，看到窗边那道熟悉的身影，像往常一样露出了无奈的笑容。"
    findings = detect_reference_copying(copied, [copied + "然后故事继续。"])
    assert findings[0].suspicious is True
    assert findings[0].longest_common_characters >= 30
    independent = detect_reference_copying("他在雨停以后独自回家。", [copied])
    assert independent[0].suspicious is False


def _block(identifier: str, text: str) -> Block:
    return Block(id=identifier, kind="paragraph", source_text=text, locator=identifier)


def _chat_config(base_url: str = "https://example.test", **changes: object) -> ChatCompletionConfig:
    values: dict[str, object] = {
        "endpoint": ValidatedEndpoint(base_url, "example.test", ("93.184.216.34",)),
        "api_key": "secret",
    }
    values.update(changes)
    return ChatCompletionConfig(**values)


def test_two_pass_translation_keeps_exact_block_ids() -> None:
    calls: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(payload)
        user = json.loads(payload["messages"][-1]["content"])
        blocks = user["blocks"]
        reviewed = "conservative bilingual" in payload["messages"][0]["content"]
        translations = [
            {
                "block_id": item["block_id"],
                "text": ("审校：" if reviewed else "初稿：")
                + (item.get("japanese") or item.get("source")),
            }
            for item in blocks
        ]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"translations": translations}, ensure_ascii=False
                            )
                        }
                    }
                ]
            },
        )

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = OpenAICompatibleChatClient(
        _chat_config(),
        client=http,
        sleeper=lambda _: None,
        endpoint_validator=lambda _: None,
    )
    checkpoints: list[str] = []
    translator = TwoPassTranslator(client, translation_model="translate", review_model="review")
    result = translator.translate_chapter(
        [_block("blk_one", "彼女は笑った。"), _block("blk_two", "雨が降る。")],
        locale="zh-CN",
        checkpoint=lambda stage, _: checkpoints.append(stage),
    )
    assert [item.block_id for item in result.results] == ["blk_one", "blk_two"]
    assert result.results[0].final_text == "审校：彼女は笑った。"
    assert checkpoints == ["translated", "reviewed"]
    assert len(calls) == 2
    assert all(call["stream"] is False for call in calls)


def test_structured_output_is_repaired_at_most_twice() -> None:
    responses = iter(
        [
            "not json",
            json.dumps({"translations": [{"block_id": "wrong", "text": "x"}]}),
            json.dumps({"translations": [{"block_id": "blk_one", "text": "初稿"}]}),
            json.dumps({"translations": [{"block_id": "blk_one", "text": "审校"}]}),
        ]
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": next(responses)}}]})

    client = OpenAICompatibleChatClient(
        _chat_config(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleeper=lambda _: None,
        endpoint_validator=lambda _: None,
    )
    result = TwoPassTranslator(client, translation_model="t", review_model="r").translate_chapter(
        [_block("blk_one", "原文")], locale="zh-CN"
    )
    assert result.results[0].draft_text == "初稿"
    assert result.results[0].final_text == "审校"
    assert result.provider_requests == 4


def test_chat_client_retries_429_without_leaking_response() -> None:
    attempts = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(429, headers={"retry-after": "0"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    client = OpenAICompatibleChatClient(
        _chat_config("https://example.test/v1"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleeper=lambda _: None,
        endpoint_validator=lambda _: None,
    )
    assert client.complete(model="m", messages=[{"role": "user", "content": "x"}]) == "{}"
    assert client.endpoint == "https://example.test/v1/chat/completions"
    assert attempts == 3


def test_chat_client_emits_stable_rate_limit_error() -> None:
    client = OpenAICompatibleChatClient(
        _chat_config(max_attempts=1),
        client=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(429))),
        sleeper=lambda _: None,
        endpoint_validator=lambda _: None,
    )
    with pytest.raises(DocumentError) as captured:
        client.complete(model="m", messages=[])
    assert captured.value.code == RATE_LIMITED


def test_chat_config_rejects_private_pinned_address_and_dns_rebinding() -> None:
    with pytest.raises(DocumentError):
        ChatCompletionConfig(
            endpoint=ValidatedEndpoint("https://127.0.0.1", "127.0.0.1", ("127.0.0.1",)),
            api_key="secret",
        )

    endpoint = ValidatedEndpoint(
        "https://provider.example",
        "provider.example",
        ("93.184.216.34",),
    )

    def private_resolver(*_: object, **__: object) -> list[tuple[object, ...]]:
        return [(0, 0, 0, "", ("169.254.169.254", 443))]

    with pytest.raises(DocumentError):
        validate_pinned_endpoint(endpoint, resolver=private_resolver)


def test_chat_client_rejects_redirect_without_following_it() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(307, headers={"location": "https://127.0.0.1/secret"})

    client = OpenAICompatibleChatClient(
        _chat_config(),
        client=httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False),
        sleeper=lambda _: None,
        endpoint_validator=lambda _: None,
    )
    with pytest.raises(DocumentError):
        client.complete(model="m", messages=[])
    assert calls == 1


def test_worker_lease_recovers_checkpoint_after_expiry() -> None:
    now = [1000.0]
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    repository = SQLiteLeaseRepository(connection, clock=lambda: now[0])
    job_id = repository.enqueue(kind="translate", payload={"book": "one"})
    first = repository.lease_next(owner="worker-a", lease_seconds=10)
    assert first is not None
    assert repository.checkpoint(
        job_id, owner="worker-a", checkpoint={"chapter": 3}, lease_seconds=10
    )
    now[0] += 11
    resumed = repository.lease_next(owner="worker-b", lease_seconds=10)
    assert resumed is not None
    assert resumed.checkpoint == {"chapter": 3}
    assert resumed.attempts == 2


def test_worker_writes_checkpoint_and_completes() -> None:
    repository = SQLiteLeaseRepository(sqlite3.connect(":memory:", check_same_thread=False))
    job_id = repository.enqueue(kind="parse", payload={"value": 7})

    def handler(job: object, checkpoint: object) -> dict[str, int]:
        checkpoint({"stage": "parsed"})
        return {"answer": job.payload["value"]}

    worker = ShioriWorker(repository, {"parse": handler}, owner="test-worker")
    assert worker.run_once() is True
    stored = repository.get(job_id)
    assert stored["state"] == "completed"
    assert stored["checkpoint"] == {"stage": "parsed"}
    assert stored["result"] == {"answer": 7}


def test_production_worker_parses_translates_respects_lock_and_exports(tmp_path: Path) -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    storage = tmp_path / "data"
    source_dir = storage / "uploads" / "complete"
    source_dir.mkdir(parents=True)
    source_path = source_dir / "source.txt"
    source_bytes = "彼女は笑った。\n\n雨が降る。".encode()
    source_path.write_bytes(source_bytes)
    master_key_path = tmp_path / "master.key"
    master_key_path.write_bytes(base64.b64encode(b"k" * 32))
    settings = Settings(
        environment="test",
        auth_mode="test",
        storage_root=storage,
        database_url="sqlite://",
        master_key_file=master_key_path,
    )
    cipher = APIKeyCipher(settings.load_master_key())

    with factory() as session, session.begin():
        upload = Upload(
            filename="source.txt",
            media_type="text/plain",
            expected_size=len(source_bytes),
            expected_sha256="a" * 64,
            chunk_size=8 * 1024 * 1024,
            status="completed",
            bytes_received=len(source_bytes),
            storage_path=str(source_path),
            file_sha256="a" * 64,
        )
        session.add(upload)
        session.flush()
        book = DatabaseBook(
            source_upload_id=upload.id,
            source_hash="a" * 64,
            filename="source.txt",
            media_type="text/plain",
            original_format="txt",
            title="source",
        )
        session.add(book)
        session.flush()
        project = TranslationProject(
            book_id=book.id,
            name="edition",
            target_locale="zh-CN",
            translation_model="translate-model",
            review_model="review-model",
            config_snapshot={},
        )
        session.add(project)
        session.add(
            ProviderConfig(
                id=1,
                base_url="https://provider.example",
                api_key_encrypted=cipher.encrypt("test-key-secret"),
                api_key_tail="cret",
                translation_model="translate-model",
                review_model="review-model",
                capabilities={"json_response_format": True},
            )
        )
        session.flush()
        project_id = project.id
        book_id = book.id

    repository = SQLAlchemyJobRepository(factory)

    def provider_handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        user_payload = json.loads(payload["messages"][-1]["content"])
        is_review = "conservative bilingual" in payload["messages"][0]["content"]
        translations = [
            {
                "block_id": item["block_id"],
                "text": ("审校：" if is_review else "初稿：")
                + str(item.get("japanese") or item.get("source")),
            }
            for item in user_payload["blocks"]
        ]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"translations": translations}, ensure_ascii=False
                            )
                        }
                    }
                ]
            },
        )

    def client_factory(config: ChatCompletionConfig) -> OpenAICompatibleChatClient:
        return OpenAICompatibleChatClient(
            config,
            client=httpx.Client(transport=httpx.MockTransport(provider_handler)),
            sleeper=lambda _: None,
            endpoint_validator=lambda _: None,
        )

    runtime = CorePipelineRuntime(
        settings=settings,
        session_factory=factory,
        jobs=repository,
        endpoint_loader=lambda _: ValidatedEndpoint(
            "https://provider.example",
            "provider.example",
            ("93.184.216.34",),
        ),
        client_factory=client_factory,
    )
    runtime.ensure_parsed(book_id)
    with factory() as session, session.begin():
        first_block = session.scalar(
            select(DatabaseBlock)
            .join(DatabaseSection, DatabaseBlock.section_id == DatabaseSection.id)
            .where(DatabaseSection.book_id == book_id)
            .order_by(DatabaseBlock.ordinal)
        )
        assert first_block is not None
        session.add(
            SegmentRevision(
                project_id=project_id,
                block_id=first_block.id,
                revision_no=1,
                revision_kind="human",
                text="人工锁定文本",
                locked=True,
                source="owner",
                context_version=1,
            )
        )
        job = TranslationJob(
            project_id=project_id,
            kind="translate",
            checkpoint={"context_version": 1},
        )
        session.add(job)
        session.flush()
        translation_job_id = job.id

    worker = ShioriWorker(repository, runtime.handlers, owner="core-worker")
    assert worker.run_once() is True
    with factory() as session:
        job = session.get(TranslationJob, translation_job_id)
        assert job is not None and job.state == "completed"
        first_revisions = list(
            session.scalars(
                select(SegmentRevision)
                .where(
                    SegmentRevision.project_id == project_id,
                    SegmentRevision.block_id == first_block.id,
                )
                .order_by(SegmentRevision.revision_no)
            ).all()
        )
        effective = select_effective_revision(first_revisions)
        assert effective is not None and effective.text == "人工锁定文本"
        candidate = next(
            revision for revision in first_revisions if revision.revision_kind == "model_candidate"
        )
        assert candidate.revision_no == 2
        assert candidate.locked is False
        assert candidate.context_version == 1
        assert candidate.source.startswith(f"worker:{translation_job_id}:model=review")
        assert candidate.text == "审校：彼女は笑った。"
        human = session.scalar(
            select(SegmentRevision)
            .where(
                SegmentRevision.project_id == project_id,
                SegmentRevision.block_id == first_block.id,
                SegmentRevision.revision_kind == "human",
            )
            .order_by(SegmentRevision.revision_no.desc())
        )
        assert human is not None and human.text == "人工锁定文本" and human.locked
        assert job.checkpoint["result"]["locked_candidates"]
        translated = list(
            session.scalars(
                select(SegmentRevision).where(
                    SegmentRevision.project_id == project_id,
                    SegmentRevision.revision_kind == "model_review",
                )
            ).all()
        )
        assert len(translated) == 1

    with factory() as session, session.begin():
        artifact = ExportArtifact(
            project_id=project_id,
            format="txt",
            locale="zh-CN",
            source_version=1,
        )
        session.add(artifact)
        session.flush()
        export_job = TranslationJob(
            project_id=project_id,
            kind="export",
            checkpoint={"context_version": 1, "export_id": artifact.id},
        )
        session.add(export_job)
        session.flush()
        artifact_id = artifact.id
        export_job_id = export_job.id

    assert worker.run_once() is True
    with factory() as session:
        artifact = session.get(ExportArtifact, artifact_id)
        export_job = session.get(TranslationJob, export_job_id)
        assert artifact is not None and artifact.status == "completed"
        assert export_job is not None and export_job.state == "completed"
        exported = Path(artifact.storage_path).read_text(encoding="utf-8")
        assert "人工锁定文本" in exported
        assert "审校：雨が降る。" in exported
        assert artifact.validation["omitted_non_text_assets"] == 0

    with factory() as session, session.begin():
        invalid_cover = Upload(
            purpose="book",
            filename="wrong.png",
            media_type="image/png",
            expected_size=len(source_bytes),
            expected_sha256="b" * 64,
            chunk_size=8 * 1024 * 1024,
            status="completed",
            bytes_received=len(source_bytes),
            storage_path=str(source_path),
            file_sha256="b" * 64,
        )
        session.add(invalid_cover)
        session.flush()
        project = session.get(TranslationProject, project_id)
        assert project is not None
        project.cover_policy = "replace"
        project.replacement_cover_upload_id = invalid_cover.id
        invalid_artifact = ExportArtifact(
            project_id=project_id,
            format="epub",
            locale="zh-CN",
            source_version=1,
        )
        session.add(invalid_artifact)
        session.flush()
        invalid_job = TranslationJob(
            project_id=project_id,
            kind="export",
            checkpoint={"context_version": 1, "export_id": invalid_artifact.id},
        )
        session.add(invalid_job)
        session.flush()
        invalid_job_id = invalid_job.id
        invalid_artifact_id = invalid_artifact.id
        invalid_cover_id = invalid_cover.id

    assert worker.run_once() is True
    with factory() as session:
        invalid_job = session.get(TranslationJob, invalid_job_id)
        invalid_artifact = session.get(ExportArtifact, invalid_artifact_id)
        assert invalid_job is not None and invalid_job.error_code == "INVALID_COVER"
        assert invalid_artifact is not None and invalid_artifact.status == "failed"

    with factory() as session, session.begin():
        invalid_cover = session.get(Upload, invalid_cover_id)
        assert invalid_cover is not None
        invalid_cover.purpose = "cover"
        invalid_cover.status = "uploading"
        incomplete_artifact = ExportArtifact(
            project_id=project_id,
            format="epub",
            locale="zh-CN",
            source_version=1,
        )
        session.add(incomplete_artifact)
        session.flush()
        incomplete_job = TranslationJob(
            project_id=project_id,
            kind="export",
            checkpoint={"context_version": 1, "export_id": incomplete_artifact.id},
        )
        session.add(incomplete_job)
        session.flush()
        incomplete_job_id = incomplete_job.id

    assert worker.run_once() is True
    with factory() as session:
        incomplete_job = session.get(TranslationJob, incomplete_job_id)
        assert incomplete_job is not None and incomplete_job.error_code == "INVALID_STATE"
    engine.dispose()
