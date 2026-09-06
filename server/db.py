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

class UserExists(Exception):
    """Raised when creating a user whose username is already taken."""


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

-- Web accounts: username + salted password hash + the account's capability
-- token. The token is the same credential used by /upload and /k/<token>/.
CREATE TABLE IF NOT EXISTS user (
    username   TEXT PRIMARY KEY,
    pw_hash    TEXT NOT NULL,
    pw_salt    TEXT NOT NULL,
    token      TEXT NOT NULL UNIQUE,
    created_at INTEGER NOT NULL
);

-- Server-side browser sessions (cookie holds only an opaque id).
CREATE TABLE IF NOT EXISTS session (
    sid        TEXT PRIMARY KEY,
    username   TEXT NOT NULL REFERENCES user(username),
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);

-- Per-user status of the web-triggered AnkiWeb sync. Holds NO credentials --
-- only the last result and the last shipped EPUB hash (for change detection).
CREATE TABLE IF NOT EXISTS sync_state (
    username   TEXT PRIMARY KEY REFERENCES user(username),
    status     TEXT NOT NULL DEFAULT 'idle',
    message    TEXT NOT NULL DEFAULT '',
    last_hash  TEXT,
    updated_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_book_owner_inbox
    ON book(owner, delivered_at);
CREATE INDEX IF NOT EXISTS idx_session_expires ON session(expires_at);
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

    # -- web users ---------------------------------------------------------
    def create_user(
        self, username: str, pw_hash: str, pw_salt: str, token: str
    ) -> None:
        """Create a web account and its capability token.

        Raises :class:`UserExists` if the username is already taken.
        """
        now = int(time.time())
        try:
            with self._tx() as conn:
                conn.execute(
                    "INSERT INTO user(username, pw_hash, pw_salt, token, created_at) "
                    "VALUES(?, ?, ?, ?, ?)",
                    (username, pw_hash, pw_salt, token, now),
                )
                # Keep the token->owner mapping used by /upload and /k feeds.
                conn.execute(
                    "INSERT INTO account(token, owner) VALUES(?, ?)",
                    (token, username),
                )
        except sqlite3.IntegrityError as exc:
            raise UserExists(str(exc)) from exc

    def get_user(self, username: str) -> sqlite3.Row | None:
        with self._tx() as conn:
            return conn.execute(
                "SELECT * FROM user WHERE username = ?", (username,)
            ).fetchone()

    def user_token(self, username: str) -> str | None:
        row = self.get_user(username)
        return row["token"] if row else None

    def rotate_token(self, username: str, new_token: str) -> None:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT token FROM user WHERE username = ?", (username,)
            ).fetchone()
            if row is None:
                return
            old = row["token"]
            conn.execute(
                "UPDATE user SET token = ? WHERE username = ?", (new_token, username)
            )
            conn.execute(
                "UPDATE account SET token = ? WHERE token = ?", (new_token, old)
            )

    # -- sessions ----------------------------------------------------------
    def create_session(self, sid: str, username: str, ttl_seconds: int) -> None:
        now = int(time.time())
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO session(sid, username, created_at, expires_at) "
                "VALUES(?, ?, ?, ?)",
                (sid, username, now, now + ttl_seconds),
            )

    def session_username(self, sid: str) -> str | None:
        now = int(time.time())
        with self._tx() as conn:
            row = conn.execute(
                "SELECT username FROM session WHERE sid = ? AND expires_at > ?",
                (sid, now),
            ).fetchone()
            return row["username"] if row else None

    def delete_session(self, sid: str) -> None:
        with self._tx() as conn:
            conn.execute("DELETE FROM session WHERE sid = ?", (sid,))

    def purge_expired_sessions(self) -> None:
        with self._tx() as conn:
            conn.execute(
                "DELETE FROM session WHERE expires_at <= ?", (int(time.time()),)
            )

    # -- sync state --------------------------------------------------------
    def get_sync_state(self, username: str) -> sqlite3.Row | None:
        with self._tx() as conn:
            return conn.execute(
                "SELECT * FROM sync_state WHERE username = ?", (username,)
            ).fetchone()

    def set_sync_state(
        self,
        username: str,
        status: str,
        message: str,
        last_hash: str | None = None,
        update_hash: bool = False,
    ) -> None:
        """Upsert a user's sync status.

        ``last_hash`` is only written when ``update_hash`` is True, so a
        'running'/'error' update doesn't clobber the last good hash.
        """
        now = int(time.time())
        with self._tx() as conn:
            if update_hash:
                conn.execute(
                    "INSERT INTO sync_state(username, status, message, last_hash, "
                    "updated_at) VALUES(?, ?, ?, ?, ?) "
                    "ON CONFLICT(username) DO UPDATE SET status=excluded.status, "
                    "message=excluded.message, last_hash=excluded.last_hash, "
                    "updated_at=excluded.updated_at",
                    (username, status, message, last_hash, now),
                )
            else:
                conn.execute(
                    "INSERT INTO sync_state(username, status, message, updated_at) "
                    "VALUES(?, ?, ?, ?) "
                    "ON CONFLICT(username) DO UPDATE SET status=excluded.status, "
                    "message=excluded.message, updated_at=excluded.updated_at",
                    (username, status, message, now),
                )

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

    def counts(self, owner: str) -> tuple[int, int]:
        """Return ``(inbox_count, total_count)`` for an owner."""
        with self._tx() as conn:
            total = conn.execute(
                "SELECT COUNT(*) AS c FROM book WHERE owner = ?", (owner,)
            ).fetchone()["c"]
            inbox = conn.execute(
                "SELECT COUNT(*) AS c FROM book "
                "WHERE owner = ? AND delivered_at IS NULL",
                (owner,),
            ).fetchone()["c"]
            return inbox, total

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
