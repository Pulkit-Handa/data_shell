"""Enterprise Security: RBAC, Multi-tenancy, Dynamic Key Management, and Rate Limiting."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from collections.abc import Callable
from enum import StrEnum
from typing import Any, cast

from fastapi import HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader

from datashell.core.config import is_production, settings


class Role(StrEnum):
    ADMIN = "ADMIN"
    DATA_ENGINEER = "DATA_ENGINEER"
    PRODUCER = "PRODUCER"
    AUDITOR = "AUDITOR"


class Capability(StrEnum):
    CONTRACTS_READ = "contracts:read"
    CONTRACTS_WRITE = "contracts:write"
    PIPELINE_ENFORCE = "pipeline:enforce"
    AUDIT_READ = "audit:read"
    METRICS_READ = "metrics:read"
    ADMIN_MANAGE = "admin:manage"
    SETTLEMENTS_READ = "settlements:read"
    SETTLEMENTS_WRITE = "settlements:write"
    BILLING_MANAGE = "billing:manage"


ROLE_CAPABILITIES: dict[Role, set[Capability]] = {
    Role.ADMIN: {
        Capability.CONTRACTS_READ,
        Capability.CONTRACTS_WRITE,
        Capability.PIPELINE_ENFORCE,
        Capability.AUDIT_READ,
        Capability.METRICS_READ,
        Capability.ADMIN_MANAGE,
        Capability.SETTLEMENTS_READ,
        Capability.SETTLEMENTS_WRITE,
        Capability.BILLING_MANAGE,
    },
    Role.DATA_ENGINEER: {
        Capability.CONTRACTS_READ,
        Capability.CONTRACTS_WRITE,
        Capability.PIPELINE_ENFORCE,
        Capability.AUDIT_READ,
        Capability.METRICS_READ,
        Capability.SETTLEMENTS_READ,
        Capability.SETTLEMENTS_WRITE,
        Capability.BILLING_MANAGE,
    },
    Role.PRODUCER: {
        Capability.CONTRACTS_READ,
        Capability.PIPELINE_ENFORCE,
    },
    Role.AUDITOR: {
        Capability.CONTRACTS_READ,
        Capability.AUDIT_READ,
        Capability.METRICS_READ,
        Capability.SETTLEMENTS_READ,
    },
}


class APIKeyMetadata:
    def __init__(self, key: str, tenant_id: str, role: Role, description: str) -> None:
        # Validate the identity once when a credential is constructed. The exact
        # tenant_id remains the security principal; encoding is only for paths.
        settings.tenant_storage_key(tenant_id)
        self.key = key
        self.tenant_id = tenant_id
        self.role = role
        self.description = description
        self.capabilities: set[Capability] = ROLE_CAPABILITIES.get(role, set()).copy()


# In-memory cache preserves the existing public API for tests and fast lookups.
# In production, dynamically managed keys are also stored as SHA-256 fingerprints
# in a durable SQLite registry. Raw API keys are never written to disk.
API_KEY_STORE: dict[str, APIKeyMetadata] = {}
_API_KEY_LOCK = threading.RLock()


def _key_fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _security_connection(*, initialize: bool = True) -> sqlite3.Connection:
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.security_db_path, timeout=5.0, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=5000")
    if initialize:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""CREATE TABLE IF NOT EXISTS api_credentials (
            key_hash TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            role TEXT NOT NULL,
            description TEXT NOT NULL,
            created_at REAL NOT NULL,
            revoked_at REAL,
            active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1))
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS security_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            actor_tenant_id TEXT NOT NULL,
            target_tenant_id TEXT NOT NULL,
            key_id TEXT NOT NULL,
            role TEXT NOT NULL,
            description TEXT NOT NULL,
            event_timestamp REAL NOT NULL
        )""")
        with contextlib.suppress(OSError):
            os.chmod(settings.security_db_path, 0o600)
    return conn


def register_api_key(metadata: APIKeyMetadata, *, persist: bool | None = None, actor_tenant_id: str = "system") -> None:
    """Register a key; production runtime-managed keys are persisted by fingerprint."""
    if not metadata.key or not isinstance(metadata.key, str):
        raise ValueError("API key must be a non-empty string")
    should_persist = settings.is_production_mode if persist is None else persist
    with _API_KEY_LOCK:
        if should_persist:
            conn = _security_connection()
            key_hash = _key_fingerprint(metadata.key)
            timestamp = time.time()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO api_credentials(key_hash, tenant_id, role, description, "
                    "created_at, revoked_at, active) "
                    "VALUES (?, ?, ?, ?, ?, NULL, 1) ON CONFLICT(key_hash) DO UPDATE SET "
                    "tenant_id=excluded.tenant_id, role=excluded.role, description=excluded.description, "
                    "revoked_at=NULL, active=1",
                    (key_hash, metadata.tenant_id, metadata.role.value, metadata.description, timestamp),
                )
                conn.execute(
                    "INSERT INTO security_events(event_type, actor_tenant_id, "
                    "target_tenant_id, key_id, role, description, event_timestamp) "
                    "VALUES ('created', ?, ?, ?, ?, ?, ?)",
                    (actor_tenant_id, metadata.tenant_id, key_hash[:24],
                     metadata.role.value, metadata.description, timestamp),
                )
                conn.execute("COMMIT")
            except Exception:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()
        API_KEY_STORE[metadata.key] = metadata


def revoke_api_key(key: str, *, actor_tenant_id: str = "system") -> bool:
    """Revoke a runtime-managed key; production revocation is durable-first."""
    with _API_KEY_LOCK:
        if settings.is_production_mode:
            conn = _security_connection()
            key_hash = _key_fingerprint(key)
            timestamp = time.time()
            try:
                conn.execute("BEGIN IMMEDIATE")
                cur = conn.execute(
                    "UPDATE api_credentials SET active=0, revoked_at=? WHERE key_hash=? AND active=1",
                    (timestamp, key_hash),
                )
                if cur.rowcount > 0:
                    row = conn.execute(
                        "SELECT tenant_id, role, description FROM api_credentials WHERE key_hash=?",
                        (key_hash,),
                    ).fetchone()
                    conn.execute(
                        "INSERT INTO security_events(event_type, actor_tenant_id, "
                        "target_tenant_id, key_id, role, description, event_timestamp) "
                        "VALUES ('revoked', ?, ?, ?, ?, ?, ?)",
                        (actor_tenant_id, row[0], key_hash[:24], row[1], row[2], timestamp),
                    )
                    conn.execute("COMMIT")
                    API_KEY_STORE.pop(key, None)
                    return True
                conn.execute("ROLLBACK")
                # Environment-provisioned credentials are rotated through the
                # deployment secret source, not revoked through this API.
                return False
            except Exception:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()
        return API_KEY_STORE.pop(key, None) is not None


def revoke_api_key_id(key_id: str, *, actor_tenant_id: str = "system") -> bool:
    """Revoke a managed key by its non-secret 24-hex-character fingerprint prefix."""
    if not isinstance(key_id, str) or not __import__("re").fullmatch(r"[0-9a-f]{24}", key_id):
        return False
    with _API_KEY_LOCK:
        if not settings.is_production_mode:
            for raw_key in list(API_KEY_STORE):
                if _key_fingerprint(raw_key).startswith(key_id):
                    return revoke_api_key(raw_key)
            return False
        conn = _security_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT key_hash, tenant_id, role, description FROM api_credentials "
                "WHERE substr(key_hash, 1, 24)=? AND active=1", (key_id,)
            ).fetchall()
            if len(rows) != 1:
                conn.execute("ROLLBACK")
                return False
            key_hash, tenant_id, role, description = rows[0]
            timestamp = time.time()
            conn.execute("UPDATE api_credentials SET active=0, revoked_at=? WHERE key_hash=?", (timestamp, key_hash))
            conn.execute(
                "INSERT INTO security_events(event_type, actor_tenant_id, "
                "target_tenant_id, key_id, role, description, event_timestamp) "
                "VALUES ('revoked', ?, ?, ?, ?, ?, ?)",
                (actor_tenant_id, tenant_id, key_hash[:24], role, description, timestamp),
            )
            conn.execute("COMMIT")
            for raw_key in list(API_KEY_STORE):
                if _key_fingerprint(raw_key) == key_hash:
                    API_KEY_STORE.pop(raw_key, None)
            return True
        except Exception:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()


def get_api_key_metadata(key: str) -> APIKeyMetadata | None:
    """Resolve a credential from memory or the durable production registry."""
    with _API_KEY_LOCK:
        metadata = API_KEY_STORE.get(key)
        if metadata is not None:
            return metadata
        if not settings.is_production_mode:
            return None
        conn = _security_connection(initialize=False)
        try:
            row = conn.execute(
                "SELECT tenant_id, role, description FROM api_credentials WHERE key_hash=? AND active=1",
                (_key_fingerprint(key),),
            ).fetchone()
            if not row:
                return None
            return APIKeyMetadata(key=key, tenant_id=row[0], role=Role(row[1]), description=row[2])
        except sqlite3.Error as exc:
            raise HTTPException(status_code=503, detail="Credential registry is unavailable") from exc
        finally:
            conn.close()


def security_registry_ready() -> bool:
    """Return whether the production credential database is queryable and consistent."""
    if not settings.is_production_mode:
        return True
    conn = _security_connection(initialize=False)
    try:
        row = conn.execute("PRAGMA quick_check").fetchone()
        if not row or row[0] != "ok":
            return False
        conn.execute("SELECT COUNT(*) FROM api_credentials").fetchone()
        conn.execute("SELECT COUNT(*) FROM security_events").fetchone()
        return True
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def init_security_store() -> None:
    """Initialize configured credentials; runtime-managed production keys persist separately."""
    API_KEY_STORE.clear()
    if settings.master_api_key:
        register_api_key(APIKeyMetadata(
            key=settings.master_api_key,
            tenant_id="enterprise-primary",
            role=Role.ADMIN,
            description="Master Platform Administrator Key"
        ), persist=False)

    custom_keys_json = os.getenv("DATASHELL_API_KEYS")
    if custom_keys_json:
        try:
            parsed = json.loads(custom_keys_json)
            if not isinstance(parsed, list):
                raise ValueError("DATASHELL_API_KEYS must be a JSON array")
            seen = set()
            for entry in parsed:
                if not isinstance(entry, dict):
                    raise ValueError("Each DATASHELL_API_KEYS entry must be an object")
                key_value = entry.get("key")
                if not isinstance(key_value, str) or not key_value.strip():
                    raise ValueError("Each API key must be a non-empty string")
                if key_value in seen or key_value == settings.master_api_key:
                    raise ValueError("Duplicate API key configured")
                seen.add(key_value)
                if settings.is_production_mode and len(key_value) < 32:
                    raise ValueError("Production tenant API keys must be at least 32 characters")
                role_val = Role(entry.get("role", "PRODUCER"))
                register_api_key(APIKeyMetadata(
                    key=key_value,
                    tenant_id=entry.get("tenant_id", "default-tenant"),
                    role=role_val,
                    description=entry.get("description", "Injected Tenant Key")
                ), persist=False)
        except Exception as e:
            if settings.is_production_mode:
                raise RuntimeError("Invalid DATASHELL_API_KEYS configuration; refusing production startup") from e
            from datashell.core.telemetry import telemetry_logger
            telemetry_logger.warning("Failed to parse DATASHELL_API_KEYS configuration", error_type=type(e).__name__)


# Initialize security store on module import. In production, validate that the
# persistent registry can be opened before the API reports healthy.
init_security_store()
if settings.is_production_mode:
    _connection = _security_connection()
    _connection.close()



class TokenBucketRateLimiter:
    """Thread-safe Token Bucket rate limiter per API key / IP."""
    def __init__(self, rate_per_minute: int = 120):
        self.capacity = float(rate_per_minute)
        self.refill_rate = self.capacity / 60.0  # tokens per second
        self.buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, last_update)
        self._lock = threading.Lock()
        self._last_cleanup = time.monotonic()

    def allow(self, identifier: str) -> bool:
        with self._lock:
            now = time.monotonic()
            # Prevent unbounded growth when many one-off tokens/IPs hit the API.
            # Entries idle for ten minutes are fully refilled and can be discarded.
            if now - self._last_cleanup >= 60.0:
                cutoff = now - 600.0
                self.buckets = {key: value for key, value in self.buckets.items() if value[1] >= cutoff}
                self._last_cleanup = now
            tokens, last_update = self.buckets.get(identifier, (self.capacity, now))
            
            # Refill tokens based on elapsed time
            elapsed = now - last_update
            tokens = min(self.capacity, tokens + elapsed * self.refill_rate)
            
            if tokens >= 1.0:
                self.buckets[identifier] = (tokens - 1.0, now)
                return True
            else:
                self.buckets[identifier] = (tokens, now)
                return False


rate_limiter = TokenBucketRateLimiter(settings.rate_limit_requests_per_minute)
# Invalid credential attempts are rate-limited by client address before any
# persistent registry lookup, limiting unauthenticated database pressure.
auth_attempt_limiter = TokenBucketRateLimiter(settings.rate_limit_requests_per_minute)
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

# Unauthenticated caller for local testing when auth_enabled=False.
# CRITICAL: Never represent an unauthenticated caller as Role.ADMIN.
API_KEY_METADATA_UNAUTHENTICATED = APIKeyMetadata(
    key="unauthenticated-dev",
    tenant_id="default-tenant",
    role=Role.PRODUCER,
    description="Development Mode (Unauthenticated - Non-Admin)"
)

# Anonymous caller for public health check and docs (holds ZERO capabilities)
API_KEY_METADATA_ANONYMOUS = APIKeyMetadata(
    key="anonymous",
    tenant_id="public",
    role=Role.AUDITOR,
    description="Anonymous Public Route"
)
# Strip all capabilities from anonymous user
API_KEY_METADATA_ANONYMOUS.capabilities = set()


def preflight_authenticate_scope(
    scope: dict[str, Any],
) -> tuple[int | None, APIKeyMetadata | None, str | None]:
    """Authenticate a body-bearing ASGI request before multipart parsing begins.

    Returns (status_code, metadata, detail). A None status means authentication
    succeeded; the middleware stores metadata in ASGI scope state so the normal
    FastAPI dependency does not consume the rate-limit token twice.
    """
    if not settings.auth_enabled:
        if settings.is_production_mode:
            return 500, None, "Authentication bypass is forbidden in production"
        return None, API_KEY_METADATA_UNAUTHENTICATED, ""

    method = str(scope.get("method", "GET")).upper()
    path = str(scope.get("path", ""))
    settlements_write = (
        method == "POST" and (
            path == "/api/v1/commercial-contracts"
            or path == "/api/v1/settlements/calculate"
            or (path.startswith("/api/v1/settlements/") and path.endswith("/disputes"))
        )
    )
    settlements_read = (
        method == "POST" and path.startswith("/api/v1/settlements/") and (
            path.endswith("/billing-preview") or path.endswith("/verify")
        )
    )
    admin_delete_key = (
        method == "DELETE"
        and re.fullmatch(r"/api/v1/admin/keys/[0-9a-f]{24}", path) is not None
    )
    if method == "POST" and path == "/api/v1/contracts":
        required_capability = Capability.CONTRACTS_WRITE
    elif method == "POST" and path == "/api/v1/enforce":
        required_capability = Capability.PIPELINE_ENFORCE
    elif (method == "POST" and path == "/api/v1/admin/keys") or admin_delete_key:
        required_capability = Capability.ADMIN_MANAGE
    elif settlements_write:
        required_capability = Capability.SETTLEMENTS_WRITE
    elif method == "POST" and path.startswith("/api/v1/settlements/") and path.endswith("/approvals"):
        required_capability = Capability.BILLING_MANAGE
    elif settlements_read:
        required_capability = Capability.SETTLEMENTS_READ
    else:
        return 404, None, "Not found"

    headers = {key.lower(): value for key, value in scope.get("headers", [])}
    client = scope.get("client") or ("unknown", 0)
    client_ip = str(client[0])

    token = headers.get(b"x-api-key", b"").decode("utf-8", errors="ignore").strip()
    if not token:
        authorization = headers.get(b"authorization", b"").decode("utf-8", errors="ignore")
        if authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()
    if not token:
        if not auth_attempt_limiter.allow(f"ip:{client_ip}"):
            return 429, None, "Rate limit exceeded. Too many authentication attempts."
        return 401, None, "Invalid or missing X-API-Key header"
    metadata = get_api_key_metadata(token)
    if metadata is None:
        if not auth_attempt_limiter.allow(f"ip:{client_ip}"):
            return 429, None, "Rate limit exceeded. Too many authentication attempts."
        return 401, None, "Invalid or missing X-API-Key header"
    if required_capability not in metadata.capabilities:
        # Kept identical to require_capability() so pre-authorised (middleware)
        # and dependency-based denials are indistinguishable to the caller. The
        # message names the missing capability and notes it is required.
        return 403, None, (
            f"Access denied: role '{metadata.role.value}' lacks capability "
            f"'{required_capability.value}' (a required capability)"
        )
    if not rate_limiter.allow(token):
        return 429, None, "Rate limit exceeded. Too many requests."
    return None, metadata, ""


def authenticate_client(
    request: Request,
    api_key: str | None = Security(api_key_header)
) -> APIKeyMetadata:
    """Authenticates API Key from headers only and checks rate limits."""
    # Body-bearing requests are pre-authenticated by ASGI middleware before the
    # request parser can spool a multipart upload to disk. ``request`` is not
    # guaranteed to expose Starlette's ``.state`` (e.g. lightweight test doubles
    # or non-Starlette callers), so resolve the attribute defensively.
    preauthenticated = cast(
        APIKeyMetadata,
        getattr(getattr(request, "state", None), "preauthenticated_client", None),
    )
    if preauthenticated is not None:
        return preauthenticated

    # Production invariant: authentication cannot be bypassed
    if not settings.auth_enabled:
        if settings.is_production_mode or is_production(settings.environment):
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="FATAL: Authentication bypass is forbidden in production environment."
            )
        return API_KEY_METADATA_UNAUTHENTICATED

    # Header-only authentication: strictly disallow query parameters to prevent log leakage
    token = api_key
    if not token:
        auth_hdr = request.headers.get("Authorization", "")
        if auth_hdr.lower().startswith("bearer "):
            token = auth_hdr[7:].strip()
    
    # If no token provided, check if accessing public routes (health, root UI, docs)
    # Notice: /metrics is NOT public; it requires authentication!
    path = request.url.path
    if path in ("/", "/health", "/ready", "/docs", "/openapi.json"):
        return API_KEY_METADATA_ANONYMOUS

    client_ip = request.client.host if request.client else "unknown"
    metadata = get_api_key_metadata(token) if token else None
    if metadata is None:
        if not auth_attempt_limiter.allow(f"ip:{client_ip}"):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded. Too many authentication attempts.",
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-API-Key header. Provide a valid enterprise authentication credential."
        )

    # Check Rate Limiter
    if not rate_limiter.allow(token or "unknown"):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded. Too many requests. Please throttle."
        )

    return metadata



def require_capability(required: Capability) -> Callable[..., APIKeyMetadata]:
    """Dependency factory ensuring authenticated client holds required capability."""
    def check_cap(meta: APIKeyMetadata = Security(authenticate_client)) -> APIKeyMetadata:
        if required not in meta.capabilities:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Access denied: role '{meta.role.value}' lacks capability "
                    f"'{required.value}' (a required capability)"
                )
            )
        return meta
    return check_cap


def verify_tenant_access(resource_tenant_id: str, client: APIKeyMetadata) -> None:
    """
    Enforces tenant data isolation:
    Clients may only view/modify resources belonging to their own tenant,
    unless they hold the ADMIN role with cross-tenant management authority.
    """
    if client.role == Role.ADMIN or Capability.ADMIN_MANAGE in client.capabilities:
        return
    if client.tenant_id != resource_tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cross-tenant access violation: this credential is not authorized to access the requested resource."
        )
