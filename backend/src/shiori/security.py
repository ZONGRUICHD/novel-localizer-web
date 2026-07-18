from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from .config import Settings
from .errors import ErrorCode, ShioriError, shiori_error_handler


@dataclass(frozen=True)
class Principal:
    subject: str
    email: str
    identity_provider: str


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class CSRFManager:
    def __init__(self, secret: str, ttl_seconds: int = 15 * 60) -> None:
        if len(secret.encode("utf-8")) < 32:
            raise ValueError("CSRF secret must be at least 32 bytes")
        self._secret = secret.encode("utf-8")
        self._ttl_seconds = ttl_seconds

    def issue(self, principal: Principal) -> str:
        payload = {
            "sub": principal.subject,
            "email": principal.email,
            "exp": int(time.time()) + self._ttl_seconds,
            "nonce": secrets.token_urlsafe(12),
        }
        encoded = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        signature = _b64url_encode(hmac.digest(self._secret, encoded.encode("ascii"), "sha256"))
        return f"{encoded}.{signature}"

    def verify(self, token: str, principal: Principal) -> None:
        try:
            encoded, signature = token.split(".", 1)
            expected = _b64url_encode(hmac.digest(self._secret, encoded.encode("ascii"), "sha256"))
            if not hmac.compare_digest(signature, expected):
                raise ValueError("signature mismatch")
            payload = json.loads(_b64url_decode(encoded))
            if int(payload["exp"]) < int(time.time()):
                raise ValueError("expired")
            if payload["sub"] != principal.subject or payload["email"] != principal.email:
                raise ValueError("principal mismatch")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ShioriError(
                ErrorCode.CSRF_INVALID,
                "The CSRF token is invalid or expired",
                status_code=403,
            ) from exc


class APIKeyCipher:
    _AAD = b"shiori/provider-api-key/v1"

    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("AES-256-GCM requires a 32-byte key")
        self._aes = AESGCM(key)

    def encrypt(self, value: str) -> bytes:
        nonce = secrets.token_bytes(12)
        return nonce + self._aes.encrypt(nonce, value.encode("utf-8"), self._AAD)

    def decrypt(self, value: bytes) -> str:
        if len(value) < 29:
            raise ValueError("encrypted API key payload is invalid")
        nonce, ciphertext = value[:12], value[12:]
        return self._aes.decrypt(nonce, ciphertext, self._AAD).decode("utf-8")


class CloudflareJWTValidator:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._keys: dict[str, Any] = {}
        self._keys_expire_at = 0.0

    async def _refresh_keys(self) -> None:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            response = await client.get(self.settings.effective_jwks_url)
        if response.is_redirect:
            raise ShioriError(ErrorCode.AUTH_INVALID, "Access JWKS redirected", status_code=401)
        try:
            response.raise_for_status()
            payload = response.json()
            keys = payload["keys"]
        except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
            raise ShioriError(
                ErrorCode.AUTH_INVALID,
                "Cloudflare Access signing keys are unavailable",
                status_code=503,
            ) from exc
        self._keys = {
            str(item["kid"]): jwt.PyJWK.from_dict(item).key
            for item in keys
            if isinstance(item, dict) and item.get("kid")
        }
        self._keys_expire_at = time.monotonic() + 3600

    async def validate(
        self,
        token: str,
        *,
        audience: str,
        require_owner: bool,
    ) -> Principal:
        try:
            header = jwt.get_unverified_header(token)
            kid = str(header["kid"])
            algorithm = str(header["alg"])
        except (jwt.PyJWTError, KeyError, TypeError) as exc:
            raise ShioriError(
                ErrorCode.AUTH_INVALID, "Access token is malformed", status_code=401
            ) from exc
        if algorithm not in {"RS256", "ES256"}:
            raise ShioriError(
                ErrorCode.AUTH_INVALID, "Access token algorithm is not allowed", status_code=401
            )
        if kid not in self._keys or time.monotonic() >= self._keys_expire_at:
            await self._refresh_keys()
        key = self._keys.get(kid)
        if key is None:
            await self._refresh_keys()
            key = self._keys.get(kid)
        if key is None:
            raise ShioriError(
                ErrorCode.AUTH_INVALID, "Access signing key is unknown", status_code=401
            )
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=[algorithm],
                audience=audience,
                issuer=self.settings.normalized_access_issuer,
                options={
                    "require": ["exp", "iat", "iss", "aud", "sub"]
                    + (["email"] if require_owner else [])
                },
                leeway=30,
            )
        except jwt.PyJWTError as exc:
            raise ShioriError(
                ErrorCode.AUTH_INVALID, "Access token validation failed", status_code=401
            ) from exc

        email = str(claims.get("email", "")).strip().lower()
        if require_owner and not hmac.compare_digest(email, self.settings.owner_email):
            raise ShioriError(ErrorCode.OWNER_ONLY, "This account is not allowed", status_code=403)
        return Principal(str(claims["sub"]), email, "")


class RequestSecurityMiddleware(BaseHTTPMiddleware):
    SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
    PUBLIC_PATHS = {"/healthz"}

    def __init__(self, app: Any, settings: Settings, csrf: CSRFManager) -> None:
        super().__init__(app)
        self.settings = settings
        self.csrf = csrf
        self.validator = CloudflareJWTValidator(settings)

    async def _authenticate(self, request: Request) -> Principal:
        if self.settings.auth_mode == "cloudflare":
            origin_token = request.headers.get(self.settings.origin_access_jwt_header)
            if not origin_token:
                raise ShioriError(
                    ErrorCode.AUTH_REQUIRED,
                    "Origin Cloudflare Access assertion is required",
                    status_code=401,
                )
            user_token = request.headers.get(self.settings.access_forwarded_jwt_header)
            if not user_token:
                raise ShioriError(
                    ErrorCode.AUTH_REQUIRED,
                    "Forwarded user Access assertion is required",
                    status_code=401,
                )
            await self.validator.validate(
                origin_token,
                audience=self.settings.origin_access_audience,
                require_owner=False,
            )
            principal = await self.validator.validate(
                user_token,
                audience=self.settings.access_audience,
                require_owner=True,
            )
            verified_idp = request.headers.get(self.settings.access_verified_idp_header, "")
            if not verified_idp:
                raise ShioriError(
                    ErrorCode.AUTH_REQUIRED,
                    "Verified identity provider is required",
                    status_code=401,
                )
            if verified_idp not in self.settings.access_allowed_idps:
                raise ShioriError(
                    ErrorCode.OWNER_ONLY,
                    "This login method is not allowed",
                    status_code=403,
                )
            return Principal(principal.subject, principal.email, verified_idp)

        header_name = (
            "X-Shiori-Test-Email" if self.settings.auth_mode == "test" else "X-Shiori-Dev-Email"
        )
        email = request.headers.get(header_name, self.settings.owner_email).strip().lower()
        if not self.settings.owner_email or not hmac.compare_digest(
            email, self.settings.owner_email
        ):
            raise ShioriError(ErrorCode.OWNER_ONLY, "This account is not allowed", status_code=403)
        return Principal(f"{self.settings.auth_mode}:owner", email, self.settings.auth_mode)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        try:
            if request.url.path in self.PUBLIC_PATHS:
                return await call_next(request)
            principal = await self._authenticate(request)
            request.state.principal = principal

            if request.method not in self.SAFE_METHODS:
                origin = request.headers.get("Origin", "")
                if not hmac.compare_digest(origin, self.settings.public_origin):
                    raise ShioriError(
                        ErrorCode.ORIGIN_REJECTED,
                        "The request origin is not allowed",
                        status_code=403,
                    )
                self.csrf.verify(request.headers.get("X-CSRF-Token", ""), principal)
            return await call_next(request)
        except ShioriError as exc:
            return await shiori_error_handler(request, exc)


def key_fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
