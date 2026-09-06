"""Deterministic EPUB builder (stdlib ``zipfile`` + ``xml.etree`` only).

``build_epub(decks, title=...)`` turns ``[(deck_name, [(front, back), ...]), ...]``
into the raw bytes of a valid EPUB with one chapter per deck.

Determinism is a hard requirement: an unchanged collection must produce
byte-identical output so the sync job can detect "nothing changed" by hashing.
We achieve that by:

* sorting decks by name and preserving the given card order,
* deriving the book id from a content hash (not the wall clock),
* pinning every zip entry's timestamp to a fixed value, and
* writing entries in a fixed order with fixed compression settings.

EPUB structural rules honoured:

* the first archive entry is ``mimetype``, STORED (uncompressed),
* ``META-INF/container.xml`` points at ``OEBPS/content.opf``,
* both ``toc.ncx`` (EPUB2) and ``nav.xhtml`` (EPUB3) are emitted.
"""

from __future__ import annotations

import hashlib
import zipfile
from xml.sax.saxutils import escape

# Fixed timestamp for every zip entry -> reproducible archives.
_FIXED_DATETIME = (1980, 1, 1, 0, 0, 0)

MIMETYPE = "application/epub+zip"

CONTAINER_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<container version="1.0" '
    'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
    "  <rootfiles>\n"
    '    <rootfile full-path="OEBPS/content.opf" '
    'media-type="application/oebps-package+xml"/>\n'
    "  </rootfiles>\n"
    "</container>\n"
)


def build_epub(
    decks: list[tuple[str, list[tuple[str, str]]]],
    title: str = "My Anki Decks",
) -> bytes:
    """Build an EPUB from decks and return its bytes."""
    sorted_decks = sorted(decks, key=lambda d: d[0].lower())
    book_id = _book_id(title, sorted_decks)

    chapters = []
    for index, (deck_name, cards) in enumerate(sorted_decks, start=1):
        chapters.append(
            {
                "id": f"deck{index}",
                "href": f"deck{index}.xhtml",
                "title": deck_name,
                "xhtml": _chapter_xhtml(deck_name, cards),
            }
        )

    from io import BytesIO

    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        _write_stored(zf, "mimetype", MIMETYPE.encode("ascii"))
        _write_deflated(zf, "META-INF/container.xml", CONTAINER_XML)
        _write_deflated(zf, "OEBPS/content.opf", _content_opf(title, book_id, chapters))
        _write_deflated(zf, "OEBPS/toc.ncx", _toc_ncx(title, book_id, chapters))
        _write_deflated(zf, "OEBPS/nav.xhtml", _nav_xhtml(title, chapters))
        for chapter in chapters:
            _write_deflated(zf, f"OEBPS/{chapter['href']}", chapter["xhtml"])
    return buf.getvalue()


# -- zip helpers -----------------------------------------------------------
def _write_stored(zf: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=_FIXED_DATETIME)
    info.compress_type = zipfile.ZIP_STORED
    zf.writestr(info, data)


def _write_deflated(zf: zipfile.ZipFile, name: str, data) -> None:
    if isinstance(data, str):
        data = data.encode("utf-8")
    info = zipfile.ZipInfo(name, date_time=_FIXED_DATETIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    zf.writestr(info, data)


# -- identity --------------------------------------------------------------
def _book_id(title: str, sorted_decks) -> str:
    """A stable urn:uuid derived from content, so identical input -> identical id."""
    hasher = hashlib.sha256()
    hasher.update(title.encode("utf-8"))
    for deck_name, cards in sorted_decks:
        hasher.update(b"\x00D")
        hasher.update(deck_name.encode("utf-8"))
        for front, back in cards:
            hasher.update(b"\x00F")
            hasher.update(front.encode("utf-8"))
            hasher.update(b"\x00B")
            hasher.update(back.encode("utf-8"))
    digest = hasher.hexdigest()
    return (
        f"urn:uuid:{digest[0:8]}-{digest[8:12]}-{digest[12:16]}-"
        f"{digest[16:20]}-{digest[20:32]}"
    )


# -- documents -------------------------------------------------------------
def _chapter_xhtml(deck_name: str, cards: list[tuple[str, str]]) -> str:
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!DOCTYPE html>',
        '<html xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:epub="http://www.idpf.org/2007/ops">',
        "<head>",
        f"<title>{escape(deck_name)}</title>",
        '<meta charset="utf-8"/>',
        "</head>",
        "<body>",
        f"<h1>{escape(deck_name)}</h1>",
    ]
    blocks = []
    for front, back in cards:
        blocks.append(
            '<div class="card">'
            f'<p class="front">{escape(front)}</p>'
            f'<p class="back">{escape(back)}</p>'
            "</div>"
        )
    parts.append("<hr/>".join(blocks))
    parts.append("</body>")
    parts.append("</html>")
    return "\n".join(parts) + "\n"


def _content_opf(title: str, book_id: str, chapters: list[dict]) -> str:
    manifest = [
        '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" '
        'properties="nav"/>',
    ]
    spine = []
    for chapter in chapters:
        manifest.append(
            f'<item id="{chapter["id"]}" href="{chapter["href"]}" '
            f'media-type="application/xhtml+xml"/>'
        )
        spine.append(f'<itemref idref="{chapter["id"]}"/>')
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
        'unique-identifier="bookid">\n'
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        f'    <dc:identifier id="bookid">{escape(book_id)}</dc:identifier>\n'
        f"    <dc:title>{escape(title)}</dc:title>\n"
        "    <dc:language>en</dc:language>\n"
        '    <meta property="dcterms:modified">1980-01-01T00:00:00Z</meta>\n'
        "  </metadata>\n"
        "  <manifest>\n    " + "\n    ".join(manifest) + "\n  </manifest>\n"
        '  <spine toc="ncx">\n    ' + "\n    ".join(spine) + "\n  </spine>\n"
        "</package>\n"
    )


def _toc_ncx(title: str, book_id: str, chapters: list[dict]) -> str:
    nav_points = []
    for order, chapter in enumerate(chapters, start=1):
        nav_points.append(
            f'    <navPoint id="{chapter["id"]}" playOrder="{order}">\n'
            f"      <navLabel><text>{escape(chapter['title'])}</text></navLabel>\n"
            f'      <content src="{chapter["href"]}"/>\n'
            f"    </navPoint>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
        "  <head>\n"
        f'    <meta name="dtb:uid" content="{escape(book_id)}"/>\n'
        '    <meta name="dtb:depth" content="1"/>\n'
        '    <meta name="dtb:totalPageCount" content="0"/>\n'
        '    <meta name="dtb:maxPageNumber" content="0"/>\n'
        "  </head>\n"
        f"  <docTitle><text>{escape(title)}</text></docTitle>\n"
        "  <navMap>\n" + "\n".join(nav_points) + "\n  </navMap>\n"
        "</ncx>\n"
    )


def _nav_xhtml(title: str, chapters: list[dict]) -> str:
    items = [
        f'      <li><a href="{chapter["href"]}">{escape(chapter["title"])}</a></li>'
        for chapter in chapters
    ]
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:epub="http://www.idpf.org/2007/ops">\n'
        f"<head><title>{escape(title)}</title><meta charset=\"utf-8\"/></head>\n"
        "<body>\n"
        '  <nav epub:type="toc" id="toc">\n'
        f"    <h1>{escape(title)}</h1>\n"
        "    <ol>\n" + "\n".join(items) + "\n    </ol>\n"
        "  </nav>\n"
        "</body>\n"
        "</html>\n"
    )
