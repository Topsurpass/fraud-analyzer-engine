"""Fernet encryption for target-database credentials.

Credentials are encrypted at rest and decrypted only in-process, immediately
before building a connection URL. No plaintext password is ever persisted and
no encrypted value is ever returned by the API.

Key resolution order:

1. ``FAE_FERNET_KEY`` environment variable (the production path).
2. ``.secrets/fernet.key`` on disk.
3. Generate one, write it to ``.secrets/fernet.key`` with mode 0600, and log a
   warning. This keeps local dev frictionless; ``.secrets/`` is gitignored.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet

from app.config import get_settings

logger = logging.getLogger(__name__)

KEY_DIR = Path(".secrets")
KEY_PATH = KEY_DIR / "fernet.key"

#: True when the key came from disk or was generated, rather than from
#: FAE_FERNET_KEY. Startup reports this: a generated key on an ephemeral
#: filesystem makes every stored credential undecryptable after a restart.
_key_is_ephemeral = False


def key_is_ephemeral() -> bool:
    """Whether the encryption key would survive a container restart."""
    get_fernet()  # resolve the key if it has not been resolved yet
    return _key_is_ephemeral


def generate_key() -> str:
    """Return a fresh urlsafe-base64 Fernet key as text."""
    return Fernet.generate_key().decode("ascii")


def _load_or_create_key() -> str:
    global _key_is_ephemeral

    configured = get_settings().fernet_key
    if configured:
        _key_is_ephemeral = False
        return configured

    _key_is_ephemeral = True

    if KEY_PATH.exists():
        key = KEY_PATH.read_text(encoding="ascii").strip()
        if key:
            return key

    key = generate_key()
    KEY_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    KEY_PATH.write_text(key, encoding="ascii")
    os.chmod(KEY_PATH, 0o600)
    logger.warning(
        "FAE_FERNET_KEY was not set. Generated a development key at %s. "
        "Set FAE_FERNET_KEY in production; losing this file makes every "
        "stored credential undecryptable.",
        KEY_PATH.resolve(),
    )
    return key


@lru_cache
def get_fernet() -> Fernet:
    """Return the process-wide Fernet instance, creating a key if needed."""
    return Fernet(_load_or_create_key().encode("ascii"))


def encrypt(plaintext: str) -> str:
    """Encrypt a credential. Output is safe to persist."""
    return get_fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    """Decrypt a stored credential. Raises ``InvalidToken`` if tampered with."""
    return get_fernet().decrypt(token.encode("ascii")).decode("utf-8")
