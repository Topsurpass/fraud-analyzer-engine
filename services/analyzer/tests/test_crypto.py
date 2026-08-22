import pytest
from cryptography.fernet import InvalidToken

from app.config import get_settings
from app.security import crypto
from app.security.crypto import decrypt, encrypt, generate_key, get_fernet


@pytest.fixture(autouse=True)
def _fixed_key(monkeypatch):
    """Give every test a known key so ordering cannot leak state."""
    monkeypatch.setenv("FAE_FERNET_KEY", generate_key())
    get_settings.cache_clear()
    get_fernet.cache_clear()
    yield
    get_settings.cache_clear()
    get_fernet.cache_clear()


def test_round_trip():
    assert decrypt(encrypt("hunter2")) == "hunter2"


def test_round_trip_unicode():
    assert decrypt(encrypt("pässwörd-ção")) == "pässwörd-ção"


def test_ciphertext_is_not_plaintext():
    assert "hunter2" not in encrypt("hunter2")


def test_two_encryptions_of_same_value_differ():
    # Fernet uses a random IV, so identical plaintexts must not collide.
    assert encrypt("x") != encrypt("x")


def test_tampered_token_raises():
    token = encrypt("x")
    tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    with pytest.raises(InvalidToken):
        decrypt(tampered)


def test_missing_key_generates_and_persists(tmp_path, monkeypatch):
    monkeypatch.delenv("FAE_FERNET_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    get_fernet.cache_clear()

    assert decrypt(encrypt("a")) == "a"

    key_file = tmp_path / ".secrets" / "fernet.key"
    assert key_file.exists()
    assert (key_file.stat().st_mode & 0o777) == 0o600


def test_existing_key_file_is_reused(tmp_path, monkeypatch):
    monkeypatch.delenv("FAE_FERNET_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    secrets_dir = tmp_path / ".secrets"
    secrets_dir.mkdir()
    known = generate_key()
    (secrets_dir / "fernet.key").write_text(known)

    get_settings.cache_clear()
    get_fernet.cache_clear()
    token = encrypt("v")

    # A separate Fernet built from the same file must decrypt it.
    from cryptography.fernet import Fernet

    assert Fernet(known.encode()).decrypt(token.encode()).decode() == "v"


def test_key_path_is_gitignored_by_convention():
    # Guards against someone moving the key outside the ignored directory.
    assert crypto.KEY_PATH.parts[0] == ".secrets"
