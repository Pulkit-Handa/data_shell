"""Enterprise configuration management."""

import os
import secrets
import re
import json
import tempfile
from pathlib import Path
from typing import List, Optional, Dict
from pydantic import BaseModel, Field


def normalize_environment(env: Optional[str]) -> str:
    """
    Normalizes environment name into lowercase canonical representation.
    Treats aliases like 'prod', 'production', and 'live' as 'production'.
    """
    if not env:
        return "development"
    cleaned = str(env).strip().lower()
    if cleaned in ("production", "prod", "live"):
        return "production"
    if cleaned in ("development", "dev", "local"):
        return "development"
    if cleaned in ("staging", "stage", "testing", "test"):
        return cleaned
    return cleaned


def is_production(env: Optional[str]) -> bool:
    """Returns True if the environment represents a production deployment."""
    return normalize_environment(env) == "production"


# Built-in inbound upload ceiling. 20 GiB matches how hosts size block volumes
# and lets the local-file CLI ingest multi-gigabyte partner feeds without an
# environment override. Operators override it with MAX_UPLOAD_SIZE_BYTES.
DEFAULT_MAX_UPLOAD_SIZE_BYTES = 20 * 1024 ** 3

# Multipart/form-data framing (boundaries, headers, trailing CRLFs) adds a small
# amount beyond the raw payload. The request-body ceiling is derived from the
# upload limit plus this fixed headroom so the two never drift apart.
MULTIPART_OVERHEAD_BYTES = 1024 * 1024

# A delivery stages a copy of the output and may retain the worst-case
# quarantine alongside it, so a run can transiently require a multiple of the
# source size on the storage volume. The preflight multiplies the source size by
# this factor and refuses to start when the volume cannot hold it.
DEFAULT_DISK_HEADROOM_MULTIPLIER = 2.0


def _load_hmac_keyring() -> Dict[str, str]:
    raw = os.getenv("DATASHELL_HMAC_KEYRING", "{}").strip()
    if not raw:
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise RuntimeError("DATASHELL_HMAC_KEYRING must be a JSON object")
    return parsed


def _load_ed25519_keyring() -> Dict[str, str]:
    """Trusted Ed25519 public keys, mapped key_id -> public key material (PEM or base64)."""
    raw = os.getenv("DATASHELL_ED25519_KEYRING", "{}").strip()
    if not raw:
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise RuntimeError("DATASHELL_ED25519_KEYRING must be a JSON object mapping key IDs to public keys")
    return parsed


def _calculate_shannon_entropy(s: str) -> float:
    """Calculates Shannon entropy in bits per character."""
    import math
    if not s:
        return 0.0
    freq = {}
    for c in s:
        freq[c] = freq.get(c, 0) + 1
    entropy = 0.0
    for count in freq.values():
        p = count / len(s)
        entropy -= p * math.log2(p)
    return entropy


def _resolve_secret(env_var: str, purpose: str, env_mode: str, value_override: Optional[str] = None) -> str:
    """
    Resolves a security secret strictly:
    - In production: refuses to start without explicitly provided, high-entropy secret.
      Rejects placeholders, default credentials, short strings (<32 chars), and low entropy.
    - In development/test: generates an ephemeral, cryptographically secure random secret.
    """
    val = value_override if value_override is not None else os.getenv(env_var)
    insecure_defaults = {
        "dm_live_secret_key_prod_9941a8",
        "dm_crypto_hmac_secret_enterprise_2026",
        "replace_with_secure_generated_api_key",
        "replace_with_secure_generated_hmac_secret",
        "admin",
        "secret",
        "changeme",
        "password",
        "placeholder",
    }
    placeholder_keywords = [
        "replace", "placeholder", "changeme", "change_me", "default",
        "secret", "password", "dummy", "todo", "insert", "your_secret"
    ]

    if is_production(env_mode):
        if not val or not val.strip():
            raise RuntimeError(
                f"FATAL: Production security violation. '{env_var}' ({purpose}) must be explicitly "
                f"set via a secret manager or secure deployment environment. "
                f"Missing production credential."
            )
        cleaned = val.strip()
        lower_val = cleaned.lower()

        # Ephemeral development credentials must never be promoted to production.
        if lower_val.startswith("dm_ephemeral_"):
            raise RuntimeError(
                f"FATAL: Production security violation. '{env_var}' ({purpose}) is an ephemeral development credential."
            )

        # Reject exact known defaults
        if lower_val in insecure_defaults:
            raise RuntimeError(
                f"FATAL: Production security violation. '{env_var}' ({purpose}) is set to an insecure default "
                f"or known placeholder value '{cleaned}'. Refusing to start production service."
            )

        # Reject placeholder patterns
        for keyword in placeholder_keywords:
            if keyword in lower_val and len(cleaned) < 40:
                raise RuntimeError(
                    f"FATAL: Production security violation. '{env_var}' ({purpose}) appears to contain "
                    f"placeholder keyword '{keyword}'. Refusing to start with placeholder secret."
                )

        # Enforce minimum length (at least 32 characters)
        if len(cleaned) < 32:
            raise RuntimeError(
                f"FATAL: Production security violation. '{env_var}' ({purpose}) must be at least 32 characters "
                f"long (got {len(cleaned)} characters). Insufficient secret length."
            )

        # Enforce entropy requirement (minimum 3.0 bits/char and at least 10 unique characters)
        unique_chars = len(set(cleaned))
        entropy = _calculate_shannon_entropy(cleaned)
        if unique_chars < 10 or entropy < 3.0:
            raise RuntimeError(
                f"FATAL: Production security violation. '{env_var}' ({purpose}) lacks sufficient cryptographic entropy "
                f"(entropy: {entropy:.2f} bits/char, unique characters: {unique_chars}). "
                f"Provide a cryptographically secure random token."
            )

        return cleaned
    else:
        if val and val.strip():
            cleaned = val.strip()
            if cleaned.lower() not in insecure_defaults:
                return cleaned
        # Ephemeral secure random token for testing / development
        return f"dm_ephemeral_{secrets.token_urlsafe(32)}"



class EnterpriseSettings(BaseModel):
    environment: str = Field(default_factory=lambda: normalize_environment(os.getenv("ENVIRONMENT", "development")))
    log_level: str = Field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))
    
    # Storage paths - Centralized under DATASHELL_STORAGE_ROOT if specified
    storage_root: Path = Field(
        default_factory=lambda: Path(os.getenv("DATASHELL_STORAGE_ROOT", ".")).resolve()
    )
    data_dir: Path = Field(
        default_factory=lambda: Path(
            os.getenv("DATASHELL_DATA_DIR", str(Path(os.getenv("DATASHELL_STORAGE_ROOT", ".")) / "data"))
        ).resolve()
    )
    contracts_dir: Path = Field(
        default_factory=lambda: Path(
            os.getenv("DATASHELL_CONTRACTS_DIR", str(Path(os.getenv("DATASHELL_STORAGE_ROOT", ".")) / "contracts_registry"))
        ).resolve()
    )
    output_dir: Path = Field(
        default_factory=lambda: Path(
            os.getenv("DATASHELL_OUTPUT_DIR", str(Path(os.getenv("DATASHELL_STORAGE_ROOT", ".")) / "output"))
        ).resolve()
    )
    audit_dir: Path = Field(
        default_factory=lambda: Path(
            os.getenv("DATASHELL_AUDIT_DIR", str(Path(os.getenv("DATASHELL_STORAGE_ROOT", ".")) / "output" / "audits"))
        ).resolve()
    )
    quarantine_dir: Path = Field(
        default_factory=lambda: Path(
            os.getenv("DATASHELL_QUARANTINE_DIR", str(Path(os.getenv("DATASHELL_STORAGE_ROOT", ".")) / "output" / "quarantine"))
        ).resolve()
    )
    # Scratch space for spooled uploads and out-of-core database spills. Point
    # TMPDIR/DATASHELL_TMP_DIR at the durable storage volume so multi-gigabyte
    # uploads and DuckDB external sorts never exhaust a bounded memory tmpfs.
    temp_dir: Path = Field(
        default_factory=lambda: Path(
            os.getenv("DATASHELL_TMP_DIR") or os.getenv("TMPDIR") or tempfile.gettempdir()
        ).resolve()
    )
    
    # Security Credentials
    auth_enabled: bool = Field(default_factory=lambda: os.getenv("AUTH_ENABLED", "true").lower() in ("true", "1"))
    master_api_key: str = Field(
        default_factory=lambda: _resolve_secret(
            "DATASHELL_API_KEY", "Master API Key", os.getenv("ENVIRONMENT", "development")
        )
    )
    hmac_secret: str = Field(
        default_factory=lambda: _resolve_secret(
            "DATASHELL_HMAC_SECRET", "HMAC Signing Secret", os.getenv("ENVIRONMENT", "development")
        )
    )
    active_key_id: str = Field(default_factory=lambda: os.getenv("DATASHELL_KEY_ID", "datashell-hmac-v1"))
    trusted_hmac_keys: Dict[str, str] = Field(default_factory=_load_hmac_keyring)
    trusted_key_ids: List[str] = Field(
        default_factory=lambda: [
            k.strip() for k in os.getenv("DATASHELL_TRUSTED_KEYS", "datashell-hmac-v1,dm-hmac-v1,dm-hmac-master").split(",") if k.strip()
        ]
    )
    # Optional asymmetric signing. When a private key is provisioned, audit
    # certificates are signed with Ed25519 so any holder of the published public
    # key can verify them without a shared secret. HMAC is retained for internal
    # (symmetric) webhook signing.
    ed25519_private_key: Optional[str] = Field(
        default_factory=lambda: (os.getenv("DATASHELL_ED25519_PRIVATE_KEY") or "").strip() or None
    )
    ed25519_active_key_id: str = Field(
        default_factory=lambda: os.getenv("DATASHELL_ED25519_KEY_ID", "datashell-ed25519-v1")
    )
    ed25519_trusted_keys: Dict[str, str] = Field(default_factory=_load_ed25519_keyring)
    # Wildcard is convenient for local development only. Production must name
    # browser origins explicitly; an empty value intentionally fails closed.
    allowed_origins: List[str] = Field(
        default_factory=lambda: os.getenv(
            "ALLOWED_ORIGINS",
            "" if is_production(os.getenv("ENVIRONMENT", "development")) else "*",
        ).split(",")
    )
    trusted_proxy_ips: List[str] = Field(
        default_factory=lambda: [item.strip() for item in os.getenv("FORWARDED_ALLOW_IPS", "127.0.0.1").split(",") if item.strip()]
    )
    
    # Engine Tuning
    # Default upload ceiling is 20 GiB; override with MAX_UPLOAD_SIZE_BYTES. The
    # request-body limit is derived from the same value so raising the upload
    # limit also raises the multipart body bound (plus a fixed 1 MiB overhead).
    max_upload_size_bytes: int = Field(
        default_factory=lambda: int(os.getenv("MAX_UPLOAD_SIZE_BYTES", str(DEFAULT_MAX_UPLOAD_SIZE_BYTES)))
    )
    stream_chunk_rows: int = Field(default_factory=lambda: int(os.getenv("STREAM_CHUNK_ROWS", "100000")))
    # Files at or above this size are processed out-of-core even when a caller
    # does not explicitly request streaming, so large inputs never materialize
    # fully in application memory.
    stream_auto_threshold_bytes: int = Field(default_factory=lambda: int(os.getenv("STREAM_AUTO_THRESHOLD_BYTES", str(64 * 1024 * 1024))))  # 64 MB
    duckdb_memory_limit: str = Field(default_factory=lambda: os.getenv("DUCKDB_MEMORY_LIMIT", "1GB"))
    rate_limit_requests_per_minute: int = Field(default_factory=lambda: int(os.getenv("RATE_LIMIT_REQUESTS_PER_MINUTE", "120")))
    max_concurrent_runs: int = Field(default_factory=lambda: int(os.getenv("MAX_CONCURRENT_RUNS", "2")))
    max_request_body_bytes: int = Field(
        default_factory=lambda: int(
            os.getenv(
                "MAX_REQUEST_BODY_BYTES",
                str(int(os.getenv("MAX_UPLOAD_SIZE_BYTES", str(DEFAULT_MAX_UPLOAD_SIZE_BYTES))) + MULTIPART_OVERHEAD_BYTES),
            )
        )
    )
    max_concurrent_requests: int = Field(default_factory=lambda: int(os.getenv("MAX_CONCURRENT_REQUESTS", "2")))
    max_control_body_bytes: int = Field(default_factory=lambda: int(os.getenv("MAX_CONTROL_BODY_BYTES", str(1024 * 1024))))
    min_free_disk_bytes: int = Field(default_factory=lambda: int(os.getenv("MIN_FREE_DISK_BYTES", str(1024 * 1024 * 1024))))
    # A run refuses to start when the storage volume cannot transiently hold
    # ``source_bytes * disk_headroom_multiplier`` (staged target plus worst-case
    # quarantine). Raise it for pipelines that hold nearly every input row.
    disk_headroom_multiplier: float = Field(
        default_factory=lambda: float(
            os.getenv("DATASHELL_DISK_HEADROOM_MULTIPLIER", str(DEFAULT_DISK_HEADROOM_MULTIPLIER))
        )
    )

    # Boundary ledger: durable, tenant-isolated run history, quarantine index,
    # and usage metering. Retention of 0 keeps history forever; drift baselines
    # use a rolling window of prior delivered runs (1 = previous-run behaviour).
    history_retention_days: int = Field(default_factory=lambda: int(os.getenv("DATASHELL_HISTORY_RETENTION_DAYS", "0")))
    # Historical drift is opt-in. Cross-run comparisons are only meaningful once
    # a feed has a stable production history: a small test run recorded first
    # would otherwise become the baseline that a normal-sized delivery is
    # rejected against. Set DRIFT_ENABLED=true to activate drift gating.
    drift_enabled: bool = Field(
        default_factory=lambda: os.getenv("DRIFT_ENABLED", "false").strip().lower() in ("true", "1", "yes", "on")
    )
    # When drift is enabled and this is set, the stored baseline for the current
    # contract/tenant is cleared before evaluation, so this run establishes the
    # new comparison point. Intended for one-shot use after an intentional
    # volume/scope change; leave unset for normal operation.
    drift_baseline_reset: bool = Field(
        default_factory=lambda: os.getenv("DRIFT_BASELINE_RESET", "false").strip().lower() in ("true", "1", "yes", "on")
    )
    drift_baseline_window: int = Field(default_factory=lambda: int(os.getenv("DRIFT_BASELINE_WINDOW", "1")))
    # Drift thresholds. Volume drift compares delivered-row counts; metric drift
    # compares the per-feed aggregates persisted in the ledger (see
    # datashell.engines.metrics). Metric comparisons stay dormant until at least
    # ``drift_metric_min_baseline_samples`` delivered baselines exist, so the
    # first runs of a new feed establish history instead of blocking on noise.
    drift_max_volume_drop_pct: float = Field(
        default_factory=lambda: float(os.getenv("DRIFT_MAX_VOLUME_DROP_PCT", "30.0"))
    )
    drift_max_metric_drift_pct: float = Field(
        default_factory=lambda: float(os.getenv("DRIFT_MAX_METRIC_DRIFT_PCT", "50.0"))
    )
    # Rate/proportion metrics (for example null rates) are compared by absolute
    # delta in proportion points rather than a relative percentage, which is
    # undefined for zero and near-zero baselines. 0.10 allows a 10-point move.
    drift_max_rate_abs_delta: float = Field(
        default_factory=lambda: float(os.getenv("DRIFT_MAX_RATE_ABS_DELTA", "0.10"))
    )
    drift_metric_min_baseline_samples: int = Field(
        default_factory=lambda: int(os.getenv("DRIFT_METRIC_MIN_BASELINE_SAMPLES", "1"))
    )
    # Optional push integration: HMAC-signed run.completed events to an operator
    # endpoint so runbooks/SLAs can react without polling the ledger.
    run_webhook_url: Optional[str] = Field(
        default_factory=lambda: (os.getenv("DATASHELL_RUN_WEBHOOK_URL", "") or "").strip() or None
    )
    webhook_timeout_seconds: float = Field(default_factory=lambda: float(os.getenv("DATASHELL_WEBHOOK_TIMEOUT_SECONDS", "5")))

    # Settlement billing adapter. Dry-run (offline, deterministic) is the
    # default so no deployment can move money by accident. Stripe is opt-in:
    # it requires an explicit secret key, runs in test mode unless separately
    # authorized, creates draft invoices only, and never finalizes or collects.
    billing_provider: str = Field(
        default_factory=lambda: os.getenv("DATASHELL_BILLING_PROVIDER", "dry-run").strip().lower()
    )
    billing_stripe_secret_key: str = Field(
        default_factory=lambda: (os.getenv("DATASHELL_BILLING_STRIPE_SECRET_KEY", "") or "").strip()
    )
    billing_stripe_webhook_secret: str = Field(
        default_factory=lambda: (os.getenv("DATASHELL_BILLING_STRIPE_WEBHOOK_SECRET", "") or "").strip()
    )
    billing_stripe_customer_id: str = Field(
        default_factory=lambda: (os.getenv("DATASHELL_BILLING_STRIPE_CUSTOMER_ID", "") or "").strip()
    )
    billing_stripe_test_mode: bool = Field(
        default_factory=lambda: os.getenv("DATASHELL_BILLING_STRIPE_TEST_MODE", "true").strip().lower()
        in ("true", "1", "yes", "on")
    )

    @property
    def is_production_mode(self) -> bool:
        return is_production(self.environment)

    @property
    def history_db_path(self) -> Path:
        return (self.output_dir / "datashell_history.db").resolve()

    @property
    def security_db_path(self) -> Path:
        """Persistent credential registry path for single-replica production deployments."""
        return (self.output_dir / "security_registry.sqlite3").resolve()

    @staticmethod
    def tenant_storage_key(tenant_id: str) -> str:
        """Map tenant IDs to collision-free path components.

        Lowercase conventional IDs remain readable. Other IDs use lowercase hex
        UTF-8 encoding with a leading '~', which cannot occur in a plain ID. Hex
        encoding also avoids case-folding collisions on case-insensitive filesystems.
        """
        if not isinstance(tenant_id, str) or not tenant_id or tenant_id.strip() != tenant_id:
            raise ValueError("tenant_id must be a non-empty string without surrounding whitespace")
        tenant_bytes = tenant_id.encode("utf-8")
        if len(tenant_bytes) > 240 or "\x00" in tenant_id:
            raise ValueError("tenant_id is invalid or exceeds the safe filesystem identifier limit")
        if re.fullmatch(r"[a-z0-9_-]+", tenant_id):
            return tenant_id
        encoded = "~" + tenant_bytes.hex()
        if len(encoded) > 240:
            raise ValueError("tenant_id cannot be represented safely as a filesystem component")
        return encoded

    def get_tenant_delivery_dir(self, tenant_id: str) -> Path:
        """Return an isolated, symlink-safe root for tenant delivery artifacts."""
        key = self.tenant_storage_key(tenant_id)
        output_root = self.output_dir.resolve()
        tenant_root_candidate = output_root / "tenants"
        if tenant_root_candidate.is_symlink():
            raise RuntimeError("Tenant delivery root cannot be a symlink")
        tenant_root_candidate.mkdir(mode=0o700, parents=True, exist_ok=True)
        tenant_root = tenant_root_candidate.resolve()
        if not tenant_root.is_relative_to(output_root):
            raise RuntimeError("Tenant delivery root escapes the output storage root")
        p = tenant_root / key
        if p.is_symlink() or not p.resolve(strict=False).is_relative_to(tenant_root):
            raise RuntimeError("Tenant delivery directory escapes its storage root")
        p.mkdir(mode=0o700, parents=True, exist_ok=True)
        return p.resolve()

    def get_tenant_quarantine_dir(self, tenant_id: str) -> Path:
        """Return an isolated, symlink-safe root for tenant quarantine artifacts."""
        key = self.tenant_storage_key(tenant_id)
        quarantine_root = self.quarantine_dir.resolve()
        tenant_root_candidate = quarantine_root / "tenants"
        if tenant_root_candidate.is_symlink():
            raise RuntimeError("Tenant quarantine root cannot be a symlink")
        tenant_root_candidate.mkdir(mode=0o700, parents=True, exist_ok=True)
        tenant_root = tenant_root_candidate.resolve()
        if not tenant_root.is_relative_to(quarantine_root):
            raise RuntimeError("Tenant quarantine root escapes the quarantine storage root")
        p = tenant_root / key
        if p.is_symlink() or not p.resolve(strict=False).is_relative_to(tenant_root):
            raise RuntimeError("Tenant quarantine directory escapes its storage root")
        p.mkdir(mode=0o700, parents=True, exist_ok=True)
        return p.resolve()

    def model_post_init(self, __context) -> None:
        self.environment = normalize_environment(self.environment)
        # Validate secrets against the resolved model environment, not merely the
        # process ENVIRONMENT variable used by default factories. This prevents
        # programmatic construction with environment="production" from receiving
        # development-only ephemeral credentials.
        if self.is_production_mode:
            self.master_api_key = _resolve_secret(
                "DATASHELL_API_KEY", "Master API Key", self.environment,
                value_override=self.master_api_key if "master_api_key" in self.model_fields_set else None,
            )
            self.hmac_secret = _resolve_secret(
                "DATASHELL_HMAC_SECRET", "HMAC Signing Secret", self.environment,
                value_override=self.hmac_secret if "hmac_secret" in self.model_fields_set else None,
            )
            if secrets.compare_digest(self.master_api_key, self.hmac_secret):
                raise RuntimeError("Production API authentication and artifact-signing secrets must be distinct")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", self.active_key_id):
            raise RuntimeError("DATASHELL_KEY_ID must be 1-64 safe identifier characters")
        if not isinstance(self.trusted_hmac_keys, dict) or any(
            not isinstance(key_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", key_id)
            or not isinstance(secret, str) for key_id, secret in self.trusted_hmac_keys.items()
        ):
            raise RuntimeError("DATASHELL_HMAC_KEYRING must be a JSON object mapping safe key IDs to secrets")
        if self.is_production_mode:
            for key_id, secret in self.trusted_hmac_keys.items():
                _resolve_secret("DATASHELL_HMAC_KEYRING", f"trusted signing key '{key_id}'", self.environment, value_override=secret)
                if secrets.compare_digest(secret, self.master_api_key):
                    raise RuntimeError("Production API authentication and artifact-signing secrets must be distinct")
        self.trusted_hmac_keys = dict(self.trusted_hmac_keys)
        existing_active_secret = self.trusted_hmac_keys.get(self.active_key_id)
        if existing_active_secret is not None and not secrets.compare_digest(existing_active_secret, self.hmac_secret):
            raise RuntimeError(
                "DATASHELL_KEY_ID already exists in DATASHELL_HMAC_KEYRING with a different secret; "
                "use a new key ID when rotating signing keys"
            )
        self.trusted_hmac_keys[self.active_key_id] = self.hmac_secret
        self.trusted_key_ids = list(dict.fromkeys([*self.trusted_key_ids, *self.trusted_hmac_keys.keys(), self.active_key_id]))

        # Optional Ed25519 certificate signing: validate key material and bind the
        # derived public key into the trusted keyring so the engine can verify the
        # certificates it emits.
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", self.ed25519_active_key_id):
            raise RuntimeError("DATASHELL_ED25519_KEY_ID must be 1-64 safe identifier characters")
        if not isinstance(self.ed25519_trusted_keys, dict) or any(
            not isinstance(key_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", key_id)
            or not isinstance(pub, str) for key_id, pub in self.ed25519_trusted_keys.items()
        ):
            raise RuntimeError("DATASHELL_ED25519_KEYRING must be a JSON object mapping safe key IDs to public keys")
        self.ed25519_trusted_keys = dict(self.ed25519_trusted_keys)
        if self.ed25519_private_key:
            from datashell.core.keys import derive_public_key_material
            try:
                derived_public = derive_public_key_material(self.ed25519_private_key)
            except Exception as err:
                raise RuntimeError(f"DATASHELL_ED25519_PRIVATE_KEY is not a valid Ed25519 private key: {err}") from err
            existing = self.ed25519_trusted_keys.get(self.ed25519_active_key_id)
            if existing is not None and existing.strip() != derived_public.strip():
                raise RuntimeError(
                    "DATASHELL_ED25519_KEY_ID already exists in DATASHELL_ED25519_KEYRING with a different "
                    "public key; use a new key ID when rotating signing keys"
                )
            self.ed25519_trusted_keys[self.ed25519_active_key_id] = derived_public
        if self.is_production_mode and not self.auth_enabled:
            raise RuntimeError(
                f"FATAL: Production security violation. AUTH_ENABLED cannot be False when ENVIRONMENT='{self.environment}'. "
                "Refusing to disable authentication in production."
            )
        if self.max_upload_size_bytes < 1:
            raise RuntimeError("MAX_UPLOAD_SIZE_BYTES must be a positive integer")
        if self.stream_chunk_rows < 1:
            raise RuntimeError("STREAM_CHUNK_ROWS must be a positive integer")
        if self.stream_auto_threshold_bytes < 0:
            raise RuntimeError("STREAM_AUTO_THRESHOLD_BYTES cannot be negative")
        if self.rate_limit_requests_per_minute < 1:
            raise RuntimeError("RATE_LIMIT_REQUESTS_PER_MINUTE must be a positive integer")
        if self.max_concurrent_runs < 1:
            raise RuntimeError("MAX_CONCURRENT_RUNS must be a positive integer")
        if self.max_concurrent_requests < 1:
            raise RuntimeError("MAX_CONCURRENT_REQUESTS must be a positive integer")
        if self.max_control_body_bytes < 1024 or self.max_control_body_bytes > self.max_request_body_bytes:
            raise RuntimeError("MAX_CONTROL_BODY_BYTES must be between 1 KiB and MAX_REQUEST_BODY_BYTES")
        if self.min_free_disk_bytes < 0:
            raise RuntimeError("MIN_FREE_DISK_BYTES cannot be negative")
        if self.disk_headroom_multiplier <= 0:
            raise RuntimeError("DATASHELL_DISK_HEADROOM_MULTIPLIER must be positive")
        if self.history_retention_days < 0:
            raise RuntimeError("DATASHELL_HISTORY_RETENTION_DAYS cannot be negative")
        if self.drift_baseline_window < 1:
            raise RuntimeError("DRIFT_BASELINE_WINDOW must be a positive integer")
        if self.drift_max_volume_drop_pct < 0:
            raise RuntimeError("DRIFT_MAX_VOLUME_DROP_PCT cannot be negative")
        if self.drift_max_metric_drift_pct < 0:
            raise RuntimeError("DRIFT_MAX_METRIC_DRIFT_PCT cannot be negative")
        if not (0.0 < self.drift_max_rate_abs_delta <= 1.0):
            raise RuntimeError("DRIFT_MAX_RATE_ABS_DELTA must be greater than 0 and at most 1")
        if self.drift_metric_min_baseline_samples < 1:
            raise RuntimeError("DRIFT_METRIC_MIN_BASELINE_SAMPLES must be a positive integer")
        if self.webhook_timeout_seconds <= 0:
            raise RuntimeError("DATASHELL_WEBHOOK_TIMEOUT_SECONDS must be positive")
        if self.billing_provider not in ("dry-run", "stripe"):
            raise RuntimeError("DATASHELL_BILLING_PROVIDER must be 'dry-run' or 'stripe'")
        if self.billing_provider == "stripe":
            if not self.billing_stripe_secret_key:
                raise RuntimeError("DATASHELL_BILLING_STRIPE_SECRET_KEY is required for the stripe provider")
            if self.is_production_mode and self.billing_stripe_test_mode:
                raise RuntimeError(
                    "DATASHELL_BILLING_STRIPE_TEST_MODE=true is forbidden in production; "
                    "disable test mode only with an explicit production authorization"
                )
        if self.run_webhook_url:
            from urllib.parse import urlsplit
            parsed_webhook = urlsplit(self.run_webhook_url)
            if parsed_webhook.scheme not in ("http", "https") or not parsed_webhook.netloc:
                raise RuntimeError("DATASHELL_RUN_WEBHOOK_URL must be an absolute http(s) URL")
            if self.is_production_mode and parsed_webhook.scheme != "https":
                raise RuntimeError("DATASHELL_RUN_WEBHOOK_URL must use https in production")
        if self.max_request_body_bytes < self.max_upload_size_bytes + MULTIPART_OVERHEAD_BYTES:
            raise RuntimeError("MAX_REQUEST_BODY_BYTES must allow MAX_UPLOAD_SIZE_BYTES plus multipart overhead (at least 1 MiB)")
        memory_match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(B|KB|MB|GB|TB)", self.duckdb_memory_limit, re.IGNORECASE)
        if not memory_match or float(memory_match.group(1)) <= 0:
            raise RuntimeError("DUCKDB_MEMORY_LIMIT must be a positive size such as 512MB or 1GB")
        origins = [origin.strip() for origin in self.allowed_origins if origin.strip()]
        if self.is_production_mode and (not origins or "*" in origins):
            raise RuntimeError(
                "FATAL: Production security violation. Set ALLOWED_ORIGINS to an explicit comma-separated "
                "allowlist of trusted browser origins; wildcard or empty CORS configuration is forbidden."
            )
        if self.is_production_mode:
            import ipaddress
            if not self.trusted_proxy_ips or "*" in self.trusted_proxy_ips:
                raise RuntimeError("FORWARDED_ALLOW_IPS must contain explicit trusted proxy IPs/CIDRs; wildcard trust is forbidden")
            for proxy in self.trusted_proxy_ips:
                try:
                    ipaddress.ip_network(proxy, strict=False)
                except ValueError as exc:
                    raise RuntimeError("FORWARDED_ALLOW_IPS contains an invalid IP address or CIDR") from exc
            from urllib.parse import urlsplit
            for origin in origins:
                parsed = urlsplit(origin)
                if parsed.scheme != "https" or not parsed.netloc or parsed.path or parsed.query or parsed.fragment:
                    raise RuntimeError(
                        "FATAL: Each production ALLOWED_ORIGINS entry must be an exact HTTPS origin "
                        "without a path, query, or fragment."
                    )
        self.allowed_origins = origins

    def init_directories(self) -> None:
        """Ensure runtime directories exist with private permissions by default."""
        # Uploaded data, tenant outputs, certificates, contracts, and quarantine
        # files can contain sensitive customer information. Restrictive umask
        # also keeps newly created artifacts private (0600 by default).
        os.umask(0o077)
        for path in [self.data_dir, self.contracts_dir, self.output_dir, self.audit_dir, self.quarantine_dir]:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                path.chmod(0o700)
            except OSError:
                # Startup/readiness checks will fail if required paths are not writable.
                pass
        # Ensure the scratch directory exists so large uploads and out-of-core
        # database spills land on it. Never chmod an operator-supplied system
        # temp directory (for example /tmp) that we do not own.
        self.temp_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.temp_dir.is_relative_to(self.storage_root):
            try:
                self.temp_dir.chmod(0o700)
            except OSError:
                pass


settings = EnterpriseSettings()
settings.init_directories()

