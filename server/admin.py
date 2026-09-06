"""Tiny account admin CLI.

Provision an account and its capability token (the whole credential for the
Inbox URL). Usage::

    python -m server.admin add <owner>      # create/rotate a token, print it
    python -m server.admin list             # list owners (tokens are secret)

The token is printed once; put it in the sync job's ``OPDS_TOKEN`` and give the
reader the URL ``<host>/k/<token>/``.
"""

from __future__ import annotations

import os
import sys

from config import ServerConfig
from .db import Database
from .tokens import new_token


def _db() -> Database:
    cfg = ServerConfig.from_env()
    return Database(os.path.join(cfg.data_dir, "library.db"))


def main(argv: list[str]) -> int:
    if len(argv) < 1:
        print(__doc__)
        return 2
    cmd = argv[0]
    db = _db()
    if cmd == "add" and len(argv) == 2:
        owner = argv[1]
        token = new_token()
        db.upsert_account(token, owner)
        print(f"owner={owner}")
        print(f"token={token}")
        print(f"inbox_url=/k/{token}/")
        return 0
    if cmd == "list":
        # Deliberately does not print tokens.
        with db._tx() as conn:  # noqa: SLF001 - admin-only introspection
            for row in conn.execute("SELECT owner FROM account ORDER BY owner"):
                print(row["owner"])
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
