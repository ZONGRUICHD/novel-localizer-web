from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CopyingFinding:
    reference_index: int
    longest_common_characters: int
    longest_common_ratio: float
    ngram_overlap_ratio: float
    suspicious: bool


def _normalize(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).lower()
    return re.sub(r"[\s\W_]+", "", normalized, flags=re.UNICODE)


def longest_common_substring_length(left: str, right: str) -> int:
    left, right = _normalize(left), _normalize(right)
    if len(left) < len(right):
        left, right = right, left
    previous = [0] * (len(right) + 1)
    longest = 0
    for left_character in left:
        current = [0]
        for index, right_character in enumerate(right, start=1):
            length = previous[index - 1] + 1 if left_character == right_character else 0
            current.append(length)
            longest = max(longest, length)
        previous = current
    return longest


def _ngrams(value: str, size: int) -> set[str]:
    normalized = _normalize(value)
    return {normalized[index : index + size] for index in range(max(len(normalized) - size + 1, 0))}


def detect_reference_copying(
    translated_text: str,
    reference_texts: Iterable[str],
    *,
    ngram_size: int = 8,
) -> list[CopyingFinding]:
    translated = _normalize(translated_text)
    output_ngrams = _ngrams(translated, ngram_size)
    findings: list[CopyingFinding] = []
    for reference_index, reference in enumerate(reference_texts):
        normalized_reference = _normalize(reference)
        if not translated or not normalized_reference:
            continue
        common = longest_common_substring_length(translated, normalized_reference)
        denominator = max(min(len(translated), len(normalized_reference)), 1)
        common_ratio = common / denominator
        reference_ngrams = _ngrams(normalized_reference, ngram_size)
        overlap = (
            len(output_ngrams & reference_ngrams) / max(len(output_ngrams), 1)
            if output_ngrams
            else 0.0
        )
        if len(translated) < 80:
            suspicious = common >= 28 and common_ratio >= 0.58
        elif len(translated) < 200:
            suspicious = (common >= 42 and common_ratio >= 0.40) or overlap >= 0.55
        else:
            suspicious = (common >= 64 and common_ratio >= 0.30) or overlap >= 0.44
        findings.append(
            CopyingFinding(
                reference_index=reference_index,
                longest_common_characters=common,
                longest_common_ratio=round(common_ratio, 6),
                ngram_overlap_ratio=round(overlap, 6),
                suspicious=suspicious,
            )
        )
    return findings
