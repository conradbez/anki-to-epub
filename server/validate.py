"""Validate that bytes really are an EPUB -- never trust the filename.

A file is an EPUB because its bytes say so: it must start with the zip magic
``PK``, contain an uncompressed ``mimetype`` entry equal to
``application/epub+zip``, and contain ``META-INF/container.xml``.

Reads out of the archive are capped so a malicious ("zip bomb") EPUB cannot
balloon memory.
"""

from __future__ import annotations

import io
import zipfile

_MAX_ENTRY_BYTES = 4 * 1024 * 1024  # cap per inspected member
EPUB_MIMETYPE = b"application/epub+zip"


class InvalidEpub(ValueError):
    pass


def validate_epub(data: bytes) -> None:
    """Raise :class:`InvalidEpub` unless ``data`` is a structurally valid EPUB."""
    if data[:2] != b"PK":
        raise InvalidEpub("missing PK zip magic")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise InvalidEpub(f"not a zip archive: {exc}") from exc
    with zf:
        names = zf.namelist()
        if "mimetype" not in names:
            raise InvalidEpub("missing mimetype entry")
        info = zf.getinfo("mimetype")
        if info.file_size > _MAX_ENTRY_BYTES:
            raise InvalidEpub("mimetype entry too large")
        mimetype = _read_capped(zf, "mimetype")
        if mimetype.strip() != EPUB_MIMETYPE:
            raise InvalidEpub("mimetype is not application/epub+zip")
        if "META-INF/container.xml" not in names:
            raise InvalidEpub("missing META-INF/container.xml")
        container_info = zf.getinfo("META-INF/container.xml")
        if container_info.file_size > _MAX_ENTRY_BYTES:
            raise InvalidEpub("container.xml too large")


def _read_capped(zf: zipfile.ZipFile, name: str) -> bytes:
    out = bytearray()
    with zf.open(name) as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            out.extend(chunk)
            if len(out) > _MAX_ENTRY_BYTES:
                raise InvalidEpub(f"{name} exceeds size cap")
    return bytes(out)


def extract_title(data: bytes, default: str) -> str:
    """Best-effort dc:title from the OPF, else ``default``. Never raises."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for name in zf.namelist():
                if name.endswith(".opf"):
                    opf = _read_capped(zf, name).decode("utf-8", "replace")
                    start = opf.find("<dc:title")
                    if start != -1:
                        gt = opf.find(">", start)
                        end = opf.find("</dc:title>", gt)
                        if gt != -1 and end != -1:
                            return opf[gt + 1 : end].strip() or default
    except (zipfile.BadZipFile, InvalidEpub, ValueError):
        pass
    return default
