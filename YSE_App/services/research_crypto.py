"""Public-key encryption for the encrypted researcher-profile fields (#627).

The server holds only the research **public** key. Values are encrypted with a
hybrid scheme: a fresh AES-256-GCM key per value, wrapped with RSA-OAEP-SHA256.
Nothing on the server can decrypt them; the private key stays with the
research data custodian, who decrypts exported ciphertexts offline (see
``decrypt_value`` and docs/researcher-profile.md).

Key configuration, first match wins:

1. env ``YSE_RESEARCH_PUBLIC_KEY``: the PEM text itself, or a path to a PEM file;
2. ``[research] public_key_path`` in ``YSE_PZ/settings.ini``.

No key, or a key that does not load, means "not configured": the encrypted
fields are not collected and their requirements are waived. That never blocks
site access.

Ciphertext format (text, safe for a TEXT column)::

    "v1:" + base64( u16 len(wrapped_key) | wrapped_key | 12-byte nonce | AES-GCM ciphertext+tag )

The AES-GCM associated data is ``b"yse-research:" + field_name``, so a value
cannot be moved to another field without failing decryption.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import struct
from typing import Optional, Tuple

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings

logger = logging.getLogger(__name__)

FORMAT_PREFIX = "v1:"
MIN_KEY_BITS = 2048
DEFAULT_KEY_BITS = 3072

_cache = {"source": None, "key": None, "fingerprint": None}
_warned = set()


class ResearchKeyError(Exception):
    """The research public key is missing or unusable."""


def _oaep():
    return padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None)


def _aad(field_name: str) -> bytes:
    return b"yse-research:" + field_name.encode("utf-8")


def _warn_once(message: str, *args) -> None:
    text = message % args if args else message
    if text not in _warned:
        _warned.add(text)
        logger.warning(text)


def _configured_source() -> Tuple[str, str]:
    """Return ``(kind, value)``: ("pem", text), ("path", path) or ("", "")."""
    env_value = (getattr(settings, "RESEARCH_PUBLIC_KEY", "") or "").strip()
    if env_value:
        if env_value.startswith("-----BEGIN"):
            return "pem", env_value
        return "path", env_value
    path = (getattr(settings, "RESEARCH_PUBLIC_KEY_PATH", "") or "").strip()
    if path and not (path.startswith("<") and path.endswith(">")):
        return "path", path
    return "", ""


def fingerprint_of(public_key) -> str:
    """SHA-256 (hex) of the key's DER SubjectPublicKeyInfo."""
    der = public_key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return hashlib.sha256(der).hexdigest()


def load_public_key_pem(data: bytes):
    key = serialization.load_pem_public_key(data)
    if not isinstance(key, rsa.RSAPublicKey):
        raise ResearchKeyError("the research public key must be an RSA key")
    if key.key_size < MIN_KEY_BITS:
        raise ResearchKeyError("the research public key must be at least %d bits" % MIN_KEY_BITS)
    return key


def get_public_key():
    """The configured research public key, or ``None`` (logged once) when unusable."""
    kind, value = _configured_source()
    if not kind:
        _warn_once(
            "No research public key is configured ([research] public_key_path or "
            "YSE_RESEARCH_PUBLIC_KEY): encrypted profile fields are not collected."
        )
        return None
    source = (kind, value)
    if kind == "path":
        try:
            source = (kind, value, os.path.getmtime(value))
        except OSError:
            _warn_once("Research public key file %s is not readable: encrypted profile fields are disabled.", value)
            return None
    if _cache["source"] == source:
        return _cache["key"]
    try:
        if kind == "pem":
            data = value.encode("ascii")
        else:
            with open(value, "rb") as fh:
                data = fh.read()
        key = load_public_key_pem(data)
    except Exception as exc:  # noqa: BLE001 - any failure means "not configured"
        _warn_once("Research public key could not be loaded (%s): encrypted profile fields are disabled.",
                   exc.__class__.__name__)
        _cache.update(source=source, key=None, fingerprint=None)
        return None
    _cache.update(source=source, key=key, fingerprint=fingerprint_of(key))
    return key


def key_available() -> bool:
    return get_public_key() is not None


def current_fingerprint() -> str:
    """Fingerprint of the configured key, or ``""``."""
    return _cache["fingerprint"] if get_public_key() is not None else ""


def encrypt_value(plaintext: str, field_name: str, public_key=None) -> Tuple[str, str]:
    """Encrypt ``plaintext`` for ``field_name``; return ``(ciphertext_text, key_fingerprint)``."""
    key = public_key if public_key is not None else get_public_key()
    if key is None:
        raise ResearchKeyError("no research public key is configured")
    data_key = AESGCM.generate_key(bit_length=256)
    nonce = os.urandom(12)
    sealed = AESGCM(data_key).encrypt(nonce, plaintext.encode("utf-8"), _aad(field_name))
    wrapped = key.encrypt(data_key, _oaep())
    blob = struct.pack(">H", len(wrapped)) + wrapped + nonce + sealed
    return FORMAT_PREFIX + base64.b64encode(blob).decode("ascii"), fingerprint_of(key)


def decrypt_value(ciphertext: str, field_name: str, private_key) -> str:
    """Offline use only: decrypt with the custodian's private key (never on the server)."""
    if not ciphertext.startswith(FORMAT_PREFIX):
        raise ValueError("unknown ciphertext format")
    blob = base64.b64decode(ciphertext[len(FORMAT_PREFIX):])
    (n,) = struct.unpack(">H", blob[:2])
    wrapped, rest = blob[2:2 + n], blob[2 + n:]
    nonce, sealed = rest[:12], rest[12:]
    data_key = private_key.decrypt(wrapped, _oaep())
    return AESGCM(data_key).decrypt(nonce, sealed, _aad(field_name)).decode("utf-8")


def generate_keypair(bits: int = DEFAULT_KEY_BITS, passphrase: Optional[bytes] = None) -> Tuple[bytes, bytes, str]:
    """Return ``(private_pem, public_pem, fingerprint)`` for a new RSA key pair."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=bits)
    encryption = (serialization.BestAvailableEncryption(passphrase) if passphrase
                  else serialization.NoEncryption())
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=encryption,
    )
    public_key = private_key.public_key()
    public_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem, fingerprint_of(public_key)


def reset_cache() -> None:
    """Forget the loaded key (tests, settings changes)."""
    _cache.update(source=None, key=None, fingerprint=None)
    _warned.clear()
