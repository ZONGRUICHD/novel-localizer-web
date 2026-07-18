from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from .errors import ErrorCode, ShioriError
from .ssrf import provider_endpoint, validate_public_https_base_url


def _raise_provider_status(response: httpx.Response) -> None:
    if response.status_code == 401 or response.status_code == 403:
        raise ShioriError(
            ErrorCode.API_INCOMPATIBLE,
            "Provider rejected the API key",
            status_code=422,
        )
    if response.status_code == 429:
        raise ShioriError(ErrorCode.RATE_LIMITED, "Provider rate limit reached", status_code=429)
    if response.is_redirect:
        raise ShioriError(
            ErrorCode.API_INCOMPATIBLE,
            "Provider redirects are not allowed",
            status_code=422,
        )
    if response.status_code >= 500:
        raise ShioriError(
            ErrorCode.PROVIDER_UNREACHABLE,
            "Provider is temporarily unavailable",
            status_code=502,
        )


def _validate_chat_payload(response: httpx.Response) -> None:
    try:
        payload = response.json()
        content = payload["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise ShioriError(
            ErrorCode.API_INCOMPATIBLE,
            "Provider returned an invalid chat completions payload",
            status_code=422,
        ) from exc
    if not isinstance(content, str):
        raise ShioriError(
            ErrorCode.API_INCOMPATIBLE,
            "Provider returned an invalid chat message",
            status_code=422,
        )


async def probe_provider(
    *,
    base_url: str,
    api_key: str,
    translation_model: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    validated = await validate_public_https_base_url(base_url)
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    capabilities: dict[str, Any] = {
        "chat_completions": False,
        "models": False,
        "json_response_format": False,
        "streaming": "unprobed",
        "checked_at": datetime.now(UTC).isoformat(),
    }
    timeout = httpx.Timeout(timeout_seconds)
    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=False, headers=headers
        ) as client:
            models_response = await client.get(provider_endpoint(validated.base_url, "models"))
            _raise_provider_status(models_response)
            if models_response.status_code < 400:
                capabilities["models"] = True

            request_body = {
                "model": translation_model,
                "messages": [{"role": "user", "content": 'Return {"ok":true}.'}],
                "max_tokens": 12,
                "temperature": 0,
                "response_format": {"type": "json_object"},
            }
            chat_response = await client.post(
                provider_endpoint(validated.base_url, "chat/completions"), json=request_body
            )
            _raise_provider_status(chat_response)
            if chat_response.status_code < 400:
                _validate_chat_payload(chat_response)
                capabilities["chat_completions"] = True
                capabilities["json_response_format"] = True
            elif chat_response.status_code in {400, 404, 422}:
                request_body.pop("response_format")
                fallback = await client.post(
                    provider_endpoint(validated.base_url, "chat/completions"), json=request_body
                )
                _raise_provider_status(fallback)
                if fallback.status_code >= 400:
                    raise ShioriError(
                        ErrorCode.API_INCOMPATIBLE,
                        "Provider does not implement compatible chat completions",
                        status_code=422,
                    )
                _validate_chat_payload(fallback)
                capabilities["chat_completions"] = True
            else:
                raise ShioriError(
                    ErrorCode.API_INCOMPATIBLE,
                    "Provider returned an incompatible response",
                    status_code=422,
                )
    except ShioriError:
        raise
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        raise ShioriError(
            ErrorCode.PROVIDER_UNREACHABLE,
            "Provider connection failed",
            status_code=502,
        ) from exc
    return capabilities
