from __future__ import annotations

import hashlib
import html
import io
import os
import re
import statistics
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import fitz

from .errors import (
    EXPORT_VALIDATION_FAILED,
    OCR_REQUIRED,
    UNSUPPORTED_DRM,
    UNSUPPORTED_FORMAT,
    DocumentError,
)
from .model import Block, BookDocument, Inline, Section, stable_identifier


@dataclass(frozen=True, slots=True)
class PdfTextItem:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    page: int = 0
    direction_x: float = 1.0
    direction_y: float = 0.0

    @property
    def width(self) -> float:
        return max(self.x1 - self.x0, 0.0)

    @property
    def height(self) -> float:
        return max(self.y1 - self.y0, 0.0)


def _cluster_columns(
    items: list[PdfTextItem], tolerance: float | None = None
) -> list[list[PdfTextItem]]:
    if not items:
        return []
    widths = [item.width for item in items if item.width > 0]
    tolerance = tolerance or max((statistics.median(widths) if widths else 8.0) * 0.8, 3.0)
    columns: list[list[PdfTextItem]] = []
    for item in sorted(items, key=lambda value: (-value.x0, value.y0)):
        target = next(
            (
                column
                for column in columns
                if abs(statistics.mean(value.x0 for value in column) - item.x0) <= tolerance
            ),
            None,
        )
        if target is None:
            columns.append([item])
        else:
            target.append(item)
    columns.sort(key=lambda column: -statistics.mean(item.x0 for item in column))
    for column in columns:
        column.sort(key=lambda item: (item.y0, -item.x0))
    return columns


def order_pdf_text_items(
    items: Iterable[PdfTextItem],
    *,
    vertical: bool,
) -> list[PdfTextItem]:
    values = list(items)
    if vertical:
        return [item for column in _cluster_columns(values) for item in column]
    return sorted(values, key=lambda item: (round(item.y0 / 3.0), item.x0, item.y0))


def _extract_page_items(page: fitz.Page, page_index: int) -> list[PdfTextItem]:
    raw = page.get_text("dict", sort=False)
    items: list[PdfTextItem] = []
    for block in raw.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            text = "".join(str(span.get("text", "")) for span in spans)
            if not text.strip():
                continue
            bbox = line.get("bbox") or block.get("bbox")
            if not bbox or len(bbox) != 4:
                continue
            direction = line.get("dir") or (1.0, 0.0)
            items.append(
                PdfTextItem(
                    text=text,
                    x0=float(bbox[0]),
                    y0=float(bbox[1]),
                    x1=float(bbox[2]),
                    y1=float(bbox[3]),
                    page=page_index,
                    direction_x=float(direction[0]),
                    direction_y=float(direction[1]),
                )
            )
    return items


def _page_is_vertical(items: list[PdfTextItem]) -> bool:
    if not items:
        return False
    directional = sum(
        len(item.text) for item in items if abs(item.direction_y) > abs(item.direction_x)
    )
    total = sum(len(item.text) for item in items)
    if total and directional / total >= 0.35:
        return True
    tall = sum(
        len(item.text)
        for item in items
        if item.height > max(item.width * 1.6, 12.0) and len(item.text.strip()) > 1
    )
    return bool(total and tall / total >= 0.55)


def parse_pdf(
    source: bytes | bytearray | str | Path,
    *,
    filename: str | None = None,
    language: str = "ja",
) -> BookDocument:
    if isinstance(source, (str, Path)):
        path = Path(source)
        data = path.read_bytes()
        filename = filename or path.name
    else:
        data = bytes(source)
    source_hash = hashlib.sha256(data).hexdigest()
    try:
        pdf = fitz.open(stream=data, filetype="pdf")
    except (fitz.FileDataError, RuntimeError, ValueError) as exc:
        raise DocumentError(UNSUPPORTED_FORMAT, "The input is not a valid PDF.") from exc
    with pdf:
        if pdf.needs_pass:
            raise DocumentError(UNSUPPORTED_DRM, "Password-protected PDFs are not supported.")
        sections: list[Section] = []
        all_characters: list[str] = []
        page_layouts: list[str] = []
        pages_without_text = 0
        for page_index, page in enumerate(pdf):
            items = _extract_page_items(page, page_index)
            if not items:
                pages_without_text += 1
                continue
            vertical = _page_is_vertical(items)
            ordered = order_pdf_text_items(items, vertical=vertical)
            page_layouts.append("vertical" if vertical else "horizontal")
            locator = f"page:{page_index + 1}"
            blocks: list[Block] = []
            for item_index, item in enumerate(ordered):
                text = item.text.strip()
                if not text:
                    continue
                all_characters.append(text)
                coordinate = f"{item.x0:.2f},{item.y0:.2f},{item.x1:.2f},{item.y1:.2f}"
                blocks.append(
                    Block.create(
                        source_hash=source_hash,
                        section_locator=locator,
                        locator=f"line:{item_index}:{coordinate}",
                        kind="paragraph",
                        source_text=text,
                        inlines=[Inline(text=text)],
                        metadata={
                            "bbox": [item.x0, item.y0, item.x1, item.y1],
                            "source_layout": "vertical" if vertical else "horizontal",
                        },
                    )
                )
            sections.append(
                Section.create(
                    source_hash=source_hash,
                    locator=locator,
                    title=f"Page {page_index + 1}",
                    blocks=blocks,
                    metadata={"page": page_index + 1, "source_layout": page_layouts[-1]},
                )
            )

        extracted = "".join(all_characters)
        replacement_ratio = extracted.count("\ufffd") / max(len(extracted), 1)
        control_ratio = len(re.findall(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", extracted)) / max(
            len(extracted), 1
        )
        if not extracted.strip() or replacement_ratio > 0.10 or control_ratio > 0.01:
            raise DocumentError(
                OCR_REQUIRED,
                "PDF に信頼できる文字レイヤーがありません。OCR が必要です。",
                {
                    "pages": pdf.page_count,
                    "pages_without_text": pages_without_text,
                    "replacement_ratio": round(replacement_ratio, 4),
                },
            )

        if page_layouts and all(layout == "vertical" for layout in page_layouts):
            layout: Literal["vertical", "horizontal", "mixed"] = "vertical"
        elif page_layouts and all(layout == "horizontal" for layout in page_layouts):
            layout = "horizontal"
        else:
            layout = "mixed"
        title = Path(filename).stem if filename else ""
        return BookDocument(
            id=stable_identifier("book", source_hash),
            title=title,
            language=language,
            source_format="pdf",
            source_hash=source_hash,
            sections=sections,
            metadata={
                "page_count": pdf.page_count,
                "source_layout": layout,
                "pages_without_text": pages_without_text,
            },
        )


def _font_candidates(locale: str) -> list[Path]:
    configured = (
        os.environ.get("SHIORI_CJK_FONT_TC")
        if locale.lower() == "zh-tw"
        else os.environ.get("SHIORI_CJK_FONT_SC")
    )
    candidates: list[str] = []
    if configured:
        candidates.append(configured)
    if locale.lower() == "zh-tw":
        candidates.extend(
            [
                "/usr/share/fonts/opentype/noto/NotoSerifCJKtc-Regular.otf",
                "/usr/share/fonts/truetype/noto/NotoSerifTC-Regular.ttf",
                r"C:\Windows\Fonts\NotoSerifTC-Regular.ttf",
                r"C:\Windows\Fonts\mingliu.ttc",
            ]
        )
    else:
        candidates.extend(
            [
                "/usr/share/fonts/opentype/noto/NotoSerifCJKsc-Regular.otf",
                "/usr/share/fonts/truetype/noto/NotoSerifSC-Regular.ttf",
                "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
                r"C:\Windows\Fonts\NotoSansSC-VF.ttf",
                r"C:\Windows\Fonts\simsunb.ttf",
                r"C:\Windows\Fonts\simsun.ttc",
            ]
        )
    return [Path(path) for path in candidates]


def find_cjk_font(locale: str, *, configured_path: str | Path | None = None) -> Path | None:
    candidates = [Path(configured_path)] if configured_path else _font_candidates(locale)
    return next((path for path in candidates if path.is_file()), None)


def _contains_cjk(value: str) -> bool:
    return bool(re.search(r"[\u3040-\u30ff\u3400-\u9fff]", value))


def export_pdf(
    document: BookDocument,
    *,
    target_locale: str | None = None,
    font_path: str | Path | None = None,
) -> bytes:
    """Create an A5, horizontal, selectable-text edition PDF."""

    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
        from reportlab.lib.pagesizes import A5
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.pdfgen.canvas import Canvas
        from reportlab.platypus import (
            BaseDocTemplate,
            Frame,
            Image,
            PageBreak,
            PageTemplate,
            Paragraph,
            Spacer,
        )
        from reportlab.platypus.tableofcontents import TableOfContents
    except ImportError as exc:  # pragma: no cover - dependency installation guard
        raise DocumentError(
            EXPORT_VALIDATION_FAILED,
            "PDF export dependencies are not installed.",
        ) from exc

    locale = target_locale or document.language or "ja"
    all_text = "".join(block.text_for(target_locale) for block in document.iter_blocks())
    chosen_font = find_cjk_font(locale, configured_path=font_path)
    font_name = "Times-Roman"
    if _contains_cjk(all_text):
        if not chosen_font:
            raise DocumentError(
                EXPORT_VALIDATION_FAILED,
                "A CJK TrueType/OpenType font is required for PDF export.",
                {"locale": locale},
            )
        font_name = f"Shiori-{hashlib.sha256(str(chosen_font).encode()).hexdigest()[:8]}"
        try:
            pdfmetrics.registerFont(TTFont(font_name, str(chosen_font), subfontIndex=0))
        except Exception as exc:
            raise DocumentError(
                EXPORT_VALIDATION_FAILED,
                "The configured CJK font could not be embedded.",
                {"path": str(chosen_font)},
            ) from exc

    page_width, page_height = A5
    buffer = io.BytesIO()

    class _EditionTemplate(BaseDocTemplate):
        def afterFlowable(self, flowable: object) -> None:  # noqa: N802 - ReportLab API
            if isinstance(flowable, Paragraph) and getattr(flowable.style, "name", "") == "Chapter":
                key = f"section-{self.seq.nextf('section')}"
                self.canv.bookmarkPage(key)
                self.canv.addOutlineEntry(flowable.getPlainText(), key, level=0)
                self.notify("TOCEntry", (0, flowable.getPlainText(), self.page, key))

    def draw_page(canvas: Canvas, doc: BaseDocTemplate) -> None:
        canvas.saveState()
        canvas.setFont(font_name, 8)
        canvas.setFillColor(colors.HexColor("#706963"))
        canvas.drawCentredString(page_width / 2, 11 * mm, str(doc.page))
        canvas.restoreState()

    doc = _EditionTemplate(
        buffer,
        pagesize=A5,
        leftMargin=18 * mm,
        rightMargin=16 * mm,
        topMargin=18 * mm,
        bottomMargin=19 * mm,
        title=document.title,
        author="Shiori",
        lang=locale,
    )
    frame = Frame(
        doc.leftMargin,
        doc.bottomMargin,
        doc.width,
        doc.height,
        id="body",
        leftPadding=0,
        rightPadding=0,
        topPadding=0,
        bottomPadding=0,
    )
    doc.addPageTemplates([PageTemplate(id="edition", frames=[frame], onPage=draw_page)])

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "EditionTitle",
        parent=styles["Title"],
        fontName=font_name,
        fontSize=22,
        leading=31,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#201d1a"),
        spaceAfter=14 * mm,
    )
    chapter_style = ParagraphStyle(
        "Chapter",
        parent=styles["Heading1"],
        fontName=font_name,
        fontSize=15,
        leading=23,
        textColor=colors.HexColor("#201d1a"),
        spaceAfter=7 * mm,
        keepWithNext=True,
    )
    body_style = ParagraphStyle(
        "EditionBody",
        parent=styles["BodyText"],
        fontName=font_name,
        fontSize=10.2,
        leading=18,
        alignment=TA_JUSTIFY,
        textColor=colors.HexColor("#201d1a"),
        firstLineIndent=10.2,
        spaceAfter=3.2 * mm,
        splitLongWords=True,
        wordWrap="CJK",
    )
    toc_style = ParagraphStyle(
        "TOC",
        parent=body_style,
        fontSize=9.5,
        leading=16,
        leftIndent=3 * mm,
        firstLineIndent=0,
    )

    story: list[object] = [
        Spacer(1, 36 * mm),
        Paragraph(html.escape(document.title or "Untitled"), title_style),
        PageBreak(),
        Paragraph("目次" if locale.startswith(("ja", "zh")) else "Contents", chapter_style),
    ]
    toc = TableOfContents()
    toc.levelStyles = [toc_style]
    story.extend([toc, PageBreak()])

    for section_index, section in enumerate(document.sections):
        if section_index:
            story.append(PageBreak())
        story.append(
            Paragraph(
                html.escape(section.title or f"Chapter {section_index + 1}"),
                chapter_style,
            )
        )
        for block in section.blocks:
            content = block.text_for(target_locale)
            if content:
                story.append(
                    Paragraph(
                        html.escape(content).replace("\n", "<br/>").replace("  ", " &nbsp;"),
                        body_style,
                    )
                )
            for inline in block.inlines:
                if inline.kind != "image" or not inline.asset_path:
                    continue
                asset = document.asset_by_path(inline.asset_path)
                if not asset or not asset.media_type.startswith("image/"):
                    continue
                try:
                    image = Image(io.BytesIO(asset.data))
                    scale = min(
                        doc.width / image.imageWidth, (doc.height * 0.72) / image.imageHeight, 1.0
                    )
                    image.drawWidth = image.imageWidth * scale
                    image.drawHeight = image.imageHeight * scale
                    image.hAlign = "CENTER"
                    story.append(image)
                    story.append(Spacer(1, 3 * mm))
                except Exception:
                    # Broken image metadata should not corrupt the text edition.
                    continue

    try:
        doc.multiBuild(story)
    except Exception as exc:
        raise DocumentError(
            EXPORT_VALIDATION_FAILED,
            "The PDF layout engine could not assemble the edition.",
        ) from exc
    payload = buffer.getvalue()
    try:
        with fitz.open(stream=payload, filetype="pdf") as check:
            if check.page_count < 1 or not payload.startswith(b"%PDF-"):
                raise ValueError("invalid PDF")
            if all_text.strip() and not "".join(page.get_text() for page in check).strip():
                raise ValueError("generated PDF has no selectable text")
    except (fitz.FileDataError, RuntimeError, ValueError) as exc:
        raise DocumentError(
            EXPORT_VALIDATION_FAILED,
            "The generated PDF failed structural validation.",
        ) from exc
    return payload
