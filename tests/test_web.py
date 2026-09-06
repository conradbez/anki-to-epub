"""Web account flow: signup, login, logout, dashboard, token isolation."""

import re

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from server import app as app_module  # noqa: E402

CSRF_RE = re.compile(r'name="csrfmiddlewaretoken" value="([^"]+)"')


def _csrf(client, path):
    html = client.get(path).text
    return CSRF_RE.search(html).group(1)


@pytest.fixture()
def client():
    return TestClient(app_module.app)


def test_index_redirects_to_login(client):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login/"


def test_signup_then_dashboard(client):
    csrf = _csrf(client, "/signup/")
    resp = client.post(
        "/signup/",
        data={
            "username": "alice",
            "password1": "supersecret",
            "password2": "supersecret",
            "csrfmiddlewaretoken": csrf,
        },
    )
    assert resp.status_code == 200
    assert "Welcome, alice" in resp.text
    # dashboard exposes a reader URL and token
    assert "/k/" in resp.text
    assert "OPDS_TOKEN" in resp.text


def test_signup_password_mismatch(client):
    csrf = _csrf(client, "/signup/")
    resp = client.post(
        "/signup/",
        data={
            "username": "bob",
            "password1": "supersecret",
            "password2": "different1",
            "csrfmiddlewaretoken": csrf,
        },
    )
    assert "Passwords do not match" in resp.text


def test_duplicate_username_rejected(client):
    for expected in ("Welcome, carol", "taken"):
        csrf = _csrf(client, "/signup/")
        resp = client.post(
            "/signup/",
            data={
                "username": "carol",
                "password1": "supersecret",
                "password2": "supersecret",
                "csrfmiddlewaretoken": csrf,
            },
        )
        assert expected in resp.text


def test_login_logout_cycle(client):
    csrf = _csrf(client, "/signup/")
    client.post(
        "/signup/",
        data={
            "username": "dave",
            "password1": "supersecret",
            "password2": "supersecret",
            "csrfmiddlewaretoken": csrf,
        },
    )
    # log out
    logout_csrf = _csrf(client, "/dashboard")
    client.post("/logout/", data={"csrfmiddlewaretoken": logout_csrf})
    # dashboard now redirects to login
    resp = client.get("/dashboard", follow_redirects=False)
    assert resp.status_code == 303
    # bad password rejected
    csrf = _csrf(client, "/login/")
    bad = client.post(
        "/login/",
        data={"username": "dave", "password": "wrong", "csrfmiddlewaretoken": csrf},
    )
    assert "Invalid username or password" in bad.text
    # good password logs in
    csrf = _csrf(client, "/login/")
    good = client.post(
        "/login/",
        data={
            "username": "dave",
            "password": "supersecret",
            "csrfmiddlewaretoken": csrf,
        },
    )
    assert "Welcome, dave" in good.text


def test_csrf_required(client):
    resp = client.post(
        "/signup/",
        data={
            "username": "eve",
            "password1": "supersecret",
            "password2": "supersecret",
            "csrfmiddlewaretoken": "bogus",
        },
    )
    assert "Session expired" in resp.text


def test_signup_mints_working_token(client):
    csrf = _csrf(client, "/signup/")
    resp = client.post(
        "/signup/",
        data={
            "username": "frank",
            "password1": "supersecret",
            "password2": "supersecret",
            "csrfmiddlewaretoken": csrf,
        },
    )
    token = re.search(r"/k/([A-Z2-9]{16})/", resp.text).group(1)
    # the freshly minted token resolves as a real OPDS account
    feed = client.get(f"/k/{token}/")
    assert feed.status_code == 200
    assert "Inbox" in feed.text
