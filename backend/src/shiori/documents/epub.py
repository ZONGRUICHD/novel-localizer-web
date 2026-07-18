from __future__ import annotations

import hashlib
import html
import io
import mimetypes
import posixpath
import re
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET

from .errors import (
    EXPORT_VALIDATION_FAILED,
    UNSAFE_ARCHIVE,
    UNSUPPORTED_DRM,
    UNSUPPORTED_FORMAT,
    DocumentError,
)
from .model import Asset, Block, BookDocument, Inline, Section, TocEntry, stable_identifier

_XHTML_TYPES = {"application/xhtml+xml", "text/html"}
_IMAGE_PREFIX = "image/"
_FONT_MEDIA_TYPES = {
    "application/font-sfnt",
    "application/font-woff",
    "application/x-font-opentype",
    "application/x-font-ttf",
    "application/x-font-woff",
    "application/vnd.ms-opentype",
    "application/vnd.ms-fontobject",
    "font/otf",
    "font/ttf",
    "font/woff",
    "font/woff2",
}
_STANDARD_FONT_OBFUSCATION = {
    "http://www.idpf.org/2008/embedding",
    "http://ns.adobe.com/pdf/enc#RC",
}
_BLOCK_TAGS = {
    "h1": "heading",
    "h2": "heading",
    "h3": "heading",
    "h4": "heading",
    "h5": "heading",
    "h6": "heading",
    "p": "paragraph",
    "li": "list_item",
    "blockquote": "quote",
    "pre": "preformatted",
}


@dataclass(frozen=True, slots=True)
class EpubLimits:
    max_entries: int = 10_000
    max_entry_bytes: int = 64 * 1024 * 1024
    max_uncompressed_bytes: int = 512 * 1024 * 1024
    max_compression_ratio: float = 500.0


@dataclass(frozen=True, slots=True)
class _ManifestItem:
    id: str
    path: str
    media_type: str
    properties: frozenset[str]


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _attribute(element: ET.Element, name: str) -> str | None:
    for key, value in element.attrib.items():
        if _local_name(key) == name:
            return value
    return None


def _safe_xml(data: bytes, *, label: str) -> ET.Element:
    prefix = data[:4096].upper()
    # ElementTree does not fetch an external DTD. Explicit entity declarations
    # are rejected because they can still be used for expansion attacks.
    if b"<!ENTITY" in prefix:
        raise DocumentError(
            UNSAFE_ARCHIVE,
            f"{label} contains a forbidden entity declaration.",
        )
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise DocumentError(
            UNSUPPORTED_FORMAT,
            f"{label} is not well-formed XML.",
            {"line": getattr(exc, "position", (None, None))[0]},
        ) from exc


def _safe_archive_path(path: str) -> str:
    if "\\" in path or "\x00" in path:
        raise DocumentError(UNSAFE_ARCHIVE, "The EPUB contains an unsafe path.")
    decoded = unquote(path)
    pure = PurePosixPath(decoded)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise DocumentError(
            UNSAFE_ARCHIVE,
            "The EPUB contains an absolute or traversing path.",
            {"path": path},
        )
    return pure.as_posix()


def _resolve_path(base_path: str, href: str) -> str:
    parsed = urlsplit(href)
    if parsed.scheme or parsed.netloc:
        raise DocumentError(
            UNSAFE_ARCHIVE,
            "External resources are not accepted inside EPUB packages.",
            {"href": href},
        )
    joined = posixpath.normpath(posixpath.join(posixpath.dirname(base_path), unquote(parsed.path)))
    return _safe_archive_path(joined)


def _validate_zip(archive: zipfile.ZipFile, limits: EpubLimits) -> dict[str, zipfile.ZipInfo]:
    infos = archive.infolist()
    if len(infos) > limits.max_entries:
        raise DocumentError(
            UNSAFE_ARCHIVE,
            "The EPUB contains too many archive entries.",
            {"entries": len(infos), "limit": limits.max_entries},
        )

    by_path: dict[str, zipfile.ZipInfo] = {}
    total = 0
    for info in infos:
        path = _safe_archive_path(info.filename)
        if path in by_path:
            raise DocumentError(
                UNSAFE_ARCHIVE,
                "The EPUB contains duplicate archive paths.",
                {"path": path},
            )
        if info.flag_bits & 0x1:
            raise DocumentError(UNSUPPORTED_DRM, "Encrypted ZIP entries are not supported.")
        if info.file_size > limits.max_entry_bytes:
            raise DocumentError(
                UNSAFE_ARCHIVE,
                "An EPUB entry is larger than the configured safety limit.",
                {"path": path, "bytes": info.file_size},
            )
        total += info.file_size
        if total > limits.max_uncompressed_bytes:
            raise DocumentError(
                UNSAFE_ARCHIVE,
                "The EPUB expands beyond the configured safety limit.",
                {"bytes": total, "limit": limits.max_uncompressed_bytes},
            )
        ratio = info.file_size / max(info.compress_size, 1)
        if info.file_size > 1024 * 1024 and ratio > limits.max_compression_ratio:
            raise DocumentError(
                UNSAFE_ARCHIVE,
                "The EPUB contains a suspiciously compressed entry.",
                {"path": path, "ratio": round(ratio, 2)},
            )
        by_path[path] = info
    return by_path


def _read_source(source: bytes | bytearray | str | Path) -> tuple[bytes, str | None]:
    if isinstance(source, (str, Path)):
        path = Path(source)
        return path.read_bytes(), path.name
    return bytes(source), None


def _find_container_rootfile(archive: zipfile.ZipFile) -> str:
    try:
        root = _safe_xml(archive.read("META-INF/container.xml"), label="container.xml")
    except KeyError as exc:
        raise DocumentError(UNSUPPORTED_FORMAT, "The EPUB has no container.xml.") from exc
    for element in root.iter():
        if _local_name(element.tag) == "rootfile":
            full_path = _attribute(element, "full-path")
            if full_path:
                return _safe_archive_path(full_path)
    raise DocumentError(UNSUPPORTED_FORMAT, "The EPUB container has no package rootfile.")


def _check_encryption(
    archive: zipfile.ZipFile,
    paths: set[str],
    font_paths: set[str],
) -> dict[str, str]:
    """Return standard-obfuscated font paths and their algorithms."""

    try:
        data = archive.read("META-INF/encryption.xml")
    except KeyError:
        return {}
    root = _safe_xml(data, label="encryption.xml")
    accepted: dict[str, str] = {}
    for encrypted_data in (
        element for element in root.iter() if _local_name(element.tag) == "encrypteddata"
    ):
        algorithm: str | None = None
        uri: str | None = None
        for descendant in encrypted_data.iter():
            local = _local_name(descendant.tag)
            if local == "encryptionmethod":
                algorithm = _attribute(descendant, "algorithm")
            elif local == "cipherreference":
                uri = _attribute(descendant, "uri")
        if not algorithm or not uri or algorithm not in _STANDARD_FONT_OBFUSCATION:
            raise DocumentError(
                UNSUPPORTED_DRM,
                "The EPUB uses DRM or an unknown encryption method.",
                {"algorithm": algorithm or "unknown"},
            )
        path = _safe_archive_path(unquote(urlsplit(uri).path))
        if path not in paths:
            raise DocumentError(UNSAFE_ARCHIVE, "Encryption metadata references a missing file.")
        if path not in font_paths:
            raise DocumentError(
                UNSUPPORTED_DRM,
                "Standard EPUB obfuscation is accepted for declared fonts only.",
                {"path": path},
            )
        accepted[path] = algorithm
    return accepted


def _element_text(element: ET.Element, *, exclude_rt: bool = True) -> str:
    pieces: list[str] = []

    def visit(node: ET.Element) -> None:
        local = _local_name(node.tag)
        if local in {"script", "style"} or (exclude_rt and local in {"rt", "rp"}):
            return
        if node.text:
            pieces.append(node.text)
        for child in node:
            if _local_name(child.tag) == "br":
                pieces.append("\n")
            else:
                visit(child)
            if child.tail:
                pieces.append(child.tail)

    visit(element)
    return "".join(pieces)


def _inline_content(element: ET.Element, *, document_path: str) -> list[Inline]:
    inlines: list[Inline] = []

    def add_text(value: str | None) -> None:
        if not value:
            return
        if inlines and inlines[-1].kind == "text":
            inlines[-1].text += value
        else:
            inlines.append(Inline(kind="text", text=value))

    def visit(node: ET.Element) -> None:
        add_text(node.text)
        for child in node:
            local = _local_name(child.tag)
            if local in {"script", "style", "rt", "rp"}:
                pass
            elif local == "br":
                inlines.append(Inline(kind="line_break", text="\n"))
            elif local == "ruby":
                base = _element_text(child, exclude_rt=True)
                readings = [
                    _element_text(descendant, exclude_rt=False)
                    for descendant in child.iter()
                    if _local_name(descendant.tag) == "rt"
                ]
                inlines.append(
                    Inline(
                        kind="ruby",
                        text=base,
                        annotation=" ".join(
                            reading.strip() for reading in readings if reading.strip()
                        )
                        or None,
                    )
                )
            elif local == "a":
                href = _attribute(child, "href")
                text = _element_text(child, exclude_rt=True)
                safe_href: str | None = None
                if href:
                    parsed = urlsplit(href)
                    if not parsed.scheme and not parsed.netloc:
                        try:
                            _resolve_path(document_path, href)
                        except DocumentError:
                            safe_href = None
                        else:
                            safe_href = href
                inlines.append(Inline(kind="link", text=text, href=safe_href))
                if safe_href:
                    epub_type = _attribute(child, "type")
                    if epub_type:
                        inlines[-1].attributes["epub_type"] = epub_type
            elif local in {"img", "image"}:
                src = _attribute(child, "src") or _attribute(child, "href")
                asset_path: str | None = None
                if src:
                    try:
                        asset_path = _resolve_path(document_path, src)
                    except DocumentError:
                        asset_path = None
                inlines.append(
                    Inline(
                        kind="image",
                        text=_attribute(child, "alt") or "",
                        asset_path=asset_path,
                    )
                )
            else:
                visit(child)
            add_text(child.tail)

    visit(element)
    return inlines


def _iter_block_elements(root: ET.Element) -> Iterable[tuple[ET.Element, str]]:
    index = 0

    def visit(node: ET.Element) -> Iterable[tuple[ET.Element, str]]:
        nonlocal index
        local = _local_name(node.tag)
        if local in _BLOCK_TAGS:
            current = index
            index += 1
            yield node, f"{local}[{current}]"
            return
        if local in {"script", "style", "head"}:
            return
        for child in node:
            yield from visit(child)

    yield from visit(root)


def _manifest_from_opf(root: ET.Element, opf_path: str) -> dict[str, _ManifestItem]:
    manifest: dict[str, _ManifestItem] = {}
    for element in root.iter():
        if _local_name(element.tag) != "item":
            continue
        item_id = _attribute(element, "id")
        href = _attribute(element, "href")
        media_type = _attribute(element, "media-type")
        if not item_id or not href or not media_type:
            continue
        manifest[item_id] = _ManifestItem(
            id=item_id,
            path=_resolve_path(opf_path, href),
            media_type=media_type.lower(),
            properties=frozenset((_attribute(element, "properties") or "").split()),
        )
    return manifest


def _metadata_text(root: ET.Element, name: str) -> str:
    for element in root.iter():
        if _local_name(element.tag) == name and element.text:
            return element.text.strip()
    return ""


def _cover_candidates(
    opf_root: ET.Element,
    manifest: dict[str, _ManifestItem],
    opf_path: str,
    archive: zipfile.ZipFile,
) -> list[str]:
    candidates: list[str] = []
    for item in manifest.values():
        if "cover-image" in item.properties:
            candidates.append(item.path)

    for element in opf_root.iter():
        local = _local_name(element.tag)
        if local == "meta" and (_attribute(element, "name") or "").lower() == "cover":
            content = _attribute(element, "content")
            if content in manifest:
                candidates.append(manifest[content].path)
        elif local == "reference" and "cover" in (_attribute(element, "type") or "").lower():
            href = _attribute(element, "href")
            if href:
                candidates.append(_resolve_path(opf_path, href))

    # Guide references often point to a cover XHTML page rather than the image.
    expanded: list[str] = []
    for candidate in candidates:
        cover_item = next((value for value in manifest.values() if value.path == candidate), None)
        if cover_item and cover_item.media_type in _XHTML_TYPES:
            try:
                root = _safe_xml(archive.read(candidate), label=candidate)
            except KeyError:
                continue
            for element in root.iter():
                if _local_name(element.tag) in {"img", "image"}:
                    src = _attribute(element, "src") or _attribute(element, "href")
                    if src:
                        try:
                            expanded.append(_resolve_path(candidate, src))
                        except DocumentError:
                            pass
                        break
        else:
            expanded.append(candidate)

    for item in manifest.values():
        if item.media_type.startswith(_IMAGE_PREFIX) and (
            "cover" in item.id.lower() or "cover" in posixpath.basename(item.path).lower()
        ):
            expanded.append(item.path)
    return list(dict.fromkeys(expanded))


def _parse_toc(
    archive: zipfile.ZipFile,
    manifest: dict[str, _ManifestItem],
    sections_by_path: dict[str, Section],
) -> list[TocEntry]:
    nav_item = next((item for item in manifest.values() if "nav" in item.properties), None)
    ncx_item = next(
        (item for item in manifest.values() if item.media_type == "application/x-dtbncx+xml"),
        None,
    )
    item = nav_item or ncx_item
    if not item:
        return []
    try:
        root = _safe_xml(archive.read(item.path), label=item.path)
    except KeyError:
        return []

    entries: list[TocEntry] = []
    if nav_item:
        for anchor in (element for element in root.iter() if _local_name(element.tag) == "a"):
            href = _attribute(anchor, "href")
            if not href:
                continue
            try:
                target = _resolve_path(item.path, href)
            except DocumentError:
                continue
            section = sections_by_path.get(target)
            if section:
                entries.append(
                    TocEntry(
                        title=_element_text(anchor).strip() or section.title,
                        section_id=section.id,
                        fragment=urlsplit(href).fragment or None,
                    )
                )
    else:
        for nav_point in (
            element for element in root.iter() if _local_name(element.tag) == "navpoint"
        ):
            label = next(
                (
                    _element_text(element).strip()
                    for element in nav_point.iter()
                    if _local_name(element.tag) == "navlabel"
                ),
                "",
            )
            content = next(
                (element for element in nav_point.iter() if _local_name(element.tag) == "content"),
                None,
            )
            src = _attribute(content, "src") if content is not None else None
            if not src:
                continue
            try:
                target = _resolve_path(item.path, src)
            except DocumentError:
                continue
            section = sections_by_path.get(target)
            if section:
                entries.append(
                    TocEntry(
                        title=label or section.title,
                        section_id=section.id,
                        fragment=urlsplit(src).fragment or None,
                    )
                )
    return entries


def parse_epub(
    source: bytes | bytearray | str | Path,
    *,
    limits: EpubLimits | None = None,
) -> BookDocument:
    data, filename = _read_source(source)
    source_hash = hashlib.sha256(data).hexdigest()
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError) as exc:
        raise DocumentError(UNSUPPORTED_FORMAT, "The input is not a valid EPUB archive.") from exc

    with archive:
        entries = _validate_zip(archive, limits or EpubLimits())
        opf_path = _find_container_rootfile(archive)
        try:
            opf_root = _safe_xml(archive.read(opf_path), label=opf_path)
        except KeyError as exc:
            raise DocumentError(
                UNSUPPORTED_FORMAT, "The EPUB package document is missing."
            ) from exc
        manifest = _manifest_from_opf(opf_root, opf_path)
        obfuscated_fonts = _check_encryption(
            archive,
            set(entries),
            {
                item.path
                for item in manifest.values()
                if item.media_type in _FONT_MEDIA_TYPES
                or item.media_type.startswith("font/")
                or "font" in item.media_type
            },
        )

        spine_ids: list[tuple[str, bool]] = []
        for element in opf_root.iter():
            if _local_name(element.tag) == "itemref":
                idref = _attribute(element, "idref")
                if idref:
                    spine_ids.append(
                        (idref, (_attribute(element, "linear") or "yes").lower() != "no")
                    )

        sections: list[Section] = []
        sections_by_path: dict[str, Section] = {}
        for spine_index, (item_id, is_linear) in enumerate(spine_ids):
            if not is_linear:
                continue
            item = manifest.get(item_id)
            if not item or item.media_type not in _XHTML_TYPES:
                continue
            try:
                chapter_root = _safe_xml(archive.read(item.path), label=item.path)
            except KeyError as exc:
                raise DocumentError(
                    UNSUPPORTED_FORMAT,
                    "A spine document is missing from the EPUB.",
                    {"path": item.path},
                ) from exc
            section_locator = f"spine:{spine_index}:{item.path}"
            blocks: list[Block] = []
            for block_index, (element, xpath) in enumerate(_iter_block_elements(chapter_root)):
                inlines = _inline_content(element, document_path=item.path)
                text = "".join(inline.text for inline in inlines if inline.kind != "image")
                if not text.strip() and not any(inline.kind == "image" for inline in inlines):
                    continue
                epub_type = _attribute(element, "type")
                source_id = _attribute(element, "id")
                block_metadata: dict[str, str] = {}
                if epub_type:
                    block_metadata["epub_type"] = epub_type
                if source_id:
                    block_metadata["source_id"] = source_id
                blocks.append(
                    Block.create(
                        source_hash=source_hash,
                        section_locator=section_locator,
                        locator=f"{xpath}:{block_index}",
                        kind=_BLOCK_TAGS[_local_name(element.tag)],  # type: ignore[arg-type]
                        source_text=text,
                        inlines=inlines,
                        metadata=block_metadata,
                    )
                )

            if not blocks:
                body = next(
                    (
                        element
                        for element in chapter_root.iter()
                        if _local_name(element.tag) == "body"
                    ),
                    chapter_root,
                )
                fallback_text = _element_text(body).strip()
                if fallback_text:
                    blocks.append(
                        Block.create(
                            source_hash=source_hash,
                            section_locator=section_locator,
                            locator="body:0",
                            kind="paragraph",
                            source_text=fallback_text,
                            inlines=[Inline(text=fallback_text)],
                        )
                    )

            section_title = next(
                (block.source_text.strip() for block in blocks if block.kind == "heading"),
                "",
            )
            section = Section.create(
                source_hash=source_hash,
                locator=section_locator,
                title=section_title,
                blocks=blocks,
                href=item.path,
                metadata={"manifest_id": item.id, "spine_index": spine_index},
            )
            sections.append(section)
            sections_by_path[item.path] = section

        cover_paths = _cover_candidates(opf_root, manifest, opf_path, archive)
        assets: list[Asset] = []
        cover_asset_id: str | None = None
        preserved_media = _FONT_MEDIA_TYPES
        for item in manifest.values():
            if not (
                item.media_type.startswith(_IMAGE_PREFIX)
                or item.media_type in preserved_media
                or item.media_type.startswith("audio/")
            ):
                continue
            if item.path not in entries:
                continue
            raw = archive.read(item.path)
            asset = Asset.create(
                path=item.path,
                media_type=item.media_type,
                data=raw,
                is_cover=item.path in cover_paths,
            )
            if asset.is_cover and cover_asset_id is None:
                cover_asset_id = asset.id
            assets.append(asset)

        version = _attribute(opf_root, "version") or "3.0"
        source_identifier = _metadata_text(opf_root, "identifier")
        title = _metadata_text(opf_root, "title") or (Path(filename).stem if filename else "")
        language = _metadata_text(opf_root, "language") or "ja"
        return BookDocument(
            id=stable_identifier("book", source_hash),
            title=title,
            language=language,
            source_format="epub",
            source_hash=source_hash,
            sections=sections,
            assets=assets,
            toc=_parse_toc(archive, manifest, sections_by_path),
            cover_asset_id=cover_asset_id,
            metadata={
                "epub_version": version,
                "opf_path": opf_path,
                "spine_paths": [section.href for section in sections],
                "source_identifier": source_identifier,
                "obfuscated_fonts": obfuscated_fonts,
            },
        )


def _asset_extension(asset: Asset) -> str:
    suffix = PurePosixPath(asset.path).suffix
    if suffix and re.fullmatch(r"\.[A-Za-z0-9]{1,8}", suffix):
        return suffix.lower()
    return mimetypes.guess_extension(asset.media_type) or ""


def _render_inlines(
    block: Block,
    *,
    target_locale: str | None,
    section: Section,
    section_paths: dict[str, str],
    asset_paths: dict[str, str],
) -> str:
    translated = block.translations.get(target_locale or "") if target_locale else None
    images = [inline for inline in block.inlines if inline.kind == "image"]
    if translated is not None:
        rendered = html.escape(translated).replace("\n", "<br/>")
        for image in images:
            if image.asset_path and image.asset_path in asset_paths:
                rendered += (
                    f'<img src="../{html.escape(asset_paths[image.asset_path], quote=True)}" '
                    f'alt="{html.escape(image.text, quote=True)}"/>'
                )
        return rendered

    pieces: list[str] = []
    for inline in block.inlines or [Inline(text=block.source_text)]:
        if inline.kind == "text":
            pieces.append(html.escape(inline.text))
        elif inline.kind == "line_break":
            pieces.append("<br/>")
        elif inline.kind == "ruby":
            annotation = html.escape(inline.annotation or "")
            pieces.append(f"<ruby><rb>{html.escape(inline.text)}</rb><rt>{annotation}</rt></ruby>")
        elif inline.kind == "image" and inline.asset_path in asset_paths:
            pieces.append(
                f'<img src="../{html.escape(asset_paths[inline.asset_path], quote=True)}" '
                f'alt="{html.escape(inline.text, quote=True)}"/>'
            )
        elif inline.kind == "link":
            href = inline.href
            rewritten: str | None = None
            if href and section.href:
                parsed = urlsplit(href)
                try:
                    resolved = _resolve_path(section.href, href)
                except DocumentError:
                    resolved = ""
                target = section_paths.get(resolved)
                if target:
                    rewritten = posixpath.basename(target)
                    if parsed.fragment:
                        rewritten += f"#{html.escape(parsed.fragment, quote=True)}"
                elif not parsed.path and parsed.fragment:
                    rewritten = f"#{html.escape(parsed.fragment, quote=True)}"
            if rewritten:
                link_type = inline.attributes.get("epub_type")
                type_attribute = (
                    f' epub:type="{html.escape(link_type, quote=True)}"' if link_type else ""
                )
                pieces.append(
                    f'<a href="{rewritten}"{type_attribute}>{html.escape(inline.text)}</a>'
                )
            else:
                pieces.append(html.escape(inline.text))
    return "".join(pieces)


def export_epub(
    document: BookDocument,
    *,
    target_locale: str | None = None,
    cover_policy: str = "preserve",
    replacement_cover: bytes | None = None,
    replacement_cover_media_type: str = "image/jpeg",
) -> bytes:
    if cover_policy not in {"preserve", "replace", "none"}:
        raise ValueError("cover_policy must be preserve, replace or none")
    if cover_policy == "replace" and not replacement_cover:
        raise ValueError("replacement_cover is required when cover_policy=replace")

    selected_assets = list(document.assets)
    cover: Asset | None = None
    if cover_policy == "preserve":
        cover = document.asset_by_id(document.cover_asset_id)
    elif cover_policy == "replace" and replacement_cover is not None:
        cover = Asset.create(
            path="replacement-cover"
            + (mimetypes.guess_extension(replacement_cover_media_type) or ".jpg"),
            media_type=replacement_cover_media_type,
            data=replacement_cover,
            is_cover=True,
        )
        selected_assets = [asset for asset in selected_assets if not asset.is_cover] + [cover]
    else:
        selected_assets = [asset for asset in selected_assets if not asset.is_cover]

    asset_output: dict[str, str] = {}
    asset_by_source_path: dict[str, str] = {}
    for asset in selected_assets:
        asset_href = f"assets/{asset.id}{_asset_extension(asset)}"
        asset_output[asset.id] = asset_href
        asset_by_source_path[asset.path] = asset_href

    chapter_output = {
        section.href or section.locator: f"text/chapter-{index + 1:04d}.xhtml"
        for index, section in enumerate(document.sections)
    }
    section_filename_by_id = {
        section.id: chapter_output[section.href or section.locator] for section in document.sections
    }
    locale = target_locale or document.language or "ja"
    version = str(document.metadata.get("epub_version", "3.0"))
    major_version = "2.0" if version.startswith("2") else "3.0"
    identifier = str(
        document.metadata.get("source_identifier") or f"urn:sha256:{document.source_hash}"
    )

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as epub_zip:
        epub_zip.writestr(
            zipfile.ZipInfo("mimetype"),
            b"application/epub+zip",
            compress_type=zipfile.ZIP_STORED,
        )
        epub_zip.writestr(
            "META-INF/container.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>""",
        )
        epub_zip.writestr(
            "OEBPS/styles/horizontal.css",
            """html, body { writing-mode: horizontal-tb !important; -webkit-writing-mode: horizontal-tb !important; direction: ltr !important; }
body { margin: 5%; line-height: 1.8; }
p, li, blockquote { text-align: justify; }
img { max-width: 100%; height: auto; }
.tcy { text-combine-upright: none !important; }
""",
        )

        for asset in selected_assets:
            epub_zip.writestr(f"OEBPS/{asset_output[asset.id]}", asset.data)

        source_obfuscation = document.metadata.get("obfuscated_fonts", {})
        if isinstance(source_obfuscation, dict):
            encrypted_entries = []
            for asset in selected_assets:
                algorithm = source_obfuscation.get(asset.path)
                if algorithm in _STANDARD_FONT_OBFUSCATION:
                    encrypted_entries.append(
                        f'<enc:EncryptedData><enc:EncryptionMethod Algorithm="{html.escape(str(algorithm), quote=True)}"/>'
                        f'<enc:CipherData><enc:CipherReference URI="OEBPS/{html.escape(asset_output[asset.id], quote=True)}"/></enc:CipherData></enc:EncryptedData>'
                    )
            if encrypted_entries:
                epub_zip.writestr(
                    "META-INF/encryption.xml",
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" xmlns:enc="http://www.w3.org/2001/04/xmlenc#">'
                    + "".join(encrypted_entries)
                    + "</encryption>",
                )

        for index, section in enumerate(document.sections):
            rendered_blocks: list[str] = []
            for block in section.blocks:
                tag = {
                    "heading": "h2",
                    "paragraph": "p",
                    "quote": "blockquote",
                    "list_item": "p",
                    "preformatted": "pre",
                    "separator": "hr",
                }[block.kind]
                content = _render_inlines(
                    block,
                    target_locale=target_locale,
                    section=section,
                    section_paths=chapter_output,
                    asset_paths=asset_by_source_path,
                )
                if tag == "hr":
                    rendered_blocks.append("<hr/>")
                else:
                    element_id = str(block.metadata.get("source_id") or block.id)
                    rendered_blocks.append(
                        f'<{tag} id="{html.escape(element_id, quote=True)}">{content}</{tag}>'
                    )
            title = section.title or f"Chapter {index + 1}"
            chapter = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="{html.escape(locale, quote=True)}" xml:lang="{html.escape(locale, quote=True)}">
<head><title>{html.escape(title)}</title><link rel="stylesheet" type="text/css" href="../styles/horizontal.css"/></head>
<body>{"".join(rendered_blocks)}</body></html>"""
            epub_zip.writestr(f"OEBPS/{chapter_output[section.href or section.locator]}", chapter)

        cover_manifest = ""
        cover_spine = ""
        cover_meta = ""
        if cover:
            cover_href = asset_output[cover.id]
            epub_zip.writestr(
                "OEBPS/text/cover.xhtml",
                f"""<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Cover</title><link rel="stylesheet" type="text/css" href="../styles/horizontal.css"/></head>
<body><img src="../{html.escape(cover_href, quote=True)}" alt="Cover"/></body></html>""",
            )
            cover_manifest = (
                '<item id="cover-page" href="text/cover.xhtml" media-type="application/xhtml+xml"/>'
            )
            cover_spine = '<itemref idref="cover-page" linear="no"/>'
            cover_meta = (
                '<meta name="cover" content="asset-cover"/>' if major_version == "2.0" else ""
            )

        nav_items = []
        for index, section in enumerate(document.sections):
            title = section.title or f"Chapter {index + 1}"
            nav_items.append(
                f'<li><a href="{posixpath.basename(section_filename_by_id[section.id])}">{html.escape(title)}</a></li>'
            )
        nav = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="{html.escape(locale, quote=True)}">
<head><title>目次</title></head><body><nav epub:type="toc" id="toc"><h1>目次</h1><ol>{"".join(nav_items)}</ol></nav></body></html>"""
        if major_version == "3.0":
            epub_zip.writestr("OEBPS/text/nav.xhtml", nav)

        ncx_points = []
        for index, section in enumerate(document.sections):
            title = section.title or f"Chapter {index + 1}"
            ncx_points.append(
                f'<navPoint id="nav-{index + 1}" playOrder="{index + 1}"><navLabel><text>{html.escape(title)}</text></navLabel><content src="{section_filename_by_id[section.id]}"/></navPoint>'
            )
        epub_zip.writestr(
            "OEBPS/toc.ncx",
            f"""<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1"><head><meta name="dtb:uid" content="{identifier}"/></head><docTitle><text>{html.escape(document.title)}</text></docTitle><navMap>{"".join(ncx_points)}</navMap></ncx>""",
        )

        manifest_items = [
            '<item id="css" href="styles/horizontal.css" media-type="text/css"/>',
            '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
            cover_manifest,
        ]
        if major_version == "3.0":
            manifest_items.append(
                '<item id="nav" href="text/nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
            )
        spine_items = [cover_spine]
        for index, section in enumerate(document.sections):
            manifest_items.append(
                f'<item id="chapter-{index + 1}" href="{section_filename_by_id[section.id]}" media-type="application/xhtml+xml"/>'
            )
            spine_items.append(f'<itemref idref="chapter-{index + 1}"/>')
        for asset in selected_assets:
            properties = (
                ' properties="cover-image"'
                if cover and asset.id == cover.id and major_version == "3.0"
                else ""
            )
            item_id = "asset-cover" if cover and asset.id == cover.id else asset.id
            manifest_items.append(
                f'<item id="{item_id}" href="{asset_output[asset.id]}" media-type="{html.escape(asset.media_type, quote=True)}"{properties}/>'
            )

        modified = (
            '<meta property="dcterms:modified">2000-01-01T00:00:00Z</meta>'
            if major_version == "3.0"
            else ""
        )
        guide = (
            '<guide><reference type="cover" title="Cover" href="text/cover.xhtml"/></guide>'
            if cover and major_version == "2.0"
            else ""
        )
        opf = f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="{major_version}" unique-identifier="book-id">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="book-id">{html.escape(identifier)}</dc:identifier><dc:title>{html.escape(document.title or "Untitled")}</dc:title><dc:language>{html.escape(locale)}</dc:language>{modified}{cover_meta}</metadata>
<manifest>{"".join(manifest_items)}</manifest><spine toc="ncx">{"".join(spine_items)}</spine>{guide}</package>"""
        epub_zip.writestr("OEBPS/content.opf", opf)

    payload = buffer.getvalue()
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as check:
            if (
                check.namelist()[0] != "mimetype"
                or check.read("mimetype") != b"application/epub+zip"
            ):
                raise ValueError("invalid mimetype")
            _safe_xml(check.read("OEBPS/content.opf"), label="exported content.opf")
            for asset in selected_assets:
                if (
                    hashlib.sha256(check.read(f"OEBPS/{asset_output[asset.id]}")).hexdigest()
                    != asset.sha256
                ):
                    raise ValueError(f"asset hash mismatch: {asset.path}")
    except (KeyError, ValueError, zipfile.BadZipFile) as exc:
        raise DocumentError(
            EXPORT_VALIDATION_FAILED,
            "The generated EPUB failed structural validation.",
        ) from exc
    return payload
