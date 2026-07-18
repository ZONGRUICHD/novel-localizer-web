from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shiori.app import create_app
from shiori.config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        environment="test",
        auth_mode="test",
        database_url=f"sqlite:///{(tmp_path / 'shiori.db').as_posix()}",
        storage_root=tmp_path / "data",
        public_origin="https://translate.example.test",
        owner_email="owner@example.test",
        csrf_secret="test-csrf-secret-with-at-least-thirty-two-bytes",
        master_key=b"K" * 32,
        auto_create_tables=True,
    )


@pytest.fixture
def app(settings: Settings):
    return create_app(settings)


@pytest.fixture
def client(app) -> Generator[TestClient, None, None]:
    with TestClient(app) as value:
        yield value


@pytest.fixture
def write_headers(client: TestClient, settings: Settings) -> dict[str, str]:
    token = client.get("/api/session").json()["csrf_token"]
    return {"Origin": settings.public_origin, "X-CSRF-Token": token}
