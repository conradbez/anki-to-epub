"""Test setup: point the server at a throwaway data dir before it imports."""

import os
import tempfile

# The server reads config (incl. DATA_DIR) at import time, so set it before any
# test module imports server.app.
_TMP = tempfile.mkdtemp(prefix="anki-deck-reader-tests-")
os.environ.setdefault("DATA_DIR", _TMP)
os.environ.setdefault("SECRET_KEY", "test-secret")
