"""SQLite storage for the OPDS server.

Content-addressed, deliver-once design:

* ``Blob(sha256 PK, size, data, cover)`` -- one row per distinct file, dedup by
  hash.
* ``Book(owner, sha256, title, filename, size, has_cover, added_at,
  delivered_at)`` -- UNIQUE(owner, sha256). ``delivered_at IS NULL`` means the
  book is still in the account's Inbox.

Sized for one household (~5 users); a single SQLite file with one writer is
plenty (Railway runs ``replicas = 1``).
"""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager

_SCHEMA = """
CREATE TABLE IF NOT EXISTS blob (
    sha256 TEXT PRIMARY KEY,
    size   INTEGER NOT NULL,
    data   BLOB NOT NULL,
    cover  BLOB
);

CREATE TABLE IF NOT EXISTS book (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    owner        TEXT NOT NULL,
    sha256       TEXT NOT NULL REFERENCES blob(sha256),
    title        TEXT NOT NULL,
    filename     TEXT NOT NULL,
    size         INTEGER NOT NULL,
    has_cover    INTEGER NOT NULL DEFAULT 0,
    added_at     INTEGER NOT NULL,
    delivered_at INTEGER,
    UNIQUE(owner, sha256)
);

CREATE TABLE IF NOT EXISTS account (
    token TEXT PRIMARY KEY,
    owner TEXT NOT NULL UNIQUE
);

CREATE INDEX IF NOT EXISTS idx_book_owner_inbox
    ON book(owner, delivered_at);
"""


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def _init_schema(self) -> None:
        with self._tx() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _tx(self):
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # -- accounts ----------------------------------------------------------
    def upsert_account(self, token: str, owner: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO account(token, owner) VALUES(?, ?) "
                "ON CONFLICT(owner) DO UPDATE SET token=excluded.token",
                (token, owner),
            )

    def owner_for_token(self, token: str) -> str | None:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT owner FROM account WHERE token = ?", (token,)
            ).fetchone()
            return row["owner"] if row else None

    # -- ingest ------------------------------------------------------------
    def store_book(
        self,
        owner: str,
        sha256: str,
        data: bytes,
        title: str,
        filename: str,
        cover: bytes | None = None,
    ) -> bool:
        """Store a blob + book atomically. Returns True if a new Book was added.

        Re-uploading an identical file (same owner + hash) is a no-op.
        """
        now = int(time.time())
        with self._tx() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO blob(sha256, size, data, cover) "
                "VALUES(?, ?, ?, ?)",
                (sha256, len(data), data, cover),
            )
            cur = conn.execute(
                "INSERT OR IGNORE INTO book"
                "(owner, sha256, title, filename, size, has_cover, added_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)",
                (owner, sha256, title, filename, len(data), 1 if cover else 0, now),
            )
            return cur.rowcount > 0

    # -- queries -----------------------------------------------------------
    def inbox(self, owner: str) -> list[sqlite3.Row]:
        with self._tx() as conn:
            return conn.execute(
                "SELECT * FROM book WHERE owner = ? AND delivered_at IS NULL "
                "ORDER BY added_at DESC, id DESC",
                (owner,),
            ).fetchall()

    def all_books(self, owner: str, limit: int | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM book WHERE owner = ? ORDER BY added_at DESC, id DESC"
        params: tuple = (owner,)
        if limit is not None:
            sql += " LIMIT ?"
            params = (owner, limit)
        with self._tx() as conn:
            return conn.execute(sql, params).fetchall()

    def get_book(self, owner: str, book_id: int) -> sqlite3.Row | None:
        with self._tx() as conn:
            return conn.execute(
                "SELECT * FROM book WHERE owner = ? AND id = ?", (owner, book_id)
            ).fetchone()

    def blob_data(self, sha256: str) -> bytes | None:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT data FROM blob WHERE sha256 = ?", (sha256,)
            ).fetchone()
            return bytes(row["data"]) if row else None

    def mark_delivered(self, owner: str, book_id: int) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE book SET delivered_at = ? "
                "WHERE owner = ? AND id = ? AND delivered_at IS NULL",
                (int(time.time()), owner, book_id),
            )
