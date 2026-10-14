"""Generates immutable run reconciliation ledgers and cryptographically signed audit certificates."""

from __future__ import annotations
import json
from pathlib import Path
from typing import Dict, Any, List, Optional
from datashell.engines.reconciler import ReconciliationLedger
from datashell.engines.quality import BatchRuleResult
from datashell.core.crypto import CryptoEngine
from datashell.core.config import settings


class ArtifactGenerator:
    """Produces permanent, cryptographically signed audit certificates and Merkle manifests."""

    @staticmethod
    def generate_audit_certificate(
        ledger: ReconciliationLedger,
        batch_results: List[BatchRuleResult],
        contract_name: str,
        contract_version: str,
        contract_canonical_hash: Optional[str] = None,
        source_file_path: Optional[Path] = None,
        delivered_file_path: Optional[Path] = None,
        delivered_sha256_override: Optional[str] = None,
        run_artifact_path: Optional[Path] = None,
        quarantine_file_path: Optional[Path] = None,
        quarantine_files: Optional[List[Path]] = None,
        drift_results: Optional[List[Dict[str, Any]]] = None,
        tenant_id: str = "default-tenant"
    ) -> Dict[str, Any]:
        """Produce tamper-evident cryptographic audit certificate."""
        
        # 1. Compute Cryptographic File Checksums
        source_hash = CryptoEngine.sha256_file(source_file_path) if (source_file_path and Path(source_file_path).exists()) else "IN_MEMORY_PAYLOAD"
        
        # Delivered hash: must distinguish actual delivered file vs rejected run
        if delivered_sha256_override:
            delivered_hash = delivered_sha256_override
            delivered_status = "DELIVERED"
        elif delivered_file_path and Path(delivered_file_path).exists():
            delivered_hash = CryptoEngine.sha256_file(delivered_file_path)
            delivered_status = "DELIVERED"
        elif ledger.actual_delivered == 0:
            delivered_hash = "ABSENT_DUE_TO_REJECTION"
            delivered_status = "REJECTED_ZERO_DELIVERED"
        else:
            raise FileNotFoundError(f"Delivered file '{delivered_file_path}' expected but not found on disk")

        # Quarantine artifacts: combine all held records and structural rejections
        quarantine_list: List[Path] = []
        if quarantine_files:
            for p in quarantine_files:
                if not p.exists():
                    raise FileNotFoundError(f"Expected quarantine artifact '{p}' was not found on disk")
                quarantine_list.append(p)
        elif quarantine_file_path:
            p = Path(quarantine_file_path)
            if not p.exists():
                raise FileNotFoundError(f"Expected quarantine artifact '{p}' was not found on disk")
            quarantine_list.append(p)

        quarantine_manifest: Dict[str, Any] = {}
        for q_path in quarantine_list:
            quarantine_manifest[q_path.name] = {
                "sha256": CryptoEngine.sha256_file(q_path),
                "path": str(q_path),
            }

        quarantine_hash = (
            CryptoEngine.compute_merkle_root([entry["sha256"] for entry in quarantine_manifest.values()])
            if quarantine_manifest
            else "NONE_NO_VIOLATIONS"
        )

        # 2. Canonical Contract Hash (Binds directly to full contract specification)
        contract_hash = contract_canonical_hash or CryptoEngine.sha256_bytes(
            f"{contract_name}:{contract_version}".encode("utf-8")
        )

        # 3. Compute Merkle Root over all dataset stages
        merkle_root = CryptoEngine.compute_merkle_root([
            source_hash,
            delivered_hash,
            quarantine_hash,
            contract_hash
        ])

        certificate = {
            "certificate_version": "2.0-enterprise",
            "run_id": ledger.run_id,
            "tenant_id": tenant_id,
            "timestamp": ledger.timestamp,
            "contract": {
                "name": contract_name,
                "version": contract_version,
                "sha256": contract_hash
            },
            "cryptographic_manifest": {
                "source_payload_sha256": source_hash,
                "delivered_payload_sha256": delivered_hash,
                "delivered_status": delivered_status,
                "delivered_file_path": str(delivered_file_path) if delivered_file_path else None,
                "run_artifact_path": str(run_artifact_path) if run_artifact_path else None,
                "quarantine_payload_sha256": quarantine_hash,
                "quarantine_artifacts": quarantine_manifest,
                "merkle_root": merkle_root
            },
            "reconciliation": {
                "rows_read": ledger.rows_read,
                "refused_structural": ledger.refused_structural,
                "held_quality": ledger.held_quality,
                "candidate_rows": ledger.rows_to_deliver,
                "rows_delivered": ledger.actual_delivered,
                "unaccounted_for": ledger.unaccounted_for,
                "footing_matched": ledger.footing_matched,
                "pct_delivered": round(ledger.pct_delivered, 2),
                "pct_refused_or_held": round(ledger.pct_refused_or_held, 2)
            },
            "rule_breakdown": ledger.rule_breakdown,
            "batch_evaluations": [
                {
                    "rule_id": b.rule_id,
                    "check": b.check,
                    "passed": b.passed,
                    "actual": b.actual_value,
                    "expected": b.expected_value,
                    "description": b.description
                }
                for b in batch_results
            ],
            "drift_evaluations": [
                {
                    "rule_id": getattr(d, "rule_id", ""),
                    "metric": getattr(d, "metric", ""),
                    "current_value": getattr(d, "current_value", 0.0),
                    "baseline_value": getattr(d, "baseline_value", 0.0),
                    "pct_change": getattr(d, "pct_change", 0.0),
                    "threshold_pct": getattr(d, "threshold_pct", 0.0),
                    "passed": getattr(d, "passed", True),
                    "details": getattr(d, "details", "")
                } if hasattr(d, "rule_id") else d
                for d in (drift_results or [])
            ]
        }

        # 4. Digitally sign the certificate with HMAC-SHA256
        active_key_id = getattr(settings, "active_key_id", "datashell-hmac-v1")
        sig_envelope = {
            "algorithm": "HMAC-SHA256",
            "key_id": active_key_id,
            "integrity_type": "SYMMETRIC_HMAC_AUTHENTICATION",
            "tamper_evident": True
        }
        certificate["signature_header"] = sig_envelope

        # Digitally sign the complete payload including signature_header
        signature = CryptoEngine.sign_manifest(certificate)
        certificate["digital_signature"] = {
            "algorithm": sig_envelope["algorithm"],
            "key_id": sig_envelope["key_id"],
            "integrity_type": sig_envelope["integrity_type"],
            "signature": signature,
            "tamper_evident": True
        }

        return certificate

    @staticmethod
    def verify_certificate_and_artifacts(cert_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Comprehensive cryptographic verifier:
        1. Validates signature algorithm, key identifier trust, and HMAC signature integrity.
        2. Recomputes hashes of the underlying delivered lineage dataset and quarantine artifacts.
        3. Identifies whether artifacts have been tampered with, deleted, or match exactly.
        """
        from datashell.core.config import settings

        if not isinstance(cert_data, dict):
            return {
                "signature_valid": False, "signature_algorithm": "", "key_id": "",
                "key_trusted": False, "artifacts_verified": False,
                "artifacts_status": "INVALID_CERTIFICATE_STRUCTURE",
                "status": "TAMPERED_OR_INVALID_SIGNATURE", "artifact_details": {},
            }
        sig_info = cert_data.get("digital_signature", {})
        if not isinstance(sig_info, dict):
            sig_info = {}
        algorithm = sig_info.get("algorithm", "")
        key_id = sig_info.get("key_id", "")
        signature = sig_info.get("signature", "")

        # The keyring is the source of truth. A key ID on an allowlist without
        # its actual secret is not a trusted signing key.
        trusted_keyring = getattr(settings, "trusted_hmac_keys", {})
        verification_secret = CryptoEngine.resolve_verification_secret(key_id, trusted_keyring)
        key_trusted = verification_secret is not None

        # Check signature header integrity if present
        header_intact = True
        if "signature_header" in cert_data:
            sig_hdr = cert_data["signature_header"]
            if not isinstance(sig_hdr, dict) or sig_hdr.get("key_id") != key_id or sig_hdr.get("algorithm") != algorithm:
                header_intact = False

        if algorithm != "HMAC-SHA256" or not key_trusted or not header_intact:
            sig_valid = False
        else:
            payload = {k: v for k, v in cert_data.items() if k != "digital_signature"}
            try:
                sig_valid = isinstance(signature, str) and CryptoEngine.verify_signature(
                    payload, signature, secret_key=verification_secret
                )
            except (TypeError, ValueError):
                sig_valid = False

        # Never dereference artifact paths from an untrusted certificate. A
        # modified certificate could otherwise turn verification into an
        # arbitrary local file-read primitive before its invalid signature is
        # reported.
        if not key_trusted:
            return {
                "signature_valid": False,
                "signature_algorithm": algorithm,
                "key_id": key_id,
                "key_trusted": False,
                "artifacts_verified": False,
                "artifacts_status": "SIGNATURE_KEY_UNTRUSTED",
                "status": "UNTRUSTED_KEY_IDENTIFIER",
                "artifact_details": {},
            }
        if not sig_valid:
            return {
                "signature_valid": False,
                "signature_algorithm": algorithm,
                "key_id": key_id,
                "key_trusted": True,
                "artifacts_verified": False,
                "artifacts_status": "SIGNATURE_INVALID",
                "status": "TAMPERED_OR_INVALID_SIGNATURE",
                "artifact_details": {},
            }

        # 2. Recompute and verify underlying data artifacts only after the
        # certificate signature is authentic.
        artifact_details: Dict[str, Any] = {}
        manifest = cert_data.get("cryptographic_manifest", {})
        if not isinstance(manifest, dict):
            return {
                "signature_valid": True, "signature_algorithm": algorithm, "key_id": key_id,
                "key_trusted": True, "artifacts_verified": False,
                "artifacts_status": "INVALID_CERTIFICATE_STRUCTURE",
                "status": "TAMPERED_OR_INVALID_SIGNATURE", "artifact_details": {},
            }
        delivered_status = manifest.get("delivered_status", "")
        expected_delivered_hash = manifest.get("delivered_payload_sha256", "")
        delivered_path_str = manifest.get("delivered_file_path")
        run_artifact_str = manifest.get("run_artifact_path")
        tenant_id = cert_data.get("tenant_id", "default-tenant")
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            return {
                "signature_valid": True, "signature_algorithm": algorithm, "key_id": key_id,
                "key_trusted": True, "artifacts_verified": False,
                "artifacts_status": "INVALID_CERTIFICATE_STRUCTURE",
                "status": "TAMPERED_OR_INVALID_SIGNATURE", "artifact_details": {},
            }
        delivery_roots = [settings.get_tenant_delivery_dir(tenant_id).resolve()]
        quarantine_roots = [settings.get_tenant_quarantine_dir(tenant_id).resolve()]
        if not settings.is_production_mode:
            import tempfile
            delivery_roots.extend([settings.output_dir.resolve(), Path(tempfile.gettempdir()).resolve()])
            quarantine_roots.extend([settings.quarantine_dir.resolve(), Path(tempfile.gettempdir()).resolve()])

        def safe_artifact_path(raw_path: Any, allowed_roots: list[Path], label: str) -> Optional[Path]:
            if not isinstance(raw_path, str) or not raw_path.strip():
                artifact_details[label] = {"status": "UNSAFE_PATH"}
                return None
            candidate = Path(raw_path)
            try:
                resolved = candidate.resolve(strict=False)
                if candidate.is_symlink() or not any(resolved.is_relative_to(root) for root in allowed_roots):
                    artifact_details[label] = {"status": "UNSAFE_PATH"}
                    return None
                return resolved
            except (OSError, RuntimeError, ValueError):
                artifact_details[label] = {"status": "UNSAFE_PATH"}
                return None

        if delivered_status == "DELIVERED":
            # Verify immutable run lineage artifact independently
            if run_artifact_str:
                run_file = safe_artifact_path(run_artifact_str, delivery_roots, "run_lineage_artifact")
                if run_file is None:
                    pass
                elif not run_file.exists():
                    artifact_details["run_lineage_artifact"] = {
                        "status": "MISSING",
                        "expected_sha256": expected_delivered_hash,
                        "path": run_artifact_str
                    }
                else:
                    try:
                        actual_run_hash = CryptoEngine.sha256_file(run_file)
                        artifact_details["run_lineage_artifact"] = {
                            "status": "VERIFIED" if actual_run_hash == expected_delivered_hash else "MODIFIED",
                            "expected_sha256": expected_delivered_hash,
                            "sha256": actual_run_hash,
                            "path": str(run_file)
                        }
                    except Exception as e:
                        artifact_details["run_lineage_artifact"] = {
                            "status": "ERROR_READING",
                            "error_type": type(e).__name__,
                            "path": str(run_file)
                        }

            # Verify active delivered dataset independently
            if delivered_path_str:
                active_file = safe_artifact_path(delivered_path_str, delivery_roots, "active_delivered_target")
                if active_file is None:
                    pass
                elif not active_file.exists():
                    artifact_details["active_delivered_target"] = {
                        "status": "MISSING",
                        "expected_sha256": expected_delivered_hash,
                        "path": delivered_path_str
                    }
                else:
                    try:
                        actual_active_hash = CryptoEngine.sha256_file(active_file)
                        artifact_details["active_delivered_target"] = {
                            "status": "VERIFIED" if actual_active_hash == expected_delivered_hash else "SUPERSEDED_OR_MODIFIED",
                            "expected_sha256": expected_delivered_hash,
                            "sha256": actual_active_hash,
                            "path": str(active_file)
                        }
                    except Exception as e:
                        artifact_details["active_delivered_target"] = {
                            "status": "ERROR_READING",
                            "error_type": type(e).__name__,
                            "path": str(active_file)
                        }

            # Backward-compatible key mapping for delivered_dataset
            if "run_lineage_artifact" in artifact_details:
                artifact_details["delivered_dataset"] = artifact_details["run_lineage_artifact"]
            elif "active_delivered_target" in artifact_details:
                artifact_details["delivered_dataset"] = artifact_details["active_delivered_target"]
            elif not run_artifact_str and not delivered_path_str:
                artifact_details["delivered_dataset"] = {
                    "status": "MISSING",
                    "expected_sha256": expected_delivered_hash,
                    "target_path": None
                }

        # Check all quarantine artifacts recorded in manifest
        quarantine_manifest = manifest.get("quarantine_artifacts", {})
        if not isinstance(quarantine_manifest, dict):
            quarantine_manifest = {}
            artifact_details["quarantine_manifest"] = {"status": "UNSAFE_PATH"}
        tenant_quarantine_dir = settings.get_tenant_quarantine_dir(tenant_id)
        for q_name, q_val in quarantine_manifest.items():
            if isinstance(q_val, dict):
                expected_q_hash = q_val.get("sha256", "")
                q_path_str = q_val.get("path")
                if q_path_str:
                    q_file = safe_artifact_path(q_path_str, quarantine_roots, str(q_name))
                elif not isinstance(q_name, str) or Path(q_name).name != q_name or q_name in {".", ".."}:
                    artifact_details[str(q_name)] = {"status": "UNSAFE_PATH"}
                    continue
                else:
                    q_file = safe_artifact_path(str(tenant_quarantine_dir / q_name), quarantine_roots, q_name)
            else:
                expected_q_hash = q_val
                if not isinstance(q_name, str) or Path(q_name).name != q_name or q_name in {".", ".."}:
                    artifact_details[str(q_name)] = {"status": "UNSAFE_PATH"}
                    continue
                candidate = tenant_quarantine_dir / q_name
                if not candidate.exists() and not settings.is_production_mode and (settings.quarantine_dir / q_name).exists():
                    candidate = settings.quarantine_dir / q_name
                q_file = safe_artifact_path(str(candidate), quarantine_roots, q_name)

            if q_file is None:
                # safe_artifact_path already recorded an UNSAFE_PATH result.
                continue
            if not q_file.exists():
                artifact_details[q_name] = {
                    "status": "MISSING",
                    "expected_sha256": expected_q_hash,
                    "path": str(q_file)
                }
            else:
                try:
                    actual_q_hash = CryptoEngine.sha256_file(q_file)
                    if actual_q_hash == expected_q_hash:
                        artifact_details[q_name] = {
                            "status": "VERIFIED",
                            "sha256": actual_q_hash,
                            "path": str(q_file)
                        }
                    else:
                        artifact_details[q_name] = {
                            "status": "MODIFIED",
                            "expected_sha256": expected_q_hash,
                            "actual_sha256": actual_q_hash,
                            "path": str(q_file)
                        }
                except Exception as e:
                    artifact_details[q_name] = {
                        "status": "ERROR_READING",
                        "error_type": type(e).__name__
                    }

        any_missing = any(v.get("status") == "MISSING" for v in artifact_details.values())
        any_modified = any(v.get("status") in ("MODIFIED", "ERROR_READING", "UNSAFE_PATH") for v in artifact_details.values())
        all_artifacts_ok = not any_missing and not any_modified and len(artifact_details) > 0

        if not key_trusted:
            overall_status = "UNTRUSTED_KEY_IDENTIFIER"
            artifacts_status = "SIGNATURE_KEY_UNTRUSTED"
        elif not sig_valid:
            overall_status = "TAMPERED_OR_INVALID_SIGNATURE"
            artifacts_status = "SIGNATURE_INVALID"
        elif any_modified:
            overall_status = "TAMPERED_UNDERLYING_ARTIFACTS"
            artifacts_status = "ARTIFACTS_MODIFIED"
        elif any_missing:
            overall_status = "MISSING_UNDERLYING_ARTIFACTS"
            artifacts_status = "ARTIFACTS_MISSING"
        else:
            overall_status = "AUTHENTIC_AND_UNMODIFIED"
            artifacts_status = "ALL_ARTIFACTS_VERIFIED" if artifact_details else "NO_DELIVERED_ARTIFACTS"

        return {
            "signature_valid": sig_valid,
            "signature_algorithm": algorithm,
            "key_id": key_id,
            "key_trusted": key_trusted,
            "artifacts_verified": all_artifacts_ok or (delivered_status != "DELIVERED" and not any_missing and not any_modified),
            "artifacts_status": artifacts_status,
            "status": overall_status,
            "artifact_details": artifact_details
        }

    @staticmethod
    def save_audit_file(audit_data: Dict[str, Any], output_path: Path) -> Path:
        """Persist audit JSON certificate to disk atomically."""
        output_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = output_path.with_name(f".tmp_{output_path.name}")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(audit_data, f, indent=2)
        import os
        os.replace(tmp_path, output_path)
        return output_path

