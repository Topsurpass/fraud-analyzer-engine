"""Hashing passwords, and the rules for choosing one.

Argon2id rather than bcrypt: it is the current password-hashing competition
winner, it resists GPU attack by being memory-hard, and ``argon2-cffi`` picks
sane parameters without this module inventing any.

Nothing here reads or writes the database. Keeping it pure is what makes the
policy testable without a fixture, and it keeps the one security-critical
primitive in a file small enough to read in full.
"""

from __future__ import annotations

import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHash, VerificationError, VerifyMismatchError

from app.errors import AppError, ErrorCode

#: Long enough that a stolen hash is not worth a dictionary run. NIST no longer
#: recommends composition rules (a symbol, a digit, a capital) because they
#: push people toward "Password1!" - length and a blocklist do more.
MIN_PASSWORD_LENGTH = 12

#: The passwords an attacker tries first. Deliberately tiny and inlined: a full
#: wordlist is a data-file dependency for a service with a handful of accounts,
#: and these cover the ones a person actually picks under protest.
_COMMON = frozenset(
    {
        "password",
        "password1",
        "password123",
        "password1234",
        "password12345",
        "password123456",
        "passw0rd",
        "qwertyuiop",
        "1234567890",
        "123456789012",
        "letmein",
        "welcome",
        "welcome123",
        "administrator",
        "changeme",
        "changeme123",
        "switchboard",
        "switchboard1",
    }
)

_hasher = PasswordHasher()

#: Unambiguous alphabet: no O/0, l/1/I. A temporary password is read aloud,
#: typed from a screenshot, or copied out of a chat message, and a character
#: nobody can identify turns into a support conversation.
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
_TEMP_LENGTH = 16


def hash_password(plaintext: str) -> str:
    """Argon2id hash, salt and parameters embedded in the returned string."""
    return _hasher.hash(plaintext)


def verify_password(plaintext: str, hashed: str) -> bool:
    """Whether the password matches. Never raises.

    A malformed hash reads as "wrong password" rather than an exception: a
    corrupt or truncated column would otherwise turn a login attempt into a 500,
    which both breaks the account and tells an attacker something is unusual
    about it.
    """
    try:
        return _hasher.verify(hashed, plaintext)
    except (VerifyMismatchError, VerificationError, InvalidHash):
        return False


def validate_password_strength(plaintext: str) -> None:
    """Raise ``AppError`` if the password is not allowed. Silent if it is."""
    if len(plaintext) < MIN_PASSWORD_LENGTH:
        raise AppError(
            ErrorCode.WEAK_PASSWORD,
            f"A password must be at least {MIN_PASSWORD_LENGTH} characters.",
        )
    if plaintext.strip().lower() in _COMMON:
        raise AppError(
            ErrorCode.WEAK_PASSWORD,
            "That password is one of the first an attacker tries. Choose another.",
        )


def generate_temporary_password() -> str:
    """A random password for an account whose owner will replace it.

    ``secrets``, never ``random``: the latter is seeded predictably and is not
    fit for anything that guards an account.
    """
    return "".join(secrets.choice(_ALPHABET) for _ in range(_TEMP_LENGTH))
