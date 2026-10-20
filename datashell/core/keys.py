"""Ed25519 key-material loading, derivation, and generation.

Deliberately dependency-free (stdlib + ``cryptography`` only, no ``settings``
import) so both configuration and the crypto engine can use it without creating
an import cycle.

Key material may be supplied as:
  * a PEM document (``-----BEGIN ...``), or
  * base64 or hex of a DER encoding, or
  * base64 or hex of the raw 32-byte seed (private) / 32-byte public key.
"""

from __future__ import annotations

import base64
import binascii

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


def _decode_raw(material: str) -> bytes:
    s = material.strip()
    try:
        return base64.b64decode(s, validate=True)
    except (binascii.Error, ValueError):
        pass
    try:
        return bytes.fromhex(s)
    except ValueError as err:
        raise ValueError("key material is neither valid base64 nor hex") from err


def load_ed25519_private_key(material: str) -> Ed25519PrivateKey:
    if not material or not material.strip():
        raise ValueError("empty Ed25519 private key material")
    s = material.strip()
    if s.startswith("-----BEGIN"):
        key = serialization.load_pem_private_key(s.encode("utf-8"), password=None)
    else:
        raw = _decode_raw(s)
        try:
            key = serialization.load_der_private_key(raw, password=None)
        except Exception:
            if len(raw) == 32:
                key = Ed25519PrivateKey.from_private_bytes(raw)
            else:
                raise ValueError("Unsupported Ed25519 private key encoding") from None
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("Provided private key is not an Ed25519 key")
    return key


def load_ed25519_public_key(material: str) -> Ed25519PublicKey:
    if not material or not material.strip():
        raise ValueError("empty Ed25519 public key material")
    s = material.strip()
    if s.startswith("-----BEGIN"):
        key = serialization.load_pem_public_key(s.encode("utf-8"))
    else:
        raw = _decode_raw(s)
        try:
            key = serialization.load_der_public_key(raw)
        except Exception:
            if len(raw) == 32:
                key = Ed25519PublicKey.from_public_bytes(raw)
            else:
                raise ValueError("Unsupported Ed25519 public key encoding") from None
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("Provided public key is not an Ed25519 key")
    return key


def public_key_pem(public_key: Ed25519PublicKey) -> str:
    return public_key.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode("utf-8")


def private_key_pem(private_key: Ed25519PrivateKey) -> str:
    return private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("utf-8")


def derive_public_key_material(private_key_material: str) -> str:
    """Return the PEM-encoded public key for a given private key material."""
    return public_key_pem(load_ed25519_private_key(private_key_material).public_key())


def generate_keypair_pem() -> tuple[str, str]:
    """Generate a fresh Ed25519 keypair as ``(private_pem, public_pem)``."""
    private_key = Ed25519PrivateKey.generate()
    return private_key_pem(private_key), public_key_pem(private_key.public_key())
