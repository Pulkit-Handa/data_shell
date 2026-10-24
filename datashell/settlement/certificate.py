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
from typing import Any

from datashell.core.config import settings
from datashell.core.crypto import CryptoEngine
from datashell.pipeline.artifacts import ArtifactGenerator
from datashell.settlement.models import SettlementStatement

SETTLEMENT_RECEIPT_VERSION = "1"


class SettlementCertificateError(RuntimeError):
    """Raised when a settlement receipt cannot be produced or parsed safely."""


def certificate_digest(cert_data: dict[str, Any]) -> str:
    """Digest over a delivery certificate's canonical JSON (signature excluded).

    Mirrors the signing convention: the ``digital_signature`` block is not
    part of the signed payload, so it is excluded from the binding digest too.
    """
    payload = {k: v for k, v in cert_data.items() if k != "digital_signature"}
    return CryptoEngine.canonical_json_hash(payload)


def receipt_digest(receipt: dict[str, Any]) -> str:
    """Digest over the complete signed receipt, including its signature.

    Used to chain lifecycle receipts: each transition receipt binds the digest
    of the receipt that preceded it, so deleting or editing any stored receipt
    breaks the chain even when the remaining signatures are valid.
    """
    if not isinstance(receipt, dict):
        raise SettlementCertificateError("settlement receipt must be a mapping")
    return CryptoEngine.canonical_json_hash(receipt)


def build_settlement_receipt(
    statement: SettlementStatement,
    delivery_certificate: dict[str, Any] | None = None,
    *,
    delivery_certificate_digest: str | None = None,
    previous_receipt: dict[str, Any] | None = None,
    transition: dict[str, Any] | None = None,
    receipt_kind: str = "ISSUANCE",
) -> dict[str, Any]:
    """Produce the signed settlement receipt for a calculated statement.

    The receipt embeds the statement's signed payload and the delivery
    certificate digest, then signs the whole envelope exactly like an audit
    certificate so independent verification uses the same trusted keyrings.

    Lifecycle transitions produce a new receipt chained to the previous one via
    ``previous_receipt_digest``. Receipts are normally persisted by the ledger at
    issuance and transition time; verification must use those stored receipts
    rather than re-signing the current database row.
    """
    if delivery_certificate is not None:
        if not isinstance(delivery_certificate, dict):
            raise SettlementCertificateError("delivery certificate must be a mapping")
        if delivery_certificate_digest is not None:
            raise SettlementCertificateError(
                "supply either a delivery certificate or its digest, not both")
        delivery_digest = certificate_digest(delivery_certificate)
    elif isinstance(delivery_certificate_digest, str) and delivery_certificate_digest:
        delivery_digest = delivery_certificate_digest
    else:
        raise SettlementCertificateError(
            "a delivery certificate or its digest is required to build a settlement receipt")

    receipt_payload = {
        "receipt_version": SETTLEMENT_RECEIPT_VERSION,
        "receipt_kind": receipt_kind,
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
    if previous_receipt is not None:
        receipt_payload["previous_receipt_digest"] = receipt_digest(previous_receipt)
    if transition is not None:
        receipt_payload["transition"] = transition

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
            "requires_shared_secret": False,  # nosec B105 — key *identifier* label, not a secret
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
            "requires_shared_secret": True,  # nosec B105 — key *identifier* label, not a secret
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


# Fields that must be identical between the signed evidence and the current
# database row. ``idempotency_key`` is deliberately excluded because the ledger
# may attach one to an existing deterministic statement after issuance; it is
# request metadata, not financial evidence.
_BOUND_STATEMENT_FIELDS = (
    "settlement_id",
    "tenant_id",
    "contract_id",
    "contract_version",
    "contract_digest",
    "run_id",
    "certificate_digest",
    "calculation",
    "currency",
    "acceptance_status",
    "settlement_status",
    "reason_codes",
    "explanation",
    "actor",
    "superseding_settlement_id",
    "billing_instruction_id",
    "created_at",
)


def verify_settlement_receipt(
    receipt: dict[str, Any],
    *,
    delivery_certificate: dict[str, Any] | None = None,
    expected_statement: SettlementStatement | None = None,
    expected_row: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Verify a settlement receipt's signature and its bindings.

    Steps:
    1. Signature algorithm/key trust + signature validity (same rules as
       audit certificate verification).
    2. Recompute the delivery certificate digest; if the caller supplies the
       certificate, it must match, and the certificate itself must verify
       through the existing artifact verifier.
    3. If the expected statement is supplied, the receipt's settlement
       evidence must equal it — tampering with any settlement-critical field
       (amounts, units, contract digest, status) fails verification.
    4. If the expected database row is supplied, mirrored financial columns
       (gross amount, qualified units, pricing model) must agree with the
       signed calculation.
    """
    if not isinstance(receipt, dict):
        return _receipt_report(False, "INVALID_RECEIPT_STRUCTURE", "receipt is not a mapping",
                               third_party=False, artifacts_verified=False)

    sig_info = receipt.get("digital_signature", {}) or {}
    algorithm = sig_info.get("algorithm", "")
    key_id = sig_info.get("key_id", "")
    signature = sig_info.get("signature", "")
    payload = {k: v for k, v in receipt.items() if k != "digital_signature"}

    verification_secret: str | None = None
    verification_public_key: str | None = None
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
        # key_trusted checked above means a resolved verification key exists
        if not isinstance(signature, str) or verification_public_key is None:
            sig_valid = False
        else:
            sig_valid = CryptoEngine.verify_signature_ed25519(
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

    # The outer identifiers and contract block are signed alongside the
    # evidence; reject a receipt whose own sections tell different stories.
    for field in ("settlement_id", "tenant_id", "run_id"):
        if receipt.get(field) != evidence.get(field):
            return _receipt_report(
                False, "INVALID_RECEIPT_STRUCTURE",
                f"receipt field '{field}' disagrees with its settlement evidence",
                third_party=third_party, artifacts_verified=False)
    contract = receipt.get("contract")
    if not isinstance(contract, dict):
        return _receipt_report(False, "INVALID_RECEIPT_STRUCTURE",
                               "settlement receipt is missing its contract binding",
                               third_party=third_party, artifacts_verified=False)
    for field in ("contract_id", "contract_version", "contract_digest"):
        if contract.get(field) != evidence.get(field):
            return _receipt_report(
                False, "INVALID_RECEIPT_STRUCTURE",
                f"receipt contract field '{field}' disagrees with its settlement evidence",
                third_party=third_party, artifacts_verified=False)
    if evidence.get("certificate_digest") != declared_digest:
        return _receipt_report(
            False, "INVALID_RECEIPT_STRUCTURE",
            "receipt delivery-certificate binding disagrees with its settlement evidence",
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
        for field in _BOUND_STATEMENT_FIELDS:
            # ``idempotency_key`` is intentionally not bound: see the constant
            # definition above.
            if field in expected_dump and evidence.get(field) != expected_dump[field]:
                return _receipt_report(
                    False, "SETTLEMENT_EVIDENCE_MISMATCH",
                    f"settlement-critical field '{field}' does not match the verified statement",
                    third_party=third_party, artifacts_verified=False)

    if expected_row is not None:
        calculation = evidence.get("calculation")
        for field in _BOUND_STATEMENT_FIELDS:
            if field in expected_row and expected_row.get(field) != evidence.get(field):
                return _receipt_report(
                    False, "SETTLEMENT_EVIDENCE_MISMATCH",
                    f"stored settlement column '{field}' does not match the signed evidence",
                    third_party=third_party, artifacts_verified=False)
        # These columns mirror values inside ``calculation_json`` but are
        # stored separately for indexing/dashboards; a DB edit may change only
        # the mirror column, so compare them explicitly.
        mirrored = {
            "gross_amount_minor": calculation.get("gross_amount_minor") if isinstance(calculation, dict) else None,
            "qualified_units": calculation.get("qualified_units") if isinstance(calculation, dict) else None,
            "pricing_model": calculation.get("pricing_model") if isinstance(calculation, dict) else None,
            "calculation_version": calculation.get("calculation_version") if isinstance(calculation, dict) else None,
        }
        for column, signed_value in mirrored.items():
            if column in expected_row and expected_row.get(column) != signed_value:
                return _receipt_report(
                    False, "SETTLEMENT_EVIDENCE_MISMATCH",
                    f"stored settlement column '{column}' does not match the signed calculation",
                    third_party=third_party, artifacts_verified=False)

    return _receipt_report(True, "AUTHENTIC_AND_UNMODIFIED",
                           "settlement receipt is authentic and binds the settlement evidence",
                           third_party=third_party,
                           artifacts_verified=delivery_certificate is not None,
                           artifacts_status=artifacts_status)


def verify_settlement_receipt_chain(
    receipts: list[dict[str, Any]],
    *,
    delivery_certificate: dict[str, Any] | None = None,
    expected_statement: SettlementStatement | None = None,
    expected_row: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Verify the stored receipt chain and bind it to the current row.

    Receipts are ordered oldest-first. The first receipt is the issuance
    receipt; every later receipt must name the digest of its predecessor. The
    newest receipt must match the current statement/row. Failures are reported
    as ``RECEIPT_NOT_FOUND``, ``RECEIPT_CHAIN_BROKEN``, or the specific
    signature/binding error from :func:`verify_settlement_receipt`.

    Verification never creates or re-signs a receipt: an attacker who can edit
    the database cannot turn a modified row into "authentic" evidence.
    """
    if not isinstance(receipts, list) or not receipts:
        return _receipt_report(
            False, "RECEIPT_NOT_FOUND",
            "no stored settlement receipt exists for this settlement; refusing to re-sign the current row",
            third_party=False, artifacts_verified=False)

    previous_digest: str | None = None
    final_report: dict[str, Any] | None = None
    for index, receipt in enumerate(receipts):
        is_latest = index == len(receipts) - 1
        report = verify_settlement_receipt(
            receipt,
            delivery_certificate=delivery_certificate,
            expected_statement=expected_statement if is_latest else None,
            expected_row=expected_row if is_latest else None,
        )
        if report.get("status") != "AUTHENTIC_AND_UNMODIFIED":
            report = dict(report)
            report["receipt_count"] = len(receipts)
            report["receipt_index"] = index
            return report

        declared_previous = receipt.get("previous_receipt_digest") if isinstance(receipt, dict) else None
        if index == 0:
            if declared_previous is not None:
                return _chain_report(
                    "RECEIPT_CHAIN_BROKEN",
                    "the first stored receipt references a predecessor that is missing",
                    report, receipt_count=len(receipts), receipt_index=index)
        elif declared_previous != previous_digest:
            return _chain_report(
                "RECEIPT_CHAIN_BROKEN",
                "a stored receipt does not reference its predecessor's digest",
                report, receipt_count=len(receipts), receipt_index=index)

        previous_digest = receipt_digest(receipt)
        final_report = report

    if final_report is None:  # defensive: the loop always sets it for non-empty input
        return _receipt_report(False, "RECEIPT_NOT_FOUND",
                               "no stored settlement receipt could be verified",
                               third_party=False, artifacts_verified=False)
    final_report = dict(final_report)
    final_report["receipt_count"] = len(receipts)
    final_report["receipt_digest"] = previous_digest
    return final_report


def _chain_report(status_code: str, detail: str, report: dict[str, Any], *,
                  receipt_count: int, receipt_index: int) -> dict[str, Any]:
    """Build a failure report for a validly signed but broken receipt chain."""
    return {
        "signature_valid": bool(report.get("signature_valid")),
        "signature_algorithm": report.get("signature_algorithm", ""),
        "third_party_verifiable": bool(report.get("third_party_verifiable")),
        "artifacts_verified": bool(report.get("artifacts_verified")),
        "artifacts_status": report.get("artifacts_status", "NOT_CHECKED"),
        "status": status_code,
        "detail": detail,
        "receipt_count": receipt_count,
        "receipt_index": receipt_index,
    }


def _receipt_report(sig_valid: bool, status_code: str, detail: str, *,
                    third_party: bool, artifacts_verified: bool,
                    artifacts_status: str = "NOT_CHECKED") -> dict[str, Any]:
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


def serialize_receipt(receipt: dict[str, Any]) -> str:
    """Canonical JSON serialization for storage and transport."""
    return json.dumps(receipt, sort_keys=True, separators=(",", ":"), default=str)
