"""Configuration, sourced ONLY from environment variables.

Credentials are never hard-coded and never logged. ``load_config`` reads what
it needs and raises if a required value is missing, so a misconfiguration fails
loudly at startup rather than silently.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"required environment variable {name} is not set")
    return value


@dataclass(frozen=True)
class SyncConfig:
    anki_username: str
    anki_password: str
    upload_url: str
    token: str
    book_title: str

    @classmethod
    def from_env(cls) -> "SyncConfig":
        return cls(
            anki_username=_require("ANKI_USERNAME"),
            anki_password=_require("ANKI_PASSWORD"),
            upload_url=_require("OPDS_UPLOAD_URL"),
            token=_require("OPDS_TOKEN"),
            book_title=os.environ.get("ANKI_BOOK_TITLE", "My Anki Decks"),
        )


@dataclass(frozen=True)
class ServerConfig:
    data_dir: str
    secret_key: str
    allowed_hosts: tuple[str, ...]

    @classmethod
    def from_env(cls) -> "ServerConfig":
        hosts = os.environ.get("ALLOWED_HOSTS", "*")
        return cls(
            data_dir=os.environ.get("DATA_DIR", "./data"),
            secret_key=os.environ.get("SECRET_KEY", ""),
            allowed_hosts=tuple(h.strip() for h in hosts.split(",") if h.strip()),
        )
