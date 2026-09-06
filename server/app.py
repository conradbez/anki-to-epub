"""FastAPI OPDS delivery server.

Endpoints:

* ``POST /upload``            -- ingest an EPUB (multipart), auth by token.
* ``GET  /k/<token>/``        -- OPDS navigation feed for an account.
* ``GET  /k/<token>/inbox``   -- acquisition feed of UNDELIVERED books.
* ``GET  /k/<token>/all``     -- acquisition feed of all books.
* ``GET  /k/<token>/recent``  -- acquisition feed of recent books.
* ``GET  /k/<token>/download/<id>`` -- download a book; stamps delivered_at.

Sized for one household. SQLite, one writer, ``replicas = 1``.
"""

from __future__ import annotations

import hashlib
import os
import tempfile

from fastapi import FastAPI, HTTPException, Path, Request, Response, UploadFile
from fastapi.responses import PlainTextResponse

from config import ServerConfig
from . import opds
from .db import Database
from .tokens import is_valid_shape
from .validate import InvalidEpub, extract_title, validate_epub

# Guardrail: bound upload size so a malicious client can't exhaust disk/memory.
_MAX_UPLOAD_BYTES = 64 * 1024 * 1024

app = FastAPI(title="anki-deck-reader OPDS")

_config = ServerConfig.from_env()
_db = Database(os.path.join(_config.data_dir, "library.db"))


def _resolve_owner(token: str) -> str:
    if not is_valid_shape(token):
        raise HTTPException(status_code=404, detail="not found")
    owner = _db.owner_for_token(token)
    if owner is None:
        raise HTTPException(status_code=404, detail="not found")
    return owner


def _base_url(request: Request, token: str) -> str:
    return str(request.base_url).rstrip("/") + f"/k/{token}"


@app.get("/healthz", response_class=PlainTextResponse)
def healthz() -> str:
    return "ok"


# -- upload ----------------------------------------------------------------
@app.post("/upload")
async def upload(request: Request, file: UploadFile) -> dict:
    token = request.headers.get("X-Auth-Token", "")
    owner = _resolve_owner(token)

    # Stream to a temp file while hashing, so a large upload never sits fully in
    # memory. Enforce the size cap as we go.
    hasher = hashlib.sha256()
    size = 0
    tmp = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)
    try:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > _MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail="file too large")
            hasher.update(chunk)
            tmp.write(chunk)
        tmp.seek(0)
        data = tmp.read()
    finally:
        tmp.close()

    try:
        validate_epub(data)
    except InvalidEpub as exc:
        raise HTTPException(status_code=400, detail=f"invalid epub: {exc}") from exc

    sha256 = hasher.hexdigest()
    title = extract_title(data, default=os.path.splitext(file.filename or "book")[0])
    filename = _safe_filename(file.filename) or f"{sha256[:12]}.epub"

    created = _db.store_book(owner, sha256, data, title, filename)
    return {"status": "stored" if created else "duplicate", "sha256": sha256}


def _safe_filename(name: str | None) -> str:
    """Don't trust the client's filename; keep only a safe basename."""
    if not name:
        return ""
    base = os.path.basename(name)
    cleaned = "".join(c for c in base if c.isalnum() or c in "._- ").strip()
    if not cleaned.lower().endswith(".epub"):
        cleaned += ".epub"
    return cleaned


# -- feeds -----------------------------------------------------------------
def _book_dict(request: Request, token: str, row) -> dict:
    base = _base_url(request, token)
    return {
        "id": row["id"],
        "title": row["title"],
        "filename": row["filename"],
        "size": row["size"],
        "added_at": row["added_at"],
        "has_cover": bool(row["has_cover"]),
        "download_href": f"{base}/download/{row['id']}",
        "cover_href": f"{base}/cover/{row['id']}" if row["has_cover"] else None,
    }


def _feed_response(body: str) -> Response:
    return Response(content=body, media_type=opds.OPDS_ACQUISITION_TYPE)


@app.get("/k/{token}/")
def root_feed(request: Request, token: str = Path(...)) -> Response:
    _resolve_owner(token)
    base = _base_url(request, token)
    body = opds.navigation_feed(
        base=base + "/",
        feed_id=f"anki-deck-reader:{token}",
        title="Anki Deck Reader",
        entries=[
            {"title": "Inbox", "href": base + "/inbox", "content": "New, undelivered books"},
            {"title": "All Books", "href": base + "/all", "content": "Everything ever sent"},
            {"title": "Recent", "href": base + "/recent", "content": "Most recent books"},
        ],
    )
    return Response(content=body, media_type=opds.OPDS_NAVIGATION_TYPE)


@app.get("/k/{token}/inbox")
def inbox_feed(request: Request, token: str = Path(...)) -> Response:
    owner = _resolve_owner(token)
    base = _base_url(request, token)
    books = [_book_dict(request, token, r) for r in _db.inbox(owner)]
    body = opds.acquisition_feed(
        base=base + "/", self_href=base + "/inbox",
        feed_id=f"anki-deck-reader:{token}:inbox", title="Inbox", books=books,
    )
    return _feed_response(body)


@app.get("/k/{token}/all")
def all_feed(request: Request, token: str = Path(...)) -> Response:
    owner = _resolve_owner(token)
    base = _base_url(request, token)
    books = [_book_dict(request, token, r) for r in _db.all_books(owner)]
    body = opds.acquisition_feed(
        base=base + "/", self_href=base + "/all",
        feed_id=f"anki-deck-reader:{token}:all", title="All Books", books=books,
    )
    return _feed_response(body)


@app.get("/k/{token}/recent")
def recent_feed(request: Request, token: str = Path(...)) -> Response:
    owner = _resolve_owner(token)
    base = _base_url(request, token)
    books = [_book_dict(request, token, r) for r in _db.all_books(owner, limit=20)]
    body = opds.acquisition_feed(
        base=base + "/", self_href=base + "/recent",
        feed_id=f"anki-deck-reader:{token}:recent", title="Recent", books=books,
    )
    return _feed_response(body)


# -- download --------------------------------------------------------------
@app.get("/k/{token}/download/{book_id}")
def download(request: Request, token: str, book_id: int) -> Response:
    owner = _resolve_owner(token)
    row = _db.get_book(owner, book_id)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    data = _db.blob_data(row["sha256"])
    if data is None:
        raise HTTPException(status_code=404, detail="not found")
    # Downloading takes the book out of the Inbox.
    _db.mark_delivered(owner, book_id)
    return Response(
        content=data,
        media_type="application/epub+zip",
        headers={
            "Content-Disposition": f'attachment; filename="{row["filename"]}"'
        },
    )
