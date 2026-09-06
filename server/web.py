"""Browser-facing account UI: signup, login, logout, dashboard.

Server-side sessions (an opaque id in an HttpOnly cookie); passwords are
PBKDF2-hashed (see :mod:`server.auth`). A double-submit CSRF token plus
``SameSite=Lax`` cookies guard the state-changing POSTs.

On signup an account's capability token is minted; the dashboard shows the
reader URL (``/k/<token>/``) and the upload token the sync job needs. Open
signup can be gated with the ``SIGNUP_CODE`` env var (unset = open, like the
reference app).
"""

from __future__ import annotations

import html
import os
import re

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from . import auth
from .db import UserExists
from .tokens import new_token

router = APIRouter()

SESSION_COOKIE = "session"
CSRF_COOKIE = "csrf"
SESSION_TTL = 30 * 24 * 3600  # 30 days

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
_MIN_PASSWORD = 8


# -- dependencies bound in app.py -----------------------------------------
def _db(request: Request):
    return request.app.state.db


def _signup_code() -> str:
    return os.environ.get("SIGNUP_CODE", "")


# -- request helpers -------------------------------------------------------
def _external_base(request: Request) -> str:
    """Absolute base URL, honouring the reverse proxy (Railway)."""
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host") or request.headers.get(
        "host", request.url.netloc
    )
    return f"{proto}://{host}"


def _is_secure(request: Request) -> bool:
    return request.headers.get("x-forwarded-proto", request.url.scheme) == "https"


def _current_user(request: Request) -> str | None:
    sid = request.cookies.get(SESSION_COOKIE)
    if not sid:
        return None
    return _db(request).session_username(sid)


def _set_cookie(resp: Response, name: str, value: str, request: Request, max_age: int):
    resp.set_cookie(
        name,
        value,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=_is_secure(request),
        path="/",
    )


def _check_csrf(request: Request, form_token: str) -> bool:
    return auth.csrf_ok(request.cookies.get(CSRF_COOKIE), form_token)


# -- routes ----------------------------------------------------------------
@router.get("/", response_class=HTMLResponse)
def index(request: Request):
    if _current_user(request):
        return RedirectResponse("/dashboard", status_code=303)
    return RedirectResponse("/login/", status_code=303)


@router.get("/signup/", response_class=HTMLResponse)
@router.get("/signup", response_class=HTMLResponse)
def signup_form(request: Request):
    if _current_user(request):
        return RedirectResponse("/dashboard", status_code=303)
    return _reissue(request, _signup_page, None)


@router.post("/signup/", response_class=HTMLResponse)
@router.post("/signup", response_class=HTMLResponse)
def signup_submit(
    request: Request,
    username: str = Form(...),
    password1: str = Form(...),
    password2: str = Form(...),
    csrfmiddlewaretoken: str = Form(""),
    code: str = Form(""),
):
    if not _check_csrf(request, csrfmiddlewaretoken):
        return _reissue(request, _signup_page, "Session expired, please retry.")
    username = username.strip()
    required = _signup_code()
    if required and code.strip() != required:
        return _reissue(request, _signup_page, "Invalid signup code.")
    if not _USERNAME_RE.match(username):
        return _reissue(
            request, _signup_page,
            "Username must be 3-32 chars: letters, digits, . _ -",
        )
    if len(password1) < _MIN_PASSWORD:
        return _reissue(
            request, _signup_page,
            f"Password must be at least {_MIN_PASSWORD} characters.",
        )
    if password1 != password2:
        return _reissue(request, _signup_page, "Passwords do not match.")

    pw_hash, pw_salt = auth.hash_password(password1)
    token = _unique_token(_db(request))
    try:
        _db(request).create_user(username, pw_hash, pw_salt, token)
    except UserExists:
        return _reissue(request, _signup_page, "That username is taken.")

    return _start_session(request, username, "/dashboard")


@router.get("/login/", response_class=HTMLResponse)
@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if _current_user(request):
        return RedirectResponse("/dashboard", status_code=303)
    return _reissue(request, _login_page, None, status_code=200)


@router.post("/login/", response_class=HTMLResponse)
@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    csrfmiddlewaretoken: str = Form(""),
):
    if not _check_csrf(request, csrfmiddlewaretoken):
        return _reissue(request, _login_page, "Session expired, please retry.")
    row = _db(request).get_user(username.strip())
    if row is None or not auth.verify_password(
        password, row["pw_hash"], row["pw_salt"]
    ):
        # Same message either way -> don't reveal whether the user exists.
        return _reissue(request, _login_page, "Invalid username or password.")
    return _start_session(request, row["username"], "/dashboard")


@router.post("/logout/", response_class=HTMLResponse)
@router.post("/logout", response_class=HTMLResponse)
def logout(request: Request, csrfmiddlewaretoken: str = Form("")):
    sid = request.cookies.get(SESSION_COOKIE)
    if sid and _check_csrf(request, csrfmiddlewaretoken):
        _db(request).delete_session(sid)
    resp = RedirectResponse("/login/", status_code=303)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@router.post("/dashboard/rotate", response_class=HTMLResponse)
def rotate_token(request: Request, csrfmiddlewaretoken: str = Form("")):
    user = _current_user(request)
    if user and _check_csrf(request, csrfmiddlewaretoken):
        _db(request).rotate_token(user, _unique_token(_db(request)))
    return RedirectResponse("/dashboard", status_code=303)


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login/", status_code=303)
    csrf = auth.new_csrf_token()
    resp = HTMLResponse(_dashboard_page(request, user, csrf))
    _set_cookie(resp, CSRF_COOKIE, csrf, request, SESSION_TTL)
    return resp


# -- flow helpers ----------------------------------------------------------
def _unique_token(db) -> str:
    for _ in range(10):
        token = new_token()
        if db.owner_for_token(token) is None:
            return token
    raise RuntimeError("could not mint a unique token")


def _start_session(request: Request, username: str, redirect_to: str) -> Response:
    db = _db(request)
    db.purge_expired_sessions()
    sid = auth.new_session_id()
    db.create_session(sid, username, SESSION_TTL)
    resp = RedirectResponse(redirect_to, status_code=303)
    _set_cookie(resp, SESSION_COOKIE, sid, request, SESSION_TTL)
    return resp


def _reissue(request, page_fn, error, status_code: int = 200) -> HTMLResponse:
    """Render a form page with a fresh CSRF token (and optional error)."""
    csrf = auth.new_csrf_token()
    resp = HTMLResponse(page_fn(request, csrf=csrf, error=error), status_code=status_code)
    _set_cookie(resp, CSRF_COOKIE, csrf, request, SESSION_TTL)
    return resp


# -- HTML ------------------------------------------------------------------
_CSS = """
:root{color-scheme:light dark}
*{box-sizing:border-box}
body{margin:0;font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,
  Helvetica,Arial,sans-serif;background:#f6f7f9;color:#1b1f24}
@media(prefers-color-scheme:dark){body{background:#14171a;color:#e6e8ea}}
.bar{display:flex;align-items:center;justify-content:space-between;
  padding:14px 20px;border-bottom:1px solid rgba(128,128,128,.25)}
.brand{font-weight:600;text-decoration:none;color:inherit}
main{max-width:640px;margin:32px auto;padding:0 16px}
.card{background:rgba(255,255,255,.7);border:1px solid rgba(128,128,128,.25);
  border-radius:14px;padding:24px;margin-bottom:20px}
@media(prefers-color-scheme:dark){.card{background:rgba(255,255,255,.04)}}
.narrow{max-width:400px;margin:40px auto}
h1{margin:0 0 16px;font-size:22px}
label{display:block;margin:12px 0 4px;font-weight:500;font-size:14px}
input[type=text],input[type=password]{width:100%;padding:10px 12px;
  border:1px solid rgba(128,128,128,.4);border-radius:8px;background:transparent;
  color:inherit;font-size:15px}
button{margin-top:18px;width:100%;padding:11px;border:0;border-radius:8px;
  background:#2f6df6;color:#fff;font-size:15px;font-weight:600;cursor:pointer}
button.secondary{background:transparent;border:1px solid rgba(128,128,128,.5);
  color:inherit}
button.danger{background:transparent;border:1px solid #d9534f;color:#d9534f}
.hint{margin-top:16px;font-size:14px;color:#6b7280}
a{color:#2f6df6}
.err{background:#fdecea;color:#b3261e;border:1px solid #f4c7c3;padding:10px 12px;
  border-radius:8px;font-size:14px;margin-bottom:8px}
@media(prefers-color-scheme:dark){.err{background:#3b1f1e;color:#f2b8b5;
  border-color:#5c2b28}}
.kv{margin:14px 0}
.kv .k{font-size:13px;color:#6b7280;margin-bottom:4px}
.copyrow{display:flex;gap:8px}
.copyrow input{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
  font-size:13px}
.copyrow button{margin:0;width:auto;padding:0 14px;font-size:13px;background:#555}
.stats{display:flex;gap:24px;margin:8px 0 4px}
.stat b{font-size:22px;display:block}
.stat span{font-size:13px;color:#6b7280}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px;
  background:rgba(128,128,128,.15);padding:1px 5px;border-radius:4px}
.rowforms{display:flex;gap:10px;flex-wrap:wrap}
.rowforms form{flex:1;min-width:150px}
"""

_COPY_JS = """
document.addEventListener('click',function(e){
  var b=e.target.closest('[data-copy]');if(!b)return;
  var i=document.getElementById(b.getAttribute('data-copy'));
  if(!i)return;i.select();navigator.clipboard&&navigator.clipboard.writeText(i.value);
  var t=b.textContent;b.textContent='Copied';setTimeout(function(){b.textContent=t},1200);
});
"""


def _page(title: str, body: str, *, logged_in: bool = False) -> str:
    nav = ""
    if logged_in:
        nav = '<a href="/dashboard">Dashboard</a>'
    return (
        "<!doctype html>\n<html lang=\"en\"><head>"
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title>"
        f"<style>{_CSS}</style>"
        '<link rel="icon" href="data:image/svg+xml,'
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'>"
        "<text y='14' font-size='14'>📚</text></svg>\">"
        "</head><body>"
        '<header class="bar"><a class="brand" href="/">📚 Anki Deck Reader</a>'
        f"{nav}</header><main>{body}</main>"
        f"<script>{_COPY_JS}</script>"
        "</body></html>"
    )


def _err_html(error: str | None) -> str:
    return f'<div class="err">{html.escape(error)}</div>' if error else ""


def _signup_page(request: Request, csrf: str = "", error: str | None = None) -> str:
    code_field = ""
    if _signup_code():
        code_field = (
            '<label for="id_code">Signup code</label>'
            '<input type="text" name="code" id="id_code" required>'
        )
    body = (
        '<div class="card narrow"><h1>Create account</h1>'
        f"{_err_html(error)}"
        '<form method="post" action="/signup/">'
        f'<input type="hidden" name="csrfmiddlewaretoken" value="{html.escape(csrf)}">'
        '<label for="id_username">Username</label>'
        '<input type="text" name="username" id="id_username" autofocus '
        'autocapitalize="none" autocomplete="username" required>'
        '<label for="id_password1">Password</label>'
        '<input type="password" name="password1" id="id_password1" '
        'autocomplete="new-password" required>'
        '<label for="id_password2">Confirm password</label>'
        '<input type="password" name="password2" id="id_password2" '
        'autocomplete="new-password" required>'
        f"{code_field}"
        "<button type=\"submit\">Create account</button></form>"
        '<p class="hint">Already have one? <a href="/login/">Sign in</a>.</p>'
        "</div>"
    )
    return _page("Create account", body)


def _login_page(request: Request, csrf: str = "", error: str | None = None) -> str:
    body = (
        '<div class="card narrow"><h1>Sign in</h1>'
        f"{_err_html(error)}"
        '<form method="post" action="/login/">'
        f'<input type="hidden" name="csrfmiddlewaretoken" value="{html.escape(csrf)}">'
        '<label for="id_username">Username</label>'
        '<input type="text" name="username" id="id_username" autofocus '
        'autocapitalize="none" autocomplete="username" required>'
        '<label for="id_password">Password</label>'
        '<input type="password" name="password" id="id_password" '
        'autocomplete="current-password" required>'
        "<button type=\"submit\">Sign in</button></form>"
        '<p class="hint">No account yet? <a href="/signup/">Create one</a>.</p>'
        "</div>"
    )
    return _page("Sign in", body)


def _copy_row(field_id: str, value: str) -> str:
    v = html.escape(value, quote=True)
    return (
        '<div class="copyrow">'
        f'<input type="text" id="{field_id}" value="{v}" readonly>'
        f'<button type="button" data-copy="{field_id}">Copy</button>'
        "</div>"
    )


def _dashboard_page(request: Request, username: str, csrf: str) -> str:
    db = _db(request)
    token = db.user_token(username) or ""
    inbox, total = db.counts(username)
    base = _external_base(request)
    reader_url = f"{base}/k/{token}/"
    upload_url = f"{base}/upload"
    esc_csrf = html.escape(csrf)

    body = (
        f'<div class="card"><h1>Welcome, {html.escape(username)}</h1>'
        '<div class="stats">'
        f'<div class="stat"><b>{inbox}</b><span>in Inbox</span></div>'
        f'<div class="stat"><b>{total}</b><span>total books</span></div>'
        "</div></div>"
        '<div class="card"><h1>Your reader</h1>'
        "<p>Add this URL as an OPDS catalog in your e-ink reader "
        "(KOReader, Marvin, Foliate…). No username or password &mdash; the "
        "token in the URL is the credential, so keep it private.</p>"
        '<div class="kv"><div class="k">Reader catalog URL</div>'
        f"{_copy_row('reader_url', reader_url)}</div>"
        "</div>"
        '<div class="card"><h1>Sync job settings</h1>'
        "<p>Set these environment variables for the "
        "<code>python -m sync</code> job that uploads your decks:</p>"
        '<div class="kv"><div class="k">OPDS_UPLOAD_URL</div>'
        f"{_copy_row('upload_url', upload_url)}</div>"
        '<div class="kv"><div class="k">OPDS_TOKEN</div>'
        f"{_copy_row('token', token)}</div>"
        "</div>"
        '<div class="card"><h1>Account</h1>'
        '<div class="rowforms">'
        '<form method="post" action="/dashboard/rotate" '
        "onsubmit=\"return confirm('Regenerate token? Your current reader URL "
        "and sync config will stop working until updated.')\">"
        f'<input type="hidden" name="csrfmiddlewaretoken" value="{esc_csrf}">'
        '<button type="submit" class="danger">Regenerate token</button></form>'
        '<form method="post" action="/logout/">'
        f'<input type="hidden" name="csrfmiddlewaretoken" value="{esc_csrf}">'
        '<button type="submit" class="secondary">Sign out</button></form>'
        "</div></div>"
    )
    return _page("Dashboard", body, logged_in=True)
