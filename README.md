# anki-deck-reader

Pull every deck from an AnkiWeb account, bind them into a single EPUB (one
chapter per deck), and auto-sync that EPUB onto an e-ink reader through a
self-hosted OPDS library.

**No Anki desktop, no AnkiConnect, no cable, no Calibre.**

A learner reviews their Anki cards on an e-ink reader. A scheduled job rebuilds
one consolidated EPUB from all their decks and delivers it; the reader pulls it
exactly once, and it refreshes automatically whenever the decks change.

## How it works

```
AnkiWeb ──(anki/client.py)──> decks & cards
        ──(epub/builder.py)──> one deterministic EPUB (sha256)
        ──(sync.py)─────────> POST /upload  ─────> OPDS server (server/)
                                                    e-ink reader pulls
                                                    /k/<token>/  (Inbox)
```

Change detection is free: unchanged decks produce a **byte-identical** EPUB, so
its hash is unchanged, the upload is a no-op, and the reader's Inbox stays quiet.
Change a card and the hash changes, a new book appears in the Inbox, and the
reader downloads it once.

## Components

| Path                | Role |
|---------------------|------|
| `anki/protobuf.py`  | Minimal hand-rolled protobuf codec (varint + length-delimited). No `protobuf` dependency. |
| `anki/client.py`    | AnkiWeb login + deck/notetype listing over `/svc/`. |
| `anki/colpkg.py`    | Reads cards by parsing a `.colpkg` export with stdlib `sqlite3` (see below). |
| `epub/builder.py`   | `build_epub(decks, title)` — deterministic EPUB2+EPUB3, stdlib `zipfile` only. |
| `server/app.py`     | FastAPI + SQLite OPDS server: content-addressed storage, deliver-once Inbox. |
| `server/web.py`     | Browser account UI: signup, login, logout, dashboard, Sync now. |
| `server/ankisync.py`| In-process AnkiWeb sync triggered from the dashboard (background thread). |
| `sync.py`           | The glue: log in → read decks → build → hash → upload if changed. |

### How cards are read

The README's spec allows either a reverse-engineered read RPC **or** a fallback
that downloads the full `.colpkg` export and reads it with `sqlite3`. **This
implementation uses the `.colpkg` fallback** — it has no dependency on an
undocumented, version-unstable SPA endpoint. `anki/colpkg.py` opens the export's
`collection.anki21`/`.anki2` SQLite database read-only and joins
`cards → notes → decks` to produce `{deck_name: [(front, back), …]}`. The public
`cards_in_deck()` / `read_all()` interface is identical regardless of strategy,
so an RPC reader can be dropped in later without touching callers.

> The one endpoint that needs confirming against live devtools is the export
> download path in `AnkiWebClient._download_export`. For offline use, set
> `ANKI_COLPKG_PATH` to a manually exported `.colpkg` and everything downstream
> works unchanged.

## Configuration (environment variables only)

Credentials come **only** from the environment — never hard-coded, committed, or
logged. Copy `.env.example` to `.env`:

| Var | Used by | Notes |
|-----|---------|-------|
| `ANKI_USERNAME`, `ANKI_PASSWORD` | sync | AnkiWeb login |
| `OPDS_UPLOAD_URL` | sync | e.g. `https://your-app.up.railway.app/upload` |
| `OPDS_TOKEN` | sync | account capability token (see below) |
| `ANKI_BOOK_TITLE` | sync | optional, default `My Anki Decks` |
| `ANKI_COLPKG_PATH` | sync | optional; read a local export instead of AnkiWeb |
| `DATA_DIR` | server | default `./data`; put the SQLite file + sync state here |
| `SECRET_KEY`, `ALLOWED_HOSTS` | server | |
| `SIGNUP_CODE` | server | optional; if set, the web signup page requires this code |

## Accounts

Create an account from the web UI (the same flow as a normal web app):

- `GET /signup/` — create an account (username + password).
- `GET /login/` — sign in.
- `GET /dashboard` — after login, lets you **Sync now** (below), and shows your
  **reader catalog URL** (`/k/<token>/`), the CLI `OPDS_UPLOAD_URL` /
  `OPDS_TOKEN` values, and a *Regenerate token* button if a token ever leaks.
- `/` redirects to the dashboard (if signed in) or the login page.

Passwords are stored as PBKDF2-HMAC-SHA256 with a per-user salt; sessions are
server-side (an opaque id in an HttpOnly, `SameSite=Lax` cookie) and form POSTs
carry a double-submit CSRF token. Set `SIGNUP_CODE` to gate open signup.

### Sync from the web (no CLI needed)

The dashboard has a **Sync your Anki decks** form: enter your AnkiWeb email and
password and click **Sync now**. The server logs in to AnkiWeb, reads every
deck, builds the consolidated EPUB, and drops it straight into your Inbox — the
same pipeline as `python -m sync`, but in-process and on demand. Status
(running / synced / error) shows on the dashboard and refreshes itself while a
sync runs.

Your **AnkiWeb password is used only for that one sync and is never stored or
logged** — it is not written to the database and never appears in status
messages. (That's why automatic nightly syncing still uses the CLI/cron path
with env-var credentials; the web path is manual by design.) Unchanged decks
produce an identical EPUB hash, so re-syncing is a no-op and your Inbox stays
quiet.

> Prefer the CLI (e.g. for scripting)? `python -m server.admin add <owner>`
> still provisions an account + token without the web form.

## Local run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 1. Start the OPDS server
export DATA_DIR=./data SECRET_KEY=$(python -c "import secrets;print(secrets.token_hex(32))")
uvicorn server.app:app --reload --port 8000

# 2. Create an account in the browser at http://localhost:8000/signup/
#    then copy the reader URL + OPDS_TOKEN from the dashboard.

# 3. Run the sync job
export ANKI_USERNAME=... ANKI_PASSWORD=...
export OPDS_UPLOAD_URL=http://localhost:8000/upload
export OPDS_TOKEN=<token from the dashboard>
python -m sync
```

Point your reader (or a browser) at `http://localhost:8000/k/<token>/`.

## Reader setup (e-ink)

Most e-ink readers with an OPDS catalog client (KOReader, Marvin, Foliate, etc.)
just need the capability URL:

1. In the reader's OPDS/catalog settings, **add a catalog** with the URL from
   your dashboard, `https://<your-host>/k/<token>/`. No username/password — the
   token *is* the credential.
2. Open **Inbox** to see undelivered books. Downloading a book stamps it
   delivered, so it leaves the Inbox and won't be pulled again.
3. **All Books** and **Recent** sub-feeds show history.

Keep the token secret and use HTTPS — anyone with the URL can read the Inbox.

## Railway deploy

1. Create a Railway project from this repo (`Procfile` + `railway.json` are
   included). It uses Nixpacks and starts `uvicorn server.app:app`.
2. **Add a volume** and set `DATA_DIR` to its mount path (e.g. `/data`) so the
   library and last-hash state survive redeploys. Keep **`replicas = 1`** —
   SQLite has a single writer.
3. Set `SECRET_KEY`, `ALLOWED_HOSTS`.
4. Create your account at `https://<your-app>/signup/` and copy the reader URL
   and `OPDS_TOKEN` from the dashboard (or use `python -m server.admin add
   <owner>` in a Railway shell). Set `SIGNUP_CODE` first if you want to keep
   signup private.
5. **Nightly cron:** `railway.json` declares a cron entry
   (`0 3 * * *` → `python -m sync`). Set `ANKI_USERNAME`, `ANKI_PASSWORD`,
   `OPDS_UPLOAD_URL`, `OPDS_TOKEN` on the cron service (or as shared variables).

## Tests

```bash
pip install pytest httpx
python -m pytest
```

Covers: protobuf codec round-trip; `build_epub` structural validity
(mimetype-first-and-stored, `container.xml`, one chapter per deck, deterministic
bytes for identical input); the `.colpkg` reader; upload dedup by sha256; the
Inbox emptying after download; the web account flow (signup, login, logout,
CSRF, token minting); and the web-triggered sync (delivers a book, skips
unchanged decks, reports errors without leaking the password).

## Guardrails

- Credentials are env-only — never in source, never logged, never echoed in errors.
- Every read out of a zip archive (uploads, colpkg, EPUB validation) is size-capped
  so a malicious archive can't balloon memory.
- A file is treated as an EPUB because its **bytes** say so (`PK` magic +
  `mimetype` entry + `container.xml`), not because of its filename.
- Sized for one household (~5 users).

## Follow-ups (not v1)

- **Supersede previous book:** on a new upload, delete the sender's prior
  consolidated book so the Inbox never accumulates stale editions.
- Cover rendering with Pillow (dependency already declared).
- Rich card HTML (currently all field text is HTML-escaped as plain text).
