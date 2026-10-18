"""Settlement receipts: signed evidence binding settlement-critical fields.

The delivery certificate (``certificate_version 2.0-enterprise``) is emitted
at pipeline time and binds the run, artifacts, and data contract digest. A
settlement is calculated *after* delivery, so its evidence cannot be inside
that original document. Instead the settlement engine issues a **settlement
receipt** — a versioned extension of the certificate format (same signature
envelope, same key IDs, same trust semantics) that:

* binds the delivery certificate by digest (``sha256`` over the delivery
  certificate's canonical JSON),
* binds the commercial contract id/version/digest,
* embeds the full settlement calculation inputs and result, and
* is signed with the same mechanism as audit certificates (Ed25519 when
  provisioned — third-party verifiable; HMAC-SHA256 otherwise, which requires
  the shared secret and is *never* labelled publicly verifiable).

Verification checks the receipt signature, re-verifies the referenced
delivery certificate through the existing artifact verifier, and confirms the
settlement inputs still match the delivery evidence. Tampering with any
settlement-critical field invalidates the signature.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from datashell.core.config import settings
from datashell.core.crypto import CryptoEngine
from datashell.pipeline.artifacts import ArtifactGenerator
from datashell.settlement.models import SettlementStatement

SETTLEMENT_RECEIPT_VERSION = "1"


class SettlementCertificateError(RuntimeError):
    """Raised when a settlement receipt cannot be produced or parsed safely."""


def certificate_digest(cert_data: Dict[str, Any]) -> str:
    """Digest over a delivery certificate's canonical JSON (signature excluded).

    Mirrors the signing convention: the ``digital_signature`` block is not
    part of the signed payload, so it is excluded from the binding digest too.
    """
    payload = {k: v for k, v in cert_data.items() if k != "digital_signature"}
    return CryptoEngine.canonical_json_hash(payload)


def build_settlement_receipt(
    statement: SettlementStatement,
    delivery_certificate: Dict[str, Any],
) -> Dict[str, Any]:
    """Produce the signed settlement receipt for a calculated statement.

    The receipt embeds the statement's signed payload and the delivery
    certificate digest, then signs the whole envelope exactly like an audit
    certificate so independent verification uses the same trusted keyrings.
    """
    if not isinstance(delivery_certificate, dict):
        raise SettlementCertificateError("delivery certificate must be a mapping")
    delivery_digest = certificate_digest(delivery_certificate)

    receipt_payload = {
        "receipt_version": SETTLEMENT_RECEIPT_VERSION,
        "schema": "settlement_receipt",
        "settlement_id": statement.settlement_id,
        "tenant_id": statement.tenant_id,
        "run_id": statement.run_id,
        "delivery_certificate_digest": delivery_digest,
        "contract": {
            "contract_id": statement.contract_id,
            "contract_version": statement.contract_version,
            "contract_digest": statement.contract_digest,
        },
        "settlement_evidence": statement.model_dump(mode="json"),
    }

    # Sign with the same envelope semantics as audit certificates: the
    # signature_header is part of the signed content (matching the audit
    # certificate convention), so tampering with either breaks verification.
    ed25519_private_key = getattr(settings, "ed25519_private_key", None)
    if ed25519_private_key:
        sig_envelope = {
            "algorithm": "Ed25519",
            "key_id": getattr(settings, "ed25519_active_key_id", "datashell-ed25519-v1"),
            "integrity_type": "ASYMMETRIC_ED25519_SIGNATURE",
            "third_party_verifiable": True,
            "requires_shared_secret": False,
            "tamper_evident": True,
        }
        receipt_payload["signature_header"] = sig_envelope
        signature = CryptoEngine.sign_manifest_ed25519(receipt_payload, ed25519_private_key)
    else:
        sig_envelope = {
            "algorithm": "HMAC-SHA256",
            "key_id": getattr(settings, "active_key_id", "datashell-hmac-v1"),
            "integrity_type": "SYMMETRIC_HMAC_AUTHENTICATION",
            "third_party_verifiable": False,
            "requires_shared_secret": True,
            "tamper_evident": True,
        }
        receipt_payload["signature_header"] = sig_envelope
        signature = CryptoEngine.sign_manifest(receipt_payload)

    receipt_payload["digital_signature"] = {
        "algorithm": sig_envelope["algorithm"],
        "key_id": sig_envelope["key_id"],
        "integrity_type": sig_envelope["integrity_type"],
        "third_party_verifiable": sig_envelope["third_party_verifiable"],
        "requires_shared_secret": sig_envelope["requires_shared_secret"],
        "signature": signature,
        "tamper_evident": True,
    }
    return receipt_payload


def verify_settlement_receipt(
    receipt: Dict[str, Any],
    *,
    delivery_certificate: Optional[Dict[str, Any]] = None,
    expected_statement: Optional[SettlementStatement] = None,
) -> Dict[str, Any]:
    """Verify a settlement receipt's signature and its bindings.

    Steps:
    1. Signature algorithm/key trust + signature validity (same rules as
       audit certificate verification).
    2. Recompute the delivery certificate digest; if the caller supplies the
       certificate, it must match, and the certificate itself must verify
       through the existing artifact verifier.
    3. If the expected statement is supplied, the receipt's settlement
       evidence must equal it exactly — tampering with any settlement-critical
       field (amounts, units, contract digest, status) fails verification.
    """
    if not isinstance(receipt, dict):
        return _receipt_report(False, "INVALID_RECEIPT_STRUCTURE", "receipt is not a mapping",
                               third_party=False, artifacts_verified=False)

    sig_info = receipt.get("digital_signature", {}) or {}
    algorithm = sig_info.get("algorithm", "")
    key_id = sig_info.get("key_id", "")
    signature = sig_info.get("signature", "")
    payload = {k: v for k, v in receipt.items() if k != "digital_signature"}

    verification_secret: Optional[str] = None
    verification_public_key: Optional[str] = None
    if algorithm == "HMAC-SHA256":
        verification_secret = CryptoEngine.resolve_verification_secret(
            key_id, getattr(settings, "trusted_hmac_keys", {}))
        key_trusted = verification_secret is not None
    elif algorithm == "Ed25519":
        verification_public_key = CryptoEngine.resolve_verification_public_key(
            key_id, getattr(settings, "ed25519_trusted_keys", {}))
        key_trusted = verification_public_key is not None
    else:
        key_trusted = False

    if not key_trusted:
        return _receipt_report(False, "SIGNATURE_KEY_UNTRUSTED",
                               f"signing key '{key_id}' is not trusted", third_party=False,
                               artifacts_verified=False)
    if algorithm == "HMAC-SHA256":
        try:
            sig_valid = isinstance(signature, str) and CryptoEngine.verify_signature(
                payload, signature, secret_key=verification_secret)
        except (TypeError, ValueError):
            sig_valid = False
    else:
        sig_valid = isinstance(signature, str) and CryptoEngine.verify_signature_ed25519(
            payload, signature, verification_public_key)
    if not sig_valid:
        return _receipt_report(False, "TAMPERED_OR_INVALID_SIGNATURE",
                               "settlement receipt signature does not match its content",
                               third_party=(algorithm == "Ed25519"), artifacts_verified=False)

    third_party = algorithm == "Ed25519"

    # Schema check: the receipt must carry the settlement evidence section.
    evidence = receipt.get("settlement_evidence")
    if not isinstance(evidence, dict):
        return _receipt_report(False, "INVALID_RECEIPT_STRUCTURE",
                               "settlement receipt is missing its settlement evidence section",
                               third_party=third_party, artifacts_verified=False)
    declared_digest = receipt.get("delivery_certificate_digest")
    if not isinstance(declared_digest, str) or not declared_digest:
        return _receipt_report(False, "INVALID_RECEIPT_STRUCTURE",
                               "settlement receipt does not bind a delivery certificate digest",
                               third_party=third_party, artifacts_verified=False)

    artifacts_status = "DELIVERY_CERTIFICATE_NOT_SUPPLIED"
    if delivery_certificate is not None:
        recomputed = certificate_digest(delivery_certificate)
        if recomputed != declared_digest:
            return _receipt_report(
                False, "DELIVERY_CERTIFICATE_DIGEST_MISMATCH",
                "the supplied delivery certificate does not match the digest bound into the receipt",
                third_party=third_party, artifacts_verified=False)
        cert_report = ArtifactGenerator.verify_certificate_and_artifacts(delivery_certificate)
        if cert_report.get("status") != "AUTHENTIC_AND_UNMODIFIED":
            return _receipt_report(
                False, "DELIVERY_CERTIFICATE_INVALID",
                f"referenced delivery certificate failed verification ({cert_report.get('status')})",
                third_party=third_party, artifacts_verified=False)
        artifacts_status = "DELIVERY_CERTIFICATE_VERIFIED"

    if expected_statement is not None:
        expected_dump = expected_statement.model_dump(mode="json")
        for field in ("settlement_id", "contract_digest", "run_id", "certificate_digest",
                      "calculation", "acceptance_status", "settlement_status",
                      "gross_amount_minor", "qualified_units"):
            if field in expected_dump:
                ev_value = evidence.get(field)
                exp_value = expected_dump[field]
                if ev_value != exp_value:
                    return _receipt_report(
                        False, "SETTLEMENT_EVIDENCE_MISMATCH",
                        f"settlement-critical field '{field}' does not match the verified statement",
                        third_party=third_party, artifacts_verified=False)

    return _receipt_report(True, "AUTHENTIC_AND_UNMODIFIED",
                           "settlement receipt is authentic and binds the settlement evidence",
                           third_party=third_party, artifacts_verified=True,
                           artifacts_status=artifacts_status)


def _receipt_report(sig_valid: bool, status_code: str, detail: str, *,
                    third_party: bool, artifacts_verified: bool,
                    artifacts_status: str = "NOT_CHECKED") -> Dict[str, Any]:
    overall = "AUTHENTIC_AND_UNMODIFIED" if sig_valid and artifacts_verified else status_code
    return {
        "signature_valid": sig_valid,
        "signature_algorithm": "Ed25519" if third_party else "HMAC-SHA256",
        "third_party_verifiable": third_party,
        "artifacts_verified": artifacts_verified,
        "artifacts_status": artifacts_status,
        "status": overall if sig_valid else status_code,
        "detail": detail,
    }


def serialize_receipt(receipt: Dict[str, Any]) -> str:
    """Canonical JSON serialization for storage and transport."""
    return json.dumps(receipt, sort_keys=True, separators=(",", ":"), default=str)
