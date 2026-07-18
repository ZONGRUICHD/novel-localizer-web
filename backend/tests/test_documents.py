from __future__ import annotations

import hashlib
import io
import zipfile

import fitz
import pytest
from fixtures.builders import PNG_1X1, synthetic_epub

from shiori.documents import (
    DocumentError,
    EpubLimits,
    PdfTextItem,
    export_epub,
    export_pdf,
    export_txt,
    order_pdf_text_items,
    parse_epub,
    parse_pdf,
    parse_txt,
)
from shiori.documents.errors import (
    ENCODING_CONFIRMATION_REQUIRED,
    OCR_REQUIRED,
    UNSAFE_ARCHIVE,
    UNSUPPORTED_DRM,
)


def test_txt_detects_utf8_and_cp932_and_preserves_spacing() -> None:
    original = "\n第一段。\n同じ段落。\n\n\n第二段。\n"
    utf8 = parse_txt(original.encode("utf-8"), filename="novel.txt")
    assert utf8.metadata["encoding"] == "utf-8"
    assert export_txt(utf8).decode("utf-8") == original

    cp932 = parse_txt("これは日本語です。".encode("cp932"))
    assert cp932.metadata["encoding"] == "cp932"
    assert cp932.sections[0].blocks[0].source_text == "これは日本語です。"


def test_txt_requires_confirmation_for_low_confidence_legacy_text() -> None:
    with pytest.raises(DocumentError) as captured:
        parse_txt("＝＝＝".encode("cp932"))
    assert captured.value.code == ENCODING_CONFIRMATION_REQUIRED
    confirmed = parse_txt("＝＝＝".encode("cp932"), confirmed_encoding="shift-jis")
    assert confirmed.sections[0].blocks[0].source_text == "＝＝＝"


def test_epub_uses_spine_order_extracts_ruby_toc_and_assets() -> None:
    payload = synthetic_epub()
    document = parse_epub(payload)
    assert document.title == "合成書籍"
    assert [section.title for section in document.sections] == ["第一章", "第二章"]
    assert [entry.title for entry in document.toc] == ["第一章", "第二章"]
    ruby = next(
        inline
        for block in document.sections[0].blocks
        for inline in block.inlines
        if inline.kind == "ruby"
    )
    assert ruby.text == "勝"
    assert ruby.annotation == "か"
    assert "盗む" not in "".join(block.source_text for block in document.iter_blocks())
    assert (
        document.asset_by_id(document.cover_asset_id).sha256 == hashlib.sha256(PNG_1X1).hexdigest()
    )
    assert len(document.assets) == 2

    reparsed = parse_epub(payload)
    assert [block.id for block in document.iter_blocks()] == [
        block.id for block in reparsed.iter_blocks()
    ]


def test_epub_export_is_horizontal_preserves_assets_and_translations() -> None:
    document = parse_epub(synthetic_epub())
    for index, block in enumerate(document.iter_blocks()):
        block.translations["zh-CN"] = f"译文 {index}"
    exported = export_epub(document, target_locale="zh-CN")
    with zipfile.ZipFile(io.BytesIO(exported)) as archive:
        assert archive.namelist()[0] == "mimetype"
        css = archive.read("OEBPS/styles/horizontal.css").decode("utf-8")
        assert "vertical-rl" not in css
        assert "horizontal-tb" in css
        chapters = [name for name in archive.namelist() if name.startswith("OEBPS/text/chapter-")]
        assert "译文 0" in archive.read(chapters[0]).decode("utf-8")
        cover_bytes = [
            archive.read(name)
            for name in archive.namelist()
            if name.startswith("OEBPS/assets/") and archive.read(name) == PNG_1X1
        ]
        assert cover_bytes == [PNG_1X1]
    output_document = parse_epub(exported)
    assert output_document.language == "zh-CN"
    assert [section.title for section in output_document.sections][:2] == ["译文 0", "译文 3"]


def test_epub_rejects_unknown_encryption_and_unsafe_archives() -> None:
    with pytest.raises(DocumentError) as drm:
        parse_epub(synthetic_epub(encryption_algorithm="urn:vendor:drm"))
    assert drm.value.code == UNSUPPORTED_DRM

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../escape", "bad")
    with pytest.raises(DocumentError) as unsafe:
        parse_epub(buffer.getvalue())
    assert unsafe.value.code == UNSAFE_ARCHIVE


def test_epub_rejects_expansion_over_configured_limit() -> None:
    with pytest.raises(DocumentError) as unsafe:
        parse_epub(synthetic_epub(), limits=EpubLimits(max_uncompressed_bytes=512))
    assert unsafe.value.code == UNSAFE_ARCHIVE


def test_pdf_orders_vertical_columns_right_to_left() -> None:
    items = [
        PdfTextItem("右下", 90, 30, 100, 40),
        PdfTextItem("左上", 50, 10, 60, 20),
        PdfTextItem("右上", 90, 10, 100, 20),
        PdfTextItem("左下", 50, 30, 60, 40),
    ]
    assert [item.text for item in order_pdf_text_items(items, vertical=True)] == [
        "右上",
        "右下",
        "左上",
        "左下",
    ]


def test_pdf_text_layer_parses_and_scanned_pdf_requires_ocr() -> None:
    source = fitz.open()
    page = source.new_page(width=300, height=400)
    page.insert_text((30, 40), "First line")
    page.insert_text((30, 70), "Second line")
    payload = source.tobytes()
    source.close()
    parsed = parse_pdf(payload, filename="sample.pdf")
    assert parsed.metadata["source_layout"] == "horizontal"
    assert [block.source_text for block in parsed.sections[0].blocks] == [
        "First line",
        "Second line",
    ]

    scan = fitz.open()
    scan_page = scan.new_page(width=300, height=400)
    scan_page.draw_rect(fitz.Rect(0, 0, 300, 400), color=(0, 0, 0), fill=(0.8, 0.8, 0.8))
    scan_payload = scan.tobytes()
    scan.close()
    with pytest.raises(DocumentError) as captured:
        parse_pdf(scan_payload)
    assert captured.value.code == OCR_REQUIRED


def test_all_documents_can_export_selectable_a5_pdf() -> None:
    document = parse_txt(b"Chapter one.\n\nBody text.", filename="sample.txt")
    payload = export_pdf(document)
    with fitz.open(stream=payload, filetype="pdf") as parsed:
        assert parsed.page_count >= 3
        text = "".join(page.get_text() for page in parsed)
        assert "Body text" in text
