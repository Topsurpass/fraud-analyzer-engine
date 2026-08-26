"""Password hashing and the rules for choosing one."""

from __future__ import annotations

import pytest

from app.errors import AppError, ErrorCode
from app.security.passwords import (
    MIN_PASSWORD_LENGTH,
    generate_temporary_password,
    hash_password,
    validate_password_strength,
    verify_password,
)


def test_a_password_verifies_against_its_own_hash():
    hashed = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", hashed) is True


def test_a_wrong_password_does_not_verify():
    hashed = hash_password("correct horse battery staple")
    assert verify_password("Correct horse battery staple", hashed) is False


def test_the_hash_does_not_contain_the_password():
    """The obvious property, asserted because it is the whole point."""
    hashed = hash_password("correct horse battery staple")
    assert "correct" not in hashed


def test_the_same_password_hashes_differently_every_time():
    """Argon2 salts per call, so two users with one password are not
    identifiable as such from the database."""
    assert hash_password("correct horse battery staple") != hash_password(
        "correct horse battery staple"
    )


def test_verify_returns_false_for_a_hash_it_cannot_parse():
    """A corrupt or truncated hash column must read as 'wrong password', not
    raise - otherwise a damaged row turns a login into a 500 that leaks the
    shape of the problem."""
    assert verify_password("anything", "not-a-hash") is False


def test_verify_returns_false_for_a_missing_hash():
    """A user row created before a password was ever set has a null/empty
    hash column. argon2-cffi raises AttributeError on a falsy hash rather
    than one of its own exceptions, so this must be guarded explicitly or
    the same corrupt-row problem above shows up as an unhandled 500 instead
    of a normal failed login."""
    assert verify_password("anything", "") is False
    assert verify_password("anything", None) is False


def test_a_short_password_is_refused():
    with pytest.raises(AppError) as caught:
        validate_password_strength("a" * (MIN_PASSWORD_LENGTH - 1))
    assert caught.value.error_code is ErrorCode.WEAK_PASSWORD


def test_a_long_enough_password_is_accepted():
    validate_password_strength("a-perfectly-fine-password")


def test_a_common_password_is_refused_however_long():
    """Length alone is not strength: 'password123456' clears twelve characters
    and is in every wordlist an attacker owns."""
    with pytest.raises(AppError) as caught:
        validate_password_strength("password123456")
    assert caught.value.error_code is ErrorCode.WEAK_PASSWORD


def test_the_refusal_says_what_is_wrong():
    """A rejection with no reason makes a user try the same class of password
    again."""
    with pytest.raises(AppError) as caught:
        validate_password_strength("short")
    assert str(MIN_PASSWORD_LENGTH) in caught.value.message


def test_a_generated_temporary_password_passes_the_rules():
    """It is handed out to a real person, so it has to satisfy the same policy
    they will be held to."""
    validate_password_strength(generate_temporary_password())


def test_generated_temporary_passwords_are_not_repeated():
    assert len({generate_temporary_password() for _ in range(50)}) == 50
