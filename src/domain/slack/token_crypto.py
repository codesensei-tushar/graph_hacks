"""Credential encryption at rest: Slack bot tokens and connector secrets.

MultiFernet from the `cryptography` package. ``TOKEN_ENCRYPTION_KEYS`` is a comma-separated
list: the first key encrypts, every key decrypts, so a key is rotated by putting a new one
first, re-wrapping (``rewrap_token``; ``POST /ops/tokens/rewrap``) and then dropping the old
one. ``TOKEN_ENCRYPTION_KEY``, the single key used before, is still read and decrypts as the
last key (it encrypts only when it is the only key).

Stored values describe themselves: ``fernet:<token>``, or ``plain:<token>`` for a Slack bot
token written while no key was set. Slack tokens may still be stored that way (a warning is
logged once) so production keeps working before a key is configured; connector credentials
pass ``allow_plaintext=False`` and are refused instead. ``rewrap_token`` turns either into ``fernet:`` under
the first key once one is set.
"""

import hashlib
import logging

from src.core.config import settings

log = logging.getLogger(__name__)

_FERNET_PREFIX = "fernet:"
_PLAIN_PREFIX = "plain:"
_warned = False


def _keys() -> list[str]:
    keys = list(settings.token_encryption_keys)
    if settings.token_encryption_key and settings.token_encryption_key not in keys:
        keys.append(settings.token_encryption_key)
    return keys


def _fernet():
    keys = _keys()
    if not keys:
        return None
    try:
        from cryptography.fernet import Fernet, MultiFernet
    except ImportError:
        return None
    return MultiFernet([Fernet(key.encode()) for key in keys])


def encryption_available() -> bool:
    return _fernet() is not None


def current_key_id() -> str | None:
    """A short, non-secret id of the key that encrypts now (stored next to connector secrets)."""
    keys = _keys()
    return hashlib.sha256(keys[0].encode()).hexdigest()[:12] if keys else None


def encrypt_token(token: str, allow_plaintext: bool = True) -> str:
    """``allow_plaintext=False`` (connector credentials) raises instead of storing the token unencrypted."""
    global _warned
    fernet = _fernet()
    if fernet is None:
        if not allow_plaintext:
            raise RuntimeError("refusing to store a credential unencrypted: install `cryptography` and set TOKEN_ENCRYPTION_KEYS")
        if not _warned:
            log.warning("[slack] storing bot tokens unencrypted: install `cryptography` and set TOKEN_ENCRYPTION_KEYS")
            _warned = True
        return _PLAIN_PREFIX + token
    return _FERNET_PREFIX + fernet.encrypt(token.encode()).decode()


def is_encrypted(value: str | None) -> bool:
    return bool(value) and value.startswith(_FERNET_PREFIX)


def decrypt_token(value: str, allow_plaintext: bool = True) -> str:
    if value.startswith(_FERNET_PREFIX):
        fernet = _fernet()
        if fernet is None:
            raise RuntimeError("the credential is encrypted but no key (TOKEN_ENCRYPTION_KEYS) or `cryptography` is available")
        return fernet.decrypt(value[len(_FERNET_PREFIX) :].encode()).decode()
    if value.startswith(_PLAIN_PREFIX):
        if not allow_plaintext:
            raise ValueError("refusing a credential that was stored unencrypted")
        return value[len(_PLAIN_PREFIX) :]
    raise ValueError("unrecognized stored token format")


def rewrap_token(value: str, allow_plaintext: bool = True) -> str | None:
    """``value`` encrypted under the current first key, or None when it already is (or no key is set).
    A plaintext legacy value is encrypted; an encrypted one is rotated to the first key."""
    fernet = _fernet()
    if fernet is None or not value:
        return None
    if value.startswith(_FERNET_PREFIX):
        token = value[len(_FERNET_PREFIX) :].encode()
        rotated = fernet.rotate(token).decode()
        # Fernet tokens carry a fresh IV and timestamp, so compare by key: rotate only when the first key could not read it.
        from cryptography.fernet import Fernet, InvalidToken

        try:
            Fernet(_keys()[0].encode()).decrypt(token)
            return None
        except InvalidToken:
            return _FERNET_PREFIX + rotated
    return encrypt_token(decrypt_token(value, allow_plaintext=allow_plaintext), allow_plaintext=False)
