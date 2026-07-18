from __future__ import annotations

import asyncio
import base64
import time
from pathlib import Path

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from pydantic import ValidationError

from shiori.app import create_app
from shiori.config import Settings
from shiori.errors import ErrorCode, ShioriError
from shiori.security import APIKeyCipher
from shiori.ssrf import provider_endpoint, validate_public_https_base_url


def test_aes_gcm_round_trip_does_not_embed_plaintext() -> None:
    cipher = APIKeyCipher(b"A" * 32)
    encrypted = cipher.encrypt("sk-secret-value")
    assert b"sk-secret-value" not in encrypted
    assert cipher.decrypt(encrypted) == "sk-secret-value"
    assert cipher.encrypt("sk-secret-value") != encrypted


def test_file_backed_secrets_and_compatibility_aliases(tmp_path: Path) -> None:
    master = tmp_path / "master"
    csrf = tmp_path / "csrf"
    master.write_bytes(base64.b64encode(b"M" * 32))
    csrf.write_text("c" * 40, encoding="utf-8")
    settings = Settings(
        SHIORI_ENV="production",
        auth_mode="cloudflare",
        database_url="sqlite:///ignored.db",
        SHIORI_DATA_DIR=tmp_path,
        public_origin="https://translate.example.test",
        owner_email="owner@example.test",
        ACCESS_TEAM_DOMAIN="team.cloudflareaccess.com",
        access_audience="frontend-audience",
        origin_access_audience="origin-audience",
        access_allowed_idps=("github", "google"),
        csrf_secret_file=csrf,
        master_key_file=master,
    )
    assert settings.environment == "production"
    assert settings.storage_root == tmp_path
    assert settings.access_issuer == "https://team.cloudflareaccess.com"
    assert settings.load_master_key() == b"M" * 32
    assert settings.load_csrf_secret() == "c" * 40


def test_master_key_credential_file_requires_base64(tmp_path: Path) -> None:
    key_file = tmp_path / "master"
    key_file.write_text("R" * 32, encoding="ascii")
    settings = Settings(
        environment="test",
        auth_mode="test",
        storage_root=tmp_path,
        owner_email="owner@example.test",
        csrf_secret="C" * 40,
        master_key_file=key_file,
    )
    with pytest.raises(ValueError, match="base64"):
        settings.load_master_key()


def test_production_rejects_insecure_auth_mode(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        Settings(
            environment="production",
            auth_mode="development",
            storage_root=tmp_path,
            owner_email="owner@example.test",
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://api.example.com",
        "https://localhost/v1",
        "https://127.0.0.1/v1",
        "https://10.0.0.1/v1",
        "https://169.254.169.254/latest/meta-data",
        "https://[::1]/v1",
        "https://user:secret@api.example.com/v1",
    ],
)
def test_provider_url_rejects_unsafe_targets(url: str) -> None:
    with pytest.raises(ShioriError) as caught:
        asyncio.run(validate_public_https_base_url(url))
    assert caught.value.code == ErrorCode.API_INCOMPATIBLE


def test_provider_url_resolves_all_addresses_as_public(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "shiori.ssrf.socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("8.8.8.8", 443))],
    )
    endpoint = asyncio.run(validate_public_https_base_url("https://API.EXAMPLE.test/v1/"))
    assert endpoint.base_url == "https://api.example.test/v1"
    assert provider_endpoint(endpoint.base_url, "chat/completions").endswith("/v1/chat/completions")


def _jwk(private_key: rsa.RSAPrivateKey, kid: str) -> dict[str, str]:
    numbers = private_key.public_key().public_numbers()

    def encode(number: int) -> str:
        raw = number.to_bytes((number.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    return {
        "kty": "RSA",
        "kid": kid,
        "alg": "RS256",
        "use": "sig",
        "n": encode(numbers.n),
        "e": encode(numbers.e),
    }


def test_cloudflare_origin_and_user_assertions_are_both_required(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    kid = "test-key"
    jwks = {"keys": [_jwk(key, kid)]}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def get(self, url: str) -> httpx.Response:
            return httpx.Response(200, json=jwks, request=httpx.Request("GET", url))

    monkeypatch.setattr("shiori.security.httpx.AsyncClient", FakeAsyncClient)
    issuer = "https://team.cloudflareaccess.com"
    now = int(time.time())

    def token(
        audience: str,
        *,
        email: str | None = None,
        idp: str | None = None,
        expires_at: int | None = None,
    ) -> str:
        claims: dict[str, object] = {
            "iss": issuer,
            "aud": [audience],
            "sub": f"subject:{audience}",
            "iat": now,
            "exp": expires_at if expires_at is not None else now + 300,
        }
        if email is not None:
            claims["email"] = email
        if idp is not None:
            claims["idp"] = idp
        return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})

    csrf = tmp_path / "csrf"
    master = tmp_path / "master"
    csrf.write_text("S" * 40, encoding="utf-8")
    master.write_bytes(base64.b64encode(b"M" * 32))
    settings = Settings(
        environment="production",
        auth_mode="cloudflare",
        database_url=f"sqlite:///{(tmp_path / 'prod.db').as_posix()}",
        storage_root=tmp_path / "data",
        public_origin="https://translate.example.test",
        owner_email="owner@example.test",
        access_issuer=issuer,
        access_audience="frontend-aud",
        origin_access_audience="origin-aud",
        access_allowed_idps=("github", "google"),
        csrf_secret_file=csrf,
        master_key_file=master,
        auto_create_tables=True,
    )
    origin = token("origin-aud")
    user = token("frontend-aud", email="owner@example.test")
    verified_headers = {"Shiori-Verified-IdP": "github"}
    with TestClient(create_app(settings)) as client:
        missing_both = client.get("/api/session")
        assert missing_both.status_code == 401
        assert missing_both.json()["error"]["code"] == ErrorCode.AUTH_REQUIRED

        missing_user = client.get("/api/session", headers={"Cf-Access-Jwt-Assertion": origin})
        assert missing_user.status_code == 401

        forged_internal_header_without_origin_access = client.get(
            "/api/session",
            headers={
                "Shiori-User-Assertion": user,
                "Shiori-Verified-IdP": "github",
            },
        )
        assert forged_internal_header_without_origin_access.status_code == 401

        valid = client.get(
            "/api/session",
            headers={
                "Cf-Access-Jwt-Assertion": origin,
                "Shiori-User-Assertion": user,
                **verified_headers,
            },
        )
        assert valid.status_code == 200
        assert valid.json()["owner"]["email"] == "owner@example.test"
        assert valid.json()["owner"]["identity_provider"] == "github"

        wrong_origin = client.get(
            "/api/session",
            headers={
                "Cf-Access-Jwt-Assertion": token("frontend-aud"),
                "Shiori-User-Assertion": user,
                **verified_headers,
            },
        )
        assert wrong_origin.status_code == 401

        wrong_owner = client.get(
            "/api/session",
            headers={
                "Cf-Access-Jwt-Assertion": origin,
                "Shiori-User-Assertion": token(
                    "frontend-aud", email="intruder@example.test", idp="github"
                ),
                **verified_headers,
            },
        )
        assert wrong_owner.status_code == 403
        assert wrong_owner.json()["error"]["code"] == ErrorCode.OWNER_ONLY

        missing_verified_idp = client.get(
            "/api/session",
            headers={
                "Cf-Access-Jwt-Assertion": origin,
                "Shiori-User-Assertion": user,
            },
        )
        assert missing_verified_idp.status_code == 401

        wrong_idp = client.get(
            "/api/session",
            headers={
                "Cf-Access-Jwt-Assertion": origin,
                "Shiori-User-Assertion": user,
                "Shiori-Verified-IdP": "otp",
            },
        )
        assert wrong_idp.status_code == 403

        expired = client.get(
            "/api/session",
            headers={
                "Cf-Access-Jwt-Assertion": origin,
                "Shiori-User-Assertion": token(
                    "frontend-aud",
                    email="owner@example.test",
                    idp="google",
                    expires_at=now - 60,
                ),
                **verified_headers,
            },
        )
        assert expired.status_code == 401

        tampered_parts = user.split(".")
        tampered_parts[2] = ("A" if tampered_parts[2][0] != "A" else "B") + tampered_parts[2][1:]
        tampered_user = ".".join(tampered_parts)
        tampered = client.get(
            "/api/session",
            headers={
                "Cf-Access-Jwt-Assertion": origin,
                "Shiori-User-Assertion": tampered_user,
                **verified_headers,
            },
        )
        assert tampered.status_code == 401
