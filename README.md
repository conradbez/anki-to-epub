Build a new Python app called `anki-deck-reader` that pulls every deck from a
user's AnkiWeb account, binds them into a single EPUB (one chapter per deck),
and auto-syncs that EPUB onto an e-ink reader through a self-hosted OPDS library.
No Anki desktop, no AnkiConnect, no cable, no Calibre.

## Product goal
A learner reviews their Anki cards on an e-ink reader. A scheduled job rebuilds
one consolidated EPUB from all their decks and delivers it; the reader pulls it
exactly once, and it refreshes automatically whenever the decks change.

## Stack & constraints
- Python 3.13, standard library first. Third-party: `requests` (AnkiWeb client),
  `fastapi` + `uvicorn` + `python-multipart` (upload/OPDS server), `pillow`
  (cover rendering only). EPUB generation and parsing = stdlib `zipfile` +
  `xml.etree` ONLY — no ebooklib.
- Deployable on Railway (Procfile + railway.json). SQLite on one volume,
  `replicas = 1` (SQLite has one writer).
- Credentials come ONLY from environment variables — never hard-coded, never
  committed, never logged.

## Architecture — three components

### 1. AnkiWeb client (`anki/client.py`)
AnkiWeb's web app is a SPA talking to a protobuf-over-HTTP backend under `/svc/`,
`Content-Type: application/octet-stream`. Implement a MINIMAL hand-rolled
protobuf codec (varints + length-delimited fields only) — do not add protobuf
as a dependency. Helpers needed: `pb_string(field, str)`, `pb_int(field, int)`,
`pb_message(field, bytes)`, and `pb_parse(bytes) -> list[(field_no, wire, value)]`.

The backend is split across two hosts sharing one account:
- `https://ankiuser.net`  — editor service
- `https://ankiweb.net`   — decks service

Endpoints (login sets an `ankiweb` session cookie):
- POST `/svc/account/login`            body = pb_string(1, user)+pb_string(2, pass);
                                       response field 1 (varint) == 1 means SUCCESS.
- POST `/svc/editor/get-info-for-adding` -> lists notetypes (field 1) and decks
                                       (field 2), each a submessage {1:id, 2:name};
                                       field 3 = current_deck_id, 4 = current_notetype_id.
- POST `/svc/editor/get-notetype-fields` body = pb_int(1, notetype_id) -> field
                                       names in order (e.g. Front, Back).

NEW capability to reverse-engineer: **reading cards out of a deck.** The browse/
search reviewer in the SPA calls a `/svc/...` service with an Anki search query
(e.g. `deck:"Spanish"`). Capture the exact path + request/response shape from
browser devtools and implement `cards_in_deck(name) -> list[(front, back)]`.
If no usable read endpoint exists, implement the FALLBACK: download AnkiWeb's
full `.colpkg` export and read the `notes` table with stdlib `sqlite3`. Pick one,
document which, and make the interface identical either way.

Expose: `login(user, pass)`, `decks() -> [(id, name)]`, `cards_in_deck(name)`,
and `read_all() -> [(deck_name, [(front, back), ...]), ...]` (name-sorted decks,
skip empty decks).

### 2. EPUB builder (`epub/builder.py`)
Pure function: `build_epub(decks, title="My Anki Decks") -> bytes`. Stdlib
`zipfile` only. Requirements:
- First archive entry MUST be `mimetype`, stored UNCOMPRESSED, contents
  `application/epub+zip`.
- `META-INF/container.xml` pointing at `OEBPS/content.opf`.
- `content.opf` (manifest + spine), `toc.ncx` (EPUB2) AND `nav.xhtml` (EPUB3) —
  emit both so more e-ink firmwares show the chapter list.
- One XHTML chapter per deck: deck name as `<h1>`, each card a Front→Back block
  (`<hr/>` between). HTML-escape all field text in v1.
- DETERMINISTIC output: sorted decks, stable card order, fixed date-based book id,
  so an unchanged collection produces byte-identical EPUBs (this drives sync).

### 3. OPDS delivery server (`server/`)
FastAPI + SQLite. Content-addressed storage and deliver-once semantics:
- `Blob(sha256 PK, size, data, cover)` — one row per distinct file, dedup by hash.
- `Book(owner, sha256, title, filename, size, has_cover, added_at, delivered_at)`
  with a UNIQUE constraint on (owner, sha256). `delivered_at IS NULL` == still in
  the Inbox (the entire delivery record).
- `POST /upload` — accepts an EPUB (multipart), validates magic bytes (`PK`) +
  `mimetype` entry + `META-INF/container.xml`, streams to temp file while hashing,
  stores Blob+Book atomically. Re-uploading an identical file is a no-op.
- Per-account **capability URL** `GET /k/<token>/` = an OPDS Atom feed of that
  account's UNDELIVERED books (the Inbox). Token is the whole credential (16 chars,
  unambiguous alphabet). Sub-feeds `All Books` and `Recent`.
- Downloading a book stamps `delivered_at` so it leaves the Inbox.

### 4. Sync job (`sync.py`, runnable as `python -m sync`)
1. `client.login()`, `client.read_all()`.
2. `build_epub(decks)`, `sha256` the bytes.
3. If hash == last shipped hash, log "unchanged" and stop.
4. POST the EPUB to the upload endpoint with the account token.
Change detection is free: unchanged decks -> identical hash -> ingest no-op ->
Inbox stays quiet; changed decks -> new hash -> new Book -> reappears in Inbox.
Note in a comment the "supersede previous book" option (delete the sender's
prior consolidated book on new upload) as a follow-up, not v1.

## Configuration (env only)
ANKI_USERNAME, ANKI_PASSWORD, OPDS_UPLOAD_URL, OPDS_TOKEN, ANKI_BOOK_TITLE (opt),
DATA_DIR (default ./data), SECRET_KEY, ALLOWED_HOSTS.

## Deliverables
- Working code for all four components, wired together.
- `requirements.txt`, `Procfile`, `railway.json`, `.env.example`, `README.md`
  (local run + Railway deploy + reader setup steps).
- Tests: protobuf codec round-trip; `build_epub` produces a structurally valid
  EPUB (unzip, assert mimetype-first-and-stored, container.xml, one chapter per
  deck, deterministic bytes for identical input); upload dedup by sha256;
  Inbox empties after download.
- Railway nightly cron entry that runs `python -m sync`.

## Guardrails
- Credentials env-only; never in source, never logged, never echoed in errors.
- Cap all reads out of any zip archive so a malicious EPUB can't balloon memory.
- Don't trust filenames — a file is an EPUB because its bytes say so.
- Keep it small: this targets one household (~5 users), sized for exactly that.

Start by scaffolding the repo and the protobuf codec + AnkiWeb login (verify
login works against the live service before building further), then the EPUB
builder with tests, then the server, then the sync glue.
