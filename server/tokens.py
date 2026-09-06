"""Capability-URL token helpers.

A token is the whole credential for an account's Inbox, so it must be
unguessable. 16 chars from a 32-symbol unambiguous alphabet (no 0/O/1/I/L) is
~80 bits of entropy -- plenty for a ~5-user household.
"""

from __future__ import annotations

import secrets

ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"  # no 0 O 1 I L
TOKEN_LEN = 16


def new_token() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(TOKEN_LEN))


def is_valid_shape(token: str) -> bool:
    return (
        len(token) == TOKEN_LEN
        and all(c in ALPHABET for c in token)
    )
