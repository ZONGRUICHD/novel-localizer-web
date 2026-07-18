from __future__ import annotations

import asyncio

import httpx
import pytest

from shiori.errors import ErrorCode, ShioriError
from shiori.provider import probe_provider


def _public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "shiori.ssrf.socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("8.8.8.8", 443))],
    )


def test_provider_probe_detects_json_format_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    _public_dns(monkeypatch)
    real_client = httpx.AsyncClient
    chat_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal chat_calls
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "translator"}]})
        chat_calls += 1
        if chat_calls == 1:
            return httpx.Response(400, json={"error": {"message": "unsupported format"}})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"ok":true}'}}]},
        )

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "shiori.provider.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )
    capabilities = asyncio.run(
        probe_provider(
            base_url="https://provider.example.test",
            api_key="sk-secret",
            translation_model="translator",
            timeout_seconds=2,
        )
    )
    assert capabilities["models"] is True
    assert capabilities["chat_completions"] is True
    assert capabilities["json_response_format"] is False


def test_provider_probe_maps_rate_limit_to_stable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _public_dns(monkeypatch)
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda _: httpx.Response(429, json={"error": "limited"}))
    monkeypatch.setattr(
        "shiori.provider.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )
    with pytest.raises(ShioriError) as caught:
        asyncio.run(
            probe_provider(
                base_url="https://provider.example.test/v1",
                api_key="sk-secret",
                translation_model="translator",
                timeout_seconds=2,
            )
        )
    assert caught.value.code == ErrorCode.RATE_LIMITED
