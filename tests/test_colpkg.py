"""colpkg reader test against a synthetic collection (new-schema layout)."""

import io
import sqlite3
import zipfile

from anki.colpkg import read_colpkg


def _make_colpkg() -> bytes:
    """Build a minimal colpkg with a 'decks' table and notes/cards."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE decks (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE notes (id INTEGER PRIMARY KEY, flds TEXT);
        CREATE TABLE cards (id INTEGER PRIMARY KEY, nid INTEGER, did INTEGER);
        """
    )
    conn.execute("INSERT INTO decks VALUES (1, 'Spanish'), (2, 'French')")
    sep = "\x1f"
    conn.execute("INSERT INTO notes VALUES (10, ?)", (f"hola{sep}hello",))
    conn.execute("INSERT INTO notes VALUES (11, ?)", (f"gato{sep}cat",))
    conn.execute("INSERT INTO notes VALUES (12, ?)", (f"bonjour{sep}hello",))
    conn.execute("INSERT INTO cards VALUES (100, 10, 1)")
    conn.execute("INSERT INTO cards VALUES (101, 11, 1)")
    conn.execute("INSERT INTO cards VALUES (102, 12, 2)")
    conn.commit()

    # dump the in-memory db to bytes via a temp on-disk copy
    import tempfile, os

    with tempfile.NamedTemporaryFile(suffix=".anki21", delete=False) as tmp:
        path = tmp.name
    disk = sqlite3.connect(path)
    conn.backup(disk)
    disk.close()
    conn.close()
    with open(path, "rb") as fh:
        db_bytes = fh.read()
    os.unlink(path)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("collection.anki21", db_bytes)
        zf.writestr("media", "{}")
    return buf.getvalue()


def test_read_colpkg_maps_decks_to_cards():
    result = read_colpkg(io.BytesIO(_make_colpkg()))
    assert result["Spanish"] == [("hola", "hello"), ("gato", "cat")]
    assert result["French"] == [("bonjour", "hello")]
