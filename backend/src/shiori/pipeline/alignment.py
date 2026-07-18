from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AlignedPair:
    source_indices: tuple[int, ...]
    target_indices: tuple[int, ...]
    source_text: str
    target_text: str
    confidence: float
    content_hash: str

    @property
    def needs_review(self) -> bool:
        return self.confidence < 0.64


def _normalized_length(value: str) -> int:
    return len(re.sub(r"\s+", "", value))


def _dialogue_signature(value: str) -> tuple[bool, bool]:
    stripped = value.strip()
    return (
        stripped.startswith(("「", "『", "“", '"')),
        stripped.endswith(("」", "』", "”", '"')),
    )


def _match_cost(source: str, target: str) -> float:
    source_length = max(_normalized_length(source), 1)
    target_length = max(_normalized_length(target), 1)
    # Literary Chinese is usually more compact than Japanese. A broad log
    # curve is used so dialogue and headings are not over-penalized.
    expected_target = max(source_length * 0.72, 1.0)
    length_cost = min(abs(math.log(target_length / expected_target)), 2.5)
    dialogue_cost = 0.0 if _dialogue_signature(source) == _dialogue_signature(target) else 0.28
    terminal_cost = 0.0
    if bool(re.search(r"[。！？!?]$", source.strip())) != bool(
        re.search(r"[。！？!?]$", target.strip())
    ):
        terminal_cost = 0.12
    return length_cost + dialogue_cost + terminal_cost


def align_paragraphs(
    source_paragraphs: Iterable[str],
    target_paragraphs: Iterable[str],
) -> list[AlignedPair]:
    """Align paragraph sequences with 1:1, 1:2, 2:1 and skip transitions."""

    source = list(source_paragraphs)
    target = list(target_paragraphs)
    n, m = len(source), len(target)
    infinity = float("inf")
    costs = [[infinity] * (m + 1) for _ in range(n + 1)]
    previous: list[list[tuple[int, int, int, int] | None]] = [
        [None] * (m + 1) for _ in range(n + 1)
    ]
    costs[0][0] = 0.0
    transitions = ((1, 1, 0.0), (1, 2, 0.22), (2, 1, 0.22), (1, 0, 1.35), (0, 1, 1.35))
    for i in range(n + 1):
        for j in range(m + 1):
            if costs[i][j] == infinity:
                continue
            for take_source, take_target, penalty in transitions:
                ni, nj = i + take_source, j + take_target
                if ni > n or nj > m:
                    continue
                source_text = "\n".join(source[i:ni])
                target_text = "\n".join(target[j:nj])
                match = (
                    _match_cost(source_text, target_text)
                    if take_source and take_target
                    else penalty
                )
                candidate = costs[i][j] + match + (penalty if take_source and take_target else 0.0)
                if candidate < costs[ni][nj]:
                    costs[ni][nj] = candidate
                    previous[ni][nj] = (i, j, take_source, take_target)

    path: list[tuple[int, int, int, int]] = []
    i, j = n, m
    while i or j:
        step = previous[i][j]
        if step is None:
            break
        path.append(step)
        i, j = step[0], step[1]
    path.reverse()

    results: list[AlignedPair] = []
    for i, j, take_source, take_target in path:
        if not take_source or not take_target:
            continue
        source_text = "\n".join(source[i : i + take_source])
        target_text = "\n".join(target[j : j + take_target])
        cost = _match_cost(source_text, target_text) + (0.22 if take_source != take_target else 0.0)
        confidence = max(0.0, min(1.0, math.exp(-cost)))
        digest = hashlib.sha256(
            (source_text.strip() + "\x1f" + target_text.strip()).encode("utf-8")
        ).hexdigest()
        results.append(
            AlignedPair(
                source_indices=tuple(range(i, i + take_source)),
                target_indices=tuple(range(j, j + take_target)),
                source_text=source_text,
                target_text=target_text,
                confidence=confidence,
                content_hash=digest,
            )
        )
    return results
