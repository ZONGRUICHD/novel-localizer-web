from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

BlockKind = Literal[
    "heading",
    "paragraph",
    "quote",
    "list_item",
    "preformatted",
    "separator",
]


def _canonical_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def stable_identifier(prefix: str, *parts: object) -> str:
    """Build a stable identifier from source identity, location and text.

    ``ensure_ascii=False`` makes the canonical form independent from callers'
    choice of JSON escaping, while the explicit separators prevent accidental
    concatenation collisions.
    """

    payload = json.dumps(
        [str(part) for part in parts],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(payload).hexdigest()[:24]}"


@dataclass(slots=True)
class Inline:
    kind: Literal["text", "ruby", "link", "image", "line_break"] = "text"
    text: str = ""
    annotation: str | None = None
    href: str | None = None
    asset_path: str | None = None
    attributes: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class Asset:
    id: str
    path: str
    media_type: str
    data: bytes = field(repr=False)
    sha256: str
    is_cover: bool = False

    @classmethod
    def create(
        cls,
        *,
        path: str,
        media_type: str,
        data: bytes,
        is_cover: bool = False,
    ) -> Asset:
        digest = hashlib.sha256(data).hexdigest()
        return cls(
            id=stable_identifier("ast", path, digest),
            path=path,
            media_type=media_type,
            data=data,
            sha256=digest,
            is_cover=is_cover,
        )


@dataclass(slots=True)
class Block:
    id: str
    kind: BlockKind
    source_text: str
    locator: str
    inlines: list[Inline] = field(default_factory=list)
    translations: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        source_hash: str,
        section_locator: str,
        locator: str,
        kind: BlockKind,
        source_text: str,
        inlines: Iterable[Inline] = (),
        metadata: dict[str, Any] | None = None,
    ) -> Block:
        normalized = _canonical_text(source_text)
        return cls(
            id=stable_identifier("blk", source_hash, section_locator, locator, normalized),
            kind=kind,
            source_text=source_text,
            locator=locator,
            inlines=list(inlines),
            metadata=dict(metadata or {}),
        )

    def text_for(self, locale: str | None) -> str:
        if locale:
            translated = self.translations.get(locale)
            if translated is not None:
                return translated
        return self.source_text


@dataclass(slots=True)
class Section:
    id: str
    title: str
    locator: str
    blocks: list[Block] = field(default_factory=list)
    href: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        source_hash: str,
        locator: str,
        title: str = "",
        blocks: Iterable[Block] = (),
        href: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Section:
        return cls(
            id=stable_identifier("sec", source_hash, locator),
            title=title,
            locator=locator,
            blocks=list(blocks),
            href=href,
            metadata=dict(metadata or {}),
        )


@dataclass(slots=True)
class TocEntry:
    title: str
    section_id: str
    fragment: str | None = None
    children: list[TocEntry] = field(default_factory=list)


@dataclass(slots=True)
class BookDocument:
    id: str
    title: str
    language: str
    source_format: Literal["epub", "txt", "pdf"]
    source_hash: str
    sections: list[Section] = field(default_factory=list)
    assets: list[Asset] = field(default_factory=list)
    toc: list[TocEntry] = field(default_factory=list)
    cover_asset_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        source_bytes: bytes,
        source_format: Literal["epub", "txt", "pdf"],
        title: str = "",
        language: str = "ja",
        sections: Iterable[Section] = (),
        assets: Iterable[Asset] = (),
        toc: Iterable[TocEntry] = (),
        cover_asset_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> BookDocument:
        source_hash = hashlib.sha256(source_bytes).hexdigest()
        return cls(
            id=stable_identifier("book", source_hash),
            title=title,
            language=language,
            source_format=source_format,
            source_hash=source_hash,
            sections=list(sections),
            assets=list(assets),
            toc=list(toc),
            cover_asset_id=cover_asset_id,
            metadata=dict(metadata or {}),
        )

    def iter_blocks(self) -> Iterable[Block]:
        for section in self.sections:
            yield from section.blocks

    def asset_by_id(self, asset_id: str | None) -> Asset | None:
        if asset_id is None:
            return None
        return next((asset for asset in self.assets if asset.id == asset_id), None)

    def asset_by_path(self, path: str) -> Asset | None:
        return next((asset for asset in self.assets if asset.path == path), None)


def document_to_dict(document: BookDocument) -> dict[str, Any]:
    """Serialize a document for checkpoints without silently decoding assets."""

    result = asdict(document)
    for asset in result["assets"]:
        asset["data"] = asset["data"].hex()
    return result


def document_from_dict(value: dict[str, Any]) -> BookDocument:
    sections: list[Section] = []
    for raw_section in value.get("sections", []):
        blocks: list[Block] = []
        for raw_block in raw_section.get("blocks", []):
            raw_block = dict(raw_block)
            raw_block["inlines"] = [Inline(**inline) for inline in raw_block.get("inlines", [])]
            blocks.append(Block(**raw_block))
        raw_section = dict(raw_section)
        raw_section["blocks"] = blocks
        sections.append(Section(**raw_section))

    assets = []
    for raw_asset in value.get("assets", []):
        raw_asset = dict(raw_asset)
        raw_asset["data"] = bytes.fromhex(raw_asset["data"])
        assets.append(Asset(**raw_asset))

    def parse_toc(entries: list[dict[str, Any]]) -> list[TocEntry]:
        return [
            TocEntry(
                title=entry["title"],
                section_id=entry["section_id"],
                fragment=entry.get("fragment"),
                children=parse_toc(entry.get("children", [])),
            )
            for entry in entries
        ]

    raw_document = dict(value)
    raw_document["sections"] = sections
    raw_document["assets"] = assets
    raw_document["toc"] = parse_toc(value.get("toc", []))
    return BookDocument(**raw_document)
