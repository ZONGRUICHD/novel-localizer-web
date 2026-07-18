from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from shiori.config import Settings
from shiori.errors import ErrorCode
from shiori.models import (
    Block,
    BookDocument,
    ProviderConfig,
    Section,
    SegmentRevision,
    TranslationJob,
)


def test_exact_origin_and_csrf_are_required(
    client: TestClient,
    settings: Settings,
) -> None:
    payload = {
        "filename": "sample.txt",
        "media_type": "text/plain",
        "size": 3,
        "sha256": hashlib.sha256(b"abc").hexdigest(),
    }
    no_origin = client.post("/api/uploads", json=payload)
    assert no_origin.status_code == 403
    assert no_origin.json()["error"]["code"] == ErrorCode.ORIGIN_REJECTED

    no_csrf = client.post("/api/uploads", json=payload, headers={"Origin": settings.public_origin})
    assert no_csrf.status_code == 403
    assert no_csrf.json()["error"]["code"] == ErrorCode.CSRF_INVALID

    token = client.get("/api/session").json()["csrf_token"]
    wrong_origin = client.post(
        "/api/uploads",
        json=payload,
        headers={"Origin": "https://evil.example", "X-CSRF-Token": token},
    )
    assert wrong_origin.status_code == 403
    assert wrong_origin.json()["error"]["code"] == ErrorCode.ORIGIN_REJECTED


def test_provider_is_encrypted_and_never_returned(
    client: TestClient,
    app,
    write_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "shiori.ssrf.socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("8.8.8.8", 443))],
    )
    secret = "test-key-must-never-be-returned"
    response = client.put(
        "/api/settings/provider",
        headers=write_headers,
        json={
            "base_url": "https://provider.example.test/v1",
            "api_key": secret,
            "translation_model": "translator-model",
            "review_model": "reviewer-model",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["configured"] is True
    assert body["api_key_tail"] == secret[-4:]
    assert secret not in response.text
    assert "api_key" not in body

    read_back = client.get("/api/settings/provider")
    assert secret not in read_back.text
    with app.state.session_factory() as db:
        stored = db.get(ProviderConfig, 1)
        assert stored is not None
        assert secret.encode() not in stored.api_key_encrypted
        assert app.state.key_cipher.decrypt(stored.api_key_encrypted) == secret


def test_draft_project_can_be_created_before_provider_configuration(
    client: TestClient,
    app,
    write_headers: dict[str, str],
) -> None:
    with app.state.session_factory() as db:
        book = BookDocument(
            source_hash="d" * 64,
            filename="draft.txt",
            media_type="text/plain",
            original_format="txt",
            title="Draft",
        )
        db.add(book)
        db.commit()
        book_id = book.id
    project = client.post(
        "/api/projects",
        headers=write_headers,
        json={"book_id": book_id, "name": "待配置", "target_locale": "zh-CN"},
    )
    assert project.status_code == 201, project.text
    assert project.json()["translation_model"] == "pending"
    blocked = client.post(
        "/api/jobs",
        headers=write_headers,
        json={"project_id": project.json()["id"], "kind": "translate"},
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == ErrorCode.PROVIDER_NOT_CONFIGURED


def test_resumable_upload_crud_job_and_locked_revision(
    client: TestClient,
    app,
    write_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "shiori.ssrf.socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("8.8.8.8", 443))],
    )
    provider = client.put(
        "/api/settings/provider",
        headers=write_headers,
        json={
            "base_url": "https://provider.example.test",
            "api_key": "test-provider-secret",
            "translation_model": "translate-v1",
            "review_model": "review-v1",
        },
    )
    assert provider.status_code == 200

    content = "第一章\n\nこれは本文です。".encode()
    digest = hashlib.sha256(content).hexdigest()
    created = client.post(
        "/api/uploads",
        headers=write_headers,
        json={
            "filename": "novel.txt",
            "media_type": "text/plain",
            "size": len(content),
            "sha256": digest,
        },
    )
    assert created.status_code == 201, created.text
    upload_id = created.json()["id"]
    assert created.json()["purpose"] == "book"
    chunk_headers = {
        **write_headers,
        "X-Chunk-Sha256": digest,
        "Idempotency-Key": "chunk-00000000",
        "Content-Type": "application/octet-stream",
    }
    chunk = client.put(f"/api/uploads/{upload_id}/chunks/0", headers=chunk_headers, content=content)
    assert chunk.status_code == 200, chunk.text
    assert chunk.json()["idempotent"] is False
    repeated = client.put(
        f"/api/uploads/{upload_id}/chunks/0", headers=chunk_headers, content=content
    )
    assert repeated.status_code == 200
    assert repeated.json()["idempotent"] is True

    completed = client.post(f"/api/uploads/{upload_id}/complete", headers=write_headers)
    assert completed.status_code == 200, completed.text
    assert completed.json()["upload"]["file_sha256"] == digest
    book_id = completed.json()["book_id"]

    library = client.post(
        "/api/libraries",
        headers=write_headers,
        json={
            "name": "败犬风格参考",
            "mode": "style_only",
            "target_locale": "zh-CN",
            "priority": 100,
            "rights_confirmed": True,
            "allows_external_snippets": False,
            "source_upload_ids": [upload_id],
        },
    )
    assert library.status_code == 201, library.text
    library_id = library.json()["id"]
    assert (
        client.post(f"/api/libraries/{library_id}/build-profile", headers=write_headers).status_code
        == 202
    )

    project = client.post(
        "/api/projects",
        headers=write_headers,
        json={
            "book_id": book_id,
            "name": "第一卷简中",
            "target_locale": "zh-CN",
            "selected_library_ids": [library_id],
        },
    )
    assert project.status_code == 201, project.text
    project_id = project.json()["id"]

    with app.state.session_factory() as db:
        section = Section(book_id=book_id, ordinal=0, title="第一章")
        db.add(section)
        db.flush()
        block = Block(
            section_id=section.id,
            ordinal=0,
            stable_id="stable-block-001",
            source_text="これは本文です。",
        )
        db.add(block)
        db.commit()
        block_id = block.id

    edited = client.patch(
        f"/api/books/{book_id}/segments/{block_id}",
        headers=write_headers,
        json={"project_id": project_id, "text": "这是正文。", "locked": True},
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["locked"] is True

    with app.state.session_factory() as db:
        candidate = SegmentRevision(
            project_id=project_id,
            block_id=block_id,
            revision_no=2,
            revision_kind="model_candidate",
            text="这是一份新的模型候选。",
            locked=False,
            source="review-v1",
            context_version=1,
        )
        db.add(candidate)
        db.commit()
        candidate_id = candidate.id

    listing = client.get(f"/api/books/{book_id}/segments", params={"project_id": project_id})
    assert listing.status_code == 200
    assert listing.json()["items"][0]["translation"] == "这是正文。"
    assert listing.json()["items"][0]["locked"] is True
    assert listing.json()["items"][0]["candidates"] == [
        {
            "id": candidate_id,
            "text": "这是一份新的模型候选。",
            "kind": "model_candidate",
            "source": "review-v1",
            "revision_no": 2,
            "context_version": 1,
        }
    ]

    job = client.post(
        "/api/jobs",
        headers=write_headers,
        json={"project_id": project_id, "kind": "translate"},
    )
    assert job.status_code == 201
    job_id = job.json()["id"]
    paused = client.post(f"/api/jobs/{job_id}/pause", headers=write_headers)
    assert paused.json()["state"] == "paused"
    resumed = client.post(f"/api/jobs/{job_id}/resume", headers=write_headers)
    assert resumed.json()["state"] == "queued"
    with app.state.session_factory() as db:
        awaiting_job = db.get(TranslationJob, job_id)
        assert awaiting_job is not None
        awaiting_job.state = "awaiting_review"
        awaiting_job.current_stage = "awaiting_review"
        awaiting_job.checkpoint = {"review_reason": "owner_confirmation", "chapter": 3}
        db.commit()
    confirmed = client.post(f"/api/jobs/{job_id}/resume", headers=write_headers)
    assert confirmed.status_code == 200
    assert confirmed.json()["state"] == "queued"
    assert confirmed.json()["checkpoint"]["chapter"] == 3
    cancelled = client.post(f"/api/jobs/{job_id}/cancel", headers=write_headers)
    assert cancelled.json()["state"] == "cancelled"

    candidate_job = client.post(
        f"/api/books/{book_id}/segments/{block_id}/retranslate",
        headers=write_headers,
        json={"project_id": project_id},
    )
    assert candidate_job.status_code == 202
    assert candidate_job.json()["checkpoint"]["block_id"] == block_id

    export = client.post(
        "/api/exports",
        headers=write_headers,
        json={"project_id": project_id, "format": "epub"},
    )
    assert export.status_code == 201
    assert export.json()["status"] == "queued"

    with app.state.session_factory() as db:
        revisions = db.scalars(select(Block).where(Block.id == block_id)).all()
        assert len(revisions) == 1


def test_invalid_upload_hash_is_stable_error(
    client: TestClient,
    write_headers: dict[str, str],
) -> None:
    content = b"real"
    declared = hashlib.sha256(b"different").hexdigest()
    created = client.post(
        "/api/uploads",
        headers=write_headers,
        json={
            "filename": "bad.txt",
            "media_type": "text/plain",
            "size": len(content),
            "sha256": declared,
        },
    )
    upload_id = created.json()["id"]
    actual = hashlib.sha256(content).hexdigest()
    chunk_headers = {
        **write_headers,
        "X-Chunk-Sha256": actual,
        "Idempotency-Key": "chunk-bad-0000",
    }
    assert (
        client.put(
            f"/api/uploads/{upload_id}/chunks/0", headers=chunk_headers, content=content
        ).status_code
        == 200
    )
    completed = client.post(f"/api/uploads/{upload_id}/complete", headers=write_headers)
    assert completed.status_code == 422
    assert completed.json()["error"]["code"] == ErrorCode.HASH_MISMATCH


def test_replacement_cover_upload_and_project_contract(
    client: TestClient,
    app,
    write_headers: dict[str, str],
) -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"synthetic-cover"
    digest = hashlib.sha256(png).hexdigest()
    created = client.post(
        "/api/uploads",
        headers=write_headers,
        json={
            "purpose": "cover",
            "filename": "cover.png",
            "media_type": "image/png",
            "size": len(png),
            "sha256": digest,
        },
    )
    assert created.status_code == 201, created.text
    cover_id = created.json()["id"]
    chunk = client.put(
        f"/api/uploads/{cover_id}/chunks/0",
        headers={
            **write_headers,
            "X-Chunk-Sha256": digest,
            "Idempotency-Key": "cover-chunk-0000",
        },
        content=png,
    )
    assert chunk.status_code == 200
    completed = client.post(f"/api/uploads/{cover_id}/complete", headers=write_headers)
    assert completed.status_code == 200, completed.text
    assert completed.json()["book_id"] is None
    assert completed.json()["upload"]["purpose"] == "cover"
    assert client.get("/api/books").json() == []

    with app.state.session_factory() as db:
        book = BookDocument(
            source_hash="e" * 64,
            filename="source.epub",
            media_type="application/epub+zip",
            original_format="epub",
            title="Source",
        )
        db.add(book)
        db.commit()
        book_id = book.id

    project = client.post(
        "/api/projects",
        headers=write_headers,
        json={
            "book_id": book_id,
            "name": "替换封面",
            "target_locale": "zh-CN",
            "cover_policy": "replace",
            "replacement_cover_upload_id": cover_id,
        },
    )
    assert project.status_code == 201, project.text
    assert project.json()["replacement_cover_upload_id"] == cover_id

    draft = client.post(
        "/api/projects",
        headers=write_headers,
        json={"book_id": book_id, "name": "稍后替换", "target_locale": "zh-TW"},
    )
    patched = client.patch(
        f"/api/projects/{draft.json()['id']}",
        headers=write_headers,
        json={"cover_policy": "replace", "replacement_cover_upload_id": cover_id},
    )
    assert patched.status_code == 200
    assert patched.json()["cover_policy"] == "replace"

    incomplete = client.post(
        "/api/uploads",
        headers=write_headers,
        json={
            "purpose": "cover",
            "filename": "later.jpg",
            "media_type": "image/jpeg",
            "size": 4,
            "sha256": hashlib.sha256(b"later").hexdigest(),
        },
    )
    rejected = client.patch(
        f"/api/projects/{project.json()['id']}",
        headers=write_headers,
        json={"replacement_cover_upload_id": incomplete.json()["id"]},
    )
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == ErrorCode.INVALID_STATE


def test_cover_type_and_magic_are_validated(
    client: TestClient,
    write_headers: dict[str, str],
) -> None:
    wrong_book_media = client.post(
        "/api/uploads",
        headers=write_headers,
        json={
            "filename": "book.pdf",
            "media_type": "text/plain",
            "size": 4,
            "sha256": hashlib.sha256(b"fake").hexdigest(),
        },
    )
    assert wrong_book_media.status_code == 422

    mismatched_type = client.post(
        "/api/uploads",
        headers=write_headers,
        json={
            "purpose": "cover",
            "filename": "cover.png",
            "media_type": "image/jpeg",
            "size": 4,
            "sha256": hashlib.sha256(b"fake").hexdigest(),
        },
    )
    assert mismatched_type.status_code == 422
    assert mismatched_type.json()["error"]["code"] == ErrorCode.INVALID_COVER

    fake = b"not-a-png"
    fake_digest = hashlib.sha256(fake).hexdigest()
    created = client.post(
        "/api/uploads",
        headers=write_headers,
        json={
            "purpose": "cover",
            "filename": "cover.png",
            "media_type": "image/png",
            "size": len(fake),
            "sha256": fake_digest,
        },
    )
    upload_id = created.json()["id"]
    client.put(
        f"/api/uploads/{upload_id}/chunks/0",
        headers={
            **write_headers,
            "X-Chunk-Sha256": fake_digest,
            "Idempotency-Key": "fake-cover-chunk",
        },
        content=fake,
    )
    completed = client.post(f"/api/uploads/{upload_id}/complete", headers=write_headers)
    assert completed.status_code == 422
    assert completed.json()["error"]["code"] == ErrorCode.INVALID_COVER
