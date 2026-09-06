"""Web-triggered AnkiWeb sync (runs inside the server process).

This is the same pipeline as the standalone ``python -m sync`` job, but driven
from the dashboard: the logged-in user submits their AnkiWeb credentials, we log
in, read every deck, build the deterministic EPUB, and store it straight into
their Inbox (no HTTP round-trip to /upload -- we're already in the server).

**Credentials are never persisted or logged.** They live only as local
variables inside :func:`_run` for the duration of one sync and are dropped when
it returns. Only the *result* (status + message + last EPUB hash) is stored, so
the dashboard can show progress and skip unchanged collections.

The work runs on a daemon thread so the HTTP request returns immediately; the
dashboard polls status by refreshing.
"""

from __future__ import annotations

import hashlib
import logging
import threading

from anki.client import AnkiWebClient
from epub.builder import build_epub

log = logging.getLogger("web.sync")

# Factory so tests can inject a fake client without touching the network.
_client_factory = AnkiWebClient


def start_sync(db, owner: str, anki_user: str, anki_pass: str, title: str) -> None:
    """Kick off a background sync for ``owner``. Returns immediately."""
    db.set_sync_state(owner, "running", "Syncing… this can take a minute.")
    thread = threading.Thread(
        target=_run,
        args=(db, owner, anki_user, anki_pass, title),
        daemon=True,
        name=f"anki-sync-{owner}",
    )
    thread.start()


def _run(db, owner: str, anki_user: str, anki_pass: str, title: str) -> None:
    try:
        client = _client_factory()
        client.login(anki_user, anki_pass)
        decks = client.read_all()
        if not decks:
            db.set_sync_state(owner, "error", "No non-empty decks found on AnkiWeb.")
            return

        epub_bytes = build_epub(decks, title=title)
        digest = hashlib.sha256(epub_bytes).hexdigest()
        card_count = sum(len(cards) for _name, cards in decks)

        state = db.get_sync_state(owner)
        if state is not None and state["last_hash"] == digest:
            db.set_sync_state(
                owner,
                "ok",
                f"Up to date — {len(decks)} decks, {card_count} cards. "
                "No changes since the last sync.",
                last_hash=digest,
                update_hash=True,
            )
            return

        db.store_book(owner, digest, epub_bytes, title, _epub_filename(title))
        db.set_sync_state(
            owner,
            "ok",
            f"Synced {len(decks)} decks ({card_count} cards). "
            "A fresh EPUB is waiting in your Inbox.",
            last_hash=digest,
            update_hash=True,
        )
    except Exception as exc:  # noqa: BLE001 - report any failure to the dashboard
        # exc from the client never contains the password (see anki/client.py).
        log.error("web sync failed for %s: %s", owner, exc)
        db.set_sync_state(owner, "error", f"Sync failed: {exc}")


def _epub_filename(title: str) -> str:
    safe = "".join(c for c in title if c.isalnum() or c in " -_").strip() or "anki-decks"
    return f"{safe}.epub"
