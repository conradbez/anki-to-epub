"""OPDS 1.2 (Atom) feed rendering.

Small, dependency-free feed builder. e-ink readers point at a capability URL and
get back an Atom feed of acquisition entries; each entry's acquisition link is
the download URL the reader fetches (which stamps the book delivered).
"""

from __future__ import annotations

from datetime import datetime, timezone
from xml.sax.saxutils import escape

OPDS_ACQUISITION_TYPE = (
    "application/atom+xml;profile=opds-catalog;kind=acquisition"
)
OPDS_NAVIGATION_TYPE = "application/atom+xml;profile=opds-catalog;kind=navigation"
EPUB_TYPE = "application/epub+zip"


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def navigation_feed(base: str, feed_id: str, title: str, entries: list[dict]) -> str:
    """Render a navigation feed. ``entries`` = [{title, href, content}]."""
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<feed xmlns="http://www.w3.org/2005/Atom" '
        'xmlns:opds="http://opds-spec.org/2010/catalog">',
        f"<id>{escape(feed_id)}</id>",
        f"<title>{escape(title)}</title>",
        f"<updated>{_now()}</updated>",
        f'<link rel="self" href="{escape(base)}" type="{OPDS_NAVIGATION_TYPE}"/>',
        f'<link rel="start" href="{escape(base)}" type="{OPDS_NAVIGATION_TYPE}"/>',
    ]
    for entry in entries:
        parts.append(
            "<entry>"
            f"<id>{escape(feed_id)}:{escape(entry['title'])}</id>"
            f"<title>{escape(entry['title'])}</title>"
            f"<updated>{_now()}</updated>"
            f"<content type=\"text\">{escape(entry.get('content', ''))}</content>"
            f'<link rel="subsection" href="{escape(entry["href"])}" '
            f'type="{OPDS_ACQUISITION_TYPE}"/>'
            "</entry>"
        )
    parts.append("</feed>")
    return "\n".join(parts) + "\n"


def acquisition_feed(
    base: str, self_href: str, feed_id: str, title: str, books: list[dict]
) -> str:
    """Render an acquisition feed. ``books`` = [{id, title, filename, size,
    added_at, download_href, has_cover, cover_href}]."""
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<feed xmlns="http://www.w3.org/2005/Atom" '
        'xmlns:dc="http://purl.org/dc/terms/" '
        'xmlns:opds="http://opds-spec.org/2010/catalog">',
        f"<id>{escape(feed_id)}</id>",
        f"<title>{escape(title)}</title>",
        f"<updated>{_now()}</updated>",
        f'<link rel="self" href="{escape(self_href)}" '
        f'type="{OPDS_ACQUISITION_TYPE}"/>',
        f'<link rel="start" href="{escape(base)}" type="{OPDS_NAVIGATION_TYPE}"/>',
    ]
    for book in books:
        entry = [
            "<entry>",
            f"<id>urn:book:{book['id']}</id>",
            f"<title>{escape(book['title'])}</title>",
            f"<updated>{_iso(book['added_at'])}</updated>",
            f"<dc:extent>{book['size']}</dc:extent>",
        ]
        if book.get("has_cover") and book.get("cover_href"):
            entry.append(
                f'<link rel="http://opds-spec.org/image" '
                f'href="{escape(book["cover_href"])}" type="image/png"/>'
            )
        entry.append(
            f'<link rel="http://opds-spec.org/acquisition" '
            f'href="{escape(book["download_href"])}" type="{EPUB_TYPE}"/>'
        )
        entry.append("</entry>")
        parts.append("".join(entry))
    parts.append("</feed>")
    return "\n".join(parts) + "\n"
