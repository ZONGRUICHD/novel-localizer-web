from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def normalize_reference_text(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip()


def content_hash(source_text: str, target_text: str, target_locale: str) -> str:
    canonical = "\x1f".join(
        [
            target_locale.lower(),
            normalize_reference_text(source_text),
            normalize_reference_text(target_text),
        ]
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def japanese_search_tokens(value: str) -> str:
    """Pre-tokenize Japanese into overlapping bigrams/trigrams for FTS5."""

    normalized = unicodedata.normalize("NFKC", value).lower()
    compact = "".join(
        character
        for character in normalized
        if not character.isspace() and not unicodedata.category(character).startswith("P")
    )
    tokens: list[str] = []
    for size in (2, 3):
        tokens.extend(
            compact[index : index + size] for index in range(max(len(compact) - size + 1, 0))
        )
    tokens.extend(re.findall(r"[a-z0-9_]{2,}", normalized))
    return " ".join(dict.fromkeys(token for token in tokens if token.strip()))


@dataclass(frozen=True, slots=True)
class ReferencePair:
    library_id: str
    target_locale: str
    source_text: str
    target_text: str = ""
    priority: int = 20
    external_allowed: bool = False
    mode: str = "paired"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ReferenceSnippet:
    id: int
    library_id: str
    source_text: str
    target_text: str
    priority: int
    score: float
    mode: str
    external_allowed: bool = False

    @property
    def total_characters(self) -> int:
        return len(self.source_text) + len(self.target_text)


class ReferenceIndex:
    """Small SQLite FTS5 translation-memory index.

    The source table owns de-duplication. FTS is a derived index and never
    carries weighting by duplicate row count.
    """

    def __init__(self, database: sqlite3.Connection | str | Path) -> None:
        self.connection = (
            database
            if isinstance(database, sqlite3.Connection)
            else sqlite3.connect(str(database), timeout=30.0)
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self._initialize()

    def _initialize(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS reference_pairs (
                id INTEGER PRIMARY KEY,
                library_id TEXT NOT NULL,
                target_locale TEXT NOT NULL,
                source_text TEXT NOT NULL,
                target_text TEXT NOT NULL DEFAULT '',
                pair_hash TEXT NOT NULL UNIQUE,
                priority INTEGER NOT NULL DEFAULT 20,
                external_allowed INTEGER NOT NULL DEFAULT 0,
                mode TEXT NOT NULL CHECK (mode IN ('paired', 'style_only')),
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS reference_fts USING fts5(
                pair_id UNINDEXED,
                search_tokens,
                tokenize='unicode61 remove_diacritics 0'
            );
            """
        )
        self.connection.commit()

    def add(self, pair: ReferencePair) -> tuple[int, bool]:
        digest = content_hash(pair.source_text, pair.target_text, pair.target_locale)
        metadata_json = json.dumps(pair.metadata, ensure_ascii=False, separators=(",", ":"))
        with self.connection:
            cursor = self.connection.execute(
                """
                INSERT OR IGNORE INTO reference_pairs
                    (library_id, target_locale, source_text, target_text, pair_hash,
                     priority, external_allowed, mode, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pair.library_id,
                    pair.target_locale,
                    pair.source_text,
                    pair.target_text,
                    digest,
                    pair.priority,
                    int(pair.external_allowed),
                    pair.mode,
                    metadata_json,
                ),
            )
            inserted = cursor.rowcount == 1
            row = self.connection.execute(
                "SELECT id FROM reference_pairs WHERE pair_hash = ?", (digest,)
            ).fetchone()
            assert row is not None
            pair_id = int(row["id"])
            if inserted:
                tokens = japanese_search_tokens(pair.source_text)
                self.connection.execute(
                    "INSERT INTO reference_fts(pair_id, search_tokens) VALUES (?, ?)",
                    (pair_id, tokens),
                )
        return pair_id, inserted

    def add_many(self, pairs: Iterable[ReferencePair]) -> tuple[int, int]:
        inserted = 0
        duplicates = 0
        for pair in pairs:
            _, was_inserted = self.add(pair)
            inserted += int(was_inserted)
            duplicates += int(not was_inserted)
        return inserted, duplicates

    def search(
        self,
        query: str,
        *,
        target_locale: str,
        library_ids: Iterable[str] | None = None,
        for_external_api: bool = True,
        limit: int = 4,
        max_side_characters: int = 300,
        max_total_characters: int = 2400,
    ) -> list[ReferenceSnippet]:
        limit = min(max(limit, 0), 4)
        tokens = japanese_search_tokens(query).split()
        if not tokens or limit == 0:
            return []
        expression = " OR ".join(
            f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens[:96]
        )
        params: list[Any] = [expression, target_locale]
        filters = ["reference_fts MATCH ?", "p.target_locale = ?"]
        if for_external_api:
            filters.append("p.external_allowed = 1")
        libraries = list(dict.fromkeys(library_ids or []))
        if libraries:
            filters.append(f"p.library_id IN ({','.join('?' for _ in libraries)})")
            params.extend(libraries)
        params.append(max(limit * 8, 24))
        rows = self.connection.execute(
            f"""
            SELECT p.*, bm25(reference_fts) AS lexical_rank
            FROM reference_fts
            JOIN reference_pairs p ON p.id = CAST(reference_fts.pair_id AS INTEGER)
            WHERE {" AND ".join(filters)}
            ORDER BY p.priority DESC, lexical_rank ASC, p.id ASC
            LIMIT ?
            """,
            params,
        ).fetchall()

        candidates: list[ReferenceSnippet] = []
        for row in rows:
            source = str(row["source_text"])[:max_side_characters]
            target = str(row["target_text"])[:max_side_characters]
            rank = float(row["lexical_rank"] or 0.0)
            score = float(row["priority"]) * 1000.0 - rank
            candidates.append(
                ReferenceSnippet(
                    id=int(row["id"]),
                    library_id=str(row["library_id"]),
                    source_text=source,
                    target_text=target,
                    priority=int(row["priority"]),
                    score=score,
                    mode=str(row["mode"]),
                    external_allowed=bool(row["external_allowed"]),
                )
            )

        selected: list[ReferenceSnippet] = []
        total = 0
        for candidate in candidates:
            if len(selected) >= limit:
                break
            remaining = max_total_characters - total
            if remaining <= 0:
                break
            source = candidate.source_text
            target = candidate.target_text
            if len(source) + len(target) > remaining:
                source_budget = min(len(source), min(max_side_characters, remaining))
                source = source[:source_budget]
                target = target[
                    : min(len(target), min(max_side_characters, remaining - len(source)))
                ]
            if not source and not target:
                continue
            bounded = ReferenceSnippet(
                id=candidate.id,
                library_id=candidate.library_id,
                source_text=source,
                target_text=target,
                priority=candidate.priority,
                score=candidate.score,
                mode=candidate.mode,
                external_allowed=candidate.external_allowed,
            )
            selected.append(bounded)
            total += bounded.total_characters
        return selected

    def count(self) -> int:
        return int(self.connection.execute("SELECT COUNT(*) FROM reference_pairs").fetchone()[0])


def build_style_profile(texts: Iterable[str]) -> dict[str, Any]:
    values = [normalize_reference_text(value) for value in texts if value.strip()]
    if not values:
        return {
            "sample_count": 0,
            "average_length": 0.0,
            "dialogue_ratio": 0.0,
            "punctuation": {},
        }
    total_characters = sum(len(value) for value in values)
    dialogue = sum(value.count("「") + value.count("『") for value in values)
    punctuation = {
        mark: sum(value.count(mark) for value in values)
        for mark in ("。", "、", "！", "？", "……", "——")
    }
    return {
        "sample_count": len(values),
        "average_length": round(total_characters / len(values), 2),
        "dialogue_ratio": round(dialogue / max(total_characters, 1), 6),
        "punctuation": punctuation,
    }
