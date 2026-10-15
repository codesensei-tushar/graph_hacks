"""Bot token encryption at rest. Port of company-brain `@/lib/crypto` (encryptToken / decryptToken).

company-brain encrypts with PBKDF2 + AES-GCM. Here: Fernet from the `cryptography`
package keyed by ``TOKEN_ENCRYPTION_KEY``. When the package or the key is missing
(PyPI was unreachable when this was written), tokens are stored as plaintext with a
``plain:`` prefix and a warning is logged once. Stored values are self-describing,
so adding the key later only affects new writes; reinstall to re-encrypt.
"""

import logging

from src.core.config import settings

log = logging.getLogger(__name__)

_FERNET_PREFIX = "fernet:"
_PLAIN_PREFIX = "plain:"
_warned = False


def _fernet():
    if not settings.token_encryption_key:
        return None
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        return None
    return Fernet(settings.token_encryption_key.encode())


def encryption_available() -> bool:
    return _fernet() is not None


def encrypt_token(token: str) -> str:
    global _warned
    fernet = _fernet()
    if fernet is None:
        if not _warned:
            log.warning(
                "[slack] storing bot tokens unencrypted: install `cryptography` and set TOKEN_ENCRYPTION_KEY"
            )
            _warned = True
        return _PLAIN_PREFIX + token
    return _FERNET_PREFIX + fernet.encrypt(token.encode()).decode()


def decrypt_token(value: str) -> str:
    if value.startswith(_PLAIN_PREFIX):
        return value[len(_PLAIN_PREFIX) :]
    if value.startswith(_FERNET_PREFIX):
        fernet = _fernet()
        if fernet is None:
            raise RuntimeError("bot token is encrypted but TOKEN_ENCRYPTION_KEY/cryptography is unavailable")
        return fernet.decrypt(value[len(_FERNET_PREFIX) :].encode()).decode()
    raise ValueError("unrecognized stored token format")
