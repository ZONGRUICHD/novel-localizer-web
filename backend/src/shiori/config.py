from __future__ import annotations

import base64
import binascii
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SHIORI_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    environment: Literal["production", "development", "test"] = Field(
        default="production",
        validation_alias=AliasChoices("SHIORI_ENV", "SHIORI_ENVIRONMENT", "environment"),
    )
    auth_mode: Literal["cloudflare", "development", "test"] = "cloudflare"
    database_url: str = "sqlite:////var/lib/shiori/shiori.sqlite3"
    storage_root: Path = Field(
        default=Path("/var/lib/shiori"),
        validation_alias=AliasChoices("SHIORI_DATA_DIR", "SHIORI_STORAGE_ROOT", "storage_root"),
    )
    public_origin: str = "https://translate.zongtech.xyz"
    owner_email: str = ""

    access_issuer: str = Field(
        default="",
        validation_alias=AliasChoices(
            "SHIORI_ACCESS_ISSUER",
            "SHIORI_ACCESS_TEAM_DOMAIN",
            "ACCESS_TEAM_DOMAIN",
            "access_issuer",
        ),
    )
    access_audience: str = ""
    origin_access_audience: str = ""
    access_jwks_url: str = ""
    access_forwarded_jwt_header: str = "Shiori-User-Assertion"
    origin_access_jwt_header: str = "Cf-Access-Jwt-Assertion"
    access_verified_idp_header: str = "Shiori-Verified-IdP"
    access_allowed_idps: tuple[str, ...] = ()

    csrf_secret: SecretStr = SecretStr("")
    csrf_secret_file: Path | None = None
    master_key: SecretStr | None = None
    master_key_file: Path | None = None

    upload_chunk_size: int = 8 * 1024 * 1024
    upload_max_bytes: int = 256 * 1024 * 1024
    provider_timeout_seconds: float = 20.0
    sqlite_busy_timeout_ms: int = 5_000
    auto_create_tables: bool = False

    @field_validator("owner_email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("public_origin")
    @classmethod
    def normalize_origin(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("access_issuer")
    @classmethod
    def normalize_issuer_input(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        if normalized and "://" not in normalized:
            normalized = f"https://{normalized}"
        return normalized

    @field_validator("access_allowed_idps", mode="before")
    @classmethod
    def parse_idps(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(part.strip() for part in value.split(",") if part.strip())
        return value

    @model_validator(mode="after")
    def validate_security_mode(self) -> Settings:
        if self.environment == "production":
            if self.auth_mode != "cloudflare":
                raise ValueError("production requires SHIORI_AUTH_MODE=cloudflare")
            required = {
                "SHIORI_OWNER_EMAIL": self.owner_email,
                "SHIORI_ACCESS_ISSUER": self.access_issuer,
                "SHIORI_ACCESS_AUDIENCE": self.access_audience,
                "SHIORI_ORIGIN_ACCESS_AUDIENCE": self.origin_access_audience,
            }
            missing = [name for name, value in required.items() if not value]
            if missing:
                raise ValueError(f"missing production security settings: {', '.join(missing)}")
            if not self.access_allowed_idps:
                raise ValueError("production requires SHIORI_ACCESS_ALLOWED_IDPS")
            if self.access_audience == self.origin_access_audience:
                raise ValueError("frontend and origin Access audiences must be different")
            if not self.csrf_secret.get_secret_value() and self.csrf_secret_file is None:
                raise ValueError("production requires SHIORI_CSRF_SECRET_FILE")
            if self.master_key is None and self.master_key_file is None:
                raise ValueError("production requires SHIORI_MASTER_KEY_FILE")
        if self.auth_mode == "test" and self.environment != "test":
            raise ValueError("test auth mode is only available in the test environment")
        if self.auth_mode == "development" and self.environment != "development":
            raise ValueError("development auth mode is only available in development")
        return self

    @property
    def normalized_access_issuer(self) -> str:
        return self.access_issuer.rstrip("/")

    @property
    def effective_jwks_url(self) -> str:
        if self.access_jwks_url:
            return self.access_jwks_url
        return f"{self.normalized_access_issuer}/cdn-cgi/access/certs"

    def load_master_key(self) -> bytes:
        """Load the AES-256 key without retaining a printable representation."""
        if self.master_key_file is not None:
            # systemd credentials are provisioned as ASCII base64. Never treat
            # credential-file bytes as a raw key: stripping a random raw key
            # could silently change valid leading or trailing whitespace bytes.
            raw = self.master_key_file.read_text(encoding="ascii").strip().encode("ascii")
            allow_raw = False
        elif self.master_key is not None:
            raw = self.master_key.get_secret_value().encode("ascii")
            allow_raw = True
        else:
            raise ValueError("provider encryption master key is not configured")

        if allow_raw and len(raw) == 32:
            return raw
        try:
            decoded = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("master key must be 32 raw bytes or base64-encoded") from exc
        if len(decoded) != 32:
            raise ValueError("master key base64 must decode to exactly 32 bytes")
        return decoded

    def load_csrf_secret(self) -> str:
        if self.csrf_secret_file is not None:
            value = self.csrf_secret_file.read_text(encoding="utf-8").strip()
        else:
            value = self.csrf_secret.get_secret_value()
        if len(value.encode("utf-8")) < 32:
            raise ValueError("CSRF secret must be at least 32 bytes")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
