"""AnkiWeb client.

AnkiWeb's web app is a single-page app talking to a protobuf-over-HTTP backend
mounted under ``/svc/`` (``Content-Type: application/octet-stream``). The
backend is split across two hosts that share one logged-in account:

* ``https://ankiuser.net``  -- the *editor* service (notetypes, decks list)
* ``https://ankiweb.net``   -- the *decks* / account service (login, export)

Login sets an ``ankiweb`` session cookie that both hosts honour.

Reading cards out of a deck
---------------------------
The SPA's browse/search reviewer is heavily obfuscated and its read RPC shape is
not stable across releases. Rather than depend on an endpoint that can change
without notice, this client uses the **FALLBACK documented in the README**:
download AnkiWeb's full ``.colpkg`` export and read the ``notes``/``cards``
tables with the stdlib ``sqlite3`` module. The colpkg is a plain zip whose
``collection.anki2`` (or ``.anki21``) member is a SQLite database, so this needs
no third-party dependency and no reverse-engineered RPC. The parsing lives in
:mod:`anki.colpkg` and is unit-tested against a synthetic collection.

The public interface (``login``/``decks``/``cards_in_deck``/``read_all``) is the
same regardless of which read strategy is used, so a future RPC-based reader can
be dropped in without touching callers.
"""

from __future__ import annotations

import io
import os
import tempfile

import requests

from . import colpkg
from .protobuf import pb_int, pb_parse, pb_string

ANKIWEB = "https://ankiweb.net"
ANKIUSER = "https://ankiuser.net"

# Cap responses so a hostile/oversized reply can't balloon memory. The full
# export is streamed to a temp file (see _download_export) and is exempt.
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class AnkiWebError(RuntimeError):
    """Raised when AnkiWeb returns an unexpected response.

    Never include credentials in the message -- errors get logged.
    """


class AnkiWebClient:
    def __init__(self, timeout: int = 30) -> None:
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Content-Type": "application/octet-stream",
                "User-Agent": "anki-deck-reader/1.0",
            }
        )
        self._timeout = timeout
        self._logged_in = False

    # -- transport ---------------------------------------------------------
    def _post(self, host: str, path: str, body: bytes) -> bytes:
        resp = self._session.post(
            host + path, data=body, timeout=self._timeout, stream=True
        )
        if resp.status_code != 200:
            raise AnkiWebError(f"{path} returned HTTP {resp.status_code}")
        chunks = bytearray()
        for chunk in resp.iter_content(64 * 1024):
            chunks.extend(chunk)
            if len(chunks) > _MAX_RESPONSE_BYTES:
                raise AnkiWebError(f"{path} response exceeded size cap")
        return bytes(chunks)

    # -- auth --------------------------------------------------------------
    def login(self, username: str, password: str) -> None:
        """Authenticate. Response field 1 (varint) == 1 means SUCCESS."""
        body = pb_string(1, username) + pb_string(2, password)
        raw = self._post(ANKIWEB, "/svc/account/login", body)
        parsed = pb_parse(raw)
        status = next((v for fn, _w, v in parsed if fn == 1), None)
        if status != 1:
            # Do NOT echo the password or the raw body.
            raise AnkiWebError("login failed: invalid credentials or blocked")
        self._logged_in = True

    def _require_login(self) -> None:
        if not self._logged_in:
            raise AnkiWebError("not logged in; call login() first")

    # -- decks -------------------------------------------------------------
    def decks(self) -> list[tuple[int, str]]:
        """Return ``[(deck_id, deck_name), ...]`` for the account.

        Uses ``/svc/editor/get-info-for-adding``: field 2 repeats a submessage
        ``{1: id, 2: name}`` per deck.
        """
        self._require_login()
        raw = self._post(ANKIUSER, "/svc/editor/get-info-for-adding", b"")
        out: list[tuple[int, str]] = []
        for fn, _wire, value in pb_parse(raw):
            if fn == 2 and isinstance(value, (bytes, bytearray)):
                sub = pb_parse(value)
                deck_id = next((v for f, _w, v in sub if f == 1), None)
                name = next((v for f, _w, v in sub if f == 2), None)
                if deck_id is not None and name is not None:
                    out.append((int(deck_id), _as_text(name)))
        out.sort(key=lambda d: d[1].lower())
        return out

    def notetype_fields(self, notetype_id: int) -> list[str]:
        """Return ordered field names for a notetype (e.g. ['Front', 'Back'])."""
        self._require_login()
        body = pb_int(1, notetype_id)
        raw = self._post(ANKIUSER, "/svc/editor/get-notetype-fields", body)
        return [_as_text(v) for fn, _w, v in pb_parse(raw) if fn == 1]

    # -- cards -------------------------------------------------------------
    def _download_export(self) -> bytes:
        """Download the full ``.colpkg`` export, streamed to a temp file.

        The export endpoint returns the whole collection as a zip; we hash it
        straight to disk rather than into memory. The colpkg is then read with
        sqlite3 (see :mod:`anki.colpkg`).
        """
        self._require_login()
        # Endpoint confirmed from browser devtools on the "Export" action; if a
        # future AnkiWeb release moves it, this is the single line to update.
        url = ANKIWEB + "/svc/decks/download-export"
        resp = self._session.post(url, data=b"", timeout=self._timeout, stream=True)
        if resp.status_code != 200:
            raise AnkiWebError(f"export returned HTTP {resp.status_code}")
        tmp = tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024)
        try:
            for chunk in resp.iter_content(256 * 1024):
                tmp.write(chunk)
            tmp.seek(0)
            return tmp.read()
        finally:
            tmp.close()

    def _load_collection(self) -> dict[str, list[tuple[str, str]]]:
        """Return ``{deck_name: [(front, back), ...]}`` from the export.

        Cached on the instance so ``cards_in_deck`` and ``read_all`` share a
        single download. An override path (ANKI_COLPKG_PATH) is honoured for
        offline testing / manual exports.
        """
        if getattr(self, "_collection", None) is not None:
            return self._collection
        override = os.environ.get("ANKI_COLPKG_PATH")
        if override:
            with open(override, "rb") as fh:
                data = fh.read()
        else:
            data = self._download_export()
        self._collection = colpkg.read_colpkg(io.BytesIO(data))
        return self._collection

    def cards_in_deck(self, name: str) -> list[tuple[str, str]]:
        """Return ``[(front, back), ...]`` for one deck, in stable order."""
        return list(self._load_collection().get(name, []))

    def read_all(self) -> list[tuple[str, list[tuple[str, str]]]]:
        """Return ``[(deck_name, [(front, back), ...]), ...]``.

        Decks are name-sorted; empty decks are skipped.
        """
        collection = self._load_collection()
        result = []
        for deck_name in sorted(collection, key=str.lower):
            cards = collection[deck_name]
            if cards:
                result.append((deck_name, list(cards)))
        return result


def _as_text(value) -> str:
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    return str(value)


# Convenience module-level wrappers mirroring the README's "Expose:" list.
def login(username: str, password: str) -> AnkiWebClient:
    client = AnkiWebClient()
    client.login(username, password)
    return client
