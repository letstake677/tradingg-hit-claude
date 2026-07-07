"""
Encrypts/decrypts Bitget credentials before they touch the database.

Why this exists: the dashboard lets you "attach" API keys through a form
instead of only editing .env by hand. That means credentials now flow
through the API and land in the database — so they're encrypted at rest
with a key that itself only ever lives in .env, never in the db. Stealing
the db file alone isn't enough to read them back out.

Generate a key ONCE per deployment:
    python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
Put the result in .env as CREDENTIAL_ENCRYPTION_KEY. Losing/rotating this
key makes any previously-saved credentials unreadable — you'd re-attach
them through the dashboard again.
"""

import os
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken


class CredentialCryptoError(Exception):
    pass


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    key = os.getenv("CREDENTIAL_ENCRYPTION_KEY")
    if not key:
        raise CredentialCryptoError(
            "CREDENTIAL_ENCRYPTION_KEY is not set in .env. Generate one with:\n"
            "  python3 -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\"\n"
            "and put it in .env before saving credentials through the dashboard."
        )
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except (ValueError, TypeError) as e:
        raise CredentialCryptoError(f"CREDENTIAL_ENCRYPTION_KEY in .env is malformed: {e}")


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        raise CredentialCryptoError(
            "Could not decrypt stored credentials — CREDENTIAL_ENCRYPTION_KEY "
            "in .env doesn't match the key they were saved with."
        )


def mask(value: str, keep: int = 4) -> str:
    """For display only — never send the real secret back to the dashboard."""
    if not value:
        return ""
    if len(value) <= keep:
        return "*" * len(value)
    return "*" * (len(value) - keep) + value[-keep:]
