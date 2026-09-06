"""Read an AnkiWeb ``.colpkg`` export with the stdlib only.

A ``.colpkg`` is a zip archive. Its collection database is one of, in order of
preference:

* ``collection.anki21b`` -- zstd-compressed SQLite (newest). We can only read
  this if the ``zstandard``/``zstd`` module is present; if not we fall back.
* ``collection.anki21`` -- plain SQLite (schema 11+, decks in a ``decks`` table)
* ``collection.anki2``  -- plain SQLite (legacy, decks as JSON in ``col.decks``)

We extract the member to a temp file (never trusting its name, and capping the
extracted size) and open it read-only with :mod:`sqlite3`.

The mapping we need is ``deck_name -> [(front, back), ...]``:

* ``cards.nid`` links a card to its note; ``cards.did`` links it to a deck.
* ``notes.flds`` holds the note's fields joined by the unit separator ``\\x1f``;
  fields[0]/[1] are conventionally Front/Back.
* deck names come from the ``decks`` table (new) or the ``col.decks`` JSON (old).

Field text is returned verbatim (still HTML from Anki); the EPUB builder escapes
it.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import zipfile

FIELD_SEP = "\x1f"

# Guardrail: never inflate an untrusted archive member without bound.
_MAX_DB_BYTES = 512 * 1024 * 1024

_PREFERRED_MEMBERS = ("collection.anki21b", "collection.anki21", "collection.anki2")


def read_colpkg(fileobj) -> dict[str, list[tuple[str, str]]]:
    """Return ``{deck_name: [(front, back), ...]}`` from a colpkg/apkg file.

    ``fileobj`` is a binary file-like object positioned at the start.
    """
    with zipfile.ZipFile(fileobj) as zf:
        member = _pick_member(zf)
        raw = _extract_capped(zf, member)

    if member.endswith(".anki21b"):
        raw = _maybe_decompress_zstd(raw)

    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tmp:
        tmp.write(raw)
        db_path = tmp.name
    try:
        return _read_db(db_path)
    finally:
        os.unlink(db_path)


def _pick_member(zf: zipfile.ZipFile) -> str:
    names = set(zf.namelist())
    for candidate in _PREFERRED_MEMBERS:
        if candidate in names:
            return candidate
    raise ValueError("no collection database found in colpkg")


def _extract_capped(zf: zipfile.ZipFile, member: str) -> bytes:
    info = zf.getinfo(member)
    if info.file_size > _MAX_DB_BYTES:
        raise ValueError("collection database exceeds size cap")
    out = bytearray()
    with zf.open(member) as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            out.extend(chunk)
            if len(out) > _MAX_DB_BYTES:
                raise ValueError("collection database exceeds size cap")
    return bytes(out)


def _maybe_decompress_zstd(raw: bytes) -> bytes:
    try:
        import zstandard  # type: ignore
    except ImportError:  # pragma: no cover - depends on optional dep
        raise ValueError(
            "collection.anki21b is zstd-compressed; install 'zstandard' or "
            "export an older format"
        )
    return zstandard.ZstdDecompressor().decompress(raw)


def _read_db(db_path: str) -> dict[str, list[tuple[str, str]]]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        deck_names = _deck_names(conn)
        out: dict[str, list[tuple[str, str]]] = {name: [] for name in deck_names.values()}
        # Order by note id for a stable, deterministic card order.
        rows = conn.execute(
            "SELECT c.did, n.flds FROM cards c "
            "JOIN notes n ON n.id = c.nid "
            "ORDER BY c.did, n.id"
        )
        for did, flds in rows:
            deck_name = deck_names.get(did, "Default")
            front, back = _split_front_back(flds)
            out.setdefault(deck_name, []).append((front, back))
        return out
    finally:
        conn.close()


def _deck_names(conn: sqlite3.Connection) -> dict[int, str]:
    """Map deck id -> deck name, handling both schema layouts."""
    if _table_exists(conn, "decks"):
        names: dict[int, str] = {}
        for did, name in conn.execute("SELECT id, name FROM decks"):
            names[did] = _clean_deck_name(name)
        if names:
            return names
    # Legacy: decks stored as JSON in the single-row col table.
    try:
        (decks_json,) = conn.execute("SELECT decks FROM col LIMIT 1").fetchone()
    except (sqlite3.OperationalError, TypeError):
        return {}
    names = {}
    for did, deck in json.loads(decks_json).items():
        names[int(did)] = _clean_deck_name(deck.get("name", "Default"))
    return names


def _clean_deck_name(name: str) -> str:
    # Anki uses "::" to denote nested decks; keep the leaf-friendly full path.
    return name.replace("\x1f", "::")


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _split_front_back(flds: str) -> tuple[str, str]:
    parts = flds.split(FIELD_SEP)
    front = parts[0] if parts else ""
    back = parts[1] if len(parts) > 1 else ""
    return front, back
