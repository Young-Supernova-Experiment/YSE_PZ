"""Encryption layer for :class:`YSE_App.models.EncryptedCredential` (#264).

Payloads are JSON objects encrypted with Fernet (AES-128-CBC + HMAC-SHA256,
from the ``cryptography`` package already pinned in ``requirements.txt``).
The key comes from ``settings.CREDENTIALS_KEY`` (``YSE_CREDENTIALS_KEY`` env
var or ``[secrets] credentials_key`` in ``settings.ini``); it may be a
comma-separated list, in which case the first key encrypts and every key
decrypts, so a rotation can be staged without downtime.

The design follows SkyPortal's encrypted allocation ``altdata`` (BSD-3-Clause)
in spirit only: nothing here is copied from that project.
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Dict, Iterable, List, Optional

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

MASK = "••••••••"  # 8 bullets, what masked views show


class CredentialError(Exception):
    """Base class for credential storage errors."""


class CredentialKeyMissing(CredentialError, ImproperlyConfigured):
    """No usable CREDENTIALS_KEY is configured."""


class CredentialKeyInvalid(CredentialError, ImproperlyConfigured):
    """CREDENTIALS_KEY is not a valid Fernet key."""


class CredentialDecryptError(CredentialError):
    """The stored payload cannot be decrypted with the configured key(s)."""


def generate_key() -> str:
    """Return a fresh URL-safe base64 Fernet key as text."""
    return Fernet.generate_key().decode()


def split_keys(raw: Optional[str]) -> List[str]:
    """Split a comma/whitespace separated key list, dropping blanks."""
    if not raw:
        return []
    parts = raw.replace("\n", ",").split(",")
    return [p.strip() for p in parts if p.strip()]


def key_fingerprint(key: str) -> str:
    """Short, non-reversible identifier of a key (safe to store and log)."""
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def _fernet_for(key: str) -> Fernet:
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except (ValueError, TypeError) as exc:
        raise CredentialKeyInvalid(
            "CREDENTIALS_KEY is not a valid Fernet key (32 URL-safe base64 bytes). "
            "Generate one with `python manage.py generate_credentials_key`."
        ) from exc


def configured_keys() -> List[str]:
    """Keys from settings, primary first; raises when none is configured."""
    keys = split_keys(getattr(settings, "CREDENTIALS_KEY", ""))
    if not keys:
        raise CredentialKeyMissing(
            "CREDENTIALS_KEY is not configured. Set the YSE_CREDENTIALS_KEY environment "
            "variable or credentials_key under [secrets] in YSE_PZ/settings.ini."
        )
    return keys


def _multi(keys: Iterable[str]) -> MultiFernet:
    fernets = [_fernet_for(k) for k in keys]
    if not fernets:
        raise CredentialKeyMissing("No encryption key supplied.")
    return MultiFernet(fernets)


def encrypt_payload(payload: Dict, keys: Optional[Iterable[str]] = None) -> str:
    """Serialise ``payload`` (a JSON object) and encrypt it with the primary key."""
    if not isinstance(payload, dict):
        raise TypeError("credential payload must be a dict (JSON object)")
    keys = list(keys) if keys is not None else configured_keys()
    data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return _multi(keys).encrypt(data).decode()


def decrypt_payload(token: str, keys: Optional[Iterable[str]] = None) -> Dict:
    """Decrypt a token produced by :func:`encrypt_payload` with any configured key."""
    if not token:
        return {}
    keys = list(keys) if keys is not None else configured_keys()
    try:
        raw = _multi(keys).decrypt(token.encode() if isinstance(token, str) else token)
    except InvalidToken as exc:
        raise CredentialDecryptError(
            "Stored credential cannot be decrypted with the configured CREDENTIALS_KEY; "
            "the key changed without `manage.py rotate_credentials_key`, or the row is corrupt."
        ) from exc
    try:
        payload = json.loads(raw.decode())
    except ValueError as exc:
        raise CredentialDecryptError("Decrypted credential is not valid JSON.") from exc
    return payload if isinstance(payload, dict) else {"value": payload}


def reencrypt(token: str, old_keys: Iterable[str], new_key: str) -> str:
    """Decrypt with ``old_keys`` and encrypt with ``new_key`` (key rotation)."""
    return encrypt_payload(decrypt_payload(token, old_keys), [new_key])


def mask_payload(payload: Dict) -> Dict:
    """Same keys as ``payload``, every value replaced by :data:`MASK`."""
    return {str(k): MASK for k in payload}


def primary_key_fingerprint() -> str:
    return key_fingerprint(configured_keys()[0])


def derive_debug_key(secret_key: str) -> str:
    """The key settings.py uses when DEBUG is on and none is configured."""
    return base64.urlsafe_b64encode(
        hashlib.sha256(("yse-credentials:" + secret_key).encode()).digest()
    ).decode()
