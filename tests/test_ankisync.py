"""Web-triggered AnkiWeb sync: in-process pipeline + endpoint wiring."""

import pytest

pytest.importorskip("fastapi")

from server import ankisync  # noqa: E402
from server.db import Database  # noqa: E402


class _FakeClient:
    """Stand-in for AnkiWebClient that never touches the network."""

    decks = [("Spanish", [("hola", "hello"), ("gato", "cat")])]
    fail = False

    def login(self, user, password):
        assert user and password
        if self._fail:
            from anki.client import AnkiWebError

            raise AnkiWebError("login failed: invalid credentials or blocked")

    def read_all(self):
        return self._decks


def _factory(decks=None, fail=False):
    def make():
        c = _FakeClient()
        c._decks = decks if decks is not None else _FakeClient.decks
        c._fail = fail
        return c

    return make


@pytest.fixture()
def db(tmp_path):
    d = Database(str(tmp_path / "lib.db"))
    d.create_user("alice", "h", "s", "TESTTOKEN00000AA")
    return d


def test_sync_delivers_book(db, monkeypatch):
    monkeypatch.setattr(ankisync, "_client_factory", _factory())
    ankisync._run(db, "alice", "user", "pw", "My Anki Decks")

    state = db.get_sync_state("alice")
    assert state["status"] == "ok"
    assert "Synced" in state["message"]
    assert len(db.inbox("alice")) == 1
    assert state["last_hash"]


def test_sync_unchanged_is_noop(db, monkeypatch):
    monkeypatch.setattr(ankisync, "_client_factory", _factory())
    ankisync._run(db, "alice", "user", "pw", "My Anki Decks")
    ankisync._run(db, "alice", "user", "pw", "My Anki Decks")

    assert len(db.inbox("alice")) == 1  # not duplicated
    assert "No changes" in db.get_sync_state("alice")["message"]


def test_sync_changed_delivers_again(db, monkeypatch):
    monkeypatch.setattr(ankisync, "_client_factory", _factory())
    ankisync._run(db, "alice", "user", "pw", "My Anki Decks")

    changed = [("Spanish", [("hola", "HELLO CHANGED")])]
    monkeypatch.setattr(ankisync, "_client_factory", _factory(decks=changed))
    ankisync._run(db, "alice", "user", "pw", "My Anki Decks")

    assert len(db.inbox("alice")) == 2


def test_sync_empty_decks_errors(db, monkeypatch):
    monkeypatch.setattr(ankisync, "_client_factory", _factory(decks=[]))
    ankisync._run(db, "alice", "user", "pw", "My Anki Decks")

    assert db.get_sync_state("alice")["status"] == "error"
    assert len(db.inbox("alice")) == 0


def test_sync_bad_login_reports_error_without_leaking(db, monkeypatch):
    monkeypatch.setattr(ankisync, "_client_factory", _factory(fail=True))
    ankisync._run(db, "alice", "user", "sekret-password", "My Anki Decks")

    state = db.get_sync_state("alice")
    assert state["status"] == "error"
    assert "sekret-password" not in state["message"]
    assert len(db.inbox("alice")) == 0


# -- endpoint wiring -------------------------------------------------------
from fastapi.testclient import TestClient  # noqa: E402
from server import app as app_module  # noqa: E402
from server import web as web_module  # noqa: E402


def _signup(client, name):
    import re

    csrf = re.search(
        r'name="csrfmiddlewaretoken" value="([^"]+)"', client.get("/signup/").text
    ).group(1)
    client.post(
        "/signup/",
        data={
            "username": name,
            "password1": "supersecret",
            "password2": "supersecret",
            "csrfmiddlewaretoken": csrf,
        },
    )


def test_sync_endpoint_requires_login():
    client = TestClient(app_module.app)
    resp = client.post(
        "/dashboard/sync",
        data={"anki_username": "x", "anki_password": "y"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login/"


def test_sync_endpoint_starts_sync(monkeypatch):
    calls = []
    monkeypatch.setattr(
        web_module, "start_sync", lambda *a, **k: calls.append(a)
    )
    client = TestClient(app_module.app)
    _signup(client, "grace")
    import re

    csrf = re.search(
        r'name="csrfmiddlewaretoken" value="([^"]+)"', client.get("/dashboard").text
    ).group(1)
    client.post(
        "/dashboard/sync",
        data={
            "anki_username": "me@example.com",
            "anki_password": "pw",
            "csrfmiddlewaretoken": csrf,
        },
    )
    assert len(calls) == 1
    # (db, owner, anki_user, anki_pass, title)
    assert calls[0][1] == "grace"
    assert calls[0][2] == "me@example.com"


def test_sync_endpoint_rejects_bad_csrf(monkeypatch):
    calls = []
    monkeypatch.setattr(web_module, "start_sync", lambda *a, **k: calls.append(a))
    client = TestClient(app_module.app)
    _signup(client, "heidi")
    client.post(
        "/dashboard/sync",
        data={
            "anki_username": "me@example.com",
            "anki_password": "pw",
            "csrfmiddlewaretoken": "bogus",
        },
    )
    assert calls == []
