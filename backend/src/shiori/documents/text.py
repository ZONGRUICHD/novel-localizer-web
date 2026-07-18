from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .errors import ENCODING_CONFIRMATION_REQUIRED, DocumentError
from .model import Block, BookDocument, Inline, Section, stable_identifier

_JAPANESE_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _normalize_encoding_name(value: str) -> str:
    normalized = value.lower().replace("_", "-")
    aliases = {
        "shift-jis": "cp932",
        "shiftjis": "cp932",
        "sjis": "cp932",
        "windows-31j": "cp932",
        "utf8": "utf-8",
        "utf-8-sig": "utf-8-sig",
    }
    return aliases.get(normalized, normalized)


def detect_txt_encoding(data: bytes, *, confirmed_encoding: str | None = None) -> tuple[str, str]:
    """Return decoded text and a canonical encoding name.

    Detection is deliberately conservative. Japanese legacy encodings have a
    large byte domain, so "it decoded" is not treated as enough evidence.
    Callers can resume with ``confirmed_encoding`` after presenting a preview.
    """

    if confirmed_encoding:
        encoding = _normalize_encoding_name(confirmed_encoding)
        try:
            return data.decode(encoding), encoding
        except (LookupError, UnicodeDecodeError) as exc:
            raise DocumentError(
                ENCODING_CONFIRMATION_REQUIRED,
                "指定された文字コードでは読み取れませんでした。",
                {"encoding": encoding},
            ) from exc

    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig"), "utf-8-sig"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16"), "utf-16"

    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass

    try:
        decoded = data.decode("cp932")
    except UnicodeDecodeError as exc:
        raise DocumentError(
            ENCODING_CONFIRMATION_REQUIRED,
            "文字コードを自動判定できませんでした。プレビューを確認してください。",
            {"candidates": ["cp932", "shift_jis", "utf-8"]},
        ) from exc

    visible = re.sub(r"\s", "", decoded)
    japanese_ratio = len(_JAPANESE_RE.findall(visible)) / len(visible) if visible else 1.0
    control_ratio = len(_CONTROL_RE.findall(decoded)) / max(len(decoded), 1)
    if japanese_ratio < 0.02 or control_ratio > 0.001:
        raise DocumentError(
            ENCODING_CONFIRMATION_REQUIRED,
            "CP932 の可能性がありますが、確信度が低いため確認が必要です。",
            {
                "candidate": "cp932",
                "preview": decoded[:240],
                "japanese_ratio": round(japanese_ratio, 4),
            },
        )
    return decoded, "cp932"


def parse_txt(
    source: bytes | bytearray | str | Path,
    *,
    filename: str | None = None,
    confirmed_encoding: str | None = None,
    language: str = "ja",
) -> BookDocument:
    if isinstance(source, (str, Path)):
        path = Path(source)
        data = path.read_bytes()
        filename = filename or path.name
    else:
        data = bytes(source)

    text, encoding = detect_txt_encoding(data, confirmed_encoding=confirmed_encoding)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    source_hash = hashlib.sha256(data).hexdigest()
    locator = "txt:body"

    leading_match = re.match(r"^\n+", text)
    trailing_match = re.search(r"\n+$", text)
    leading = leading_match.group(0) if leading_match else ""
    trailing = trailing_match.group(0) if trailing_match else ""
    start = len(leading)
    end = len(text) - len(trailing) if trailing else len(text)
    body = text[start:end]

    blocks: list[Block] = []
    parts = re.split(r"(\n{2,})", body)
    block_index = 0
    for part_index in range(0, len(parts), 2):
        paragraph = parts[part_index]
        if paragraph == "" and part_index == len(parts) - 1:
            continue
        separator = parts[part_index + 1] if part_index + 1 < len(parts) else ""
        block = Block.create(
            source_hash=source_hash,
            section_locator=locator,
            locator=f"paragraph:{block_index}",
            kind="paragraph",
            source_text=paragraph,
            inlines=[Inline(text=paragraph)],
            metadata={"separator_after": separator},
        )
        blocks.append(block)
        block_index += 1

    if not blocks:
        blocks.append(
            Block.create(
                source_hash=source_hash,
                section_locator=locator,
                locator="paragraph:0",
                kind="paragraph",
                source_text="",
                metadata={"separator_after": ""},
            )
        )

    title = Path(filename).stem if filename else ""
    section = Section.create(
        source_hash=source_hash,
        locator=locator,
        title=title,
        blocks=blocks,
    )
    return BookDocument(
        id=stable_identifier("book", source_hash),
        title=title,
        language=language,
        source_format="txt",
        source_hash=source_hash,
        sections=[section],
        metadata={
            "encoding": encoding,
            "leading_newlines": leading,
            "trailing_newlines": trailing,
        },
    )


def export_txt(document: BookDocument, *, target_locale: str | None = None) -> bytes:
    pieces: list[str] = [str(document.metadata.get("leading_newlines", ""))]
    for section_index, section in enumerate(document.sections):
        if section_index and pieces and not pieces[-1].endswith("\n\n"):
            pieces.append("\n\n")
        for block_index, block in enumerate(section.blocks):
            pieces.append(block.text_for(target_locale))
            separator = block.metadata.get("separator_after")
            if isinstance(separator, str):
                pieces.append(separator)
            elif block_index < len(section.blocks) - 1:
                pieces.append("\n\n")
    pieces.append(str(document.metadata.get("trailing_newlines", "")))
    return "".join(pieces).encode("utf-8")
