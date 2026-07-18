from __future__ import annotations

import ipaddress
import json
import random
import re
import socket
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from shiori.documents.errors import API_INCOMPATIBLE, RATE_LIMITED, DocumentError
from shiori.documents.model import Block
from shiori.ssrf import ValidatedEndpoint, provider_endpoint

from .quality import detect_reference_copying
from .retrieval import ReferenceSnippet

Message = dict[str, str]
CheckpointCallback = Callable[[str, dict[str, Any]], None]


@dataclass(frozen=True, slots=True)
class ChatCompletionConfig:
    endpoint: ValidatedEndpoint
    api_key: str
    timeout_seconds: float = 60.0
    max_attempts: int = 4
    supports_json_response_format: bool = True

    def __post_init__(self) -> None:
        _validate_endpoint_shape(self.endpoint)


def _is_public_ip(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return bool(ip.is_global) and not any(
        (
            ip.is_private,
            ip.is_loopback,
            ip.is_link_local,
            ip.is_multicast,
            ip.is_reserved,
            ip.is_unspecified,
        )
    )


def _validate_endpoint_shape(endpoint: ValidatedEndpoint) -> None:
    if not isinstance(endpoint, ValidatedEndpoint):
        raise TypeError("ChatCompletionConfig.endpoint must be a ValidatedEndpoint")
    parsed = urlsplit(endpoint.base_url)
    hostname = (parsed.hostname or "").rstrip(".").lower()
    if (
        parsed.scheme.lower() != "https"
        or not hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or hostname != endpoint.hostname.rstrip(".").lower()
        or not endpoint.addresses
    ):
        raise DocumentError(API_INCOMPATIBLE, "Provider endpoint validation is missing or invalid.")
    try:
        public = all(_is_public_ip(address) for address in endpoint.addresses)
    except ValueError as exc:
        raise DocumentError(
            API_INCOMPATIBLE, "Provider endpoint has an invalid pinned address."
        ) from exc
    if not public:
        raise DocumentError(
            API_INCOMPATIBLE, "Provider endpoint is not pinned to public addresses."
        )


def validate_pinned_endpoint(
    endpoint: ValidatedEndpoint,
    *,
    resolver: Callable[..., list[tuple[Any, ...]]] = socket.getaddrinfo,
) -> None:
    """Fail closed if DNS no longer matches the already validated public set."""

    _validate_endpoint_shape(endpoint)
    parsed = urlsplit(endpoint.base_url)
    try:
        infos = resolver(
            endpoint.hostname,
            parsed.port or 443,
            type=socket.SOCK_STREAM,
        )
        current = {str(info[4][0]) for info in infos}
    except (OSError, IndexError, TypeError) as exc:
        raise DocumentError(
            API_INCOMPATIBLE, "Provider endpoint could not be revalidated."
        ) from exc
    pinned = set(endpoint.addresses)
    try:
        all_public = bool(current) and all(_is_public_ip(address) for address in current)
    except ValueError as exc:
        raise DocumentError(API_INCOMPATIBLE, "Provider DNS returned an invalid address.") from exc
    if not all_public or not current.issubset(pinned):
        raise DocumentError(
            API_INCOMPATIBLE,
            "Provider DNS no longer matches the validated public endpoint.",
        )


class OpenAICompatibleChatClient:
    """Minimal OpenAI-compatible Chat Completions client.

    Base URL network validation is performed by the provider settings layer;
    this class only constructs the standardized endpoint and never logs keys or
    response bodies.
    """

    def __init__(
        self,
        config: ChatCompletionConfig,
        *,
        client: httpx.Client | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        random_source: random.Random | None = None,
        endpoint_validator: Callable[[ValidatedEndpoint], None] = validate_pinned_endpoint,
    ) -> None:
        self.config = config
        self._owned_client = client is None
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(config.timeout_seconds),
            follow_redirects=False,
        )
        self.sleeper = sleeper
        self.random = random_source or random.Random()
        self.endpoint_validator = endpoint_validator
        self.endpoint_validator(self.config.endpoint)

    @property
    def endpoint(self) -> str:
        parsed = urlsplit(self.config.endpoint.base_url)
        if parsed.path.rstrip("/").endswith("/chat/completions"):
            return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))
        return provider_endpoint(self.config.endpoint.base_url, "chat/completions")

    def close(self) -> None:
        if self._owned_client:
            self.client.close()

    def __enter__(self) -> OpenAICompatibleChatClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def complete(
        self,
        *,
        model: str,
        messages: Sequence[Message],
        temperature: float = 0.2,
        json_response: bool = True,
    ) -> str:
        # Revalidate at the last possible point before opening a connection.
        # The strict address pin prevents a stored hostname from rebinding to a
        # private or metadata address after the provider connection test.
        self.endpoint_validator(self.config.endpoint)
        payload: dict[str, Any] = {
            "model": model,
            "messages": list(messages),
            "temperature": temperature,
            "stream": False,
        }
        if json_response and self.config.supports_json_response_format:
            payload["response_format"] = {"type": "json_object"}

        last_status: int | None = None
        for attempt in range(self.config.max_attempts):
            try:
                response = self.client.post(
                    self.endpoint,
                    headers={
                        "Authorization": f"Bearer {self.config.api_key}",
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                    json=payload,
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt + 1 >= self.config.max_attempts:
                    raise DocumentError(
                        API_INCOMPATIBLE,
                        "The translation provider did not respond in time.",
                        {"kind": type(exc).__name__},
                    ) from exc
                self._backoff(attempt)
                continue

            last_status = response.status_code
            if response.is_redirect:
                raise DocumentError(
                    API_INCOMPATIBLE,
                    "Provider redirects are not allowed.",
                    {"status": response.status_code},
                )
            if response.status_code == 429:
                if attempt + 1 >= self.config.max_attempts:
                    raise DocumentError(
                        RATE_LIMITED,
                        "The translation provider is rate limiting requests.",
                        {"status": 429},
                    )
                retry_after = response.headers.get("retry-after")
                try:
                    delay = min(float(retry_after), 30.0) if retry_after else None
                except ValueError:
                    delay = None
                self._backoff(attempt, delay=delay)
                continue
            if 500 <= response.status_code < 600:
                if attempt + 1 >= self.config.max_attempts:
                    break
                self._backoff(attempt)
                continue
            if response.status_code < 200 or response.status_code >= 300:
                raise DocumentError(
                    API_INCOMPATIBLE,
                    "The provider rejected the Chat Completions request.",
                    {"status": response.status_code},
                )
            try:
                body = response.json()
                content = body["choices"][0]["message"]["content"]
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                raise DocumentError(
                    API_INCOMPATIBLE,
                    "The provider returned an incompatible Chat Completions response.",
                    {"status": response.status_code},
                ) from exc
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                text_parts = [
                    part.get("text", "")
                    for part in content
                    if isinstance(part, dict) and part.get("type") in {"text", "output_text"}
                ]
                if text_parts:
                    return "".join(text_parts)
            raise DocumentError(
                API_INCOMPATIBLE,
                "The provider returned a non-text message.",
            )
        raise DocumentError(
            API_INCOMPATIBLE,
            "The translation provider is temporarily unavailable.",
            {"status": last_status},
        )

    def _backoff(self, attempt: int, *, delay: float | None = None) -> None:
        wait = delay if delay is not None else min(0.75 * (2**attempt), 20.0)
        wait += self.random.random() * min(wait * 0.2, 0.75)
        self.sleeper(wait)


@dataclass(slots=True)
class TranslationResult:
    block_id: str
    source_text: str
    draft_text: str
    reviewed_text: str
    final_text: str
    quality_flags: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ChapterTranslation:
    locale: str
    results: list[TranslationResult]
    awaiting_review: bool = False
    provider_requests: int = 0


class StructuredOutputError(ValueError):
    pass


def _parse_json_object(raw: str) -> dict[str, Any]:
    value = raw.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", value, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        value = fenced.group(1)
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise StructuredOutputError("response is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise StructuredOutputError("response root must be an object")
    return parsed


def _extract_translations(raw: str, expected_ids: Sequence[str]) -> dict[str, str]:
    parsed = _parse_json_object(raw)
    translations = parsed.get("translations")
    if not isinstance(translations, list):
        raise StructuredOutputError("translations must be an array")
    values: dict[str, str] = {}
    for item in translations:
        if not isinstance(item, dict):
            raise StructuredOutputError("each translation must be an object")
        block_id = item.get("block_id")
        text = item.get("text")
        if not isinstance(block_id, str) or not isinstance(text, str):
            raise StructuredOutputError("block_id and text must be strings")
        if block_id in values:
            raise StructuredOutputError("duplicate block_id")
        values[block_id] = text
    if set(values) != set(expected_ids):
        missing = sorted(set(expected_ids) - set(values))
        unexpected = sorted(set(values) - set(expected_ids))
        raise StructuredOutputError(f"block IDs differ; missing={missing}; unexpected={unexpected}")
    return values


def _bounded_references(references: Iterable[ReferenceSnippet]) -> list[dict[str, str]]:
    bounded: list[dict[str, str]] = []
    total = 0
    for reference in list(references)[:4]:
        if not reference.external_allowed:
            continue
        source = reference.source_text[:300]
        target = reference.target_text[:300]
        remaining = 2400 - total
        if remaining <= 0:
            break
        if len(source) + len(target) > remaining:
            source = source[: min(300, remaining)]
            target = target[: min(300, max(remaining - len(source), 0))]
        if source or target:
            bounded.append({"source": source, "target": target})
            total += len(source) + len(target)
    return bounded


class TwoPassTranslator:
    def __init__(
        self,
        client: OpenAICompatibleChatClient,
        *,
        translation_model: str,
        review_model: str,
        max_batch_characters: int = 6000,
        structured_repairs: int = 2,
    ) -> None:
        self.client = client
        self.translation_model = translation_model
        self.review_model = review_model
        self.max_batch_characters = max_batch_characters
        self.structured_repairs = structured_repairs
        self._request_count = 0

    def translate_chapter(
        self,
        blocks: Sequence[Block],
        *,
        locale: str,
        references: Iterable[ReferenceSnippet] = (),
        terminology: Mapping[str, str] | None = None,
        style_profile: Mapping[str, Any] | None = None,
        checkpoint: CheckpointCallback | None = None,
    ) -> ChapterTranslation:
        if locale not in {"zh-CN", "zh-TW"}:
            raise ValueError("locale must be zh-CN or zh-TW")
        active_blocks = [block for block in blocks if block.source_text.strip()]
        bounded_references = _bounded_references(references)
        result_by_id: dict[str, TranslationResult] = {}
        awaiting_review = False

        for batch_index, batch in enumerate(self._batches(active_blocks)):
            expected_ids = [block.id for block in batch]
            draft = self._structured_call(
                model=self.translation_model,
                messages=self._translation_messages(
                    batch,
                    locale=locale,
                    references=bounded_references,
                    terminology=terminology or {},
                    style_profile=style_profile or {},
                ),
                expected_ids=expected_ids,
            )
            if checkpoint:
                checkpoint(
                    "translated",
                    {"batch": batch_index, "locale": locale, "translations": draft},
                )

            reviewed = self._structured_call(
                model=self.review_model,
                messages=self._review_messages(
                    batch,
                    draft=draft,
                    locale=locale,
                    terminology=terminology or {},
                ),
                expected_ids=expected_ids,
            )
            if checkpoint:
                checkpoint(
                    "reviewed",
                    {"batch": batch_index, "locale": locale, "translations": reviewed},
                )

            target_references = [
                reference["target"] for reference in bounded_references if reference["target"]
            ]
            suspicious_ids = [
                block.id
                for block in batch
                if any(
                    finding.suspicious
                    for finding in detect_reference_copying(reviewed[block.id], target_references)
                )
            ]
            final = dict(reviewed)
            flags: dict[str, list[str]] = {block.id: [] for block in batch}
            if suspicious_ids:
                suspicious_blocks = [block for block in batch if block.id in suspicious_ids]
                rewritten = self._structured_call(
                    model=self.review_model,
                    messages=self._rewrite_messages(
                        suspicious_blocks,
                        current=reviewed,
                        locale=locale,
                    ),
                    expected_ids=suspicious_ids,
                )
                final.update(rewritten)
                for block_id in suspicious_ids:
                    if any(
                        finding.suspicious
                        for finding in detect_reference_copying(final[block_id], target_references)
                    ):
                        flags[block_id].append("REFERENCE_OVERLAP")
                        awaiting_review = True
                if checkpoint:
                    checkpoint(
                        "rewritten",
                        {"batch": batch_index, "locale": locale, "translations": rewritten},
                    )

            for block in batch:
                result_by_id[block.id] = TranslationResult(
                    block_id=block.id,
                    source_text=block.source_text,
                    draft_text=draft[block.id],
                    reviewed_text=reviewed[block.id],
                    final_text=final[block.id],
                    quality_flags=flags[block.id],
                )

        return ChapterTranslation(
            locale=locale,
            results=[result_by_id[block.id] for block in active_blocks],
            awaiting_review=awaiting_review,
            provider_requests=self._request_count,
        )

    def _batches(self, blocks: Sequence[Block]) -> Iterable[list[Block]]:
        batch: list[Block] = []
        characters = 0
        for block in blocks:
            length = len(block.source_text)
            if batch and characters + length > self.max_batch_characters:
                yield batch
                batch = []
                characters = 0
            batch.append(block)
            characters += length
        if batch:
            yield batch

    def _structured_call(
        self,
        *,
        model: str,
        messages: list[Message],
        expected_ids: Sequence[str],
    ) -> dict[str, str]:
        current_messages = list(messages)
        last_error: Exception | None = None
        for repair_index in range(self.structured_repairs + 1):
            raw = self.client.complete(model=model, messages=current_messages, json_response=True)
            self._request_count += 1
            try:
                return _extract_translations(raw, expected_ids)
            except StructuredOutputError as exc:
                last_error = exc
                if repair_index >= self.structured_repairs:
                    break
                current_messages = [
                    *messages,
                    {"role": "assistant", "content": raw[:12_000]},
                    {
                        "role": "user",
                        "content": (
                            "The previous response violated the schema. Return only one JSON object "
                            f"with exactly these block IDs: {json.dumps(list(expected_ids))}. "
                            f"Validation error: {exc}"
                        ),
                    },
                ]
        raise DocumentError(
            API_INCOMPATIBLE,
            "The provider could not return translations keyed by stable Block ID.",
            {"reason": str(last_error)},
        ) from last_error

    @staticmethod
    def _translation_messages(
        blocks: Sequence[Block],
        *,
        locale: str,
        references: list[dict[str, str]],
        terminology: Mapping[str, str],
        style_profile: Mapping[str, Any],
    ) -> list[Message]:
        regional_rule = (
            "Use natural Mainland Simplified Chinese and mainland publishing punctuation."
            if locale == "zh-CN"
            else "Use natural Taiwan Traditional Chinese, Taiwan terminology, and Taiwan publishing punctuation. Translate directly from Japanese."
        )
        payload = {
            "locale": locale,
            "blocks": [{"block_id": block.id, "source": block.source_text} for block in blocks],
            "terminology": dict(terminology),
            "style_profile": dict(style_profile),
            "reference_pairs": references,
        }
        return [
            {
                "role": "system",
                "content": (
                    "You are a literary Japanese-to-Chinese translator. Preserve facts, voice, "
                    "paragraph boundaries, names, jokes, and ambiguity. Do not copy reference wording "
                    "unless required by the terminology. "
                    + regional_rule
                    + ' Return JSON only: {"translations":[{"block_id":"...","text":"..."}]} with every input ID exactly once.'
                ),
            },
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            },
        ]

    @staticmethod
    def _review_messages(
        blocks: Sequence[Block],
        *,
        draft: Mapping[str, str],
        locale: str,
        terminology: Mapping[str, str],
    ) -> list[Message]:
        payload = {
            "locale": locale,
            "terminology": dict(terminology),
            "blocks": [
                {
                    "block_id": block.id,
                    "japanese": block.source_text,
                    "draft": draft[block.id],
                }
                for block in blocks
            ],
        }
        return [
            {
                "role": "system",
                "content": (
                    "Act as a conservative bilingual literary editor. Compare every draft against "
                    "the Japanese and change only clear mistranslations, omissions, terminology, "
                    "regional usage, punctuation, or broken Chinese. Never introduce facts. Return "
                    "JSON only with translations[{block_id,text}], every supplied ID exactly once."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            },
        ]

    @staticmethod
    def _rewrite_messages(
        blocks: Sequence[Block],
        *,
        current: Mapping[str, str],
        locale: str,
    ) -> list[Message]:
        payload = {
            "locale": locale,
            "blocks": [
                {
                    "block_id": block.id,
                    "japanese": block.source_text,
                    "current": current[block.id],
                }
                for block in blocks
            ],
        }
        return [
            {
                "role": "system",
                "content": (
                    "Rewrite the Chinese independently from the Japanese because the current wording "
                    "overlaps too closely with reference material. Preserve meaning, names and voice; "
                    "do not mention the rewrite. Return JSON only with translations[{block_id,text}]."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            },
        ]
