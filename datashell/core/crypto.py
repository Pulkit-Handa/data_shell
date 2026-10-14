"""Cryptographic integrity verification and tamper-evident audit manifests."""

from __future__ import annotations
import hmac
import hashlib
import json
from pathlib import Path
from typing import Dict, Any, Union, Optional, List
from datashell.core.config import settings


class CryptoEngine:
    """
    Computes cryptographically verifiable receipts and Merkle manifests for every run.
    Guarantees tamper-evident audit trails with HMAC-SHA256 cryptographic binding.
    """

    @staticmethod
    def sha256_file(file_path: Union[str, Path]) -> str:
        """
        Computes SHA-256 hash of a file streaming in 64KB blocks.
        Raises FileNotFoundError if the file does not exist on disk.
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"Expected cryptographic artifact '{path}' was not found on disk")
        hasher = hashlib.sha256()
        with open(path, "rb") as f:
            while chunk := f.read(65536):
                hasher.update(chunk)
        return hasher.hexdigest()

    @staticmethod
    def sha256_bytes(data: bytes) -> str:
        """Computes SHA-256 hash of in-memory bytes."""
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def canonical_json_hash(data: Any) -> str:
        """Computes deterministic SHA-256 digest over canonical JSON serialization."""
        canonical_str = json.dumps(data, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical_str.encode("utf-8")).hexdigest()

    @staticmethod
    def compute_merkle_root(hashes: List[str]) -> str:
        """Computes a balanced Merkle Root over an ordered list of SHA-256 digests."""
        if not hashes:
            return "0" * 64
        current_level = hashes[:]
        while len(current_level) > 1:
            next_level = []
            for i in range(0, len(current_level), 2):
                h1 = current_level[i]
                h2 = current_level[i + 1] if i + 1 < len(current_level) else h1
                combined = hashlib.sha256((h1 + h2).encode("utf-8")).hexdigest()
                next_level.append(combined)
            current_level = next_level
        return current_level[0]

    @staticmethod
    def sign_manifest(manifest_payload: Dict[str, Any], secret_key: Optional[str] = None) -> str:
        """Generates an HMAC-SHA256 signature over a canonical JSON manifest."""
        key = (secret_key or settings.hmac_secret).encode("utf-8")
        canonical_str = json.dumps(manifest_payload, sort_keys=True, separators=(",", ":"))
        return hmac.new(key, canonical_str.encode("utf-8"), hashlib.sha256).hexdigest()

    @staticmethod
    def verify_signature(manifest_payload: Dict[str, Any], signature: str, secret_key: Optional[str] = None) -> bool:
        """Verifies if an HMAC-SHA256 signature matches the payload."""
        expected = CryptoEngine.sign_manifest(manifest_payload, secret_key)
        return hmac.compare_digest(expected, signature)

    @staticmethod
    def resolve_verification_secret(key_id: str, keyring: Optional[Dict[str, str]]) -> Optional[str]:
        """Resolve a signing secret only when that exact key ID is provisioned.

        Never fall back to the active key for an unknown/retired key ID: doing so
        silently changes key identity semantics and can make an unprovisioned ID
        appear trusted. The caller must reject a missing result.
        """
        if not isinstance(key_id, str) or not key_id or not isinstance(keyring, dict):
            return None
        secret = keyring.get(key_id)
        return secret if isinstance(secret, str) and secret else None
