"""Sync job: rebuild the consolidated EPUB and deliver it.

Runnable as ``python -m sync`` (Railway nightly cron).

Flow:
1. log in to AnkiWeb and read every deck,
2. build one deterministic EPUB and sha256 the bytes,
3. if the hash equals the last shipped hash, log "unchanged" and stop,
4. otherwise POST the EPUB to the upload endpoint with the account token.

Change detection is free: unchanged decks -> identical bytes -> identical hash
-> the upload is a no-op -> the Inbox stays quiet. Changed decks -> new hash ->
a new Book -> the book reappears in the Inbox.

Follow-up (not v1): "supersede previous book" -- on a new upload, delete the
sender's prior consolidated book so the Inbox never accumulates stale editions.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sys

import requests

from anki.client import AnkiWebClient
from config import SyncConfig
from epub.builder import build_epub

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sync")

# Where we remember the last shipped hash (a tiny state file, not a secret).
_STATE_PATH = os.environ.get("SYNC_STATE_PATH", "./data/last_hash.txt")


def _read_last_hash() -> str | None:
    try:
        with open(_STATE_PATH, "r", encoding="ascii") as fh:
            return fh.read().strip() or None
    except FileNotFoundError:
        return None


def _write_last_hash(value: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(_STATE_PATH)) or ".", exist_ok=True)
    with open(_STATE_PATH, "w", encoding="ascii") as fh:
        fh.write(value)


def run(cfg: SyncConfig | None = None) -> int:
    cfg = cfg or SyncConfig.from_env()

    client = AnkiWebClient()
    client.login(cfg.anki_username, cfg.anki_password)
    log.info("logged in to AnkiWeb")

    decks = client.read_all()
    log.info("read %d non-empty deck(s)", len(decks))

    epub_bytes = build_epub(decks, title=cfg.book_title)
    digest = hashlib.sha256(epub_bytes).hexdigest()

    if digest == _read_last_hash():
        log.info("unchanged (sha256=%s); nothing to deliver", digest[:12])
        return 0

    log.info("changed; uploading epub (%d bytes, sha256=%s)", len(epub_bytes), digest[:12])
    _upload(cfg, epub_bytes)
    _write_last_hash(digest)
    log.info("delivered")
    return 0


def _upload(cfg: SyncConfig, epub_bytes: bytes) -> None:
    resp = requests.post(
        cfg.upload_url,
        files={"file": ("anki-decks.epub", epub_bytes, "application/epub+zip")},
        headers={"X-Auth-Token": cfg.token},
        timeout=120,
    )
    if resp.status_code != 200:
        # Never echo the token; only the status is safe to log.
        raise RuntimeError(f"upload failed with HTTP {resp.status_code}")


if __name__ == "__main__":
    try:
        sys.exit(run())
    except Exception as exc:  # noqa: BLE001 - top-level guard for cron logs
        # Keep credentials out of the message.
        log.error("sync failed: %s", exc)
        sys.exit(1)
