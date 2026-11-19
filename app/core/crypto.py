from __future__ import annotations

import base64
import hashlib
import os
from functools import lru_cache
from pathlib import Path

from app.core.config import settings
from cryptography.fernet import Fernet, InvalidToken


class SecretError(RuntimeError):
    """Raised when a stored secret cannot be decrypted safely."""


def _key_path() -> Path:
    return Path(settings.data_dir) / "secrets.key"


def _load_or_create_key() -> bytes:
    """Return a Fernet key.

    Production deployments should provide ``JARVIS_SECRETS_KEY``. For a local
    single-user desktop installation, a random key is generated once and kept
    in the per-user backend data directory with owner-only permissions. The key
    never leaves the machine and is never exposed over the API.
    """
    if settings.secrets_key:
        digest = hashlib.sha256(settings.secrets_key.encode("utf-8")).digest()
        return base64.urlsafe_b64encode(digest)

    path = _key_path()
    try:
        existing = path.read_bytes().strip()
        if existing:
            return existing
    except FileNotFoundError:
        pass

    key = Fernet.generate_key()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Create atomically with restrictive permissions where the platform supports it.
    temp = path.with_suffix(".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, key)
    finally:
        os.close(fd)
    os.replace(temp, path)
    return key


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    return Fernet(_load_or_create_key())


def encrypt_text(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_text(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise SecretError(
            "A stored credential could not be decrypted. Reconnect the integration."
        ) from exc
