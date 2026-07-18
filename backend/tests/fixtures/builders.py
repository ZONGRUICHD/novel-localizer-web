from __future__ import annotations

import base64
import io
import zipfile

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2nGQAAAAASUVORK5CYII="
)


def synthetic_epub(*, encryption_algorithm: str | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            zipfile.ZipInfo("mimetype"),
            b"application/epub+zip",
            compress_type=zipfile.ZIP_STORED,
        )
        archive.writestr(
            "META-INF/container.xml",
            """<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
<rootfiles><rootfile full-path="OPS/package.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>""",
        )
        archive.writestr(
            "OPS/package.opf",
            """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">synthetic</dc:identifier><dc:title>合成書籍</dc:title><dc:language>ja</dc:language></metadata>
<manifest>
 <item id="z" href="text/z.xhtml" media-type="application/xhtml+xml"/>
 <item id="a" href="text/a.xhtml" media-type="application/xhtml+xml"/>
 <item id="nav" href="text/nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
 <item id="cover" href="images/cover.png" media-type="image/png" properties="cover-image"/>
 <item id="inside" href="images/inside.png" media-type="image/png"/>
 <item id="css" href="style/vertical.css" media-type="text/css"/>
</manifest>
<spine page-progression-direction="rtl"><itemref idref="z"/><itemref idref="a"/></spine>
</package>""",
        )
        archive.writestr(
            "OPS/text/z.xhtml",
            """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>Z</title><link rel="stylesheet" href="../style/vertical.css"/></head>
<body><h1>第一章</h1><p>彼女は<ruby>勝<rt>か</rt></ruby>った。</p><p><a epub:type="noteref" href="a.xhtml#note">注</a></p><script>盗む</script></body></html>""",
        )
        archive.writestr(
            "OPS/text/a.xhtml",
            """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>A</title></head>
<body><h1>第二章</h1><p id="note" epub:type="footnote">脚注本文。</p><p><img src="../images/inside.png" alt="挿絵"/>終わり。</p></body></html>""",
        )
        archive.writestr(
            "OPS/text/nav.xhtml",
            """<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body><nav epub:type="toc"><ol><li><a href="z.xhtml">第一章</a></li><li><a href="a.xhtml">第二章</a></li></ol></nav></body></html>""",
        )
        archive.writestr("OPS/images/cover.png", PNG_1X1)
        archive.writestr("OPS/images/inside.png", PNG_1X1 + b"inside")
        archive.writestr(
            "OPS/style/vertical.css",
            "html { writing-mode: vertical-rl; -epub-writing-mode: vertical-rl; }",
        )
        if encryption_algorithm:
            archive.writestr(
                "META-INF/encryption.xml",
                f"""<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" xmlns:enc="http://www.w3.org/2001/04/xmlenc#"><enc:EncryptedData><enc:EncryptionMethod Algorithm="{encryption_algorithm}"/><enc:CipherData><enc:CipherReference URI="OPS/images/inside.png"/></enc:CipherData></enc:EncryptedData></encryption>""",
            )
    return buffer.getvalue()
