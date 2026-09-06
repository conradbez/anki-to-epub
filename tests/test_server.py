"""OPDS server tests: upload dedup by sha256, Inbox empties after download."""

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from epub.builder import build_epub  # noqa: E402
from server import app as app_module  # noqa: E402

TOKEN = "ABCDEFGHJKMNPQRS"  # valid shape (16 chars, unambiguous alphabet)
OWNER = "household"


@pytest.fixture()
def client():
    app_module._db.upsert_account(TOKEN, OWNER)
    return TestClient(app_module.app)


def _epub():
    return build_epub([("Spanish", [("hola", "hello")])])


def _upload(client, data, filename="decks.epub"):
    return client.post(
        "/upload",
        files={"file": (filename, data, "application/epub+zip")},
        headers={"X-Auth-Token": TOKEN},
    )


def test_upload_requires_valid_token(client):
    resp = client.post(
        "/upload",
        files={"file": ("x.epub", _epub(), "application/epub+zip")},
        headers={"X-Auth-Token": "bogus"},
    )
    assert resp.status_code == 404


def test_upload_rejects_non_epub(client):
    resp = _upload(client, b"not a zip at all")
    assert resp.status_code == 400


def test_upload_dedup_by_sha256(client):
    data = _epub()
    first = _upload(client, data)
    second = _upload(client, data, filename="different-name.epub")
    assert first.json()["status"] == "stored"
    assert second.json()["status"] == "duplicate"
    assert first.json()["sha256"] == second.json()["sha256"]

    feed = client.get(f"/k/{TOKEN}/all").text
    assert feed.count("<entry>") == 1


def test_inbox_empties_after_download(client):
    _upload(client, _epub())

    inbox = client.get(f"/k/{TOKEN}/inbox").text
    assert inbox.count("<entry>") == 1

    # find the book id from the acquisition link
    all_feed = client.get(f"/k/{TOKEN}/all").text
    start = all_feed.find("/download/") + len("/download/")
    book_id = all_feed[start:].split('"')[0]

    dl = client.get(f"/k/{TOKEN}/download/{book_id}")
    assert dl.status_code == 200
    assert dl.content[:2] == b"PK"

    inbox_after = client.get(f"/k/{TOKEN}/inbox").text
    assert inbox_after.count("<entry>") == 0

    # ...but it still shows in All Books
    all_after = client.get(f"/k/{TOKEN}/all").text
    assert all_after.count("<entry>") == 1


def test_root_navigation_feed(client):
    resp = client.get(f"/k/{TOKEN}/")
    assert resp.status_code == 200
    assert "Inbox" in resp.text
    assert "All Books" in resp.text
    assert "Recent" in resp.text
